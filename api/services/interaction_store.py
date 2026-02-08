"""
Interaction Store for LifeOS People System v2.

Stores lightweight interaction records with links to sources.
Each interaction represents a single touchpoint (email, meeting, note mention).
"""
import sqlite3
import json
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from config.settings import settings
from config.people_config import InteractionConfig

from api.utils.datetime_utils import make_aware as _make_aware

logger = logging.getLogger(__name__)

# Sentinel date for undated vault notes - allows them to appear in counts
# while being filterable in timeline views
UNDATED_SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)


def get_interaction_db_path() -> str:
    """Get the path to the interactions database."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "interactions.db")


@dataclass
class Interaction:
    """
    A single interaction with a person.

    Stores metadata and links to source content, NOT the full content itself.
    """

    id: str
    person_id: str  # FK to PersonEntity.id
    timestamp: datetime
    source_type: str  # "gmail", "calendar", "vault", "granola"

    # Metadata (not full content)
    title: str  # Email subject, meeting title, note filename
    snippet: Optional[str] = None  # First N chars for preview

    # Links to actual content
    source_link: str = ""  # Gmail URL, obsidian:// link, calendar URL
    source_id: Optional[str] = None  # Gmail message ID, calendar event ID, file path

    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)

    # Account and subtype info (for weighted scoring)
    source_account: Optional[str] = None  # "personal" or "work"
    attendee_count: Optional[int] = None  # For calendar events: number of other attendees

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Interaction":
        """Create Interaction from dict."""
        if isinstance(data.get("timestamp"), str):
            dt = datetime.fromisoformat(data["timestamp"])
            data["timestamp"] = _make_aware(dt)
        if isinstance(data.get("created_at"), str):
            dt = datetime.fromisoformat(data["created_at"])
            data["created_at"] = _make_aware(dt)
        return cls(**data)

    @classmethod
    def from_row(cls, row: tuple) -> "Interaction":
        """Create Interaction from SQLite row."""
        # Parse and normalize timestamps to be timezone-aware
        timestamp = datetime.fromisoformat(row[2]) if row[2] else datetime.now(timezone.utc)
        timestamp = _make_aware(timestamp)
        created_at = datetime.fromisoformat(row[8]) if row[8] else datetime.now(timezone.utc)
        created_at = _make_aware(created_at)

        # Handle optional new columns (source_account at index 9, attendee_count at index 10)
        source_account = row[9] if len(row) > 9 else None
        attendee_count = row[10] if len(row) > 10 else None

        return cls(
            id=row[0],
            person_id=row[1],
            timestamp=timestamp,
            source_type=row[3],
            title=row[4],
            snippet=row[5],
            source_link=row[6] or "",
            source_id=row[7],
            created_at=created_at,
            source_account=source_account,
            attendee_count=attendee_count,
        )

    @property
    def source_badge(self) -> str:
        """Get emoji badge for source type."""
        badges = {
            "gmail": "📧",
            "calendar": "📅",
            "vault": "📝",
            "granola": "📝",
            "imessage": "💬",
            "whatsapp": "💬",
            "contacts": "📇",
            "phone": "📞",
            "photos": "📷",
        }
        return badges.get(self.source_type, "📄")


def build_obsidian_link(file_path: str, vault_path: str = None) -> str:
    """
    Build an obsidian:// URI for a vault file.

    Args:
        file_path: Absolute or relative path to the file
        vault_path: Path to vault root (default from settings)

    Returns:
        obsidian:// URI
    """
    if vault_path is None:
        vault_path = str(settings.vault_path)

    # Get relative path from vault root
    path = Path(file_path)
    try:
        rel_path = path.relative_to(vault_path)
    except ValueError:
        rel_path = path

    # Build URI - obsidian://open?vault=VaultName&file=path/to/file
    vault_name = Path(vault_path).name
    file_param = quote(str(rel_path).replace(".md", ""), safe="")
    return f"obsidian://open?vault={quote(vault_name)}&file={file_param}"


def build_gmail_link(message_id: str) -> str:
    """
    Build a Gmail deep link for a message.

    Args:
        message_id: Gmail message ID

    Returns:
        Gmail web URL
    """
    return f"https://mail.google.com/mail/u/0/#inbox/{message_id}"


def build_calendar_link(event_id: str, calendar_id: str = "primary") -> str:
    """
    Build a Google Calendar link for an event.

    Args:
        event_id: Calendar event ID
        calendar_id: Calendar ID (default "primary")

    Returns:
        Google Calendar web URL
    """
    return f"https://calendar.google.com/calendar/event?eid={event_id}"


class InteractionStore:
    """
    SQLite-backed interaction storage.

    Manages interaction records with efficient queries by person and time range.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize interaction store.

        Args:
            db_path: Path to SQLite database (default from settings)
        """
        self.db_path = db_path or get_interaction_db_path()
        self._init_db()

    def _init_db(self):
        """Create database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS interactions (
                    id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    source_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    snippet TEXT,
                    source_link TEXT,
                    source_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Index for efficient person + time queries
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_interactions_person_timestamp
                ON interactions(person_id, timestamp DESC)
            """
            )

            # Index for source deduplication
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_interactions_source
                ON interactions(source_type, source_id)
            """
            )

            # Index for time-based queries
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_interactions_timestamp
                ON interactions(timestamp DESC)
            """
            )

            # Migration: Add source_account and attendee_count columns if missing
            cursor = conn.execute("PRAGMA table_info(interactions)")
            columns = {row[1] for row in cursor.fetchall()}

            if "source_account" not in columns:
                conn.execute("ALTER TABLE interactions ADD COLUMN source_account TEXT")
                logger.info("Added source_account column to interactions table")

            if "attendee_count" not in columns:
                conn.execute("ALTER TABLE interactions ADD COLUMN attendee_count INTEGER")
                logger.info("Added attendee_count column to interactions table")

            conn.commit()
            logger.info(f"Initialized interaction database at {self.db_path}")
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)

    def add(self, interaction: Interaction) -> Interaction:
        """
        Add a new interaction.

        Automatically follows merge chain - if the person_id was merged into
        another person, links to the surviving primary instead.

        Args:
            interaction: Interaction to add

        Returns:
            The added interaction
        """
        # Follow merge chain to get the canonical person ID
        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()
        resolved_person_id = person_store.get_canonical_id(interaction.person_id)

        conn = self._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO interactions
                (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    interaction.id,
                    resolved_person_id,  # Use canonical ID
                    interaction.timestamp.isoformat(),
                    interaction.source_type,
                    interaction.title,
                    interaction.snippet,
                    interaction.source_link,
                    interaction.source_id,
                    interaction.created_at.isoformat(),
                ),
            )
            conn.commit()
            # Update the interaction object with the resolved ID
            interaction.person_id = resolved_person_id
            return interaction
        finally:
            conn.close()

    def add_if_not_exists(
        self, interaction: Interaction
    ) -> tuple[Interaction, bool]:
        """
        Add interaction if source_id doesn't already exist.

        Useful for avoiding duplicate imports.

        Args:
            interaction: Interaction to add

        Returns:
            Tuple of (interaction, was_added)
        """
        if interaction.source_id:
            existing = self.get_by_source(
                interaction.source_type, interaction.source_id
            )
            if existing:
                return existing, False

        return self.add(interaction), True

    def get_by_id(self, interaction_id: str) -> Optional[Interaction]:
        """Get interaction by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
            )
            row = cursor.fetchone()
            if row:
                return Interaction.from_row(row)
            return None
        finally:
            conn.close()

    def get_by_source(
        self, source_type: str, source_id: str
    ) -> Optional[Interaction]:
        """Get interaction by source type and ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM interactions WHERE source_type = ? AND source_id = ?",
                (source_type, source_id),
            )
            row = cursor.fetchone()
            if row:
                return Interaction.from_row(row)
            return None
        finally:
            conn.close()

    def get_for_person(
        self,
        person_id: str,
        days_back: int = None,
        limit: int = None,
        source_type: Optional[str] = None,
        specific_date: Optional[str] = None,
    ) -> list[Interaction]:
        """
        Get interactions for a person.

        Args:
            person_id: PersonEntity ID
            days_back: Only return interactions from last N days (default from config)
            limit: Maximum interactions to return (default from config)
            source_type: Filter by source type. Supports comma-separated values for
                         multiple types (e.g., "imessage,whatsapp" for messages).
            specific_date: Filter to a specific date (YYYY-MM-DD format, optional)

        Returns:
            List of interactions, most recent first
        """
        if limit is None:
            limit = InteractionConfig.MAX_INTERACTIONS_PER_QUERY

        conn = self._get_connection()
        try:
            # Parse source_type into list if comma-separated (e.g., "imessage,whatsapp")
            # This enables compound filters like "messages" = imessage + whatsapp
            source_types = None
            if source_type:
                source_types = [s.strip() for s in source_type.split(",") if s.strip()]

            # Build query based on filters
            if specific_date:
                # Filter to a specific day
                date_start = f"{specific_date}T00:00:00"
                date_end = f"{specific_date}T23:59:59"

                if source_types:
                    # Use IN clause for multiple source types
                    placeholders = ",".join("?" * len(source_types))
                    cursor = conn.execute(
                        f"""
                        SELECT * FROM interactions
                        WHERE person_id = ? AND timestamp >= ? AND timestamp <= ?
                            AND source_type IN ({placeholders})
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (person_id, date_start, date_end, *source_types, limit),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT * FROM interactions
                        WHERE person_id = ? AND timestamp >= ? AND timestamp <= ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (person_id, date_start, date_end, limit),
                    )
            else:
                # Use days_back cutoff
                if days_back is None:
                    days_back = InteractionConfig.DEFAULT_WINDOW_DAYS
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(days=days_back)

                if source_types:
                    # Use IN clause for multiple source types
                    placeholders = ",".join("?" * len(source_types))
                    cursor = conn.execute(
                        f"""
                        SELECT * FROM interactions
                        WHERE person_id = ? AND timestamp > ? AND timestamp <= ?
                            AND source_type IN ({placeholders})
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (person_id, cutoff.isoformat(), now.isoformat(), *source_types, limit),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT * FROM interactions
                        WHERE person_id = ? AND timestamp > ? AND timestamp <= ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (person_id, cutoff.isoformat(), now.isoformat(), limit),
                    )

            return [Interaction.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_interaction_counts(
        self, person_id: str, days_back: int = None
    ) -> dict[str, int]:
        """
        Get count of interactions by source type for a person.

        Args:
            person_id: PersonEntity ID
            days_back: Only count interactions from last N days

        Returns:
            Dict mapping source_type to count
        """
        if days_back is None:
            days_back = InteractionConfig.DEFAULT_WINDOW_DAYS

        cutoff = datetime.now() - timedelta(days=days_back)

        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT source_type, COUNT(*) as count
                FROM interactions
                WHERE person_id = ? AND timestamp > ?
                GROUP BY source_type
            """,
                (person_id, cutoff.isoformat()),
            )

            return {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            conn.close()

    def get_interaction_counts_with_subtypes(
        self, person_id: str, days_back: int = None
    ) -> list[dict]:
        """
        Get interaction counts with subtype detail for weight calculation.

        For gmail: parses direction from title prefix (→/←/↔)
        For calendar: derives size from attendee_count

        Args:
            person_id: PersonEntity ID
            days_back: Only count interactions from last N days

        Returns:
            List of dicts with keys: source_type, subtype, source_account, count
        """
        if days_back is None:
            days_back = InteractionConfig.DEFAULT_WINDOW_DAYS

        cutoff = datetime.now() - timedelta(days=days_back)

        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT
                    source_type,
                    source_account,
                    CASE
                        WHEN source_type = 'gmail' AND title LIKE '→ %' THEN 'gmail_sent'
                        WHEN source_type = 'gmail' AND title LIKE '← %' THEN 'gmail_received'
                        WHEN source_type = 'gmail' AND title LIKE '↔ %' THEN 'gmail_cc'
                        WHEN source_type = 'calendar' AND attendee_count = 1 THEN 'calendar_1on1'
                        WHEN source_type = 'calendar' AND attendee_count BETWEEN 2 AND 5 THEN 'calendar_small_group'
                        WHEN source_type = 'calendar' AND attendee_count >= 6 THEN 'calendar_large_meeting'
                        ELSE NULL
                    END as subtype,
                    COUNT(*) as count
                FROM interactions
                WHERE person_id = ? AND timestamp > ?
                GROUP BY source_type, subtype, source_account
            """,
                (person_id, cutoff.isoformat()),
            )

            results = []
            for row in cursor.fetchall():
                results.append({
                    "source_type": row[0],
                    "source_account": row[1],
                    "subtype": row[2],
                    "count": row[3],
                })
            return results
        finally:
            conn.close()

    def get_for_people_batch(
        self,
        person_ids: set[str],
        days_back: int = 365,
        limit_per_person: int = 1000,
    ) -> dict[str, list[Interaction]]:
        """
        Batch fetch interactions for multiple people in one query.

        This is significantly more efficient than calling get_for_person() in a loop.
        Used by the family dashboard to avoid N+1 queries.

        Args:
            person_ids: Set of PersonEntity IDs
            days_back: Only return interactions from last N days (default 365)
            limit_per_person: Maximum interactions per person (default 1000)

        Returns:
            Dict mapping person_id to list of interactions, most recent first
        """
        if not person_ids:
            return {}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        now = datetime.now(timezone.utc)
        person_ids_list = list(person_ids)
        placeholders = ",".join("?" * len(person_ids_list))

        conn = self._get_connection()
        try:
            # Fetch all interactions for the given people in one query
            # Order by person_id, timestamp DESC so we can process in order
            cursor = conn.execute(
                f"""
                SELECT * FROM interactions
                WHERE person_id IN ({placeholders})
                  AND timestamp >= ?
                  AND timestamp <= ?
                ORDER BY person_id, timestamp DESC
            """,
                person_ids_list + [cutoff.isoformat(), now.isoformat()],
            )

            # Build result dict, respecting per-person limit
            result: dict[str, list[Interaction]] = {pid: [] for pid in person_ids}
            for row in cursor.fetchall():
                interaction = Interaction.from_row(row)
                person_list = result[interaction.person_id]
                if len(person_list) < limit_per_person:
                    person_list.append(interaction)

            return result
        finally:
            conn.close()

    def get_last_interaction(self, person_id: str) -> Optional[Interaction]:
        """Get the most recent interaction with a person (excludes future dates)."""
        conn = self._get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """
                SELECT * FROM interactions
                WHERE person_id = ? AND timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT 1
            """,
                (person_id, now),
            )
            row = cursor.fetchone()
            if row:
                return Interaction.from_row(row)
            return None
        finally:
            conn.close()

    def get_last_interaction_by_source(self, person_id: str) -> dict[str, datetime]:
        """
        Get the most recent interaction timestamp for each source type.

        Used for channel-aware routing: knowing when you last communicated
        with someone on each channel helps decide which sources to query.

        Args:
            person_id: PersonEntity ID

        Returns:
            Dict mapping source_type to last interaction timestamp.
            e.g., {"gmail": datetime(...), "imessage": datetime(...)}
            Only includes source types with at least one interaction.
            Excludes future dates (e.g., from future calendar events).
        """
        conn = self._get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """
                SELECT source_type, MAX(timestamp) as last_ts
                FROM interactions
                WHERE person_id = ? AND timestamp <= ?
                GROUP BY source_type
                """,
                (person_id, now),
            )
            result = {}
            for row in cursor.fetchall():
                source_type = row[0]
                ts_str = row[1]
                if ts_str:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    result[source_type] = _make_aware(dt)
            return result
        finally:
            conn.close()

    def get_first_interaction_dates(self, min_interactions: int = 1) -> dict[str, datetime]:
        """
        Get the earliest interaction timestamp for each person.

        Args:
            min_interactions: Minimum number of interactions required to include
                             a person. Use >1 to filter out one-off contacts.

        Returns a dict mapping person_id -> first interaction datetime.
        Used for calculating true network growth over time.
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT person_id, MIN(timestamp) as first_timestamp, COUNT(*) as cnt
                FROM interactions
                GROUP BY person_id
                HAVING cnt >= ?
            """,
                (min_interactions,),
            )
            result = {}
            for row in cursor.fetchall():
                person_id = row[0]
                ts_str = row[1]
                if ts_str:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    result[person_id] = _make_aware(dt)
            return result
        finally:
            conn.close()

    def get_conversation_context(
        self,
        interaction_id: str,
        window: int = 5,
        time_window_hours: int = 24,
    ) -> list[Interaction]:
        """
        Get messages surrounding an interaction in the same conversation.

        For message-based interactions (iMessage, WhatsApp, Slack), this returns
        neighboring messages to provide context for fact extraction.

        Args:
            interaction_id: The target interaction's ID
            window: Number of messages to fetch before and after
            time_window_hours: Max time span to consider same conversation

        Returns:
            List of interactions: [N before] + [target] + [N after], sorted by timestamp
        """
        # Message-based source types that benefit from context
        MESSAGE_SOURCES = {"imessage", "whatsapp", "slack"}

        conn = self._get_connection()
        try:
            # Get the target interaction
            cursor = conn.execute(
                "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
            )
            row = cursor.fetchone()
            if not row:
                return []

            target = Interaction.from_row(row)

            # Only fetch context for message-based sources
            if target.source_type not in MESSAGE_SOURCES:
                return [target]

            # Calculate time window boundaries
            from datetime import timedelta
            time_delta = timedelta(hours=time_window_hours)
            window_start = (target.timestamp - time_delta).isoformat()
            window_end = (target.timestamp + time_delta).isoformat()

            # Get messages before the target
            cursor = conn.execute(
                """
                SELECT * FROM interactions
                WHERE person_id = ?
                  AND source_type = ?
                  AND timestamp < ?
                  AND timestamp >= ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (
                    target.person_id,
                    target.source_type,
                    target.timestamp.isoformat(),
                    window_start,
                    window,
                ),
            )
            before = [Interaction.from_row(r) for r in cursor.fetchall()]
            before.reverse()  # Reverse to chronological order

            # Get messages after the target
            cursor = conn.execute(
                """
                SELECT * FROM interactions
                WHERE person_id = ?
                  AND source_type = ?
                  AND timestamp > ?
                  AND timestamp <= ?
                ORDER BY timestamp ASC
                LIMIT ?
            """,
                (
                    target.person_id,
                    target.source_type,
                    target.timestamp.isoformat(),
                    window_end,
                    window,
                ),
            )
            after = [Interaction.from_row(r) for r in cursor.fetchall()]

            # Combine: before + target + after
            return before + [target] + after

        finally:
            conn.close()

    def enrich_interactions_with_context(
        self,
        interactions: list[dict],
        window: int = 5,
    ) -> list[dict]:
        """
        Enrich a list of interactions with conversation context.

        For message-based interactions (iMessage, WhatsApp, Slack), adds
        surrounding messages to provide better context for fact extraction.

        Args:
            interactions: List of interaction dicts (with 'id' field)
            window: Number of messages to include before/after

        Returns:
            Enriched list where message-based interactions include 'context' field
        """
        MESSAGE_SOURCES = {"imessage", "whatsapp", "slack"}

        enriched = []
        seen_context_ids = set()

        for interaction in interactions:
            interaction_id = interaction.get("id")
            source_type = interaction.get("source_type", "")

            if source_type in MESSAGE_SOURCES and interaction_id:
                # Get context for this message
                context = self.get_conversation_context(interaction_id, window)

                if len(context) > 1:
                    # Format context as a thread
                    context_snippets = []
                    for ctx in context:
                        if ctx.id == interaction_id:
                            context_snippets.append(f">>> {ctx.snippet or ctx.title}")
                        else:
                            context_snippets.append(f"  {ctx.snippet or ctx.title}")

                    # Add context to the interaction
                    enriched_interaction = dict(interaction)
                    enriched_interaction["context"] = "\n".join(context_snippets)
                    enriched_interaction["context_count"] = len(context)

                    # Track context IDs to avoid duplicate processing
                    for ctx in context:
                        seen_context_ids.add(ctx.id)

                    enriched.append(enriched_interaction)
                else:
                    enriched.append(interaction)
            else:
                enriched.append(interaction)

        return enriched

    def delete(self, interaction_id: str) -> bool:
        """
        Delete an interaction by ID.

        Returns:
            True if deleted, False if not found
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM interactions WHERE id = ?", (interaction_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_for_person(self, person_id: str) -> int:
        """
        Delete all interactions for a person.

        Returns:
            Number of interactions deleted
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM interactions WHERE person_id = ?", (person_id,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def delete_by_source_type(self, source_type: str) -> int:
        """
        Delete all interactions of a specific source type.

        Useful for cleanup before re-indexing vault notes with improved
        date extraction logic.

        Args:
            source_type: The source type to delete (e.g., "vault", "granola")

        Returns:
            Number of interactions deleted
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM interactions WHERE source_type = ?", (source_type,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def count(self) -> int:
        """Get total number of interactions."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM interactions")
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def create_backup(self) -> Optional[Path]:
        """
        Create a backup of interactions.db.

        Uses LIFEOS_BACKUP_PATH from settings.
        Keeps only 2 most recent backups.

        Returns:
            Path to backup file if created, None if no db to backup
        """
        import shutil

        db_path = Path(self.db_path)
        if not db_path.exists():
            logger.warning("No interactions.db to backup")
            return None

        backup_dir = Path(settings.backup_path)
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"interactions.db.{timestamp}.backup"

        try:
            shutil.copy2(db_path, backup_path)
            logger.info(f"Created interactions backup: {backup_path}")

            # Keep only 2 most recent backups
            backups = sorted(backup_dir.glob("interactions.db.*.backup"))
            for old_backup in backups[:-2]:
                old_backup.unlink()
                logger.debug(f"Removed old backup: {old_backup}")

            return backup_path
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
            # Record backup failure for nightly alert
            from api.services.notifications import record_failure
            record_failure("backup_storage", f"Interactions backup failed: {e}", severity="warning")
            return None

    def get_statistics(self) -> dict:
        """Get aggregate statistics about stored interactions."""
        conn = self._get_connection()
        try:
            # Total count
            total = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]

            # By source type
            by_source = {}
            cursor = conn.execute(
                """
                SELECT source_type, COUNT(*) as count
                FROM interactions
                GROUP BY source_type
            """
            )
            for row in cursor.fetchall():
                by_source[row[0]] = row[1]

            # Unique people
            unique_people = conn.execute(
                "SELECT COUNT(DISTINCT person_id) FROM interactions"
            ).fetchone()[0]

            # Date range
            date_range = conn.execute(
                """
                SELECT MIN(timestamp), MAX(timestamp)
                FROM interactions
            """
            ).fetchone()

            return {
                "total_interactions": total,
                "by_source": by_source,
                "unique_people": unique_people,
                "earliest_interaction": date_range[0],
                "latest_interaction": date_range[1],
            }
        finally:
            conn.close()

    def get_all_in_range(
        self,
        start_date: datetime,
        end_date: datetime,
        exclude_person_ids: list[str] = None,
        source_type: Optional[str] = None,
        limit: Optional[int] = None,
        specific_date: Optional[str] = None,
    ) -> list[Interaction]:
        """
        Get all interactions within a date range.

        Used for aggregate views like the "Me" dashboard and timeline.

        Args:
            start_date: Start of date range (inclusive)
            end_date: End of date range (inclusive)
            exclude_person_ids: Person IDs to exclude (e.g., self)
            source_type: Filter by source type. Supports comma-separated values
                         (e.g., "imessage,whatsapp" for messages).
            limit: Maximum number of interactions to return
            specific_date: Filter to a specific date (YYYY-MM-DD), overrides start/end

        Returns:
            List of interactions in the date range
        """
        conn = self._get_connection()
        try:
            # Handle specific date filter (overrides start/end range)
            # Note: Timestamps in DB are ISO format (e.g., 2023-02-24T16:00:00-05:00)
            # Use simple date comparison which works with ISO strings
            if specific_date:
                # For single date: timestamp >= 'YYYY-MM-DD' AND timestamp < next day
                start_str = specific_date
                # Calculate next day for exclusive upper bound
                from datetime import datetime as dt, timedelta
                next_day = (dt.strptime(specific_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
                end_str = next_day
                use_less_than = True
            else:
                # Format dates for SQLite
                start_str = start_date.strftime('%Y-%m-%d')
                end_str = end_date.strftime('%Y-%m-%d 23:59:59')
                use_less_than = False

            # Build query - use < for specific date (exclusive upper bound), <= otherwise
            if use_less_than:
                query = """
                    SELECT id, person_id, timestamp, source_type, title, snippet, source_link, source_id
                    FROM interactions
                    WHERE timestamp >= ? AND timestamp < ?
                """
            else:
                query = """
                    SELECT id, person_id, timestamp, source_type, title, snippet, source_link, source_id
                    FROM interactions
                    WHERE timestamp >= ? AND timestamp <= ?
                """
            params = [start_str, end_str]

            # Exclude specific person IDs if provided
            if exclude_person_ids:
                placeholders = ','.join('?' * len(exclude_person_ids))
                query += f" AND person_id NOT IN ({placeholders})"
                params.extend(exclude_person_ids)

            # Filter by source type(s) - supports comma-separated values
            if source_type:
                source_types = [s.strip() for s in source_type.split(",") if s.strip()]
                if source_types:
                    placeholders = ','.join('?' * len(source_types))
                    query += f" AND source_type IN ({placeholders})"
                    params.extend(source_types)

            query += " ORDER BY timestamp DESC"

            # Apply limit if specified
            if limit:
                query += f" LIMIT {int(limit)}"

            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)

            interactions = []
            for row in cursor.fetchall():
                interactions.append(Interaction(
                    id=row['id'],
                    person_id=row['person_id'],
                    timestamp=datetime.fromisoformat(row['timestamp']),
                    source_type=row['source_type'],
                    title=row['title'],
                    snippet=row['snippet'],
                    source_link=row['source_link'],
                    source_id=row['source_id'],
                ))

            return interactions
        finally:
            conn.close()

    def format_interaction_history(
        self, person_id: str, days_back: int = None, limit: int = None
    ) -> str:
        """
        Format interaction history as markdown for briefings.

        Args:
            person_id: PersonEntity ID
            days_back: Days to look back
            limit: Maximum interactions

        Returns:
            Formatted markdown string
        """
        interactions = self.get_for_person(person_id, days_back, limit)
        counts = self.get_interaction_counts(person_id, days_back)
        last = self.get_last_interaction(person_id)

        if not interactions:
            return "_No interactions found in the specified time period._"

        # Build summary line
        total = sum(counts.values())
        count_parts = []
        if counts.get("gmail", 0):
            count_parts.append(f"📧 {counts['gmail']} emails")
        if counts.get("calendar", 0):
            count_parts.append(f"📅 {counts['calendar']} meetings")
        if counts.get("vault", 0) or counts.get("granola", 0):
            notes = counts.get("vault", 0) + counts.get("granola", 0)
            count_parts.append(f"📝 {notes} notes")

        last_str = ""
        if last:
            days_ago = (datetime.now(timezone.utc) - _make_aware(last.timestamp)).days
            if days_ago == 0:
                last_str = "today"
            elif days_ago == 1:
                last_str = "yesterday"
            else:
                last_str = f"{days_ago} days ago"

        lines = [
            f"**Summary:** {total} interactions | Last: {last_str}",
            " | ".join(count_parts),
            "",
            "### Recent Activity",
        ]

        # Add individual interactions
        for interaction in interactions[:20]:  # Cap at 20 for display
            date_str = interaction.timestamp.strftime("%b %d")
            badge = interaction.source_badge

            if interaction.source_link:
                if interaction.source_type in ("vault", "granola"):
                    lines.append(
                        f"- {badge} {date_str}: {interaction.title} — [[{interaction.title}]]"
                    )
                else:
                    lines.append(
                        f"- {badge} {date_str}: {interaction.title} — [View]({interaction.source_link})"
                    )
            else:
                lines.append(f"- {badge} {date_str}: {interaction.title}")

        return "\n".join(lines)


# Singleton instance
_interaction_store: Optional[InteractionStore] = None


def get_interaction_store(db_path: Optional[str] = None) -> InteractionStore:
    """
    Get or create the singleton InteractionStore.

    Args:
        db_path: Path to SQLite database

    Returns:
        InteractionStore instance
    """
    global _interaction_store
    if _interaction_store is None:
        _interaction_store = InteractionStore(db_path)
    return _interaction_store


# Factory functions for creating interactions from different sources


def create_gmail_interaction(
    person_id: str,
    message_id: str,
    subject: str,
    timestamp: datetime,
    snippet: Optional[str] = None,
) -> Interaction:
    """
    Create an interaction from a Gmail message.

    Args:
        person_id: PersonEntity ID
        message_id: Gmail message ID
        subject: Email subject line
        timestamp: Email date
        snippet: First part of email body

    Returns:
        Interaction ready to be stored
    """
    return Interaction(
        id=str(uuid.uuid4()),
        person_id=person_id,
        timestamp=timestamp,
        source_type="gmail",
        title=subject,
        snippet=snippet[:InteractionConfig.SNIPPET_LENGTH] if snippet else None,
        source_link=build_gmail_link(message_id),
        source_id=message_id,
    )


def create_calendar_interaction(
    person_id: str,
    event_id: str,
    title: str,
    timestamp: datetime,
    snippet: Optional[str] = None,
) -> Interaction:
    """
    Create an interaction from a Calendar event.

    Args:
        person_id: PersonEntity ID
        event_id: Calendar event ID
        title: Event title
        timestamp: Event start time
        snippet: Event description

    Returns:
        Interaction ready to be stored
    """
    return Interaction(
        id=str(uuid.uuid4()),
        person_id=person_id,
        timestamp=timestamp,
        source_type="calendar",
        title=title,
        snippet=snippet[:InteractionConfig.SNIPPET_LENGTH] if snippet else None,
        source_link=build_calendar_link(event_id),
        source_id=event_id,
    )


def create_vault_interaction(
    person_id: str,
    file_path: str,
    title: str,
    timestamp: datetime,
    snippet: Optional[str] = None,
    is_granola: bool = False,
) -> Interaction:
    """
    Create an interaction from a vault note.

    Args:
        person_id: PersonEntity ID
        file_path: Path to the note file
        title: Note title (usually filename without .md)
        timestamp: Note date (from frontmatter or filename)
        snippet: First part of note content
        is_granola: Whether this is a Granola meeting note

    Returns:
        Interaction ready to be stored
    """
    return Interaction(
        id=str(uuid.uuid4()),
        person_id=person_id,
        timestamp=timestamp,
        source_type="granola" if is_granola else "vault",
        title=title,
        snippet=snippet[:InteractionConfig.SNIPPET_LENGTH] if snippet else None,
        source_link=build_obsidian_link(file_path),
        source_id=file_path,
    )
