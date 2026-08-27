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
# `claude_code` is an explicit route — preflight never emits it. Sessions
# arrive with routing="claude_code" pre-set when spawned by the `/claude`
# surface (see `agent_worker/claude_code_spawn.py`) or via the `#claude` tag.
ROUTE_CLAUDE_CODE = "claude_code"
# `codex` is the sibling explicit route for `/codex` (see
# `agent_worker/codex_spawn.py`). Same semantics as ROUTE_CLAUDE_CODE —
# preflight never emits it directly except via the `#codex` tag.
ROUTE_CODEX = "codex"

# All routing destinations `parse_preflight_response` accepts from the model,
# and the same set `settings.agent_default_route` (#707) is validated
# against — one source of truth so the two checks can't drift apart.
KNOWN_ROUTES = (ROUTE_LOCAL, ROUTE_CLAUDE, ROUTE_CLAUDE_CODE, ROUTE_CODEX, ROUTE_ASK)

# Allowed expected-output shapes. Used to phrase the final Telegram summary.
OUTPUT_KINDS = ("text", "file", "external_action", "structured")

# Per-task model identifiers emitted by preflight. The cloud routing case
# defaults to Sonnet for general work; Haiku is selected by tag override (or
# in future by smart routing — see #139 §2 rubric). "local" maps to the
# local Gemma backend. None means "no override, use settings.agent_managed_model".
MODEL_LOCAL = "local"
MODEL_HAIKU = "claude-haiku-4-5"
MODEL_SONNET = "claude-sonnet-5"
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
    # True only when a sane=False verdict is grounded in something the *code*
    # established deterministically — an empty title, a preflight-call
    # failure/unparseable reply, or a title matched against
    # `_DESTRUCTIVE_TITLE_RE` — rather than merely the model's own inferred
    # "this isn't executable" opinion (#747). The worker fails the task
    # closed (cancels it) only when this is True; a non-fatal sane=False is
    # parked like an ambiguous task instead, since a cheap classifier
    # ignoring the prompt's "mundane tasks are sane" rule has already been
    # observed to silently destroy real work. Meaningless when sane is True.
    sane_fatal: bool = False
    # Whether the cloud (API) route was *asked for* rather than inferred (#584).
    # Only an explicit request — a `#cloud*` tag, or a model/engine named in the
    # title — may dispatch to the Anthropic API without confirmation; an
    # inferred cloud route is downgraded to `ask`. Defaults False so every path
    # that doesn't positively establish intent lands on the safe side.
    routing_explicit: bool = False
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
    # Cache-cold cost estimate for this dispatch (#139 §6). Computed from
    # the per-class cache_creation token estimate + the model's input rate.
    # Used to refuse dispatch when even the cache-cold cost would exceed
    # 2× max_dollars (refuse only when the cheap path can't fit either).
    estimated_cost_dollars: float = 0.0
    # When True, the orchestrator should surface a confirmation prompt to
    # the operator before dispatching (#139 §7). Driven by
    # settings.agent_cost_confirm_threshold_dollars.
    needs_cost_confirmation: bool = False
    # Set when a non-null `ambiguity` was demoted to advisory-only because
    # `settings.agent_default_route` is configured (#751) — holds the
    # original question text so the worker can log it as context (session
    # transcript / completion note) instead of discarding it silently.
    # `ambiguity` itself is cleared to None in the same step, since a
    # demoted ambiguity must not block. None when nothing was demoted.
    demoted_ambiguity: str | None = None
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
  "routing_explicit": <true|false>,
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
       These are INFERENCES, so set routing_explicit=false — the worker will
       confirm before spending API credits on them.
       Do NOT trigger on bare nouns where the meaning is ambiguous —
       e.g., "drive home" (vehicle), "book recommendation" (literature),
       "email signature design" (general design task).
    5) If none of the above match, set routing="ask" (the worker will
       ask the operator which model).
