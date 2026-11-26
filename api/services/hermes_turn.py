"""The operator turn behind a Hermes call to the LifeOS tools that can start
agent work.

Hermes reaches LifeOS through the stdio MCP server it starts
(`mcp_server.py`). Everything inside the Hermes process (its model, its
plugins, its terminal tool) runs as the same Unix user as LifeOS and can
read any file or environment variable that user can, so nothing Hermes
sends can carry a secret that proves anything. The agent-work approval gate
(`agent_task_approval`) therefore takes the operator's words for a Hermes
turn from one place only: LifeOS's own record of the question it forwarded
to Hermes.

- When LifeOS forwards a turn to Hermes (`/api/hermes/ask/stream`: the
  Hermes chat backend, persona bots on Hermes), it writes a
  `caller_session_id` and a fresh per-turn `lifeos_turn_id` into the turn
  and records the operator's question under them (`record_proxy_turn`).
  A newer question for the same session replaces the older one, the record
  is closed when the relayed turn ends (`end_proxy_turn`), and it lapses
  after `PROXY_TURN_MAX_AGE_SECONDS`.
- Hermes's adapter writes both ids into the turn's opening block, and its
  `lifeos_agent_approval` plugin puts them, read from that fixed position,
  into the `_lifeos_hermes_turn` argument of every gated tool call as
  `<caller_session_id>.<lifeos_turn_id>`, overwriting anything the model
  wrote there. `mcp_server.py`, started with `LIFEOS_MCP_CALLER=hermes`,
  sends it as `X-LifeOS-Hermes-Turn`, or `none`.
- The value names a turn; it proves nothing. `resolve` looks it up in
  LifeOS's own stores:

  - a running board `#hermes` worker session: the call is that agent
    session's own write, exactly as if it had sent `X-LifeOS-Agent-Session`;
  - the exact forwarded turn that is open now for that session: the gate
    judges LifeOS's copy of the operator's question for that turn;
  - anything else (a Hermes Telegram, CLI or cron turn, a delegated
    sub-agent, a spoken turn that reached Hermes directly, an earlier turn
    of the same conversation, an unknown, closed, lapsed or malformed
    value): there is no operator text, so agent work is parked for the
    operator's Telegram approval.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

HEADER = "X-LifeOS-Hermes-Turn"
ARGUMENT = "_lifeos_hermes_turn"
NO_TURN = "none"
PROXY_TURN_MAX_AGE_SECONDS = 15 * 60.0
_PROXY_TURN_LIMIT = 1024
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# `<caller_session_id>` or `<caller_session_id>.<lifeos_turn_id>`.
VALUE_RE = re.compile(r"^([A-Za-z0-9_-]{1,128})(?:\.([A-Za-z0-9_-]{1,64}))?$")


def parse_value(value: Any) -> tuple[str, str]:
    """`(session_id, turn_id)` a header or argument value names; either is
    "" when absent (`none`, or not a well-formed value)."""
    text = value.strip() if isinstance(value, str) else ""
    match = VALUE_RE.fullmatch(text)
    if text == NO_TURN or not match:
        return "", ""
    return match.group(1), match.group(2) or ""


# ---------------------------------------------------------------------------
# LifeOS's own record of the turns it forwards to Hermes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProxyTurn:
    question: str
    conversation_id: str
    persona_id: str
    started_at: float
    turn_id: str


_proxy_turns: "OrderedDict[str, ProxyTurn]" = OrderedDict()
_proxy_lock = threading.Lock()


def record_proxy_turn(caller_session_id: str, question: str, conversation_id: Optional[str],
                      persona_id: str, *, now: Optional[float] = None) -> str:
    """Record the operator's question LifeOS is forwarding to Hermes under
    `caller_session_id`, replacing any earlier turn for that session.
    Returns the new turn's `lifeos_turn_id` ("" when there is no session to
    record it under)."""
    if not _SESSION_ID_RE.fullmatch(caller_session_id or ""):
        return ""
    now = time.time() if now is None else now
    turn_id = secrets.token_hex(8)
    with _proxy_lock:
        _proxy_turns.pop(caller_session_id, None)
        _proxy_turns[caller_session_id] = ProxyTurn(
            question or "", conversation_id or "", persona_id or "", now, turn_id,
        )
        while len(_proxy_turns) > _PROXY_TURN_LIMIT:
            _proxy_turns.popitem(last=False)
    return turn_id


def end_proxy_turn(caller_session_id: str, turn_id: str) -> None:
    """Close the forwarded turn `turn_id` once its relay has ended; a newer
    turn for the same session is left alone."""
    with _proxy_lock:
        current = _proxy_turns.get(caller_session_id or "")
        if current is not None and current.turn_id == turn_id:
            del _proxy_turns[caller_session_id]


def current_proxy_turn(caller_session_id: str, turn_id: str, *,
                       now: Optional[float] = None) -> Optional[ProxyTurn]:
    """The forwarded turn `turn_id` when it is the open turn for
    `caller_session_id` now, or None (another turn is current, none is, or
    it is older than `PROXY_TURN_MAX_AGE_SECONDS`)."""
    now = time.time() if now is None else now
    with _proxy_lock:
        turn = _proxy_turns.get(caller_session_id or "")
    if (
        turn is None
        or not turn_id
        or turn.turn_id != turn_id
        or not 0 <= now - turn.started_at <= PROXY_TURN_MAX_AGE_SECONDS
    ):
        return None
    return turn


def clear_proxy_turns() -> None:
    with _proxy_lock:
        _proxy_turns.clear()


# ---------------------------------------------------------------------------
# Header -> caller
# ---------------------------------------------------------------------------

def _session_store():
    from api.services.agent_worker.session_store import SessionStore

    return SessionStore()


def _running_worker_session(session_id: str) -> bool:
    """A worker session running a Hermes turn now: an approved agent session
    (a board `#hermes` task, or one an approved session spawned), not the
    root session LifeOS keeps for a chat conversation."""
    from api.services.agent_worker.hermes_session import HERMES_ROUTING
    from api.services.agent_worker.session_store import STATUS_RUNNING

    session = _session_store().get_by_session_id(session_id)
    return bool(
        session is not None
        and (session.origin or "") != "hermes"
        and session.routing == HERMES_ROUTING
        and session.status == STATUS_RUNNING
    )


@dataclass(frozen=True)
class HermesCaller:
    """Who a Hermes call acts as. Exactly one field is set: `agent_session_id`
    for a running worker session's own write, `approval_turn` for a call the
    agent-work approval gate judges."""
    agent_session_id: Optional[str] = None
    approval_turn: Any = None


def resolve(header_value: Optional[str]) -> HermesCaller:
    """Resolve an `X-LifeOS-Hermes-Turn` value against LifeOS's own records."""
    from api.services.agent_task_approval import ApprovalTurn

    session_id, turn_id = parse_value(header_value)
    if session_id and _running_worker_session(session_id):
        return HermesCaller(agent_session_id=session_id)
    recorded = current_proxy_turn(session_id, turn_id) if session_id else None
    if recorded is None:
        return HermesCaller(approval_turn=ApprovalTurn("", persona_id="hermes", surface="mcp"))
    persona = f"hermes/{recorded.persona_id}" if recorded.persona_id else "hermes"
    return HermesCaller(approval_turn=ApprovalTurn(
        recorded.question, conversation_id=recorded.conversation_id, persona_id=persona,
        started_at=recorded.started_at, surface="mcp",
    ))
