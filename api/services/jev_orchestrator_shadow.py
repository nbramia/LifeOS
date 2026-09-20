"""Production SHADOW instrumentation for the chat orchestrator's Jev
typed-judgment questions.

Gated by `LIFEOS_JEV_ORCHESTRATOR` (`off` default / `shadow`, and
effectively `off` without a TypeSafe key — see `shadow_enabled()`).
`shadow` mode records two perf-trace spans per turn and changes nothing
else: `run_agent_loop` always receives the same arguments, tool catalog,
and round cap it would in `off` mode.

  - `jev_preturn` — one span per turn, from `api/routes/chat.py`. The
    Jev call is started as a background task right before
    `run_agent_loop` and never awaited ahead of it, so it always runs
    concurrent with the first round. Questions: `needs_tools`, the nine
    tool-family nouls, `difficulty`, `is_followup` — identical wording
    to `scripts/jev_eval/e1_preturn.py`'s `QUESTIONS`, so live shadow
    answers are comparable to that offline run. The span also records
    which of E3's k=5 bundles (`data/jev_eval/e3_results.json`,
    90.18% coverage, SHIP verdict) the family answers would have
    selected, and whether that differs from the previous turn's choice
    in the same conversation.
  - `jev_inloop` — one span per round from the second on, from
    `api/services/agent_loop.py`'s round loop, fired right after that
    round's tool results are gathered. Questions: `is_repeating`,
    `answered` — identical wording to `scripts/jev_eval/e2_inloop.py`.

Both calls fail open: on any exception or a 500ms-from-start timeout,
the span records only `{"error": "<ExceptionClassName>"}` and the turn
is otherwise unaffected. Neither call, nor any span it writes, carries
message text, tool arguments, or tool results — only calibrated
probabilities/scores, tool names, bundle ids, and (for the tool span
`execute_tool_parallel` already writes to) a 300-char result preview.
Perf traces are local SQLite and never leave the machine — see
`docs/specs/technical/security-privacy.md`'s "What Stays Local" table.
"""
import asyncio
import json
import logging
import time

from api.services.jev_client import JevClient, jev_configured
from api.services.perf_trace import _current_trace, trace_span
from config.settings import settings

logger = logging.getLogger(__name__)

# Hard stop on how long a turn will wait for either shadow call, measured
# from the task's own start (not from when the caller begins awaiting it).
_TIMEOUT_S = 0.5

# ---------------------------------------------------------------------------
# Pre-turn questions — verbatim from scripts/jev_eval/e1_preturn.py so live
# shadow answers are comparable to that offline run.
# ---------------------------------------------------------------------------
FAMILY_TO_TOOLS = {
    "comm": {"search_email", "search_slack", "get_message_history", "create_email_draft", "send_email_draft"},
    "calendar": {"search_calendar", "create_calendar_event", "update_calendar_event", "delete_calendar_event"},
    "people_crm": {"person_info"},
    "vault": {"search_vault", "read_vault_file", "save_memory", "search_memories", "search_drive"},
    "finance": {"search_finances"},
    "tasks": {"manage_tasks", "manage_reminders", "manage_schedules", "manage_human_queue"},
    "fitness": {"manage_workouts"},
    "web": {"search_web"},
    "home": {"pause_internet", "resume_internet", "internet_status"},
}

_FAMILY_INSTRUCTIONS = {
    "comm": "Would answering this message require searching or sending email, Slack messages, or iMessage/text messages?",
    "calendar": "Would answering this message require checking, creating, or modifying a calendar event?",
    "people_crm": "Would answering this message require looking up a specific person's profile, facts, or relationship/contact history?",
    "vault": "Would answering this message require searching notes, journal entries, saved memories, or files (vault or Drive)?",
    "finance": "Would answering this message require looking up financial data (transactions, budgets, accounts, or investments)?",
    "tasks": "Would answering this message require creating, listing, or updating a task, reminder, or scheduled automation?",
    "fitness": "Would answering this message require looking up or logging a workout, fitness metric, or exercise history?",
    "web": "Would answering this message require searching the public web for current information not in personal data?",
    "home": "Would answering this message require checking or changing home network/internet access (e.g. pausing or resuming a device)?",
}

PRETURN_QUESTIONS = {
    "needs_tools": {
        "type": "noul",
        "instructions": (
            "This is one turn of a chat with a personal-assistant agent that has tools for "
            "search, calendar, email, tasks, etc. Does answering THIS message require calling "
            "at least one tool, rather than being answerable from general knowledge or the "
            "conversation history alone?"
        ),
    },
    **{
        f"family_{fam}": {"type": "noul", "instructions": instr}
        for fam, instr in _FAMILY_INSTRUCTIONS.items()
    },
    "difficulty": {
        "type": "score",
        "instructions": "How many distinct personal-data sources would a careful assistant need to consult to fully answer this message?",
        "criteria": ["0 sources", "1 source", "2 sources", "3 sources", "4 or more sources"],
    },
    "is_followup": {
        "type": "noul",
        "instructions": (
            "Is this message a follow-up that can only be understood using the prior "
            "conversation turns (e.g. a pronoun or implicit reference to something just "
            "discussed), rather than a self-contained question?"
        ),
    },
}

