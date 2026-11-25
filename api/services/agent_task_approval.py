"""Operator approval for agent work that the chat orchestrator creates.

A chat turn (web chat, the Telegram bots, voice, a `prompt` schedule) can
create or change a task so that the agent worker would pick it up: an
engine or `#agent` tag in the `tags` list or anywhere in the text the task
store parses, an update that assigns an engine or reopens such a task, a
sub-task, `resume_execution` on an engine-assigned task, or `plan_project`.
That work goes ahead only when the operator's own message for the turn
asked for agent work. Otherwise it is saved parked and the operator is
asked:

- **Parked task.** The task is written with status `blocked`, which the
  worker never claims, and the fields `agent_approval:: pending` and
  `agent_approval_id:: <token>`. A `plan_project` or `resume_execution`
  call is not run; the target task gets the same fields plus
  `agent_approval_action`.
- **In the conversation.** The tool result tells the model to ask. When the
  operator's next message in the same conversation is a go-ahead, the model
  repeats the release (`update` with `status: todo`, or the same project
  action) and it goes through.
- **On Telegram.** The primary bot sends the exact task with Approve and
  Deny buttons. The buttons carry the task id and the request token; a
  decision is spent the first time it is used, and any change to a parked
  task's content issues a new token, so an approval only ever releases the
  text the operator saw.

"The operator asked for agent work" is judged only from the operator's own
message text for the turn, never from tool results, memories or earlier
assistant text. Literal anchors decide the plain cases (an engine hashtag
the operator typed, "use codex to...", "have Claude fix...", "start an
agent...", "delegate this to codex"); Jev judges the rest when configured.
If neither says yes, including when Jev is unavailable, the request is not
explicit and the work is parked.
"""
from __future__ import annotations

import copy
import logging
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Optional

from api.services.agent_board import AGENT_PICKUP_TAGS

logger = logging.getLogger(__name__)

APPROVAL_FIELD = "agent_approval"
APPROVAL_ID_FIELD = "agent_approval_id"
APPROVAL_ACTION_FIELD = "agent_approval_action"
APPROVAL_CONVERSATION_FIELD = "agent_approval_conversation"
APPROVAL_STATUS_FIELD = "agent_approval_status"
APPROVAL_FIELDS = (
    APPROVAL_FIELD, APPROVAL_ID_FIELD, APPROVAL_ACTION_FIELD,
    APPROVAL_CONVERSATION_FIELD, APPROVAL_STATUS_FIELD,
)
PENDING = "pending"
DENIED = "denied"
# The worker claims only `todo`/`urgent` tasks (`worker.AGENT_PICKUP_STATUSES`),
# so a parked task carries a status it never claims.
PARKED_STATUS = "blocked"
PICKUP_STATUSES = ("todo", "urgent")

ACTION_RELEASE = "release"
ACTION_PLAN_PROJECT = "plan_project"
ACTION_RESUME_EXECUTION = "resume_execution"
_PROJECT_ACTIONS = (ACTION_PLAN_PROJECT, ACTION_RESUME_EXECUTION)
_CONTENT_KEYS = ("description", "notes", "tags", "fields")

CALLBACK_PREFIX = "ata"
_CALLBACK_RE = re.compile(r"^ata:([yn]):([\w-]{1,32}):([0-9a-f]{16})$")
_CONVERSATION_ID_RE = re.compile(r"^[\w-]{1,80}$")
_TASK_OWN_ID_RE = re.compile(r"\[id:([\w-]+)\]\s*$")

# Jev's `noul` is a probability; a judgement counts only at or above this.
JEV_THRESHOLD = 0.8
_JEV_TIMEOUT_SECONDS = 10.0
_OPERATOR_MESSAGE_MAX_CHARS = 4000
_REQUEST_TEXT_MAX_CHARS = 3800

_PICKUP = frozenset(t.lower() for t in AGENT_PICKUP_TAGS)


class ApprovalNotPending(Exception):
    """The request a decision names was already decided or replaced."""


# ---------------------------------------------------------------------------
# Would this task start an agent?
# ---------------------------------------------------------------------------

