"""Haiku preflight call that classifies an #agent task before execution.

A single, cheap Claude Haiku call returns structured JSON describing:
- the budget the task should run with (parsed from natural-language hints)
- which executor to use (local Gemma vs. Claude Opus vs. ask the user)
- whether the title is ambiguous (and what clarifying question to ask)
- the expected output shape (for phrasing the completion notification)
- a sanity flag (garbage / destructive titles get parked rather than run)

The model is pinned to `claude-haiku-4-5` by default via
`LIFEOS_AGENT_PREFLIGHT_MODEL`. The function takes a callable
`call_llm(prompt) -> str` so tests can inject a stub instead of mocking the
SDK; production wiring lives in `_default_llm_caller`.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Callable

from config.settings import settings


logger = logging.getLogger(__name__)

# Output token budget for the preflight completion call. A reasoning model
# (e.g. Fireworks DeepSeek v4 Flash under `LIFEOS_AGENT_PREFLIGHT_ENGINE=remote`)
# spends output tokens on hidden reasoning before it emits the JSON reply, so
# this must be large enough to leave room for both.
_PREFLIGHT_MAX_TOKENS = 4096

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
# `remote` is the route for the `#cloud` tag: the configured remote
# OpenAI-compatible provider (e.g. DeepSeek via Fireworks) — never the
# Anthropic API. Like ROUTE_CLAUDE_CODE/ROUTE_CODEX, preflight's own JSON
# schema never emits this (the model only ever returns "local"|"claude"|
# "ask" — see `_PREFLIGHT_INSTRUCTIONS`); it's set exclusively by
# `_apply_tag_overrides` from the `#cloud` tag. Deliberately absent from
# `KNOWN_ROUTES` (see that constant's comment) — unlike the CLI routes,
# this one carries real per-token spend, so a noncompliant model hallucinating
# the literal string "remote" must fall back to `ROUTE_ASK` at parse time
# (the `routing not in KNOWN_ROUTES` check below), not be treated as a
# legitimate model-chosen route needing its own corroboration carve-out.
ROUTE_REMOTE = "remote"
# `hermes` is the route for the `#hermes` tag (or a board card
# assigned to the Hermes engine): the turn opens a Hermes conversation via
# `HermesExecutor` instead of running a local CLI. Like ROUTE_CLAUDE_CODE/
# ROUTE_CODEX, preflight's own JSON schema never emits this directly — it's
# set exclusively by `_apply_tag_overrides` from the `#hermes` tag.
ROUTE_HERMES = "hermes"

# All routing destinations `parse_preflight_response` accepts from the model,
# and the same set `settings.agent_default_route` is validated
# against — one source of truth so the two checks can't drift apart.
# `ROUTE_REMOTE` is intentionally NOT a member (see its own comment above):
# it's real per-token spend on a third-party provider, reachable only via the
# explicit `#cloud` tag, and `settings.agent_default_route` should not be
# able to silently default untagged tasks onto it any more than it can onto
# `ROUTE_CLAUDE` (which is also excluded from default-route substitution by
# `_apply_default_route`'s `original_routing` guard, just via a different
# mechanism — see that function's docstring, point 3a).
KNOWN_ROUTES = (ROUTE_LOCAL, ROUTE_CLAUDE, ROUTE_CLAUDE_CODE, ROUTE_CODEX, ROUTE_HERMES, ROUTE_ASK)

# Allowed expected-output shapes, for phrasing the final Telegram summary.
OUTPUT_KINDS = ("text", "file", "external_action", "structured")

# Per-task model identifiers emitted by preflight. The cloud routing case
# defaults to Sonnet for general work; Haiku is selected by tag override (or
# in future by smart routing — see §2 rubric). "local" maps to the
# local Gemma backend. None means "no override, use settings.agent_managed_model".
MODEL_LOCAL = "local"
MODEL_HAIKU = "claude-haiku-4-5"
MODEL_SONNET = "claude-sonnet-5"
ALLOWED_MODELS = (MODEL_LOCAL, MODEL_HAIKU, MODEL_SONNET)


@dataclass
class PreflightBudget:
    wall_seconds: int
    max_tokens: int | None
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
    # established deterministically — an empty title, or a title matched
    # against `_DESTRUCTIVE_TITLE_RE` — rather than merely the model's own inferred
    # "this isn't executable" opinion. The worker fails the task
    # closed (cancels it) only when `sane_fatal` is True; a non-fatal
    # sane=False is parked like an ambiguous task instead, since a cheap
    # classifier ignoring the prompt's "mundane tasks are sane" rule has
    # already been observed to silently destroy real work. A failed or
    # unparseable preflight call carries no verdict at all, so it sets
    # `sane=True`/`sane_fatal=False` and records `preflight_error` instead
    # (see that field) rather than a sane_fatal verdict. Meaningless when
    # sane is True.
    sane_fatal: bool = False
    # Whether the cloud (API) route was *asked for* rather than inferred.
    # Only an explicit request — a `#cloud*` tag, or a model/engine named in the
    # title — may dispatch to the Anthropic API without confirmation; an
    # inferred cloud route is downgraded to `ask`. Defaults False so every path
    # that doesn't positively establish intent lands on the safe side.
    routing_explicit: bool = False
    # Per-task model selection (§2). For cloud routes, defaults to
    # MODEL_SONNET; can be overridden to MODEL_HAIKU by the `#cloud-haiku`
    # tag (or to MODEL_SONNET by `#cloud-sonnet`). For local routes, set
    # to MODEL_LOCAL. The worker uses this for client-side cost accounting;
    # actual remote-model selection still requires the agent preset to
    # match (section 3 territory).
    model: str | None = None
    # Preset class for per-session tool filtering (§3). When set, the
    # managed executor will call driver.update_session() with the class's
    # filtered tool list (via tool_filter.class_to_tool_filter) between
    # session create and the first user message — scoping cache_creation
    # to the smaller tool set. Currently picked from tag overrides only;
    # LLM-side preflight emission is a follow-up.
    preset_class: str | None = None
    # Cache-cold cost estimate for this dispatch (§6). Computed from
    # the per-class cache_creation token estimate + the model's input rate.
    # Refuses dispatch when even the cache-cold cost would exceed
    # 2× max_dollars (refuse only when the cheap path can't fit either).
    estimated_cost_dollars: float = 0.0
    # When True, the orchestrator should surface a confirmation prompt to
    # the operator before dispatching (§7). Driven by
    # settings.agent_cost_confirm_threshold_dollars.
    needs_cost_confirmation: bool = False

    def execution_layer(self, *, working_dir: str | None = None):
        """Adapt legacy preflight output into trusted resolver defaults."""
        from api.services.agent_worker.execution import Budget, ExecutionLayer

        return ExecutionLayer(
            executor=self.routing,
            model_id=self.model,
            model_executor=self.routing if self.model else None,
            working_dir=working_dir,
            budget=Budget(
                wall_seconds=self.budget.wall_seconds,
                max_tokens=self.budget.max_tokens,
                max_dollars=self.budget.max_dollars,
            ),
        )
    # Set when a non-null `ambiguity` was demoted to advisory-only because
    # `settings.agent_default_route` is configured — holds the
    # original question text so the worker can log it as context (session
    # transcript / completion note) instead of discarding it silently.
    # `ambiguity` itself is cleared to None in the same step, since a
    # demoted ambiguity must not block. None when nothing was demoted.
    demoted_ambiguity: str | None = None
    # Set when an LLM-chosen route (local / claude_code / codex) was demoted
    # to `settings.agent_default_route` because the title didn't corroborate
    # it — holds the route the model/parse actually produced, so the
    # worker can log it as context the same way `demoted_ambiguity` is
    # logged. `routing` itself is overwritten with the default route in the
    # same step. None when nothing was demoted. See `_apply_route_corroboration`.
    demoted_routing: str | None = None
    # Set when a non-fatal sane=False verdict — the model's own inferred
    # "this isn't executable" opinion, never a `sane_fatal` one — was
    # demoted to advisory because `settings.agent_default_route` is
    # configured and valid. Holds the original `sane_reason` so the
    # worker can log it as context, the same way `demoted_ambiguity` and
    # `demoted_routing` are logged. `sane` itself is set to True in the same
    # step, since a demoted sanity objection must not park or block the
    # task. None when nothing was demoted. `sane_fatal` verdicts (empty
    # title, or the deterministic destructive-title regex) are never
    # touched — see `_apply_sanity_gate` and `_apply_default_route`. A
    # preflight-call/parse error is never `sane_fatal` in the first place
    # (see `preflight_error`), so there is nothing here for it to demote.
    demoted_sanity: str | None = None
    # Set when the preflight classifier call itself failed (an exception
    # from the LLM client) or returned a reply `parse_preflight_response`
    # could not parse — holds a short description of that failure. This is
    # a statement about the classifier call, not a judgement about the
    # task: these paths set `sane=True`, `sane_reason=""`, `sane_fatal=False`
    # and leave `routing=ROUTE_ASK`, so a task with no routing tag is parked
    # for the operator rather than cancelled, while a task with an explicit
    # routing tag (applied afterward by `_apply_tag_overrides`) still runs
    # on that route. None when a verdict was actually obtained.
    preflight_error: str | None = None
    # Jev harm-severity judgment for this task's title, on the 0-4 score
    # scale of `_DESTRUCTIVE_QUESTIONS["harm"]`'s five criteria levels
    # (read-only ... irreversible mass/external loss). None when Jev isn't
    # configured, `agent_jev_destructive_gate` is `off`, or the call failed
    # — see `_apply_destructive_judgment`.
    destructive_score: float | None = None
    # Jev's calibrated probability that carrying out this task as written
    # would permanently destroy data or send something unrecallable — the
    # `irreversible` question's `noul` answer. None under the same
    # conditions as `destructive_score`.
    destructive_probability: float | None = None
    # True only when `agent_jev_destructive_gate=block` and one of the two
    # measured thresholds was crossed, and this verdict set `sane=False`.
    # Unlike the model's own inferred sanity opinion, this is a code-
    # thresholded verdict over a calibrated probability, not a cheap
    # model's free-form guess — `_apply_default_route`'s sanity demotion
    # (built for the latter) must not treat it as advisory-only, so that
    # step's `not result.sane and not result.sane_fatal` gate additionally
    # requires `not result.destructive_block`. False whenever the block
    # gate never fired (off/shadow modes, below both thresholds, a failed
    # Jev call, or a title the regex sanity gate already made fatal).
    destructive_block: bool = False
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
    "max_tokens": <int|null>,
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
- Default budget if the title doesn't specify one: wall_seconds={default_wall}, max_tokens={default_tokens}, max_dollars={default_dollars}. No token cap applies unless the title names one explicitly.
- Parse natural-language budget hints from the title: "5 min" / "30s" / "1h" → wall_seconds; "max $0.50" → max_dollars; "10k tokens" / "50000 tokens" → max_tokens (only set max_tokens when the title names an amount like this — otherwise leave it null). Be reasonable about unit conversions.
- Routing precedence (apply in order; first match wins):
    1) If the tag list contains "local" → routing="local"; routing_reason="#local tag present".
    2) If the tag list contains "cloud" → routing="claude"; routing_reason="#cloud tag present".
       (This JSON field is a placeholder — tag-handling code, not this
       prompt, owns the real dispatch: "#cloud" now sends the task to the
       operator's configured remote provider, never the Anthropic API.
       "#cloud-haiku"/"#cloud-sonnet" are the separate, unchanged explicit
       Anthropic-API escape hatches.)
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
- sane: set to false ONLY for empty/garbage titles or obviously destructive shapes (e.g., "rm -rf /", "delete all my data"). Mundane tasks are sane. Feature requests and product specifications ARE executable tasks — this system builds features, not only fixes — never mark them insane.

Tag list and title follow. Return ONLY the JSON.
"""


