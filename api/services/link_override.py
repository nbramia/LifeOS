"""
Link Override Service for entity resolution disambiguation.

Stores rules that override default entity resolution behavior,
ensuring that specific name/context combinations always resolve
to the intended person.
"""
import sqlite3
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LinkOverride:
    """A rule that overrides entity resolution for specific patterns."""

    id: str
    name_pattern: str  # Name to match (case-insensitive)
    source_type: Optional[str]  # 'vault', 'gmail', etc. (None = all)
    context_pattern: Optional[str]  # Path pattern like 'Work/ML/' (None = all)
    preferred_person_id: str  # Always link to this person
    rejected_person_id: Optional[str]  # Never link to this person
    reason: Optional[str]  # Why this override exists
    created_at: datetime = None

    def matches(self, name: str, source_type: str = None, context_path: str = None) -> bool:
        """
        Check if this override matches the given parameters.

        Args:
            name: Name being resolved
            source_type: Type of source (vault, gmail, etc.)
            context_path: Full path for context (e.g., '/Users/.../Work/ML/note.md')

        Returns:
            True if this override applies
        """
        # Name must match (case-insensitive)
        if self.name_pattern.lower() != name.lower():
            return False

        # Source type must match if specified
        if self.source_type and source_type:
            if self.source_type.lower() != source_type.lower():
                return False

        # Context pattern must be in path if specified
        if self.context_pattern and context_path:
            if self.context_pattern.lower() not in context_path.lower():
                return False

        return True


class LinkOverrideStore:
    """Store for link override rules."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path(__file__).parent.parent.parent / "data" / "crm.db"
        self._ensure_table()

    def _ensure_table(self):
        """Ensure the link_overrides table exists."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS link_overrides (
                id TEXT PRIMARY KEY,
                name_pattern TEXT NOT NULL,
                source_type TEXT,
                context_pattern TEXT,
                preferred_person_id TEXT NOT NULL,
                rejected_person_id TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_link_overrides_name
            ON link_overrides(name_pattern COLLATE NOCASE)
        """)
        conn.commit()
        conn.close()

    def add(self, override: LinkOverride) -> LinkOverride:
        """Add a new link override rule."""
        if not override.id:
            override.id = str(uuid.uuid4())
        if not override.created_at:
            override.created_at = datetime.now(timezone.utc)

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO link_overrides
            (id, name_pattern, source_type, context_pattern, preferred_person_id, rejected_person_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            override.id,
            override.name_pattern,
            override.source_type,
            override.context_pattern,
            override.preferred_person_id,
            override.rejected_person_id,
            override.reason,
            override.created_at.isoformat(),
        ))
        conn.commit()
        conn.close()

        logger.info(f"Created link override: '{override.name_pattern}' -> {override.preferred_person_id[:8]}")
        return override

    def get_all(self) -> list[LinkOverride]:
        """Get all link override rules."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        cursor = conn.execute("""
            SELECT * FROM link_overrides ORDER BY created_at DESC
        """)

        overrides = []
        for row in cursor:
            created_at = row['created_at']
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))

            overrides.append(LinkOverride(
                id=row['id'],
                name_pattern=row['name_pattern'],
                source_type=row['source_type'],
                context_pattern=row['context_pattern'],
                preferred_person_id=row['preferred_person_id'],
                rejected_person_id=row['rejected_person_id'],
                reason=row['reason'],
                created_at=created_at,
            ))

        conn.close()
        return overrides

    def find_matching(
        self,
        name: str,
        source_type: str = None,
        context_path: str = None
    ) -> Optional[LinkOverride]:
        """
        Find the most specific override matching the given parameters.

        Specificity order:
        1. name + source_type + context_pattern
        2. name + source_type
        3. name + context_pattern
        4. name only

        Returns:
            Most specific matching override, or None
        """
        overrides = self.get_all()

        # Score each override by specificity
        matches = []
        for override in overrides:
            if override.matches(name, source_type, context_path):
                specificity = 0
                if override.source_type:
                    specificity += 1
                if override.context_pattern:
                    specificity += 1
                matches.append((specificity, override))

        if not matches:
            return None

        # Return most specific
        matches.sort(key=lambda x: -x[0])
        return matches[0][1]

    def delete(self, override_id: str) -> bool:
        """Delete a link override rule."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("""
            DELETE FROM link_overrides WHERE id = ?
        """, (override_id,))
        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()
        return deleted

    def get_for_person(self, person_id: str) -> list[LinkOverride]:
        """Get all overrides that affect a specific person."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        cursor = conn.execute("""
            SELECT * FROM link_overrides
            WHERE preferred_person_id = ? OR rejected_person_id = ?
            ORDER BY created_at DESC
        """, (person_id, person_id))

        overrides = []
        for row in cursor:
            created_at = row['created_at']
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))

            overrides.append(LinkOverride(
                id=row['id'],
                name_pattern=row['name_pattern'],
                source_type=row['source_type'],
                context_pattern=row['context_pattern'],
                preferred_person_id=row['preferred_person_id'],
                rejected_person_id=row['rejected_person_id'],
                reason=row['reason'],
                created_at=created_at,
            ))

        conn.close()
        return overrides


# Singleton instance
_link_override_store: Optional[LinkOverrideStore] = None


def get_link_override_store() -> LinkOverrideStore:
    """Get singleton LinkOverrideStore instance."""
    global _link_override_store
    if _link_override_store is None:
        _link_override_store = LinkOverrideStore()
    return _link_override_store
