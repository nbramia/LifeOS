"""
Agentic chat loop for LifeOS.

Runs a multi-turn conversation where the model can call tools
autonomously. Implemented as an async generator that yields events so the
caller (SSE endpoint) can stream them to the client in real time.

Uses the local LLM (OpenAI-compatible llama-server) by default.

Event types yielded:
  {"type": "turn_state", "result": AgentResult} -- live, mutable accumulator
                                                     (first event; #615)
  {"type": "text",   "content": "..."}       -- streamed text chunk
  {"type": "status", "message": "..."}       -- tool execution status
  {"type": "self_correction"}                -- model retrying (consumers should clear buffered text)
  {"type": "result", "result": AgentResult}  -- final result (last event)
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator
from api.services.agent_system_prompt import build_system_prompt
from api.services.agent_tools import TOOL_DEFINITIONS, TOOL_STATUS_MESSAGES, execute_tool_parallel, begin_email_send_turn
from api.services.synthesizer import build_message_content
from api.services.perf_trace import trace_span
from api.services.llm_client import get_local_llm, openai_tool_calls_to_anthropic, LLMUsage, LocalLLMClient
from api.services.resilience import is_retryable_api_error
from config.settings import settings

logger = logging.getLogger(__name__)

# Consolidated tools that use sub-action status messages
_CONSOLIDATED_TOOLS = {"manage_tasks", "manage_reminders", "manage_schedules", "person_info"}

# Patterns that indicate the model is giving up without trying tools
_GIVE_UP_PATTERNS = re.compile(
    r"(?i)("
    r"can'?t access|cannot access|unable to access"
    r"|can'?t browse|cannot browse|unable to browse"
    r"|don'?t have access to|do not have access to"
    r"|can'?t search the (web|internet)|cannot search the (web|internet)"
    r"|knowledge cutoff|training data"
    r"|can'?t provide real-?time|cannot provide real-?time"
    r"|can'?t look up|cannot look up|unable to look up"
    r"|don'?t have the ability|do not have the ability"
    r"|can'?t fetch|cannot fetch|unable to fetch"
    r"|as of my last|as of my knowledge"
    r"|I don'?t have (?:access to )?(?:live|current|real-?time|up-to-date)"
    r")"
)

SELF_CORRECTION_NUDGE = (
    "Stop — you DO have a search_web tool. Use it now to answer the question "
    "with current information. Do not apologize or explain limitations, just "
    "call search_web and answer."
)


def _looks_like_giving_up(text: str) -> bool:
    """Return True if the response text contains give-up phrases."""
    return bool(_GIVE_UP_PATTERNS.search(text))


# Phantom-write guard: a reply that opens with "Logged …" / "Updated …" claims a
# state change. If the turn made ZERO tool calls, nothing was written — the model
# pattern-matched earlier confirmation lines in the conversation history instead
# of calling the tool (observed with the fitness bot on a weak model: "Logged …"
# replies with tool_rounds=0 silently lost sets). Scope: only the zero-tool-call
# turn is caught; a turn that makes a READ call and then claims a write still
# passes — that variant hasn't been observed and distinguishing reads from
# writes here isn't worth the tool-registry coupling yet.
_WRITE_CLAIM_PATTERN = re.compile(r"(?i)^\s*(logged|updated|recorded|saved)\b")

PHANTOM_WRITE_NUDGE = (
    "Stop — you replied as if something was recorded, but you made NO tool call "
    "this turn, so nothing was saved. If the user's CURRENT message contains new "
    "data to record (a workout, a metric, a task…), call the appropriate tool NOW "
    "to actually record it, then confirm. If it was already recorded in a "
    "PREVIOUS turn, do NOT record it again — just answer the question. If "
    "nothing needed recording, rephrase your answer without claiming anything "
    "was logged or updated."
)


def _claims_write_without_tools(text: str) -> bool:
    """Reply asserts a write ('Logged …') — only meaningful when no tools ran."""
    return bool(_WRITE_CLAIM_PATTERN.match(text))


# Cross-turn escalation (#303). A weak orchestrator sometimes declares something
# impossible / unavailable / "not released" from stale training and won't budge.
# When the PRIOR assistant turn refused like that AND the user's NEW message pushes
# back, we retry the turn on a stronger model rather than re-refusing.
# Scoped to the *stale-knowledge / world-fact* refusal class — claims that
# something about the current world isn't released/announced/scheduled/known.
# A stronger model + web search fixes these. Deliberately NOT matching generic
# data-lookup negatives ("I can't find any emails from Sarah", "no such
# contact", "that file doesn't exist") — those are usually correct, and a
# pricier model can't find data that isn't there. The give-up patterns
# (knowledge-cutoff / can't-access-live) are also treated as refusals (see
# should_escalate).
_REFUSAL_PATTERNS = re.compile(
    r"(?i)("
    r"(hasn'?t|has not|haven'?t|have not)\s+(yet\s+)?(been\s+)?"
    r"(released|announced|published|scheduled|finalized|determined|set|made public|come out)"
    r"|not\s+(yet\s+)?(been\s+)?(released|announced|published|scheduled|finalized|determined|available|out)"
    r"|isn'?t\s+(yet\s+)?(available|out|released|published|finalized)"
    r"|aren'?t\s+(yet\s+)?(available|released|published)"
    r")"
)
_PUSHBACK_PATTERNS = re.compile(
    r"(?i)("
    r"do (the )?research|do more research"
    r"|you'?re wrong|that'?s wrong|that'?s (not|in)correct|that'?s not (true|right)"
    r"|look it up|search (for it|again|the web|online)|try again|check again"
    r"|it should be possible|it is possible|yes it (has|is|did|does)"
    r"|i'?m telling you|i know (it|they|you|for a fact)"
    r"|that'?s not true|actually,? (it|that|they|the)"
    r"|they have been|it has been (released|announced|published)"
    r")"
)


def _role_content(msg) -> tuple[str, str]:
    """Extract (role, content) from a Message object or dict; '' for missing."""
    role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    return (role or ""), (content if isinstance(content, str) else "")


def _last_assistant_text(conversation_history) -> str:
    """Return the most recent assistant message's text, or '' if none."""
    for msg in reversed(conversation_history or []):
        role, content = _role_content(msg)
        if role == "assistant":
            return content
    return ""


