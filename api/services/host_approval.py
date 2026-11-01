"""Telegram approval for `lifeos_shell` commands outside the allowlist.

Off by default (`LIFEOS_HOST_TOOLS_SHELL_APPROVAL`). When on, the MCP server
process sends the command to the primary Telegram bot and waits. The operator
approves by replying to that message with the single word APPROVE; any other
reply denies it, and no reply before the timeout denies it.

The MCP server and the Telegram listener run in different processes, so the
request and its decision live in a small SQLite file both can open.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "host_approvals.db"
APPROVE_WORD = "approve"


class ApprovalStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS host_approvals ("
                " id TEXT PRIMARY KEY,"
                " created_at REAL NOT NULL,"
                " expires_at REAL NOT NULL,"
                " message_ids TEXT NOT NULL DEFAULT '[]',"
                " decision TEXT,"
                " decided_at REAL)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return closing(conn)  # type: ignore[return-value]

    def create(self, expires_at: float) -> str:
        request_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO host_approvals (id, created_at, expires_at) VALUES (?, ?, ?)",
                (request_id, time.time(), expires_at),
            )
        return request_id

    def attach_messages(self, request_id: str, message_ids: list[int]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE host_approvals SET message_ids = ? WHERE id = ?",
                (json.dumps([int(m) for m in message_ids]), request_id),
            )

    def decide_by_reply(self, reply_to_message_id: int, text: str) -> bool:
        """Record a reply to an open request. Returns True when one matched."""
        now = time.time()
        decision = "approved" if text.strip().lower() == APPROVE_WORD else "denied"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM host_approvals WHERE decision IS NULL AND expires_at > ? "
                "AND EXISTS (SELECT 1 FROM json_each(message_ids) WHERE value = ?)",
                (now, int(reply_to_message_id)),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE host_approvals SET decision = ?, decided_at = ? "
                "WHERE id = ? AND decision IS NULL",
                (decision, now, row["id"]),
            )
        return True

    def decision(self, request_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT decision FROM host_approvals WHERE id = ?", (request_id,),
            ).fetchone()
        return row["decision"] if row else None

    def expire(self, request_id: str) -> str:
        """Mark a still-open request timed out. Returns the final decision."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE host_approvals SET decision = 'timeout', decided_at = ? "
                "WHERE id = ? AND decision IS NULL",
                (time.time(), request_id),
            )
        return self.decision(request_id) or "timeout"


def request_approval(
    summary: str,
    *,
    timeout_seconds: int,
    store: ApprovalStore | None = None,
    send: Callable[[str], list[int]] | None = None,
    poll_seconds: float = 1.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Ask the operator on Telegram. Returns "approved", "denied" or "timeout"."""
    store = store or ApprovalStore()
    if send is None:
        from api.services.telegram import send_message_capture_ids
        from config.settings import settings

        if not settings.telegram_enabled:
            return "denied"
        send = send_message_capture_ids
    deadline = clock() + timeout_seconds
    request_id = store.create(expires_at=deadline)
    text = (
        "LifeOS host tools: approval needed.\n\n"
        f"{summary}\n\n"
        f"Reply to this message with APPROVE within {timeout_seconds} seconds to run it. "
        "Any other reply, or no reply, refuses it."
    )
    message_ids = send(text)
    if not message_ids:
        store.expire(request_id)
        return "denied"
    store.attach_messages(request_id, message_ids)
    while clock() < deadline:
        decision = store.decision(request_id)
        if decision:
            return decision
        sleep(poll_seconds)
    return store.expire(request_id)
