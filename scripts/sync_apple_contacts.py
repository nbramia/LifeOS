#!/usr/bin/env python3
"""
Sync Apple Contacts to LifeOS CRM.

Creates SourceEntity records and links them to PersonEntity via entity resolution.
Updates PersonEntity with contact data (company, position, phones).

Requires:
- macOS
- pyobjc-framework-Contacts
- Contacts permission granted
"""
import logging
import argparse
from datetime import datetime, timezone

from api.services.apple_contacts import (
    get_contacts_reader,
    create_contact_source_entity,
    SOURCE_CONTACTS,
)
from api.services.source_entity import get_source_entity_store, LINK_STATUS_AUTO
from api.services.entity_resolver import get_entity_resolver
from api.services.person_entity import get_person_entity_store

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format."""
    import re
    # Remove all non-digit characters
    digits = re.sub(r'\D', '', phone)

    # Handle US numbers
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    elif len(digits) > 10:
        return f"+{digits}"
    return phone  # Return original if can't normalize


def sync_apple_contacts(dry_run: bool = True) -> dict:
    """
    Sync Apple Contacts to CRM.

    Args:
        dry_run: If True, don't actually modify data

    Returns:
        Stats dict
    """
    stats = {
        'contacts_read': 0,
        'source_entities_created': 0,
        'source_entities_updated': 0,
        'persons_linked': 0,
        'persons_created': 0,
        'persons_updated': 0,
        'birthdays_synced': 0,
        'entities_retrolinked': 0,  # Source entities retroactively linked
        'skipped': 0,
        'errors': 0,
    }

    reader = get_contacts_reader()

    if not reader.is_available:
        logger.error("Apple Contacts not available. Install pyobjc-framework-Contacts.")
        stats['error'] = "Apple Contacts not available"
        return stats

    auth_status = reader.check_authorization()
    if auth_status != "authorized":
        logger.error(f"Contacts access not authorized: {auth_status}")
        logger.info("Grant permission in System Preferences > Privacy & Security > Contacts")
        stats['error'] = f"Not authorized: {auth_status}"
        return stats

    logger.info("Reading Apple Contacts...")
    contacts = reader.get_all_contacts()
    stats['contacts_read'] = len(contacts)
    logger.info(f"Found {len(contacts)} contacts")

    source_store = get_source_entity_store()
    person_store = get_person_entity_store()
    resolver = get_entity_resolver()

    for contact in contacts:
        try:
            # Skip contacts without meaningful identity
            if not contact.display_name or contact.display_name == contact.identifier:
                stats['skipped'] += 1
                continue

            # Skip contacts without email or phone
            if not contact.primary_email and not contact.primary_phone:
                stats['skipped'] += 1
                continue

            # Create/update SourceEntity
            source_entity = create_contact_source_entity(contact)

            existing_source = source_store.get_by_source(SOURCE_CONTACTS, contact.identifier)
            if existing_source:
                if not dry_run:
                    existing_source.observed_name = source_entity.observed_name
                    existing_source.observed_email = source_entity.observed_email
                    existing_source.observed_phone = source_entity.observed_phone
                    existing_source.metadata = source_entity.metadata
                    existing_source.observed_at = datetime.now(timezone.utc)
                    source_store.update(existing_source)
                stats['source_entities_updated'] += 1
                source_entity = existing_source
            else:
                if not dry_run:
                    source_entity = source_store.add(source_entity)
                stats['source_entities_created'] += 1

            # Normalize phone for lookup
            phone_normalized = None
            if contact.primary_phone:
                phone_normalized = normalize_phone(contact.primary_phone)

            # Resolve to PersonEntity
            result = resolver.resolve(
                name=contact.display_name,
                email=contact.primary_email,
                phone=phone_normalized,
                create_if_missing=True,
            )

            if result and result.entity:
                person = result.entity
                person_updated = False

                # Track original emails/phones BEFORE modifications for retroactive linking
                original_emails = set(e.lower() for e in person.emails)
                original_phones = set(person.phone_numbers)

                # Link source entity to person
                if source_entity.canonical_person_id != person.id:
                    if not dry_run:
                        source_store.link_to_person(
                            source_entity.id,
                            person.id,
                            confidence=0.9,
                            status=LINK_STATUS_AUTO,
                        )
                    stats['persons_linked'] += 1

                # Update person with contact data
                if contact.organization and not person.company:
                    person.company = contact.organization
                    person_updated = True

                if contact.job_title and not person.position:
                    person.position = contact.job_title
                    person_updated = True

                # Add phones if not present
                if phone_normalized and phone_normalized not in person.phone_numbers:
                    person.phone_numbers.append(phone_normalized)
                    if not person.phone_primary:
                        person.phone_primary = phone_normalized
                    person_updated = True

                # Add all phones from contact
                for phone_entry in contact.phones:
                    phone_val = normalize_phone(phone_entry['value'])
                    if phone_val and phone_val not in person.phone_numbers:
                        person.phone_numbers.append(phone_val)
                        person_updated = True

                # Add email if not present
                if contact.primary_email:
                    email_lower = contact.primary_email.lower()
                    if email_lower not in [e.lower() for e in person.emails]:
                        person.emails.append(email_lower)
                        person_updated = True

                # Add all emails from contact
                for email_entry in contact.emails:
                    email_val = email_entry['value'].lower()
                    if email_val not in [e.lower() for e in person.emails]:
                        person.emails.append(email_val)
                        person_updated = True

                # Add source
                if 'contacts' not in person.sources:
                    person.sources.append('contacts')
                    person_updated = True

                # Update birthday if contact has one and person doesn't
                if contact.birthday and not person.birthday:
                    # Convert datetime to "MM-DD" format
                    person.birthday = f"{contact.birthday.month:02d}-{contact.birthday.day:02d}"
                    person_updated = True
                    stats['birthdays_synced'] += 1

                # Update source_entity_count
                new_count = source_store.count_for_person(person.id)
                if person.source_entity_count != new_count:
                    person.source_entity_count = new_count
                    person_updated = True

                if person_updated:
                    if not dry_run:
                        person_store.update(person)

                        # Retroactively link unlinked source entities for new emails/phones
                        new_emails = set(e.lower() for e in person.emails) - original_emails
                        new_phones = set(person.phone_numbers) - original_phones

                        for email in new_emails:
                            linked = source_store.link_unlinked_by_email(email, person.id)
                            stats['entities_retrolinked'] += linked

                        for phone in new_phones:
                            linked = source_store.link_unlinked_by_phone(phone, person.id)
                            stats['entities_retrolinked'] += linked

                    stats['persons_updated'] += 1

                if result.is_new:
                    stats['persons_created'] += 1

        except Exception as e:
            logger.error(f"Error processing contact {contact.display_name}: {e}")
            stats['errors'] += 1

    # Save person store
    if not dry_run:
        person_store.save()

    # Log summary
    logger.info(f"\n=== Apple Contacts Sync Summary ===")
    logger.info(f"Contacts read: {stats['contacts_read']}")
    logger.info(f"Source entities created: {stats['source_entities_created']}")
    logger.info(f"Source entities updated: {stats['source_entities_updated']}")
    logger.info(f"Persons linked: {stats['persons_linked']}")
    logger.info(f"Persons created: {stats['persons_created']}")
    logger.info(f"Persons updated: {stats['persons_updated']}")
    logger.info(f"Birthdays synced: {stats['birthdays_synced']}")
    logger.info(f"Entities retrolinked: {stats['entities_retrolinked']}")
    logger.info(f"Skipped: {stats['skipped']}")
    logger.info(f"Errors: {stats['errors']}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made")

    return stats


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync Apple Contacts to CRM')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    args = parser.parse_args()

    sync_apple_contacts(dry_run=not args.execute)