- routing_explicit: true ONLY when the operator named the engine or model
  themselves — a "#cloud"/"#local"/"#claude"/"#codex" tag, or a title that says
  "use claude", "with opus", "using gemma". False for every routing you reached
  by inference (rule 4) or by default. When in doubt, false: a false value costs
  one confirmation question, a wrong true value spends API credits unasked.
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

# Phrasings the preflight model uses when it smuggles a method-of-execution /
# engine-selection question into `ambiguity` instead of leaving it null
# (#748) — e.g. "Should this task be routed to a local agent for code
# implementation, or is it a design/specification task for a human
# engineer?". The prompt already forbids this explicitly, but this is the
# *second* observed case of the model ignoring an explicit negative
# constraint (see `_DESTRUCTIVE_TITLE_RE` / #747), so the code cannot rely on
# prompt compliance alone. There's no structural field distinguishing
# ambiguity "kinds" today — adding one would just move the same compliance
# risk into a different JSON key the model could also ignore — so this
# inspects the question prose directly.
#
# Kept deliberately narrow: BOTH halves must match. A genuine missing-
# referent ambiguity can innocently contain a word like "engineer" or
# "agent" (e.g. "send the update to the engineer" with no named engineer)
# without being a routing question at all — matching on either half alone
# would swallow that case, and a swallowed real ambiguity is worse than one
# extra question to the operator.
_ROUTING_DECISION_RE = re.compile(
    r"(?i)\b(should\s+(this|it)\s+(task\s+)?be\s+(routed|handled|assigned|executed|done)\b"
    r"|which\s+(model|engine|agent)\s+should\b"
    r"|(is\s+this|or\s+is\s+(this|it))\s+(a\s+)?(design|specification|spec)\b)"
)
_EXECUTOR_NOUN_RE = re.compile(
    r"(?i)\b(local\s+agent|human\s+engineer|code\s+implementation|"
    r"design(?:/|\s+or\s+)?specification|a\s+human\b|which\s+engine\b|"
    r"which\s+model\b|rout(?:e|ed|es|ing)\b)"
)


def _is_routing_flavored_ambiguity(question: str) -> bool:
    """True when an `ambiguity.question` is actually a method-of-execution /
    routing question (#748), not a genuine missing-referent ambiguity."""
    return bool(
        _ROUTING_DECISION_RE.search(question or "")
        and _EXECUTOR_NOUN_RE.search(question or "")
    )


# Deterministic destructive-title check (#747). Matched against the title
# directly — NOT inferred from the model's `sane_reason` prose — so the code
# can independently confirm a fail-closed verdict rather than trust a single
# cheap model's judgement. Deliberately narrow, mirroring the prompt's own
# examples ("rm -rf /", "delete all my data") plus a couple of obviously
# analogous shapes: a broad matcher would start catching mundane tasks that
# merely mention deletion (e.g. "delete the stale draft email"), which is
# exactly the false-positive failure mode #747 exists to fix.
_DESTRUCTIVE_TITLE_RE = re.compile(
    r"(?i)\brm\s+-rf\b"
    r"|\bdelete\s+all\s+(my\s+)?(data|files|everything)\b"
    r"|\bwipe\s+(the\s+)?(disk|drive|database|everything)\b"
    r"|\bformat\s+(the\s+)?(disk|drive)\b"
    r"|\bdrop\s+(the\s+)?database\b"
)


