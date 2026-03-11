"""
PersonEntity - Enhanced person model for LifeOS People System v2.

Key changes from PersonRecord:
- Email is primary identifier (emails is a list, not single optional string)
- Supports multiple emails per person
- Includes vault_contexts for context-aware resolution
- Includes confidence_score for merge quality tracking
- Includes display_name for disambiguation (e.g., "Sarah (Movement)")
"""
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from api.services.people_aggregator import PersonRecord

from api.utils.datetime_utils import make_aware as _make_aware

logger = logging.getLogger(__name__)


@dataclass
class PersonEntity:
    """
    Enhanced person record with email-anchored identity.

    Primary identifier: emails list (email is most reliable cross-source anchor)
    Secondary: canonical_name + aliases for fuzzy matching
    """

    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    canonical_name: str = ""
    display_name: str = ""  # For disambiguation: "Sarah Chen (Movement)"

    # Email anchors (PRIMARY identifier)
    emails: list[str] = field(default_factory=list)

    # Professional info
    company: Optional[str] = None
    position: Optional[str] = None
    linkedin_url: Optional[str] = None

    # Context
    category: str = "unknown"  # work, personal, family
    vault_contexts: list[str] = field(default_factory=list)  # ["Work/ML/", "Personal/"]

    # Source tracking
    sources: list[str] = field(default_factory=list)  # linkedin, gmail, calendar, vault, granola

    # Timestamps
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    # Aggregated stats
    meeting_count: int = 0
    email_count: int = 0
    mention_count: int = 0
    message_count: int = 0  # iMessage/SMS count
    slack_message_count: int = 0  # Actual Slack messages (not daily summaries)
    photo_count: int = 0  # Photos with this person (Apple Photos)

    # Related content
    related_notes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    # Phone numbers (E.164 format: +1XXXXXXXXXX)
    phone_numbers: list[str] = field(default_factory=list)
    phone_primary: Optional[str] = None  # Preferred phone (mobile > business > home)

    # Resolution metadata
    confidence_score: float = 1.0  # 0.0-1.0, how confident we are in merges

    # CRM fields
    tags: list[str] = field(default_factory=list)  # User-defined tags
    notes: str = ""  # User notes about the person
    source_entity_count: int = 0  # Count of linked SourceEntity records
    birthday: Optional[str] = None  # Birthday as "MM-DD" (month-day only, no year)

    # Hidden person (soft delete)
    # When hidden=True, person is excluded from search/list results and their
    # emails/phones are added to a blocklist to prevent sync recreation.
    hidden: bool = False
    hidden_at: Optional[datetime] = None
    hidden_reason: str = ""

    # Relationship strength (computed, not stored - see relationship_metrics.py)
    # Formula: (recency × 0.3) + (frequency × 0.6) + (diversity × 0.1)
    _relationship_strength: Optional[float] = field(default=None, repr=False)

    # Peripheral contact flag - True means excluded from expensive aggregations
    # Set automatically when relationship_strength < PERIPHERAL_THRESHOLD (default 3.0)
    is_peripheral_contact: bool = False

    # Pre-computed Dunbar circle (0-6 for meaningful contacts, 7 for peripheral)
    # Calculated based on relationship_strength ranking among non-peripheral contacts
    dunbar_circle: Optional[int] = None

    def __post_init__(self):
        """Set display_name to canonical_name if not specified."""
        if not self.display_name and self.canonical_name:
            self.display_name = self.canonical_name

    @property
    def primary_email(self) -> Optional[str]:
        """Get the primary (first) email address."""
        return self.emails[0] if self.emails else None

    @property
    def relationship_strength(self) -> float:
        """
        Get computed relationship strength (0-100).

        If not computed yet, returns 0.0. Use relationship_metrics.py
        to compute and cache this value.
        """
        return self._relationship_strength if self._relationship_strength is not None else 0.0

    @relationship_strength.setter
    def relationship_strength(self, value: float) -> None:
        """Set relationship strength (0-100 scale)."""
        self._relationship_strength = max(0.0, min(100.0, value))

    def add_tag(self, tag: str) -> bool:
        """Add a tag if not already present."""
        if not tag:
            return False
        tag = tag.strip().lower()
        if tag not in self.tags:
            self.tags.append(tag)
            return True
        return False

    def remove_tag(self, tag: str) -> bool:
        """Remove a tag if present."""
        tag = tag.strip().lower()
        if tag in self.tags:
            self.tags.remove(tag)
            return True
        return False

    def has_email(self, email: str) -> bool:
        """Check if this person has a specific email (case-insensitive)."""
        email_lower = email.lower()
        return any(e.lower() == email_lower for e in self.emails)

    def add_email(self, email: str) -> bool:
        """
        Add an email if not already present (case-insensitive check).

        Returns:
            True if email was added, False if already exists
        """
        if not email:
            return False
        if not self.has_email(email):
            self.emails.append(email.lower())
            return True
        return False

    def has_phone(self, phone: str) -> bool:
        """Check if this person has a specific phone number."""
        return phone in self.phone_numbers

    def add_phone(self, phone: str) -> bool:
        """
        Add a phone number if not already present.

        Args:
            phone: E.164 format phone number (+1XXXXXXXXXX)

        Returns:
            True if phone was added, False if already exists
        """
        if not phone:
            return False
        if not self.has_phone(phone):
            self.phone_numbers.append(phone)
            # Set as primary if first phone number
            if not self.phone_primary:
                self.phone_primary = phone
            return True
        return False

    def merge(self, other: "PersonEntity") -> "PersonEntity":
        """
        Merge another entity into this one.

        Follows same logic as PersonRecord.merge() but enhanced for new fields.
        """
        # Combine emails (unique, case-insensitive)
        emails = list(self.emails)
        for email in other.emails:
            if not any(e.lower() == email.lower() for e in emails):
                emails.append(email.lower())

        # Combine sources
        sources = list(set(self.sources + other.sources))

        # Combine vault contexts
        vault_contexts = list(set(self.vault_contexts + other.vault_contexts))

        # Take earliest first_seen (use _make_aware for safe comparison)
        first_seen = self.first_seen
        if other.first_seen:
            if first_seen is None or _make_aware(other.first_seen) < _make_aware(first_seen):
                first_seen = other.first_seen

        # Take latest last_seen (use _make_aware for safe comparison)
        last_seen = self.last_seen
        if other.last_seen:
            if last_seen is None or _make_aware(other.last_seen) > _make_aware(last_seen):
                last_seen = other.last_seen

        # Sum counts
        meeting_count = self.meeting_count + other.meeting_count
        email_count = self.email_count + other.email_count
        mention_count = self.mention_count + other.mention_count
        message_count = self.message_count + other.message_count
        slack_message_count = self.slack_message_count + other.slack_message_count
        photo_count = self.photo_count + other.photo_count

        # Combine related notes
        related_notes = list(set(self.related_notes + other.related_notes))

        # Combine aliases
        aliases = list(set(self.aliases + other.aliases))

        # Combine phone numbers (unique)
        phone_numbers = list(self.phone_numbers)
        for phone in other.phone_numbers:
            if phone not in phone_numbers:
                phone_numbers.append(phone)

        # Phone primary: prefer self, then other
        phone_primary = self.phone_primary or other.phone_primary

        # Take first non-None values for single fields
        company = self.company or other.company
        position = self.position or other.position
        linkedin_url = self.linkedin_url or other.linkedin_url

        # Category: use hierarchy (family > work > personal > unknown)
        category_priority = {"family": 0, "work": 1, "personal": 2, "unknown": 3}
        self_priority = category_priority.get(self.category, 3)
        other_priority = category_priority.get(other.category, 3)
        category = self.category if self_priority <= other_priority else other.category

        # Confidence: average of both, slightly reduced for merge uncertainty
        confidence_score = (self.confidence_score + other.confidence_score) / 2 * 0.95

        # Combine tags (unique)
        tags = list(set(self.tags + other.tags))

        # Notes: concatenate if both have content
        notes = self.notes
        if other.notes and other.notes != self.notes:
            if notes:
                notes = f"{notes}\n\n---\n\n{other.notes}"
            else:
                notes = other.notes

        # Source entity count: sum
        source_entity_count = self.source_entity_count + other.source_entity_count

        # Birthday: prefer self, then other
        birthday = self.birthday or other.birthday

        return PersonEntity(
            id=self.id,  # Keep original ID
            canonical_name=self.canonical_name,
            display_name=self.display_name,
            emails=emails,
            company=company,
            position=position,
            linkedin_url=linkedin_url,
            category=category,
            vault_contexts=vault_contexts,
            sources=sources,
            first_seen=first_seen,
            last_seen=last_seen,
            meeting_count=meeting_count,
            email_count=email_count,
            mention_count=mention_count,
            message_count=message_count,
            slack_message_count=slack_message_count,
            photo_count=photo_count,
            related_notes=related_notes,
            aliases=aliases,
            phone_numbers=phone_numbers,
            phone_primary=phone_primary,
            confidence_score=confidence_score,
            tags=tags,
            notes=notes,
            source_entity_count=source_entity_count,
            birthday=birthday,
        )

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        data = asdict(self)
        # Convert datetime to ISO format strings
        if self.first_seen:
            data["first_seen"] = self.first_seen.isoformat()
        if self.last_seen:
            data["last_seen"] = self.last_seen.isoformat()
        if self.hidden_at:
            data["hidden_at"] = self.hidden_at.isoformat()
        if self.birthday:
            data["birthday"] = self.birthday  # Already "MM-DD" string
        # Remove private fields (they start with _)
        data.pop("_relationship_strength", None)
        # Add computed relationship_strength if available
        if self._relationship_strength is not None:
            data["relationship_strength"] = self._relationship_strength
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PersonEntity":
        """Create PersonEntity from dict."""
        # Parse datetime strings and ensure timezone-aware
        if data.get("first_seen") and isinstance(data["first_seen"], str):
            dt = datetime.fromisoformat(data["first_seen"])
            data["first_seen"] = _make_aware(dt)
        if data.get("last_seen") and isinstance(data["last_seen"], str):
            dt = datetime.fromisoformat(data["last_seen"])
            data["last_seen"] = _make_aware(dt)
        if data.get("hidden_at") and isinstance(data["hidden_at"], str):
            dt = datetime.fromisoformat(data["hidden_at"])
            data["hidden_at"] = _make_aware(dt)
        if data.get("birthday") and isinstance(data["birthday"], str):
            bday = data["birthday"]
            # Handle old datetime format (e.g., "2000-08-07T00:00:00+00:00") -> convert to "MM-DD"
            if "T" in bday or len(bday) > 5:
                try:
                    dt = datetime.fromisoformat(bday)
                    data["birthday"] = f"{dt.month:02d}-{dt.day:02d}"
                except ValueError:
                    data["birthday"] = None
            # Otherwise assume already in "MM-DD" format
        # Handle relationship_strength -> _relationship_strength
        if "relationship_strength" in data:
            data["_relationship_strength"] = data.pop("relationship_strength")
        # Handle legacy data without new fields
        data.setdefault("tags", [])
        data.setdefault("notes", "")
        data.setdefault("source_entity_count", 0)
        data.setdefault("message_count", 0)
        data.setdefault("slack_message_count", 0)
        data.setdefault("photo_count", 0)
        # Handle hidden fields (default to not hidden)
        data.setdefault("hidden", False)
        data.setdefault("hidden_at", None)
        data.setdefault("hidden_reason", "")
        # Handle peripheral contact fields (default to not peripheral, no circle)
        data.setdefault("is_peripheral_contact", False)
        data.setdefault("dunbar_circle", None)
        # Handle birthday field (default to None)
        data.setdefault("birthday", None)
        return cls(**data)

    @classmethod
    def from_person_record(cls, record: "PersonRecord") -> "PersonEntity":
        """
        Migrate a PersonRecord to PersonEntity.

        This is the primary migration path from the v1 system.
        """
        # Convert single email to list
        emails = [record.email.lower()] if record.email else []

        return cls(
            id=str(uuid.uuid4()),
            canonical_name=record.canonical_name,
            display_name=record.canonical_name,
            emails=emails,
            company=record.company,
            position=record.position,
            linkedin_url=record.linkedin_url,
            category=record.category,
            vault_contexts=[],  # Will be populated during re-indexing
            sources=record.sources,
            first_seen=record.first_seen,
            last_seen=record.last_seen,
            meeting_count=record.meeting_count,
            email_count=record.email_count,
            mention_count=record.mention_count,
            related_notes=record.related_notes,
            aliases=record.aliases,
            confidence_score=1.0,  # Full confidence for migrated records
        )

    def to_person_record(self) -> "PersonRecord":
        """
        Convert back to PersonRecord for backward compatibility.

        Note: Some data may be lost (multiple emails → single email, vault_contexts, confidence_score)
        """
        # Import here to avoid circular import
        from api.services.people_aggregator import PersonRecord

        return PersonRecord(
            canonical_name=self.canonical_name,
            email=self.primary_email,
            sources=self.sources,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            company=self.company,
            position=self.position,
            linkedin_url=self.linkedin_url,
            meeting_count=self.meeting_count,
            email_count=self.email_count,
            mention_count=self.mention_count,
            related_notes=self.related_notes,
            aliases=self.aliases,
            category=self.category,
        )


