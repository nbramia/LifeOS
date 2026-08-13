"""
Remembers Gmail messages that were fetched and deliberately discarded.

The Gmail sync decides a message is marketing only *after* fetching it, and a
discarded message never produces an interaction row. Since the sync's
"already seen" set is derived from the interactions table, promotional mail was
invisible to it and got re-fetched every night for the full 30-day window —
~13k messages on a busy personal account, dominating the nightly run (#552).

This is a sidecar sync-state database, following the pattern already used by
``data/slack_sync_timestamps.db`` and ``data/imessage.db``'s ``sync_state``:
bookkeeping about what a sync has done, kept out of the main data model.

Caching a skip decision means it is not re-evaluated while the entry lives. If
the marketing rules in ``config/marketing_patterns`` change and you want them
applied to already-skipped mail, delete the database file — the next run
re-fetches and re-evaluates everything in the window.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Entries are pruned well past any realistic sync window (the nightly run looks
# back 30 days), so a message ages out of the query before it ages out of here.
DEFAULT_RETENTION_DAYS = 90


def get_gmail_skip_cache_path() -> str:
    """Path to the skip-cache database, creating its parent directory."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "gmail_skip_cache.db")


class GmailSkipCache:
    """Per-account record of message IDs already judged not worth keeping."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_gmail_skip_cache_path()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            # Keyed by (account, message_id): Gmail ids are only unique within a
            # mailbox, and the same sync process handles several accounts.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skipped_messages (
                    account TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    skipped_at TEXT NOT NULL,
                    reason TEXT,
                    PRIMARY KEY (account, message_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_skipped_at
                ON skipped_messages(skipped_at)
            """)
            conn.commit()
        finally:
            conn.close()

    def get_skipped_ids(self, account: str) -> set[str]:
        """Every message id previously skipped for this account."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT message_id FROM skipped_messages WHERE account = ?",
                (account,),
            )
            return {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()

    def record_skipped(
        self,
        account: str,
        message_ids: Iterable[str],
        reason: str = "marketing",
    ) -> int:
        """
        Remember that these messages were fetched and discarded.

        Returns the number of ids written. Re-recording an existing id is a
        no-op rather than an error, so a partial run is safe to repeat.
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = [(account, message_id, now, reason) for message_id in message_ids]
        if not rows:
            return 0

        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO skipped_messages "
                "(account, message_id, skipped_at, reason) VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return len(rows)

    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """
        Drop entries older than the retention window.

        Without this the cache grows without bound, and an entry is useless once
        the message has fallen out of the sync's look-back window.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "DELETE FROM skipped_messages WHERE skipped_at < ?", (cutoff,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


_skip_cache: Optional[GmailSkipCache] = None


def get_gmail_skip_cache() -> GmailSkipCache:
    """Get or create the singleton skip cache."""
    global _skip_cache
    if _skip_cache is None:
        _skip_cache = GmailSkipCache()
    return _skip_cache