def _is_refusal(text: str) -> bool:
    """A stale-knowledge world-fact negative OR a give-up phrase — both fixable
    by a stronger model + web search."""
    return bool(_REFUSAL_PATTERNS.search(text or "") or _GIVE_UP_PATTERNS.search(text or ""))


def should_escalate(conversation_history, question: str) -> bool:
    """True when the prior assistant turn refused/claimed-impossible AND the
    current user message pushes back — the signal to retry on a stronger model."""
    prior = _last_assistant_text(conversation_history)
    if not prior or not _is_refusal(prior):
        return False
    return bool(_PUSHBACK_PATTERNS.search(question or ""))


def _count_escalation_cycles(conversation_history) -> int:
    """Count *completed* refusal→pushback cycles in the trailing chain (#305c).

    The current (in-flight) pushback isn't in history; this counts how many
    times the user already pushed back against a refusal in the immediately
    preceding alternating chain (… R P R P R). It is NOT a raw refusal count —
    refusals the user never pushed back on (e.g. an earlier topic) don't inflate
    the rung. The chain breaks at the first normal exchange (a non-refusing
    assistant turn or a non-pushback user turn). rung = this count.
    """
    cycles = 0
    for msg in reversed(conversation_history or []):
        role, content = _role_content(msg)
        if role == "assistant":
            if not _is_refusal(content):
                break
        elif role == "user":
            if not _PUSHBACK_PATTERNS.search(content):
                break
            cycles += 1
    return cycles


def _original_request(conversation_history, fallback: str) -> str:
    """The user's original ask before the pushback chain — the most recent user
    turn that ISN'T a pushback. Used so an engine handoff at the top of the
    ladder gets the real task, not the bare pushback that triggered it."""
    for msg in reversed(conversation_history or []):
        role, content = _role_content(msg)
        if role == "user" and not _PUSHBACK_PATTERNS.search(content):
            return content or fallback
    return fallback


# Engines the orchestrator may climb to on its own. All three are free of
# per-token API cost: `claude_code` and `codex` bill the operator's CLI
# subscriptions, `local` runs the on-box Gemma. An Anthropic model id is NOT on
# this list — automatic escalation must never spend API credits, so a model rung
# is dropped from the climb (#584). The operator can still name a model
# themselves ("escalate to opus"), which is an explicit request, not an
# escalation LifeOS chose.
NON_API_RUNGS = ("claude_code", "codex", "local")

# Default climb when no ladder is configured: the strongest subscription engine
# first, then the other one. `local` is a legal rung but not a default one —
# escalation fires *because* a turn failed, and the on-box model is weaker at
# tool use than the Haiku it would be replacing.
DEFAULT_LADDER = ["claude_code", "codex"]


