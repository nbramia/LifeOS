"""Haiku preflight call that classifies an #agent task before execution.

A single, cheap Claude Haiku call returns structured JSON describing:
- the budget the task should run with (parsed from natural-language hints)
- which executor to use (local Gemma vs. Claude Opus vs. ask the user)
- whether the title is ambiguous (and what clarifying question to ask)
- the expected output shape (used to phrase the completion notification)
- a sanity flag (garbage / destructive titles get parked rather than run)

The model is pinned to `claude-haiku-4-5` by default via
`LIFEOS_AGENT_PREFLIGHT_MODEL`. The function takes a callable
`call_llm(prompt) -> str` so tests can inject a stub instead of mocking the
SDK; production wiring lives in `_default_llm_caller`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from config.settings import settings


logger = logging.getLogger(__name__)


# Routing destinations. Anything else from the model is treated as `ask`.
ROUTE_LOCAL = "local"
ROUTE_CLAUDE = "claude"
ROUTE_ASK = "ask"

# Allowed expected-output shapes. Used to phrase the final Telegram summary.
OUTPUT_KINDS = ("text", "file", "external_action", "structured")

# Per-task model identifiers emitted by preflight. The cloud routing case
# defaults to Sonnet for general work; Haiku is selected by tag override (or
# in future by smart routing — see #139 §2 rubric). "local" maps to the
# local Gemma backend. None means "no override, use settings.agent_managed_model".
MODEL_LOCAL = "local"
MODEL_HAIKU = "claude-haiku-4-5"
MODEL_SONNET = "claude-sonnet-4-6"
ALLOWED_MODELS = (MODEL_LOCAL, MODEL_HAIKU, MODEL_SONNET)


@dataclass
class PreflightBudget:
    wall_seconds: int
    max_tokens: int
    max_dollars: float


@dataclass
class PreflightAmbiguity:
    question: str


@dataclass
class PreflightResult:
    budget: PreflightBudget
    routing: str  # one of ROUTE_LOCAL / ROUTE_CLAUDE / ROUTE_ASK
    routing_reason: str
    expected_output: str
    ambiguity: PreflightAmbiguity | None = None
    sane: bool = True
    sane_reason: str = ""
    # Per-task model selection (#139 §2). For cloud routes, defaults to
    # MODEL_SONNET; can be overridden to MODEL_HAIKU by the `#cloud-haiku`
    # tag (or to MODEL_SONNET by `#cloud-sonnet`). For local routes, set
    # to MODEL_LOCAL. The worker uses this for client-side cost accounting;
    # actual remote-model selection still requires the agent preset to
    # match (section 3 territory).
    model: str | None = None
    # Preset class for per-session tool filtering (#139 §3). When set, the
    # managed executor will call driver.update_session() with the class's
    # filtered tool list (via tool_filter.class_to_tool_filter) between
    # session create and the first user message — scoping cache_creation
    # to the smaller tool set. Currently picked from tag overrides only;
    # LLM-side preflight emission is a follow-up.
    preset_class: str | None = None
    raw: dict = field(default_factory=dict)  # the parsed JSON for debugging


# ---------------------------------------------------------------------------
# Defaults + prompt
# ---------------------------------------------------------------------------

def _defaults() -> PreflightBudget:
    return PreflightBudget(
        wall_seconds=settings.agent_default_wall_seconds,
        max_tokens=settings.agent_default_max_tokens,
        max_dollars=settings.agent_default_budget_dollars,
    )


_PREFLIGHT_INSTRUCTIONS = """\
You are the preflight classifier for an external agent worker. Given a task
title (and tag list) from a user's task manager, decide how it should run.

Reply with a single JSON object, no prose, matching this exact schema:

{{
  "budget": {{
    "wall_seconds": <int>,
    "max_tokens": <int>,
    "max_dollars": <float>
  }},
  "routing": "local" | "claude" | "ask",
  "routing_reason": "<one short sentence>",
  "expected_output": "text" | "file" | "external_action" | "structured",
  "ambiguity": null | {{"question": "<one clarifying question>"}},
  "sane": <true|false>,
  "sane_reason": "<one short sentence; empty string if sane>"
}}

