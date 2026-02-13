"""
iMessage integration for LifeOS.

Exports iMessage history from macOS Messages database to a local SQLite database,
with support for incremental sync and entity resolution by phone number.

Privacy note: This reads from ~/Library/Messages/chat.db which requires
Full Disk Access permission in System Preferences.
"""
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from api.services.phone_utils import normalize_phone

logger = logging.getLogger(__name__)

# Apple's epoch starts at 2001-01-01 00:00:00 UTC
# Dates are stored in nanoseconds
APPLE_EPOCH_OFFSET = 978307200  # Seconds from Unix epoch to Apple epoch
NANOS_PER_SECOND = 1_000_000_000


def apple_timestamp_to_datetime(apple_ts: int) -> Optional[datetime]:
    """
    Convert Apple timestamp (nanoseconds since 2001-01-01) to datetime.

    Args:
        apple_ts: Apple timestamp in nanoseconds

    Returns:
        UTC datetime or None if invalid
    """
    if not apple_ts:
        return None
    try:
        # Convert nanoseconds to seconds, then add Apple epoch offset
        unix_ts = (apple_ts / NANOS_PER_SECOND) + APPLE_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def datetime_to_apple_timestamp(dt: datetime) -> int:
    """
    Convert datetime to Apple timestamp (nanoseconds since 2001-01-01).

    Args:
        dt: datetime to convert

    Returns:
        Apple timestamp in nanoseconds
    """
    unix_ts = dt.timestamp()
    apple_seconds = unix_ts - APPLE_EPOCH_OFFSET
    return int(apple_seconds * NANOS_PER_SECOND)


def extract_text_from_attributed_body(blob: bytes) -> Optional[str]:
    """
    Extract text content from NSAttributedString binary data.

    Newer iOS/macOS versions store message text in the attributedBody column
    as a binary NSKeyedArchiver/typedstream format instead of the text column.

    Args:
        blob: Binary attributedBody data

    Returns:
        Extracted text content, or None if extraction fails
    """
    if not blob:
        return None

    try:
        # Decode as UTF-8, ignoring invalid bytes
        decoded = blob.decode("utf-8", errors="ignore")

        # Find sequences of printable characters (2+ chars)
        matches = re.findall(r"[\x20-\x7e\u00a0-\uffff]{2,}", decoded)

        # Filter out common metadata/class names
        skip_patterns = {
            "NSMutableAttributedString",
            "NSAttributedString",
            "NSString",
            "NSDictionary",
            "NSNumber",
            "NSArray",
            "NSObject",
            "NSValue",
            "NSFont",
            "NSParagraphStyle",
            "NSColor",
            "NSMutableParagraphStyle",
            "__kIMMessagePartAttributeName",
            "__kIMDataDetectedAttributeName",
            "__kIMFileTransferGUIDAttributeName",
            "__kIMFilenameAttributeName",
            "__kIMInlineMediaHeightAttributeName",
            "__kIMInlineMediaWidthAttributeName",
            "__kIMBaseWritingDirectionAttributeName",
            "streamtyped",
            "$class",
            "$classes",
            "$classname",
        }

        # Find best candidate (longest non-metadata string)
        candidates = [
            m
            for m in matches
            if m not in skip_patterns
            and len(m) > 1
            and not m.startswith("$")
            and not m.startswith("__kIM")
            and not m.startswith("NS")
        ]

        if candidates:
            # Return the longest candidate that looks like actual message content
            return max(candidates, key=len)

        return None
    except Exception:
        return None


@dataclass
class IMessageRecord:
    """Represents a single iMessage/SMS message."""

    rowid: int  # Original ROWID from chat.db (for incremental sync)
    text: str
    timestamp: datetime
    is_from_me: bool
    handle: str  # Phone number or email (raw from iMessage)
    handle_normalized: Optional[str]  # E.164 phone number if applicable
    service: str  # "iMessage", "SMS", "RCS"
    person_entity_id: Optional[str] = None  # Joined PersonEntity ID