def _escalation_ladder(escalation_model: str) -> list[str]:
    """Ordered escalation rungs the orchestrator may climb automatically.

    Only non-API rungs (see ``NON_API_RUNGS``) survive: LifeOS escalating on its
    own must not put a turn on the Anthropic API (#584). A configured
    ``agent_escalation_ladder`` is filtered rather than rejected, so an existing
    'claude-sonnet-4-6,claude_code' setting keeps working — it just climbs
    straight to the engine. ``escalation_model`` no longer contributes a rung of
    its own; it survives only as the gate that says escalation is configured at
    all, and as the target for a user-directed "escalate" (handled separately in
    ``resolve_orchestrator_model``).

    Empty when nothing is left to climb to. Deduped, order preserved.
    """
    raw = (getattr(settings, "agent_escalation_ladder", "") or "").strip()
    if raw:
        rungs = [r.strip() for r in raw.split(",") if r.strip()]
    elif escalation_model:
        rungs = list(DEFAULT_LADDER)
    else:
        return []
    seen, out = set(), []
    for r in rungs:
        if r in seen:
            continue
        seen.add(r)
        if r not in NON_API_RUNGS:
            logger.info(
                "escalation ladder: dropping API rung %r — automatic escalation "
                "is limited to %s", r, ", ".join(NON_API_RUNGS),
            )
            continue
        out.append(r)
    return out


# User-directed escalation (#305). Tier words the operator can name in a chat
# message map to concrete Anthropic model ids. Update the opus id here if the
# account is pinned to a different opus release.
_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}
# A directive verb immediately followed by a tier word: "escalate to opus",
# "use sonnet", "with claude opus", "switch to haiku", "retry on opus".
_DIRECTIVE_MODEL_RE = re.compile(
    r"(?i)\b(?:escalate(?:\s+to)?|use|using|with|switch\s+to|retry\s+(?:with|on)|try\s+(?:with|on))\s+"
    r"(?:claude\s+)?(opus|sonnet|haiku)\b"
)
# A generic "use a stronger model" directive with no tier named → fall back to
# the configured escalation model. Requires an explicit model reference so a
# bare "escalate this ticket to the team" doesn't trigger a model swap.
_DIRECTIVE_SMARTER_RE = re.compile(
    r"(?i)("
    r"(?:use|using|try|switch\s+to|escalate\s+to)\s+(?:a\s+)?"
    r"(?:smarter|stronger|better|bigger|more\s+capable)\s+(?:model|llm)"
    r"|escalate\s+the\s+(?:model|response|answer)"
    r")"
)
# A directive negated or posed as a meta-question is NOT a request to switch.
# Negation anywhere in the run-up to the directive ("don't … use opus",
# "I didn't ask you to use opus") cancels it; question framing about a model
# ("why did you use sonnet?", "can you use opus?") is likewise not a directive.
# Only true negations — NOT "instead of"/"rather than", which contrast options
# and leave the *named* model as the desired one ("instead of haiku, use opus").
_NEGATION_BEFORE_RE = re.compile(
    r"(?i)\b(don'?t|do not|didn'?t|did not|never|no need to|without)\b"
)
_META_QUESTION_RE = re.compile(
    r"(?i)\b(why|can|could|should|would|do|did|are)\s+(?:you|i|we|they)?\s*"
    r"(?:use|using|used|switch(?:ing)?|escalate)\b"
)
# Engine names that aren't an inline model swap — _parse_escalation_directive
# returns '' for these so naming one never becomes a model escalation. (codex /
# claude code ARE wired for a handoff via parse_engine_directive; gpt / gemini
# are recognized only to keep them out of the model-escalation path.)
_UNSUPPORTED_ENGINE_RE = re.compile(r"(?i)\b(codex|claude\s*code|gpt|gemini)\b")

# An engine handoff is an IMPERATIVE command, recognized in two shapes:
#   - LEADING:  "use codex to add X", "with claude code, …", "hand this to codex: …"
#   - TRAILING: "add the games using codex", "fix the bug with claude code"
# Both are anchored (start / end), so an incidental mid-sentence mention
# ("remind me to use codex tomorrow", "what time do I usually use codex") never
# spawns a worker subprocess.
_ENGINE_DIRECTIVE_LEAD_RE = re.compile(
    r"(?i)^\s*(?:please\s+|ok,?\s+|hey,?\s+)?"
    r"(?:use|using|with|via|escalate\s+to|hand\s+(?:this\s+|it\s+)?(?:to|off\s+to)"
    r"|switch\s+to|run\s+(?:this\s+|it\s+)?(?:with|on|in))\s+(codex|claude\s*code)\b"
)
# Trailing: the engine phrase ends the message ("<task> using codex").
_ENGINE_DIRECTIVE_TRAIL_RE = re.compile(
    r"(?i)\b(?:using|with|via|through)\s+(codex|claude\s*code)\s*[.!?]*$"
)
# A message starting with one of these is a statement/question, NOT an imperative
# command — so a trailing "with codex" shouldn't route ("I've been working with
# codex", "the report should run with codex"). Leading-form handoffs are exempt
# (they begin with the directive verb itself).
_NON_COMMAND_LEAD_RE = re.compile(
    r"(?i)^\s*(i|i'?m|i'?ve|i'?d|i'?ll|you|we|my|me|what|what'?s|why|how|when|where|who"
    r"|is|are|was|were|do|does|did|can|could|would|should|has|have|had|the|a|an|there"
    r"|it|that|this|maybe|perhaps)\b"
)
# Strips a leading connector left after removing a leading directive ("...to add X").
_LEADING_CONNECTOR_RE = re.compile(r"(?i)^(?:to|and|please)\s+")
# Strips a trailing model phrase so "use codex with opus" doesn't leak "with
# opus" into the spawned task (the CLI engine picks its own model).
_TRAILING_MODEL_RE = re.compile(r"(?i)\b(?:with|using|on)\s+(?:claude\s+)?(?:opus|sonnet|haiku)\s*$")


