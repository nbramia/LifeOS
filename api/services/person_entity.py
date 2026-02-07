"""
PersonEntity - Enhanced person model for LifeOS People System v2.

Key changes from PersonRecord:
- Email is primary identifier (emails is a list, not single optional string)
- Supports multiple emails per person
- Includes vault_contexts for context-aware resolution
- Includes confidence_score for merge quality tracking
- Includes display_name for disambiguation (e.g., "Sarah (Movement)")
"""
import fcntl
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
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
    # Formula: (recency × 0.3) + (frequency × 0.4) + (diversity × 0.3)
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
    Storage layer for PersonEntity objects.

    Provides CRUD operations and persistence to JSON file.

    IMPORTANT - ID DURABILITY WARNING:
    ==================================
    Person IDs (UUIDs) are generated ONCE when a person is first created and
    are persisted in the storage file. These IDs are:
    - Referenced by relationships, interactions, and source entities
    - Hardcoded in settings (e.g., my_person_id for the CRM owner)
    - Used in merged_person_ids.json to track person merges

    NEVER delete or rebuild people_entities.json from scratch unless absolutely
    necessary. Doing so will:
    - Generate NEW IDs for everyone, breaking all relationships
    - Invalidate hardcoded IDs like my_person_id in settings
    - Break the merge history tracking

    If you need to fix data issues, prefer:
    - Editing individual entities via the API or directly in the JSON
    - Using the merge/split functionality to reorganize people
    - Running incremental syncs (which look up existing entities by email/phone/name)
    """

    # Path to merged IDs file (secondary_id -> primary_id mapping)
    MERGED_IDS_PATH = Path(__file__).parent.parent.parent / "data" / "merged_person_ids.json"
    # Path to CRM database (shared with link_override)
    CRM_DB_PATH = Path(__file__).parent.parent.parent / "data" / "crm.db"

    def __init__(self, storage_path: str = "./data/people_entities.json"):
        """
        Initialize the entity store.

        Args:
            storage_path: Path to JSON file for persistence
        """
        self.storage_path = Path(storage_path)
        self._entities: dict[str, PersonEntity] = {}  # Keyed by entity ID
        self._email_index: dict[str, str] = {}  # email.lower() → entity ID
        self._name_index: dict[str, str] = {}  # canonical_name.lower() → entity ID
        self._phone_index: dict[str, str] = {}  # E.164 phone → entity ID
        self._merged_ids: dict[str, str] = {}  # secondary_id -> primary_id
        self._blocklist: set[str] = set()  # Blocked emails/phones (lowercase)
        self._ensure_blocklist_table()
        self._load_blocklist()
        self._load()
        self._load_merged_ids()

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

    def _ensure_blocklist_table(self) -> None:
        """Create the person_blocklist table if it doesn't exist."""
        self.CRM_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.CRM_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS person_blocklist (
                identifier TEXT PRIMARY KEY,  -- Email or phone (lowercase)
                identifier_type TEXT NOT NULL,  -- 'email' or 'phone'
                person_name TEXT,  -- Original person name (for reference)
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def _load_blocklist(self) -> None:
        """Load blocked identifiers from database."""
        try:
            conn = sqlite3.connect(self.CRM_DB_PATH)
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
        conn = sqlite3.connect(self.CRM_DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO person_blocklist
            (identifier, identifier_type, person_name, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (identifier, identifier_type, person_name, reason,
              datetime.now(timezone.utc).isoformat()))
        conn.commit()
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
        entity = self._entities.get(entity_id)
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

        # Update and save
        self.update(entity)
        self.save()

        logger.info(f"Hidden person '{entity.canonical_name}' (ID: {entity_id[:8]}), "
                    f"blocklisted {len(entity.emails)} emails, {len(entity.phone_numbers)} phones")
        return entity

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

    def _load(self) -> None:
        """Load entities from disk."""
        if not self.storage_path.exists():
            logger.info(f"No existing entity store at {self.storage_path}")
            return

        # Check for empty file
        if self.storage_path.stat().st_size == 0:
            logger.info(f"Empty entity store at {self.storage_path}")
            return

        try:
            with open(self.storage_path, "r") as f:
                data = json.load(f)

            for entity_data in data:
                entity = PersonEntity.from_dict(entity_data)
                self._entities[entity.id] = entity
                self._index_entity(entity)

            logger.info(f"Loaded {len(self._entities)} entities from {self.storage_path}")
        except Exception as e:
            logger.error(f"Failed to load entity store: {e}")

    def _index_entity(self, entity: PersonEntity) -> None:
        """Add entity to lookup indices."""
        # Email index
        for email in entity.emails:
            self._email_index[email.lower()] = entity.id

        # Name index
        if entity.canonical_name:
            self._name_index[entity.canonical_name.lower()] = entity.id

        # Alias index (also add to name index)
        for alias in entity.aliases:
            if alias:
                self._name_index[alias.lower()] = entity.id

        # Phone index
        for phone in entity.phone_numbers:
            if phone:
                self._phone_index[phone] = entity.id

    def _remove_from_indices(self, entity: PersonEntity) -> None:
        """Remove entity from lookup indices."""
        for email in entity.emails:
            self._email_index.pop(email.lower(), None)

        if entity.canonical_name:
            self._name_index.pop(entity.canonical_name.lower(), None)

        for alias in entity.aliases:
            if alias:
                self._name_index.pop(alias.lower(), None)

        for phone in entity.phone_numbers:
            if phone:
                self._phone_index.pop(phone, None)

    def save(self) -> None:
        """
        Persist entities to disk with atomic writes and rolling backups.

        Safety features:
        1. Writes to temp file first, validates JSON, then atomic rename
        2. Creates rolling backup before each save (keeps last 5)
        3. Validates entity count doesn't drastically drop (>50% loss = abort)
        """
        import shutil
        import tempfile

        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = [entity.to_dict() for entity in self._entities.values()]

        # Pre-save validation: prevent drastic entity count drops
        if self.storage_path.exists():
            try:
                with open(self.storage_path) as f:
                    old_data = json.load(f)
                old_count = len(old_data)
                new_count = len(data)
                # If we're losing more than 50% of entities, abort (likely corruption)
                if old_count > 100 and new_count < old_count * 0.5:
                    raise ValueError(
                        f"Save aborted: entity count dropped from {old_count} to {new_count} "
                        f"(>{50}% loss). This may indicate corruption. Check data before saving."
                    )
            except json.JSONDecodeError:
                pass  # Old file was corrupt, OK to overwrite

        # Create rolling backup before save
        if self.storage_path.exists():
            try:
                from config.settings import settings
                backup_dir = Path(settings.backup_path)
                backup_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = backup_dir / f"people_entities.{timestamp}.json"
                shutil.copy(self.storage_path, backup_path)
                logger.info(f"Created backup: {backup_path}")

                # Keep only last 2 backups
                backups = sorted(backup_dir.glob("people_entities.*.json"))
                for old_backup in backups[:-2]:
                    old_backup.unlink()
                    logger.debug(f"Removed old backup: {old_backup}")
            except Exception as e:
                logger.warning(f"Could not create backup: {e}")
                # Continue with save - backup failure shouldn't block saves

        # Write to temp file first (atomic write pattern)
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".json", dir=self.storage_path.parent
        )
        try:
            with os.fdopen(temp_fd, "w") as f:
                json.dump(data, f, indent=2)

            # Validate the temp file: re-read and verify count
            with open(temp_path) as f:
                validated_data = json.load(f)
            if len(validated_data) != len(data):
                raise ValueError(
                    f"Write validation failed: expected {len(data)}, got {len(validated_data)}"
                )

            # Atomic rename (same filesystem = atomic on POSIX)
            shutil.move(temp_path, self.storage_path)
            logger.info(f"Saved {len(data)} entities to {self.storage_path}")

        except Exception:
            # Clean up temp file on failure
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

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

        # Store a copy to avoid reference issues
        stored = PersonEntity.from_dict(entity.to_dict())
        self._entities[stored.id] = stored
        self._index_entity(stored)
        return stored

    def update(self, entity: PersonEntity) -> PersonEntity:
        """
        Update an existing entity.

        Args:
            entity: PersonEntity with updated data

        Returns:
            The updated entity (a copy is stored internally)
        """
        # Get the OLD stored entity (not the passed-in one which may have been modified)
        old_entity = self._entities.get(entity.id)
        if old_entity:
            self._remove_from_indices(old_entity)

        # Store a copy to avoid reference issues
        stored = PersonEntity.from_dict(entity.to_dict())
        self._entities[stored.id] = stored
        self._index_entity(stored)
        return stored

    def delete(self, entity_id: str) -> bool:
        """
        Delete an entity by ID.

        Args:
            entity_id: ID of entity to delete

        Returns:
            True if deleted, False if not found
        """
        entity = self._entities.pop(entity_id, None)
        if entity:
            self._remove_from_indices(entity)
            return True
        return False

    def get_by_id(self, entity_id: str) -> Optional[PersonEntity]:
        """
        Get entity by ID, following merge chain if needed.

        If this ID was merged into another person, returns the surviving
        primary person instead.
        """
        # Follow merge chain to get canonical ID
        canonical_id = self.get_canonical_id(entity_id)
        return self._entities.get(canonical_id)

    def get_by_email(self, email: str) -> Optional[PersonEntity]:
        """Get entity by email address (case-insensitive), following merge chain."""
        entity_id = self._email_index.get(email.lower())
        if entity_id:
            return self.get_by_id(entity_id)  # Uses canonical ID
        return None

    def get_by_phone(self, phone: str) -> Optional[PersonEntity]:
        """Get entity by phone number (E.164 format), following merge chain."""
        entity_id = self._phone_index.get(phone)
        if entity_id:
            return self.get_by_id(entity_id)  # Uses canonical ID
        return None

    def get_by_name(self, name: str) -> Optional[PersonEntity]:
        """Get entity by canonical name or alias (case-insensitive), following merge chain."""
        entity_id = self._name_index.get(name.lower())
        if entity_id:
            return self.get_by_id(entity_id)  # Uses canonical ID
        return None

    def reload_merged_ids(self) -> None:
        """Reload merged IDs mapping from disk (call after a merge operation)."""
        self._load_merged_ids()

    def search(self, query: str, limit: int = 20, include_hidden: bool = False, include_merged: bool = False) -> list[PersonEntity]:
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
        results = []

        for entity in self._entities.values():
            # Skip hidden entities unless explicitly requested
            if entity.hidden and not include_hidden:
                continue

            # Skip entities that were merged into another person
            if entity.id in self._merged_ids and not include_merged:
                continue

            # Check canonical name
            if query_lower in entity.canonical_name.lower():
                results.append(entity)
                continue

            # Check display name
            if query_lower in entity.display_name.lower():
                results.append(entity)
                continue

            # Check emails
            if any(query_lower in email.lower() for email in entity.emails):
                results.append(entity)
                continue

            # Check aliases
            if any(query_lower in alias.lower() for alias in entity.aliases):
                results.append(entity)
                continue

            if len(results) >= limit:
                break

        # Sort by last_seen (most recent first), then by name
        results.sort(
            key=lambda e: (e.last_seen or datetime.min, e.canonical_name),
            reverse=True,
        )

        return results[:limit]

    def get_all(self, include_hidden: bool = False, include_merged: bool = False) -> list[PersonEntity]:
        """
        Get all entities.

        Args:
            include_hidden: If True, include hidden entities (default: False)
            include_merged: If True, include entities that were merged into others (default: False)

        Returns:
            List of PersonEntity objects
        """
        results = []
        for entity in self._entities.values():
            # Skip hidden entities unless explicitly requested
            if entity.hidden and not include_hidden:
                continue
            # Skip entities that were merged into another person
            # (their ID is in _merged_ids as a secondary ID)
            if entity.id in self._merged_ids and not include_merged:
                continue
            results.append(entity)
        return results

    def count(self) -> int:
        """Get total number of entities."""
        return len(self._entities)

    def get_statistics(self) -> dict:
        """Get aggregate statistics about stored entities."""
        by_source: dict[str, int] = {}
        by_category: dict[str, int] = {}

        for entity in self._entities.values():
            for source in entity.sources:
                by_source[source] = by_source.get(source, 0) + 1
            by_category[entity.category] = by_category.get(entity.category, 0) + 1

        return {
            "total_entities": len(self._entities),
            "by_source": by_source,
            "by_category": by_category,
            "total_emails_indexed": len(self._email_index),
            "total_names_indexed": len(self._name_index),
            "total_phones_indexed": len(self._phone_index),
        }


# Singleton instance
_entity_store: Optional[PersonEntityStore] = None


def get_person_entity_store(
    storage_path: str = "./data/people_entities.json",
) -> PersonEntityStore:
    """
    Get or create the singleton PersonEntityStore.

    Args:
        storage_path: Path to JSON file for persistence

    Returns:
        PersonEntityStore instance
    """
    global _entity_store
    if _entity_store is None:
        _entity_store = PersonEntityStore(storage_path)
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
                set(name.lower() for name in config.get("family_exact_names", []))
            )
        except Exception as e:
            logger.warning(f"Failed to load family config: {e}")
    return set(), set()


FAMILY_LAST_NAMES, FAMILY_EXACT_NAMES = _load_family_config()


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

    work_email_domain = settings.work_email_domain if hasattr(settings, 'work_email_domain') and settings.work_email_domain else "example.com"

    # 1. Check if this is "me" (the CRM owner)
    if person.id == settings.my_person_id:
        return "self"

    # 2. Check family membership (by name)
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
    for email in person.emails:
        if email and work_email_domain in email.lower():
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
        if se.observed_email and work_email_domain in se.observed_email.lower():
            return "work"
        if se.metadata and se.metadata.get("account") == "work":
            return "work"

    # 4. Default to personal
    return "personal"
