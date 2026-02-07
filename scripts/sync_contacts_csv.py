#!/usr/bin/env python3
"""
Sync phone contacts from CSV export to LifeOS CRM.

Creates SourceEntity records and links them to PersonEntity via entity resolution.
Updates PersonEntity with contact data (company, position, phones).

Uses CSV export since pyobjc doesn't support Python 3.13 yet.
"""
import csv
import re
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

from api.services.source_entity import (
    get_source_entity_store,
    SourceEntity,
    LINK_STATUS_AUTO,
)
from api.services.entity_resolver import get_entity_resolver
from api.services.person_entity import get_person_entity_store

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Default CSV path
DEFAULT_CSV_PATH = Path(__file__).parent.parent / "data" / "phonecontacts20260109.csv"


def normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format."""
    if not phone:
        return ""
    # Remove all non-digit characters
    digits = re.sub(r'\D', '', phone)

    # Handle US numbers
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    elif len(digits) > 10:
        return f"+{digits}"
    return ""  # Return empty if can't normalize


def sync_contacts_csv(csv_path: str = None, dry_run: bool = True) -> dict:
    """
    Sync contacts from CSV to CRM.

    Args:
        csv_path: Path to CSV file
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
        'skipped': 0,
        'errors': 0,
    }

    if csv_path is None:
        csv_path = str(DEFAULT_CSV_PATH)

    if not Path(csv_path).exists():
        logger.error(f"CSV file not found: {csv_path}")
        stats['error'] = f"File not found: {csv_path}"
        return stats

    source_store = get_source_entity_store()
    person_store = get_person_entity_store()
    resolver = get_entity_resolver()

    logger.info(f"Reading contacts from {csv_path}...")

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)

        for row in reader:
            stats['contacts_read'] += 1

            try:
                # Get display name
                display_name = row.get('Display Name', '').strip()
                if not display_name:
                    first = row.get('First Name', '').strip()
                    last = row.get('Last Name', '').strip()
                    display_name = f"{first} {last}".strip()

                if not display_name:
                    stats['skipped'] += 1
                    continue

                # Get emails
                emails = []
                for col in ['E-mail Address', 'E-mail Address 2', 'E-mail Address 3']:
                    email = row.get(col, '').strip().lower()
                    if email and '@' in email:
                        emails.append(email)

                # Get phones
                phones = []
                for col in ['Mobile Phone', 'Home Phone', 'Business Phone']:
                    phone = normalize_phone(row.get(col, ''))
                    if phone:
                        phones.append(phone)

                # Skip if no email or phone
                if not emails and not phones:
                    stats['skipped'] += 1
                    continue

                primary_email = emails[0] if emails else None
                primary_phone = phones[0] if phones else None

                # Get company
                organization = row.get('Organization', '').strip()

                # Create unique source_id from name + email/phone
                source_id = f"csv_{display_name}_{primary_email or primary_phone}"
                source_id = re.sub(r'[^a-zA-Z0-9_@.]', '_', source_id)

                # Create/update SourceEntity
                existing_source = source_store.get_by_source('contacts', source_id)

                source_entity = SourceEntity(
                    source_type='contacts',
                    source_id=source_id,
                    observed_name=display_name,
                    observed_email=primary_email,
                    observed_phone=primary_phone,
                    metadata={
                        'first_name': row.get('First Name', ''),
                        'last_name': row.get('Last Name', ''),
                        'nickname': row.get('Nickname', ''),
                        'organization': organization,
                        'emails': emails,
                        'phones': phones,
                        'notes': row.get('Notes', '')[:500] if row.get('Notes') else '',
                    },
                    observed_at=datetime.now(timezone.utc),
                )

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

                # Resolve to PersonEntity
                result = resolver.resolve(
                    name=display_name,
                    email=primary_email,
                    phone=primary_phone,
                    create_if_missing=True,
                )

                if result and result.entity:
                    person = result.entity
                    person_updated = False

                    # Link source entity to person
                    if not existing_source or existing_source.canonical_person_id != person.id:
                        if not dry_run:
                            source_store.link_to_person(
                                source_entity.id,
                                person.id,
                                confidence=0.9,
                                status=LINK_STATUS_AUTO,
                            )
                        stats['persons_linked'] += 1

                    # Update person with contact data
                    if organization and not person.company:
                        person.company = organization
                        person_updated = True

                    # Add phones if not present
                    for phone in phones:
                        if phone and phone not in person.phone_numbers:
                            person.phone_numbers.append(phone)
                            if not person.phone_primary:
                                person.phone_primary = phone
                            person_updated = True

                    # Add emails if not present
                    for email in emails:
                        if email not in [e.lower() for e in person.emails]:
                            person.emails.append(email)
                            person_updated = True

                    # Add source
                    if 'contacts' not in person.sources:
                        person.sources.append('contacts')
                        person_updated = True

                    # Update source_entity_count
                    if not dry_run:
                        new_count = source_store.count_for_person(person.id)
                        if person.source_entity_count != new_count:
                            person.source_entity_count = new_count
                            person_updated = True

                    if person_updated:
                        if not dry_run:
                            person_store.update(person)
                        stats['persons_updated'] += 1

                    if result.is_new:
                        stats['persons_created'] += 1

            except Exception as e:
                logger.error(f"Error processing row: {e}")
                stats['errors'] += 1

    # Save person store
    if not dry_run:
        person_store.save()

    # Log summary
    logger.info(f"\n=== Contacts CSV Sync Summary ===")
    logger.info(f"Contacts read: {stats['contacts_read']}")
    logger.info(f"Source entities created: {stats['source_entities_created']}")
    logger.info(f"Source entities updated: {stats['source_entities_updated']}")
    logger.info(f"Persons linked: {stats['persons_linked']}")
    logger.info(f"Persons created: {stats['persons_created']}")
    logger.info(f"Persons updated: {stats['persons_updated']}")
    logger.info(f"Skipped: {stats['skipped']}")
    logger.info(f"Errors: {stats['errors']}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made")

    return stats


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync phone contacts CSV to CRM')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--csv', type=str, help='Path to CSV file')
    args = parser.parse_args()

    sync_contacts_csv(csv_path=args.csv, dry_run=not args.execute)