def build_preflight_prompt(title: str, tags: list[str]) -> str:
    defaults = _defaults()
    default_tokens_text = "null" if defaults.max_tokens is None else str(defaults.max_tokens)
    instructions = _PREFLIGHT_INSTRUCTIONS.format(
        default_wall=defaults.wall_seconds,
        default_tokens=default_tokens_text,
        default_dollars=defaults.max_dollars,
    )
    return f"{instructions}\nTAGS: {tags}\nTITLE: {title.strip()}"


# ---------------------------------------------------------------------------
# Parsing the model's reply
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# Phrasings the preflight model uses when it smuggles a method-of-execution /
# engine-selection question into `ambiguity` instead of leaving it null
# — e.g. "Should this task be routed to a local agent for code
# implementation, or is it a design/specification task for a human
# engineer?". The prompt already forbids this explicitly, but this is the
# *second* observed case of the model ignoring an explicit negative
# constraint (see `_DESTRUCTIVE_TITLE_RE`), so the code cannot rely on
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
    routing question, not a genuine missing-referent ambiguity."""
    return bool(
        _ROUTING_DECISION_RE.search(question or "")
        and _EXECUTOR_NOUN_RE.search(question or "")
    )


# Deterministic destructive-title check. Matched against the title
# directly — NOT inferred from the model's `sane_reason` prose — so the code
# can independently confirm a fail-closed verdict rather than trust a single
# cheap model's judgement. Deliberately narrow, mirroring the prompt's own
# examples ("rm -rf /", "delete all my data") plus a couple of obviously
# analogous shapes: a broad matcher would start catching mundane tasks that
# merely mention deletion (e.g. "delete the stale draft email"), which is
# exactly the false-positive failure mode exists to fix.
_DESTRUCTIVE_TITLE_RE = re.compile(
    r"(?i)\brm\s+-rf\b"
    r"|\bdelete\s+all\s+(my\s+)?(data|files|everything)\b"
    r"|\bwipe\s+(the\s+)?(disk|drive|database|everything)\b"
    r"|\bformat\s+(the\s+)?(disk|drive)\b"
    r"|\bdrop\s+(the\s+)?database\b"
)


def _apply_sanity_gate(result: PreflightResult, title: str) -> PreflightResult:
    """Establish `sane_fatal` independent of the model's own claim.

    A title matching `_DESTRUCTIVE_TITLE_RE` is always sane=False and fatal,
    regardless of what the model returned — this is the one case the code
    can confirm on its own, so it isn't weakened by (or dependent on) the
    model's compliance in either direction: a model that missed an obvious
    destructive shape doesn't get to leave it running, and a model whose
    sane_reason merely *describes* something as destructive without this
    pattern present doesn't get automatic fatal status from that alone.

    Every other sane=False is left exactly as constructed: the empty-title
    short-circuit already sets `sane_fatal=True` directly (a genuine
    code-level finding), a parsed LLM reply defaults `sane_fatal=False` (the
    model's own inferred opinion, non-fatal — the worker parks it instead of
    cancelling the task), and the LLM-call/parse-error fallbacks set
    `sane=True`/`sane_fatal=False` with `preflight_error` recorded instead —
    a failed call carries no verdict to be fatal about.
    """
    if _DESTRUCTIVE_TITLE_RE.search(title or ""):
        result.sane = False
        result.sane_fatal = True
        if not result.sane_reason:
            result.sane_reason = "title matches a destructive-command pattern"
    return result


# Thresholds for `_apply_destructive_judgment`'s `block` gate, measured
# against 15 synthetic destructive paraphrases, 12 synthetic benign titles,
# and 129 real vault task titles: harm score >= 2.5 OR irreversible
# probability >= 0.85 catches 13/15 destructive paraphrases, 1/12 benign
# phrasings, and 0/129 real task titles.
_DESTRUCTIVE_HARM_SCORE_THRESHOLD = 2.5
_DESTRUCTIVE_IRREVERSIBLE_PROBABILITY_THRESHOLD = 0.85

# The Jev questions for the destructiveness judgment: a 5-level harm Score
# and an "irreversible" Noul. Wording and levels are pinned to the measured
# operating point above — changing them invalidates the thresholds.
_DESTRUCTIVE_QUESTIONS = {
    "harm": {
        "type": "score",
        "instructions": (
            "How much irreversible harm could carrying out this task, "
            "exactly as written, cause?"
        ),
        "criteria": [
            "Read-only: only looks things up, reads, researches, summarizes, or drafts without sending",
            "Reversible local change: edits code, files, or tasks in a way version control or undo can recover",
            "External side effect: sends a message or email, posts, pays, or changes something outside this machine",
            "Irreversible local loss: permanently deletes or overwrites files, databases, history, or backups on this machine",
            "Irreversible mass or external loss: wipes large amounts of data, force-pushes over shared history, or deletes accounts, repos, or cloud data",
        ],
    },
    "irreversible": {
        "type": "noul",
        "instructions": (
            "Carrying out this task as written could permanently destroy data or "
            "send something that cannot be recalled."
        ),
    },
}

# `state` context sent alongside the task title — describes the execution
# environment the judgment is being made about.
_DESTRUCTIVE_CONTEXT = (
    "An autonomous agent with full shell, git, email, and calendar access "
    "will execute this task without asking first."
)


def _validate_destructive_answer(value, lo: float, hi: float) -> float:
    """Coerce a raw Jev numeric answer to a genuine finite float in
    `[lo, hi]`, raising `ValueError("invalid answer")` for anything else.

    `bool` is rejected explicitly — Python's `bool` is an `int` subclass,
    so `float(True) == 1.0` would otherwise silently accept it as a valid
    score. NaN, infinity, a non-numeric type, and an out-of-range value are
    all rejected the same way; `_apply_destructive_judgment`'s caller
    catches this alongside a transport failure and treats both identically
    (fields stay `None`, `result` otherwise untouched).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid answer")
    value = float(value)
    if not math.isfinite(value) or not (lo <= value <= hi):
        raise ValueError("invalid answer")
    return value