# ---------------------------------------------------------------------------
# In-loop questions — verbatim from scripts/jev_eval/e2_inloop.py.
# ---------------------------------------------------------------------------
INLOOP_QUESTIONS = {
    "is_repeating": {
        "type": "noul",
        "instructions": (
            "This is an in-progress agent turn: the user's message, then the tools called so far "
            "(name and arguments), grouped by round. If the agent calls another tool right now, "
            "would that next call be repeating a search it has already made in an earlier round "
            "with no new information to justify repeating it?"
        ),
    },
    "answered": {
        "type": "noul",
        "instructions": (
            "This is an in-progress agent turn: the user's message, then the tools called so far "
            "(name and arguments), grouped by round. Based only on which tools have been called "
            "(not their results, which aren't shown here), does it look like the agent has now "
            "gathered everything a careful assistant would need to answer the user's message, "
            "such that stopping and synthesizing an answer now would be reasonable?"
        ),
    },
}

# ---------------------------------------------------------------------------
# E3's k=5 greedy-set-cover bundles (data/jev_eval/e3_results.json,
# 90.18% coverage of tool-using turns, SHIP verdict). Shadow-recorded only —
# not applied to filter what run_agent_loop advertises.
# ---------------------------------------------------------------------------
BUNDLES: tuple[dict, ...] = (
    {"id": 0, "tools": ("manage_tasks", "person_info", "read_vault_file", "search_calendar", "search_email", "search_vault", "search_web")},
    {"id": 1, "tools": ("get_message_history", "person_info", "read_vault_file", "search_calendar", "search_email", "search_vault", "search_web")},
    {"id": 2, "tools": ("person_info", "search_email", "search_finances", "search_vault", "search_web")},
    {"id": 3, "tools": ("manage_human_queue", "manage_tasks", "person_info", "search_calendar", "search_email", "search_slack", "search_vault", "search_web")},
    {"id": 4, "tools": ("manage_workouts", "person_info", "search_vault", "search_web")},
)

# Last chosen bundle per conversation, for the cache-thrash signal
# (`bundle_changed`). Unbounded for the life of the process — a shadow-only
# observation over a small number of concurrently active conversations, not
# worth an eviction policy.
_LAST_BUNDLE_BY_CONVERSATION: dict[str, int | None] = {}
# Distinguishes "no turn recorded yet for this conversation" from "the
# recorded turn's bundle_id was itself None" — both would otherwise read as
# a missing dict entry.
_NO_PRIOR_TURN = object()


def jev_orchestrator_mode() -> str:
    """`settings.jev_orchestrator`, validated. An unrecognized value logs a
    warning and is treated as `off`."""
    mode = (settings.jev_orchestrator or "off").strip().lower()
    if mode not in ("off", "shadow"):
        logger.warning(
            "invalid LIFEOS_JEV_ORCHESTRATOR=%r — must be one of off/shadow; "
            "falling back to off", mode,
        )
        return "off"
    return mode


def shadow_enabled() -> bool:
    """True only when the setting is `shadow` AND a TypeSafe key is
    configured — the single gate every call site in this module checks
    before constructing a `JevClient` or starting a task."""
    return jev_orchestrator_mode() == "shadow" and jev_configured()


def choose_bundle(family_answers: dict[str, float], threshold: float = 0.5) -> int | None:
    """The E3 bundle id whose tools cover the most families the pre-turn
    judgment flagged as needed (`family_answers[fam] >= threshold`), or
    None if no family was flagged. Ties go to the lowest bundle id
    (`BUNDLES`' own order)."""
    needed = {fam for fam, val in family_answers.items() if val >= threshold}
    if not needed:
        return None
    best_id, best_score = None, 0
    for bundle in BUNDLES:
        covered = {fam for fam, tools in FAMILY_TO_TOOLS.items() if tools & set(bundle["tools"])}
        score = len(needed & covered)
        if score > best_score:
            best_id, best_score = bundle["id"], score
    return best_id


def bundle_changed(conversation_id: str, bundle_id: int | None) -> bool:
    """True iff `bundle_id` differs from the last turn's choice recorded
    for this conversation (the cache-thrash signal E3's report flagged as
    unmeasured). False on a conversation's first recorded turn — there is
    nothing yet to have changed from, even when that turn's own bundle_id
    is None (no bundle needed)."""
    prev = _LAST_BUNDLE_BY_CONVERSATION.get(conversation_id, _NO_PRIOR_TURN)
    _LAST_BUNDLE_BY_CONVERSATION[conversation_id] = bundle_id
    if prev is _NO_PRIOR_TURN:
        return False
    return prev != bundle_id


def build_preturn_state(persona: str, conversation_history: list | None, message: str) -> dict:
    """Same shape as scripts/jev_eval/e1_preturn.py's `build_state`: persona
    id, the last two conversation-history messages (600 chars each), and
    the current message (1200 chars)."""
    prev = list(conversation_history or [])[-2:]
    return {
        "persona": persona,
        "prev_turns": [
            {"role": getattr(m, "role", ""), "content": (getattr(m, "content", "") or "")[:600]}
            for m in prev
        ],
        "message": (message or "")[:1200],
    }