class PersonEntityStore:
    """
    Storage layer for PersonEntity objects using SQLite.

    Provides CRUD operations with immediate persistence to SQLite database.
    Each add/update/delete operation is committed immediately — there is no
    need to call save() (retained as a no-op for backward compatibility).

    IMPORTANT - ID DURABILITY WARNING:
    ==================================
    Person IDs (UUIDs) are generated ONCE when a person is first created and
    are persisted in the database. These IDs are:
    - Referenced by relationships, interactions, and source entities
    - Hardcoded in settings (e.g., my_person_id for the CRM owner)
    - Used in merged_person_ids.json to track person merges

    NEVER drop the person_entities table unless absolutely necessary. Doing so
    will:
    - Generate NEW IDs for everyone, breaking all relationships
    - Invalidate hardcoded IDs like my_person_id in settings
    - Break the merge history tracking

    If you need to fix data issues, prefer:
    - Editing individual entities via the API
    - Using the merge/split functionality to reorganize people
    - Running incremental syncs (which look up existing entities by email/phone/name)
    """

    # Path to merged IDs file (secondary_id -> primary_id mapping)
    MERGED_IDS_PATH = Path(__file__).parent.parent.parent / "data" / "merged_person_ids.json"
    # Path to CRM database (shared with link_override, blocklist, etc.)
    CRM_DB_PATH = Path(__file__).parent.parent.parent / "data" / "crm.db"

    # Column names for INSERT/UPDATE statements
    _COLUMNS = (
        "id", "canonical_name", "display_name", "emails", "company", "position",
        "linkedin_url", "category", "vault_contexts", "sources", "first_seen", "last_seen",
        "meeting_count", "email_count", "mention_count", "message_count", "slack_message_count", "photo_count",
        "related_notes", "aliases", "phone_numbers", "phone_primary", "confidence_score",
        "tags", "notes", "source_entity_count", "birthday", "hidden", "hidden_at",
        "hidden_reason", "relationship_strength", "is_peripheral_contact", "dunbar_circle",
    )
    _PLACEHOLDERS = ", ".join(["?"] * len(_COLUMNS))
    _COLUMNS_STR = ", ".join(_COLUMNS)

    def __init__(self, db_path: str = None):
        """
        Initialize the entity store.

        Args:
            db_path: Path to SQLite database. Defaults to ./data/crm.db.
        """
        if db_path is None:
            db_path = str(self.CRM_DB_PATH)
        self.db_path = Path(db_path)
        self._merged_ids: dict[str, str] = {}  # secondary_id -> primary_id
        self._blocklist: set[str] = set()  # Blocked emails/phones (lowercase)
        self._init_db()
        self._load_blocklist()
        self._load_merged_ids()

    def _init_db(self) -> None:
        """Create SQLite tables and indices if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS person_entities (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    emails TEXT NOT NULL DEFAULT '[]',
                    company TEXT,
                    position TEXT,
                    linkedin_url TEXT,
                    category TEXT NOT NULL DEFAULT 'unknown',
                    vault_contexts TEXT NOT NULL DEFAULT '[]',
                    sources TEXT NOT NULL DEFAULT '[]',
                    first_seen TEXT,
                    last_seen TEXT,
                    meeting_count INTEGER NOT NULL DEFAULT 0,
                    email_count INTEGER NOT NULL DEFAULT 0,
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    slack_message_count INTEGER NOT NULL DEFAULT 0,
                    photo_count INTEGER NOT NULL DEFAULT 0,
                    related_notes TEXT NOT NULL DEFAULT '[]',
                    aliases TEXT NOT NULL DEFAULT '[]',
                    phone_numbers TEXT NOT NULL DEFAULT '[]',
                    phone_primary TEXT,
                    confidence_score REAL NOT NULL DEFAULT 1.0,
                    tags TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    source_entity_count INTEGER NOT NULL DEFAULT 0,
                    birthday TEXT,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    hidden_at TEXT,
                    hidden_reason TEXT NOT NULL DEFAULT '',
                    relationship_strength REAL,
                    is_peripheral_contact INTEGER NOT NULL DEFAULT 0,
                    dunbar_circle INTEGER
                );

                CREATE TABLE IF NOT EXISTS person_emails (
                    email TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS person_phones (
                    phone TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS person_names (
                    name TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS person_blocklist (
                    identifier TEXT PRIMARY KEY,
                    identifier_type TEXT NOT NULL,
                    person_name TEXT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_pe_canonical_name ON person_entities(canonical_name);
                CREATE INDEX IF NOT EXISTS idx_pe_category ON person_entities(category);
                CREATE INDEX IF NOT EXISTS idx_pe_hidden ON person_entities(hidden);
                CREATE INDEX IF NOT EXISTS idx_pe_last_seen ON person_entities(last_seen);
                CREATE INDEX IF NOT EXISTS idx_person_emails_pid ON person_emails(person_id);
                CREATE INDEX IF NOT EXISTS idx_person_phones_pid ON person_phones(person_id);
                CREATE INDEX IF NOT EXISTS idx_person_names_pid ON person_names(person_id);
            """)
            # Migration: add photo_count column if missing
            try:
                conn.execute("ALTER TABLE person_entities ADD COLUMN photo_count INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists
            conn.commit()
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a new SQLite connection with WAL mode and row factory."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _entity_to_values(self, entity: PersonEntity) -> tuple:
        """Convert PersonEntity to tuple of values for SQL INSERT/UPDATE."""
        return (
            entity.id,
            entity.canonical_name,
            entity.display_name,
            json.dumps(entity.emails),
            entity.company,
            entity.position,
            entity.linkedin_url,
            entity.category,
            json.dumps(entity.vault_contexts),
            json.dumps(entity.sources),
            entity.first_seen.isoformat() if entity.first_seen else None,
            entity.last_seen.isoformat() if entity.last_seen else None,
            entity.meeting_count,
            entity.email_count,
            entity.mention_count,
            entity.message_count,
            entity.slack_message_count,
            entity.photo_count,
            json.dumps(entity.related_notes),
            json.dumps(entity.aliases),
            json.dumps(entity.phone_numbers),
            entity.phone_primary,
            entity.confidence_score,
            json.dumps(entity.tags),
            entity.notes,
            entity.source_entity_count,
            entity.birthday,
            1 if entity.hidden else 0,
            entity.hidden_at.isoformat() if entity.hidden_at else None,
            entity.hidden_reason,
            entity._relationship_strength,
            1 if entity.is_peripheral_contact else 0,
            entity.dunbar_circle,
        )

    def _row_to_entity(self, row: sqlite3.Row) -> PersonEntity:
        """Convert SQLite row to PersonEntity."""
        data = dict(row)
        # Parse JSON list fields
        for list_field in ('emails', 'vault_contexts', 'sources', 'related_notes',
                           'aliases', 'phone_numbers', 'tags'):
            val = data.get(list_field)
            data[list_field] = json.loads(val) if val else []
        # Convert SQLite integer to boolean
        data['hidden'] = bool(data.get('hidden', 0))
        data['is_peripheral_contact'] = bool(data.get('is_peripheral_contact', 0))
        return PersonEntity.from_dict(data)

    def _update_lookup_tables(self, conn: sqlite3.Connection, entity: PersonEntity) -> None:
        """Update email, phone, and name lookup tables for an entity."""
        pid = entity.id
        conn.execute("DELETE FROM person_emails WHERE person_id = ?", (pid,))
        conn.execute("DELETE FROM person_phones WHERE person_id = ?", (pid,))
        conn.execute("DELETE FROM person_names WHERE person_id = ?", (pid,))

        for email in entity.emails:
            conn.execute(
                "INSERT OR REPLACE INTO person_emails (email, person_id) VALUES (?, ?)",
                (email.lower(), pid))
        for phone in entity.phone_numbers:
            if phone:
                conn.execute(
                    "INSERT OR REPLACE INTO person_phones (phone, person_id) VALUES (?, ?)",
                    (phone, pid))
        if entity.canonical_name:
            conn.execute(
                "INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
                (entity.canonical_name.lower(), pid))
        for alias in entity.aliases:
            if alias:
                conn.execute(
                    "INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
                    (alias.lower(), pid))

    # --- Blocklist methods ---

    def _load_blocklist(self) -> None:
        """Load blocked identifiers from database."""
        try:
            conn = self._get_connection()
            cursor = conn.execute("SELECT identifier FROM person_blocklist")
            self._blocklist = {row[0] for row in cursor}
            conn.close()
            if self._blocklist:
                logger.info(f"Loaded {len(self._blocklist)} blocked identifiers")
        except Exception as e:
            logger.warning(f"Failed to load blocklist: {e}")

    def is_blocked(self, identifier: str) -> bool:
        """
        Check if an email or phone is blocked.

        Args:
            identifier: Email or phone number to check

        Returns:
            True if blocked, False otherwise
        """
        return identifier.lower() in self._blocklist

    def _add_to_blocklist(self, identifier: str, identifier_type: str,
                          person_name: str, reason: str) -> None:
        """Add an identifier to the blocklist."""
        identifier = identifier.lower()
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO person_blocklist
                (identifier, identifier_type, person_name, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (identifier, identifier_type, person_name, reason,
                  datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
        self._blocklist.add(identifier)

    def hide_person(self, entity_id: str, reason: str = "") -> Optional[PersonEntity]:
        """
        Hide a person (soft delete) and blocklist their identifiers.

        This marks the person as hidden and adds all their emails/phones to
        a blocklist to prevent sync from recreating them.

        Args:
            entity_id: ID of the person to hide
            reason: Optional reason for hiding (e.g., "fake marketing persona")

        Returns:
            The hidden PersonEntity, or None if not found
        """
        entity = self.get_by_id(entity_id)
        if not entity:
            return None

        # Mark as hidden
        entity.hidden = True
        entity.hidden_at = datetime.now(timezone.utc)
        entity.hidden_reason = reason

        # Add all emails and phones to blocklist
        for email in entity.emails:
            self._add_to_blocklist(email, "email", entity.canonical_name, reason)
        for phone in entity.phone_numbers:
            self._add_to_blocklist(phone, "phone", entity.canonical_name, reason)

        # Update in database
        self.update(entity)

        logger.info(f"Hidden person '{entity.canonical_name}' (ID: {entity_id[:8]}), "
                    f"blocklisted {len(entity.emails)} emails, {len(entity.phone_numbers)} phones")
        return entity

    def purge_hidden(self, older_than_days: int = 90) -> int:
        """
        Permanently delete merge-hidden entities older than the retention period.

        Only deletes entities where hidden_reason starts with 'merged_into:'
        and hidden_at is older than `older_than_days` days ago. Entities hidden
        via hide_person() (blocklist) are not affected.

        Does NOT remove entries from merged_person_ids.json (those must persist
        to prevent entity resolution from recreating duplicates).

        Args:
            older_than_days: Minimum age in days before purging (default 90)

        Returns:
            Number of entities permanently deleted
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()

        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT id FROM person_entities WHERE hidden = 1 AND hidden_at IS NOT NULL AND hidden_at < ?"
                " AND hidden_reason LIKE 'merged_into:%'",
                (cutoff,),
            ).fetchall()

            count = 0
            for (entity_id,) in rows:
                conn.execute("DELETE FROM person_emails WHERE person_id = ?", (entity_id,))
                conn.execute("DELETE FROM person_phones WHERE person_id = ?", (entity_id,))
                conn.execute("DELETE FROM person_names WHERE person_id = ?", (entity_id,))
                conn.execute("DELETE FROM person_entities WHERE id = ?", (entity_id,))
                count += 1

            if count > 0:
                conn.commit()
                logger.info(f"Purged {count} hidden entities older than {older_than_days} days")

            return count
        finally:
            conn.close()

    # --- Merge chain methods ---

    def get_canonical_id(self, person_id: str) -> str:
        """
        Get the canonical (primary) person ID, following merge chain if needed.

        This ensures that if a person was merged into another, we always
        return the surviving primary ID.
        """
        visited = set()
        while person_id in self._merged_ids and person_id not in visited:
            visited.add(person_id)
            person_id = self._merged_ids[person_id]
        return person_id

    def _load_merged_ids(self) -> None:
        """Load the merged IDs mapping for durability."""
        if self.MERGED_IDS_PATH.exists():
            try:
                with open(self.MERGED_IDS_PATH) as f:
                    self._merged_ids = json.load(f)
                if self._merged_ids:
                    logger.info(f"Loaded {len(self._merged_ids)} merged ID mappings")
            except Exception as e:
                logger.warning(f"Failed to load merged IDs: {e}")

    def reload_merged_ids(self) -> None:
        """Reload merged IDs mapping from disk (call after a merge operation)."""
        self._load_merged_ids()

    # --- CRUD methods ---

    def save(self) -> None:
        """
        No-op: SQLite persists changes immediately on each operation.

        Retained for backward compatibility with code that calls save()
        after batch add/update operations. With SQLite, each add/update/delete
        is committed immediately, so explicit save() is no longer needed.
        """
        pass

    def add(self, entity: PersonEntity) -> Optional[PersonEntity]:
        """
        Add a new entity to the store.

        Checks blocklist before adding - if any email or phone is blocked,
        the entity is not added (prevents sync from recreating hidden people).

        Args:
            entity: PersonEntity to add

        Returns:
            The added entity, or None if blocked
        """
        # Check blocklist - reject if any identifier is blocked
        for email in entity.emails:
            if self.is_blocked(email):
                logger.info(f"Blocked adding entity '{entity.canonical_name}': "
                            f"email {email} is blocklisted")
                return None
        for phone in entity.phone_numbers:
            if self.is_blocked(phone):
                logger.info(f"Blocked adding entity '{entity.canonical_name}': "
                            f"phone {phone} is blocklisted")
                return None

        values = self._entity_to_values(entity)
        conn = self._get_connection()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO person_entities ({self._COLUMNS_STR}) "
                f"VALUES ({self._PLACEHOLDERS})", values)
            self._update_lookup_tables(conn, entity)
            conn.commit()
        finally:
            conn.close()

        # Return a copy to avoid reference issues
        return PersonEntity.from_dict(entity.to_dict())

    def update(self, entity: PersonEntity) -> PersonEntity:
        """
        Update an existing entity.

        Args:
            entity: PersonEntity with updated data

        Returns:
            The updated entity (a copy is stored internally)
        """
        values = self._entity_to_values(entity)
        conn = self._get_connection()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO person_entities ({self._COLUMNS_STR}) "
                f"VALUES ({self._PLACEHOLDERS})", values)
            self._update_lookup_tables(conn, entity)
            conn.commit()
        finally:
            conn.close()

        return PersonEntity.from_dict(entity.to_dict())

    def delete(self, entity_id: str) -> bool:
        """
        Delete an entity by ID.

        Args:
            entity_id: ID of entity to delete

        Returns:
            True if deleted, False if not found
        """
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM person_entities WHERE id = ?", (entity_id,)).fetchone()
            if not row:
                return False

            # Safety guard: refuse to delete if interactions exist
            from api.services.interaction_store import get_interaction_store
            int_store = get_interaction_store()
            if int_store.get_for_person(entity_id, limit=1):
                raise ValueError(
                    f"Cannot delete person {entity_id}: has interactions. "
                    "Use merge_people.py to merge, or delete interactions first."
                )

            conn.execute("DELETE FROM person_emails WHERE person_id = ?", (entity_id,))
            conn.execute("DELETE FROM person_phones WHERE person_id = ?", (entity_id,))
            conn.execute("DELETE FROM person_names WHERE person_id = ?", (entity_id,))
            conn.execute("DELETE FROM person_entities WHERE id = ?", (entity_id,))
            conn.commit()
            return True
        finally:
            conn.close()

    def get_by_id(self, entity_id: str) -> Optional[PersonEntity]:
        """
        Get entity by ID, following merge chain if needed.

        If this ID was merged into another person, returns the surviving
        primary person instead.
        """
        canonical_id = self.get_canonical_id(entity_id)
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM person_entities WHERE id = ?", (canonical_id,)).fetchone()
            if row:
                return self._row_to_entity(row)
            return None
        finally:
            conn.close()

    def get_by_email(self, email: str) -> Optional[PersonEntity]:
        """Get entity by email address (case-insensitive), following merge chain."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT person_id FROM person_emails WHERE email = ?",
                (email.lower(),)).fetchone()
            if row:
                return self.get_by_id(row['person_id'])
            return None
        finally:
            conn.close()

    def get_by_phone(self, phone: str) -> Optional[PersonEntity]:
        """Get entity by phone number (E.164 format), following merge chain."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT person_id FROM person_phones WHERE phone = ?",
                (phone,)).fetchone()
            if row:
                return self.get_by_id(row['person_id'])
            return None
        finally:
            conn.close()

    def get_by_name(self, name: str) -> Optional[PersonEntity]:
        """Get entity by canonical name or alias (case-insensitive), following merge chain."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT person_id FROM person_names WHERE name = ?",
                (name.lower(),)).fetchone()
            if row:
                return self.get_by_id(row['person_id'])
            return None
        finally:
            conn.close()

    def search(self, query: str, limit: int = 20, include_hidden: bool = False,
               include_merged: bool = False) -> list[PersonEntity]:
        """
        Search entities by name, email, or alias.

        Args:
            query: Search string
            limit: Maximum results to return
            include_hidden: If True, include hidden entities (default: False)
            include_merged: If True, include entities that were merged into others (default: False)

        Returns:
            List of matching entities
        """
        query_lower = query.lower()
        merged_ids = set(self._merged_ids.keys()) if not include_merged else set()
        pattern = f"%{query_lower}%"

        conn = self._get_connection()
        try:
            conditions = [
                "(LOWER(canonical_name) LIKE ? OR LOWER(display_name) LIKE ? "
                "OR LOWER(emails) LIKE ? OR LOWER(aliases) LIKE ?)"
            ]
            params: list = [pattern, pattern, pattern, pattern]

            if not include_hidden:
                conditions.append("hidden = 0")

            where = " AND ".join(conditions)
            sql = (f"SELECT * FROM person_entities WHERE {where} "
                   f"ORDER BY last_seen DESC, canonical_name ASC")

            rows = conn.execute(sql, params).fetchall()

            results = []
            for row in rows:
                if row['id'] in merged_ids:
                    continue
                results.append(self._row_to_entity(row))
                if len(results) >= limit:
                    break

            return results
        finally:
            conn.close()

    def get_all(self, include_hidden: bool = False,
                include_merged: bool = False) -> list[PersonEntity]:
        """
        Get all entities.

        Args:
            include_hidden: If True, include hidden entities (default: False)
            include_merged: If True, include entities that were merged into others (default: False)

        Returns:
            List of PersonEntity objects
        """
        merged_ids = set(self._merged_ids.keys()) if not include_merged else set()

        conn = self._get_connection()
        try:
            if include_hidden:
                rows = conn.execute("SELECT * FROM person_entities").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM person_entities WHERE hidden = 0").fetchall()

            results = []
            for row in rows:
                if row['id'] in merged_ids:
                    continue
                results.append(self._row_to_entity(row))

            return results
        finally:
            conn.close()

    def count(self) -> int:
        """Get total number of entities."""
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT COUNT(*) FROM person_entities").fetchone()
            return row[0]
        finally:
            conn.close()

    def get_statistics(self) -> dict:
        """Get aggregate statistics about stored entities."""
        conn = self._get_connection()
        try:
            total = conn.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0]
            email_count = conn.execute("SELECT COUNT(*) FROM person_emails").fetchone()[0]
            name_count = conn.execute("SELECT COUNT(*) FROM person_names").fetchone()[0]
            phone_count = conn.execute("SELECT COUNT(*) FROM person_phones").fetchone()[0]

            # Category breakdown
            by_category: dict[str, int] = {}
            for row in conn.execute(
                    "SELECT category, COUNT(*) as cnt FROM person_entities GROUP BY category"):
                by_category[row['category']] = row['cnt']

            # Source breakdown (parse JSON column)
            by_source: dict[str, int] = {}
            for row in conn.execute("SELECT sources FROM person_entities"):
                sources = json.loads(row['sources']) if row['sources'] else []
                for source in sources:
                    by_source[source] = by_source.get(source, 0) + 1

            return {
                "total_entities": total,
                "by_source": by_source,
                "by_category": by_category,
                "total_emails_indexed": email_count,
                "total_names_indexed": name_count,
                "total_phones_indexed": phone_count,
            }
        finally:
            conn.close()

    def export_json(self, path: str = None) -> str:
        """
        Export all entities to JSON file for backup.

        Args:
            path: Output file path. Defaults to ./data/people_entities.json.

        Returns:
            The path of the exported file.
        """
        if path is None:
            path = str(self.db_path.parent / "people_entities.json")
        entities = self.get_all(include_hidden=True, include_merged=True)
        data = [e.to_dict() for e in entities]
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info(f"Exported {len(data)} entities to {path}")
        return path