def _apply_sanity_gate(result: PreflightResult, title: str) -> PreflightResult:
    """Establish `sane_fatal` independent of the model's own claim (#747).

    A title matching `_DESTRUCTIVE_TITLE_RE` is always sane=False and fatal,
    regardless of what the model returned — this is the one case the code
    can confirm on its own, so it isn't weakened by (or dependent on) the
    model's compliance in either direction: a model that missed an obvious
    destructive shape doesn't get to leave it running, and a model whose
    sane_reason merely *describes* something as destructive without this
    pattern present doesn't get automatic fatal status from that alone.

    Every other sane=False is left exactly as constructed: the empty-title
    short-circuit and the LLM-call/parse-error fallbacks already set
    `sane_fatal=True` directly (genuine code-level failures), and a parsed
    LLM reply defaults `sane_fatal=False` (the model's own inferred opinion,
    non-fatal — the worker parks it instead of cancelling the task).
    """
    if _DESTRUCTIVE_TITLE_RE.search(title or ""):
        result.sane = False
        result.sane_fatal = True
        if not result.sane_reason:
            result.sane_reason = "title matches a destructive-command pattern"
    return result


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
            sane_fatal=True,  # genuine preflight error, not a model opinion — keep fail-closed
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
    if routing not in KNOWN_ROUTES:
        routing = ROUTE_ASK

    expected_output = raw.get("expected_output", "text")
    if expected_output not in OUTPUT_KINDS:
        expected_output = "text"

    ambiguity = None
    amb_raw = raw.get("ambiguity")
    if isinstance(amb_raw, dict) and amb_raw.get("question"):
        question = str(amb_raw["question"])
        if _is_routing_flavored_ambiguity(question):
            # #748: the model put a method-of-execution / engine-selection
            # question into `ambiguity` despite the prompt explicitly
            # excluding those ("Method-of-execution questions ... are NOT
            # ambiguity"). Routing (including LIFEOS_AGENT_DEFAULT_ROUTE,
            # #707) owns that decision, not the operator-facing block —
            # leave ambiguity null so the task isn't blocked on it.
            logger.info(
                "preflight ambiguity suppressed as routing-flavored: %r", question,
            )
        else:
            ambiguity = PreflightAmbiguity(question=question)

    return PreflightResult(
        budget=budget,
        routing=routing,
        routing_reason=str(raw.get("routing_reason", "")),
        routing_explicit=bool(raw.get("routing_explicit", False)),
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
    """Production caller: pick a client in priority order, then run one
    short completion.

    1. Anthropic, when `settings.anthropic_api_key` is set. Byte-identical
       to the pre-#704 behavior of this function — same import, same
       client construction, same call — because this is the maintainer's
       own install and every other install with a key configured; no probe
       runs on this branch.
    2. Otherwise, the local llama-server, when reachable
       (`LocalLLMClient().is_available()` — one short GET /health, same
       check `local_executor._default_llm_client` uses). Unlike that
       function, this always probes once there's no Anthropic key —
       preflight has no `agent_remote_executor` flag gate to hide behind;
       it just needs *some* usable client.
    3. Otherwise, the #699 remote provider, when `agent_remote_executor`
       and `remote_llm_configured` — mirrors the remote branch of
       `local_executor._default_llm_client`.
    4. Otherwise, raise. `run_preflight`'s existing except-clause already
       degrades this to sane=False/routing=ask — unchanged.
    """
    if settings.anthropic_api_key:
        # Import inside the branch so tests that monkeypatch run_preflight
        # don't need the Anthropic SDK installed, and so a no-key install
        # never imports it either.
        from api.services.llm_client import AnthropicLLMClient

        client = AnthropicLLMClient(model=settings.agent_preflight_model)
        response = client.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.0,
        )
        return response.text

    from api.services.llm_client import LocalLLMClient

    local_client = LocalLLMClient()
    if local_client.is_available():
        client = local_client
    elif settings.agent_remote_executor and settings.remote_llm_configured:
        client = LocalLLMClient(
            base_url=settings.remote_llm_base_url,
            model=settings.remote_llm_model,
            api_key=settings.remote_llm_api_key,
            timeout=settings.remote_llm_timeout,
        )
    else:
        raise RuntimeError(
            "no LLM client available for preflight: no Anthropic API key, "
            "local llama-server unreachable, and no remote provider configured"
        )

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