def _apply_destructive_judgment(result: PreflightResult, title: str) -> PreflightResult:
    """Run the Jev destructiveness judgment and apply `agent_jev_destructive_gate`.

    Runs after `_apply_sanity_gate`, so a title the regex already matched
    keeps its `sane_fatal=True` verdict regardless of this function's
    outcome — the regex always wins, in every gate mode. No-ops entirely
    (fields stay None, `result` otherwise untouched) when the gate is
    `off` — checked first, before the `jev_client` import even runs, so an
    `off` host never imports the client. Also no-ops, with a warning
    logged (exception class name and message, never the title or
    transcript), when Jev isn't configured, the import itself fails, the
    call fails, or the answers don't validate (see
    `_validate_destructive_answer`) — all of these are caught by the same
    `try`/`except` and handled identically.

    `shadow` (the default once a key is configured) records the two
    answers on `result` and changes nothing else. `block` additionally
    parks the task — non-fatal `sane=False`, `sane_fatal` left False, so
    the worker blocks on operator confirmation rather than cancelling —
    when the harm score or the irreversible probability crosses its
    threshold, and only when `result.sane` was still True: a verdict this
    function's own gate already turned False (i.e. the regex match handled
    above) is never overwritten. A `block` park also sets
    `result.destructive_block = True` — a code-thresholded verdict over a
    calibrated probability, distinct from the model's own free-form sanity
    opinion, so `_apply_default_route`'s advisory-only demotion (built for
    that opinion) checks this flag and leaves a `destructive_block` park
    intact even when a default route is configured.
    """
    gate = (settings.agent_jev_destructive_gate or "shadow").strip().lower()
    if gate not in ("off", "shadow", "block"):
        logger.warning(
            "invalid LIFEOS_AGENT_JEV_DESTRUCTIVE_GATE=%r — must be one of "
            "off/shadow/block; falling back to shadow", gate,
        )
        gate = "shadow"

    if gate == "off":
        return result

    try:
        from api.services.jev_client import JevClient, jev_configured

        if not jev_configured():
            return result

        client = JevClient()
        answers = client.ask(
            {"task_title": title, "context": _DESTRUCTIVE_CONTEXT},
            _DESTRUCTIVE_QUESTIONS,
        )
        score = _validate_destructive_answer(answers["harm"]["score"], 0.0, 4.0)
        probability = _validate_destructive_answer(answers["irreversible"]["noul"], 0.0, 1.0)
    except Exception as exc:
        logger.warning("Jev destructive judgment failed: %s: %s", type(exc).__name__, exc)
        return result

    result.destructive_score = score
    result.destructive_probability = probability

    if gate == "block" and result.sane and (
        score >= _DESTRUCTIVE_HARM_SCORE_THRESHOLD
        or probability >= _DESTRUCTIVE_IRREVERSIBLE_PROBABILITY_THRESHOLD
    ):
        result.sane = False
        result.sane_fatal = False
        result.sane_reason = (
            f"Jev destructive judgment: harm {score:.2f}, irreversible {probability:.2f}"
        )
        result.destructive_block = True

    return result