# Singleton instance
_entity_store: Optional[PersonEntityStore] = None


def get_person_entity_store(db_path: str = None) -> PersonEntityStore:
    """
    Get or create the singleton PersonEntityStore.

    Args:
        db_path: Path to SQLite database. Defaults to ./data/crm.db.

    Returns:
        PersonEntityStore instance
    """
    global _entity_store
    if _entity_store is None:
        _entity_store = PersonEntityStore(db_path)
    return _entity_store


# ============================================================================
# Category Computation (shared by routes and services)
# ============================================================================

def _load_family_config():
    """Load family configuration from JSON file."""
    config_path = Path(__file__).parent.parent.parent / "config" / "family_members.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
            return (
                set(name.lower() for name in config.get("family_last_names", [])),
                set(name.lower() for name in config.get("family_exact_names", [])),
                set(config.get("family_person_ids", []))
            )
        except Exception as e:
            logger.warning(f"Failed to load family config: {e}")
    return set(), set(), set()


FAMILY_LAST_NAMES, FAMILY_EXACT_NAMES, FAMILY_PERSON_IDS = _load_family_config()


def _is_family_member(name: str) -> bool:
    """Check if a name matches family criteria."""
    if not name:
        return False
    name_lower = name.lower().strip()

    # Check exact name match
    if name_lower in FAMILY_EXACT_NAMES:
        return True

    # Check last name match
    name_parts = name_lower.split()
    if name_parts:
        last_name = name_parts[-1]
        if last_name in FAMILY_LAST_NAMES:
            return True

    return False


