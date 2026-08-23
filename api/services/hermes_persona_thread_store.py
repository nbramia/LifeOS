"""Reply-thread persona inheritance for Hermes-Telegram (#644 follow-up).

Threading a reply (Telegram's native reply-to) inherits the persona of the
message being replied to, without re-tagging. That needs a small amount of
state — a `(chat_id, message_id) -> persona_id` mapping — and it has to live
on the LifeOS side: putting it in Hermes would make persona resolution
two-sourced again, which the #644 AC forbids.

Design choices:
- **No message content, ever.** Only the id -> persona mapping is stored —
  these are personal Telegram messages and their text has no business in a
  routing table.
- **Scoped per chat.** Telegram message ids are unique only within a chat,
  so the primary key is `(chat_id, message_id)`, never a bare message id.
- **Bounded, not unbounded.** Rows expire after `_TTL_SECONDS` and the table
  is capped at `_MAX_ROWS`, oldest first — both enforced opportunistically on
  every `record()` rather than via a separate cron, since write volume here
  (one row per Hermes-Telegram turn) is low. An expired or evicted row is
  never an error for the reader: `lookup()` returns `None` exactly as it
  would for an id it never saw, and the caller (hermes_proxy.py) treats that
  as "no inheritance, fall through to no persona" — the same safe default a
  thread that predates this feature gets.
- **Persisted, not in-memory.** Unlike `SessionToolResultCache` (in-process,
  cache-shaped), this needs to survive an API restart — this host redeploys
  automatically within ~10 minutes of `main` advancing, and a Hermes-Telegram
  thread can easily go longer than that between replies. An in-memory table
  would silently reset the mapping on every deploy; sqlite (mirroring
  `UsageStore`/`ConversationStore`) doesn't.
- **Every resolved message is recorded, not just tagged ones.** Whether a
  turn's persona came from an explicit `@tag` or from inheriting its own
  reply-to, `hermes_proxy.py` records THIS message's id under that persona
  too — that's what makes a reply-to-a-reply chain inherit transitively
  without a special case: each link in the chain becomes a valid anchor for
  the next.
"""
import sqlite3
import time
from pathlib import Path
from typing import Optional

from config.settings import settings

# A Hermes-Telegram persona thread is a conversational thread, not a
# long-lived relationship — a week comfortably covers "picked back up a
# few days later" while still bounding the table for an install that's
# been running for months.
_TTL_SECONDS = 7 * 24 * 3600

# Cheap upper bound on table size regardless of traffic; oldest rows are
# evicted first once this is exceeded; generous by real Telegram-turn volume.
_MAX_ROWS = 20_000


class HermesPersonaThreadStore:
    """SQLite-backed `(chat_id, message_id) -> persona_id` mapping."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(settings.chroma_path).parent / "hermes_persona_threads.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persona_threads (
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_persona_threads_created_at
                ON persona_threads(created_at)
            """)

    def record(self, chat_id: str, message_id: str, persona_id: str) -> None:
        """Anchor `message_id` (within `chat_id`) to `persona_id`, so a later
        reply to it can inherit. Idempotent — re-recording the same id just
        refreshes its timestamp (extends its TTL), which matters for a
        message that gets threaded off more than once.

        Prunes expired and (if still over `_MAX_ROWS`) oldest rows on every
        call — see the module docstring for why this is opportunistic rather
        than a separate sweep.
        """
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO persona_threads (chat_id, message_id, persona_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (chat_id, message_id)
                DO UPDATE SET persona_id = excluded.persona_id, created_at = excluded.created_at
                """,
                (chat_id, message_id, persona_id, now),
            )
            conn.execute(
                "DELETE FROM persona_threads WHERE created_at < ?", (now - _TTL_SECONDS,),
            )
            (row_count,) = conn.execute("SELECT COUNT(*) FROM persona_threads").fetchone()
            if row_count > _MAX_ROWS:
                conn.execute(
                    """
                    DELETE FROM persona_threads WHERE rowid IN (
                        SELECT rowid FROM persona_threads
                        ORDER BY created_at ASC LIMIT ?
                    )
                    """,
                    (row_count - _MAX_ROWS,),
                )

    def lookup(self, chat_id: str, message_id: str) -> Optional[str]:
        """The persona anchored to `message_id` in `chat_id`, or `None` if
        it was never recorded, has expired, or belongs to a different chat.
        Never raises — an unknown id is exactly as valid a result as an
        expired one, both meaning "no inheritance" to the caller."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT persona_id, created_at FROM persona_threads WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        if row is None:
            return None
        persona_id, created_at = row
        if created_at < time.time() - _TTL_SECONDS:
            return None
        return persona_id


_persona_thread_store: Optional[HermesPersonaThreadStore] = None


def get_persona_thread_store() -> HermesPersonaThreadStore:
    global _persona_thread_store
    if _persona_thread_store is None:
        _persona_thread_store = HermesPersonaThreadStore()
    return _persona_thread_store