# Model/engine words that count as the operator naming a cloud route themselves.
# Used to corroborate the classifier's `routing_explicit` before any API dispatch
# happens without a confirmation (#584) — the tags are checked separately.
_TITLE_NAMES_A_CLOUD_ENGINE = re.compile(
    r"(?i)\b(claude|opus|sonnet|haiku|cloud|anthropic|api)\b"
)


def _apply_tag_overrides(result: PreflightResult, tags: list[str], title: str = "") -> PreflightResult:
    """Apply tag-based routing/model overrides (#139 §2 precedence).

    Tag precedence (a tag always wins over preflight's LLM choice):
      `#local`        → routing=local, model=local
      `#claude`       → routing=code   (Claude Code CLI, subscription-billed)
      `#codex`        → routing=codex  (Codex CLI, subscription-billed)
      `#cloud-haiku`  → routing=claude, model=claude-haiku-4-5
      `#cloud-sonnet` → routing=claude, model=claude-sonnet-5
      `#cloud`        → routing=claude, model=(whatever preflight picked, else Sonnet)

    Returns a new PreflightResult so the caller can chain. Tag list is
    normalized case-insensitively with optional leading `#`.
    """
    normalized = {_normalize_tag(t) for t in (tags or [])}
    if "local" in normalized:
        result.routing = ROUTE_LOCAL
        result.routing_reason = "#local tag present"
        result.routing_explicit = True
        result.model = MODEL_LOCAL
        return result
    if "claude" in normalized:
        # Claude Code CLI route — dispatched through ClaudeCodeExecutor like
        # /claude. Billed against the operator's Claude Pro subscription
        # rather than per-token Anthropic API rates, so cost-gating is skipped.
        result.routing = ROUTE_CLAUDE_CODE
        result.routing_reason = "#claude tag present"
        result.routing_explicit = True
        result.model = ""  # CLI picks its own model from settings
        return result
    if "codex" in normalized:
        # Codex CLI route — dispatched through CodexExecutor like /codex.
        # Billed against the operator's ChatGPT plan.
        result.routing = ROUTE_CODEX
        result.routing_reason = "#codex tag present"
        result.routing_explicit = True
        result.model = ""  # CLI picks its own model from ~/.codex/config.toml
        return result
    if "cloud-haiku" in normalized:
        result.routing = ROUTE_CLAUDE
        result.routing_reason = "#cloud-haiku tag present"
        result.routing_explicit = True
        result.model = MODEL_HAIKU
        return result
    if "cloud-sonnet" in normalized:
        result.routing = ROUTE_CLAUDE
        result.routing_reason = "#cloud-sonnet tag present"
        result.routing_explicit = True
        result.model = MODEL_SONNET
        return result
    if "cloud" in normalized:
        result.routing = ROUTE_CLAUDE
        result.routing_explicit = True
        if not result.routing_reason:
            result.routing_reason = "#cloud tag present"
        if result.model not in ALLOWED_MODELS:
            result.model = MODEL_SONNET
        return result
    # No tag override. A cloud route that nobody asked for must not dispatch:
    # downgrade it to `ask` so the worker confirms first (#584). The classifier's
    # own `routing_explicit` is trusted only when the title actually contains a
    # model/engine cue — a deterministic cross-check, so a hallucinated `true`
    # still lands on the safe side. `#cloud*` tags returned above already.
    if result.routing == ROUTE_CLAUDE and not (
        result.routing_explicit and _TITLE_NAMES_A_CLOUD_ENGINE.search(title or "")
    ):
        result.routing_explicit = False
        inferred = result.routing_reason or "inferred cloud route"
        result.routing = ROUTE_ASK
        result.routing_reason = (
            f"cloud route not explicitly requested ({inferred}) — confirming before "
            f"spending API credits"
        )
        result.model = None

    # No override — pick a sensible default model for the routing.
    if result.model not in ALLOWED_MODELS:
        if result.routing == ROUTE_CLAUDE:
            result.model = MODEL_SONNET
        elif result.routing == ROUTE_LOCAL:
            result.model = MODEL_LOCAL
        # ROUTE_ASK leaves model=None — the worker will set it after the
        # operator answers.
    return result