def agent_tags(task) -> list[str]:
    """The pickup tags (`#agent` and every engine) the store would read back
    from `task`, from its tag list and from any text it parses."""
    from api.services.task_manager import rendered_tags

    return sorted({t.lower() for t in rendered_tags(task)} & _PICKUP)


def starts_agent(task) -> bool:
    """True when the worker would claim `task` as written: a pickup status,
    a pickup tag, and execution not paused. A future snooze still counts,
    since the task starts once it wakes."""
    from api.services.task_projects import EXECUTION_PAUSED_FIELD, field_truthy

    return (
        (task.status or "todo").lower() in PICKUP_STATUSES
        and bool(agent_tags(task))
        and not field_truthy((task.fields or {}).get(EXECUTION_PAUSED_FIELD))
    )


def is_pending(task) -> bool:
    return (task.fields or {}).get(APPROVAL_FIELD) == PENDING


# ---------------------------------------------------------------------------
# Was the operator explicit?
# ---------------------------------------------------------------------------

_ENGINE = (
    r"(?:claude(?:\s+code)?|codex|hermes|an?\s+(?:background\s+)?agent|the\s+agent"
    r"|an?\s+worker|the\s+worker)"
)
_CLAUSE_START = r"(?:^|[.!?;:\n]\s*)"
_POLITE = (
    r"(?:(?:please|ok(?:ay)?|hey|so|and|then|now|also)[,\s]+)*"
    r"(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
)
_DELEGATION_RE = re.compile(
    _CLAUSE_START + _POLITE + r"(?:"
    r"(?:have|get|let|ask|tell|put)\s+" + _ENGINE + r"\s+(?:to\s+|on\s+)?\w"
    r"|(?:start|spawn|launch|kick\s+off|fire\s+up|spin\s+up)\s+(?:up\s+)?(?:an?\s+|the\s+)?"
    r"(?:new\s+)?(?:background\s+)?(?:agent|worker|claude(?:\s+code)?|codex|hermes)\b"
    r"|(?:create|make|file|add|open)\s+(?:an?\s+|the\s+)?(?:new\s+)?#?(?:agent|claude|codex|hermes)"
    r"\s+(?:task|job|session|ticket)\b"
    r"|(?:delegate|hand|assign|give|send|pass)\s+"
    r"(?:(?:this|it|that|them)\s+|the\s+[\w-]+(?:\s+[\w-]+){0,3}\s+)?(?:off\s+|over\s+)?to\s+"
    + _ENGINE + r"\b"
    r")",
    re.IGNORECASE,
)
_PICKUP_HASHTAG_RE = re.compile(
    r"(?<![\w#])#(" + "|".join(sorted(map(re.escape, _PICKUP), key=len, reverse=True)) + r")(?![\w-])",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"(?i)\b(?:don'?t|do\s+not|didn'?t|did\s+not|never|no\s+need\s+to|without|not|no)\b"
)
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;\n]")
_AFFIRMATIVE = frozenset({
    "yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "approve", "approved",
    "go", "go ahead", "do it", "please do", "proceed", "confirm", "confirmed",
    "start it", "run it", "go for it", "sounds good", "ship it", "yes please",
    "yes do it", "yes go ahead", "yes start it", "yes run it", "yes approve",
    "ok do it", "ok go ahead", "sure go ahead", "approved go ahead",
})
_REFINEMENT_RE = re.compile(r"(?i)\b(?:but|don'?t|do\s+not|not|no|wait|instead|only|except|unless)\b|\?")


def _clause_before(message: str, index: int) -> str:
    """The text of the clause that ends at `index`."""
    head = message[:index]
    parts = _CLAUSE_SPLIT_RE.split(head)
    return parts[-1] if parts else head


