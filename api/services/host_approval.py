"""Telegram approval for `lifeos_shell` commands outside the allowlist.

Off by default (`LIFEOS_HOST_TOOLS_SHELL_APPROVAL`). When on, a command
outside the allowlist is not run. Instead the operator's configured Telegram
chat gets the exact command, and the caller gets a "pending" result straight
away. The operator approves by replying APPROVE to that message; any other
reply refuses it.

An approval does not run anything by itself. The command runs once, the next
time the same caller asks for the same command in the same directory as the
same account. If the caller never asks again, nothing runs and the approval
expires unused (or, with expiry turned off, stays unused). An approval is bound
to a hash of the caller, the run-as account, the directory and the argv, and
is spent the moment the command starts, so it cannot be replayed.

The MCP server and the Telegram listener run in different processes, so
requests and decisions live in a small SQLite file both can open.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from api.services.sqlite_connect import connect_closing

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "host_approvals.db"
APPROVE_WORD = "approve"
# Requests from one caller that may wait for an answer at the same time.
MAX_PENDING_PER_CALLER = 3
# One Telegram message holds 4,096 characters. A request that does not fit is
# refused rather than split or shortened, so the operator always sees all of it.
MESSAGE_MAX_CHARS = 3800

# States: pending -> approved -> ran; pending -> denied -> reported;
# pending or approved -> expired; pending -> unsent when the message fails.
_LIVE_STATES = ("pending", "approved", "denied")


@dataclass(frozen=True)
class ApprovalRow:
    id: str
    created_at: float
    expires_at: float | None
    caller: str
    command_hash: str
    chat_id: str
    state: str
    decided_at: float | None
    approver: str | None

    @property
    def short_id(self) -> str:
        return self.id[:8]


def command_hash(*, caller: str, run_as: str, cwd: str, argv: list[str]) -> str:
    """The hash an approval is bound to."""
    payload = json.dumps(
        {"caller": caller, "run_as": run_as, "cwd": cwd, "argv": argv},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


class ApprovalStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS host_approval_requests ("
                " id TEXT PRIMARY KEY,"
                " created_at REAL NOT NULL,"
                " expires_at REAL,"
                " caller TEXT NOT NULL,"
                " command_hash TEXT NOT NULL,"
                " chat_id TEXT NOT NULL,"
                " message_ids TEXT NOT NULL DEFAULT '[]',"
                " state TEXT NOT NULL DEFAULT 'pending',"
                " decided_at REAL,"
                " approver TEXT,"
                " closed_at REAL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS host_approval_requests_lookup "
                "ON host_approval_requests (caller, command_hash, created_at)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with connect_closing(str(self.db_path), timeout=10.0, isolation_level=None) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> ApprovalRow | None:
        if row is None:
            return None
        return ApprovalRow(
            id=row["id"], created_at=row["created_at"], expires_at=row["expires_at"],
            caller=row["caller"], command_hash=row["command_hash"], chat_id=row["chat_id"],
            state=row["state"], decided_at=row["decided_at"], approver=row["approver"],
        )

    def create(
        self, *, caller: str, command_hash: str, chat_id: str,
        expires_at: float | None, now: float | None = None,
    ) -> str:
        request_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO host_approval_requests "
                "(id, created_at, expires_at, caller, command_hash, chat_id) VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, time.time() if now is None else now, expires_at, caller, command_hash, str(chat_id)),
            )
        return request_id

    def attach_messages(self, request_id: str, message_ids: list[int]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE host_approval_requests SET message_ids = ? WHERE id = ?",
                (json.dumps([int(m) for m in message_ids]), request_id),
            )

    def get(self, request_id: str) -> ApprovalRow | None:
        with self._connect() as conn:
            return self._row(conn.execute(
                "SELECT * FROM host_approval_requests WHERE id = ?", (request_id,),
            ).fetchone())

    def latest(self, caller: str, command_hash: str, now: float | None = None) -> ApprovalRow | None:
        """The newest live, unexpired request for this caller and command."""
        now = time.time() if now is None else now
        with self._connect() as conn:
            return self._row(conn.execute(
                "SELECT * FROM host_approval_requests WHERE caller = ? AND command_hash = ? "
                "AND state IN (?, ?, ?) AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (caller, command_hash, *_LIVE_STATES, now),
            ).fetchone())

    def count_pending(self, caller: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM host_approval_requests WHERE caller = ? AND state = 'pending' "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (caller, now),
            ).fetchone()[0])

    def decide_by_reply(
        self, reply_to_message_id: int, text: str, *, chat_id: str, now: float | None = None,
    ) -> ApprovalRow | None:
        """Record a reply from `chat_id` to an open request's message.

        Returns the decided request, or None when the reply matches no pending,
        unexpired request sent to that chat.
        """
        now = time.time() if now is None else now
        state = "approved" if text.strip().lower() == APPROVE_WORD else "denied"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM host_approval_requests WHERE state = 'pending' AND chat_id = ? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "AND EXISTS (SELECT 1 FROM json_each(message_ids) WHERE value = ?)",
                (str(chat_id), now, int(reply_to_message_id)),
            ).fetchone()
            if not row:
                return None
            changed = conn.execute(
                "UPDATE host_approval_requests SET state = ?, decided_at = ?, approver = ? "
                "WHERE id = ? AND state = 'pending'",
                (state, now, f"telegram:{chat_id}", row["id"]),
            ).rowcount
        return self.get(row["id"]) if changed == 1 else None

    def find_by_message(self, message_id: int, *, chat_id: str) -> ApprovalRow | None:
        """The request (in any state) whose Telegram message this is, if any."""
        with self._connect() as conn:
            return self._row(conn.execute(
                "SELECT * FROM host_approval_requests WHERE chat_id = ? "
                "AND EXISTS (SELECT 1 FROM json_each(message_ids) WHERE value = ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (str(chat_id), int(message_id)),
            ).fetchone())

    def pending_in_chat(self, chat_id: str, now: float | None = None) -> list[ApprovalRow]:
        """Pending, unexpired requests sent to `chat_id`, oldest first."""
        now = time.time() if now is None else now
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM host_approval_requests WHERE chat_id = ? AND state = 'pending' "
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at",
                (str(chat_id), now),
            ).fetchall()
        return [r for r in (self._row(row) for row in rows) if r is not None]

    def consume(self, request_id: str, command_hash: str, now: float | None = None) -> bool:
        """Spend an approval. True only for the one call that wins it."""
        now = time.time() if now is None else now
        with self._connect() as conn:
            return conn.execute(
                "UPDATE host_approval_requests SET state = 'ran', closed_at = ? "
                "WHERE id = ? AND command_hash = ? AND state = 'approved' "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (now, request_id, command_hash, now),
            ).rowcount == 1

    def mark_reported(self, request_id: str, now: float | None = None) -> bool:
        """Close a denial once the caller has been told about it."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE host_approval_requests SET state = 'reported', closed_at = ? "
                "WHERE id = ? AND state = 'denied'",
                (time.time() if now is None else now, request_id),
            ).rowcount == 1

    def mark_unsent(self, request_id: str, now: float | None = None) -> None:
        """Close a request whose message never reached the operator."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE host_approval_requests SET state = 'unsent', closed_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (time.time() if now is None else now, request_id),
            )

    def sweep_expired(self, now: float | None = None) -> list[ApprovalRow]:
        """Expire pending and unused approved requests past their expiry.

        Returns them as they were before expiry, so each can be audited once.
        """
        now = time.time() if now is None else now
        with self._connect() as conn:
            rows = [self._row(r) for r in conn.execute(
                "SELECT * FROM host_approval_requests WHERE state IN ('pending', 'approved') "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchall()]
            expired = []
            for row in rows:
                if conn.execute(
                    "UPDATE host_approval_requests SET state = 'expired', closed_at = ? "
                    "WHERE id = ? AND state = ?",
                    (now, row.id, row.state),
                ).rowcount == 1:
                    expired.append(row)
        return expired


@dataclass(frozen=True)
class GateResult:
    """What the gate decided for one call.

    outcome is "run" (an approval was spent; run the command now), "pending"
    (waiting for the operator), "denied" (the operator refused it) or
    "refused" (the request could not be made).
    """
    outcome: str
    message: str
    request: ApprovalRow | None = None


def _default_sender(chat_id: str) -> Callable[[str], list[int]]:
    def send(text: str) -> list[int]:
        from api.services.telegram import send_message_capture_ids

        # Plain text: Markdown would change how the command reads.
        return send_message_capture_ids(text, chat_id=chat_id, parse_mode=None)

    return send


class ApprovalGate:
    """Ask the operator on Telegram, and hand out each approval once."""

    def __init__(
        self,
        *,
        timeout_seconds: int | None,
        chat_id: str,
        store: ApprovalStore | None = None,
        send: Callable[[str], list[int]] | None = None,
        clock: Callable[[], float] = time.time,
        max_pending: int = MAX_PENDING_PER_CALLER,
    ):
        self.timeout_seconds = timeout_seconds
        self.chat_id = str(chat_id or "").strip()
        self.store = store or ApprovalStore()
        self.send = send if send is not None else (_default_sender(self.chat_id) if self.chat_id else None)
        self.clock = clock
        self.max_pending = max_pending

    def sweep(self) -> list[ApprovalRow]:
        return self.store.sweep_expired(now=self.clock())

    def check(self, *, caller: str, command_hash: str, details: str) -> GateResult:
        if not self.chat_id or self.send is None:
            return GateResult("refused", "approval is on but no Telegram chat is configured (TELEGRAM_CHAT_ID)")
        now = self.clock()
        row = self.store.latest(caller, command_hash, now=now)
        if row is not None:
            if row.state == "approved":
                if self.store.consume(row.id, command_hash, now=now):
                    return GateResult("run", f"approved request {row.short_id}", row)
            elif row.state == "pending":
                return GateResult(
                    "pending",
                    f"request {row.short_id} is still waiting for the operator "
                    f"(asked {format_age(now - row.created_at)} ago). Ask again with the same "
                    "command after it is approved; it will run once.",
                    row,
                )
            elif row.state == "denied":
                self.store.mark_reported(row.id, now=now)
                return GateResult("denied", f"the operator refused request {row.short_id}", row)
        if self.store.count_pending(caller, now=now) >= self.max_pending:
            return GateResult(
                "refused",
                f"{self.max_pending} requests from this caller are already waiting for the operator",
            )
        expires_at = None if self.timeout_seconds is None else now + self.timeout_seconds

        def message(short_id: str) -> str:
            return (
                f"LifeOS host tools: approval needed (request {short_id})\n"
                f"Asked: {_utc(now)}\n"
                f"Expires: {_utc(expires_at) if expires_at is not None else 'never'}\n\n"
                f"{details}\n\n"
                "Reply APPROVE to this message to allow it once. Any other reply refuses it. "
                "Nothing runs until the caller asks for this exact command again."
            )

        if len(message("0" * 8)) > MESSAGE_MAX_CHARS:
            return GateResult("refused", "the command is too long to show in one approval message")
        request_id = self.store.create(
            caller=caller, command_hash=command_hash, chat_id=self.chat_id,
            expires_at=expires_at, now=now,
        )
        text = message(request_id[:8])
        message_ids = self.send(text)
        if not message_ids:
            self.store.mark_unsent(request_id, now=now)
            return GateResult("refused", "the approval message could not be sent")
        self.store.attach_messages(request_id, message_ids)
        row = self.store.get(request_id)
        return GateResult(
            "pending",
            f"sent to the operator for approval as request {request_id[:8]}. Ask again with the "
            "same command after it is approved; it will run once.",
            row,
        )

    def notify_ran(self, row: ApprovalRow, exit_code: int | None) -> None:
        """Tell the operator an approved command ran. Best effort."""
        if self.send is None:
            return
        now = self.clock()
        approved_ago = format_age(now - row.decided_at) if row.decided_at else "?"
        try:
            self.send(
                f"LifeOS host tools: request {row.short_id} ran (exit {exit_code}). "
                f"Asked {format_age(now - row.created_at)} ago, approved {approved_ago} ago."
            )
        except Exception as exc:  # noqa: BLE001 — a notice must not fail the call
            logger.warning("host approval run notice failed: %s", exc)


def already_decided_text(row: ApprovalRow, now: float | None = None) -> str:
    """The listener's answer to a reply to a request that is not pending."""
    state = row.state
    if state == "pending" and row.expires_at is not None and row.expires_at <= (time.time() if now is None else now):
        state = "expired"
    return f"Request {row.short_id} is no longer waiting ({state}). Nothing changed."


def unthreaded_approve_text(rows: list[ApprovalRow]) -> str:
    """The listener's answer to a bare APPROVE that is not a reply to a request."""
    ids = ", ".join(r.short_id for r in rows)
    return (
        "Nothing was approved. To approve a command, reply APPROVE to the request's "
        f"own message. Waiting: {ids}."
    )


def approval_reply_text(row: ApprovalRow, now: float | None = None) -> str:
    """The listener's answer to the operator's reply."""
    now = time.time() if now is None else now
    age = format_age(now - row.created_at)
    if row.state == "approved":
        return (
            f"Approved request {row.short_id} (asked {age} ago). It runs once, the next time "
            f"{row.caller} asks for this exact command."
        )
    return f"Refused request {row.short_id} (asked {age} ago)."