def _apply_default_route(result: PreflightResult, original_routing: str) -> PreflightResult:
    """Apply `settings.agent_default_route` (#707), and — as of #751 — demote
    any non-null `ambiguity` to advisory once that setting is configured and
    valid. Empty setting (default) is a no-op, so an unset install is
    byte-identical to pre-#707 behavior; both parts of this function are
    gated on the setting being non-empty and valid.

    Two independent things happen here, in order:

    1. **Ambiguity demotion (#751).** Configuring a default route is the
       operator saying "run untagged tasks without asking me" — a cheap
       classifier's hedging (`ambiguity`) shouldn't override that standing
       instruction, regardless of what routing was ultimately picked. So
       once the setting is confirmed non-empty and valid, any non-null
       `result.ambiguity` is stashed on `result.demoted_ambiguity` (for the
       worker to log as advisory context) and cleared. String-matching the
       question text (#748's approach) is whack-a-mole — the model keeps
       rephrasing around the pattern — so this demotes unconditionally on
       the *value being non-null* rather than trying to classify its prose.
       This intentionally does NOT touch `result.sane` — a non-fatal
       `sane=False` still parks the task (#747): sanity is "should this run
       at all", a question this setting has no bearing on, whereas ambiguity
       is "who resolves an open question", which is exactly what a standing
       default-route instruction answers.

    2. **Route substitution (#707), unchanged.** Gate:
       `result.routing == ROUTE_ASK and result.sane` (the `ambiguity is
       None` half of the old gate is now always true here, since part 1 just
       cleared it) `and original_routing == ROUTE_ASK`. Deliberately NOT a
       string match against `routing_reason` — the LLM's reason text is
       free-form prose ("no tag and no title cue" today, but not a stable
       contract), so matching on it would be brittle. This one structural
       gate covers both "lack of cues" paths:
         - the LLM's own rule-5 answer ("none of the routing rules matched")
         - `parse_preflight_response`'s deterministic fallback when the model
           omitted `routing` or returned a value outside `KNOWN_ROUTES`
       and it correctly excludes the "ask" outcome the issue says must stay
       ask: sanity failures — including the empty-title short-circuit and
       the LLM-call-failure fallback in `run_preflight`, both of which set
       `sane=False`.

       One more `ask` source needs excluding: `_apply_tag_overrides`'s #584
       downgrade, which turns an *inferred* (unconfirmed) cloud route into
       `ask` specifically so it can't auto-dispatch. That result is also
       sane=True, so it would slip through the gate above — this isn't
       "lack of cues", it's a cue nobody confirmed, and silently routing it
       via the default would undo the confirmation #584 exists for.
       Excluded via `original_routing`: the routing value from BEFORE
       `_apply_tag_overrides` ran. Only when the original LLM/parse outcome
       was *already* `ask` — not downgraded from `claude` — do we know this
       is genuinely nothing-to-route-on. (Ambiguity demotion in part 1 still
       runs on this path — the #584 downgrade blocks via `routing == ask`
       either way, so demoting the ambiguity text just avoids a redundant
       question, not a redundant block.)
    """
    if not settings.agent_default_route:
        return result

    default_route = settings.agent_default_route
    if default_route not in KNOWN_ROUTES:
        # Surface loudly (AC): no precedent in this codebase for hard-failing
        # process startup over a bad setting value (config/settings.py has no
        # validators), so this logs an ERROR — one line per occurrence, not a
        # crash — and falls back to `ask`, same as every other preflight
        # error path. The worker loop must never die over a typo'd env var.
        # Deliberately checked (and returned on) BEFORE ambiguity demotion —
        # a misconfigured setting is not a *valid* standing instruction, so
        # it must not weaken the ambiguity block either.
        logger.error(
            "invalid LIFEOS_AGENT_DEFAULT_ROUTE=%r — must be one of %s; "
            "falling back to ask", default_route, KNOWN_ROUTES,
        )
        return result

    if result.ambiguity is not None:
        logger.info(
            "preflight ambiguity demoted to advisory (LIFEOS_AGENT_DEFAULT_ROUTE=%s "
            "configured): %r", default_route, result.ambiguity.question,
        )
        result.demoted_ambiguity = result.ambiguity.question
        result.ambiguity = None

    if result.routing != ROUTE_ASK or not result.sane:
        return result
    if original_routing != ROUTE_ASK:
        return result

    result.routing = default_route
    result.routing_reason = f"no cues; LIFEOS_AGENT_DEFAULT_ROUTE={default_route}"
    if result.model not in ALLOWED_MODELS:
        if default_route == ROUTE_CLAUDE:
            result.model = MODEL_SONNET
        elif default_route == ROUTE_LOCAL:
            result.model = MODEL_LOCAL
        # ROUTE_CLAUDE_CODE / ROUTE_CODEX / ROUTE_ASK: leave model as-is
        # (CLI routes pick their own model; ROUTE_ASK is a pointless but
        # harmless configuration — model stays unset for the operator).
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


