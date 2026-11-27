"""Operator Approve/Deny for a local caller on this host.

A process on this machine (Chief of Staff's `ask-nathan`) opens a request
with `POST /api/approvals/external`: a short summary, an optional source
line, the 64-hex `request_hash` of the action it wants to take, and a
lifetime. The primary bot sends the operator the summary with Approve and
Deny buttons; the caller polls `GET /api/approvals/external/{token}` and
checks that the `request_hash` it reads back is the one it opened.

- **Who may call.** Only a direct loopback peer that carries no proxy,
  Tailscale or public-edge header, is not a browser, and is not one of
  LifeOS's own relayed tool calls (agent sessions, Hermes turns, connected
  apps, scheduled fires). `refusal_reason` applies every rule.
- **Who decides.** Only the operator's press in the operator chat, which
  `TelegramBotListener._handle_callback_query` checks before it calls
  `ExternalApprovalStore.decide`.
- **Binding and single use.** A decision is bound to the token and the
  message that carried the buttons, counts only while the request is
  pending and unexpired, and is written with one conditional UPDATE, so
  exactly one press wins across threads and processes.
- **Persistence.** Requests live in SQLite next to the other LifeOS stores
  and survive a restart; expiry comes from the stored timestamps. Rows
  more than `RETENTION_SECONDS` past expiry are deleted when a request is
  opened. A press on a request missing from the store answers that it was
  lost, and `GET` reports 404.

Summaries and sources are shown to the operator and stored, never logged.
"""
from __future__ import annotations

import logging
import posixpath
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from api.services.mcp_oauth import PUBLIC_EDGE_HEADERS
from api.services.sqlite_connect import connect_closing
from config.settings import settings

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "ext"
_CALLBACK_RE = re.compile(r"^ext:([yn]):([0-9a-f]{16})$")
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
REQUEST_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

SUMMARY_MAX_CHARS = 300
SOURCE_MAX_CHARS = 120
TTL_MIN_SECONDS = 60
TTL_MAX_SECONDS = 86400
TTL_DEFAULT_SECONDS = 10800
RETENTION_SECONDS = 7 * 86400

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"
STATE_EXPIRED = "expired"

OUTCOME_APPROVED = "Approved."
OUTCOME_DENIED = "Denied."
OUTCOME_EXPIRED = "This request expired; nothing was approved."
OUTCOME_ALREADY_DECIDED = "This request was already decided; nothing changed."
OUTCOME_LOST = "This request was lost; ask again."
OUTCOME_WRONG_MESSAGE = "This message is not the current request; nothing changed."

# ---------------------------------------------------------------------------
# Who may call
# ---------------------------------------------------------------------------

LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# Headers LifeOS itself puts on calls it relays for agents, Hermes,
# connected apps and scheduled fires.
SCHEDULED_FIRE_HEADER = "X-LifeOS-Scheduled-Fire"
LIFEOS_RELAY_HEADERS = (
    "x-lifeos-agent-session",
    "x-lifeos-hermes-turn",
    "x-lifeos-connected-app",
    "x-lifeos-turn-id",
    SCHEDULED_FIRE_HEADER.lower(),
)
# A loopback peer carrying any of these was proxied (Tailscale Serve and
# Funnel forward to 127.0.0.1) or came from a browser.
PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "forwarded",
    "x-real-ip",
    *PUBLIC_EDGE_HEADERS,
)
REFUSED_HEADER_PREFIXES = ("tailscale-",)
BROWSER_HEADERS = ("origin",)
BROWSER_HEADER_PREFIXES = ("sec-fetch-",)


def refusal_reason(peer: Optional[str], headers) -> Optional[str]:
    """Why a request may not use the approval routes, or None when it may.

    `headers` is any mapping of header names to values (names are compared
    case-insensitively)."""
    if peer not in LOOPBACK_PEERS:
        return "peer is not loopback"
    names = {str(name).lower(): str(value) for name, value in headers.items()}
    for name in names:
        if name in PROXY_HEADERS or name.startswith(REFUSED_HEADER_PREFIXES):
            return "proxied request"
        if name in BROWSER_HEADERS or name.startswith(BROWSER_HEADER_PREFIXES):
            return "browser request"
        if name in LIFEOS_RELAY_HEADERS:
            return "relayed LifeOS call"
    if "mozilla/" in names.get("user-agent", "").lower():
        return "browser request"
    return None


_APPROVALS_PREFIX = "/api/approvals"


def is_approval_path(endpoint: object) -> bool:
    """True when a schedule endpoint path would reach the approval routes,
    however it is spelled: percent-encoding, case, repeated slashes,
    backslashes and dot segments are all normalized first."""
    path = str(endpoint or "")
    for _ in range(5):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = "".join(ch for ch in path if unicodedata.category(ch)[0] != "C" and not ch.isspace())
    path = path.replace("\\", "/").lower()
    path = "/" + path.lstrip("/")
    path = posixpath.normpath(path)
    path = re.sub(r"/+", "/", path)
    return path == _APPROVALS_PREFIX or path.startswith(_APPROVALS_PREFIX + "/")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def clean_text(value: str, *, keep_newlines: bool) -> str:
    """Drop control and format characters (newlines kept when asked) and
    surrounding whitespace."""
    kept = []
    for ch in value:
        if ch == "\n" and keep_newlines:
            kept.append(ch)
        elif unicodedata.category(ch)[0] != "C":
            kept.append(ch)
    return "".join(kept).strip()


def callback_data(approve: bool, token: str) -> str:
    return f"{CALLBACK_PREFIX}:{'y' if approve else 'n'}:{token}"


