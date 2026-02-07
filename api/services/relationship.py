"""
Relationship - Tracks connections between people.

Relationships are discovered by analyzing shared contexts:
- Shared calendar events
- Shared email threads
- Shared Slack channels
- Co-mentions in notes
"""
import sqlite3
import json
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from api.services.source_entity import get_crm_db_path
from api.utils.datetime_utils import make_aware as _make_aware

logger = logging.getLogger(__name__)


# Relationship types
TYPE_COWORKER = "coworker"
TYPE_FRIEND = "friend"
TYPE_FAMILY = "family"
TYPE_INFERRED = "inferred"  # Discovered through shared contexts


@dataclass
class Relationship:
    """
    A relationship between two people.

    Relationships are bidirectional - if A knows B, B knows A.
    The person_a_id is always lexicographically smaller than person_b_id
    to ensure uniqueness.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Person IDs (canonical person IDs)
    person_a_id: str = ""
    person_b_id: str = ""

    # Relationship metadata
    relationship_type: str = TYPE_INFERRED
    shared_contexts: list[str] = field(default_factory=list)  # vault paths, slack channels

    # Interaction stats - multi-source tracking
    shared_events_count: int = 0  # Calendar events together
    shared_threads_count: int = 0  # Email threads together
    shared_messages_count: int = 0  # iMessage/SMS direct threads
    shared_whatsapp_count: int = 0  # WhatsApp direct threads
    shared_slack_count: int = 0  # Slack DM message count
    shared_phone_calls_count: int = 0  # Phone calls (high value - synchronous)
    shared_photos_count: int = 0  # Photos together (strong relationship signal)
    is_linkedin_connection: bool = False  # LinkedIn connection flag

    # Timestamps
    first_seen_together: Optional[datetime] = None
    last_seen_together: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        """Ensure person_a_id < person_b_id for uniqueness."""
        if self.person_a_id > self.person_b_id:
            self.person_a_id, self.person_b_id = self.person_b_id, self.person_a_id

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        data = asdict(self)
        if self.first_seen_together:
            data["first_seen_together"] = self.first_seen_together.isoformat()
        if self.last_seen_together:
            data["last_seen_together"] = self.last_seen_together.isoformat()
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Relationship":
        """Create Relationship from dict."""
        if data.get("first_seen_together") and isinstance(data["first_seen_together"], str):
            data["first_seen_together"] = _make_aware(datetime.fromisoformat(data["first_seen_together"]))
        if data.get("last_seen_together") and isinstance(data["last_seen_together"], str):
            data["last_seen_together"] = _make_aware(datetime.fromisoformat(data["last_seen_together"]))
        if data.get("created_at") and isinstance(data["created_at"], str):
            data["created_at"] = _make_aware(datetime.fromisoformat(data["created_at"]))
        if data.get("updated_at") and isinstance(data["updated_at"], str):
            data["updated_at"] = _make_aware(datetime.fromisoformat(data["updated_at"]))
        return cls(**data)

    @classmethod
    def from_row(cls, row: tuple) -> "Relationship":
        """Create Relationship from SQLite row."""
        # Row order: id, person_a_id, person_b_id, relationship_type, shared_contexts,
        #            shared_events_count, shared_threads_count, first_seen_together,
        #            last_seen_together, created_at, updated_at,
        #            shared_messages_count, shared_whatsapp_count, shared_slack_count, is_linkedin_connection
        first_seen = datetime.fromisoformat(row[7]) if row[7] else None
        last_seen = datetime.fromisoformat(row[8]) if row[8] else None
        created_at = datetime.fromisoformat(row[9]) if row[9] else datetime.now(timezone.utc)
        updated_at = datetime.fromisoformat(row[10]) if row[10] else datetime.now(timezone.utc)

        # Handle new fields (may not exist in older rows)
        shared_messages_count = row[11] if len(row) > 11 else 0
        shared_whatsapp_count = row[12] if len(row) > 12 else 0
        shared_slack_count = row[13] if len(row) > 13 else 0
        is_linkedin_connection = bool(row[14]) if len(row) > 14 else False
        shared_phone_calls_count = row[15] if len(row) > 15 else 0
        shared_photos_count = row[16] if len(row) > 16 else 0

        return cls(
            id=row[0],
            person_a_id=row[1],
            person_b_id=row[2],
            relationship_type=row[3] or TYPE_INFERRED,
            shared_contexts=json.loads(row[4]) if row[4] else [],
            shared_events_count=row[5] or 0,
            shared_threads_count=row[6] or 0,
            first_seen_together=_make_aware(first_seen),
            last_seen_together=_make_aware(last_seen),
            created_at=_make_aware(created_at),
            updated_at=_make_aware(updated_at),
            shared_messages_count=shared_messages_count or 0,
            shared_whatsapp_count=shared_whatsapp_count or 0,
            shared_slack_count=shared_slack_count or 0,
            is_linkedin_connection=is_linkedin_connection,
            shared_phone_calls_count=shared_phone_calls_count or 0,
            shared_photos_count=shared_photos_count or 0,
        )

    @property
    def total_shared_interactions(self) -> int:
        """Get total number of shared interactions from all sources."""
        return (
            self.shared_events_count +
            self.shared_threads_count +
            self.shared_messages_count +
            self.shared_whatsapp_count +
            self.shared_slack_count +
            self.shared_phone_calls_count +
            self.shared_photos_count
        )

    @property
    def edge_weight_raw(self) -> int:
        """Calculate raw edge weight (sum of weighted interactions)."""
        return (
            self.shared_events_count * 3 +       # Calendar meetings (high signal)
            self.shared_threads_count * 2 +      # Email threads
            self.shared_messages_count * 2 +     # iMessage threads
            self.shared_whatsapp_count * 2 +     # WhatsApp threads
            self.shared_slack_count * 1 +        # Slack DMs (weaker per-message signal)
            self.shared_phone_calls_count * 4 +  # Phone calls (highest - synchronous voice)
            self.shared_photos_count * 3 +       # Photos together (high signal - in-person)
            (10 if self.is_linkedin_connection else 0)  # LinkedIn connection bonus
        )

    @property
    def edge_weight(self) -> int:
        """
        Calculate normalized edge weight (0-100 scale).

        Uses logarithmic scaling to spread values evenly:
        - Raw weight 1 → ~15%
        - Raw weight 10 → ~40%
        - Raw weight 50 → ~60%
        - Raw weight 200 → ~75%
        - Raw weight 1000 → ~90%
        - Raw weight 5000+ → 95-100%
        """
        import math
        raw = self.edge_weight_raw
        if raw <= 0:
            return 0
        # Log scale with base that gives good spread
        # log(1 + raw) / log(1 + 10000) * 100, capped at 100
        normalized = math.log1p(raw) / math.log1p(10000) * 100
        return min(100, round(normalized))

    @property
    def pair_strength(self) -> int:
        """
        Calculate pair relationship strength (0-100 scale).

        Uses the same formula as person relationship strength:
            strength = (recency × 0.30) + (frequency × 0.60) + (diversity × 0.10)

        This unifies edge_weight and relationship_strength into a single metric.
        Uses adjusted frequency scaling to work for both high-interaction (owner)
        and low-interaction (non-owner) edges.

        Components:
        - Recency: How recently the pair interacted (decays over 200 days)
        - Frequency: Weighted interaction count with log scaling
        - Diversity: Number of different interaction types used
        """
        import math
        from datetime import timezone

        # Constants - match relationship_weights.py but adjusted for pair relationships
        RECENCY_WEIGHT = 0.30
        FREQUENCY_WEIGHT = 0.60
        DIVERSITY_WEIGHT = 0.10
        RECENCY_WINDOW_DAYS = 200

        # Frequency target tuned for pair relationships
        # Owner edges avg ~36, non-owner avg ~1.6, max 387
        # Use lower target so non-owner edges with 10-50 interactions get reasonable scores
        FREQUENCY_TARGET = 100  # Lower than person strength (250) for better non-owner spread

        # Interaction type weights (same rationale as relationship_weights.py)
        TYPE_WEIGHTS = {
            'events': 1.0,      # Calendar meetings
            'threads': 0.8,     # Email threads
            'messages': 1.5,    # iMessage (personal)
            'whatsapp': 1.5,    # WhatsApp (personal)
            'slack': 1.2,       # Slack DMs
            'phone_calls': 2.0, # Phone calls (highest - synchronous)
        }

        # --- RECENCY SCORE ---
        recency_score = 0.0
        if self.last_seen_together:
            now = datetime.now(timezone.utc)
            last_seen = self.last_seen_together
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            # Cap future dates at today
            if last_seen > now:
                last_seen = now
            days_since = (now - last_seen).days
            recency_score = max(0.0, 1.0 - (days_since / RECENCY_WINDOW_DAYS))

        # --- FREQUENCY SCORE ---
        # Calculate weighted interaction count
        weighted_count = (
            self.shared_events_count * TYPE_WEIGHTS['events'] +
            self.shared_threads_count * TYPE_WEIGHTS['threads'] +
            self.shared_messages_count * TYPE_WEIGHTS['messages'] +
            self.shared_whatsapp_count * TYPE_WEIGHTS['whatsapp'] +
            self.shared_slack_count * TYPE_WEIGHTS['slack'] +
            self.shared_phone_calls_count * TYPE_WEIGHTS['phone_calls']
        )

        # Log scaling for better spread across low and high counts
        # log(1 + count) / log(1 + target) gives 0-1 range with good distribution
        if weighted_count <= 0:
            frequency_score = 0.0
        else:
            frequency_score = min(1.0, math.log1p(weighted_count) / math.log1p(FREQUENCY_TARGET))

        # --- DIVERSITY SCORE ---
        # Count how many source types have interactions
        source_count = 0
        total_sources = 6  # events, threads, messages, whatsapp, slack, phone_calls
        if self.shared_events_count > 0:
            source_count += 1
        if self.shared_threads_count > 0:
            source_count += 1
        if self.shared_messages_count > 0:
            source_count += 1
        if self.shared_whatsapp_count > 0:
            source_count += 1
        if self.shared_slack_count > 0:
            source_count += 1
        if self.shared_phone_calls_count > 0:
            source_count += 1

        # LinkedIn connection gives a small diversity bonus
        if self.is_linkedin_connection:
            source_count += 0.5  # Partial credit for passive connection

        diversity_score = min(1.0, source_count / total_sources)

        # --- COMBINE SCORES ---
        strength = (
            recency_score * RECENCY_WEIGHT +
            frequency_score * FREQUENCY_WEIGHT +
            diversity_score * DIVERSITY_WEIGHT
        )

        # Scale to 0-100
        return min(100, round(strength * 100))

    def involves(self, person_id: str) -> bool:
        """Check if this relationship involves a specific person."""
        return person_id == self.person_a_id or person_id == self.person_b_id

    def other_person(self, person_id: str) -> Optional[str]:
        """Get the other person in this relationship."""
        if person_id == self.person_a_id:
            return self.person_b_id
        elif person_id == self.person_b_id:
            return self.person_a_id
        return None


class RelationshipStore:
    """
    SQLite-backed storage for Relationship records.

    Provides efficient queries for finding connections between people.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the relationship store.

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
                CREATE TABLE IF NOT EXISTS relationships (
                    id TEXT PRIMARY KEY,
                    person_a_id TEXT NOT NULL,
                    person_b_id TEXT NOT NULL,
                    relationship_type TEXT,
                    shared_contexts TEXT,
                    shared_events_count INTEGER DEFAULT 0,
                    shared_threads_count INTEGER DEFAULT 0,
                    first_seen_together TIMESTAMP,
                    last_seen_together TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(person_a_id, person_b_id)
                )
            """)

            # Add new columns for multi-source tracking (migration)
            new_columns = [
                ("shared_messages_count", "INTEGER DEFAULT 0"),
                ("shared_whatsapp_count", "INTEGER DEFAULT 0"),
                ("shared_slack_count", "INTEGER DEFAULT 0"),
                ("is_linkedin_connection", "INTEGER DEFAULT 0"),
                ("shared_phone_calls_count", "INTEGER DEFAULT 0"),
                ("shared_photos_count", "INTEGER DEFAULT 0"),
            ]
            for col_name, col_type in new_columns:
                try:
                    conn.execute(f"ALTER TABLE relationships ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Added column {col_name} to relationships table")
                except sqlite3.OperationalError:
                    # Column already exists
                    pass

            # Index for finding relationships by person
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_person_a
                ON relationships(person_a_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_person_b
                ON relationships(person_b_id)
            """)

            # Index for finding relationships by type
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_type
                ON relationships(relationship_type)
            """)

            conn.commit()
            logger.info(f"Initialized relationships database at {self.db_path}")
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)

    def _normalize_ids(self, person_a_id: str, person_b_id: str) -> tuple[str, str]:
        """Ensure person_a_id < person_b_id for uniqueness."""
        if person_a_id > person_b_id:
            return person_b_id, person_a_id
        return person_a_id, person_b_id

    def add(self, relationship: Relationship) -> Relationship:
        """
        Add a new relationship.

        Args:
            relationship: Relationship to add

        Returns:
            The added relationship
        """
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO relationships
                (id, person_a_id, person_b_id, relationship_type, shared_contexts,
                 shared_events_count, shared_threads_count, first_seen_together,
                 last_seen_together, created_at, updated_at,
                 shared_messages_count, shared_whatsapp_count, shared_slack_count,
                 is_linkedin_connection, shared_phone_calls_count, shared_photos_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                relationship.id,
                relationship.person_a_id,
                relationship.person_b_id,
                relationship.relationship_type,
                json.dumps(relationship.shared_contexts),
                relationship.shared_events_count,
                relationship.shared_threads_count,
                relationship.first_seen_together.isoformat() if relationship.first_seen_together else None,
                relationship.last_seen_together.isoformat() if relationship.last_seen_together else None,
                relationship.created_at.isoformat(),
                relationship.updated_at.isoformat(),
                relationship.shared_messages_count,
                relationship.shared_whatsapp_count,
                relationship.shared_slack_count,
                1 if relationship.is_linkedin_connection else 0,
                relationship.shared_phone_calls_count,
                relationship.shared_photos_count,
            ))
            conn.commit()
            return relationship
        finally:
            conn.close()

    def add_or_update(self, relationship: Relationship) -> tuple[Relationship, bool]:
        """
        Add relationship or update if already exists.

        Args:
            relationship: Relationship to add/update

        Returns:
            Tuple of (relationship, was_created)
        """
        existing = self.get_between(relationship.person_a_id, relationship.person_b_id)
        if existing:
            # Update existing - merge shared contexts and update counts
            relationship.id = existing.id
            relationship.created_at = existing.created_at

            # Merge shared contexts
            contexts = set(existing.shared_contexts)
            contexts.update(relationship.shared_contexts)
            relationship.shared_contexts = list(contexts)

            # Keep earliest first_seen
            if existing.first_seen_together:
                if relationship.first_seen_together is None or existing.first_seen_together < relationship.first_seen_together:
                    relationship.first_seen_together = existing.first_seen_together

            self.update(relationship)
            return relationship, False

        return self.add(relationship), True

    def update(self, relationship: Relationship) -> Relationship:
        """
        Update an existing relationship.

        Args:
            relationship: Relationship with updated data

        Returns:
            The updated relationship
        """
        relationship.updated_at = datetime.now(timezone.utc)
        conn = self._get_connection()
        try:
            conn.execute("""
                UPDATE relationships SET
                    relationship_type = ?,
                    shared_contexts = ?,
                    shared_events_count = ?,
                    shared_threads_count = ?,
                    first_seen_together = ?,
                    last_seen_together = ?,
                    updated_at = ?,
                    shared_messages_count = ?,
                    shared_whatsapp_count = ?,
                    shared_slack_count = ?,
                    is_linkedin_connection = ?,
                    shared_phone_calls_count = ?,
                    shared_photos_count = ?
                WHERE id = ?
            """, (
                relationship.relationship_type,
                json.dumps(relationship.shared_contexts),
                relationship.shared_events_count,
                relationship.shared_threads_count,
                relationship.first_seen_together.isoformat() if relationship.first_seen_together else None,
                relationship.last_seen_together.isoformat() if relationship.last_seen_together else None,
                relationship.updated_at.isoformat(),
                relationship.shared_messages_count,
                relationship.shared_whatsapp_count,
                relationship.shared_slack_count,
                1 if relationship.is_linkedin_connection else 0,
                relationship.shared_phone_calls_count,
                relationship.shared_photos_count,
                relationship.id,
            ))
            conn.commit()
            return relationship
        finally:
            conn.close()

    def get_by_id(self, relationship_id: str) -> Optional[Relationship]:
        """Get relationship by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM relationships WHERE id = ?",
                (relationship_id,)
            )
            row = cursor.fetchone()
            if row:
                return Relationship.from_row(row)
            return None
        finally:
            conn.close()

    def get_between(self, person_a_id: str, person_b_id: str) -> Optional[Relationship]:
        """Get the relationship between two people."""
        a_id, b_id = self._normalize_ids(person_a_id, person_b_id)
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM relationships WHERE person_a_id = ? AND person_b_id = ?",
                (a_id, b_id)
            )
            row = cursor.fetchone()
            if row:
                return Relationship.from_row(row)
            return None
        finally:
            conn.close()

    def get_for_person(
        self,
        person_id: str,
        relationship_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[Relationship]:
        """
        Get all relationships for a person.

        Args:
            person_id: Canonical person ID
            relationship_type: Optional filter by type
            limit: Maximum results

        Returns:
            List of relationships
        """
        conn = self._get_connection()
        try:
            if relationship_type:
                cursor = conn.execute("""
                    SELECT * FROM relationships
                    WHERE (person_a_id = ? OR person_b_id = ?)
                      AND relationship_type = ?
                    ORDER BY last_seen_together DESC
                    LIMIT ?
                """, (person_id, person_id, relationship_type, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM relationships
                    WHERE person_a_id = ? OR person_b_id = ?
                    ORDER BY last_seen_together DESC
                    LIMIT ?
                """, (person_id, person_id, limit))

            return [Relationship.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_connections(self, person_id: str, limit: int = 100) -> list[str]:
        """
        Get IDs of all people connected to a person.

        Args:
            person_id: Canonical person ID
            limit: Maximum results

        Returns:
            List of connected person IDs
        """
        relationships = self.get_for_person(person_id, limit=limit)
        connections = []
        for rel in relationships:
            other = rel.other_person(person_id)
            if other:
                connections.append(other)
        return connections

    def increment_shared_event(
        self,
        person_a_id: str,
        person_b_id: str,
        event_time: Optional[datetime] = None,
        context: Optional[str] = None,
    ) -> Relationship:
        """
        Increment the shared events count between two people.

        Creates relationship if it doesn't exist.

        Args:
            person_a_id: First person ID
            person_b_id: Second person ID
            event_time: Time of the shared event
            context: Optional context (e.g., calendar ID)

        Returns:
            Updated relationship
        """
        a_id, b_id = self._normalize_ids(person_a_id, person_b_id)
        existing = self.get_between(a_id, b_id)

        now = event_time or datetime.now(timezone.utc)
        contexts = [context] if context else []

        if existing:
            existing.shared_events_count += 1
            existing.last_seen_together = now
            if context and context not in existing.shared_contexts:
                existing.shared_contexts.append(context)
            return self.update(existing)
        else:
            rel = Relationship(
                person_a_id=a_id,
                person_b_id=b_id,
                shared_events_count=1,
                first_seen_together=now,
                last_seen_together=now,
                shared_contexts=contexts,
            )
            return self.add(rel)

    def increment_shared_thread(
        self,
        person_a_id: str,
        person_b_id: str,
        thread_time: Optional[datetime] = None,
        context: Optional[str] = None,
    ) -> Relationship:
        """
        Increment the shared threads count between two people.

        Creates relationship if it doesn't exist.

        Args:
            person_a_id: First person ID
            person_b_id: Second person ID
            thread_time: Time of the shared thread
            context: Optional context (e.g., thread ID)

        Returns:
            Updated relationship
        """
        a_id, b_id = self._normalize_ids(person_a_id, person_b_id)
        existing = self.get_between(a_id, b_id)

        now = thread_time or datetime.now(timezone.utc)
        contexts = [context] if context else []

        if existing:
            existing.shared_threads_count += 1
            existing.last_seen_together = now
            if context and context not in existing.shared_contexts:
                existing.shared_contexts.append(context)
            return self.update(existing)
        else:
            rel = Relationship(
                person_a_id=a_id,
                person_b_id=b_id,
                shared_threads_count=1,
                first_seen_together=now,
                last_seen_together=now,
                shared_contexts=contexts,
            )
            return self.add(rel)

    def delete(self, relationship_id: str) -> bool:
        """Delete a relationship."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM relationships WHERE id = ?",
                (relationship_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_for_person(self, person_id: str) -> int:
        """Delete all relationships involving a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM relationships WHERE person_a_id = ? OR person_b_id = ?",
                (person_id, person_id)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def count(self) -> int:
        """Get total number of relationships."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM relationships")
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def get_people_with_relationships(self) -> set[str]:
        """
        Get all person IDs that have at least one relationship.

        Returns:
            Set of person IDs with relationships
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT DISTINCT person_a_id FROM relationships
                UNION
                SELECT DISTINCT person_b_id FROM relationships
            """)
            return {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()

    def get_for_people_batch(self, person_ids: set[str]) -> dict[str, list["Relationship"]]:
        """
        Get all relationships for multiple people in a single query.

        This is much more efficient than calling get_for_person() in a loop.

        Args:
            person_ids: Set of person IDs to fetch relationships for

        Returns:
            Dict mapping person_id to list of their relationships
        """
        if not person_ids:
            return {}

        conn = self._get_connection()
        try:
            # Create placeholders for IN clause
            placeholders = ",".join("?" * len(person_ids))
            ids_list = list(person_ids)

            cursor = conn.execute(f"""
                SELECT * FROM relationships
                WHERE person_a_id IN ({placeholders})
                   OR person_b_id IN ({placeholders})
                ORDER BY last_seen_together DESC
            """, ids_list + ids_list)

            # Group relationships by person
            result: dict[str, list[Relationship]] = {pid: [] for pid in person_ids}
            for row in cursor.fetchall():
                rel = Relationship.from_row(row)
                if rel.person_a_id in person_ids:
                    result[rel.person_a_id].append(rel)
                if rel.person_b_id in person_ids:
                    result[rel.person_b_id].append(rel)

            return result
        finally:
            conn.close()

    def get_all_relationships(self, limit: Optional[int] = None) -> list["Relationship"]:
        """
        Get all relationships.

        Args:
            limit: Maximum relationships to return (None = no limit)

        Returns:
            List of all relationships
        """
        conn = self._get_connection()
        try:
            if limit is not None:
                cursor = conn.execute("""
                    SELECT * FROM relationships
                    ORDER BY last_seen_together DESC
                    LIMIT ?
                """, (limit,))
            else:
                cursor = conn.execute("""
                    SELECT * FROM relationships
                    ORDER BY last_seen_together DESC
                """)
            return [Relationship.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_statistics(self) -> dict:
        """Get aggregate statistics about relationships."""
        conn = self._get_connection()
        try:
            total = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]

            by_type = {}
            cursor = conn.execute("""
                SELECT relationship_type, COUNT(*) as count
                FROM relationships
                GROUP BY relationship_type
            """)
            for row in cursor.fetchall():
                by_type[row[0] or "inferred"] = row[1]

            # Average shared interactions
            avg = conn.execute("""
                SELECT AVG(shared_events_count + shared_threads_count)
                FROM relationships
            """).fetchone()[0] or 0

            return {
                "total_relationships": total,
                "by_type": by_type,
                "avg_shared_interactions": round(avg, 2),
            }
        finally:
            conn.close()


# Singleton instance
_relationship_store: Optional[RelationshipStore] = None


def get_relationship_store(db_path: Optional[str] = None) -> RelationshipStore:
    """
    Get or create the singleton RelationshipStore.

    Args:
        db_path: Path to SQLite database

    Returns:
        RelationshipStore instance
    """
    global _relationship_store
    if _relationship_store is None:
        _relationship_store = RelationshipStore(db_path)
    return _relationship_store