def _apply_cost_gates(result: PreflightResult) -> PreflightResult:
    """Compute the cache-cold cost estimate and apply #139 §6 + §7 gates.

    §6 (fail-fast): if the estimated cache-cold dispatch cost exceeds
    2× the task's `max_dollars`, refuse via `sane=False` with reason
    `budget_too_small`. The 2× margin (not 1×) keeps cache-warm tasks
    from being over-refused — when prompt cache is warm the real cost
    is 0.10× input instead of 1.25× cache_creation, so the cheap path
    is 12.5× cheaper; refusing only when even the cold path can't fit
    avoids killing tasks that would have happily run from cache.

    §7 (cost preview): set `needs_cost_confirmation=True` when the
    estimate exceeds `settings.agent_cost_confirm_threshold_dollars`.
    Local-routed tasks always set the estimate to 0 and never trigger
    confirmation.
    """
    # Only the Anthropic-API route (ROUTE_CLAUDE / Managed Agents) is gated.
    # ROUTE_CLAUDE_CODE / ROUTE_CODEX bill against a flat subscription; ROUTE_LOCAL
    # is free. Per-session $ rollups for CLI routes still populate via the
    # rollout ingest (cc:/cx: sources in /agents).
    if result.routing != ROUTE_CLAUDE:
        result.estimated_cost_dollars = 0.0
        result.needs_cost_confirmation = False
        return result

    # Local imports keep preflight cheap to import when cost gating isn't
    # in play (and avoid a settings-import cycle in some test paths).
    from api.services.agent_worker.pricing import (
        CACHE_CREATION_RATE_MULTIPLIER,
        MANAGED_SESSION_HOUR_OVERHEAD,
        PRICING,
        fallback_rates,
    )
    from api.services.agent_worker.tool_filter import estimated_cache_creation_tokens

    model = result.model or MODEL_SONNET
    rates = PRICING.get(model) or fallback_rates()
    cache_tokens = estimated_cache_creation_tokens(result.preset_class)
    cache_cold_dollars = cache_tokens * rates["input"] * CACHE_CREATION_RATE_MULTIPLIER
    # Add session-hour overhead as a small floor so estimates align with
    # `managed_session_cost`'s shape (token cost + overhead).
    overhead = (result.budget.wall_seconds / 3600.0) * MANAGED_SESSION_HOUR_OVERHEAD
    result.estimated_cost_dollars = round(cache_cold_dollars + overhead, 4)

    # §6 fail-fast — refuse only when 2× margin can't fit.
    if (
        result.budget.max_dollars > 0
        and result.estimated_cost_dollars > 2.0 * result.budget.max_dollars
    ):
        result.sane = False
        result.sane_reason = (
            f"budget_too_small: cache-cold estimate ${result.estimated_cost_dollars:.2f} "
            f"exceeds 2× max_dollars (${result.budget.max_dollars:.2f}). "
            "Raise the budget or pick a smaller preset_class."
        )

    # §7 confirm-threshold check. Local import so settings reload in tests works.
    try:
        from config.settings import settings as _settings
        threshold = float(_settings.agent_cost_confirm_threshold_dollars)
    except Exception:
        threshold = 1.0
    if threshold > 0 and result.estimated_cost_dollars > threshold:
        result.needs_cost_confirmation = True

    return result


