"""
Conversation Store for LifeOS.

Manages persistent conversation threads using SQLite.
"""
import sqlite3
import json
import uuid
import logging
from dataclasses import dataclass, field
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

    def _init_db(self):
        """Create database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
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

    def create_conversation(self, title: Optional[str] = None) -> Conversation:
        """
        Create a new conversation.

        Args:
            title: Optional title (default "New Conversation")

        Returns:
            Created conversation
        """
        conv_id = str(uuid.uuid4())
        title = title or "New Conversation"
        now = datetime.now()

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conv_id, title, now, now)
            )
            conn.commit()
        finally:
            conn.close()

        return Conversation(
            id=conv_id,
            title=title,
            created_at=now,
            updated_at=now,
            message_count=0
        )

    def get_conversation(self, conv_id: str) -> Optional[Conversation]:
        """
        Get a conversation by ID.

        Args:
            conv_id: Conversation ID

        Returns:
            Conversation or None if not found
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) as message_count
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
                message_count=row[4]
            )
        finally:
            conn.close()

    def list_conversations(self, limit: int = 50) -> list[Conversation]:
        """
        List all conversations sorted by updated_at desc.

        Args:
            limit: Maximum number of conversations to return

        Returns:
            List of conversations
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) as message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (limit,)
            )

            conversations = []
            for row in cursor.fetchall():
                conversations.append(Conversation(
                    id=row[0],
                    title=row[1],
                    created_at=datetime.fromisoformat(row[2]) if isinstance(row[2], str) else row[2],
                    updated_at=datetime.fromisoformat(row[3]) if isinstance(row[3], str) else row[3],
                    message_count=row[4]
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
        conn = sqlite3.connect(self.db_path)
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

        conn = sqlite3.connect(self.db_path)
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
        """
        conn = sqlite3.connect(self.db_path)
        try:
            if limit:
                # Get last N messages
                cursor = conn.execute(
                    """
                    SELECT id, conversation_id, role, content, sources, routing, created_at
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at DESC
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
                    ORDER BY created_at ASC
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
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, datetime.now(), conv_id)
            )
            conn.commit()
            return cursor.rowcount > 0
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
