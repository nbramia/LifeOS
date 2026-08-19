"""
Tracks Gmail drafts created by LifeOS so send requests can be gated.

The Gmail send safety guarantee must hold for every caller that reaches
``GmailService.send_draft()`` — the HTTP `/api/gmail/send` route, the MCP
surface (which rides the same route), and the in-process agent-loop tool.
Gmail itself does not mark which drafts were created by LifeOS, so this
sidecar database keeps only the minimum bookkeeping needed for the send
gate: account, draft id, creation time, and the optional turn id supplied by
a cooperating caller. ``check_send_gate()`` below is the single choke point
all three callers go through before a send is allowed to reach Gmail.
"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

SEND_CONFIRMATION_MESSAGE = (
    "Draft was created by LifeOS too recently to send safely. Show the draft "
    "to the user, obtain explicit confirmation, and retry later."
)
SEND_LEDGER_UNAVAILABLE_MESSAGE = (
    "Gmail draft safety ledger is unavailable, so the draft was not sent. "
    "Show the draft to the user, obtain explicit confirmation, and retry later."
)


@dataclass(frozen=True)
class DraftLedgerEntry:
    """A LifeOS-created Gmail draft recorded for send-gate checks."""

    account: str
    draft_id: str
    created_at: datetime
    turn_id: Optional[str] = None


class GmailSendGateBlocked(Exception):
    """Raised by check_send_gate() when a send must be refused.

    `unavailable=True` marks a ledger failure (read error, or a ledger that
    was just recreated after apparently losing its data) — the fail-closed
    case — as distinct from an ordinary cooldown/turn-id match.
    """

    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.message = message
        self.unavailable = unavailable


def get_gmail_draft_ledger_path() -> str:
    """Path to the draft ledger database, creating its parent directory."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "gmail_draft_ledger.db")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    return _ensure_aware_utc(datetime.fromisoformat(value))


def _inside_cooldown(entry: DraftLedgerEntry, window_seconds: int) -> bool:
    if window_seconds <= 0:
        return False
    age = _utc_now() - entry.created_at
    return age.total_seconds() < window_seconds


