"""
Interaction Store for LifeOS People System v2.

Stores lightweight interaction records with links to sources.
Each interaction represents a single touchpoint (email, meeting, note mention).
"""
import bisect
import sqlite3
import threading
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

from config.settings import settings
from config.people_config import InteractionConfig

from api.utils.datetime_utils import make_aware as _make_aware
from api.services import backup_retention as _backup_retention

logger = logging.getLogger(__name__)

# Sentinel date for undated vault notes - allows them to appear in counts
# while being filterable in timeline views
UNDATED_SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Reject interactions with source_ids pointing to temp directories (test artifacts)
TEMP_PREFIXES = ('/tmp', '/private/var/folders', '/var/folders')

# Valid source types for interactions
VALID_SOURCE_TYPES = frozenset({
    "gmail", "calendar", "vault", "granola", "imessage",
    "whatsapp", "contacts", "phone", "photos", "slack",
})

# Chunk size for any SQL IN/NOT IN clause built from a caller-supplied list
# of ids. SQLite's default SQLITE_MAX_VARIABLE_NUMBER has been 32766 since
# 3.32.0 (999 was the default before that, and some builds raise it further
# still -- this one measures 250,000). 900 has no relationship to any of
# those numbers; it's a deliberately conservative, portable chunk size that
# stays safely under all of them without detecting the compiled-in limit at
# runtime. Mirrored in api/services/person_entity.py's PersonEntityStore
# (kept as a separate constant there -- no shared import for one number).
SQL_IN_CLAUSE_CHUNK_SIZE = 900

# Reasonable timestamp bounds
_MIN_TIMESTAMP = datetime(2000, 1, 1, tzinfo=timezone.utc)
_MAX_FUTURE_DAYS = 90  # Calendar events can be up to ~30 days out; allow margin

# Opening of the empty-history message. Callers detect emptiness by this prefix
# rather than by an exact string, so the window named after it can vary.
NO_INTERACTIONS_PREFIX = "_No interactions found"


def format_window_label(days_back: Optional[int]) -> str:
    """Human phrasing for a lookback window, for messages that must name it.

    None means the store's default window (InteractionConfig.DEFAULT_WINDOW_DAYS,
    which spans the whole indexed period), so it is described as such rather than
    as "the specified time period" — a phrase that tells a reader nothing.
    """
    if days_back is None:
        return "the full history on record"
    return f"the last {days_back} days"


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

    def validate(self) -> None:
        """Validate required fields and value constraints.

        Raises:
            ValueError: If any field fails validation.
        """
        if not self.person_id:
            raise ValueError("Interaction.person_id is required")

        if not self.source_type:
            raise ValueError("Interaction.source_type is required")
        if self.source_type not in VALID_SOURCE_TYPES:
            raise ValueError(
                f"Interaction.source_type {self.source_type!r} not in {sorted(VALID_SOURCE_TYPES)}"
            )

        if self.timestamp and self.timestamp != UNDATED_SENTINEL:
            ts = _make_aware(self.timestamp)
            if ts < _MIN_TIMESTAMP:
                raise ValueError(f"Interaction.timestamp too old: {self.timestamp}")
            max_future = datetime.now(timezone.utc) + timedelta(days=_MAX_FUTURE_DAYS)
            if ts > max_future:
                raise ValueError(f"Interaction.timestamp is in the future: {self.timestamp}")

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


# Backup retention is owned by ``api.services.backup_retention`` so other
# SQLite stores (crm.db, sync_health.db, …) can adopt the same policy
# without copy-pasting the bucket math. See issue #227. The wrappers
# below are kept so legacy test code that imported the private helpers
# from this module keeps working.
_BACKUP_DB_BASENAME = "interactions.db"


def _backup_filename(ts: datetime) -> str:
    return _backup_retention.backup_filename(_BACKUP_DB_BASENAME, ts)


def _parse_backup_timestamp(name: str) -> Optional[datetime]:
    return _backup_retention.parse_backup_timestamp(name, _BACKUP_DB_BASENAME)


def _prune_backups(backup_dir: Path, now: Optional[datetime] = None) -> list[Path]:
    return _backup_retention.prune(backup_dir, _BACKUP_DB_BASENAME, now=now)


