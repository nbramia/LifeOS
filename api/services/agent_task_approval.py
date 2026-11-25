"""Operator approval for agent work that the chat orchestrator creates.

A chat turn (web chat, the Telegram bots, voice, a `prompt` schedule) can
create or change a task so that the agent worker would pick it up: an
engine or `#agent` tag on the task line, an update that assigns an engine
or reopens such a task, a sub-task, `resume_execution` on an
engine-assigned task, or `plan_project`. That work goes ahead only when the
operator's own message for the turn asked for agent work in so many words.
Otherwise it is saved parked and the operator is asked:

- **Parked task.** The task is written with status `blocked`, which the
  worker never claims, and `agent_approval:: pending` plus a request token,
  the time of the request, and a hash of the task's content. A
  `plan_project` or `resume_execution` call is not run; the target task
  gets the same fields plus `agent_approval_action`.
- **In the conversation.** The tool result tells the model to ask. When the
  operator's next message in the same conversation is a plain go-ahead, the
  model repeats the release and it goes through, for the most recent request
  of that conversation only, and only while the content is unchanged.
- **On Telegram.** The primary bot sends the exact task with Approve and
  Deny buttons. A press counts only from the operator, on the message that
  carried that request's buttons, while the request token and the content
  hash still match; it is spent on first use.

Every check runs again against the task's current state inside the task
store's write, under its lock, so parallel tool calls cannot combine into
agent work that no single check saw.

"The operator asked for agent work" is judged only from the operator's own
message text for the turn, and only by literal wording: an engine hashtag
they typed, "use codex to ...", "have Claude fix ...", "start an agent
...", "delegate this to codex". Text the operator quotes or asks about
("what does this mean: ...", anything in quotes, after a colon, or on a
later line) never counts. Everything else is parked and asked.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from api.services.agent_board import AGENT_PICKUP_TAGS

logger = logging.getLogger(__name__)

APPROVAL_FIELD = "agent_approval"
APPROVAL_ID_FIELD = "agent_approval_id"
APPROVAL_ACTION_FIELD = "agent_approval_action"
APPROVAL_CONVERSATION_FIELD = "agent_approval_conversation"
APPROVAL_STATUS_FIELD = "agent_approval_status"
APPROVAL_HASH_FIELD = "agent_approval_hash"
APPROVAL_AT_FIELD = "agent_approval_at"
APPROVAL_MESSAGE_FIELD = "agent_approval_message"
APPROVAL_FIELDS = (
    APPROVAL_FIELD, APPROVAL_ID_FIELD, APPROVAL_ACTION_FIELD, APPROVAL_CONVERSATION_FIELD,
    APPROVAL_STATUS_FIELD, APPROVAL_HASH_FIELD, APPROVAL_AT_FIELD, APPROVAL_MESSAGE_FIELD,
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
_REQUEST_TEXT_MAX_CHARS = 3800
_WRITE_ATTEMPTS = 3

_PICKUP = frozenset(t.lower() for t in AGENT_PICKUP_TAGS)


class ApprovalNotPending(Exception):
    """The request a decision names was already decided, replaced or changed."""


class _Replan(Exception):
    """The task changed between the gate's decision and its write."""


# ---------------------------------------------------------------------------
# Would this task start an agent?
# ---------------------------------------------------------------------------

def agent_tags(task) -> list[str]:
    """The pickup tags (`#agent` and every engine) that route `task`: those
    on its task line, raw or NFKC-normalized, and any the store's own parser
    reads back from the written block. Prose in the notes does not count."""
    from api.services.task_manager import claimed_tags

    line_tags, block_tags = claimed_tags(task)
    return sorted({t.lower() for t in (line_tags | block_tags)} & _PICKUP)


def notes_routing_error(task) -> Optional[str]:
    """An error when the store would read a pickup tag from the notes body
    rather than the task line (notes that would add a routing tag)."""
    from api.services.task_manager import claimed_tags

    line_tags, block_tags = claimed_tags(task)
    extra = {t.lower() for t in block_tags} - {t.lower() for t in line_tags}
    if extra & _PICKUP:
        return "Error: the notes would add a routing tag to the task; put engine tags in tags."
    return None


def starts_agent(task) -> bool:
    """True when the worker would claim `task` as written: a pickup status,
    a pickup tag on the task line, and execution not paused. A future snooze
    still counts, since the task starts once it wakes."""
    from api.services.task_projects import EXECUTION_PAUSED_FIELD, field_truthy

    return (
        (task.status or "todo").lower() in PICKUP_STATUSES
        and bool(agent_tags(task))
        and not field_truthy((task.fields or {}).get(EXECUTION_PAUSED_FIELD))
    )