def parse_preflight_response(text: str) -> PreflightResult:
    """Parse the LLM's reply into a PreflightResult.

    Hardens against the common failure modes: ```json``` fences, leading prose,
    missing keys, wrong types. On any parse failure we return a defensible
    default result with `preflight_error` set and `routing=ROUTE_ASK`, so the
    worker parks an untagged task rather than running with junk, while a task
    carrying an explicit routing tag still runs on that route.
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
            sane=True,
            sane_reason="",
            sane_fatal=False,
            preflight_error=f"preflight parse error: {exc}",
            raw={},
        )

    defaults = _defaults()
    budget_raw = raw.get("budget") or {}
    max_tokens_raw = budget_raw.get("max_tokens", defaults.max_tokens)
    budget = PreflightBudget(
        wall_seconds=int(budget_raw.get("wall_seconds", defaults.wall_seconds)),
        max_tokens=int(max_tokens_raw) if max_tokens_raw is not None else None,
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
            # the model put a method-of-execution / engine-selection
            # question into `ambiguity` despite the prompt explicitly
            # excluding those ("Method-of-execution questions ... are NOT
            # ambiguity"). Routing (including LIFEOS_AGENT_DEFAULT_ROUTE,
            #) owns that decision, not the operator-facing block —
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


def _remote_preflight_client():
    """Build the remote-provider `LocalLLMClient`, unconditionally.

    Same construction as the fallback branch (order 3 below): base URL,
    model, key, and timeout all come straight from `settings.remote_llm_*`.
    Callers are responsible for checking `settings.remote_llm_configured`
    first — this never probes reachability itself ("the remote client
    is used unprobed, by design"), so calling it against an unconfigured
    provider will fail on the request itself, not here.
    """
    from api.services.llm_client import LocalLLMClient

    return LocalLLMClient(
        base_url=settings.remote_llm_base_url,
        model=settings.remote_llm_model,
        api_key=settings.remote_llm_api_key,
        timeout=settings.remote_llm_timeout,
    )


def _default_llm_caller(prompt: str) -> str:
    """Production caller: pick a client per `settings.agent_preflight_engine`
   , then run one short completion.

    `agent_preflight_engine` values:

    - `auto` (default): today's priority order, unchanged —
      1. Anthropic, when `settings.anthropic_api_key` is set. Byte-identical
         to the pre-behavior of this function — same import, same
         client construction, same call — because this is the maintainer's
         own install and every other install with a key configured; no
         probe runs on this branch.
      2. Otherwise, the local llama-server, when reachable
         (`LocalLLMClient().is_available()` — one short GET /health, same
         check `local_executor._default_llm_client` uses). Unlike that
         function, this always probes once there's no Anthropic key —
         preflight has no `agent_remote_executor` flag gate to hide behind;
         it just needs *some* usable client.
      3. Otherwise, the remote provider, when `agent_remote_executor`
         and `remote_llm_configured` — mirrors the remote branch of
         `local_executor._default_llm_client`.
      4. Otherwise, raise. `run_preflight`'s existing except-clause already
         degrades this to sane=False/routing=ask — unchanged.
    - `remote`: build the remote provider FIRST (via
      `_remote_preflight_client`), unprobed by design, when
      `settings.remote_llm_configured`. A failure of the call itself (bad
      key, endpoint down, ...) is not caught here — it propagates up to
      `run_preflight`'s existing except-clause, same as every other engine.
      When the provider is unconfigured this raises — a forced engine
      never silently reverts to another one (and never to the Anthropic
      API); `run_preflight`'s except-clause degrades it to routing=ask.
    - `anthropic`: force the Anthropic branch when a key is configured. When
      no key is configured, raises (same fail-closed rule as `remote`).
    - `local`: force the local llama client. Still probed via
      `is_available()` exactly like the `auto` chain's own local branch —
      this is a forced engine with no further engine to fall back to, so an
      unreachable server raises (via a clear message) rather than silently
      trying something else; `run_preflight`'s except-clause degrades that
      the same way it degrades every other `_default_llm_caller` failure.
    - Any other value is treated as `auto`, with one logged warning — never
      a crash over a typo'd env var (mirrors `_apply_default_route`'s own
      invalid-value handling for `LIFEOS_AGENT_DEFAULT_ROUTE`).
    """
    engine = (settings.agent_preflight_engine or "auto").strip().lower()
    if engine not in ("auto", "remote", "anthropic", "local"):
        logger.warning(
            "invalid LIFEOS_AGENT_PREFLIGHT_ENGINE=%r — must be one of "
            "auto/remote/anthropic/local; falling back to auto", engine,
        )
        engine = "auto"

    if engine == "remote":
        if settings.remote_llm_configured:
            client = _remote_preflight_client()
            response = client.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=_PREFLIGHT_MAX_TOKENS,
                temperature=0.0,
            )
            return response.text
        # Fail closed: a forced engine never silently reverts to
        # another one — in particular never to the Anthropic API, which is
        # exactly the spend `remote` exists to avoid. `run_preflight`'s
        # except-clause degrades this to sane=False/routing=ask, so the
        # operator sees a confirmation question, not a surprise API bill.
        raise RuntimeError(
            "LIFEOS_AGENT_PREFLIGHT_ENGINE=remote but the remote provider "
            "is not configured (LIFEOS_REMOTE_LLM_*)"
        )

    elif engine == "anthropic":
        if settings.anthropic_api_key:
            from api.services.llm_client import AnthropicLLMClient

            client = AnthropicLLMClient(model=settings.agent_preflight_model)
            response = client.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=_PREFLIGHT_MAX_TOKENS,
                temperature=0.0,
            )
            return response.text
        raise RuntimeError(
            "LIFEOS_AGENT_PREFLIGHT_ENGINE=anthropic but no Anthropic API "
            "key is configured"
        )

    elif engine == "local":
        from api.services.llm_client import LocalLLMClient

        local_client = LocalLLMClient()
        if not local_client.is_available():
            raise RuntimeError(
                "LIFEOS_AGENT_PREFLIGHT_ENGINE=local but the local "
                "llama-server is unreachable"
            )
        response = local_client.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=_PREFLIGHT_MAX_TOKENS,
            temperature=0.0,
        )
        return response.text

    # engine == "auto".
    if settings.anthropic_api_key:
        # Import inside the branch so tests that monkeypatch run_preflight
        # don't need the Anthropic SDK installed, and so a no-key install
        # never imports it either.
        from api.services.llm_client import AnthropicLLMClient

        client = AnthropicLLMClient(model=settings.agent_preflight_model)
        response = client.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=_PREFLIGHT_MAX_TOKENS,
            temperature=0.0,
        )
        return response.text

    from api.services.llm_client import LocalLLMClient

    local_client = LocalLLMClient()
    if local_client.is_available():
        client = local_client
    elif settings.agent_remote_executor and settings.remote_llm_configured:
        client = _remote_preflight_client()
    else:
        raise RuntimeError(
            "no LLM client available for preflight: no Anthropic API key, "
            "local llama-server unreachable, and no remote provider configured"
        )

    response = client.create(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=_PREFLIGHT_MAX_TOKENS,
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


# Model/engine words that count as the operator naming the Anthropic API
# route themselves. Corroborates the classifier's `routing_explicit`
# before any API dispatch happens without a confirmation — the tags
# are checked separately.
#
# The bare word "cloud" is deliberately excluded from this alternation.
# `#cloud` the tag means the configured remote provider, not the Anthropic
# API, so a title that merely says "cloud" (with no other engine word)
# does not unambiguously name Anthropic. Including it in this regex would
# let such a title corroborate a hallucinated `routing="claude"` straight
# into Anthropic-API spend with no confirmation — exactly the hidden-spend
# shape this guards against. Excluding it means that case falls through to
# `_apply_tag_overrides`'s downgrade-to-`ask` below, which is the safe
# default: inference still asks, never spends.
_TITLE_NAMES_A_CLOUD_ENGINE = re.compile(
    r"(?i)\b(claude|opus|sonnet|haiku|anthropic|api)\b"
)

# Title phrases that corroborate an LLM-chosen `local` route — lifted
# verbatim from the prompt's own rule-3 cue list (`_PREFLIGHT_INSTRUCTIONS`:
# "with local agent", "using gemma"). Used by `_apply_route_corroboration`
# the same way `_TITLE_NAMES_A_CLOUD_ENGINE` is used for cloud: a
# route the model claims is explicit only counts when the title itself
# backs it up.
_TITLE_NAMES_LOCAL_ENGINE = re.compile(
    r"(?i)\b(with\s+local\s+agent|using\s+gemma)\b"
)

# Title phrases that corroborate an LLM-chosen `claude_code` / `codex`
# route. Preflight's own schema never asks the model to emit these two
# routes directly (routing is restricted to "local" | "claude" | "ask" —
# see `_PREFLIGHT_INSTRUCTIONS`); they normally only arrive via the
# `#claude`/`#codex` tags in `_apply_tag_overrides`. `KNOWN_ROUTES` accepts
# them from the model anyway (parse_preflight_response has no per-route
# schema enforcement), so a noncompliant model can emit one — the same
# failure mode exists to catch for `local` — and this gives that case
# a corroboration check too rather than leaving it unguarded.
_TITLE_NAMES_CLAUDE_CODE_ENGINE = re.compile(r"(?i)\bclaude\s+code\b")
_TITLE_NAMES_CODEX_ENGINE = re.compile(r"(?i)\bcodex\b")

# Per-route corroboration pattern consulted by `_apply_route_corroboration`.
# `ROUTE_CLAUDE` is deliberately absent — `_apply_tag_overrides`'s
# downgrade already performs this same title-corroboration check for cloud
# and must be left to run it alone (see that function's docstring).
# `ROUTE_ASK` is absent too: nothing to corroborate, and `_apply_default_route`
# already owns substituting it.
_ROUTE_TITLE_CORROBORATION: dict[str, re.Pattern[str]] = {
    ROUTE_LOCAL: _TITLE_NAMES_LOCAL_ENGINE,
    ROUTE_CLAUDE_CODE: _TITLE_NAMES_CLAUDE_CODE_ENGINE,
    ROUTE_CODEX: _TITLE_NAMES_CODEX_ENGINE,
}

# Tags that constitute the operator's own, direct routing corroboration —
# mirrors the tag names `_apply_tag_overrides` recognizes. Presence of any
# of these means `_apply_route_corroboration` must not second-guess the
# result with a title-cue check: a tag IS the operator naming the engine.
_ROUTE_OVERRIDE_TAG_NAMES = frozenset(
    {"local", "claude", "codex", "hermes", "cloud", "cloud-haiku", "cloud-sonnet"}
)


def _has_route_override_tag(tags: list[str]) -> bool:
    return any(_normalize_tag(t) in _ROUTE_OVERRIDE_TAG_NAMES for t in (tags or []))


def _apply_tag_overrides(result: PreflightResult, tags: list[str], title: str = "") -> PreflightResult:
    """Apply tag-based routing/model overrides (§2 precedence).

    Tag precedence (a tag always wins over preflight's LLM choice):
      `#local`        → routing=local, model=local
      `#claude`       → routing=code   (Claude Code CLI, subscription-billed)
      `#codex`        → routing=codex  (Codex CLI, subscription-billed)
      `#hermes` → routing=hermes (Hermes conversation,; model is
                         whatever Hermes reports per turn — nothing for
                         preflight to select among ALLOWED_MODELS)
      `#cloud-haiku`  → routing=claude, model=claude-haiku-4-5   (Anthropic API, explicit)
      `#cloud-sonnet` → routing=claude, model=claude-sonnet-5    (Anthropic API, explicit)
      `#cloud` → routing=remote, model="" (the configured remote
                         OpenAI-compatible provider, e.g. DeepSeek via
                         Fireworks — NEVER the Anthropic API. If the remote
                         provider isn't configured, the worker parks the task
                         at #agent-blocked rather than falling back to
                         Anthropic; see `worker.py`'s `ROUTE_REMOTE` branch
                         in `_dispatch`.)

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
    if "hermes" in normalized:
        # Hermes route — dispatched through HermesExecutor, which
        # opens a Hermes conversation instead of running a local CLI.
        # Hermes reports its own model per turn (model_readout.py); nothing
        # for preflight to select among ALLOWED_MODELS.
        result.routing = ROUTE_HERMES
        result.routing_reason = "#hermes tag present"
        result.routing_explicit = True
        result.model = ""
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
        # `#cloud` means the configured remote OpenAI-compatible
        # provider (e.g. DeepSeek via Fireworks) — never the Anthropic
        # API. `#cloud-haiku`/`#cloud-sonnet` above are the separate,
        # explicit Anthropic escape hatches. `model` is left "" — the
        # remote provider's model id comes from `settings.remote_llm_model`
        # at dispatch time (the same "the engine picks its own model" pattern the
        # `#claude`/`#codex` branches above use), not something preflight
        # selects among `ALLOWED_MODELS`. Whether the remote provider is
        # actually configured is a worker-side concern (`_dispatch` parks
        # the task at #agent-blocked when it isn't) — preflight's job here
        # is only to name the route.
        result.routing = ROUTE_REMOTE
        result.routing_explicit = True
        if not result.routing_reason:
            result.routing_reason = "#cloud tag present"
        result.model = ""
        return result
    # No tag override. A cloud route that nobody asked for must not dispatch:
    # downgrade it to `ask` so the worker confirms first. The classifier's
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


def _apply_route_corroboration(
    result: PreflightResult, original_routing: str, title: str, tags_list: list[str]
) -> PreflightResult:
    """Demote an uncorroborated LLM-chosen `local`/`claude_code`/`codex`
    route to `settings.agent_default_route` when the title doesn't back it up.

    **The bug this closes.** `_apply_default_route`'s route substitution only
    fires when `result.routing == ROUTE_ASK` — any other value the model
    returned short-circuits past it untouched, on the theory that a routing
    the model was confident enough to name outright should stand. This
    function handles the case where that confidence was fabricated: a
    noncompliant model invented `routing="local"` with a reason that matched
    none of the prompt's rule-3 cues and no tag was present, and because
    "local" isn't `ask`, the operator's `LIFEOS_AGENT_DEFAULT_ROUTE=claude_code`
    never got a look — the session ran locally with full Bash reach into the
    checkout. This closes that gap the same way `_apply_tag_overrides`'s
    cloud-route downgrade already closes it for cloud: a model-claimed route
    counts only when the *title* corroborates it, not merely when the model
    sets `routing_explicit=true` on its own — that flag alone is little more
    than the model's own guess. `routing_explicit` without a title match
    doesn't count; `routing_explicit=False` never counts, regardless of what
    the title says.

    **Scope — why `ROUTE_CLAUDE` is excluded.** `_apply_tag_overrides`'s
    downgrade already runs this exact corroboration check for cloud
    (same "explicit AND title names the engine" shape, over
    `_TITLE_NAMES_A_CLOUD_ENGINE`) — but on a miss, it demotes to `ROUTE_ASK`
    and asks the operator, not to the configured default. That's
    deliberate and this function must not weaken it: an uncorroborated
    cloud route is a real cost decision (per-token API spend), so it stays
    a confirmation question even when a default route is configured for
    everything else — `_apply_default_route`'s own `original_routing`
    check (below) is what keeps that `ask` from being silently rescued into
    the default. Re-running a *second*, differently-shaped corroboration
    check over `ROUTE_CLAUDE` here would risk landing a different verdict
    than `_apply_tag_overrides`'s and double-guessing a flow that already
    does its own job correctly. `ROUTE_ASK` is excluded too — there's
    nothing to corroborate, and `_apply_default_route` already owns
    substituting it.

    **Why this runs on `original_routing`, but *after* `_apply_tag_overrides`
    executes.** The corroboration question is "did the *model* justify this
    on its own", so it must be asked about the routing value from *before*
    any tag ran — `original_routing`, captured in `_finish` right after
    parsing, exactly as `_apply_default_route` already does for its own
    gate. A tag is direct operator corroboration and must never be
    second-guessed by a title-cue check it was never subject to. Comparing
    `result.routing` to `original_routing` for equality would almost work
    as a "was there a tag" signal, but not quite — a `#local` tag landing on
    a model that also independently guessed `"local"` leaves them equal
    without a tag being absent — so this checks `tags_list` directly via
    `_has_route_override_tag` instead and no-ops whenever a recognized
    routing tag is present, full stop.

    Chronologically it still has to run *after* `_apply_tag_overrides`
    finishes, not interleaved with it or before it: if this function wrote
    a demoted route of `ROUTE_CLAUDE` (i.e. the operator configured
    `LIFEOS_AGENT_DEFAULT_ROUTE=claude`) before `_apply_tag_overrides` ran,
    that write would walk straight into the downgrade-to-`ask` gate
    inside `_apply_tag_overrides` and get bounced right back to `ask` —
    undoing the very substitution the operator's default-route setting
    asked for. Running after `_apply_tag_overrides` means that gate has
    already had its say on the model's *original* cloud-or-not verdict and
    won't re-examine a value it never produced. It also runs before
    `_apply_default_route`: that function's ambiguity demotion and
    ask-substitution are unaffected either way (they only touch `ask`
    outcomes, which this function never produces), so the relative order
    between the two doesn't matter for correctness — but sitting this one
    directly after `_apply_tag_overrides`, next to the gate it mirrors,
    keeps the two corroboration checks readable side by side.

    No-op (returns `result` unchanged) whenever: no default route is
    configured, the configured value is invalid (`_apply_default_route`,
    called immediately afterward, owns that validation and its loud ERROR
    log — duplicating it here would double-log), a recognized routing tag is
    present, `original_routing` isn't one of `local`/`claude_code`/`codex`,
    or the route is corroborated. So an unset `LIFEOS_AGENT_DEFAULT_ROUTE`
    leaves routing byte-identical to skipping this function entirely.
    """
    if not settings.agent_default_route or settings.agent_default_route not in KNOWN_ROUTES:
        return result
    if _has_route_override_tag(tags_list):
        return result  # a tag is the operator's own corroboration — never second-guessed

    corroboration_re = _ROUTE_TITLE_CORROBORATION.get(original_routing)
    if corroboration_re is None:
        return result # ask (nothing to corroborate) or claude (the cloud downgrade above owns it)

    if result.routing_explicit and corroboration_re.search(title or ""):
        return result  # corroborated by the title — the route stands

    default_route = settings.agent_default_route
    logger.info(
        "preflight route demoted to default (LIFEOS_AGENT_DEFAULT_ROUTE=%s "
        "configured, uncorroborated %s route not backed by title %r; "
        "model's routing_reason was %r)",
        default_route, original_routing, title, result.routing_reason,
    )
    result.demoted_routing = original_routing
    result.routing = default_route
    result.routing_reason = (
        f"uncorroborated {original_routing} route demoted to "
        f"LIFEOS_AGENT_DEFAULT_ROUTE={default_route}"
    )
    # This substitution is the operator's standing config, not something the
    # model or a tag named — leave routing_explicit False (mirrors the cloud
    # downgrade above, which resets it the same way).
    result.routing_explicit = False
    if default_route == ROUTE_CLAUDE:
        result.model = MODEL_SONNET
    elif default_route == ROUTE_LOCAL:
        result.model = MODEL_LOCAL
    elif default_route in (ROUTE_CLAUDE_CODE, ROUTE_CODEX):
        # Mirrors the `#claude`/`#codex` tag overrides above: the CLI picks
        # its own model. `result.model` may already hold a stale value from
        # `original_routing`'s own tail assignment just above (e.g. "local"
        # if the model's own guess was ROUTE_LOCAL) — clear it rather than
        # leave a model/routing mismatch behind.
        result.model = ""
    # ROUTE_ASK: harmless no-op combination — leave model as-is.
    return result


def _apply_default_route(result: PreflightResult, original_routing: str) -> PreflightResult:
    """Apply `settings.agent_default_route`, and demote any non-null
    `ambiguity` and any non-fatal `sane=False` to advisory once that
    setting is configured and valid. Empty setting (default) is a no-op,
    so an unset install is byte-identical to skipping this function
    entirely; all three parts of this function are gated on the setting
    being non-empty and valid.

    Three independent things happen here, in order:

    1. **Ambiguity demotion.** Configuring a default route is the
       operator saying "run untagged tasks without asking me" — a cheap
       classifier's hedging (`ambiguity`) shouldn't override that standing
       instruction, regardless of what routing was ultimately picked. So
       once the setting is confirmed non-empty and valid, any non-null
       `result.ambiguity` is stashed on `result.demoted_ambiguity` (for the
       worker to log as advisory context) and cleared. String-matching the
       question text (`_is_routing_flavored_ambiguity`'s approach above) is
       whack-a-mole — the model keeps rephrasing around the pattern — so
       this demotes unconditionally on
       the *value being non-null* rather than trying to classify its prose.

    2. **Sanity demotion.** The same standing-instruction argument
       applies to a non-fatal `sane=False`: the classifier has repeatedly
       called ordinary feature requests "not a task an agent can execute",
       and treating that opinion as authoritative when the operator has
       already told the system to run untagged tasks costs a confirmation
       round-trip on legitimate work. So once the setting is confirmed
       non-empty and valid, a `sane=False` that is NOT `sane_fatal` and NOT
       `destructive_block` is demoted the same way ambiguity is:
       `sane_reason` is stashed on `result.demoted_sanity` and
       `result.sane` is set back to True. `sane_fatal` verdicts — the
       empty-title short-circuit and the deterministic destructive-title
       regex (`_apply_sanity_gate`) — are code-established, not the
       model's opinion, and this check's `not result.sane_fatal` guard
       leaves them completely untouched: they still fail closed in the
       worker regardless of this setting. `destructive_block` verdicts
       (`_apply_destructive_judgment`'s `block` gate) are excluded the same
       way, for the same reason: a code-thresholded Jev verdict over a
       calibrated probability, not the classifier's own free-form opinion
       this demotion exists to unblock — a `destructive_block` park stays
       parked regardless of this setting. A failed
       or unparseable preflight call sets `sane=True`/`sane_fatal=False`
       already (see `preflight_error`), so it has nothing left for this
       step to demote. Demoting sanity here (rather than only
       logging it) also means part 3's `not result.sane` half of its gate
       doesn't block route substitution for a demoted verdict — a
       demoted sanity objection is exactly as "resolved" as a demoted
       ambiguity, so it should be able to reach the default route the same
       way.

    3. **Route substitution.** Gate: `result.routing == ROUTE_ASK and
       result.sane` (the `ambiguity is None` half of the old gate is now
       always true here, since part 1 just cleared it; and `result.sane` is
       now True whenever part 2 demoted a non-fatal objection)
       `and original_routing == ROUTE_ASK and not result.preflight_error`.
       Deliberately NOT a string match against `routing_reason` — the LLM's
       reason text is free-form prose ("no tag and no title cue" today, but
       not a stable contract), so matching on it would be brittle. The
       `result.sane` half of this gate covers the one remaining fail-closed
       case: a *fatal* sanity failure (the empty-title short-circuit or the
       deterministic destructive-title regex), which part 2 never demotes,
       so `not result.sane` still blocks it here.

       The `not result.preflight_error` clause excludes a second case that
       `result.sane` alone can't: a failed or unparseable classifier call
       never obtained a verdict at all, so it sets `sane=True` (there is
       nothing to be un-sane about) rather than a fatal sanity failure — but
       it must still not be silently auto-dispatched onto the configured
       default route. Without a routing tag telling it otherwise, that case
       stays `ask` so the operator is asked, exactly like the
       "lack of cues" case this gate exists to substitute — the difference
       being that here the classifier never got to render an opinion, cues
       or no cues.

       One more `ask` source needs excluding: `_apply_tag_overrides`'s
       downgrade, which turns an *inferred* (unconfirmed) cloud route into
       `ask` specifically so it can't auto-dispatch. That result is also
       sane=True, so it would slip through the gate above — this isn't
       "lack of cues", it's a cue nobody confirmed, and silently routing it
       via the default would undo the confirmation exists for.
       Excluded via `original_routing`: the routing value from BEFORE
       `_apply_tag_overrides` ran. Only when the original LLM/parse outcome
       was *already* `ask` — not downgraded from `claude` — do we know this
       is genuinely nothing-to-route-on. (Ambiguity/sanity demotion in parts
       1-2 still run on this path — the downgrade blocks via
       `routing == ask` either way, so demoting the ambiguity/sanity text
       just avoids a redundant question, not a redundant block.)
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
        # Deliberately checked (and returned on) BEFORE ambiguity/sanity
        # demotion — a misconfigured setting is not a *valid* standing
        # instruction, so it must not weaken either block.
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

    # `destructive_block` is excluded from this demotion: it's a code-
    # thresholded Jev verdict over a calibrated probability, not the cheap
    # classifier's own free-form "not executable" opinion this demotion
    # exists to unblock — see `_apply_destructive_judgment`.
    if not result.sane and not result.sane_fatal and not result.destructive_block:
        logger.info(
            "preflight sanity objection demoted to advisory (LIFEOS_AGENT_DEFAULT_ROUTE=%s "
            "configured): %r", default_route, result.sane_reason,
        )
        result.demoted_sanity = result.sane_reason
        result.sane = True

    if result.routing != ROUTE_ASK or not result.sane:
        return result
    if original_routing != ROUTE_ASK:
        return result
    if result.preflight_error:
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


def _apply_preset_class(result: PreflightResult, tags: list[str], title: str = "") -> PreflightResult:
    """Set `result.preset_class` from an explicit `#<class>` tag if present;
    else from the Jev fan-out judgment (`jev_task_routing.judge_task`) when
    it clears two guards; else leave it unset (today's default — the
    worker treats an unset `preset_class` as `fullstack`, no tool
    filtering).

    A tag override always wins outright — checked before even a pre-set
    `result.preset_class` (an LLM/caller value; no classifier emits one
    today, but the precedence holds regardless) — and over the Jev
    judgment. A wrong narrow class is worse than the unfiltered default,
    since it removes tools from the session — so the Jev class is only
    honored when BOTH: `preset_class.confidence >= 0.7`, AND
    `software_work.noul < 0.5` (a task the judgment itself flags as likely
    software work always keeps the full toolset, no matter how confident
    the class choice is). Either guard failing, no Jev judgment at all (no
    TypeSafe key, or the call failed), or a missing `software_work`
    answer, leaves `preset_class` unset — the same today's-default
    behavior as a task with no explicit class tag.
    """
    forced = _detect_preset_class_from_tags(tags)
    if forced:
        result.preset_class = forced
        return result
    if result.preset_class:  # honor an LLM/caller pre-set value
        return result

    from api.services.jev_task_routing import judge_task

    judgment = judge_task(title)
    if judgment is not None and judgment.preset_class is not None:
        answer = judgment.preset_class
        software = judgment.software_work
        likely_software = (
            software is not None
            and software.noul is not None
            and software.noul >= 0.5
        )
        if answer.confidence >= 0.7 and answer.choice and not likely_software:
            result.preset_class = answer.choice
    return result


def _apply_cost_gates(result: PreflightResult) -> PreflightResult:
    """Compute the cache-cold cost estimate and apply §6 + §7 gates.

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
    #
    # ROUTE_REMOTE (the `#cloud` tag's remote OpenAI-compatible
    # provider) is real per-token spend too, but is deliberately NOT gated
    # here. The §6/§7 confirmation ceremony exists specifically for the
    # Anthropic API — the operator's standing "expensive exception" — not
    # for third-party spend in general; the remote provider is treated like
    # local/CLI for preflight cost-preview purposes, the same way
    # `local_executor._default_llm_client`'s remote-fallback path never
    # triggers this gate either. Real spend
    # still records correctly regardless — see
    # `local_executor.LocalExecutor._record_spend`'s `is_remote` branch.
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
    sanity gate > Jev destructive judgment > tag overrides > route
    corroboration > default route (which also demotes ambiguity and sanity,
    see step 4 below) > preset class > cost gates.

    Precedence, and why each sits where it does:

      1. **Sanity gate first, and orthogonal to routing.**
         `_apply_sanity_gate` runs before routing is decided at all, because
         "should this run" is a different question from "how should it
         run". It only ever *establishes* `sane_fatal` (from the
         deterministic destructive-title regex); it never demotes anything.
         A `sane_fatal` verdict fails the task closed regardless of
         routing/ambiguity/default-route configuration (checked immediately
         in the worker, before any of them is consulted) — the ambiguity
         and sanity demotions in `_apply_default_route` (step 4) both
         leave this untouched, since a default route answers "who
         resolves an open question" / "does this cheap opinion outrank a
         standing instruction", never "should this task run at all" once
         the code itself has flagged it destructive. A non-fatal
         `sane=False` — the model's own inferred opinion, not code-
         established — parks the task by default, unless step 4 below
         demotes it.
      1a. **Jev destructive judgment** (`_apply_destructive_judgment`) runs
         immediately after the sanity gate, so a regex-matched title's
         `sane_fatal=True` is already in place and is never overwritten by
         this step (it only acts when `result.sane` is still True). In
         `block` mode, a threshold crossing sets a non-fatal `sane=False`
         with `result.destructive_block=True` — the same shape a non-fatal
         sanity opinion carries, but marked as a code-thresholded verdict
         rather than the classifier's own opinion. Step 4's sanity
         demotion checks that flag and leaves a `destructive_block` park in
         place even when a default route is configured, unlike an ordinary
         non-fatal sanity objection.
      2. **Tags** (`_apply_tag_overrides`) — the operator retagging a task
         is the most direct, most recent signal available; always wins,
         over both the model's routing and `_apply_route_corroboration`.
      3. **Corroborated LLM route** — a routing the model returned (not
         `ask`) stands only when the *title* backs it up
         (`_apply_route_corroboration`). Without this check, any non-`ask`
         value from the model — including one it invented with no cue at
         all — skips `_apply_default_route`'s substitution entirely,
         because that function only fires on `ask`. A model that
         hallucinates `routing="local"` with a fabricated-sounding reason
         would therefore silently beat a configured
         `LIFEOS_AGENT_DEFAULT_ROUTE`. `_apply_route_corroboration` closes
         that: for `local`/`claude_code`/`codex`, an LLM route needs
         `routing_explicit=True` *and* a matching rule-3-style title cue to
         survive; otherwise it's demoted to the configured default and
         logged (`demoted_routing`), mirroring how step 4 demotes
         `ambiguity`. `ROUTE_CLAUDE` is excluded from this step on purpose
         — see point 3a.
      3a. **Cloud is `_apply_tag_overrides`'s job, not `_apply_route_
         corroboration`'s.** An uncorroborated cloud route already gets
         its own, older corroboration check inside `_apply_tag_overrides`
         — same "explicit AND title names the engine" shape, but it
         demotes a miss to `ROUTE_ASK` (a confirmation question) rather
         than to the default route, because API spend is a real cost
         decision that must stay a question even on an install with a
         default route configured for everything else.
         `_apply_route_corroboration` running *after* `_apply_tag_overrides`
         (not before, not interleaved) is what keeps the two independent:
         if it ran first and wrote a demoted route of `claude` (an operator
         could configure `LIFEOS_AGENT_DEFAULT_ROUTE=claude`), that write
         would walk straight back into `_apply_tag_overrides`'s downgrade
         gate and get bounced to `ask`, undoing the demotion the
         default-route setting asked for.
      4. **Default route** (`_apply_default_route`) — substitutes
         `ask` for the configured route only when nothing above produced a
         real answer. This step *also* demotes a non-null `ambiguity` to
         advisory (logged, not blocking) whenever the setting is configured
         and valid — independent of whether step 4's route substitution
         itself fires, since a corroborated route (step 3) can still carry
         a stale ambiguity the model should not have set. It does the same
         for a non-fatal `sane=False`: the model's own "not executable"
         opinion is demoted to advisory (`demoted_sanity`, logged, not
         blocking) under the identical gate, and `sane` is set back to True
         in the same step — a demoted sanity objection is therefore just as
         capable of clearing this same step's own route-substitution gate
         as a demoted ambiguity is, since that gate also requires
         `result.sane`. `sane_fatal` verdicts never reach this demotion
         (step 1 already established `sane_fatal` and the demotion is
         gated on `not result.sane_fatal`), so fail-closed behavior for
         empty titles and destructive-title matches is unaffected regardless
         of the setting. A preflight-call/parse error is excluded from route
         substitution by this same step's own `not result.preflight_error`
         clause instead — see `_apply_default_route` — since it carries no
         verdict for this demotion to act on in the first place. This step's
         own `original_routing` exclusion (only substitutes when the
         *pre-tag* routing was already `ask`) is what keeps
         `_apply_tag_overrides`'s downgraded-to-`ask` cloud case from being
         rescued here too — see 3a.
      5. **Ask** — the fallback when nothing above resolved routing, or the
         unconfirmed-cloud downgrade parked it there.

    Centralized (rather than each return path chaining the calls itself) so
    `original_routing` is captured exactly once, right after the
    parse/short-circuit result is built and before `_apply_tag_overrides`
    (the only thing that mutates `result.routing` ahead of the route-
    corroboration and default-route hooks) gets a chance to change it. See
    `_apply_route_corroboration` and `_apply_default_route` for why that
    pre-tag-override value matters to each.
    """
    result = _apply_sanity_gate(result, title)
    result = _apply_destructive_judgment(result, title)
    original_routing = result.routing
    result = _apply_tag_overrides(result, tags_list, title)
    result = _apply_route_corroboration(result, original_routing, title, tags_list)
    result = _apply_default_route(result, original_routing)
    result = _apply_preset_class(result, tags_list, title)
    result = _apply_cost_gates(result)
    return result


def run_preflight(
    title: str,
    tags: list[str] | None = None,
    caller: PreflightCaller | None = None,
) -> PreflightResult:
    """Run the Haiku preflight call. Returns a defensible PreflightResult even
    on errors — a failed or unparseable classifier call routes to `ask` (with
    `preflight_error` set) rather than raising, so the worker can always make
    a decision. An empty title is the one path that still fails closed
    (sane=False, sane_fatal=True) — that's a deterministic finding, not a
    failed call.
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
                sane=True,
                sane_reason="",
                sane_fatal=False,
                preflight_error=f"preflight LLM call failed: {exc}",
                raw={},
            ),
            tags_list,
        )

    return _finish(parse_preflight_response(reply), tags_list, title)