class InteractionStore:
    """
    SQLite-backed interaction storage.

    Manages interaction records with efficient queries by person and time range.
    """

    def __init__(self, db_path: Optional[str] = None, strict: bool = True):
        """
        Initialize interaction store.

        Args:
            db_path: Path to SQLite database (default from settings)
            strict: If True, validate person_id exists before inserting.
                    Set False in tests that use fake person_ids.
        """
        self.db_path = db_path or get_interaction_db_path()
        self._strict = strict
        self._init_db()

    def _init_db(self):
        """Create database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
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

            # Migration: Enforce UNIQUE(source_type, source_id) at the DB level
            unique_idx_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_interactions_source_unique'"
            ).fetchone()
            if not unique_idx_exists:
                # Fix existing photos rows: scope source_id by person_id
                updated = conn.execute("""
                    UPDATE interactions
                    SET source_id = source_id || ':' || person_id
                    WHERE source_type = 'photos'
                      AND source_id IS NOT NULL
                      AND source_id NOT LIKE '%:%'
                """).rowcount
                if updated:
                    logger.info("Migration: scoped %d photos source_ids by person_id", updated)

                # Deduplicate: keep first-inserted row per (source_type, source_id)
                deleted = conn.execute("""
                    DELETE FROM interactions
                    WHERE source_id IS NOT NULL
                      AND rowid NOT IN (
                          SELECT MIN(rowid)
                          FROM interactions
                          WHERE source_id IS NOT NULL
                          GROUP BY source_type, source_id
                      )
                """).rowcount
                if deleted:
                    logger.info("Migration: removed %d duplicate interactions", deleted)

                # Replace old non-unique index with UNIQUE index
                conn.execute("DROP INDEX IF EXISTS idx_interactions_source")
                conn.execute("""
                    CREATE UNIQUE INDEX idx_interactions_source_unique
                    ON interactions(source_type, source_id)
                """)
                logger.info("Migration: created UNIQUE index idx_interactions_source_unique")

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
        interaction.validate()

        # Guard: reject interactions with temp-dir source_ids (test artifacts)
        if interaction.source_id and any(interaction.source_id.startswith(p) for p in TEMP_PREFIXES):
            logger.warning("Skipping interaction with temp-dir source_id: %s", interaction.source_id[:80])
            return interaction

        # Follow merge chain to get the canonical person ID
        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()
        resolved_person_id = person_store.get_canonical_id(interaction.person_id)

        if self._strict and person_store.get_by_id(resolved_person_id) is None:
            raise ValueError(
                f"Cannot add interaction: person_id '{resolved_person_id}' does not exist"
            )

        conn = self._get_connection()
        try:
            # If the existing row carries the 1970 UNDATED_SENTINEL timestamp
            # but we now have a real date (from the indexer's mtime fallback or
            # a newly-extractable filename date), upgrade the timestamp in place.
            # All other columns stay frozen to avoid stomping legitimate edits.
            cursor = conn.execute(
                """
                INSERT INTO interactions
                (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at,
                 source_account, attendee_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_id) DO UPDATE SET
                    timestamp = excluded.timestamp
                WHERE interactions.timestamp = ? AND excluded.timestamp != ?
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
                    interaction.source_account,
                    interaction.attendee_count,
                    UNDATED_SENTINEL.isoformat(),
                    UNDATED_SENTINEL.isoformat(),
                ),
            )
            if cursor.rowcount == 0 and interaction.source_id is not None:
                logger.debug("Duplicate interaction skipped: %s/%s", interaction.source_type, interaction.source_id)
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

    def _prepare_rows(
        self, interactions: list[Interaction]
    ) -> tuple[list[tuple], set[str]]:
        """
        Resolve merge chains, filter temp artifacts, and build INSERT tuples.

        Returns:
            (rows, affected_person_ids) — rows ready for executemany,
            and the set of canonical person IDs touched.
        """
        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()

        rows = []
        affected_person_ids: set[str] = set()
        for interaction in interactions:
            interaction.validate()

            if interaction.source_id and any(
                interaction.source_id.startswith(p) for p in TEMP_PREFIXES
            ):
                continue

            resolved_id = person_store.get_canonical_id(interaction.person_id)
            if self._strict and person_store.get_by_id(resolved_id) is None:
                raise ValueError(
                    f"Cannot add interaction: person_id '{resolved_id}' does not exist"
                )

            rows.append((
                interaction.id,
                resolved_id,
                interaction.timestamp.isoformat(),
                interaction.source_type,
                interaction.title,
                interaction.snippet,
                interaction.source_link,
                interaction.source_id,
                interaction.created_at.isoformat(),
                interaction.source_account,
                interaction.attendee_count,
            ))
            affected_person_ids.add(resolved_id)

        return rows, affected_person_ids

    def batch_add(self, interactions: list[Interaction]) -> dict:
        """
        Add multiple interactions in a single transaction.

        Uses INSERT OR IGNORE for natural deduplication via the UNIQUE
        constraint on (source_type, source_id). Resolves merge chains
        before inserting.

        Args:
            interactions: List of Interaction objects to add.

        Returns:
            Dict with 'added' (int), 'skipped' (int),
            and 'affected_person_ids' (set of canonical person IDs).
            Caller should call refresh_person_stats() with affected_person_ids.
        """
        if not interactions:
            return {"added": 0, "skipped": 0, "affected_person_ids": set()}

        rows, affected_person_ids = self._prepare_rows(interactions)

        if not rows:
            return {"added": 0, "skipped": 0, "affected_person_ids": set()}

        conn = self._get_connection()
        try:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO interactions
                (id, person_id, timestamp, source_type, title, snippet,
                 source_link, source_id, created_at, source_account, attendee_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_id) DO NOTHING
                """,
                rows,
            )
            added = conn.total_changes - before
            conn.commit()
            return {
                "added": added,
                "skipped": len(rows) - added,
                "affected_person_ids": affected_person_ids,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def atomic_replace(self, source_type: str, interactions: list[Interaction]) -> dict:
        """
        Replace all interactions for a source_type in a single transaction.

        Deletes all existing interactions of the given source_type, then
        inserts the new ones. If the insert fails, the delete is also
        rolled back — no partial state.

        Args:
            source_type: The source type to replace (e.g., "vault", "granola").
            interactions: Complete set of interactions for this source type.
                All interactions must have source_type matching the parameter.

        Returns:
            Dict with 'deleted' (int), 'added' (int),
            and 'affected_person_ids' (set of canonical person IDs).
            Caller should call refresh_person_stats() with affected_person_ids.

        Raises:
            ValueError: If any interaction has a mismatched source_type.
        """
        mismatched = [i for i in interactions if i.source_type != source_type]
        if mismatched:
            raise ValueError(
                f"atomic_replace('{source_type}') received {len(mismatched)} "
                f"interactions with mismatched source_type"
            )

        rows, affected_person_ids = self._prepare_rows(interactions)

        conn = self._get_connection()
        try:
            # Collect person_ids being deleted so their stats get refreshed too
            cursor = conn.execute(
                "SELECT DISTINCT person_id FROM interactions WHERE source_type = ?",
                (source_type,),
            )
            for row in cursor:
                affected_person_ids.add(row[0])

            delete_cursor = conn.execute(
                "DELETE FROM interactions WHERE source_type = ?",
                (source_type,),
            )
            deleted = delete_cursor.rowcount

            added = 0
            if rows:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT INTO interactions
                    (id, person_id, timestamp, source_type, title, snippet,
                     source_link, source_id, created_at, source_account, attendee_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_type, source_id) DO NOTHING
                    """,
                    rows,
                )
                added = conn.total_changes - before

            conn.commit()
            return {
                "deleted": deleted,
                "added": added,
                "affected_person_ids": affected_person_ids,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
                  AND NOT (source_type = 'calendar' AND COALESCE(attendee_count, 0) > ?)
                GROUP BY source_type, subtype, source_account
            """,
                (
                    person_id,
                    cutoff.isoformat(),
                    InteractionConfig.MASS_MEETING_ATTENDEE_LIMIT,
                ),
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

    def get_last_interaction_by_source_batch(
        self, person_ids: list[str]
    ) -> dict[str, dict[str, datetime]]:
        """
        Batched version of get_last_interaction_by_source() for many people.

        Runs one `GROUP BY person_id, source_type` query per chunk of ids
        instead of one query per person, so callers that need this for a
        page of search/list results don't pay an N+1 cost. See
        SQL_IN_CLAUSE_CHUNK_SIZE for why the chunk size is 900.

        Args:
            person_ids: PersonEntity IDs to fetch recency for.

        Returns:
            Dict mapping person_id to the same per-source dict that
            get_last_interaction_by_source() would return for that person.
            IDs with no interactions are simply absent.
        """
        if not person_ids:
            return {}

        # De-dupe while preserving determinism, then chunk the IN clause.
        unique_ids = list(dict.fromkeys(person_ids))
        result: dict[str, dict[str, datetime]] = {}
        now = datetime.now(timezone.utc).isoformat()

        conn = self._get_connection()
        try:
            chunk_size = SQL_IN_CLAUSE_CHUNK_SIZE
            for i in range(0, len(unique_ids), chunk_size):
                chunk = unique_ids[i:i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"""
                    SELECT person_id, source_type, MAX(timestamp) as last_ts
                    FROM interactions
                    WHERE person_id IN ({placeholders}) AND timestamp <= ?
                    GROUP BY person_id, source_type
                    """,
                    chunk + [now],
                )
                for row in cursor.fetchall():
                    person_id = row[0]
                    source_type = row[1]
                    ts_str = row[2]
                    if not ts_str:
                        continue
                    dt = _make_aware(datetime.fromisoformat(ts_str.replace("Z", "+00:00")))
                    result.setdefault(person_id, {})[source_type] = dt
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

    # ---- Aggregate queries for the Me/Family dashboards (#871) ----
    #
    # These return plain tuples/dicts instead of hydrated Interaction objects,
    # so a 10-year dashboard window doesn't require constructing (and
    # datetime-parsing) hundreds of thousands of Python objects just to sum or
    # bucket them. All of them share `_range_predicate` for their WHERE
    # clause, which mirrors get_all_in_range's existing index-friendly
    # convention: bounds are compared as calendar-day strings against the raw
    # `timestamp` column (no timezone conversion — `timestamp >= 'YYYY-MM-DD'`
    # is a safe, sargable lower bound regardless of a row's own UTC offset,
    # since it can only ever be more inclusive than an exact-instant bound,
    # never less).
    #
    # Day-string bounds are exactly what get_all_in_range's callers already
    # relied on for their day-granular outputs (the heatmap, by_source,
    # by_month, by_circle, total_count). A few widgets (the 30-day top
    # contacts list, and the trend/health-period comparisons) apply a
    # sub-day-precision cutoff in the original code — `exact=True` adds an
    # additional `julianday(timestamp)` comparison (which SQLite evaluates
    # offset-aware, matching Python's aware-datetime comparisons) on top of a
    # one-day-wider version of the same string bound, so the
    # (person_id, timestamp) index can still narrow rows before the
    # unindexed julianday() expression runs on what's left.

    def _range_predicate(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        person_ids: Optional[Iterable[str]] = None,
        exclude_person_ids: Optional[Iterable[str]] = None,
        source_types: Optional[Iterable[str]] = None,
        gmail_sent_only: bool = False,
        exact: bool = False,
        end_inclusive: bool = True,
        pool_start_date: Optional[datetime] = None,
        pool_end_date: Optional[datetime] = None,
    ) -> tuple[str, list]:
        """
        Build a WHERE fragment (no leading WHERE) and its params, shared by
        the aggregate queries below. See the module note above this method
        for the day-string-vs-exact-instant tradeoff `exact` controls.

        `pool_start_date`/`pool_end_date` AND in an *additional*, always
        day-string (never `exact`) bound, independent of `start_date`/
        `end_date`/`exact` above. Use this when a caller needs a sub-day-
        precision window (`exact=True`) that must still be clipped to a
        wider day-granular "pool" — e.g. a trend-period comparison whose
        window can be longer than half of the dashboard's own `days_back`.
        The original Python implementation combined exactly these two
        things: an outer `all_interactions` fetch bounded to `days_back`
        (day-string, matching `get_all_in_range`), then a precise per-item
        comparison within it — so a trend window that reached further back
        than `days_back` was silently clipped by the outer fetch. Passing
        only `start_date`/`end_date` with `exact=True` does NOT reproduce
        that clipping (its own bound is a same-or-wider index-narrowing
        margin around the exact window, not the outer pool), which is what
        made `warming`/`cooling` diverge from the original whenever
        `2 * trend_days > days_back` (#897 review finding 1).

        Note on the end-date convention (day-string bounds, `pool_end_date`
        included): comparing an ISO timestamp string (`...T23:59:59+00:00`)
        against `'<end> 23:59:59'` (a space, not `T`, before the time)
        lexically excludes every row dated exactly on `end_date`'s calendar
        day — `'T' > ' '` in ASCII, so any same-day row sorts as "greater
        than" the bound. This is `get_all_in_range`'s existing convention
        (unchanged by this method), not something introduced here; the
        practical effect is that a window's own end day never contributes
        to the non-`exact` aggregates. Fixing it is out of scope for this
        change (#897 review finding 8) — noted so the next reader doesn't
        trust the boundary as exact.
        """
        clauses: list[str] = []
        params: list = []

        if start_date is not None:
            floor_dt = (start_date - timedelta(days=1)) if exact else start_date
            clauses.append("timestamp >= ?")
            params.append(floor_dt.strftime('%Y-%m-%d'))
            if exact:
                clauses.append("julianday(timestamp) >= julianday(?)")
                params.append(start_date.isoformat())

        if end_date is not None:
            ceil_dt = (end_date + timedelta(days=1)) if exact else end_date
            clauses.append("timestamp <= ?")
            params.append(ceil_dt.strftime('%Y-%m-%d 23:59:59'))
            if exact:
                op = "<=" if end_inclusive else "<"
                clauses.append(f"julianday(timestamp) {op} julianday(?)")
                params.append(end_date.isoformat())

        if pool_start_date is not None:
            clauses.append("timestamp >= ?")
            params.append(pool_start_date.strftime('%Y-%m-%d'))

        if pool_end_date is not None:
            clauses.append("timestamp <= ?")
            params.append(pool_end_date.strftime('%Y-%m-%d 23:59:59'))

        if person_ids is not None:
            ids = list(person_ids)
            if not ids:
                # An explicit empty id set matches nothing (avoids `IN ()`).
                clauses.append("1=0")
            else:
                clauses.append(f"person_id IN ({','.join('?' * len(ids))})")
                params.extend(ids)

        if exclude_person_ids:
            ids = list(exclude_person_ids)
            clauses.append(f"person_id NOT IN ({','.join('?' * len(ids))})")
            params.extend(ids)

        if source_types:
            types = list(source_types)
            clauses.append(f"source_type IN ({','.join('?' * len(types))})")
            params.extend(types)

        if gmail_sent_only:
            # Matches the "Me" dashboard's historical rule of only counting
            # sent email (title prefix "→ "), never received/cc'd email.
            clauses.append("(source_type != 'gmail' OR title LIKE '→%')")

        return (" AND ".join(clauses) if clauses else "1=1"), params

    def get_span(
        self,
        person_ids: Optional[Iterable[str]] = None,
        exclude_person_ids: Optional[Iterable[str]] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Earliest and latest interaction timestamps (as stored, ISO strings),
        optionally restricted to (or excluding) a set of person ids. Excludes
        UNDATED_SENTINEL rows (undated vault notes deliberately stored at
        1970-01-01, exempted from the normal minimum-timestamp validation) —
        without that floor, one such note would make the span (and so the Me
        dashboard's heatmap window) span back to 1970 regardless of when
        real interactions actually started.

        Used to size the Me dashboard's default heatmap window from the
        actual span of data instead of requesting a fixed 10 years and
        shrinking the display afterward.

        Implemented as two `ORDER BY ... LIMIT 1` queries rather than
        `SELECT MIN(timestamp), MAX(timestamp)`: SQLite can answer each via a
        single index seek that stops at the first row satisfying the
        (typically large, e.g. thousands of hidden/peripheral ids)
        `exclude_person_ids` filter, whereas combining MIN and MAX into one
        aggregate query forces a full-table scan on this schema (measured
        ~300ms+ vs ~5ms on the production dataset with a large exclude list).
        """
        floor = _MIN_TIMESTAMP
        where, params = self._range_predicate(
            start_date=floor, person_ids=person_ids, exclude_person_ids=exclude_person_ids,
        )
        conn = self._get_connection()
        try:
            earliest_row = conn.execute(
                f"SELECT timestamp FROM interactions WHERE {where} ORDER BY timestamp ASC LIMIT 1",
                params,
            ).fetchone()
            latest_row = conn.execute(
                f"SELECT timestamp FROM interactions WHERE {where} ORDER BY timestamp DESC LIMIT 1",
                params,
            ).fetchone()
            earliest = earliest_row[0] if earliest_row else None
            latest = latest_row[0] if latest_row else None
            return (earliest, latest)
        finally:
            conn.close()

    def get_daily_source_counts(
        self,
        start_date: datetime,
        end_date: datetime,
        person_ids: Optional[Iterable[str]] = None,
        exclude_person_ids: Optional[Iterable[str]] = None,
        source_types: Optional[Iterable[str]] = None,
        gmail_sent_only: bool = False,
    ) -> list[tuple[str, str, int]]:
        """
        Grouped (day, source_type) -> count within [start_date, end_date].

        `day` is the literal "YYYY-MM-DD" prefix of the stored timestamp
        string (via `substr`, not SQLite's `date()`, which would convert to
        UTC first) — this matches the historical behavior of parsing the ISO
        string into an aware datetime and formatting its own date fields
        as-is, which never shifts across the stored offset.
        """
        where, params = self._range_predicate(
            start_date, end_date, person_ids, exclude_person_ids, source_types, gmail_sent_only,
        )
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                f"""
                SELECT substr(timestamp, 1, 10) as day, source_type, COUNT(*) as cnt
                FROM interactions
                WHERE {where}
                GROUP BY day, source_type
                """,
                params,
            )
            return [(row[0], row[1], row[2]) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_daily_person_source_counts(
        self,
        start_date: datetime,
        end_date: datetime,
        person_ids: Optional[Iterable[str]] = None,
        exclude_person_ids: Optional[Iterable[str]] = None,
        source_types: Optional[Iterable[str]] = None,
        gmail_sent_only: bool = False,
    ) -> list[tuple[str, str, str, int]]:
        """
        Grouped (day, person_id, source_type) -> count within [start_date,
        end_date] — a single pass covering what would otherwise be separate
        full-window scans for day-level, person-level, and per-month
        breakdowns.

        Worth it whenever a caller needs day-level, person-level, AND
        per-source breakdowns from the SAME window: on a real dataset where a
        large fraction of people are excluded (e.g. many peripheral
        contacts), passing that exclusion here as `exclude_person_ids` still
        costs a per-row `NOT IN` membership check across the whole scanned
        range (measured ~250-400ms for a 10-year window on production,
        regardless of which columns are grouped — the exclusion check
        dominates, not the grouping). A caller that's going to iterate the
        (already much smaller, grouped) result in Python anyway should
        usually skip exclude_person_ids/person_ids here and filter with a
        Python `set` instead, which is a hash lookup per grouped row rather
        than a per-raw-row SQL scan.
        """
        where, params = self._range_predicate(
            start_date, end_date, person_ids, exclude_person_ids, source_types, gmail_sent_only,
        )
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                f"""
                SELECT substr(timestamp, 1, 10) as day, person_id, source_type, COUNT(*) as cnt
                FROM interactions
                WHERE {where}
                GROUP BY day, person_id, source_type
                """,
                params,
            )
            return [(row[0], row[1], row[2], row[3]) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_person_counts(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        person_ids: Optional[Iterable[str]] = None,
        exclude_person_ids: Optional[Iterable[str]] = None,
        source_types: Optional[Iterable[str]] = None,
        gmail_sent_only: bool = False,
        exact: bool = False,
        end_inclusive: bool = True,
        pool_start_date: Optional[datetime] = None,
        pool_end_date: Optional[datetime] = None,
    ) -> dict[str, int]:
        """
        Grouped person_id -> count within [start_date, end_date].

        Pass exact=True for sub-day-precision windows (e.g. the "last 30
        days" top-contacts cutoff, or a trend comparison anchored to the
        exact request time) — see `_range_predicate`. `end_inclusive=False`
        excludes the end instant itself, for a "previous period" bucket that
        must not double-count the instant where the "recent period" begins.
        `end_date=None` omits the exact upper bound entirely (e.g. "last 30
        days" has no upper cutoff of its own beyond the outer window) —
        pass `pool_start_date`/`pool_end_date` for that outer window, which
        AND in as day-string bounds regardless of `exact`.
        """
        where, params = self._range_predicate(
            start_date, end_date, person_ids, exclude_person_ids, source_types,
            gmail_sent_only, exact=exact, end_inclusive=end_inclusive,
            pool_start_date=pool_start_date, pool_end_date=pool_end_date,
        )
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                f"SELECT person_id, COUNT(*) FROM interactions WHERE {where} GROUP BY person_id",
                params,
            )
            return {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            conn.close()

    def get_bucketed_counts(
        self,
        time_points: list[datetime],
        person_ids: Optional[Iterable[str]] = None,
        exclude_person_ids: Optional[Iterable[str]] = None,
        source_types: Optional[Iterable[str]] = None,
        gmail_sent_only: bool = False,
        pool_start_date: Optional[datetime] = None,
        pool_end_date: Optional[datetime] = None,
    ) -> list[int]:
        """
        Counts interactions into the periods bounded by consecutive
        `time_points`: bucket i is `(prev, time_points[i]]`, where `prev` is
        `time_points[i-1]` or, for bucket 0, one inter-point interval before
        `time_points[0]` — the same rule `_bucket_counts_by_period`
        (api/routes/crm.py) implements for a list of already-hydrated items.

        Fetches only `julianday(timestamp)` for the matching rows (no
        person_id, no other columns) and buckets them with `bisect` in
        Python, rather than one `SUM(CASE ...)` expression per bucket in
        SQL: measured on the production dataset, a single combined
        multi-bucket CASE query still has to evaluate every bucket's
        `julianday()` comparison against every matching row (13 buckets x
        ~150,000 rows for the health-score widget's "top 25 by relationship
        strength" population), whereas computing `julianday()` once per row
        and then binary-searching a handful of boundaries is measurably
        faster (~320ms vs ~490ms) despite transferring the (still small
        relative to a full hydration) row set instead of none.

        Returns `[]` for an empty `time_points`; otherwise one int per
        bucket, in `time_points` order.
        """
        if not time_points:
            return []

        bounds: list[tuple[datetime, datetime]] = []
        for i, point in enumerate(time_points):
            if i == 0:
                interval = (
                    (time_points[1] - time_points[0])
                    if len(time_points) > 1 else timedelta(days=14)
                )
                prev = point - interval
            else:
                prev = time_points[i - 1]
            bounds.append((prev, point))

        where, where_params = self._range_predicate(
            None, None, person_ids, exclude_person_ids, source_types, gmail_sent_only,
            pool_start_date=pool_start_date, pool_end_date=pool_end_date,
        )

        # One query to resolve every distinct boundary datetime to SQLite's
        # own julianday() value (so bucket comparisons below use the exact
        # same conversion as the row fetch, not a Python reimplementation
        # of it), instead of one query per boundary.
        boundary_dts: list[datetime] = []
        seen: set[str] = set()
        for prev, point in bounds:
            for dt in (prev, point):
                key = dt.isoformat()
                if key not in seen:
                    seen.add(key)
                    boundary_dts.append(dt)

        conn = self._get_connection()
        try:
            boundary_exprs = ", ".join("julianday(?)" for _ in boundary_dts)
            boundary_row = conn.execute(
                f"SELECT {boundary_exprs}",
                [dt.isoformat() for dt in boundary_dts],
            ).fetchone()
            jd_for = {dt.isoformat(): boundary_row[i] for i, dt in enumerate(boundary_dts)}

            rows = conn.execute(
                f"SELECT julianday(timestamp) FROM interactions WHERE {where}",
                where_params,
            ).fetchall()
        finally:
            conn.close()

        jds = sorted(row[0] for row in rows)
        counts = []
        for prev, point in bounds:
            lo = bisect.bisect_right(jds, jd_for[prev.isoformat()])
            hi = bisect.bisect_right(jds, jd_for[point.isoformat()])
            counts.append(hi - lo)
        return counts

    def get_person_julianday_timestamps(
        self,
        start_date: datetime,
        end_date: datetime,
        person_ids: Optional[Iterable[str]] = None,
        exclude_person_ids: Optional[Iterable[str]] = None,
    ) -> list[tuple[str, float]]:
        """
        (person_id, julianday(timestamp)) tuples within [start_date,
        end_date] — a covering-index query (person_id + timestamp only, no
        source_type) for widgets that need exact per-interaction timestamps
        for a specific set of people but don't care what kind of
        interaction it was (e.g. neglected-contacts' median-gap
        calculation). Returns julian-day floats rather than parsed
        datetimes so gap arithmetic is plain float subtraction (1.0 == one
        day) instead of building a datetime per row; use get_julianday(dt)
        to get a directly-comparable value for "now" or any other cutoff.
        """
        where, params = self._range_predicate(start_date, end_date, person_ids, exclude_person_ids)
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                f"SELECT person_id, julianday(timestamp) FROM interactions WHERE {where}",
                params,
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_julianday(self, dt: datetime) -> float:
        """
        Julian day number for a given datetime, computed the same way
        SQLite's julianday() computes it for a UTC or UTC-offset ISO8601
        string (days since the Julian epoch, with 1970-01-01T00:00:00Z =
        2440587.5) — avoids opening a connection to evaluate one scalar
        (#897 review nit 3). A naive dt is treated as UTC, matching
        SQLite's own treatment of an offset-less timestamp string.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() / 86400.0 + 2440587.5

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
        Create a verified backup of interactions.db and prune old ones.

        Returns:
            Path to backup file, or None if there is no db to back up or the
            snapshot failed verification (in which case nothing is pruned).
        """
        try:
            # Delegates to the shared helper so interactions.db gets the same
            # treatment as every other store: a WAL-safe online-backup snapshot
            # rather than a file copy, and verification before anything older
            # is discarded.
            return _backup_retention.create_snapshot(
                Path(self.db_path),
                Path(settings.backup_path),
                _BACKUP_DB_BASENAME,
            )
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
        person_ids: Optional[list[str]] = None,
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
            person_ids: If given, restrict to interactions with these person
                        ids (SQL `person_id IN (...)`) instead of everyone —
                        used by the Family dashboard so it never loads
                        interactions for people outside the selection.
            exclude_person_ids: Person IDs to exclude (e.g., self)
            source_type: Filter by source type. Supports comma-separated values
                         (e.g., "imessage,whatsapp" for messages).
            limit: Maximum number of interactions to return
            specific_date: Filter to a specific date (YYYY-MM-DD), overrides start/end

        Returns:
            List of interactions in the date range

        Note: `end_date`'s own calendar day is silently excluded — the
        `timestamp <= '<end> 23:59:59'` bound compares against a string with
        a space before the time, and every stored timestamp has a `T`
        there instead, which sorts as "greater than" for any row on that
        day. Pre-existing, not something to rely on as exact — see
        `_range_predicate`'s docstring for the full explanation (that
        helper reproduces this same convention for the newer aggregate
        queries below).
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

            # Restrict to specific person IDs if provided
            if person_ids is not None:
                if not person_ids:
                    query += " AND 1=0"
                else:
                    placeholders = ','.join('?' * len(person_ids))
                    query += f" AND person_id IN ({placeholders})"
                    params.extend(person_ids)

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
            Formatted markdown string. An empty result names the window it
            searched (see NO_INTERACTIONS_PREFIX) — callers must not restate it
            as "no history", which claims more than the window established.
        """
        interactions = self.get_for_person(person_id, days_back, limit)
        counts = self.get_interaction_counts(person_id, days_back)
        last = self.get_last_interaction(person_id)

        if not interactions:
            return f"{NO_INTERACTIONS_PREFIX} in {format_window_label(days_back)}._"

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
# #868 moved CRM/people/photos handlers off the event loop and onto worker
# threads, so two first-requests after a restart can now race this
# check-and-set. Double-checked locking: the lock is only taken while
# _interaction_store is still None, so it costs nothing once constructed.
_interaction_store_lock = threading.Lock()


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
        with _interaction_store_lock:
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
