"""
Conversation Store for LifeOS.

Manages persistent conversation threads using SQLite.
"""
import sqlite3
import json
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


def get_conversation_db_path() -> str:
    """Get the path to the conversations database."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "conversations.db")


@dataclass
class Conversation:
    """A conversation thread."""
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    persona_id: str = "primary"
    # Agent-worker session this web/voice conversation spawned, if any (#403).
    # Set when an orchestrating persona (e.g. doctor) is selected and the turn
    # spawns a background Claude Code session. Lets the conversation answer that
    # session's [CLARIFY]/[GOAL] without a Telegram message id. NULL for normal
    # inline conversations.
    agent_session_id: Optional[str] = None
    # Text backend the conversation is tagged with, for sidebar filtering
    # (#596). "lifeos" is the native default; an orchestrating-persona turn
    # sent while Hermes is selected tags its (LifeOS-native) conversation
    # "hermes" instead, so it doesn't vanish from the thread list the user
    # started it in. Purely a label — never used to route a turn.
    backend: str = "lifeos"


@dataclass
class Message:
    """A message in a conversation."""
    id: str
    conversation_id: str
    role: str  # "user" or "assistant"
    content: str
    created_at: datetime
    sources: Optional[list] = None
    routing: Optional[dict] = None


class ConversationStore:
    """
    SQLite-backed conversation storage.

    Manages conversation threads and messages with full persistence.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize conversation store.

        Args:
            db_path: Path to SQLite database (default from settings)
        """
        self.db_path = db_path or get_conversation_db_path()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with a generous busy timeout.

        This PR adds the agent worker as a SECOND concurrent writer to
        conversations.db (it mirrors a spawned session's output into the linked
        thread, #311) alongside the web /chat process. Under WAL two writers can
        still collide on the single write lock; without a busy timeout the loser
        gets an immediate "database is locked", and the mirror's best-effort
        try/except would silently drop the message. A 10s busy timeout (matching
        SessionStore._connect) lets the loser wait out the brief write instead.
        `sqlite3.connect`'s `timeout=` sets exactly this at the driver level —
        Python's default is 5s, so this widens it and makes the intent explicit.
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self):
        """Create database tables if they don't exist."""
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    persona_id TEXT NOT NULL DEFAULT 'primary',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Additive migration for pre-existing databases (#351): tag each
            # conversation with the persona that owns it. Existing rows backfill
            # to 'primary' so web/Telegram history is unaffected.
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(conversations)")
            }
            if "persona_id" not in existing_cols:
                conn.execute(
                    "ALTER TABLE conversations "
                    "ADD COLUMN persona_id TEXT NOT NULL DEFAULT 'primary'"
                )
            # Additive migration for the spawned agent-worker session link
            # (#403). Existing rows backfill to NULL (no spawned session).
            if "agent_session_id" not in existing_cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN agent_session_id TEXT"
                )
            # Additive migration for the backend tag (#596). Existing rows
            # backfill to 'lifeos' so pre-existing history is unaffected.
            if "backend" not in existing_cols:
                conn.execute(
                    "ALTER TABLE conversations "
                    "ADD COLUMN backend TEXT NOT NULL DEFAULT 'lifeos'"
                )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources TEXT,
                    routing TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                        ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, created_at)
            """)
            conn.commit()
        finally:
            conn.close()

    def create_conversation(
        self,
        title: Optional[str] = None,
        persona_id: str = "primary",
        backend: str = "lifeos",
        conv_id: Optional[str] = None,
    ) -> Conversation:
        """
        Create a new conversation.

        Args:
            title: Optional title (default "New Conversation")
            persona_id: Persona that owns the thread (default "primary")
            backend: Text backend to tag the thread with (default "lifeos"),
                purely a sidebar-filtering label (#596) — never used to route.
            conv_id: Optional caller-supplied id, used verbatim when given
                (#592 — the Hermes proxy adopts the id its upstream backend
                already minted for the thread). Omitted (the default), a
                uuid4 is minted exactly as before. If the id already exists,
                the existing conversation is returned rather than raising or
                duplicating the row — the proxy calls this on every turn of a
                thread it already created, not just the first.

        Returns:
            Created conversation (or the existing one, if conv_id collided)
        """
        new_id = conv_id or str(uuid.uuid4())
        title = title or "New Conversation"
        now = datetime.now()

        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO conversations (id, title, persona_id, backend, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_id, title, persona_id, backend, now, now)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            existing = self.get_conversation(new_id)
            if existing is None:
                raise
            return existing
        finally:
            conn.close()

        return Conversation(
            id=new_id,
            title=title,
            created_at=now,
            updated_at=now,
            message_count=0,
            persona_id=persona_id,
            backend=backend,
        )

    def get_conversation(self, conv_id: str) -> Optional[Conversation]:
        """
        Get a conversation by ID.

        Args:
            conv_id: Conversation ID

        Returns:
            Conversation or None if not found
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) as message_count, c.persona_id,
                       c.agent_session_id, c.backend
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.id = ?
                GROUP BY c.id
                """,
                (conv_id,)
            )
            row = cursor.fetchone()

            if not row:
                return None

            return Conversation(
                id=row[0],
                title=row[1],
                created_at=datetime.fromisoformat(row[2]) if isinstance(row[2], str) else row[2],
                updated_at=datetime.fromisoformat(row[3]) if isinstance(row[3], str) else row[3],
                message_count=row[4],
                persona_id=row[5] or "primary",
                agent_session_id=row[6],
                backend=row[7] or "lifeos",
            )
        finally:
            conn.close()

    def list_conversations(
        self,
        limit: int = 50,
        persona_id: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> list[Conversation]:
        """
        List conversations sorted by updated_at desc.

        Args:
            limit: Maximum number of conversations to return
            persona_id: When set, return only threads owned by that persona.
                When None, return all personas' threads.
            backend: When set, return only threads tagged with that backend
                (#596). When None, return threads from every backend —
                preserving today's unfiltered behavior for existing callers.

        Returns:
            List of conversations
        """
        clauses = []
        params: list = []
        if persona_id is not None:
            clauses.append("c.persona_id = ?")
            params.append(persona_id)
        if backend is not None:
            clauses.append("c.backend = ?")
            params.append(backend)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        conn = self._connect()
        try:
            cursor = conn.execute(
                f"""
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) as message_count, c.persona_id, c.backend
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                {where}
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                params
            )

            conversations = []
            for row in cursor.fetchall():
                conversations.append(Conversation(
                    id=row[0],
                    title=row[1],
                    created_at=datetime.fromisoformat(row[2]) if isinstance(row[2], str) else row[2],
                    updated_at=datetime.fromisoformat(row[3]) if isinstance(row[3], str) else row[3],
                    message_count=row[4],
                    persona_id=row[5] or "primary",
                    backend=row[6] or "lifeos",
                ))

            return conversations
        finally:
            conn.close()

    def delete_conversation(self, conv_id: str) -> bool:
        """
        Delete a conversation and all its messages.

        Args:
            conv_id: Conversation ID

        Returns:
            True if deleted, False if not found
        """
        conn = self._connect()
        try:
            # Check if exists
            cursor = conn.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conv_id,)
            )
            if not cursor.fetchone():
                return False

            # Delete messages first (FK constraint)
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (conv_id,)
            )
            conn.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conv_id,)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        sources: Optional[list] = None,
        routing: Optional[dict] = None
    ) -> Message:
        """
        Add a message to a conversation.

        Args:
            conv_id: Conversation ID
            role: "user" or "assistant"
            content: Message content
            sources: Optional list of source documents
            routing: Optional routing metadata

        Returns:
            Created message
        """
        msg_id = str(uuid.uuid4())
        now = datetime.now()

        sources_json = json.dumps(sources) if sources else None
        routing_json = json.dumps(routing) if routing else None

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO messages (id, conversation_id, role, content, sources, routing, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (msg_id, conv_id, role, content, sources_json, routing_json, now)
            )
            # Update conversation's updated_at
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conv_id)
            )
            conn.commit()
        finally:
            conn.close()

        return Message(
            id=msg_id,
            conversation_id=conv_id,
            role=role,
            content=content,
            created_at=now,
            sources=sources,
            routing=routing
        )

    def get_messages(
        self,
        conv_id: str,
        limit: Optional[int] = None
    ) -> list[Message]:
        """
        Get messages for a conversation.

        Args:
            conv_id: Conversation ID
            limit: Optional limit (returns last N messages)

        Returns:
            List of messages in chronological order

        `created_at` alone doesn't guarantee order: a user message and its
        assistant reply are two separate `add_message()` calls, and
        `datetime.now()` can tie between them (#592 review). `rowid` (the
        table's implicit insertion-order column) breaks that tie, since it
        reflects the order rows were actually written rather than their
        clock reading.
        """
        conn = self._connect()
        try:
            if limit:
                # Get last N messages
                cursor = conn.execute(
                    """
                    SELECT id, conversation_id, role, content, sources, routing, created_at
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (conv_id, limit)
                )
                rows = cursor.fetchall()
                rows.reverse()  # Return in chronological order
            else:
                cursor = conn.execute(
                    """
                    SELECT id, conversation_id, role, content, sources, routing, created_at
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (conv_id,)
                )
                rows = cursor.fetchall()

            messages = []
            for row in rows:
                messages.append(Message(
                    id=row[0],
                    conversation_id=row[1],
                    role=row[2],
                    content=row[3],
                    sources=json.loads(row[4]) if row[4] else None,
                    routing=json.loads(row[5]) if row[5] else None,
                    created_at=datetime.fromisoformat(row[6]) if isinstance(row[6], str) else row[6]
                ))

            return messages
        finally:
            conn.close()

    def update_title(self, conv_id: str, title: str) -> bool:
        """
        Update conversation title.

        Args:
            conv_id: Conversation ID
            title: New title

        Returns:
            True if updated, False if not found
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, datetime.now(), conv_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def set_agent_session_id(self, conv_id: str, session_id: Optional[str]) -> bool:
        """Link a conversation to the agent-worker session it spawned (#403).

        Set after an orchestrating persona spawns a background Claude Code
        session, so the conversation can later answer that session's
        [CLARIFY]/[GOAL] via the session-keyed deposit path. Idempotently
        overwrites any prior link (a fresh spawn supersedes). Returns True if
        the conversation row was updated.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE conversations SET agent_session_id = ?, updated_at = ? "
                "WHERE id = ?",
                (session_id, datetime.now(), conv_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_conversation_id_by_agent_session_id(self, session_id: str) -> Optional[str]:
        """Reverse of set_agent_session_id: the conversation linked to a spawned
        session, or None (#311).

        The agent worker runs out-of-process and only knows a session_id; this
        lets it resolve the web/voice conversation thread that spawned the
        session so its progress + result can be mirrored back there. Returns
        None for Telegram-origin sessions (never linked to a conversation),
        which keeps the mirror a no-op for them.
        """
        if not session_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM conversations WHERE agent_session_id = ? LIMIT 1",
                (session_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()


def generate_title(question: str, max_length: int = 50) -> str:
    """
    Generate a conversation title from the first question.

    Args:
        question: The user's first question
        max_length: Maximum title length

    Returns:
        Generated title
    """
    # Clean up the question
    title = question.strip()

    # Remove question marks for cleaner titles
    title = title.rstrip('?')

    if len(title) <= max_length:
        return title

    # Truncate at word boundary
    truncated = title[:max_length]
    last_space = truncated.rfind(' ')
    if last_space > max_length // 2:
        truncated = truncated[:last_space]

    return truncated.strip()


def format_conversation_history(
    messages: list[Message],
    max_tokens: int = 2000
) -> str:
    """
    Format conversation history for inclusion in prompt.

    Args:
        messages: List of messages
        max_tokens: Maximum approximate tokens (chars / 4)

    Returns:
        Formatted conversation history string
    """
    if not messages:
        return ""

    max_chars = max_tokens * 4  # Rough approximation

    formatted_parts = []
    total_chars = 0

    for msg in messages:
        role_label = "User" if msg.role == "user" else "Assistant"
        formatted = f"{role_label}: {msg.content}"

        if total_chars + len(formatted) > max_chars:
            break

        formatted_parts.append(formatted)
        total_chars += len(formatted)

    return "\n\n".join(formatted_parts)


# Singleton store instance
_store_instance: Optional[ConversationStore] = None


def get_store() -> ConversationStore:
    """Get the singleton ConversationStore instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = ConversationStore()
    return _store_instance


def reset_conversation_store() -> None:
    """
    Reset the conversation store singleton.

    For testing only - allows tests to start with fresh state.
    """
    global _store_instance
    _store_instance = None