def anchor_asks_for_agent(message: str) -> bool:
    """Literal operator wording that asks for agent work.

    An engine or `#agent` hashtag the operator typed (not negated in its
    clause), a CLI-engine handoff ("use codex to...", "...with claude
    code"), or an imperative delegation at the start of a clause ("have
    Claude fix...", "start an agent to...", "create a claude task to...",
    "delegate this to codex").
    """
    text = message or ""
    for match in _PICKUP_HASHTAG_RE.finditer(text):
        if not _NEGATION_RE.search(_clause_before(text, match.start())):
            return True
    from api.services.agent_loop import parse_engine_directive

    if parse_engine_directive(text)[0]:
        return True
    return bool(_DELEGATION_RE.search(text))


def anchor_approves(message: str) -> bool:
    """A short, unqualified go-ahead ("yes", "do it", "approved")."""
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    if not text or len(text) > 40 or _REFINEMENT_RE.search(text):
        return False
    text = re.sub(r"[,.!]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*(?:thanks|thank you|please)$", "", text).strip() or text
    return text in _AFFIRMATIVE


def _noul(raw: Any) -> Optional[float]:
    value = raw.get("noul") if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0.0 <= value <= 1.0 else None


def _jev_verdict(message: str) -> Optional[tuple[Optional[float], Optional[float]]]:
    """Jev's probabilities that the operator's message asks for agent work
    and that it is a go-ahead, or None when Jev is not configured or the
    call fails. The state is the operator's message alone. Never logs it."""
    from api.services.jev_client import JevClient, jev_configured

    if not jev_configured():
        return None
    try:
        answers = JevClient(timeout=_JEV_TIMEOUT_SECONDS).ask(
            {"operator_message": message[:_OPERATOR_MESSAGE_MAX_CHARS]},
            {
                "asks_for_agent": {
                    "type": "noul",
                    "instructions": (
                        "the operator's message explicitly asks for work to be handed to an "
                        "AI agent or coding engine (Claude, Claude Code, Codex, Hermes, a "
                        "background agent or worker) to carry out, for example 'have Claude "
                        "fix X', 'start an agent to...', 'delegate this to codex' or 'create "
                        "a claude task'. Asking to summarise, answer, search, remind, or add "
                        "a task for the operator themself is not, and neither is a question "
                        "about agents"
                    ),
                },
                "approves": {
                    "type": "noul",
                    "instructions": (
                        "the operator's message is a plain go-ahead or approval, such as "
                        "'yes', 'do it', 'go ahead' or 'approved', and not a refusal, a "
                        "question, a change of plan or a new request"
                    ),
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - an unavailable judgement means "not explicit"
        logger.warning("agent-approval judgement unavailable: %s", type(exc).__name__)
        return None
    return _noul(answers.get("asks_for_agent")), _noul(answers.get("approves"))


@dataclass(frozen=True)
class OperatorVerdict:
    asks_for_agent: bool
    approves: bool
    basis: str  # "anchor", "jev", "unavailable" or "empty"


def judge_operator_message(message: str) -> OperatorVerdict:
    """Judge the operator's own message for the turn.

    A literal anchor that asks for agent work decides on its own. Otherwise
    Jev answers both questions; a missing or failed judgement counts as no.
    """
    text = (message or "").strip()
    if not text:
        return OperatorVerdict(False, False, "empty")
    approves = anchor_approves(text)
    if anchor_asks_for_agent(text):
        return OperatorVerdict(True, approves, "anchor")
    verdict = _jev_verdict(text)
    if verdict is None:
        return OperatorVerdict(False, approves, "anchor" if approves else "unavailable")
    asks_p, approves_p = verdict
    return OperatorVerdict(
        asks_for_agent=asks_p is not None and asks_p >= JEV_THRESHOLD,
        approves=approves or (approves_p is not None and approves_p >= JEV_THRESHOLD),
        basis="jev",
    )


class ApprovalTurn:
    """Per-turn approval state: the operator's own message, the conversation,
    the ids this turn parked, and the verdict, judged at most once."""

    def __init__(self, user_message: str = "", *, conversation_id: str = "", persona_id: str = ""):
        self.user_message = user_message or ""
        conv = (conversation_id or "").strip()
        self.conversation_id = conv if _CONVERSATION_ID_RE.match(conv) else ""
        self.persona_id = persona_id or ""
        self.parked_ids: set[str] = set()
        self._verdict: Optional[OperatorVerdict] = None
        self._lock = threading.Lock()

    def verdict(self) -> OperatorVerdict:
        with self._lock:
            if self._verdict is None:
                self._verdict = judge_operator_message(self.user_message)
            return self._verdict

    def approved_pending(self, task, action: str) -> bool:
        """A go-ahead from the operator releases a request parked in an
        earlier turn of this same conversation, for that same action."""
        fields = task.fields or {}
        return (
            is_pending(task)
            and (fields.get(APPROVAL_ACTION_FIELD) or ACTION_RELEASE) == action
            and task.id not in self.parked_ids
            and bool(self.conversation_id)
            and fields.get(APPROVAL_CONVERSATION_FIELD) == self.conversation_id
            and self.verdict().approves
        )


# ---------------------------------------------------------------------------
# The manage_tasks gate
# ---------------------------------------------------------------------------

@dataclass
class TaskGate:
    """What `execute_tool_parallel` does with one `manage_tasks` call: run it
    with `tool_input`, or return `refusal` without running it. A set
    `park_token` means the call was rewritten to park its task, and
    `finish_park` completes the request once it has run."""
    tool_input: Optional[dict] = None
    refusal: Optional[str] = None
    park_token: Optional[str] = None
    park_task_id: Optional[str] = None


def _new_token() -> str:
    return secrets.token_hex(8)


def _pending_fields(token: str, action: str, turn: ApprovalTurn, restore_status: str = "todo") -> dict:
    fields = {APPROVAL_FIELD: PENDING, APPROVAL_ID_FIELD: token}
    if action != ACTION_RELEASE:
        fields[APPROVAL_ACTION_FIELD] = action
    if restore_status == "urgent":
        fields[APPROVAL_STATUS_FIELD] = "urgent"
    if turn.conversation_id:
        fields[APPROVAL_CONVERSATION_FIELD] = turn.conversation_id
    return fields


def _cleared_fields() -> dict:
    return {key: None for key in APPROVAL_FIELDS}


def _restore_status(task) -> str:
    status = (task.fields or {}).get(APPROVAL_STATUS_FIELD) or "todo"
    return status if status in PICKUP_STATUSES else "todo"


def _string_fields(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None}


def _task_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(t) for t in raw]


def _proposed_create(inp: dict):
    from api.services.task_manager import Task

    return Task(
        id="00000000",
        description=str(inp.get("description") or ""),
        status=str(inp.get("status") or "todo"),
        priority=str(inp.get("priority") or ""),
        due_date=inp.get("due_date"),
        tags=_task_list(inp.get("tags")),
        notes=inp.get("notes"),
        fields=_string_fields(inp.get("fields")),
    )


def _proposed_update(current, inp: dict):
    after = copy.copy(current)
    after.fields = dict(current.fields or {})
    after.tags = list(current.tags or [])
    for key in ("description", "status", "priority", "due_date", "notes"):
        if inp.get(key) is not None:
            setattr(after, key, inp[key])
    if inp.get("tags") is not None:
        after.tags = _task_list(inp["tags"])
    patch = inp.get("fields")
    if isinstance(patch, dict):
        for key, value in patch.items():
            if value is None:
                after.fields.pop(str(key), None)
            else:
                after.fields[str(key)] = str(value)
    return after


def _is_project(tm, task_id: str) -> bool:
    from api.services.task_projects import build_task_hierarchy

    return build_task_hierarchy(tm.list_tasks()).is_project(task_id)


def check_manage_tasks(tool_input: dict, turn: ApprovalTurn) -> TaskGate:
    """Decide how a chat `manage_tasks` call that would start agent work
    proceeds: unchanged, parked, or refused. Calls that start no agent pass
    through untouched. Any failure to decide refuses the call."""
    try:
        fields = tool_input.get("fields")
        if isinstance(fields, dict) and any(str(k) in APPROVAL_FIELDS for k in fields):
            return TaskGate(refusal=(
                "Error: the agent_approval fields are set only by the approval flow. "
                "To release a parked task once the operator approves, update its status to todo."
            ))
        action = tool_input.get("action")
        if action == "create":
            return _check_create(tool_input, turn)
        if action == "update":
            return _check_update(tool_input, turn)
        if action in _PROJECT_ACTIONS:
            return _check_project_action(tool_input, turn, action)
        return TaskGate(tool_input=tool_input)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("agent-approval gate failed: %s", type(exc).__name__)
        return TaskGate(refusal=f"Error: could not check this task call for agent work ({type(exc).__name__}).")


def _check_create(inp: dict, turn: ApprovalTurn) -> TaskGate:
    proposed = _proposed_create(inp)
    if not starts_agent(proposed) or turn.verdict().asks_for_agent:
        return TaskGate(tool_input=inp)
    token = _new_token()
    parked = {
        **inp,
        "status": PARKED_STATUS,
        "fields": {
            **(inp.get("fields") or {}),
            **_pending_fields(token, ACTION_RELEASE, turn, proposed.status),
        },
    }
    return TaskGate(tool_input=parked, park_token=token)


def _check_update(inp: dict, turn: ApprovalTurn) -> TaskGate:
    from api.services.task_manager import get_task_manager

    task_id = inp.get("task_id")
    tm = get_task_manager()
    current = tm.get(task_id) if task_id else None
    if current is None or _is_project(tm, current.id):
        # Unknown ids fail in the handler; projects are never claimed, and
        # their agent owner starts only through `plan_project`.
        return TaskGate(tool_input=inp)
    content_change = any(inp.get(key) is not None for key in _CONTENT_KEYS)
    pending = is_pending(current)
    status_given = inp.get("status") is not None
    if pending and not content_change and not status_given:
        return TaskGate(tool_input=inp)  # stays parked, request unchanged
    after = _proposed_update(current, inp)
    if pending and not status_given:
        judged = copy.copy(after)
        judged.status = _restore_status(current)
    else:
        judged = after
    if not starts_agent(judged):
        if pending:
            # No longer agent work: the request is void, and the task returns
            # to the status it was parked from.
            return TaskGate(tool_input={
                **inp,
                "status": inp.get("status") or _restore_status(current),
                "fields": {**(inp.get("fields") or {}), **_cleared_fields()},
            })
        return TaskGate(tool_input=inp)
    if starts_agent(current) and not content_change:
        return TaskGate(tool_input=inp)
    if turn.verdict().asks_for_agent or (not content_change and turn.approved_pending(current, ACTION_RELEASE)):
        if not pending:
            return TaskGate(tool_input=inp)
        released = {**inp, "fields": {**(inp.get("fields") or {}), **_cleared_fields()}}
        if not status_given:
            released["status"] = _restore_status(current)
        return TaskGate(tool_input=released)
    if pending and not content_change:
        return TaskGate(refusal=(
            f"Error: task {current.id} is waiting for the operator's approval before an agent "
            "starts on it. Ask the operator in your reply; they can also approve it in "
            "Telegram. If they say yes in their next message, set its status to todo then."
        ))
    token = _new_token()
    restore = judged.status if judged.status in PICKUP_STATUSES else "todo"
    parked = {
        **inp,
        "status": PARKED_STATUS,
        "fields": {
            **(inp.get("fields") or {}),
            **_cleared_fields(),
            **_pending_fields(token, ACTION_RELEASE, turn, restore),
        },
    }
    return TaskGate(tool_input=parked, park_token=token, park_task_id=current.id)


def _check_project_action(inp: dict, turn: ApprovalTurn, action: str) -> TaskGate:
    from api.services.task_manager import get_task_manager
    from api.services.task_projects import EXECUTION_PAUSED_FIELD

    task_id = inp.get("task_id")
    tm = get_task_manager()
    current = tm.get(task_id) if task_id else None
    if current is None:
        return TaskGate(tool_input=inp)
    if action == ACTION_RESUME_EXECUTION:
        resumed = copy.copy(current)
        resumed.fields = {k: v for k, v in (current.fields or {}).items() if k != EXECUTION_PAUSED_FIELD}
        if current.status == "in_progress":
            resumed.status = "todo"
        if not starts_agent(resumed):
            return TaskGate(tool_input=inp)
    if turn.verdict().asks_for_agent or turn.approved_pending(current, action):
        if is_pending(current):
            tm.update(current.id, fields=_cleared_fields())
        return TaskGate(tool_input=inp)
    if is_pending(current) and (current.fields or {}).get(APPROVAL_ACTION_FIELD) == action:
        return TaskGate(refusal=_project_notice(current, action, sent=None))
    token = _new_token()
    tm.update(current.id, fields={**_cleared_fields(), **_pending_fields(token, action, turn)})
    turn.parked_ids.add(current.id)
    task = tm.get(current.id) or current
    sent = send_approval_request(task, token, action, turn)
    return TaskGate(refusal=_project_notice(task, action, sent=sent))


def _created_task_id(result: str) -> Optional[str]:
    if not result.startswith("Task created:"):
        return None
    lines = result.splitlines()
    match = _TASK_OWN_ID_RE.search(lines[1]) if len(lines) > 1 else None
    return match.group(1) if match else None


def finish_park(gate: TaskGate, result: str, turn: ApprovalTurn) -> str:
    """After a parked create or update has run: send the Telegram request
    and tell the model to ask the operator."""
    from api.services.task_manager import get_task_manager

    task_id = gate.park_task_id if result.startswith("Task updated") else _created_task_id(result)
    if not task_id or not gate.park_token:
        return result
    task = get_task_manager().get(task_id)
    if task is None or (task.fields or {}).get(APPROVAL_ID_FIELD) != gate.park_token:
        return result
    turn.parked_ids.add(task_id)
    sent = send_approval_request(task, gate.park_token, ACTION_RELEASE, turn)
    engines = ", ".join(f"#{t}" for t in agent_tags(task)) or "an agent"
    telegram = (
        " An Approve/Deny request was also sent to the operator's Telegram."
        if sent else ""
    )
    return (
        f"{result}\n\n"
        f"AWAITING OPERATOR APPROVAL: this task would start an agent ({engines}), and the "
        "operator's own message this turn did not ask for agent work, so it was saved parked "
        f"(status blocked, agent_approval: pending) and will not run.{telegram} Tell the operator "
        "what the task is and ask whether to start it. Do not release it in this turn. If their "
        f"next message says yes, release it with manage_tasks update task_id={task_id} status=todo."
    )


def _project_notice(task, action: str, *, sent: Optional[bool]) -> str:
    verb = "plan and delegate the project" if action == ACTION_PLAN_PROJECT else "resume execution"
    telegram = (
        " An Approve/Deny request was sent to the operator's Telegram." if sent
        else " It is already waiting for the operator's approval." if sent is None
        else ""
    )
    return (
        f"NOT STARTED, AWAITING OPERATOR APPROVAL: {action} on task {task.id} would start an "
        f"agent, and the operator's own message this turn did not ask for agent work.{telegram} "
        f"Ask the operator whether to {verb} for \"{task.description}\". If their next message "
        f"says yes, call manage_tasks {action} with task_id={task.id} again."
    )


# ---------------------------------------------------------------------------
# Telegram request and decision
# ---------------------------------------------------------------------------

def request_text(task, action: str, persona_id: str = "") -> str:
    """The plain text of the Telegram approval request: the exact task the
    buttons decide. Notes are shortened only when the whole would not fit
    one message, and the text says so."""
    engines = ", ".join(f"#{t}" for t in agent_tags(task)) or "an agent"
    what = {
        ACTION_PLAN_PROJECT: "Plan and delegate this project (starts its agent owner)",
        ACTION_RESUME_EXECUTION: f"Resume agent execution ({engines})",
    }.get(action, f"Start an agent on this task ({engines})")
    surface = persona_id or "chat"
    head = (
        f"Approve agent work?\n\n{what}\n\nTask {task.id}: {task.description}"
    )
    tail = (
        f"\n\nAsked from: {surface}. It stays parked until you decide; "
        "Approve releases it once."
    )
    notes = (task.notes or "").strip()
    if notes:
        room = _REQUEST_TEXT_MAX_CHARS - len(head) - len(tail) - 10
        if len(notes) > room:
            notes = notes[: max(0, room - 60)].rstrip() + "\n[notes continue in the task]"
        head += f"\n\nNotes:\n{notes}"
    text = head + tail
    if len(text) > _REQUEST_TEXT_MAX_CHARS:
        text = text[: _REQUEST_TEXT_MAX_CHARS - 40].rstrip() + "\n[shortened; see the task]"
    return text


def callback_data(approve: bool, task_id: str, token: str) -> str:
    return f"{CALLBACK_PREFIX}:{'y' if approve else 'n'}:{task_id}:{token}"


def parse_callback_data(data: str) -> Optional[tuple[bool, str, str]]:
    match = _CALLBACK_RE.match(data or "")
    if not match:
        return None
    return match.group(1) == "y", match.group(2), match.group(3)


def _send_buttons(text: str, buttons: list[tuple[str, str]]) -> Optional[int]:
    from api.services.telegram import send_message_with_buttons

    return send_message_with_buttons(text, buttons)


def send_approval_request(task, token: str, action: str, turn: ApprovalTurn) -> bool:
    """Send the Approve/Deny request to the primary bot. Returns whether it
    was sent; an unsent request still leaves the task parked."""
    try:
        sent = _send_buttons(
            request_text(task, action, turn.persona_id),
            [("Approve", callback_data(True, task.id, token)),
             ("Deny", callback_data(False, task.id, token))],
        )
    except Exception as exc:  # noqa: BLE001 - the task stays parked either way
        logger.warning("agent-approval Telegram request failed: %s", type(exc).__name__)
        return False
    return sent is not None


def decide(task_id: str, token: str, *, approve: bool) -> str:
    """Record the operator's Approve or Deny for one request.

    Bound to the task and its current request token, and spent on first
    use: the write re-checks both under the task store's lock. Deny keeps
    the task parked. Approve releases a parked task to its engine, or runs
    the parked `plan_project`/`resume_execution`.
    """
    from api.services.task_manager import get_task_manager

    tm = get_task_manager()
    current = tm.get(task_id)
    if current is None:
        return "That task no longer exists; nothing was started."

    def still_pending(task) -> None:
        fields = task.fields or {}
        if fields.get(APPROVAL_FIELD) != PENDING or fields.get(APPROVAL_ID_FIELD) != token:
            raise ApprovalNotPending()

    try:
        still_pending(current)
        action = (current.fields or {}).get(APPROVAL_ACTION_FIELD) or ACTION_RELEASE
        if not approve:
            tm.update(
                task_id, fields={APPROVAL_FIELD: DENIED, APPROVAL_ID_FIELD: None},
                _precondition=still_pending,
            )
            return "Denied. The task stays parked and no agent will start."
        if action == ACTION_RELEASE:
            kwargs: dict[str, Any] = {"fields": _cleared_fields()}
            if current.status == PARKED_STATUS:
                kwargs["status"] = _restore_status(current)
            task = tm.update(task_id, _precondition=still_pending, **kwargs)
            engines = ", ".join(f"#{t}" for t in agent_tags(task)) if task else ""
            return f"Approved. Released to {engines or 'its agent'}."
        tm.update(task_id, fields=_cleared_fields(), _precondition=still_pending)
    except ApprovalNotPending:
        return "This request was already decided or replaced; nothing changed."

    from api.services import agent_tools

    try:
        if action == ACTION_PLAN_PROJECT:
            outcome = agent_tools._task_plan_project({"task_id": task_id, "operation_id": f"approval-{token}"})
        else:
            outcome = agent_tools._task_resume_execution({"task_id": task_id})
    except Exception as exc:  # noqa: BLE001 - report, never crash the listener
        return f"Approved, but it could not start: {exc}"
    return f"Approved. {outcome}"