def parse_callback_data(data: str) -> Optional[tuple[bool, str]]:
    match = _CALLBACK_RE.match(data or "")
    if not match:
        return None
    return match.group(1) == "y", match.group(2)


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _local_time(epoch: float) -> str:
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
        tz = timezone.utc
    return datetime.fromtimestamp(epoch, tz=tz).strftime("%b %d, %I:%M %p %Z")


def request_text(summary: str, source: Optional[str], request_hash: str, expires_at: float) -> str:
    """The Telegram message: the exact summary the buttons decide, where the
    request came from, and what identifies it."""
    lines = ["Chief of Staff asks:", "", summary, ""]
    if source:
        lines.append(f"From: {source}")
    lines.append(f"Request: {request_hash[:12]}")
    lines.append(f"Expires: {_local_time(expires_at)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def get_db_path() -> str:
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "external_approvals.db")


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS external_approvals ("
    " token TEXT PRIMARY KEY,"
    " request_hash TEXT NOT NULL,"
    " summary TEXT NOT NULL,"
    " source TEXT,"
    " message_id INTEGER,"
    " state TEXT NOT NULL,"
    " created_at REAL NOT NULL,"
    " expires_at REAL NOT NULL,"
    " decided_at REAL"
    ")"
)


def _log(event: str, token: str, request_hash: str, outcome: str, at: float) -> None:
    logger.info(
        "external approval %s token=%s request_hash=%s outcome=%s at=%s",
        event, token, request_hash, outcome, iso_utc(at),
    )


class ExternalApprovalStore:
    """SQLite-backed external approval requests."""

    def __init__(self, db_path: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.db_path = db_path or get_db_path()
        self.clock = clock
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)

    def _connect(self):
        return connect_closing(self.db_path, timeout=10.0)

    def open(self, request_hash: str, summary: str, source: Optional[str], ttl_seconds: int) -> dict:
        """Store a pending request with no message yet and return it."""
        now = self.clock()
        token = secrets.token_hex(8)
        expires_at = now + ttl_seconds
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM external_approvals WHERE expires_at < ?", (now - RETENTION_SECONDS,),
            )
            conn.execute(
                "INSERT INTO external_approvals (token, request_hash, summary, source, message_id,"
                " state, created_at, expires_at, decided_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
                (token, request_hash, summary, source, STATE_PENDING, now, expires_at),
            )
        _log("open", token, request_hash, STATE_PENDING, now)
        return {"token": token, "request_hash": request_hash, "expires_at": expires_at}

    def set_message_id(self, token: str, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE external_approvals SET message_id = ? WHERE token = ? AND message_id IS NULL",
                (int(message_id), token),
            )

    def discard(self, token: str, request_hash: str, reason: str) -> None:
        """Delete a request whose message could not be sent."""
        with self._connect() as conn:
            conn.execute("DELETE FROM external_approvals WHERE token = ?", (token,))
        _log("discard", token, request_hash, reason, self.clock())

    def _row(self, conn, token: str) -> Optional[sqlite3.Row]:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT token, request_hash, source, message_id, state, expires_at"
            " FROM external_approvals WHERE token = ?",
            (token,),
        ).fetchone()

    def get(self, token: str) -> Optional[dict]:
        """The request's state as the caller sees it; a pending request past
        its expiry reads as expired."""
        if not TOKEN_RE.match(token or ""):
            return None
        with self._connect() as conn:
            row = self._row(conn, token)
        if row is None:
            return None
        state = row["state"]
        if state == STATE_PENDING and self.clock() >= row["expires_at"]:
            state = STATE_EXPIRED
        return {
            "state": state,
            "request_hash": row["request_hash"],
            "expires_at": iso_utc(row["expires_at"]),
            "source": row["source"],
        }

    def decide(self, token: str, *, approve: bool, message_id: int) -> str:
        """Record the operator's press and return the answer to show.

        Counts once, only on the message that carried the buttons, and only
        while the request is pending and unexpired. A press after expiry
        marks the request expired."""
        now = self.clock()
        with self._connect() as conn:
            row = self._row(conn, token)
            if row is None:
                outcome, request_hash, event = OUTCOME_LOST, "", "lost"
            elif row["message_id"] is None or int(row["message_id"]) != int(message_id):
                outcome, request_hash, event = OUTCOME_WRONG_MESSAGE, row["request_hash"], "refused"
            elif row["state"] != STATE_PENDING:
                outcome = OUTCOME_EXPIRED if row["state"] == STATE_EXPIRED else OUTCOME_ALREADY_DECIDED
                request_hash, event = row["request_hash"], "refused"
            else:
                request_hash = row["request_hash"]
                expired = now >= row["expires_at"]
                if expired:
                    state, outcome, event = STATE_EXPIRED, OUTCOME_EXPIRED, "expired"
                    window = "expires_at <= ?"
                else:
                    state = STATE_APPROVED if approve else STATE_DENIED
                    outcome = OUTCOME_APPROVED if approve else OUTCOME_DENIED
                    event, window = "decision", "expires_at > ?"
                updated = conn.execute(
                    "UPDATE external_approvals SET state = ?, decided_at = ?"
                    " WHERE token = ? AND state = 'pending' AND message_id = ? AND " + window,
                    (state, now, token, int(message_id), now),
                ).rowcount
                if updated != 1:
                    outcome, event = OUTCOME_ALREADY_DECIDED, "refused"
        _log(event, token, request_hash, outcome, now)
        return outcome


_store: Optional[ExternalApprovalStore] = None
_store_lock = threading.Lock()


def get_store() -> ExternalApprovalStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ExternalApprovalStore()
        return _store


def reset_store(store: Optional[ExternalApprovalStore] = None) -> None:
    """Replace the process-wide store (tests pass one on a temporary file)."""
    global _store
    with _store_lock:
        _store = store