class GmailDraftLedger:
    """Sidecar store of LifeOS-created Gmail drafts."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_gmail_draft_ledger_path()
        db_path_obj = Path(self.db_path)
        marker_path = db_path_obj.with_name(db_path_obj.name + ".initialized")
        db_file_existed = db_path_obj.exists()
        marker_existed = marker_path.exists()

        self._init_db()

        if db_file_existed or not marker_existed:
            # Either the database was already there (an ordinary restart with
            # intact data), or there is no evidence it ever existed before (a
            # genuine first run on a fresh install) — nothing was lost, so
            # behave normally from the start. Write the marker so a *future*
            # restart can tell the difference if the .db file alone goes
            # missing later.
            self.freshly_initialized_at: Optional[datetime] = None
            marker_path.touch(exist_ok=True)
        else:
            # The marker survived in this directory but the database file did
            # not — something deleted the ledger's data out from under an
            # existing deployment (the whole data directory vanishing looks
            # like a fresh install instead, which is the correct call: nothing
            # else survived either, so there is nothing to distrust). Refuse
            # "unknown draft" lookups for one cooling-off window; see
            # is_within_fresh_grace_period().
            self.freshly_initialized_at = _utc_now()
            logger.error(
                "Gmail draft ledger database is missing at %s but its marker "
                "file survived — treating as data loss and failing closed "
                "for one cooling-off window.",
                self.db_path,
            )
        self.marker_path = marker_path

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gmail_drafts (
                    account TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    turn_id TEXT,
                    PRIMARY KEY (account, draft_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_gmail_drafts_created_at
                ON gmail_drafts(created_at)
            """)
            conn.commit()
        finally:
            conn.close()

    def is_within_fresh_grace_period(
        self,
        *,
        window_seconds: int,
        now: Optional[datetime] = None,
    ) -> bool:
        """True if this ledger was just recreated after apparent data loss and
        is still inside the grace period during which an "unknown" draft
        cannot be trusted to mean "not LifeOS-created" — it may just mean the
        ledger's memory of it was lost. Bounded to `window_seconds` (the same
        cooldown window used for sends): any draft that could have been
        silently lost was created before the ledger was recreated, so once one
        full cooldown window has elapsed, it would no longer be blocked even
        if it had been tracked perfectly.
        """
        if self.freshly_initialized_at is None:
            return False
        age = _ensure_aware_utc(now or _utc_now()) - self.freshly_initialized_at
        return age.total_seconds() < window_seconds

    def record_created(
        self,
        account: str,
        draft_id: str,
        *,
        created_at: Optional[datetime] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        """Record or replace a LifeOS-created draft."""
        created = _ensure_aware_utc(created_at or _utc_now()).isoformat()
        normalized_turn_id = (turn_id or "").strip() or None
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO gmail_drafts "
                "(account, draft_id, created_at, turn_id) VALUES (?, ?, ?, ?)",
                (account, draft_id, created, normalized_turn_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_entry(self, account: str, draft_id: str) -> Optional[DraftLedgerEntry]:
        """Return the recorded draft entry, if LifeOS created this draft."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT account, draft_id, created_at, turn_id "
                "FROM gmail_drafts WHERE account = ? AND draft_id = ?",
                (account, draft_id),
            )
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return None
        return DraftLedgerEntry(
            account=row[0],
            draft_id=row[1],
            created_at=_parse_datetime(row[2]),
            turn_id=row[3],
        )

    def prune(
        self,
        *,
        window_seconds: int,
        now: Optional[datetime] = None,
        max_turn_tagged_rows: Optional[int] = None,
    ) -> int:
        """Bound the ledger's growth without breaking the turn-id guarantee.

        Untagged entries are dropped once older than the cooling-off window —
        they only ever matter for the time-based fallback check, so once that
        window has passed they can never block anything again.

        Turn-tagged entries back a *different* guarantee: "a send carrying the
        same turn id as creation is refused regardless of elapsed time" (#588).
        A time-based cutoff would delete exactly the rows that promise applies
        to once they age past the cooldown, silently reopening the same-turn
        bypass the gate exists to close. So turn-tagged rows are exempt from
        the time cutoff and instead bounded by count: only the newest
        `max_turn_tagged_rows` are kept, oldest first evicted. That still
        guarantees the table can't grow without bound, without ever letting a
        row expire out from under the guarantee it's for.
        """
        max_rows = (
            max_turn_tagged_rows
            if max_turn_tagged_rows is not None
            else settings.gmail_draft_ledger_max_turn_tagged_rows
        )
        cutoff = (
            _ensure_aware_utc(now or _utc_now()) - timedelta(seconds=window_seconds)
        ).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "DELETE FROM gmail_drafts WHERE created_at < ? AND turn_id IS NULL",
                (cutoff,),
            )
            deleted = cursor.rowcount

            (turn_tagged_count,) = conn.execute(
                "SELECT COUNT(*) FROM gmail_drafts WHERE turn_id IS NOT NULL"
            ).fetchone()
            overflow = turn_tagged_count - max_rows
            if overflow > 0:
                cursor = conn.execute(
                    "DELETE FROM gmail_drafts WHERE rowid IN ("
                    "  SELECT rowid FROM gmail_drafts WHERE turn_id IS NOT NULL "
                    "  ORDER BY created_at ASC LIMIT ?"
                    ")",
                    (overflow,),
                )
                deleted += cursor.rowcount

            conn.commit()
            return deleted
        finally:
            conn.close()


_draft_ledger: Optional[GmailDraftLedger] = None


def get_gmail_draft_ledger() -> GmailDraftLedger:
    """Get or create the singleton Gmail draft ledger."""
    global _draft_ledger
    if _draft_ledger is None:
        _draft_ledger = GmailDraftLedger()
    return _draft_ledger


def check_send_gate(
    account: str,
    draft_id: str,
    turn_id: Optional[str],
) -> None:
    """Raise GmailSendGateBlocked if this draft may not be sent right now.

    This is the single shared choke point in front of
    ``GmailService.send_draft()``: the HTTP `/api/gmail/send` route and the
    in-process `send_email_draft` agent tool both call it before reaching
    Gmail, so the guarantee holds for every caller rather than only the one
    that happens to track its own turns in memory.
    """
    ledger = get_gmail_draft_ledger()
    try:
        entry = ledger.get_entry(account, draft_id)
        ledger_lost = entry is None and ledger.is_within_fresh_grace_period(
            window_seconds=settings.gmail_draft_send_cooldown_seconds,
        )
    except Exception as e:
        raise GmailSendGateBlocked(
            SEND_LEDGER_UNAVAILABLE_MESSAGE, unavailable=True
        ) from e

    if ledger_lost:
        raise GmailSendGateBlocked(SEND_LEDGER_UNAVAILABLE_MESSAGE, unavailable=True)

    if entry is None:
        return  # Not a LifeOS-created draft (e.g. hand-composed in Gmail).

    if entry.turn_id and turn_id:
        if entry.turn_id == turn_id:
            raise GmailSendGateBlocked(SEND_CONFIRMATION_MESSAGE)
        return  # A different turn id is an explicit, unconditional go-ahead.

    if _inside_cooldown(entry, settings.gmail_draft_send_cooldown_seconds):
        raise GmailSendGateBlocked(SEND_CONFIRMATION_MESSAGE)