class IMessageStore:
    """
    Storage for exported iMessage data.

    Provides:
    - Export from macOS Messages database
    - Incremental sync tracking
    - Query by phone number / person entity
    """

    # Source database path (macOS Messages)
    SOURCE_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

    def __init__(self, storage_path: str = "./data/imessage.db"):
        """
        Initialize the iMessage store.

        Args:
            storage_path: Path to local SQLite database for exports
        """
        self.storage_path = Path(storage_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the local database schema if it doesn't exist."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.storage_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    rowid INTEGER PRIMARY KEY,
                    text TEXT,
                    timestamp TEXT NOT NULL,
                    is_from_me INTEGER NOT NULL,
                    handle TEXT NOT NULL,
                    handle_normalized TEXT,
                    service TEXT NOT NULL,
                    person_entity_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_messages_handle_normalized
                    ON messages(handle_normalized);

                CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                    ON messages(timestamp);

                CREATE INDEX IF NOT EXISTS idx_messages_person_entity_id
                    ON messages(person_entity_id);

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

    def _get_last_synced_rowid(self) -> int:
        """Get the last synced ROWID for incremental sync."""
        with sqlite3.connect(self.storage_path) as conn:
            cursor = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'last_rowid'"
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def _set_last_synced_rowid(self, rowid: int) -> None:
        """Update the last synced ROWID."""
        with sqlite3.connect(self.storage_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_state (key, value)
                VALUES ('last_rowid', ?)
                """,
                (str(rowid),),
            )

    def export_from_source(
        self,
        limit: Optional[int] = None,
        full_resync: bool = False,
    ) -> dict:
        """
        Export messages from macOS Messages database.

        Args:
            limit: Maximum number of messages to export (for testing)
            full_resync: If True, clear and re-export all data

        Returns:
            Dict with export statistics
        """
        if not self.SOURCE_DB_PATH.exists():
            raise FileNotFoundError(
                f"iMessage database not found at {self.SOURCE_DB_PATH}. "
                "Ensure Full Disk Access is granted."
            )

        if full_resync:
            self._clear_data()

        last_rowid = self._get_last_synced_rowid()
        logger.info(f"Starting iMessage export from ROWID > {last_rowid}")

        stats = {
            "messages_exported": 0,
            "messages_skipped": 0,
            "last_rowid": last_rowid,
            "new_last_rowid": last_rowid,
        }

        # Query source database
        # Include attributedBody for extracting text from newer iOS/macOS versions
        query = """
            SELECT
                m.ROWID,
                m.text,
                m.attributedBody,
                m.date,
                m.is_from_me,
                h.id as handle,
                m.service
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
        """

        if limit:
            query += f" LIMIT {limit}"

        try:
            with sqlite3.connect(f"file:{self.SOURCE_DB_PATH}?mode=ro", uri=True) as source_conn:
                source_conn.row_factory = sqlite3.Row
                cursor = source_conn.execute(query, (last_rowid,))

                batch = []
                max_rowid = last_rowid

                for row in cursor:
                    rowid = row["ROWID"]
                    text = row["text"]
                    attributed_body = row["attributedBody"]
                    apple_ts = row["date"]
                    is_from_me = bool(row["is_from_me"])
                    handle = row["handle"] or ""
                    service = row["service"] or "Unknown"

                    # Try text field first, then attributedBody
                    final_text = text
                    if not final_text and attributed_body:
                        final_text = extract_text_from_attributed_body(attributed_body)

                    # Skip messages with no extractable text (attachments, etc.)
                    if not final_text:
                        stats["messages_skipped"] += 1
                        max_rowid = max(max_rowid, rowid)
                        continue

                    # Convert timestamp
                    timestamp = apple_timestamp_to_datetime(apple_ts)
                    if not timestamp:
                        stats["messages_skipped"] += 1
                        max_rowid = max(max_rowid, rowid)
                        continue

                    # Normalize phone number
                    handle_normalized = normalize_phone(handle) if handle else None

                    batch.append((
                        rowid,
                        final_text,
                        timestamp.isoformat(),
                        1 if is_from_me else 0,
                        handle,
                        handle_normalized,
                        service,
                    ))

                    max_rowid = max(max_rowid, rowid)
                    stats["messages_exported"] += 1

                    # Batch insert
                    if len(batch) >= 1000:
                        self._insert_batch(batch)
                        batch = []

                # Insert remaining
                if batch:
                    self._insert_batch(batch)

                # Update sync state
                if max_rowid > last_rowid:
                    self._set_last_synced_rowid(max_rowid)
                    stats["new_last_rowid"] = max_rowid

        except sqlite3.OperationalError as e:
            if "unable to open database" in str(e).lower():
                raise PermissionError(
                    f"Cannot access iMessage database. "
                    f"Grant Full Disk Access to Terminal/IDE in System Preferences."
                ) from e
            raise

        logger.info(
            f"iMessage export complete: {stats['messages_exported']} messages, "
            f"ROWID {last_rowid} -> {stats['new_last_rowid']}"
        )

        return stats

    def _insert_batch(self, batch: list) -> None:
        """Insert a batch of messages."""
        with sqlite3.connect(self.storage_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO messages
                (rowid, text, timestamp, is_from_me, handle, handle_normalized, service)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )

    def _clear_data(self) -> None:
        """Clear all exported data (for full resync)."""
        with sqlite3.connect(self.storage_path) as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sync_state")
        logger.info("Cleared iMessage export data for full resync")

    def update_entity_mappings(self, phone_to_entity: dict[str, str]) -> int:
        """
        Update person_entity_id for messages based on phone mappings.

        Args:
            phone_to_entity: Dict mapping E.164 phones to entity IDs

        Returns:
            Number of messages updated
        """
        total_updated = 0

        with sqlite3.connect(self.storage_path) as conn:
            for phone, entity_id in phone_to_entity.items():
                cursor = conn.execute(
                    """
                    UPDATE messages
                    SET person_entity_id = ?
                    WHERE handle_normalized = ?
                    AND (person_entity_id IS NULL OR person_entity_id != ?)
                    """,
                    (entity_id, phone, entity_id),
                )
                total_updated += cursor.rowcount

        logger.info(f"Updated entity mappings for {total_updated} messages (by phone)")
        return total_updated

    def update_entity_mappings_by_handle(self, handle_to_entity: dict[str, str]) -> int:
        """
        Update person_entity_id for messages based on raw handle (email) mappings.

        Args:
            handle_to_entity: Dict mapping email handles to entity IDs

        Returns:
            Number of messages updated
        """
        total_updated = 0

        with sqlite3.connect(self.storage_path) as conn:
            for handle, entity_id in handle_to_entity.items():
                # Match case-insensitively on handle
                cursor = conn.execute(
                    """
                    UPDATE messages
                    SET person_entity_id = ?
                    WHERE LOWER(handle) = LOWER(?)
                    AND (person_entity_id IS NULL OR person_entity_id != ?)
                    """,
                    (entity_id, handle, entity_id),
                )
                total_updated += cursor.rowcount

        logger.info(f"Updated entity mappings for {total_updated} messages (by email handle)")
        return total_updated

    def get_messages_for_phone(
        self,
        phone: str,
        limit: int = 100,
        since: Optional[datetime] = None,
    ) -> list[IMessageRecord]:
        """
        Get messages for a phone number.

        Args:
            phone: E.164 format phone number
            limit: Maximum messages to return
            since: Only return messages after this time

        Returns:
            List of IMessageRecord objects, most recent first
        """
        query = """
            SELECT rowid, text, timestamp, is_from_me, handle,
                   handle_normalized, service, person_entity_id
            FROM messages
            WHERE handle_normalized = ?
        """
        params = [phone]

        if since:
            query += " AND timestamp > ?"
            params.append(since.isoformat())

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.storage_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)

            return [
                IMessageRecord(
                    rowid=row["rowid"],
                    text=row["text"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    is_from_me=bool(row["is_from_me"]),
                    handle=row["handle"],
                    handle_normalized=row["handle_normalized"],
                    service=row["service"],
                    person_entity_id=row["person_entity_id"],
                )
                for row in cursor
            ]

    def get_messages_for_entity(
        self,
        entity_id: str,
        limit: int = 100,
        since: Optional[datetime] = None,
    ) -> list[IMessageRecord]:
        """
        Get messages for a PersonEntity.

        Args:
            entity_id: PersonEntity ID
            limit: Maximum messages to return
            since: Only return messages after this time

        Returns:
            List of IMessageRecord objects, most recent first
        """
        query = """
            SELECT rowid, text, timestamp, is_from_me, handle,
                   handle_normalized, service, person_entity_id
            FROM messages
            WHERE person_entity_id = ?
        """
        params = [entity_id]

        if since:
            query += " AND timestamp > ?"
            params.append(since.isoformat())

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.storage_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)

            return [
                IMessageRecord(
                    rowid=row["rowid"],
                    text=row["text"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    is_from_me=bool(row["is_from_me"]),
                    handle=row["handle"],
                    handle_normalized=row["handle_normalized"],
                    service=row["service"],
                    person_entity_id=row["person_entity_id"],
                )
                for row in cursor
            ]

    def search_messages(
        self,
        query: str,
        limit: int = 50,
        phone: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> list[IMessageRecord]:
        """
        Search messages by text content.

        Args:
            query: Search query (case-insensitive substring)
            limit: Maximum results
            phone: Optional filter by phone number
            entity_id: Optional filter by entity ID

        Returns:
            List of matching IMessageRecord objects
        """
        sql = """
            SELECT rowid, text, timestamp, is_from_me, handle,
                   handle_normalized, service, person_entity_id
            FROM messages
            WHERE text LIKE ?
        """
        params = [f"%{query}%"]

        if phone:
            sql += " AND handle_normalized = ?"
            params.append(phone)

        if entity_id:
            sql += " AND person_entity_id = ?"
            params.append(entity_id)

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.storage_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params)

            return [
                IMessageRecord(
                    rowid=row["rowid"],
                    text=row["text"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    is_from_me=bool(row["is_from_me"]),
                    handle=row["handle"],
                    handle_normalized=row["handle_normalized"],
                    service=row["service"],
                    person_entity_id=row["person_entity_id"],
                )
                for row in cursor
            ]

    def query_messages(
        self,
        entity_id: Optional[str] = None,
        phone: Optional[str] = None,
        search_term: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        direction: Optional[str] = None,  # "sent", "received", or None for both
        limit: int = 100,
    ) -> list[IMessageRecord]:
        """
        Flexible message query with date range and search support.

        This method is designed for the orchestrator to query messages
        with custom parameters based on user questions.

        Args:
            entity_id: Filter by PersonEntity ID
            phone: Filter by E.164 phone number
            search_term: Search within message text (case-insensitive)
            start_date: Only messages after this datetime
            end_date: Only messages before this datetime
            direction: "sent" for is_from_me=1, "received" for is_from_me=0
            limit: Maximum messages to return

        Returns:
            List of IMessageRecord objects, ordered by timestamp DESC
        """
        sql = """
            SELECT rowid, text, timestamp, is_from_me, handle,
                   handle_normalized, service, person_entity_id
            FROM messages
            WHERE 1=1
        """
        params = []

        if entity_id:
            sql += " AND person_entity_id = ?"
            params.append(entity_id)

        if phone:
            sql += " AND handle_normalized = ?"
            params.append(phone)

        if search_term:
            sql += " AND text LIKE ?"
            params.append(f"%{search_term}%")

        if start_date:
            sql += " AND timestamp >= ?"
            params.append(start_date.isoformat())

        if end_date:
            sql += " AND timestamp <= ?"
            params.append(end_date.isoformat())

        if direction == "sent":
            sql += " AND is_from_me = 1"
        elif direction == "received":
            sql += " AND is_from_me = 0"

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.storage_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params)

            return [
                IMessageRecord(
                    rowid=row["rowid"],
                    text=row["text"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    is_from_me=bool(row["is_from_me"]),
                    handle=row["handle"],
                    handle_normalized=row["handle_normalized"],
                    service=row["service"],
                    person_entity_id=row["person_entity_id"],
                )
                for row in cursor
            ]

    def format_messages_for_context(
        self,
        messages: list[IMessageRecord],
        include_direction: bool = True,
    ) -> str:
        """
        Format messages as markdown for LLM context.

        Args:
            messages: List of IMessageRecord objects
            include_direction: Whether to show → (sent) and ← (received) arrows

        Returns:
            Markdown-formatted message history
        """
        if not messages:
            return "_No messages found._"

        lines = []
        current_date = None

        for msg in messages:
            msg_date = msg.timestamp.strftime("%Y-%m-%d")

            # Add date header when date changes
            if msg_date != current_date:
                if current_date is not None:
                    lines.append("")  # Blank line between days
                lines.append(f"### {msg_date}")
                current_date = msg_date

            time_str = msg.timestamp.strftime("%H:%M")
            direction = ""
            if include_direction:
                direction = "→ " if msg.is_from_me else "← "

            # Truncate very long messages
            text = msg.text
            if len(text) > 300:
                text = text[:300] + "..."

            # Escape markdown in message text
            text = text.replace("\n", " ").strip()
            lines.append(f"- **{time_str}** {direction}{text}")

        return "\n".join(lines)

    def get_statistics(self) -> dict:
        """Get statistics about the exported messages."""
        with sqlite3.connect(self.storage_path) as conn:
            stats = {}

            # Total messages
            cursor = conn.execute("SELECT COUNT(*) FROM messages")
            stats["total_messages"] = cursor.fetchone()[0]

            # Messages by service
            cursor = conn.execute(
                "SELECT service, COUNT(*) FROM messages GROUP BY service"
            )
            stats["by_service"] = dict(cursor.fetchall())

            # Messages from me vs received
            cursor = conn.execute(
                "SELECT is_from_me, COUNT(*) FROM messages GROUP BY is_from_me"
            )
            from_me_counts = dict(cursor.fetchall())
            stats["sent"] = from_me_counts.get(1, 0)
            stats["received"] = from_me_counts.get(0, 0)

            # Unique handles
            cursor = conn.execute(
                "SELECT COUNT(DISTINCT handle_normalized) FROM messages "
                "WHERE handle_normalized IS NOT NULL"
            )
            stats["unique_contacts"] = cursor.fetchone()[0]

            # Messages with entity mapping
            cursor = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE person_entity_id IS NOT NULL"
            )
            stats["messages_with_entity"] = cursor.fetchone()[0]

            # Date range
            cursor = conn.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM messages"
            )
            row = cursor.fetchone()
            stats["oldest_message"] = row[0]
            stats["newest_message"] = row[1]

            # Last sync ROWID
            stats["last_synced_rowid"] = self._get_last_synced_rowid()

            return stats

    def get_recent_conversations(
        self,
        days: int = 7,
        limit: int = 20,
    ) -> list[dict]:
        """
        Get recent conversations (contacts with messages in the last N days).

        Args:
            days: Number of days to look back
            limit: Maximum contacts to return

        Returns:
            List of dicts with contact info and message counts
        """
        cutoff = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.storage_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT
                    handle_normalized,
                    handle,
                    person_entity_id,
                    COUNT(*) as message_count,
                    MAX(timestamp) as last_message,
                    SUM(CASE WHEN is_from_me = 1 THEN 1 ELSE 0 END) as sent,
                    SUM(CASE WHEN is_from_me = 0 THEN 1 ELSE 0 END) as received
                FROM messages
                WHERE timestamp > datetime('now', ?)
                AND handle_normalized IS NOT NULL
                GROUP BY handle_normalized
                ORDER BY last_message DESC
                LIMIT ?
                """,
                (f"-{days} days", limit),
            )

            return [dict(row) for row in cursor]


# Singleton instance
_imessage_store: Optional[IMessageStore] = None


def get_imessage_store(storage_path: str = "./data/imessage.db") -> IMessageStore:
    """
    Get or create the singleton IMessageStore.

    Args:
        storage_path: Path to local SQLite database

    Returns:
        IMessageStore instance
    """
    global _imessage_store
    if _imessage_store is None:
        _imessage_store = IMessageStore(storage_path)
    return _imessage_store


def sync_imessages() -> dict:
    """
    Convenience function for nightly sync.

    Returns:
        Export statistics
    """
    store = get_imessage_store()
    return store.export_from_source()


def join_imessages_to_entities() -> dict:
    """
    Join iMessage records to PersonEntity records by phone number or email.

    Looks up all unique handles in the iMessage store and maps them
    to PersonEntity records via phone or email lookup.

    Returns:
        Dict with join statistics
    """
    from api.services.person_entity import get_person_entity_store

    store = get_imessage_store()
    entity_store = get_person_entity_store()

    # Get unique phone numbers from messages
    with sqlite3.connect(store.storage_path) as conn:
        cursor = conn.execute(
            """
            SELECT DISTINCT handle_normalized
            FROM messages
            WHERE handle_normalized IS NOT NULL
            """
        )
        phones = [row[0] for row in cursor]

        # Also get unique email-like handles (those without normalized phone)
        cursor = conn.execute(
            """
            SELECT DISTINCT handle
            FROM messages
            WHERE handle_normalized IS NULL
            AND handle LIKE '%@%'
            """
        )
        emails = [row[0].lower() for row in cursor]

    logger.info(f"Found {len(phones)} unique phone numbers and {len(emails)} unique emails to map")

    # Build phone -> entity_id mapping
    phone_to_entity: dict[str, str] = {}
    phones_matched = 0
    phones_unmatched = 0

    for phone in phones:
        entity = entity_store.get_by_phone(phone)
        if entity:
            phone_to_entity[phone] = entity.id
            phones_matched += 1
        else:
            phones_unmatched += 1

    # Build email -> entity_id mapping
    email_to_entity: dict[str, str] = {}
    emails_matched = 0
    emails_unmatched = 0

    for email in emails:
        entity = entity_store.get_by_email(email)
        if entity:
            email_to_entity[email] = entity.id
            emails_matched += 1
        else:
            emails_unmatched += 1

    # Update messages with entity mappings (phones via handle_normalized)
    messages_updated = 0
    if phone_to_entity:
        messages_updated = store.update_entity_mappings(phone_to_entity)

    # Update messages with entity mappings (emails via handle)
    if email_to_entity:
        messages_updated += store.update_entity_mappings_by_handle(email_to_entity)

    stats = {
        "unique_phones": len(phones),
        "phones_matched": phones_matched,
        "phones_unmatched": phones_unmatched,
        "unique_emails": len(emails),
        "emails_matched": emails_matched,
        "emails_unmatched": emails_unmatched,
        "messages_updated": messages_updated,
    }

    logger.info(f"Entity join complete: {stats}")
    return stats


def sync_and_join_imessages() -> dict:
    """
    Convenience function for nightly sync: export + entity join.

    Returns:
        Combined statistics
    """
    export_stats = sync_imessages()
    join_stats = join_imessages_to_entities()

    return {
        "export": export_stats,
        "join": join_stats,
    }


def query_person_messages(
    entity_id: str,
    search_term: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
) -> dict:
    """
    Query iMessage history for a person - designed for orchestrator use.

    This function is called by the chat orchestrator when it needs to
    retrieve message history with specific parameters (date ranges, search terms).

    Args:
        entity_id: PersonEntity ID to query messages for
        search_term: Optional text to search within messages
        start_date: Only include messages after this date
        end_date: Only include messages before this date
        limit: Maximum messages to return (default 100)

    Returns:
        Dict with:
        - messages: List of IMessageRecord objects
        - formatted: Markdown-formatted message history
        - count: Number of messages returned
        - date_range: Actual date range of returned messages
    """
    store = get_imessage_store()

    messages = store.query_messages(
        entity_id=entity_id,
        search_term=search_term,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )

    # Reverse to show chronological order (oldest first)
    messages_chronological = list(reversed(messages))

    formatted = store.format_messages_for_context(messages_chronological)

    # Calculate actual date range
    date_range = None
    if messages:
        oldest = min(m.timestamp for m in messages)
        newest = max(m.timestamp for m in messages)
        date_range = {
            "start": oldest.isoformat(),
            "end": newest.isoformat(),
        }

    return {
        "messages": messages_chronological,
        "formatted": formatted,
        "count": len(messages),
        "date_range": date_range,
    }
