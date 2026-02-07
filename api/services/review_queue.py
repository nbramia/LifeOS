"""
Entity Review Queue Service.

Manages a queue of entities that need human review:
- Duplicate candidates (shared emails, phones, similar names)
- Non-human entities that escaped automated filters
- Over-merged entities that may need to be split

The queue is populated by sync_entity_cleanup.py after each nightly sync
and reviewed via the CRM Cleanup tab.
"""
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Database path (same as CRM)
CRM_DB_PATH = Path(__file__).parent.parent.parent / "data" / "crm.db"


class ReviewType(str, Enum):
    """Types of review items."""
    DUPLICATE = "duplicate"  # Two entities may be the same person
    NON_HUMAN = "non_human"  # Entity may not be a real person
    OVER_MERGED = "over_merged"  # Entity may contain multiple people


class ReviewStatus(str, Enum):
    """Status of a review item."""
    PENDING = "pending"
    MERGED = "merged"  # Duplicates were merged
    SKIPPED = "skipped"  # Marked as different people
    HIDDEN = "hidden"  # Non-human was hidden
    KEPT = "kept"  # Non-human was confirmed as real person
    SPLIT = "split"  # Over-merged entity was split


@dataclass
class ReviewCandidate:
    """A candidate item for human review."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    review_type: str = ReviewType.DUPLICATE.value

    # For duplicates: both IDs populated
    # For non-human/over-merged: only person_a populated
    person_a_id: str = ""
    person_a_name: str = ""
    person_b_id: Optional[str] = None
    person_b_name: Optional[str] = None

    # Scoring and reasoning
    confidence: float = 0.5
    reason: str = ""  # Human-readable reason (e.g., "Shared email: foo@bar.com")
    evidence: Optional[dict] = None  # JSON blob with additional context

    # Status tracking
    status: str = ReviewStatus.PENDING.value
    reviewed_at: Optional[datetime] = None
    batch_id: Optional[str] = None  # Groups items from same cleanup run
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "review_type": self.review_type,
            "person_a_id": self.person_a_id,
            "person_a_name": self.person_a_name,
            "person_b_id": self.person_b_id,
            "person_b_name": self.person_b_name,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence": self.evidence,
            "status": self.status,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "batch_id": self.batch_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ReviewCandidate":
        """Create from database row."""
        evidence = None
        if row["evidence"]:
            try:
                evidence = json.loads(row["evidence"])
            except json.JSONDecodeError:
                pass

        reviewed_at = None
        if row["reviewed_at"]:
            try:
                reviewed_at = datetime.fromisoformat(row["reviewed_at"].replace("Z", "+00:00"))
            except ValueError:
                pass

        created_at = datetime.now(timezone.utc)
        if row["created_at"]:
            try:
                created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            except ValueError:
                pass

        return cls(
            id=row["id"],
            review_type=row["review_type"],
            person_a_id=row["person_a_id"],
            person_a_name=row["person_a_name"],
            person_b_id=row["person_b_id"],
            person_b_name=row["person_b_name"],
            confidence=row["confidence"],
            reason=row["reason"],
            evidence=evidence,
            status=row["status"],
            reviewed_at=reviewed_at,
            batch_id=row["batch_id"],
            created_at=created_at,
        )


class ReviewQueueStore:
    """SQLite-backed store for entity review queue."""

    def __init__(self, db_path: Path = CRM_DB_PATH):
        """Initialize the store."""
        self.db_path = db_path
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        """Create the review queue table if it doesn't exist."""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_review_queue (
                id TEXT PRIMARY KEY,
                review_type TEXT NOT NULL,
                person_a_id TEXT,
                person_a_name TEXT,
                person_b_id TEXT,
                person_b_name TEXT,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                evidence TEXT,
                status TEXT DEFAULT 'pending',
                reviewed_at TIMESTAMP,
                batch_id TEXT,
                created_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_queue_status
            ON entity_review_queue(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_queue_type
            ON entity_review_queue(review_type)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_queue_confidence
            ON entity_review_queue(confidence DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_queue_batch
            ON entity_review_queue(batch_id)
        """)
        conn.commit()
        conn.close()

    def add_duplicate(
        self,
        person_a_id: str,
        person_a_name: str,
        person_b_id: str,
        person_b_name: str,
        confidence: float,
        reason: str,
        evidence: Optional[dict] = None,
        batch_id: Optional[str] = None,
    ) -> ReviewCandidate:
        """
        Add a duplicate candidate to the review queue.

        Args:
            person_a_id: First person's ID
            person_a_name: First person's name
            person_b_id: Second person's ID
            person_b_name: Second person's name
            confidence: Confidence score (0-1) that these are duplicates
            reason: Human-readable reason for the match
            evidence: Additional context (e.g., shared identifiers)
            batch_id: Optional batch ID for grouping

        Returns:
            The created ReviewCandidate
        """
        # Check for existing pending item with same pair (in either order)
        conn = self._get_conn()
        existing = conn.execute("""
            SELECT id FROM entity_review_queue
            WHERE status = 'pending'
            AND review_type = 'duplicate'
            AND (
                (person_a_id = ? AND person_b_id = ?)
                OR (person_a_id = ? AND person_b_id = ?)
            )
        """, (person_a_id, person_b_id, person_b_id, person_a_id)).fetchone()

        if existing:
            conn.close()
            logger.debug(f"Duplicate candidate already exists: {person_a_id} <-> {person_b_id}")
            return self.get_by_id(existing["id"])

        candidate = ReviewCandidate(
            review_type=ReviewType.DUPLICATE.value,
            person_a_id=person_a_id,
            person_a_name=person_a_name,
            person_b_id=person_b_id,
            person_b_name=person_b_name,
            confidence=confidence,
            reason=reason,
            evidence=evidence,
            batch_id=batch_id,
        )

        self._insert(conn, candidate)
        conn.commit()
        conn.close()

        logger.info(f"Added duplicate candidate: {person_a_name} <-> {person_b_name} "
                   f"(confidence: {confidence:.2f}, reason: {reason})")
        return candidate

    def add_non_human(
        self,
        person_id: str,
        person_name: str,
        confidence: float,
        reason: str,
        evidence: Optional[dict] = None,
        batch_id: Optional[str] = None,
    ) -> ReviewCandidate:
        """
        Add a non-human candidate to the review queue.

        Args:
            person_id: The entity's ID
            person_name: The entity's name
            confidence: Confidence score (0-1) that this is non-human
            reason: Human-readable reason
            evidence: Additional context
            batch_id: Optional batch ID for grouping

        Returns:
            The created ReviewCandidate
        """
        # Check for existing pending item
        conn = self._get_conn()
        existing = conn.execute("""
            SELECT id FROM entity_review_queue
            WHERE status = 'pending'
            AND review_type = 'non_human'
            AND person_a_id = ?
        """, (person_id,)).fetchone()

        if existing:
            conn.close()
            logger.debug(f"Non-human candidate already exists: {person_id}")
            return self.get_by_id(existing["id"])

        candidate = ReviewCandidate(
            review_type=ReviewType.NON_HUMAN.value,
            person_a_id=person_id,
            person_a_name=person_name,
            confidence=confidence,
            reason=reason,
            evidence=evidence,
            batch_id=batch_id,
        )

        self._insert(conn, candidate)
        conn.commit()
        conn.close()

        logger.info(f"Added non-human candidate: {person_name} "
                   f"(confidence: {confidence:.2f}, reason: {reason})")
        return candidate

    def add_over_merged(
        self,
        person_id: str,
        person_name: str,
        confidence: float,
        reason: str,
        evidence: Optional[dict] = None,
        batch_id: Optional[str] = None,
    ) -> ReviewCandidate:
        """
        Add an over-merged candidate to the review queue.

        These are entities with many sources/aliases that may represent
        multiple real people incorrectly merged together.

        Args:
            person_id: The entity's ID
            person_name: The entity's name
            confidence: Confidence score that this is over-merged
            reason: Human-readable reason
            evidence: Additional context (source count, alias list, etc.)
            batch_id: Optional batch ID for grouping

        Returns:
            The created ReviewCandidate
        """
        # Check for existing pending item
        conn = self._get_conn()
        existing = conn.execute("""
            SELECT id FROM entity_review_queue
            WHERE status = 'pending'
            AND review_type = 'over_merged'
            AND person_a_id = ?
        """, (person_id,)).fetchone()

        if existing:
            conn.close()
            logger.debug(f"Over-merged candidate already exists: {person_id}")
            return self.get_by_id(existing["id"])

        candidate = ReviewCandidate(
            review_type=ReviewType.OVER_MERGED.value,
            person_a_id=person_id,
            person_a_name=person_name,
            confidence=confidence,
            reason=reason,
            evidence=evidence,
            batch_id=batch_id,
        )

        self._insert(conn, candidate)
        conn.commit()
        conn.close()

        logger.info(f"Added over-merged candidate: {person_name} "
                   f"(confidence: {confidence:.2f}, reason: {reason})")
        return candidate

    def _insert(self, conn: sqlite3.Connection, candidate: ReviewCandidate) -> None:
        """Insert a candidate into the database."""
        conn.execute("""
            INSERT INTO entity_review_queue
            (id, review_type, person_a_id, person_a_name, person_b_id, person_b_name,
             confidence, reason, evidence, status, reviewed_at, batch_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            candidate.id,
            candidate.review_type,
            candidate.person_a_id,
            candidate.person_a_name,
            candidate.person_b_id,
            candidate.person_b_name,
            candidate.confidence,
            candidate.reason,
            json.dumps(candidate.evidence) if candidate.evidence else None,
            candidate.status,
            candidate.reviewed_at.isoformat() if candidate.reviewed_at else None,
            candidate.batch_id,
            candidate.created_at.isoformat(),
        ))

    def get_by_id(self, item_id: str) -> Optional[ReviewCandidate]:
        """Get a review item by ID."""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT * FROM entity_review_queue WHERE id = ?
        """, (item_id,)).fetchone()
        conn.close()

        if row:
            return ReviewCandidate.from_row(row)
        return None

    def get_pending(
        self,
        review_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReviewCandidate]:
        """
        Get pending review items, sorted by confidence descending.

        Args:
            review_type: Optional filter by type ('duplicate', 'non_human', 'over_merged')
            limit: Maximum items to return
            offset: Offset for pagination

        Returns:
            List of pending ReviewCandidate objects
        """
        conn = self._get_conn()

        if review_type:
            rows = conn.execute("""
                SELECT * FROM entity_review_queue
                WHERE status = 'pending' AND review_type = ?
                ORDER BY confidence DESC, created_at ASC
                LIMIT ? OFFSET ?
            """, (review_type, limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM entity_review_queue
                WHERE status = 'pending'
                ORDER BY confidence DESC, created_at ASC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

        conn.close()
        return [ReviewCandidate.from_row(row) for row in rows]

    def mark_reviewed(
        self,
        item_id: str,
        action: str,
    ) -> Optional[ReviewCandidate]:
        """
        Mark a review item as reviewed.

        Args:
            item_id: The review item ID
            action: The action taken ('merged', 'skipped', 'hidden', 'kept', 'split')

        Returns:
            The updated ReviewCandidate, or None if not found
        """
        valid_actions = {s.value for s in ReviewStatus if s != ReviewStatus.PENDING}
        if action not in valid_actions:
            raise ValueError(f"Invalid action: {action}. Must be one of {valid_actions}")

        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        conn.execute("""
            UPDATE entity_review_queue
            SET status = ?, reviewed_at = ?
            WHERE id = ?
        """, (action, now, item_id))
        conn.commit()

        row = conn.execute("""
            SELECT * FROM entity_review_queue WHERE id = ?
        """, (item_id,)).fetchone()
        conn.close()

        if row:
            candidate = ReviewCandidate.from_row(row)
            logger.info(f"Marked review item {item_id[:8]} as {action}")
            return candidate
        return None

    def get_stats(self) -> dict:
        """
        Get statistics about the review queue.

        Returns:
            Dict with counts by type and status
        """
        conn = self._get_conn()

        # Count by status
        status_rows = conn.execute("""
            SELECT status, COUNT(*) as count
            FROM entity_review_queue
            GROUP BY status
        """).fetchall()
        by_status = {row["status"]: row["count"] for row in status_rows}

        # Count pending by type
        type_rows = conn.execute("""
            SELECT review_type, COUNT(*) as count
            FROM entity_review_queue
            WHERE status = 'pending'
            GROUP BY review_type
        """).fetchall()
        pending_by_type = {row["review_type"]: row["count"] for row in type_rows}

        # Total counts
        total_pending = sum(pending_by_type.values())
        total_reviewed = sum(c for s, c in by_status.items() if s != "pending")

        conn.close()

        return {
            "total_pending": total_pending,
            "total_reviewed": total_reviewed,
            "by_status": by_status,
            "pending_by_type": pending_by_type,
        }

    def clear_pending(self, review_type: Optional[str] = None) -> int:
        """
        Clear all pending items (for testing or reset).

        Args:
            review_type: Optional filter by type

        Returns:
            Number of items deleted
        """
        conn = self._get_conn()

        if review_type:
            result = conn.execute("""
                DELETE FROM entity_review_queue
                WHERE status = 'pending' AND review_type = ?
            """, (review_type,))
        else:
            result = conn.execute("""
                DELETE FROM entity_review_queue
                WHERE status = 'pending'
            """)

        count = result.rowcount
        conn.commit()
        conn.close()

        logger.info(f"Cleared {count} pending review items")
        return count

    def remove_for_person(self, person_id: str) -> int:
        """
        Remove all pending review items involving a person.

        Called after a person is hidden or merged to clean up the queue.

        Args:
            person_id: The person ID to remove items for

        Returns:
            Number of items removed
        """
        conn = self._get_conn()
        result = conn.execute("""
            DELETE FROM entity_review_queue
            WHERE status = 'pending'
            AND (person_a_id = ? OR person_b_id = ?)
        """, (person_id, person_id))

        count = result.rowcount
        conn.commit()
        conn.close()

        if count > 0:
            logger.info(f"Removed {count} pending review items for person {person_id[:8]}")
        return count


# Singleton instance
_review_queue_store: Optional[ReviewQueueStore] = None


def get_review_queue_store() -> ReviewQueueStore:
    """Get the singleton ReviewQueueStore instance."""
    global _review_queue_store
    if _review_queue_store is None:
        _review_queue_store = ReviewQueueStore()
    return _review_queue_store