Rules:
- Default budget if the title doesn't specify one: wall_seconds={default_wall}, max_tokens={default_tokens}, max_dollars={default_dollars}.
- Parse natural-language budget hints from the title: "5 min" / "30s" / "1h" → wall_seconds; "max $0.50" → max_dollars; "10k tokens" / "50000 tokens" → max_tokens. Be reasonable about unit conversions.
- Routing precedence (apply in order; first match wins):
    1) If the tag list contains "local" → routing="local"; routing_reason="#local tag present".
    2) If the tag list contains "cloud" → routing="claude"; routing_reason="#cloud tag present".
    3) Otherwise, look at the title for explicit model cues:
       - "with local agent", "using gemma" → "local"
       - "use claude", "with opus", "with claude opus", "with sonnet" → "claude"
    4) Otherwise, infer from capability cues in the title — tasks that
       require third-party cloud connectors should route to claude even
       without an explicit cue. Use semantic judgment, not bare keyword
       matching. Trigger only when the title clearly implies an action
       against one of these systems:
       - email actions: "search my gmail", "draft a reply", "send an
         email", "check my inbox", "summarize my superhuman threads"
       - calendar actions: "events today", "my calendar", "schedule a
         meeting", "find free time on my calendar", "book a meeting"
       - drive / docs: "google drive", "shared drive", "my drive", "a
         google doc", "find a spreadsheet"
       - workplace systems: "slack", "asana", "ramp", "granola" (these
         only appear in workplace contexts; the bare word is usually safe)
       In each case set routing="claude"; routing_reason="implies <capability>".
       Do NOT trigger on bare nouns where the meaning is ambiguous —
       e.g., "drive home" (vehicle), "book recommendation" (literature),
       "email signature design" (general design task).
    5) If none of the above match, set routing="ask" (the worker will
       ask the operator which model).
- expected_output: classify what the agent will produce.
    "text" = a written answer; "file" = creates/edits a file; "external_action" = sends an email, posts a message, schedules a meeting; "structured" = returns structured data the caller will parse.
- ambiguity: leave null in nearly all cases. The agent is autonomous and is expected to make reasonable assumptions, try one approach, and fall back to another if the first didn't work. Only set non-null when the title would *prevent* the agent from acting at all — e.g., "reply to John" with no John in scope, or "send the contract to her" with no antecedent for "her". Method-of-execution questions ("should I use web search or local data?", "which calendar?", "what format?") are NOT ambiguity — the agent picks one and adapts. 
- sane: set to false ONLY for empty/garbage titles or obviously destructive shapes (e.g., "rm -rf /", "delete all my data"). Mundane tasks are sane.