def parse_engine_directive(question: str) -> tuple[str, str]:
    """Detect an explicit CLI-engine handoff directive (#305 part b).

    Returns ``(engine, task)`` where engine is "codex" / "claude_code" / "".
    Two imperative shapes route — leading ("use codex to add X") and trailing
    ("add the games using codex"); a mid-sentence mention or a statement
    ("I've been working with codex") does not. ``task`` is the request with the
    directive phrase removed, falling back to the full question if stripping
    leaves nothing. Negations ("don't use codex") and meta-questions ("why use
    codex?") return ("", question).
    """
    q = question or ""
    if _META_QUESTION_RE.search(q):
        return "", q

    # Leading imperative: directive at the start, task follows.
    m = _ENGINE_DIRECTIVE_LEAD_RE.search(q)
    if m and not _NEGATION_BEFORE_RE.search(q[:m.start()]):
        engine = "codex" if "codex" in m.group(1).lower() else "claude_code"
        cleaned = q[m.end():].strip(" ,.:;-\t")
        cleaned = _LEADING_CONNECTOR_RE.sub("", cleaned).strip(" ,.:;-")
        cleaned = _TRAILING_MODEL_RE.sub("", cleaned).strip(" ,.:;-")
        return engine, (cleaned or q)

    # Trailing: "<task> using codex" — only when the message reads as a command
    # (doesn't start with a pronoun/question/copula word).
    mt = _ENGINE_DIRECTIVE_TRAIL_RE.search(q)
    if mt and not _NON_COMMAND_LEAD_RE.search(q) and not _NEGATION_BEFORE_RE.search(q[:mt.start()]):
        engine = "codex" if "codex" in mt.group(1).lower() else "claude_code"
        cleaned = q[:mt.start()].strip(" ,.:;-\t")
        return engine, (cleaned or q)

    return "", q


def _parse_escalation_directive(question: str, escalation_model: str) -> str:
    """Return the model id the user explicitly asked for, or ''.

    A named tier ("use opus") resolves to that model even when auto-escalation
    is unconfigured — explicit intent. A generic "use a smarter model" falls
    back to ``escalation_model`` (which may be ''). Negated/meta-question
    framing and unsupported-engine names ("escalate to codex") return ''.
    """
    q = question or ""
    # Don't escalate to a model when the user named an engine we can't hand off
    # to yet, or framed the model mention as a question.
    if _UNSUPPORTED_ENGINE_RE.search(q) or _META_QUESTION_RE.search(q):
        return ""
    m = _DIRECTIVE_MODEL_RE.search(q)
    if m:
        # A negation before the matched directive cancels it.
        if _NEGATION_BEFORE_RE.search(q[:m.start()]):
            return ""
        return _MODEL_ALIASES.get(m.group(1).lower(), "")
    if _DIRECTIVE_SMARTER_RE.search(q):
        return escalation_model
    return ""


def resolve_orchestrator_model(
    conversation_history, question: str, base_model: str, escalation_model: str
) -> tuple[str, bool]:
    """Pick the model for this turn. Returns (model, escalated).

    Precedence:
      1. An explicit user directive ("escalate to opus", "use sonnet", or a
         generic "use a smarter model") — honored regardless of the heuristic,
         since the intent is unambiguous. A named tier works even when
         ``escalation_model`` is unset.
      2. The auto-escalation ladder (#305c): on refuse→pushback, climb to the
         rung matching how many times the model has consecutively refused
         (1st → rung 0, 2nd → rung 1, …). The returned model may be an engine
         name (codex / claude_code) for the top rung — the caller hands that
         off to a worker session rather than running the loop on it.
    Otherwise returns ``base_model`` unchanged.
    """
    directed = _parse_escalation_directive(question, escalation_model)
    if directed and directed != base_model:
        return directed, True
    if should_escalate(conversation_history, question):
        # Auto-escalation is on when an explicit ladder is configured, or when
        # escalation_model is set to something other than base (the #304 gate —
        # escalation_model == base means "no stronger model", i.e. disabled).
        explicit_ladder = bool((getattr(settings, "agent_escalation_ladder", "") or "").strip())
        if explicit_ladder or (escalation_model and escalation_model != base_model):
            # Drop base_model from the ladder so a mid-ladder base doesn't stop
            # the climb (rung==base would otherwise read as "no escalation").
            ladder = [r for r in _escalation_ladder(escalation_model) if r != base_model]
            if ladder:
                rung = min(_count_escalation_cycles(conversation_history), len(ladder) - 1)
                return ladder[rung], True
    return base_model, False


