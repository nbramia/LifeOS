"""Operator approval for agent work a Hermes call puts on a schedule.

A schedule starts agent work when its action is `agent` (every fire writes
an engine-assigned task) or `endpoint` with `POST` (every fire calls a
LifeOS write route, task creation included). For a Hermes call that the
agent-work approval gate judges (`hermes_turn`, `agent_task_approval`), and
whose operator turn did not ask for agent work in so many words:

- **Create or update.** A write that would leave such a schedule enabled,
  when it was not one before or its definition changes, is saved with the
  schedule disabled, and the operator gets an Approve/Deny request on
  Telegram. Approve enables it.
- **Trigger.** A trigger of such a schedule does not fire it; the operator
  gets an Approve/Deny request, and Approve fires it once.

Each request is bound to the schedule and a hash of its definition (what it
runs, when, and on which engine) as the request showed it, is checked again
under the scheduler store's lock when decided, and is spent on first use.
Requests are held in this process: a button pressed after a restart finds
no open request and changes nothing, leaving the schedule disabled or
unfired for the operator to enable or trigger on the board.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Optional

CALLBACK_PREFIX = "ats"
_CALLBACK_RE = re.compile(r"^ats:([yn]):([0-9a-f]{16})$")
KIND_ENABLE = "enable_schedule"
KIND_TRIGGER = "trigger_schedule"
_REQUEST_LIMIT = 256
_REQUEST_TEXT_MAX_CHARS = 3800
_PARAMS_MAX_CHARS = 1500

# What a schedule runs, when, and as whom: the definition an approval
# approves. `name` and `bot` (where its notices go) are presentation.
_DEFINITION_FIELDS = (
    "schedule_type", "schedule_value", "timezone", "action", "message_type",
    "message_content", "endpoint_config", "executor", "persona_id", "model_id",
    "effort", "host", "working_dir", "budget_dollars", "wall_seconds",
    "agent_creator_session",
)


def starts_agent_work(action: Optional[str], endpoint_config: Any) -> bool:
    """True for a schedule action whose fire can start agent work."""
    if action == "agent":
        return True
    if action == "endpoint":
        cfg = endpoint_config if isinstance(endpoint_config, dict) else {}
        return str(cfg.get("method", "")).strip().upper() == "POST"
    return False


def _runs_agent_work(entry) -> bool:
    return bool(entry.enabled) and starts_agent_work(entry.action, entry.endpoint_config)


def definition_hash(entry) -> str:
    payload = {name: getattr(entry, name, None) for name in _DEFINITION_FIELDS}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Decisions for the scheduler routes
# ---------------------------------------------------------------------------

def holds_create(action: Optional[str], endpoint_config: Any, enabled: bool, turn) -> bool:
    """True when a Hermes create must be saved disabled and asked about."""
    return bool(enabled) and starts_agent_work(action, endpoint_config) and not turn.verdict().asks_for_agent


def holds_update(current, updates: dict, turn) -> bool:
    """True when a Hermes update must be saved with the schedule disabled
    and asked about: the result runs agent work while enabled, and either
    the schedule did not before or its definition changes."""
    known = {k: v for k, v in updates.items() if k in type(current).__dataclass_fields__}
    after = replace(current, **known)
    if not _runs_agent_work(after):
        return False
    if _runs_agent_work(current) and definition_hash(current) == definition_hash(after):
        return False
    return not turn.verdict().asks_for_agent


def prompt_source_for(turn, agent_session: Optional[str], *, writes_text: bool) -> Optional[str]:
    """The `ScheduleEntry.prompt_source` a schedule create (or an update that
    writes `message_content`, `writes_text`) records: `PROMPT_SOURCE_AGENT`
    for an agent session's write, `PROMPT_SOURCE_HERMES` for a Hermes call
    the gate judges without an operator request for agent work, "" for the
    operator's own write, and None (leave it as it is) for any other call or
    an update that leaves the text alone."""
    from api.services.scheduler_store import PROMPT_SOURCE_AGENT, PROMPT_SOURCE_HERMES

    if not writes_text:
        return None
    if agent_session:
        return PROMPT_SOURCE_AGENT
    if turn is None:
        return ""
    if turn.verdict().asks_for_agent:
        return None
    return PROMPT_SOURCE_HERMES


def holds_trigger(entry, turn) -> bool:
    """True when a Hermes trigger must not fire and is asked about instead."""
    return starts_agent_work(entry.action, entry.endpoint_config) and not turn.verdict().asks_for_agent


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

@dataclass
class _Request:
    schedule_id: str
    kind: str
    definition: str
    message_id: Optional[int] = None


_requests: "OrderedDict[str, _Request]" = OrderedDict()
_requests_lock = threading.Lock()


def clear_requests() -> None:
    with _requests_lock:
        _requests.clear()


def callback_data(approve: bool, token: str) -> str:
    return f"{CALLBACK_PREFIX}:{'y' if approve else 'n'}:{token}"


def parse_callback_data(data: str) -> Optional[tuple[bool, str]]:
    match = _CALLBACK_RE.match(data or "")
    if not match:
        return None
    return match.group(1) == "y", match.group(2)


def request_text(entry, kind: str, persona_id: str = "") -> str:
    """The Telegram request: the exact schedule the buttons decide."""
    what = ("Enable this agent schedule" if kind == KIND_ENABLE
            else "Run this agent schedule once, now")
    when = "manual" if entry.schedule_type == "manual" else f"{entry.schedule_type}: {entry.schedule_value}"
    action = entry.action
    if entry.action == "agent" and entry.executor:
        action = f"agent (#{entry.executor})"
    params = ""
    if entry.action == "endpoint":
        cfg = entry.endpoint_config or {}
        action = f"endpoint ({str(cfg.get('method', '')).upper()} {cfg.get('endpoint', '')})"
        if cfg.get("params"):
            params = json.dumps(cfg["params"], indent=1, sort_keys=True, ensure_ascii=False, default=str)
            if len(params) > _PARAMS_MAX_CHARS:
                params = params[:_PARAMS_MAX_CHARS].rstrip() + "\n[parameters continue in the schedule]"
    head = (f"Approve agent work?\n\n{what}\n\nSchedule {entry.id}: {entry.name}\n"
            f"When: {when}\nAction: {action}")
    if params:
        head += f"\n\nIt sends:\n{params}"
    tail = f"\n\nAsked from: {persona_id or 'hermes'}. Nothing runs until you decide."
    body = (entry.message_content or "").strip()
    if body:
        room = _REQUEST_TEXT_MAX_CHARS - len(head) - len(tail) - 20
        if len(body) > room:
            body = body[: max(0, room - 40)].rstrip() + "\n[continues in the schedule]"
        head += f"\n\nIt runs:\n{body}"
    return head + tail


def open_request(entry, kind: str, turn) -> str:
    """Send the Approve/Deny request for `entry` and return the notice for
    the model."""
    from api.services import agent_task_approval

    token = secrets.token_hex(8)
    with _requests_lock:
        _requests[token] = _Request(entry.id, kind, definition_hash(entry))
        while len(_requests) > _REQUEST_LIMIT:
            _requests.popitem(last=False)
    message_id = None
    try:
        message_id = agent_task_approval._send_buttons(
            request_text(entry, kind, turn.persona_id),
            [("Approve", callback_data(True, token)), ("Deny", callback_data(False, token))],
        )
    except Exception:  # noqa: BLE001 - the schedule stays held either way
        message_id = None
    with _requests_lock:
        request = _requests.get(token)
        if request is not None:
            request.message_id = message_id
    return notice(entry, kind, sent=message_id is not None)


def notice(entry, kind: str, *, sent: bool) -> str:
    telegram = " An Approve/Deny request was sent to the operator's Telegram." if sent else ""
    held = ("it was saved disabled and will not run until the operator approves it"
            if kind == KIND_ENABLE else "it was not run")
    outcome = "enables it" if kind == KIND_ENABLE else "runs it once"
    return (
        f"NOT STARTED, AWAITING OPERATOR APPROVAL: schedule {entry.id} (\"{entry.name}\") runs "
        "agent work, and the operator's own message this turn did not ask for agent work, so "
        f"{held}.{telegram} Tell the operator what the schedule does and that Approve on "
        f"Telegram {outcome}. Do not enable or trigger it yourself."
    )


def decide(token: str, *, approve: bool, message_id: Optional[int] = None) -> str:
    """Record the operator's Approve or Deny for one schedule request. Bound
    to the request token, the Telegram message that carried the buttons and
    the schedule's definition as the request showed it; spent on first use."""
    from api.services.scheduler_store import get_scheduler, get_scheduler_store

    with _requests_lock:
        request = _requests.get(token)
        if request is None:
            return "This request is not open; nothing changed."
        if message_id is not None and request.message_id != message_id:
            return "This message is not the current request for that schedule; nothing changed."
        del _requests[token]
    if not approve:
        return "Denied. Nothing was enabled or run."
    store = get_scheduler_store()
    with store._lock:
        entry = store.get(request.schedule_id)
        if entry is None:
            return "That schedule no longer exists; nothing was started."
        if definition_hash(entry) != request.definition:
            return "The schedule changed after this request was sent, so nothing was started."
        if request.kind == KIND_ENABLE:
            store.update(entry.id, enabled=True)
            return f"Approved. Schedule \"{entry.name}\" is enabled."
    asyncio.run(get_scheduler()._fire_entry(entry, manual=True, request_key=f"approval-{token}"))
    return f"Approved. Schedule \"{entry.name}\" fired."