Tag list and title follow. Return ONLY the JSON.
"""


def build_preflight_prompt(title: str, tags: list[str]) -> str:
    defaults = _defaults()
    instructions = _PREFLIGHT_INSTRUCTIONS.format(
        default_wall=defaults.wall_seconds,
        default_tokens=defaults.max_tokens,
        default_dollars=defaults.max_dollars,
    )
    return f"{instructions}\nTAGS: {tags}\nTITLE: {title.strip()}"


# ---------------------------------------------------------------------------
# Parsing the model's reply
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_preflight_response(text: str) -> PreflightResult:
    """Parse the LLM's reply into a PreflightResult.

    Hardens against the common failure modes: ```json``` fences, leading prose,
    missing keys, wrong types. On any parse failure we return a "sane=false"
    result so the worker parks the task rather than running with junk.
    """
    raw: dict = {}
    try:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise ValueError("no JSON object in preflight reply")
        raw = json.loads(match.group(0))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("preflight reply unparseable: %s; reply was: %r", exc, text[:300])
        defaults = _defaults()
        return PreflightResult(
            budget=defaults,
            routing=ROUTE_ASK,
            routing_reason="preflight could not parse classifier output",
            expected_output="text",
            ambiguity=None,
            sane=False,
            sane_reason=f"preflight parse error: {exc}",
            raw={},
        )

    defaults = _defaults()
    budget_raw = raw.get("budget") or {}
    budget = PreflightBudget(
        wall_seconds=int(budget_raw.get("wall_seconds", defaults.wall_seconds)),
        max_tokens=int(budget_raw.get("max_tokens", defaults.max_tokens)),
        max_dollars=float(budget_raw.get("max_dollars", defaults.max_dollars)),
    )

    routing = raw.get("routing", ROUTE_ASK)
    if routing not in (ROUTE_LOCAL, ROUTE_CLAUDE, ROUTE_ASK):
        routing = ROUTE_ASK

    expected_output = raw.get("expected_output", "text")
    if expected_output not in OUTPUT_KINDS:
        expected_output = "text"

    ambiguity = None
    amb_raw = raw.get("ambiguity")
    if isinstance(amb_raw, dict) and amb_raw.get("question"):
        ambiguity = PreflightAmbiguity(question=str(amb_raw["question"]))

    return PreflightResult(
        budget=budget,
        routing=routing,
        routing_reason=str(raw.get("routing_reason", "")),
        expected_output=expected_output,
        ambiguity=ambiguity,
        sane=bool(raw.get("sane", True)),
        sane_reason=str(raw.get("sane_reason", "")),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

PreflightCaller = Callable[[str], str]


def _default_llm_caller(prompt: str) -> str:
    """Production caller: hit the Anthropic API via the existing llm_client."""
    # Import inside the function so tests that monkeypatch run_preflight don't
    # need the Anthropic SDK installed.
    from api.services.llm_client import AnthropicLLMClient

    client = AnthropicLLMClient(model=settings.agent_preflight_model)
    response = client.create(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
        temperature=0.0,
    )
    return response.text


def _normalize_tag(tag: str) -> str:
    """Strip leading `#` and lowercase, so callers can pass either form."""
    return tag.lstrip("#").lower()


# Preset-class tag → class name. Mirrors tool_filter.ALL_PRESET_CLASSES so
# operators can force a class with `#research`, `#crm`, etc. Kept here as
# a local map instead of an import so preflight stays decoupled from the
# tool_filter implementation; the names are the contract.
_PRESET_CLASS_TAGS = {
    "personal-comm": "personal-comm",
    "work-comm": "work-comm",
    "research": "research",
    "financial": "financial",
    "crm": "crm",
    "fullstack": "fullstack",
}


def _detect_preset_class_from_tags(tags: list[str]) -> str | None:
    """Return the preset_class implied by a `#<class>` tag, or None.

    First match wins in the tag list order to give operators a predictable
    override when multiple class tags slip in by mistake.
    """
    for raw in tags or []:
        normalized = _normalize_tag(raw)
        if normalized in _PRESET_CLASS_TAGS:
            return _PRESET_CLASS_TAGS[normalized]
    return None


def _apply_tag_overrides(result: PreflightResult, tags: list[str]) -> PreflightResult:
    """Apply tag-based routing/model overrides (#139 §2 precedence).

    Tag precedence (a tag always wins over preflight's LLM choice):
      `#local`        → routing=local, model=local
      `#cloud-haiku`  → routing=claude, model=claude-haiku-4-5
      `#cloud-sonnet` → routing=claude, model=claude-sonnet-4-6
      `#cloud`        → routing=claude, model=(whatever preflight picked, else Sonnet)

    Returns a new PreflightResult so the caller can chain. Tag list is
    normalized case-insensitively with optional leading `#`.
    """
    normalized = {_normalize_tag(t) for t in (tags or [])}
    if "local" in normalized:
        result.routing = ROUTE_LOCAL
        result.routing_reason = "#local tag present"
        result.model = MODEL_LOCAL
        return result
    if "cloud-haiku" in normalized:
        result.routing = ROUTE_CLAUDE
        result.routing_reason = "#cloud-haiku tag present"
        result.model = MODEL_HAIKU
        return result
    if "cloud-sonnet" in normalized:
        result.routing = ROUTE_CLAUDE
        result.routing_reason = "#cloud-sonnet tag present"
        result.model = MODEL_SONNET
        return result
    if "cloud" in normalized:
        result.routing = ROUTE_CLAUDE
        if not result.routing_reason:
            result.routing_reason = "#cloud tag present"
        if result.model not in ALLOWED_MODELS:
            result.model = MODEL_SONNET
        return result
    # No override — pick a sensible default model for the routing.
    if result.model not in ALLOWED_MODELS:
        if result.routing == ROUTE_CLAUDE:
            result.model = MODEL_SONNET
        elif result.routing == ROUTE_LOCAL:
            result.model = MODEL_LOCAL
        # ROUTE_ASK leaves model=None — the worker will set it after the
        # operator answers.
    return result


def _apply_preset_class(result: PreflightResult, tags: list[str]) -> PreflightResult:
    """Set `result.preset_class` from an explicit `#<class>` tag if present.

    LLM-side preset_class emission is a follow-up; this lets operators
    force a class today via tag while the rest of #139 §3 wiring lands.
    """
    if result.preset_class:  # honor an LLM/caller pre-set value
        return result
    forced = _detect_preset_class_from_tags(tags)
    if forced:
        result.preset_class = forced
    return result


def run_preflight(
    title: str,
    tags: list[str] | None = None,
    caller: PreflightCaller | None = None,
) -> PreflightResult:
    """Run the Haiku preflight call. Returns a defensible PreflightResult even
    on errors (sane=False routing=ask) so the worker can always make a decision.
    """
    tags_list = list(tags or [])
    # Short-circuit: empty title is always unsafe, no need to spend a Haiku call.
    if not title.strip():
        defaults = _defaults()
        return _apply_preset_class(
            _apply_tag_overrides(
                PreflightResult(
                    budget=defaults,
                    routing=ROUTE_ASK,
                    routing_reason="empty title",
                    expected_output="text",
                    ambiguity=None,
                    sane=False,
                    sane_reason="task title is empty",
                    raw={},
                ),
                tags_list,
            ),
            tags_list,
        )

    call = caller or _default_llm_caller
    prompt = build_preflight_prompt(title, tags_list)
    try:
        reply = call(prompt)
    except Exception as exc:
        logger.warning("preflight LLM call failed: %s", exc)
        defaults = _defaults()
        return _apply_preset_class(
            _apply_tag_overrides(
                PreflightResult(
                    budget=defaults,
                    routing=ROUTE_ASK,
                    routing_reason="preflight LLM call failed",
                    expected_output="text",
                    ambiguity=None,
                    sane=False,
                    sane_reason=f"preflight error: {exc}",
                    raw={},
                ),
                tags_list,
            ),
            tags_list,
        )

    return _apply_preset_class(
        _apply_tag_overrides(parse_preflight_response(reply), tags_list),
        tags_list,
    )
