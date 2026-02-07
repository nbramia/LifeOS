"""
Apple Photos Sync Service - Sync face recognition data to LifeOS CRM.

Creates SourceEntity and Interaction records from Photos face appearances.
Uses Contact UUID matching for reliable person identification.

Strategy:
1. Only sync people with ZPERSONURI (linked to Apple Contacts)
2. Parse Contact UUID from ZPERSONURI
3. Query Apple Contacts to get email/phone
4. Match to PersonEntity by email/phone
5. Create SourceEntity/Interaction records
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from api.services.apple_photos import (
    ApplePhotosReader,
    PhotosPerson,
    PhotoAsset,
    get_apple_photos_reader,
)
from api.services.apple_contacts import get_contacts_reader
from api.services.source_entity import (
    SourceEntity,
    SourceEntityStore,
    get_source_entity_store,
)
from api.services.interaction_store import (
    Interaction,
    InteractionStore,
    get_interaction_store,
)
from api.services.person_entity import PersonEntity, get_person_entity_store

logger = logging.getLogger(__name__)

SOURCE_TYPE_PHOTOS = "photos"


@dataclass
class SyncStats:
    """Statistics from a sync operation."""
    photos_people_total: int = 0
    photos_people_with_contacts: int = 0
    contact_lookups_attempted: int = 0
    contact_lookups_succeeded: int = 0
    person_matches: int = 0
    source_entities_created: int = 0
    interactions_created: int = 0
    errors: int = 0

    def to_dict(self) -> dict:
        return {
            "photos_people_total": self.photos_people_total,
            "photos_people_with_contacts": self.photos_people_with_contacts,
            "contact_lookups_attempted": self.contact_lookups_attempted,
            "contact_lookups_succeeded": self.contact_lookups_succeeded,
            "person_matches": self.person_matches,
            "source_entities_created": self.source_entities_created,
            "interactions_created": self.interactions_created,
            "errors": self.errors,
        }


def parse_contact_uuid(person_uri: str) -> Optional[str]:
    """
    Parse Apple Contact UUID from Photos ZPERSONURI.

    ZPERSONURI format: "UUID:ABPerson" (e.g., "FD8F0867-9242-4CDB-AD73-BBBC9325706D:ABPerson")

    Args:
        person_uri: ZPERSONURI value from Photos database

    Returns:
        Contact UUID or None if invalid format
    """
    if not person_uri:
        return None
    if ":ABPerson" in person_uri:
        return person_uri.replace(":ABPerson", "")
    # Try splitting on any colon
    parts = person_uri.split(":")
    if len(parts) >= 1 and len(parts[0]) == 36:  # UUID length
        return parts[0]
    return None


class ApplePhotosSync:
    """
    Sync Apple Photos face recognition data to LifeOS CRM.

    Creates SourceEntity and Interaction records for matched people.
    """

    def __init__(
        self,
        photos_reader: Optional[ApplePhotosReader] = None,
        source_store: Optional[SourceEntityStore] = None,
        interaction_store: Optional[InteractionStore] = None,
    ):
        self.photos_reader = photos_reader
        self.source_store = source_store
        self.interaction_store = interaction_store
        self.contacts_reader = get_contacts_reader()
        self.person_store = get_person_entity_store()

        # Cache for contact UUID -> PersonEntity ID mapping
        self._contact_to_person_cache: dict[str, Optional[str]] = {}

    def _get_photos_reader(self) -> ApplePhotosReader:
        if self.photos_reader is None:
            self.photos_reader = get_apple_photos_reader()
        return self.photos_reader

    def _get_source_store(self) -> SourceEntityStore:
        if self.source_store is None:
            self.source_store = get_source_entity_store()
        return self.source_store

    def _get_interaction_store(self) -> InteractionStore:
        if self.interaction_store is None:
            self.interaction_store = get_interaction_store()
        return self.interaction_store

    def match_photos_person_to_entity(
        self,
        photos_person: PhotosPerson,
    ) -> Optional[str]:
        """
        Match a Photos person to a LifeOS PersonEntity.

        Strategy (in order of preference):
        1. Parse Contact UUID from ZPERSONURI
        2. Query Apple Contacts by UUID
        3. Match PersonEntity by email or phone
        4. Fallback: Match by exact name

        Args:
            photos_person: PhotosPerson from Photos database

        Returns:
            PersonEntity ID if matched, None otherwise
        """
        # Check name cache first
        cache_key = f"name:{photos_person.full_name}"
        if cache_key in self._contact_to_person_cache:
            return self._contact_to_person_cache[cache_key]

        person_id = None

        # Strategy 1: Try Contact UUID matching if available
        if photos_person.person_uri:
            contact_uuid = parse_contact_uuid(photos_person.person_uri)
            if contact_uuid:
                uuid_cache_key = f"uuid:{contact_uuid}"
                if uuid_cache_key in self._contact_to_person_cache:
                    cached = self._contact_to_person_cache[uuid_cache_key]
                    if cached:
                        return cached
                else:
                    # Query Apple Contacts
                    contact = self.contacts_reader.get_contact_by_identifier(contact_uuid)
                    if contact:
                        # Try to match by email first (most reliable)
                        for email_entry in contact.emails:
                            email = email_entry.get("value", "").lower()
                            if email:
                                person = self.person_store.get_by_email(email)
                                if person:
                                    person_id = person.id
                                    break

                        # Try phone if no email match
                        if not person_id:
                            for phone_entry in contact.phones:
                                phone = phone_entry.get("value", "")
                                if phone:
                                    person = self.person_store.get_by_phone(phone)
                                    if person:
                                        person_id = person.id
                                        break

                        self._contact_to_person_cache[uuid_cache_key] = person_id

        # Strategy 2: Fallback to exact name matching
        if not person_id:
            person = self.person_store.get_by_name(photos_person.full_name)
            if person:
                person_id = person.id
                logger.debug(f"Matched '{photos_person.full_name}' by name to {person.canonical_name}")

        self._contact_to_person_cache[cache_key] = person_id
        return person_id

    def sync_all(self, since: Optional[datetime] = None) -> SyncStats:
        """
        Sync all Photos face recognition data to LifeOS.

        Args:
            since: Only sync faces from photos after this timestamp (for incremental sync)

        Returns:
            SyncStats with sync results
        """
        stats = SyncStats()

        try:
            photos_reader = self._get_photos_reader()
            source_store = self._get_source_store()
            interaction_store = self._get_interaction_store()

            # Get all named people from Photos
            all_people = photos_reader.get_all_people()
            stats.photos_people_total = len(all_people)
            logger.info(f"Found {len(all_people)} named people in Photos")

            # Filter to people with contact links
            people_with_contacts = [
                p for p in all_people if p.person_uri
            ]
            stats.photos_people_with_contacts = len(people_with_contacts)
            logger.info(f"Found {len(people_with_contacts)} people linked to Contacts")

            # Match each Photos person to PersonEntity
            photos_person_to_entity: dict[int, str] = {}

            for photos_person in people_with_contacts:
                stats.contact_lookups_attempted += 1

                contact_uuid = parse_contact_uuid(photos_person.person_uri)
                if contact_uuid:
                    stats.contact_lookups_succeeded += 1

                entity_id = self.match_photos_person_to_entity(photos_person)
                if entity_id:
                    photos_person_to_entity[photos_person.pk] = entity_id
                    stats.person_matches += 1
                    logger.debug(
                        f"Matched Photos person '{photos_person.full_name}' "
                        f"to PersonEntity {entity_id}"
                    )

            logger.info(f"Matched {stats.person_matches} people to PersonEntity records")

            # Now sync photos for each matched person
            for photos_pk, entity_id in photos_person_to_entity.items():
                # Find the PhotosPerson for this pk
                photos_person = next(
                    (p for p in people_with_contacts if p.pk == photos_pk),
                    None,
                )
                if not photos_person:
                    continue

                try:
                    created_sources, created_interactions = self._sync_person_photos(
                        photos_person=photos_person,
                        entity_id=entity_id,
                        source_store=source_store,
                        interaction_store=interaction_store,
                        since=since,
                    )
                    stats.source_entities_created += created_sources
                    stats.interactions_created += created_interactions
                except Exception as e:
                    logger.error(
                        f"Error syncing photos for {photos_person.full_name}: {e}"
                    )
                    stats.errors += 1

        except FileNotFoundError as e:
            logger.warning(f"Photos database not available: {e}")
            stats.errors += 1
        except Exception as e:
            logger.error(f"Error in Photos sync: {e}")
            stats.errors += 1

        logger.info(
            f"Photos sync complete: {stats.source_entities_created} sources, "
            f"{stats.interactions_created} interactions created"
        )
        return stats

    def _sync_person_photos(
        self,
        photos_person: PhotosPerson,
        entity_id: str,
        source_store: SourceEntityStore,
        interaction_store: InteractionStore,
        since: Optional[datetime] = None,
    ) -> tuple[int, int]:
        """
        Sync photos for a single matched person.

        Returns:
            Tuple of (source_entities_created, interactions_created)
        """
        photos_reader = self._get_photos_reader()

        # Get photos for this person (high limit to capture all photos)
        photos = photos_reader.get_photos_for_person(photos_person.pk, limit=5000)

        # Filter by since if provided
        if since:
            photos = [p for p in photos if p.timestamp and p.timestamp >= since]

        sources_created = 0
        interactions_created = 0

        for photo in photos:
            if not photo.uuid:
                continue

            # Create unique source_id: asset_uuid:person_pk
            source_id = f"{photo.uuid}:{photos_person.pk}"

            # Check if SourceEntity already exists
            existing = source_store.get_by_source(SOURCE_TYPE_PHOTOS, source_id)
            if existing:
                continue

            # Create SourceEntity
            source_entity = SourceEntity(
                source_type=SOURCE_TYPE_PHOTOS,
                source_id=source_id,
                observed_name=photos_person.full_name,
                canonical_person_id=entity_id,
                link_confidence=0.95,  # High confidence from contact UUID match
                observed_at=photo.timestamp or datetime.now(timezone.utc),
                metadata={
                    "photos_person_pk": photos_person.pk,
                    "asset_uuid": photo.uuid,
                    "latitude": photo.latitude,
                    "longitude": photo.longitude,
                },
            )
            source_store.add(source_entity)
            sources_created += 1

            # Create Interaction for timeline
            interaction_id = str(uuid.uuid4())
            interaction = Interaction(
                id=interaction_id,
                person_id=entity_id,
                timestamp=photo.timestamp or datetime.now(timezone.utc),
                source_type=SOURCE_TYPE_PHOTOS,
                title="Photo",
                source_link=f"photos://asset/{photo.uuid}",
                source_id=photo.uuid,
            )

            # Check if interaction already exists
            existing_interactions = interaction_store.get_for_person(
                entity_id,
                source_type=SOURCE_TYPE_PHOTOS,
            )
            already_exists = any(
                i.source_id == photo.uuid for i in existing_interactions
            )
            if not already_exists:
                interaction_store.add(interaction)
                interactions_created += 1

        return sources_created, interactions_created


def sync_apple_photos(
    since: Optional[datetime] = None,
) -> dict:
    """
    Run Apple Photos sync.

    Args:
        since: Only sync photos after this timestamp

    Returns:
        Sync statistics dict
    """
    syncer = ApplePhotosSync()
    stats = syncer.sync_all(since=since)
    return stats.to_dict()