def is_pending(task) -> bool:
    return (task.fields or {}).get(APPROVAL_FIELD) == PENDING


def content_hash(task) -> str:
    """A hash of what an approval approves: the title and notes the store
    reads back, the routing tags, and every field except the approval's own.
    Computed from the store's reading of the rendered task, so it is the
    same however a tag was written."""
    from api.services import task_manager as tm

    lines = tm._format_task_block(task)
    block = tm._match_task_block(lines, 0, task.source_file or "Inbox.md")
    parsed = block[1] if block else task
    payload = {
        "description": parsed.description,
        "notes": parsed.notes or "",
        "tags": sorted(t.lstrip("#").lower() for t in parsed.tags),
        "fields": {
            k: v for k, v in sorted((parsed.fields or {}).items()) if k not in APPROVAL_FIELDS
        },
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Was the operator explicit?
# ---------------------------------------------------------------------------

_ENGINE = (
    r"(?:claude(?:\s+code)?|codex|hermes|an?\s+(?:background\s+)?agent|the\s+agent"
    r"|an?\s+worker|the\s+worker)"
)
_CLAUSE_START = r"(?:^|[.!?;\n]\s*)"
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
# Quoted spans: straight and curly double quotes, curly single quotes,
# backticks, and straight single quotes that are not apostrophes.
_QUOTED_RE = re.compile(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|`[^`\n]*`|(?<!\w)\'[^\'\n]*\'(?!\w)')
# A colon that is not part of a time ("5:30"): what follows is pasted or
# quoted text, not the operator's instruction.
_SEPARATOR_COLON_RE = re.compile(r"(?<!\d):|:(?!\d)")
# A message that asks about some text rather than giving an instruction.
_ABOUT_TEXT_LEAD_RE = re.compile(
    r"(?i)^\s*(?:(?:hey|so|ok(?:ay)?|hmm|um)[,\s]+)*(?:"
    r"what(?:'?s|\s+is|\s+does|\s+do|\s+are|\s+about)?\b|is\s+(?:this|it|that)\b"
    r"|are\s+(?:these|they|those)\b|does\s+(?:this|it|that)\b|explain\b|translate\b"
    r"|thoughts\s+on\b|any\s+idea\b|can\s+i\s+trust\b|should\s+i\b|do\s+you\s+think\b"
    r"|tell\s+me\s+(?:if|whether|what|about|why)\b|read\s+(?:this|the\s+following|me)\b"
    r"|check\s+(?:this|if|whether)\b|summari[sz]e\b|look\s+at\s+(?:this|the\s+following)\b"
    r"|why\b|how\b|who\b|when\b|where\b|fyi\b|context\b|someone\s+(?:sent|wrote)\b"
    r"|i\s+(?:got|received|was\s+sent)\b|this\s+(?:came|arrived|email|message)\b"
    r")"
)
_AFFIRMATIVE = frozenset({
    "yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "approve", "approved",
    "go", "go ahead", "do it", "please do", "proceed", "confirm", "confirmed",
    "start it", "run it", "go for it", "sounds good", "ship it", "yes please",
    "yes do it", "yes go ahead", "yes start it", "yes run it", "yes approve",
    "ok do it", "ok go ahead", "sure go ahead", "approved go ahead",
})
_REFINEMENT_RE = re.compile(r"(?i)\b(?:but|don'?t|do\s+not|not|no|wait|instead|only|except|unless)\b|\?")


def operator_instruction_text(message: str) -> str:
    """The part of the operator's message that can be an instruction to
    start agent work: the first line, up to the first separating colon,
    with quoted spans removed. Empty when the message asks about some text
    ("what does this mean ...", "is this legit ...") rather than instructing."""
    line = (message or "").strip().split("\n", 1)[0]
    if _ABOUT_TEXT_LEAD_RE.match(line):
        return ""
    colon = _SEPARATOR_COLON_RE.search(line)
    if colon:
        line = line[: colon.start()]
    return _QUOTED_RE.sub(" ", line).strip()


def _clause_before(message: str, index: int) -> str:
    parts = _CLAUSE_SPLIT_RE.split(message[:index])
    return parts[-1] if parts else message[:index]


def anchor_asks_for_agent(message: str) -> bool:
    """Literal operator wording that asks for agent work, judged on
    `operator_instruction_text`: an engine or `#agent` hashtag the operator
    typed (not negated in its clause), a CLI-engine handoff ("use codex
    to...", "...with claude code"), or an imperative delegation at the start
    of a clause ("have Claude fix...", "start an agent to...", "create a
    claude task to...", "delegate this to codex")."""
    text = operator_instruction_text(message)
    if not text:
        return False
    for match in _PICKUP_HASHTAG_RE.finditer(text):
        if not _NEGATION_RE.search(_clause_before(text, match.start())):
            return True
    from api.services.agent_loop import parse_engine_directive

    if parse_engine_directive(text)[0]:
        return True
    return bool(_DELEGATION_RE.search(text))


def anchor_approves(message: str) -> bool:
    """A short, single-line, unqualified go-ahead ("yes", "do it", "approved")."""
    raw = (message or "").strip()
    if "\n" in raw:
        return False
    text = re.sub(r"\s+", " ", raw.lower())
    if not text or len(text) > 40 or _REFINEMENT_RE.search(text):
        return False
    text = re.sub(r"[,.!]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*(?:thanks|thank you|please)$", "", text).strip() or text
    return text in _AFFIRMATIVE


@dataclass(frozen=True)
class OperatorVerdict:
    asks_for_agent: bool
    approves: bool


def judge_operator_message(message: str) -> OperatorVerdict:
    """Judge the operator's own message for the turn, by literal wording only."""
    return OperatorVerdict(anchor_asks_for_agent(message), anchor_approves(message))


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

    def approved_request(self, task, action: str, latest_id: Optional[str]) -> bool:
        """The operator's go-ahead releases one request: the most recent one
        parked in an earlier turn of this conversation, for this action, while
        its content is what was asked about."""
        fields = task.fields or {}
        return (
            is_pending(task)
            and task.id == latest_id
            and (fields.get(APPROVAL_ACTION_FIELD) or ACTION_RELEASE) == action
            and task.id not in self.parked_ids
            and bool(self.conversation_id)
            and fields.get(APPROVAL_CONVERSATION_FIELD) == self.conversation_id
            and fields.get(APPROVAL_HASH_FIELD) == content_hash(task)
            and self.verdict().approves
        )


def latest_request_id(tm, conversation_id: str) -> Optional[str]:
    """The task id of the most recent pending request in a conversation."""
    if not conversation_id:
        return None
    best: tuple[float, str] | None = None
    for task in tm.list_tasks():
        fields = task.fields or {}
        if not is_pending(task) or fields.get(APPROVAL_CONVERSATION_FIELD) != conversation_id:
            continue
        try:
            at = float(fields.get(APPROVAL_AT_FIELD) or 0)
        except ValueError:
            at = 0.0
        if best is None or at > best[0]:
            best = (at, task.id)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# The manage_tasks gate
# ---------------------------------------------------------------------------

def _new_token() -> str:
    return secrets.token_hex(8)


def _pending_fields(token: str, action: str, turn: ApprovalTurn, restore_status: str = "todo") -> dict:
    fields = {
        APPROVAL_FIELD: PENDING,
        APPROVAL_ID_FIELD: token,
        APPROVAL_AT_FIELD: f"{time.time():.6f}",
    }
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


def _tag_list(raw: Any) -> list[str]:
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
        tags=_tag_list(inp.get("tags")),
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
        after.tags = _tag_list(inp["tags"])
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


def handle_manage_tasks(tool_input: dict, turn: ApprovalTurn) -> Optional[str]:
    """Run a chat `manage_tasks` call that could start agent work through the
    gate and return its result, or None for an action that starts no agent
    (the caller runs it as usual). Any failure to decide refuses the call."""
    try:
        fields = tool_input.get("fields")
        if isinstance(fields, dict) and any(str(k) in APPROVAL_FIELDS for k in fields):
            return (
                "Error: the agent_approval fields are set only by the approval flow. "
                "To release a parked task once the operator approves, update its status to todo."
            )
        action = tool_input.get("action")
        if action == "create":
            return _run_create(tool_input, turn)
        if action == "update":
            return _run_update(tool_input, turn)
        if action in _PROJECT_ACTIONS:
            return _run_project_action(tool_input, turn, action)
        return None
    except Exception as exc:  # noqa: BLE001 - fail closed, with the reason
        logger.warning("agent-approval gate refused a call: %s", type(exc).__name__)
        return f"Error: {exc}" if str(exc) else f"Error: {type(exc).__name__}"


def _created_task_id(result: str) -> Optional[str]:
    if not result.startswith("Task created:"):
        return None
    lines = result.splitlines()
    match = _TASK_OWN_ID_RE.search(lines[1]) if len(lines) > 1 else None
    return match.group(1) if match else None


def _run_create(inp: dict, turn: ApprovalTurn) -> str:
    from api.services import agent_tools

    proposed = _proposed_create(inp)
    error = notes_routing_error(proposed)
    if error:
        return error
    if not starts_agent(proposed) or turn.verdict().asks_for_agent:
        return agent_tools._task_create(inp)
    token = _new_token()
    parked = {
        **inp,
        "status": PARKED_STATUS,
        "fields": {
            **(inp.get("fields") or {}),
            **_pending_fields(token, ACTION_RELEASE, turn, proposed.status),
        },
    }
    result = agent_tools._task_create(parked)
    task_id = _created_task_id(result)
    if not task_id:
        return result
    return result + "\n\n" + _open_request(task_id, token, ACTION_RELEASE, turn)


@dataclass
class _Plan:
    kind: str  # pass, park, release, void, refuse
    tool_input: Optional[dict] = None
    refusal: Optional[str] = None
    token: Optional[str] = None
    signature: tuple = field(default_factory=tuple)


def _plan_update(current, inp: dict, turn: ApprovalTurn, *, is_project: bool, latest_id) -> _Plan:
    """Decide one update against `current`. Pure: the same decision is made
    again inside the write, against the snapshot actually being written."""
    request = (current.fields or {}).get(APPROVAL_ID_FIELD)

    def plan(kind: str, tool_input: Optional[dict] = None, refusal: Optional[str] = None,
             token: Optional[str] = None) -> _Plan:
        return _Plan(kind, tool_input, refusal, token, (kind, request))

    content_change = any(inp.get(key) is not None for key in _CONTENT_KEYS)
    pending = is_pending(current)
    cleared = {**(inp.get("fields") or {}), **_cleared_fields()}
    if is_project:
        # Projects are never claimed; their owner starts only through
        # `plan_project`. Changing a project's content voids its request.
        if pending and content_change:
            return plan("void", {**inp, "fields": cleared})
        return plan("pass", inp)
    after = _proposed_update(current, inp)
    error = notes_routing_error(after)
    if error:
        return plan("refuse", refusal=error)
    status_given = inp.get("status") is not None
    if pending and not content_change and not status_given:
        return plan("pass", inp)  # stays parked, request unchanged
    judged = after
    if pending and not status_given:
        judged = copy.copy(after)
        judged.status = _restore_status(current)
    if not starts_agent(judged):
        if pending:
            # No longer agent work: the request is void and the task returns
            # to the status it was parked from.
            return plan("void", {**inp, "status": inp.get("status") or _restore_status(current),
                                 "fields": cleared})
        return plan("pass", inp)
    if starts_agent(current) and not content_change:
        return plan("pass", inp)
    if turn.verdict().asks_for_agent or (
        not content_change and turn.approved_request(current, ACTION_RELEASE, latest_id)
    ):
        if not pending:
            return plan("pass", inp)
        released = {**inp, "fields": cleared}
        if not status_given:
            released["status"] = _restore_status(current)
        return plan("release", released)
    if pending and not content_change:
        return plan("refuse", refusal=(
            f"Error: task {current.id} is waiting for the operator's approval before an agent "
            "starts on it. Ask the operator about this task in your reply; they can also approve "
            "it in Telegram. If their next message says yes, set its status to todo then."
        ))
    token = _new_token()
    restore = judged.status if judged.status in PICKUP_STATUSES else "todo"
    parked = {
        **inp,
        "status": PARKED_STATUS,
        "fields": {**cleared, **_pending_fields(token, ACTION_RELEASE, turn, restore)},
    }
    return plan("park", parked, token=token)


def _run_update(inp: dict, turn: ApprovalTurn) -> Optional[str]:
    from api.services import agent_tools
    from api.services.task_manager import get_task_manager

    tm = get_task_manager()
    task_id = inp.get("task_id")
    for _attempt in range(_WRITE_ATTEMPTS):
        current = tm.get(task_id) if task_id else None
        if current is None:
            return None  # the handler reports the missing id
        is_project = _is_project(tm, current.id)
        latest_id = latest_request_id(tm, turn.conversation_id)
        planned = _plan_update(current, inp, turn, is_project=is_project, latest_id=latest_id)
        if planned.kind == "refuse":
            return planned.refusal

        def recheck(snapshot, planned=planned, is_project=is_project, latest_id=latest_id) -> None:
            again = _plan_update(snapshot, inp, turn, is_project=is_project, latest_id=latest_id)
            if again.signature != planned.signature:
                raise _Replan()

        try:
            result = agent_tools._task_update(planned.tool_input, precondition=recheck)
        except _Replan:
            continue
        if planned.kind == "park" and result.startswith("Task updated"):
            result += "\n\n" + _open_request(current.id, planned.token, ACTION_RELEASE, turn)
        return result
    return "Error: the task kept changing while it was being checked; try again."


def _run_project_action(inp: dict, turn: ApprovalTurn, action: str) -> Optional[str]:
    from api.services import agent_tools
    from api.services.task_manager import get_task_manager
    from api.services.task_projects import EXECUTION_PAUSED_FIELD

    tm = get_task_manager()
    task_id = inp.get("task_id")

    def needs_approval(task) -> bool:
        if action == ACTION_PLAN_PROJECT:
            return True
        resumed = copy.copy(task)
        resumed.fields = {k: v for k, v in (task.fields or {}).items() if k != EXECUTION_PAUSED_FIELD}
        if task.status == "in_progress":
            resumed.status = "todo"
        return starts_agent(resumed)

    def run(precondition=None) -> str:
        if action == ACTION_PLAN_PROJECT:
            return agent_tools._task_plan_project(inp)
        return agent_tools._task_resume_execution(inp, precondition=precondition)

    for _attempt in range(_WRITE_ATTEMPTS):
        current = tm.get(task_id) if task_id else None
        if current is None:
            return None
        if not needs_approval(current):
            def still_free(snapshot) -> None:
                if needs_approval(snapshot):
                    raise _Replan()
            try:
                return run(still_free)
            except _Replan:
                continue
        if turn.verdict().asks_for_agent:
            if is_pending(current):
                tm.update(current.id, fields=_cleared_fields())
            return run()
        latest_id = latest_request_id(tm, turn.conversation_id)
        if turn.approved_request(current, action, latest_id):
            token = current.fields.get(APPROVAL_ID_FIELD)
            approved_hash = current.fields.get(APPROVAL_HASH_FIELD)
            try:
                tm.update(current.id, fields=_cleared_fields(),
                          _precondition=_still_requested(token, approved_hash))
            except ApprovalNotPending:
                continue
            return run(_content_is(approved_hash))
        if is_pending(current) and (current.fields or {}).get(APPROVAL_ACTION_FIELD) == action:
            return _project_notice(current, action, sent=None)
        token = _new_token()
        tm.update(current.id, fields={**_cleared_fields(), **_pending_fields(token, action, turn)})
        return _open_request(current.id, token, action, turn)
    return "Error: the task kept changing while it was being checked; try again."


def _still_requested(token: Optional[str], approved_hash: Optional[str]):
    """Precondition: the request is still pending with this token, and the
    content is still what it was when the request was made."""
    def check(task) -> None:
        fields = task.fields or {}
        if (
            not token
            or fields.get(APPROVAL_FIELD) != PENDING
            or fields.get(APPROVAL_ID_FIELD) != token
            or not approved_hash
            or fields.get(APPROVAL_HASH_FIELD) != approved_hash
            or content_hash(task) != approved_hash
        ):
            raise ApprovalNotPending()
    return check


def _content_is(approved_hash: Optional[str]):
    def check(task) -> None:
        if content_hash(task) != approved_hash:
            raise ApprovalNotPending()
    return check


def _open_request(task_id: str, token: str, action: str, turn: ApprovalTurn) -> str:
    """Seal a just-parked request with its content hash, send it to Telegram,
    and return the instruction for the model."""
    from api.services.task_manager import get_task_manager

    tm = get_task_manager()
    turn.parked_ids.add(task_id)
    task = tm.get(task_id)
    if task is None:
        return ""

    def same_request(snapshot) -> None:
        if (snapshot.fields or {}).get(APPROVAL_ID_FIELD) != token:
            raise ApprovalNotPending()

    try:
        task = tm.update(task_id, fields={APPROVAL_HASH_FIELD: content_hash(task)},
                         _precondition=same_request) or task
    except ApprovalNotPending:
        return ""
    message_id = send_approval_request(task, token, action, turn)
    if message_id is not None:
        try:
            tm.update(task_id, fields={APPROVAL_MESSAGE_FIELD: str(message_id)},
                      _precondition=same_request)
        except ApprovalNotPending:
            pass
    if action == ACTION_RELEASE:
        return _parked_notice(task, sent=message_id is not None)
    return _project_notice(task, action, sent=message_id is not None)


def _parked_notice(task, *, sent: bool) -> str:
    engines = ", ".join(f"#{t}" for t in agent_tags(task)) or "an agent"
    telegram = " An Approve/Deny request was also sent to the operator's Telegram." if sent else ""
    return (
        f"AWAITING OPERATOR APPROVAL: this task would start an agent ({engines}), and the "
        "operator's own message this turn did not ask for agent work, so it was saved parked "
        f"(status blocked, agent_approval: pending) and will not run.{telegram} Tell the operator "
        "what the task is and ask whether to start it. Do not release it in this turn. If their "
        f"next message says yes, release it with manage_tasks update task_id={task.id} status=todo. "
        "A yes releases only the most recent request you asked about."
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
    head = f"Approve agent work?\n\n{what}\n\nTask {task.id}: {task.description}"
    tail = (
        f"\n\nAsked from: {persona_id or 'chat'}. It stays parked until you decide; "
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


def send_approval_request(task, token: str, action: str, turn: ApprovalTurn) -> Optional[int]:
    """Send the Approve/Deny request to the primary bot and return the sent
    message's id, or None; an unsent request still leaves the task parked."""
    try:
        return _send_buttons(
            request_text(task, action, turn.persona_id),
            [("Approve", callback_data(True, task.id, token)),
             ("Deny", callback_data(False, task.id, token))],
        )
    except Exception as exc:  # noqa: BLE001 - the task stays parked either way
        logger.warning("agent-approval Telegram request failed: %s", type(exc).__name__)
        return None


def decide(task_id: str, token: str, *, approve: bool, message_id: Optional[int] = None) -> str:
    """Record the operator's Approve or Deny for one request.

    Bound to the task, its request token, the Telegram message that carried
    the buttons, and the content hash taken when the request was made; every
    one is checked again inside the write, under the task store's lock, and
    the request is spent on first use. Deny keeps the task parked. Approve
    releases a parked task to its engine, or runs the held
    `plan_project`/`resume_execution`.
    """
    from api.services.task_manager import get_task_manager

    tm = get_task_manager()
    current = tm.get(task_id)
    if current is None:
        return "That task no longer exists; nothing was started."
    fields = current.fields or {}
    if message_id is not None and fields.get(APPROVAL_MESSAGE_FIELD) != str(message_id):
        return "This message is not the current request for that task; nothing changed."
    approved_hash = fields.get(APPROVAL_HASH_FIELD)
    check = _still_requested(token, approved_hash)
    try:
        if fields.get(APPROVAL_FIELD) != PENDING or fields.get(APPROVAL_ID_FIELD) != token:
            raise ApprovalNotPending()
        if content_hash(current) != approved_hash:
            return ("The task changed after this request was sent, so nothing was started. "
                    "It stays parked.")
        action = fields.get(APPROVAL_ACTION_FIELD) or ACTION_RELEASE
        if not approve:
            tm.update(task_id, fields={APPROVAL_FIELD: DENIED, APPROVAL_ID_FIELD: None},
                      _precondition=check)
            return "Denied. The task stays parked and no agent will start."
        if action == ACTION_RELEASE:
            kwargs: dict[str, Any] = {"fields": _cleared_fields()}
            if current.status == PARKED_STATUS:
                kwargs["status"] = _restore_status(current)
            task = tm.update(task_id, _precondition=check, **kwargs)
            engines = ", ".join(f"#{t}" for t in agent_tags(task)) if task else ""
            return f"Approved. Released to {engines or 'its agent'}."
        tm.update(task_id, fields=_cleared_fields(), _precondition=check)
    except ApprovalNotPending:
        return "This request was already decided, replaced or changed; nothing changed."

    from api.services import agent_tools

    try:
        if action == ACTION_PLAN_PROJECT:
            outcome = agent_tools._task_plan_project({"task_id": task_id, "operation_id": f"approval-{token}"})
        else:
            outcome = agent_tools._task_resume_execution(
                {"task_id": task_id}, precondition=_content_is(approved_hash),
            )
    except ApprovalNotPending:
        return "Approved, but the task changed before it started; nothing was started."
    except Exception as exc:  # noqa: BLE001 - report, never crash the listener
        return f"Approved, but it could not start: {exc}"
    return f"Approved. {outcome}"