def start_preturn_task(
    persona: str, conversation_history: list | None, message: str,
) -> tuple[asyncio.Task, float] | tuple[None, None]:
    """Start the pre-turn Jev call as a background task, if shadow mode is
    on. Returns `(task, start_monotonic)`, or `(None, None)` when disabled
    (no `JevClient` is constructed in that case). Never awaited here — the
    caller must let `run_agent_loop` start immediately once this returns,
    then await the task later (see `finish_preturn_span`)."""
    if not shadow_enabled():
        return None, None
    client = JevClient()
    state = build_preturn_state(persona, conversation_history, message)
    start = time.monotonic()
    task = asyncio.create_task(client.aask(state, PRETURN_QUESTIONS))
    return task, start


def _round_count(trace) -> int:
    if trace is None:
        return 0
    return sum(1 for s in trace.spans if s.name.startswith("llm_api_round_"))


def _round1_elapsed_ms(trace) -> float | None:
    """Wall time from `run_agent_loop`'s start to the end of the first
    round's LLM call (the memory-injection span plus every span up to and
    including `llm_api_round_1`), or None if the first round never
    completed (e.g. the loop errored before any round)."""
    if trace is None:
        return None
    total = 0.0
    for s in trace.spans:
        total += s.duration_ms
        if s.name == "llm_api_round_1":
            return total
    return None


async def finish_preturn_span(
    task: asyncio.Task | None, start: float | None, *, conversation_id: str, agent_result,
) -> None:
    """Await the pre-turn task (500ms cap from `start`) and record ONE
    `jev_preturn` span. No-op if `task` is None (shadow mode was off, or
    Jev wasn't configured, for this turn). On any exception or timeout the
    span records only `{"error": "<ExceptionClassName>"}`."""
    if task is None:
        return
    remaining = max(0.0, _TIMEOUT_S - (time.monotonic() - start))
    with trace_span("jev_preturn") as meta:
        try:
            answers = await asyncio.wait_for(task, timeout=remaining)
        except Exception as exc:
            meta["error"] = type(exc).__name__
            return
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        family_answers = {fam: answers[f"family_{fam}"]["noul"] for fam in FAMILY_TO_TOOLS}
        bundle_id = choose_bundle(family_answers)
        trace = _current_trace.get()
        round1_ms = _round1_elapsed_ms(trace)
        meta.update({
            "needs_tools": round(answers["needs_tools"]["noul"], 3),
            **{f"family_{fam}": round(v, 3) for fam, v in family_answers.items()},
            "difficulty": round(answers["difficulty"]["score"], 3),
            "is_followup": round(answers["is_followup"]["noul"], 3),
            "latency_ms": latency_ms,
            "before_round1": round1_ms is not None and latency_ms <= round1_ms,
            "bundle_id": bundle_id,
            "bundle_changed": bundle_changed(conversation_id, bundle_id),
            "tool_count": len(getattr(agent_result, "tool_calls_log", None) or []),
            "round_count": _round_count(trace),
        })


def _summarize_call(call: dict) -> dict:
    return {
        "tool": call["tool"],
        "args": json.dumps(call["input"], default=str)[:100],
        "result_preview": call["result_preview"],
    }


def start_inloop_task(
    message: str, calls_by_round: dict[int, list[dict]], upto_round: int,
) -> tuple[asyncio.Task, float] | tuple[None, None]:
    """Start an in-loop Jev call covering every round's calls through
    `upto_round`, if shadow mode is on. `calls_by_round[r]` entries are
    `{"tool", "input", "result_preview"}` dicts (see agent_loop.py's
    `_exec_one`). Returns `(task, start_monotonic)` or `(None, None)`."""
    if not shadow_enabled():
        return None, None
    client = JevClient()
    state = {
        "message": (message or "")[:1200],
        "calls_by_round": {
            str(r): [_summarize_call(c) for c in calls_by_round.get(r, [])]
            for r in range(1, upto_round + 1)
        },
    }
    start = time.monotonic()
    task = asyncio.create_task(client.aask(state, INLOOP_QUESTIONS))
    return task, start


async def finish_inloop_span(task: asyncio.Task | None, start: float | None, *, round_index: int) -> None:
    """Await one in-loop task (500ms cap from `start`) and record ONE
    `jev_inloop` span for that round. No-op if `task` is None. On any
    exception or timeout the span records only `{"error": "<ExceptionClassName>"}`."""
    if task is None:
        return
    remaining = max(0.0, _TIMEOUT_S - (time.monotonic() - start))
    with trace_span("jev_inloop") as meta:
        try:
            answers = await asyncio.wait_for(task, timeout=remaining)
        except Exception as exc:
            meta["error"] = type(exc).__name__
            return
        meta.update({
            "is_repeating": round(answers["is_repeating"]["noul"], 3),
            "answered": round(answers["answered"]["noul"], 3),
            "round_index": round_index,
        })
