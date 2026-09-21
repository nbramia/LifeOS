"""Reply-thread anchors for task questions delivered on the Hermes channel.

A question the agent worker sends into the Hermes Telegram DM is answered by
replying to it. Telegram carries that as a `reply_to_message_id`, and Hermes
already forwards exactly that field to LifeOS for persona resolution — so
binding `(chat_id, message_id) -> question_id` at delivery time is all
LifeOS needs to route the reply back onto the right pending question.

The shape mirrors `HermesPersonaThreadStore`, and for the same reasons:

- **No question or answer text, ever.** Only the id binding is stored —
  these are personal messages and their content has no business in a routing
  table.
- **Scoped per chat.** Telegram message ids are unique only within a chat,
  so the primary key is `(chat_id, message_id)`.
- **Bounded, not unbounded.** Rows expire after `_TTL_SECONDS` and the table
  is capped at `_MAX_ROWS`, oldest first — both enforced opportunistically
  on every `record()`, since write volume here (one row per question) is
  low. An expired or evicted row is never an error for the reader:
  `lookup()` returns `None` exactly as it would for an id it never saw, and
  the caller treats that as "no anchor, nothing to deposit".
- **Persisted, not in-memory.** A blocked task waits days for an answer,
  and the API restarts far more often than that.

The TTL comfortably exceeds `agent_clarification_timeout_hours`, so the
question a row points at is already closed by the time the row expires.
"""
import time
from pathlib import Path
from typing import Optional

from api.services.sqlite_connect import connect_closing
from config.settings import settings

_TTL_SECONDS = 7 * 24 * 3600

_MAX_ROWS = 20_000


class HermesQuestionThreadStore:
    """SQLite-backed `(chat_id, message_id) -> question_id` mapping."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(settings.chroma_path).parent / "hermes_question_threads.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with connect_closing(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS question_threads (
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    question_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_question_threads_created_at
                ON question_threads(created_at)
            """)

    def record(self, chat_id: str, message_id: str, question_id: int) -> None:
        """Anchor `message_id` (within `chat_id`) to `question_id`, so a
        reply to it can be routed onto that question. Idempotent — re-
        recording the same id rebinds it and refreshes its timestamp.

        Prunes expired and (if still over `_MAX_ROWS`) oldest rows on every
        call — see the module docstring for why this is opportunistic.
        """
        now = time.time()
        with connect_closing(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO question_threads (chat_id, message_id, question_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (chat_id, message_id)
                DO UPDATE SET question_id = excluded.question_id, created_at = excluded.created_at
                """,
                (chat_id, message_id, int(question_id), now),
            )
            conn.execute(
                "DELETE FROM question_threads WHERE created_at < ?", (now - _TTL_SECONDS,),
            )
            (row_count,) = conn.execute("SELECT COUNT(*) FROM question_threads").fetchone()
            if row_count > _MAX_ROWS:
                conn.execute(
                    """
                    DELETE FROM question_threads WHERE rowid IN (
                        SELECT rowid FROM question_threads
                        ORDER BY created_at ASC LIMIT ?
                    )
                    """,
                    (row_count - _MAX_ROWS,),
                )

    def lookup(self, chat_id: str, message_id: str) -> Optional[int]:
        """The question anchored to `message_id` in `chat_id`, or `None` if
        it was never recorded, has expired, or belongs to a different chat.
        Never raises — an unknown id is exactly as valid a result as an
        expired one, both meaning "no anchor" to the caller."""
        with connect_closing(self.db_path) as conn:
            row = conn.execute(
                "SELECT question_id, created_at FROM question_threads "
                "WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        if row is None:
            return None
        question_id, created_at = row
        if created_at < time.time() - _TTL_SECONDS:
            return None
        return int(question_id)


_question_thread_store: Optional[HermesQuestionThreadStore] = None


def get_question_thread_store() -> HermesQuestionThreadStore:
    global _question_thread_store
    if _question_thread_store is None:
        _question_thread_store = HermesQuestionThreadStore()
    return _question_thread_store