def resolve_model_alias(name: str) -> str:
    """Map a short tier word ("haiku"/"sonnet"/"opus") to its Anthropic model id;
    pass any other string through unchanged. Shared by user-directed escalation
    and the chat model picker so the alias table stays single-source."""
    return _MODEL_ALIASES.get((name or "").strip().lower(), name)


def _select_client(model: str = "", force_local: bool = False, force_remote: bool = False):
    """Pick the LLM client for a turn.

    - `force_remote` (#654): build a per-turn client for the configured paid
      OpenAI-compatible remote provider (e.g. Fireworks) — the chat model
      picker's explicit "Remote" option. Checked before `force_local` since
      both are per-turn backend switches and can't both be requested; callers
      (chat.py) only ever set one.
    - `force_local`: build a per-turn local (llama-server / Gemma) client even
      when the global backend is Anthropic — the chat model picker's "Gemma"
      option. Context is preserved the same way as any per-turn switch: the full
      conversation_history is replayed into the new client, and llm_client does
      the Anthropic↔OpenAI format translation.
    - a per-turn `model` on the Anthropic backend (escalation, #303) builds a
      dedicated client for that model; otherwise the shared singleton is used.
    """
    if force_remote:
        from api.services.llm_client import LocalLLMClient
        return LocalLLMClient(
            base_url=settings.remote_llm_base_url,
            model=settings.remote_llm_model,
            api_key=settings.remote_llm_api_key,
            timeout=settings.remote_llm_timeout,
        )
    if force_local:
        if getattr(settings, "llm_backend", "anthropic").lower() != "anthropic":
            return get_local_llm()  # already local — reuse the singleton
        from api.services.llm_client import LocalLLMClient
        return LocalLLMClient()
    if model and getattr(settings, "llm_backend", "anthropic").lower() == "anthropic":
        from api.services.llm_client import AnthropicLLMClient
        return AnthropicLLMClient(model=model)
    return get_local_llm()


@dataclass
class AgentResult:
    """Result of an agentic chat loop run."""
    full_text: str
    tool_calls_log: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    model: str = ""
    # True when this turn ran on a backend that reports real usage but has no
    # configured price for it (#654 — a remote provider picked without rates
    # set). Distinct from a genuinely free local turn: total_cost_usd is 0.0
    # in both cases, but this flag says the zero is "unknown", not "free", so
    # a caller can record it as unpriced (usage_store's #613 column) instead
    # of a confidently wrong free turn. False for every other path — no
    # existing caller needs to touch this.
    unpriced: bool = False
    # (#629) the current in-flight round's cumulative usage-so-far, from
    # AnthropicLLMClient.astream's "usage_update" event. Only that backend
    # emits it -- LocalLLMClient.astream never does (its protocol has no
    # mid-stream usage signal), so these stay 0 for a local-backed turn,
    # same as before this field existed. Folded into total_input_tokens /
    # total_output_tokens (and reset to 0) as soon as the round's "done"
    # event arrives -- see _track_usage -- so a caller reading "usage
    # accrued so far" must add these to the total_* fields rather than
    # read either in isolation, or it will double- or under-count.
    provisional_input_tokens: int = 0
    provisional_output_tokens: int = 0


