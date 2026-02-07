"""
SourceEntity - Raw observation records from various data sources.

Part of the two-tier CRM data model:
- SourceEntity: Immutable raw observations (this file)
- CanonicalPerson: Unified person records (person_entity.py)

SourceEntities preserve the original data from each source and track
their linkage to canonical person records with confidence scores.
"""
import sqlite3
import json
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from config.settings import settings
from api.utils.datetime_utils import make_aware as _make_aware
from api.utils.db_paths import get_crm_db_path

logger = logging.getLogger(__name__)


# Valid source types
SOURCE_TYPES = {
    "gmail",
    "calendar",
    "slack",
    "imessage",
    "whatsapp",
    "signal",
    "contacts",
    "phone_contacts",
    "linkedin",
    "vault",
    "granola",
    "phone_call",
    "phone",
    "photos",
}

# Link status values
LINK_STATUS_AUTO = "auto"  # Automatically linked
LINK_STATUS_CONFIRMED = "confirmed"  # User confirmed the link
LINK_STATUS_REJECTED = "rejected"  # User rejected the link


@dataclass
class SourceEntity:
    """
    A raw observation from a data source.

    Represents a single instance where we observed information about a person
    from a specific source. Multiple SourceEntities may link to the same
    CanonicalPerson.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_type: str = ""  # gmail, calendar, slack, imessage, etc.
    source_id: Optional[str] = None  # Unique ID within the source

    # Observed data (may be partial/incomplete)
    observed_name: Optional[str] = None
    observed_email: Optional[str] = None
    observed_phone: Optional[str] = None

    # Additional source-specific metadata (JSON)
    metadata: dict = field(default_factory=dict)

    # Link to canonical person
    canonical_person_id: Optional[str] = None
    link_confidence: float = 0.0  # 0.0-1.0
    link_status: str = LINK_STATUS_AUTO  # auto, confirmed, rejected
    linked_at: Optional[datetime] = None

    # Timestamps
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        """Validate source type."""
        if self.source_type and self.source_type not in SOURCE_TYPES:
            logger.warning(f"Unknown source type: {self.source_type}")

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        data = asdict(self)
        # Convert datetime to ISO format strings
        if self.observed_at:
            data["observed_at"] = self.observed_at.isoformat()
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.linked_at:
            data["linked_at"] = self.linked_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SourceEntity":
        """Create SourceEntity from dict."""
        # Parse datetime strings
        if data.get("observed_at") and isinstance(data["observed_at"], str):
            data["observed_at"] = _make_aware(datetime.fromisoformat(data["observed_at"]))
        if data.get("created_at") and isinstance(data["created_at"], str):
            data["created_at"] = _make_aware(datetime.fromisoformat(data["created_at"]))
        if data.get("linked_at") and isinstance(data["linked_at"], str):
            data["linked_at"] = _make_aware(datetime.fromisoformat(data["linked_at"]))
        return cls(**data)

    @classmethod
    def from_row(cls, row: tuple) -> "SourceEntity":
        """Create SourceEntity from SQLite row."""
        # Row order: id, source_type, source_id, observed_name, observed_email,
        #            observed_phone, metadata, canonical_person_id, link_confidence,
        #            link_status, linked_at, observed_at, created_at

        observed_at = datetime.fromisoformat(row[11]) if row[11] else datetime.now(timezone.utc)
        created_at = datetime.fromisoformat(row[12]) if row[12] else datetime.now(timezone.utc)
        linked_at = datetime.fromisoformat(row[10]) if row[10] else None

        return cls(
            id=row[0],
            source_type=row[1],
            source_id=row[2],
            observed_name=row[3],
            observed_email=row[4],
            observed_phone=row[5],
            metadata=json.loads(row[6]) if row[6] else {},
            canonical_person_id=row[7],
            link_confidence=row[8] or 0.0,
            link_status=row[9] or LINK_STATUS_AUTO,
            linked_at=_make_aware(linked_at),
            observed_at=_make_aware(observed_at),
            created_at=_make_aware(created_at),
        )

    @property
    def source_badge(self) -> str:
        """Get emoji badge for source type."""
        badges = {
            "gmail": "📧",
            "calendar": "📅",
            "slack": "💬",
            "imessage": "💬",
            "whatsapp": "💬",
            "signal": "💬",
            "contacts": "📱",
            "linkedin": "💼",
            "vault": "📝",
            "granola": "📝",
            "phone_call": "📞",
            "photos": "📷",
        }
        return badges.get(self.source_type, "📄")

    @property
    def is_linked(self) -> bool:
        """Check if this entity is linked to a canonical person."""
        return self.canonical_person_id is not None and self.link_status != LINK_STATUS_REJECTED

    @property
    def is_confirmed(self) -> bool:
        """Check if the link has been user-confirmed."""
        return self.link_status == LINK_STATUS_CONFIRMED


class SourceEntityStore:
    """
    SQLite-backed storage for SourceEntity records.

    Provides efficient queries for:
    - Finding entities by source type and ID
    - Finding all entities linked to a canonical person
    - Finding unlinked/pending entities
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the source entity store.

        Args:
            db_path: Path to SQLite database (default from settings)
        """
        self.db_path = db_path or get_crm_db_path()
        self._init_db()

    def _init_db(self):
        """Create database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS source_entities (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    observed_name TEXT,
                    observed_email TEXT,
                    observed_phone TEXT,
                    metadata TEXT,
                    canonical_person_id TEXT,
                    link_confidence REAL DEFAULT 0.0,
                    link_status TEXT DEFAULT 'auto',
                    linked_at TIMESTAMP,
                    observed_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_type, source_id)
                )
            """)

            # Index for finding entities by canonical person
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_entities_canonical
                ON source_entities(canonical_person_id)
            """)

            # Index for finding entities by source type
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_entities_source_type
                ON source_entities(source_type)
            """)

            # Index for finding unlinked entities
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_entities_unlinked
                ON source_entities(canonical_person_id) WHERE canonical_person_id IS NULL
            """)

            # Index for finding entities by email
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_entities_email
                ON source_entities(observed_email)
            """)

            # Index for finding entities by phone
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_entities_phone
                ON source_entities(observed_phone)
            """)

            # Index for time-based queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_entities_observed_at
                ON source_entities(observed_at DESC)
            """)

            # Add match_attempted_at and match_attempt_count columns for tracking
            # entities that failed resolution attempts (migration for existing DBs)
            try:
                conn.execute("""
                    ALTER TABLE source_entities
                    ADD COLUMN match_attempted_at TIMESTAMP
                """)
                logger.info("Added match_attempted_at column")
            except sqlite3.OperationalError:
                pass  # Column already exists

            try:
                conn.execute("""
                    ALTER TABLE source_entities
                    ADD COLUMN match_attempt_count INTEGER DEFAULT 0
                """)
                logger.info("Added match_attempt_count column")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Index for finding entities that need re-matching
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_entities_match_attempts
                ON source_entities(match_attempted_at, match_attempt_count)
                WHERE canonical_person_id IS NULL
            """)

            conn.commit()
            logger.info(f"Initialized source entity database at {self.db_path}")
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)

    def add(self, entity: SourceEntity, validate_person: bool = True) -> SourceEntity:
        """
        Add a new source entity.

        If entity has a canonical_person_id, validates the person exists and
        follows any merge chain to the canonical ID. This prevents orphaned
        source entities pointing to deleted or merged person IDs.

        Args:
            entity: SourceEntity to add
            validate_person: If True, validate and resolve person ID (default True)

        Returns:
            The added entity
        """
        # Validate and resolve person ID to prevent orphans
        if validate_person and entity.canonical_person_id:
            from api.services.person_entity import get_person_entity_store
            person_store = get_person_entity_store()

            # Follow merge chain to get canonical ID
            resolved_id = person_store.get_canonical_id(entity.canonical_person_id)

            # Verify the person actually exists
            person = person_store.get_by_id(resolved_id)
            if not person:
                logger.warning(
                    f"Cannot add source entity {entity.source_type}:{entity.source_id} - "
                    f"person {entity.canonical_person_id} not found (resolved: {resolved_id})"
                )
                # Clear the person link rather than creating an orphan
                entity.canonical_person_id = None
                entity.link_confidence = 0.0
                entity.linked_at = None
            else:
                # Use the resolved ID
                entity.canonical_person_id = resolved_id

        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO source_entities
                (id, source_type, source_id, observed_name, observed_email,
                 observed_phone, metadata, canonical_person_id, link_confidence,
                 link_status, linked_at, observed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entity.id,
                entity.source_type,
                entity.source_id,
                entity.observed_name,
                entity.observed_email,
                entity.observed_phone,
                json.dumps(entity.metadata) if entity.metadata else None,
                entity.canonical_person_id,
                entity.link_confidence,
                entity.link_status,
                entity.linked_at.isoformat() if entity.linked_at else None,
                entity.observed_at.isoformat(),
                entity.created_at.isoformat(),
            ))
            conn.commit()
            return entity
        finally:
            conn.close()

    def add_or_update(self, entity: SourceEntity, validate_person: bool = True) -> tuple[SourceEntity, bool]:
        """
        Add entity or update if source_type+source_id already exists.

        If entity has a canonical_person_id, validates the person exists and
        follows any merge chain to the canonical ID. This prevents orphaned
        source entities pointing to deleted or merged person IDs.

        Args:
            entity: SourceEntity to add/update
            validate_person: If True, validate and resolve person ID (default True)

        Returns:
            Tuple of (entity, was_created)
        """
        # Validate and resolve person ID to prevent orphans
        if validate_person and entity.canonical_person_id:
            from api.services.person_entity import get_person_entity_store
            person_store = get_person_entity_store()

            # Follow merge chain to get canonical ID
            resolved_id = person_store.get_canonical_id(entity.canonical_person_id)

            # Verify the person actually exists
            person = person_store.get_by_id(resolved_id)
            if not person:
                logger.warning(
                    f"Skipping source entity {entity.source_type}:{entity.source_id} - "
                    f"person {entity.canonical_person_id} not found (resolved: {resolved_id})"
                )
                # Don't create orphan - return existing if any, or a dummy result
                existing = self.get_by_source(entity.source_type, entity.source_id)
                if existing:
                    return existing, False
                # Clear the person link rather than creating an orphan
                entity.canonical_person_id = None
                entity.link_confidence = 0.0
                entity.linked_at = None
            else:
                # Use the resolved ID
                entity.canonical_person_id = resolved_id

        existing = self.get_by_source(entity.source_type, entity.source_id)
        if existing:
            # Update existing - preserve ID and creation timestamp
            entity.id = existing.id
            entity.created_at = existing.created_at
            self.update(entity)
            return entity, False

        return self.add(entity), True

    def update(self, entity: SourceEntity) -> SourceEntity:
        """
        Update an existing source entity.

        Args:
            entity: SourceEntity with updated data

        Returns:
            The updated entity
        """
        conn = self._get_connection()
        try:
            conn.execute("""
                UPDATE source_entities SET
                    source_type = ?,
                    source_id = ?,
                    observed_name = ?,
                    observed_email = ?,
                    observed_phone = ?,
                    metadata = ?,
                    canonical_person_id = ?,
                    link_confidence = ?,
                    link_status = ?,
                    linked_at = ?,
                    observed_at = ?
                WHERE id = ?
            """, (
                entity.source_type,
                entity.source_id,
                entity.observed_name,
                entity.observed_email,
                entity.observed_phone,
                json.dumps(entity.metadata) if entity.metadata else None,
                entity.canonical_person_id,
                entity.link_confidence,
                entity.link_status,
                entity.linked_at.isoformat() if entity.linked_at else None,
                entity.observed_at.isoformat(),
                entity.id,
            ))
            conn.commit()
            return entity
        finally:
            conn.close()

    def link_to_person(
        self,
        entity_id: str,
        canonical_person_id: str,
        confidence: float = 1.0,
        status: str = LINK_STATUS_AUTO,
    ) -> bool:
        """
        Link a source entity to a canonical person.

        Automatically follows merge chain - if the person_id was merged into
        another person, links to the surviving primary instead.

        IMPORTANT: Confirmed links are protected from being overwritten by auto
        resolution. This prevents sync scripts from undoing explicit user actions
        like split operations. To update a confirmed link, pass status='confirmed'.

        Args:
            entity_id: Source entity ID
            canonical_person_id: Canonical person ID (will be resolved through merge chain)
            confidence: Link confidence (0.0-1.0)
            status: Link status (auto, confirmed, rejected)

        Returns:
            True if updated, False if entity not found or link is protected
        """
        # Follow merge chain to get the canonical ID
        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()
        resolved_person_id = person_store.get_canonical_id(canonical_person_id)

        conn = self._get_connection()
        try:
            # Check if existing link is confirmed (protected from auto re-linking)
            if status != LINK_STATUS_CONFIRMED:
                cursor = conn.execute(
                    "SELECT link_status, canonical_person_id FROM source_entities WHERE id = ?",
                    (entity_id,)
                )
                row = cursor.fetchone()
                if row and row[0] == LINK_STATUS_CONFIRMED:
                    # Don't overwrite confirmed links with auto links
                    logger.debug(
                        f"Skipping link update for {entity_id}: existing confirmed link "
                        f"to {row[1][:8]}... protected from auto re-linking"
                    )
                    return False

            cursor = conn.execute("""
                UPDATE source_entities SET
                    canonical_person_id = ?,
                    link_confidence = ?,
                    link_status = ?,
                    linked_at = ?
                WHERE id = ?
            """, (
                resolved_person_id,
                confidence,
                status,
                datetime.now(timezone.utc).isoformat(),
                entity_id,
            ))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def unlink(self, entity_id: str) -> bool:
        """
        Remove link from a source entity.

        Args:
            entity_id: Source entity ID

        Returns:
            True if updated, False if entity not found
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                UPDATE source_entities SET
                    canonical_person_id = NULL,
                    link_confidence = 0.0,
                    link_status = 'auto',
                    linked_at = NULL
                WHERE id = ?
            """, (entity_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_by_id(self, entity_id: str) -> Optional[SourceEntity]:
        """Get entity by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM source_entities WHERE id = ?",
                (entity_id,)
            )
            row = cursor.fetchone()
            if row:
                return SourceEntity.from_row(row)
            return None
        finally:
            conn.close()

    def get_by_source(self, source_type: str, source_id: str) -> Optional[SourceEntity]:
        """Get entity by source type and ID."""
        if not source_id:
            return None
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM source_entities WHERE source_type = ? AND source_id = ?",
                (source_type, source_id)
            )
            row = cursor.fetchone()
            if row:
                return SourceEntity.from_row(row)
            return None
        finally:
            conn.close()

    def get_for_person(
        self,
        canonical_person_id: str,
        source_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[SourceEntity]:
        """
        Get source entities linked to a canonical person.

        Args:
            canonical_person_id: Canonical person ID
            source_type: Optional filter by source type
            limit: Maximum number of entities to return (None for all)

        Returns:
            List of source entities, most recent first
        """
        conn = self._get_connection()
        try:
            if source_type:
                if limit:
                    cursor = conn.execute("""
                        SELECT * FROM source_entities
                        WHERE canonical_person_id = ? AND source_type = ?
                        ORDER BY observed_at DESC
                        LIMIT ?
                    """, (canonical_person_id, source_type, limit))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM source_entities
                        WHERE canonical_person_id = ? AND source_type = ?
                        ORDER BY observed_at DESC
                    """, (canonical_person_id, source_type))
            else:
                if limit:
                    cursor = conn.execute("""
                        SELECT * FROM source_entities
                        WHERE canonical_person_id = ?
                        ORDER BY observed_at DESC
                        LIMIT ?
                    """, (canonical_person_id, limit))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM source_entities
                        WHERE canonical_person_id = ?
                        ORDER BY observed_at DESC
                    """, (canonical_person_id,))

            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_unlinked(
        self,
        source_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[SourceEntity]:
        """
        Get unlinked source entities.

        Args:
            source_type: Optional filter by source type
            limit: Maximum results to return

        Returns:
            List of unlinked source entities
        """
        conn = self._get_connection()
        try:
            if source_type:
                cursor = conn.execute("""
                    SELECT * FROM source_entities
                    WHERE canonical_person_id IS NULL AND source_type = ?
                    ORDER BY observed_at DESC
                    LIMIT ?
                """, (source_type, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM source_entities
                    WHERE canonical_person_id IS NULL
                    ORDER BY observed_at DESC
                    LIMIT ?
                """, (limit,))

            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_low_confidence(
        self,
        min_confidence: float = 0.0,
        max_confidence: float = 0.85,
        limit: int = 100,
    ) -> list[SourceEntity]:
        """
        Get linked source entities with low confidence.

        Used for the review queue to surface matches needing human verification.

        Args:
            min_confidence: Minimum confidence threshold
            max_confidence: Maximum confidence threshold
            limit: Maximum results to return

        Returns:
            List of source entities with confidence in range
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT * FROM source_entities
                WHERE canonical_person_id IS NOT NULL
                  AND link_confidence >= ?
                  AND link_confidence <= ?
                  AND link_status != 'confirmed'
                ORDER BY link_confidence ASC
                LIMIT ?
            """, (min_confidence, max_confidence, limit))

            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def count_low_confidence(
        self,
        min_confidence: float = 0.0,
        max_confidence: float = 0.85,
    ) -> int:
        """
        Count linked source entities with low confidence.

        Args:
            min_confidence: Minimum confidence threshold
            max_confidence: Maximum confidence threshold

        Returns:
            Count of entities in confidence range
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM source_entities
                WHERE canonical_person_id IS NOT NULL
                  AND link_confidence >= ?
                  AND link_confidence <= ?
                  AND link_status != 'confirmed'
            """, (min_confidence, max_confidence))
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def get_by_email(self, email: str) -> list[SourceEntity]:
        """Get all entities with a specific observed email."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM source_entities WHERE LOWER(observed_email) = LOWER(?)",
                (email,)
            )
            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_by_phone(self, phone: str) -> list[SourceEntity]:
        """Get all entities with a specific observed phone."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM source_entities WHERE observed_phone = ?",
                (phone,)
            )
            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_unlinked_by_email(self, email: str) -> list[SourceEntity]:
        """
        Get unlinked source entities matching a specific email.

        Args:
            email: Email address to match (case-insensitive)

        Returns:
            List of unlinked SourceEntity objects with this email
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """SELECT * FROM source_entities
                   WHERE LOWER(observed_email) = LOWER(?)
                   AND canonical_person_id IS NULL""",
                (email,)
            )
            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_unlinked_by_phone(self, phone: str) -> list[SourceEntity]:
        """
        Get unlinked source entities matching a specific phone.

        Args:
            phone: Phone number to match

        Returns:
            List of unlinked SourceEntity objects with this phone
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """SELECT * FROM source_entities
                   WHERE observed_phone = ?
                   AND canonical_person_id IS NULL""",
                (phone,)
            )
            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def link_unlinked_by_email(
        self,
        email: str,
        person_id: str,
        confidence: float = 1.0,
    ) -> int:
        """
        Link all unlinked source entities with this email to a person.

        This is used for retroactive linking when a person gains a new email
        address. All existing unlinked source entities with that email
        will be linked to the person.

        Args:
            email: Email address to match (case-insensitive)
            person_id: Canonical person ID to link to
            confidence: Link confidence score (default 1.0)

        Returns:
            Number of entities that were linked
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """UPDATE source_entities SET
                       canonical_person_id = ?,
                       link_confidence = ?,
                       link_status = ?,
                       linked_at = ?
                   WHERE LOWER(observed_email) = LOWER(?)
                   AND canonical_person_id IS NULL
                   AND link_status != ?""",
                (
                    person_id,
                    confidence,
                    LINK_STATUS_AUTO,
                    datetime.now(timezone.utc).isoformat(),
                    email,
                    LINK_STATUS_CONFIRMED,  # Don't overwrite confirmed links
                )
            )
            conn.commit()
            count = cursor.rowcount
            if count > 0:
                logger.info(f"Retroactively linked {count} source entities with email {email} to person {person_id[:8]}...")
            return count
        finally:
            conn.close()

    def link_unlinked_by_phone(
        self,
        phone: str,
        person_id: str,
        confidence: float = 1.0,
    ) -> int:
        """
        Link all unlinked source entities with this phone to a person.

        This is used for retroactive linking when a person gains a new phone
        number. All existing unlinked source entities with that phone
        will be linked to the person.

        Args:
            phone: Phone number to match
            person_id: Canonical person ID to link to
            confidence: Link confidence score (default 1.0)

        Returns:
            Number of entities that were linked
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """UPDATE source_entities SET
                       canonical_person_id = ?,
                       link_confidence = ?,
                       link_status = ?,
                       linked_at = ?
                   WHERE observed_phone = ?
                   AND canonical_person_id IS NULL
                   AND link_status != ?""",
                (
                    person_id,
                    confidence,
                    LINK_STATUS_AUTO,
                    datetime.now(timezone.utc).isoformat(),
                    phone,
                    LINK_STATUS_CONFIRMED,  # Don't overwrite confirmed links
                )
            )
            conn.commit()
            count = cursor.rowcount
            if count > 0:
                logger.info(f"Retroactively linked {count} source entities with phone {phone} to person {person_id[:8]}...")
            return count
        finally:
            conn.close()

    def record_match_attempt(self, entity_id: str) -> bool:
        """
        Record a failed match attempt for a source entity.

        Increments match_attempt_count and updates match_attempted_at timestamp.
        Used to track entities that have been processed but couldn't be matched.

        Args:
            entity_id: Source entity ID

        Returns:
            True if updated, False if entity not found
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """UPDATE source_entities SET
                       match_attempted_at = ?,
                       match_attempt_count = COALESCE(match_attempt_count, 0) + 1
                   WHERE id = ?
                   AND canonical_person_id IS NULL""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    entity_id,
                )
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_unlinked_for_rematching(
        self,
        source_type: Optional[str] = None,
        min_days_since_attempt: int = 30,
        max_attempts: int = 3,
        limit: int = 1000,
    ) -> list[SourceEntity]:
        """
        Get unlinked entities eligible for re-matching.

        Filters out:
        - Entities that were attempted recently (within min_days_since_attempt)
        - Entities with too many failed attempts (>= max_attempts)

        Args:
            source_type: Optional filter by source type
            min_days_since_attempt: Skip if attempted within this many days
            max_attempts: Skip if attempted this many times or more
            limit: Maximum entities to return

        Returns:
            List of SourceEntity objects eligible for re-matching
        """
        conn = self._get_connection()
        try:
            cutoff_date = (
                datetime.now(timezone.utc) - timedelta(days=min_days_since_attempt)
            ).isoformat()

            if source_type:
                cursor = conn.execute("""
                    SELECT * FROM source_entities
                    WHERE canonical_person_id IS NULL
                      AND source_type = ?
                      AND (match_attempted_at IS NULL OR match_attempted_at < ?)
                      AND (match_attempt_count IS NULL OR match_attempt_count < ?)
                    ORDER BY observed_at DESC
                    LIMIT ?
                """, (source_type, cutoff_date, max_attempts, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM source_entities
                    WHERE canonical_person_id IS NULL
                      AND (match_attempted_at IS NULL OR match_attempted_at < ?)
                      AND (match_attempt_count IS NULL OR match_attempt_count < ?)
                    ORDER BY observed_at DESC
                    LIMIT ?
                """, (cutoff_date, max_attempts, limit))

            return [SourceEntity.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def count_unlinked_for_rematching(
        self,
        source_type: Optional[str] = None,
        min_days_since_attempt: int = 30,
        max_attempts: int = 3,
    ) -> int:
        """
        Count unlinked entities eligible for re-matching.

        Args:
            source_type: Optional filter by source type
            min_days_since_attempt: Skip if attempted within this many days
            max_attempts: Skip if attempted this many times or more

        Returns:
            Count of eligible entities
        """
        conn = self._get_connection()
        try:
            cutoff_date = (
                datetime.now(timezone.utc) - timedelta(days=min_days_since_attempt)
            ).isoformat()

            if source_type:
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM source_entities
                    WHERE canonical_person_id IS NULL
                      AND source_type = ?
                      AND (match_attempted_at IS NULL OR match_attempted_at < ?)
                      AND (match_attempt_count IS NULL OR match_attempt_count < ?)
                """, (source_type, cutoff_date, max_attempts))
            else:
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM source_entities
                    WHERE canonical_person_id IS NULL
                      AND (match_attempted_at IS NULL OR match_attempted_at < ?)
                      AND (match_attempt_count IS NULL OR match_attempt_count < ?)
                """, (cutoff_date, max_attempts))

            return cursor.fetchone()[0]
        finally:
            conn.close()

    def delete(self, entity_id: str) -> bool:
        """Delete a source entity."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM source_entities WHERE id = ?",
                (entity_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_for_person(self, canonical_person_id: str) -> int:
        """Delete all source entities linked to a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM source_entities WHERE canonical_person_id = ?",
                (canonical_person_id,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def count(self) -> int:
        """Get total number of source entities."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM source_entities")
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def count_for_person(self, canonical_person_id: str) -> int:
        """Get count of source entities for a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?",
                (canonical_person_id,)
            )
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def get_statistics(self) -> dict:
        """Get aggregate statistics about source entities."""
        conn = self._get_connection()
        try:
            total = conn.execute("SELECT COUNT(*) FROM source_entities").fetchone()[0]

            linked = conn.execute(
                "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id IS NOT NULL"
            ).fetchone()[0]

            by_source = {}
            cursor = conn.execute("""
                SELECT source_type, COUNT(*) as count
                FROM source_entities
                GROUP BY source_type
            """)
            for row in cursor.fetchall():
                by_source[row[0]] = row[1]

            by_status = {}
            cursor = conn.execute("""
                SELECT link_status, COUNT(*) as count
                FROM source_entities
                GROUP BY link_status
            """)
            for row in cursor.fetchall():
                by_status[row[0]] = row[1]

            return {
                "total_entities": total,
                "linked_entities": linked,
                "unlinked_entities": total - linked,
                "by_source": by_source,
                "by_status": by_status,
            }
        finally:
            conn.close()


# Singleton instance
_source_entity_store: Optional[SourceEntityStore] = None


def get_source_entity_store(db_path: Optional[str] = None) -> SourceEntityStore:
    """
    Get or create the singleton SourceEntityStore.

    Args:
        db_path: Path to SQLite database

    Returns:
        SourceEntityStore instance
    """
    global _source_entity_store
    if _source_entity_store is None:
        _source_entity_store = SourceEntityStore(db_path)
    return _source_entity_store


# Factory functions for creating source entities from different sources


def create_gmail_source_entity(
    message_id: str,
    sender_email: str,
    sender_name: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """Create a source entity from a Gmail message."""
    return SourceEntity(
        source_type="gmail",
        source_id=message_id,
        observed_name=sender_name,
        observed_email=sender_email.lower() if sender_email else None,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )


def create_calendar_source_entity(
    event_id: str,
    attendee_email: str,
    attendee_name: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """Create a source entity from a calendar event attendee."""
    return SourceEntity(
        source_type="calendar",
        source_id=f"{event_id}:{attendee_email}",
        observed_name=attendee_name,
        observed_email=attendee_email.lower() if attendee_email else None,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )


def create_slack_source_entity(
    user_id: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """Create a source entity from a Slack user."""
    return SourceEntity(
        source_type="slack",
        source_id=user_id,
        observed_name=display_name,
        observed_email=email.lower() if email else None,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )


def create_imessage_source_entity(
    handle: str,
    display_name: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """Create a source entity from an iMessage handle."""
    # Handle can be email or phone
    email = None
    phone = None
    if "@" in handle:
        email = handle.lower()
    else:
        phone = handle

    return SourceEntity(
        source_type="imessage",
        source_id=handle,
        observed_name=display_name,
        observed_email=email,
        observed_phone=phone,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )


def create_contacts_source_entity(
    contact_id: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """Create a source entity from Apple Contacts."""
    return SourceEntity(
        source_type="contacts",
        source_id=contact_id,
        observed_name=name,
        observed_email=email.lower() if email else None,
        observed_phone=phone,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )


def create_linkedin_source_entity(
    profile_url: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """Create a source entity from a LinkedIn profile."""
    return SourceEntity(
        source_type="linkedin",
        source_id=profile_url,
        observed_name=name,
        observed_email=email.lower() if email else None,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )


def create_vault_source_entity(
    file_path: str,
    person_name: str,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """
    Create a source entity from a vault mention.

    Args:
        file_path: Path to the vault note file
        person_name: Name of the person mentioned
        observed_at: When the note was created/modified
        metadata: Additional metadata (note_title, is_granola, etc.)

    Returns:
        SourceEntity for this vault mention
    """
    # Use file_path:person_name as unique source_id
    # This ensures each person mention per note is tracked separately
    source_id = f"{file_path}:{person_name}"
    return SourceEntity(
        source_type="vault",
        source_id=source_id,
        observed_name=person_name,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )


def create_granola_source_entity(
    file_path: str,
    person_name: str,
    observed_at: Optional[datetime] = None,
    metadata: Optional[dict] = None,
) -> SourceEntity:
    """
    Create a source entity from a Granola meeting note mention.

    Args:
        file_path: Path to the Granola note file
        person_name: Name of the person mentioned
        observed_at: When the meeting occurred
        metadata: Additional metadata (note_title, granola_id, etc.)

    Returns:
        SourceEntity for this Granola mention
    """
    # Use file_path:person_name as unique source_id
    source_id = f"{file_path}:{person_name}"
    return SourceEntity(
        source_type="granola",
        source_id=source_id,
        observed_name=person_name,
        observed_at=observed_at or datetime.now(timezone.utc),
        metadata=metadata or {},
    )