def _finish(result: PreflightResult, tags_list: list[str], title: str = "") -> PreflightResult:
    """Shared post-processing pipeline for every `run_preflight` return path:
    sanity gate (#747) > tag overrides > default route (#707, now also
    demoting ambiguity per #751) > preset class > cost gates.

    Precedence, and why each sits where it does:

      1. **Sanity gate first, and orthogonal to everything below.**
         `_apply_sanity_gate` runs before routing is decided at all, because
         "should this run" is a different question from "how should it
         run" — a `sane_fatal` verdict fails the task closed regardless of
         routing/ambiguity (checked immediately in the worker, before either
         is consulted), and a non-fatal `sane=False` parks it. Neither is
         touched by the routing precedence below, and #751 does not change
         this: a default route answers "who resolves an open question", not
         "should this task run at all".
      2. **Tags** (`_apply_tag_overrides`) — the operator retagging a task
         is the most direct, most recent signal available; always wins.
      3. **Explicit LLM route** — preserved by tag overrides leaving a
         non-`ask` classifier routing alone, and by `_apply_default_route`
         only substituting when routing is still `ask` *and* was already
         `ask` before tag overrides ran (see `original_routing` below) — so
         a routing the model was confident enough to name outright is never
         overridden by the default route.
      4. **Default route** (`_apply_default_route`, #707) — substitutes
         `ask` for the configured route only when nothing above produced a
         real answer. As of #751, this step *also* demotes a non-null
         `ambiguity` to advisory (logged, not blocking) whenever the setting
         is configured and valid — independent of whether step 4's route
         substitution itself fires, since an explicit route (step 3) can
         still carry a stale ambiguity the model should not have set.
      5. **Ask** — the fallback when nothing above resolved routing, or the
         #584 unconfirmed-cloud downgrade parked it there.

    Centralized (rather than each return path chaining the calls itself) so
    `original_routing` is captured exactly once, right after the
    parse/short-circuit result is built and before `_apply_tag_overrides`
    (the only thing that mutates `result.routing` ahead of the default-route
    hook) gets a chance to change it. See `_apply_default_route` for why that
    pre-tag-override value matters.
    """
    result = _apply_sanity_gate(result, title)
    original_routing = result.routing
    result = _apply_tag_overrides(result, tags_list, title)
    result = _apply_default_route(result, original_routing)
    result = _apply_preset_class(result, tags_list)
    result = _apply_cost_gates(result)
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
        return _finish(
            PreflightResult(
                budget=defaults,
                routing=ROUTE_ASK,
                routing_reason="empty title",
                expected_output="text",
                ambiguity=None,
                sane=False,
                sane_reason="task title is empty",
                sane_fatal=True,  # deterministic — the code confirmed this itself
                raw={},
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
        return _finish(
            PreflightResult(
                budget=defaults,
                routing=ROUTE_ASK,
                routing_reason="preflight LLM call failed",
                expected_output="text",
                ambiguity=None,
                sane=False,
                sane_reason=f"preflight error: {exc}",
                sane_fatal=True,  # genuine preflight error, not a model opinion — keep fail-closed
                raw={},
            ),
            tags_list,
        )

    return _finish(parse_preflight_response(reply), tags_list, title)