async def run_agent_loop(
    question: str,
    conversation_history: list | None = None,
    attachments: list[dict] | None = None,
    model_tier: str = "sonnet",
    max_tool_rounds: int = 5,
    model: str = "",
    persona: str = "",
    voice_rules: tuple = (),
    personal_context: str = "",
    force_local: bool = False,
    force_remote: bool = False,
) -> AsyncGenerator[dict, None]:
    """
    Async generator that runs the agentic chat loop.

    Yields events as they happen so the caller can stream them.

    Args:
        question: The user's current question.
        conversation_history: Previous messages (list of Message objects with .role, .content).
        attachments: Optional file attachments (list of dicts with filename, media_type, data).
        model_tier: "haiku", "sonnet", or "opus" (ignored for local model, kept for API compat).
        max_tool_rounds: Max number of tool-use rounds before forcing a text response.
        model: Optional Anthropic model id override for this turn (escalation, #303).
            When set on the Anthropic backend, the turn runs on a dedicated
            AnthropicLLMClient with this model instead of the default singleton.
            Ignored on the local backend.
        persona: Optional per-bot system-prompt preamble (e.g. the fitness bot).
            Empty for the default chat surface.
        voice_rules: The selected persona's spoken-response rules, appended to the
            system prompt only on voice turns (empty for text turns).
        force_local: Run this turn on the local (llama-server / Gemma) backend
            even when the global backend is Anthropic — the chat model picker's
            "Gemma (local)" option. Builds a per-turn LocalLLMClient.
        force_remote: Run this turn on the configured paid OpenAI-compatible
            remote provider (#654) — the chat model picker's explicit "Remote"
            option. Builds a per-turn LocalLLMClient pointed at
            settings.remote_llm_*. Usage is priced from the configured rates
            (or marked unpriced if none are set) instead of the free-local
            assumption the rest of this module makes.

    Yields:
        Dicts with "type" key: "turn_state" (first event, #615 -- a live
        reference to the mutable AgentResult, for a caller that needs
        accrued usage before the loop reaches its terminal event; #629
        extends what "accrued so far" means -- see AgentResult's
        provisional_input_tokens/provisional_output_tokens), "text",
        "status", or "result".
    """
    client = _select_client(model, force_local=force_local, force_remote=force_remote)
    system_prompt = build_system_prompt(persona=persona, max_tool_rounds=max_tool_rounds,
                                        voice_rules=voice_rules, personal_context=personal_context)

    # Thinking control (#567): only LocalLLMClient.astream accepts
    # enable_thinking — AnthropicLLMClient.astream has no such kwarg, and
    # passing it unconditionally would break the (default) Anthropic backend.
    # isinstance is the simplest correct gate here: there are exactly two
    # concrete client classes, agent_loop already branches on which one it
    # has (_select_client), and a capability protocol would be overkill for
    # a single kwarg on a two-class module. settings.local_agent_enable_thinking
    # defaults True (current behaviour) -> mapped to None so the request body
    # stays byte-identical until an operator opts out.
    # `not force_remote` (#654): the remote provider is also a LocalLLMClient
    # instance (same OpenAI-compatible plumbing) but isn't llama-server —
    # it doesn't understand llama-server's chat_template_kwargs switch, so
    # this local-only knob must never reach it regardless of the setting.
    astream_kwargs: dict = {}
    if isinstance(client, LocalLLMClient) and not force_remote:
        astream_kwargs["enable_thinking"] = None if settings.local_agent_enable_thinking else False

    # Bind a fresh per-turn email-draft set. The send gate uses this to refuse
    # sending any draft created during this same turn — sends require the user
    # to confirm in a later turn (draft → confirm → send).
    begin_email_send_turn()

    # Inject relevant memories into system prompt (with token budget)
    with trace_span("memory_inject"):
        try:
            from api.services.memory_store import get_memory_store, format_memories_for_prompt
            memory_store = get_memory_store()
            relevant_memories = memory_store.get_relevant_memories(question, limit=5)
            if relevant_memories:
                # Apply token budget: ~400 words ≈ 500 tokens
                budgeted = []
                word_count = 0
                for m in relevant_memories:
                    words = len(m.content.split())
                    if word_count + words > 400:
                        break
                    budgeted.append(m)
                    word_count += words
                if budgeted:
                    memory_text = format_memories_for_prompt(budgeted)
                    system_prompt.append({"type": "text", "text": memory_text})
        except Exception as e:
            logger.warning(f"Failed to load memories: {e}")

    # Build messages array from conversation history
    messages = []
    if conversation_history:
        for msg in conversation_history[-10:]:
            if msg.role in ("user", "assistant") and msg.content:
                messages.append({"role": msg.role, "content": msg.content})

    # Add current user message (with attachments if any)
    user_content = build_message_content(question, attachments)
    messages.append({"role": "user", "content": user_content})

    # model="local" is the pre-#654 constant for every non-remote turn on
    # this native path (Anthropic included — see module notes; that gap is
    # pre-existing and untouched here). The remote provider (#654) is the one
    # case where the real model id is known up front, so it's set at
    # construction rather than left for _track_usage to patch in later —
    # a cancellation before any round completes still reports it correctly.
    result = AgentResult(full_text="", model=(settings.remote_llm_model if force_remote else "local"))
    # #615: hand the caller a live reference to `result` before the loop does
    # any work. `_track_usage` below mutates it in place every round, so a
    # caller that stashes this object can read accrued usage at any point --
    # in particular from a cancel/deadline handler that never reaches the
    # terminal `result` event because the loop was cancelled mid-round.
    yield {"type": "turn_state", "result": result}
    phantom_write_nudged = False  # phantom-write self-correction fires at most once per turn

    def _track_usage(usage: LLMUsage):
        result.total_input_tokens += usage.input_tokens
        result.total_output_tokens += usage.output_tokens
        result.total_cache_read_tokens += usage.cache_read_input_tokens
        result.total_cache_creation_tokens += usage.cache_creation_input_tokens
        if force_remote:
            # Paid remote provider (#654): unlike every other path here, this
            # one has a real, non-zero cost — accumulated across rounds from
            # the configured per-token rates. No configured rate means no
            # honest number to report: mark unpriced (usage_store's #613
            # column) rather than defaulting to a guess (the exact hazard
            # cost_tracker.calculate_cost's old Sonnet fall-through created).
            input_price = settings.remote_llm_input_price_per_mtok
            output_price = settings.remote_llm_output_price_per_mtok
            if input_price is None or output_price is None:
                result.unpriced = True
            else:
                result.total_cost_usd += (
                    (usage.input_tokens / 1_000_000) * input_price
                    + (usage.output_tokens / 1_000_000) * output_price
                )
        else:
            # Local model has no cost
            result.total_cost_usd = 0.0
        # (#629) this round's usage is now folded into the totals above --
        # clear the provisional (in-flight) figures so a caller that adds
        # provisional_* to total_* (chat.py's cancel handler) doesn't
        # double-count them once the round has actually closed out.
        result.provisional_input_tokens = 0
        result.provisional_output_tokens = 0

    # Pass tool definitions through with their cache_control marker intact so
    # Anthropic caches the large, stable tool schema across turns and rounds.
    # The local backend strips cache_control itself in _anthropic_tools_to_openai.
    tools = TOOL_DEFINITIONS

    for round_num in range(1, max_tool_rounds + 1):
        print(f"[agent] Round {round_num}/{max_tool_rounds} starting")

        text_this_round = ""
        tool_use_blocks = []
        usage_this_round = LLMUsage()
        finish_reason = ""

        api_error_fatal = False
        with trace_span(f"llm_api_round_{round_num}"):
            max_api_retries = 2
            for api_attempt in range(max_api_retries + 1):
                try:
                    async for event in client.astream(
                        messages,
                        system=system_prompt,
                        max_tokens=4096,
                        tools=tools,
                        **astream_kwargs,
                    ):
                        if event["type"] == "text":
                            text_this_round += event["content"]
                            yield {"type": "text", "content": event["content"]}
                        elif event["type"] == "tool_calls":
                            tool_use_blocks = openai_tool_calls_to_anthropic(event["calls"])
                        elif event["type"] == "usage_update":
                            # (#629) Anthropic-only -- see AgentResult's
                            # provisional_* fields. Folded into total_* (and
                            # cleared) by _track_usage once "done" arrives
                            # below, so this is never added twice.
                            result.provisional_input_tokens = event["usage"].input_tokens
                            result.provisional_output_tokens = event["usage"].output_tokens
                        elif event["type"] == "done":
                            usage_this_round = event["usage"]
                            finish_reason = event.get("finish_reason", "")
                    break  # success
                except Exception as e:
                    if api_attempt < max_api_retries and is_retryable_api_error(e):
                        delay = 2 * (2 ** api_attempt)  # 2s, 4s
                        logger.warning(f"Round {round_num} transient error ({e}), retry {api_attempt + 1}/{max_api_retries} in {delay}s")
                        if text_this_round:
                            yield {"type": "self_correction"}
                            text_this_round = ""
                        yield {"type": "status", "message": f"LLM temporarily unavailable, retrying in {delay}s..."}
                        await asyncio.sleep(delay)
                        continue
                    print(f"[agent] Round {round_num} API error: {e}")
                    if result.full_text:
                        yield {"type": "text", "content": f"\n\n(Search interrupted: {e})"}
                    else:
                        yield {"type": "text", "content": f"Sorry, I encountered an error: {e}"}
                    api_error_fatal = True
                    break
        if api_error_fatal:
            break

        _track_usage(usage_this_round)

        # Build assistant content for message history (keep narration text
        # for the LLM context even if we strip it from the user-facing response)
        assistant_content = []
        if text_this_round:
            assistant_content.append({"type": "text", "text": text_this_round})
        for block in tool_use_blocks:
            assistant_content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })

        tool_names = [b.name for b in tool_use_blocks]
        print(f"[agent] Round {round_num} done: stop={finish_reason}, tools={tool_names}, text={len(text_this_round)}ch")

        # If model produced text AND tool calls, the text is narration ("I need
        # to look up...") — clear it from the user-facing response. The LLM
        # context (assistant_content above) keeps it for continuity.
        if tool_use_blocks and text_this_round.strip():
            yield {"type": "self_correction"}
        else:
            result.full_text += text_this_round

        # If no tool calls, we're done — unless the model is giving up without trying
        # finish_reason is "tool_calls" (OpenAI) or "tool_use" (Anthropic)
        if finish_reason not in ("tool_calls", "tool_use") or not tool_use_blocks:
            if (
                round_num == 1
                and not result.tool_calls_log
                and text_this_round.strip()
                and _looks_like_giving_up(text_this_round)
            ):
                print("[agent] Self-correction triggered: model gave up without using tools")
                yield {"type": "self_correction"}
                result.full_text = ""
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": SELF_CORRECTION_NUDGE})
                continue
            if (
                not phantom_write_nudged
                and not result.tool_calls_log
                and text_this_round.strip()
                and _claims_write_without_tools(text_this_round)
            ):
                phantom_write_nudged = True
                print("[agent] Self-correction triggered: reply claims a write but no tool was called")
                yield {"type": "self_correction"}
                result.full_text = ""
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": PHANTOM_WRITE_NUDGE})
                continue
            break

        # Append the assistant message with tool use blocks
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools in parallel
        async def _exec_one(block):
            name = block.name
            logger.info(f"Executing tool: {name} with input: {block.input}")
            with trace_span(f"tool_{name}"):
                tool_result_str = await execute_tool_parallel(name, block.input)
            is_error = tool_result_str.startswith("Error:")
            result.tool_calls_log.append({
                "tool": name,
                "input": block.input,
                "result_preview": tool_result_str[:200],
                "is_error": is_error,
            })
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": tool_result_str,
                "is_error": is_error,
            }

        # Emit status for each tool (with sub-action lookup for consolidated tools)
        for block in tool_use_blocks:
            status_msg = TOOL_STATUS_MESSAGES.get(block.name, f"Running {block.name}...")
            if block.name in _CONSOLIDATED_TOOLS:
                action = block.input.get("action", "")
                sub_key = f"{block.name}.{action}"
                status_msg = TOOL_STATUS_MESSAGES.get(sub_key, status_msg)
            yield {"type": "status", "message": status_msg}

        tool_results = await asyncio.gather(*[_exec_one(b) for b in tool_use_blocks])
        print(f"[agent] Round {round_num} tools executed: {[b.name for b in tool_use_blocks]}")

        # Append tool results as a user message
        messages.append({"role": "user", "content": list(tool_results)})

    else:
        # Exhausted all tool rounds — force a final synthesis round without tools.
        # Add an explicit instruction so the LLM knows to produce a text answer
        # instead of trying to call more tools.
        print("[agent] Exhausted tool rounds, running synthesis round")
        messages.append({
            "role": "user",
            "content": (
                "You have finished gathering information. Now answer the original "
                "question based on everything you found above. Do not call any more "
                "tools — just provide your answer in plain text."
            ),
        })
        try:
            synthesis_events = 0
            async for event in client.astream(
                messages,
                system=system_prompt,
                max_tokens=4096,
                timeout=180,
                **astream_kwargs,
            ):
                synthesis_events += 1
                if event["type"] == "text":
                    result.full_text += event["content"]
                    yield {"type": "text", "content": event["content"]}
                elif event["type"] == "tool_calls":
                    # LLM tried to call tools despite no tools in request —
                    # log and ignore (the text, if any, was already captured)
                    print(f"[agent] Synthesis round produced tool_calls (ignored): {[c.get('function', {}).get('name', '?') for c in event.get('calls', [])]}")
                elif event["type"] == "usage_update":
                    # (#629) same provisional tracking as the tool-round loop above.
                    result.provisional_input_tokens = event["usage"].input_tokens
                    result.provisional_output_tokens = event["usage"].output_tokens
                elif event["type"] == "done":
                    _track_usage(event["usage"])
                    print(f"[agent] Synthesis round done: finish_reason={event.get('finish_reason', '?')}, events={synthesis_events}")
        except Exception as e:
            error_msg = str(e) or f"{type(e).__name__} (no message)"
            print(f"[agent] Synthesis round error: {error_msg}")
            yield {"type": "text", "content": f"\n\n(Error during synthesis: {error_msg})"}

    # If we ran tools but still ended up with no text, construct a fallback
    # from tool results so the user gets something useful.
    if not result.full_text.strip() and result.tool_calls_log:
        # Clear any whitespace-only content that was already streamed
        if result.full_text:
            yield {"type": "self_correction"}
            result.full_text = ""
        # Exclude sensitive tools from raw fallback output
        _SENSITIVE_TOOLS = {"get_message_history", "search_email"}
        non_error_results = [
            tc["result_preview"]
            for tc in result.tool_calls_log
            if not tc.get("is_error")
            and tc["tool"] not in _SENSITIVE_TOOLS
            and tc.get("result_preview", "").strip()
        ]
        if non_error_results:
            fallback = "Here's what I found:\n\n" + "\n\n".join(non_error_results)
        else:
            fallback = "I searched but couldn't find relevant information to answer your question."
        result.full_text = fallback
        yield {"type": "text", "content": fallback}
        print(f"[agent] Used fallback response ({len(fallback)}ch)")

    print(f"[agent] Loop complete: {len(result.tool_calls_log)} tool calls, {len(result.full_text)}ch text")
    # Yield the final result
    yield {"type": "result", "result": result}
