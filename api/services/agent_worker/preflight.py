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
- Routing precedence:
    1) If the tag list contains "local" → routing="local"; routing_reason="#local tag present".
    2) Otherwise, look at the title for explicit cues — "with local agent", "using gemma", etc. → "local"; "use claude", "with opus", "with claude opus" → "claude".
    3) If neither, set routing="ask" (the worker will ask the user which model).
- expected_output: classify what the agent will produce.
    "text" = a written answer; "file" = creates/edits a file; "external_action" = sends an email, posts a message, schedules a meeting; "structured" = returns structured data the caller will parse.
- ambiguity: set to non-null only if the title is genuinely underspecified for an autonomous agent (e.g., "reply to John" with no email/context). One question, not multiple.
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
        return PreflightResult(
            budget=defaults,
            routing=ROUTE_ASK,
            routing_reason="empty title",
            expected_output="text",
            ambiguity=None,
            sane=False,
            sane_reason="task title is empty",
            raw={},
        )

    call = caller or _default_llm_caller
    prompt = build_preflight_prompt(title, tags_list)
    try:
        reply = call(prompt)
    except Exception as exc:
        logger.warning("preflight LLM call failed: %s", exc)
        defaults = _defaults()
        return PreflightResult(
            budget=defaults,
            routing=ROUTE_ASK,
            routing_reason="preflight LLM call failed",
            expected_output="text",
            ambiguity=None,
            sane=False,
            sane_reason=f"preflight error: {exc}",
            raw={},
        )

    return parse_preflight_response(reply)