def compute_person_category(person: PersonEntity, source_entities: list = None) -> str:
    """
    Compute category with priority: self → family → work → personal.

    Rules (in order):
    1. Is the CRM owner (my_person_id) → self
    2. Has family last name or exact name match → family
    3. Has Slack or work domain email (LIFEOS_WORK_DOMAIN) → work
    4. Otherwise → personal
    """
    # Import here to avoid circular dependency
    from config.settings import settings
    from api.services.source_entity import get_source_entity_store

    work_email_domains = settings.work_email_domains if settings.work_email_domains else ["example.com"]

    # 1. Check if this is "me" (the CRM owner)
    if person.id == settings.my_person_id:
        return "self"

    # 2. Check family membership (by ID first, then by name)
    if person.id in FAMILY_PERSON_IDS:
        return "family"
    if _is_family_member(person.canonical_name):
        return "family"
    # Also check display name and aliases
    if _is_family_member(person.display_name):
        return "family"
    for alias in person.aliases:
        if _is_family_member(alias):
            return "family"

    # 3. Check for work indicators
    # Check person's own emails first
    for domain in work_email_domains:
        for email in person.emails:
            if email and domain in email.lower():
                return "work"

    # Check sources list for slack
    if "slack" in person.sources:
        return "work"

    # If no source entities provided, fetch them
    if source_entities is None:
        source_store = get_source_entity_store()
        source_entities = source_store.get_for_person(person.id, limit=500)

    for se in source_entities:
        if se.source_type == "slack":
            return "work"
        for domain in work_email_domains:
            if se.observed_email and domain in se.observed_email.lower():
                return "work"
        if se.metadata and se.metadata.get("account") in ("work", "work2"):
            return "work"

    # 4. Default to personal
    return "personal"
