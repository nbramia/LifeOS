#!/usr/bin/env python3
"""
Push birthdays from LifeOS PersonEntity to Apple Contacts.

This script finds people in LifeOS who have birthdays set, matches them
to Apple Contacts by email, and updates the contact's birthday if it's
not already set.

Usage:
    python scripts/push_birthdays_to_contacts.py           # Dry run
    python scripts/push_birthdays_to_contacts.py --execute # Actually update
"""
import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import get_person_entity_store

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


def get_apple_contacts_with_email():
    """Get all Apple Contacts indexed by email address."""
    try:
        import Contacts
    except ImportError:
        logger.error("pyobjc-framework-Contacts not available")
        return {}

    store = Contacts.CNContactStore.alloc().init()

    keys_to_fetch = [
        Contacts.CNContactIdentifierKey,
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactEmailAddressesKey,
        Contacts.CNContactBirthdayKey,
    ]

    request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys_to_fetch)

    contacts_by_email = {}

    def enumerate_contact(contact, stop):
        try:
            emails = contact.emailAddresses()
            if emails:
                for email_entry in emails:
                    email = email_entry.value().lower()
                    birthday = contact.birthday()
                    contacts_by_email[email] = {
                        'identifier': contact.identifier(),
                        'name': f"{contact.givenName()} {contact.familyName()}".strip(),
                        'has_birthday': birthday is not None,
                        'birthday_month': birthday.month() if birthday else None,
                        'birthday_day': birthday.day() if birthday else None,
                    }
        except Exception as e:
            pass  # Skip contacts that fail to parse

    store.enumerateContactsWithFetchRequest_error_usingBlock_(request, None, enumerate_contact)

    return contacts_by_email


def update_contact_birthday(identifier: str, month: int, day: int) -> bool:
    """Update a contact's birthday in Apple Contacts."""
    try:
        import Contacts
        from Foundation import NSDateComponents
    except ImportError:
        return False

    store = Contacts.CNContactStore.alloc().init()

    # Fetch the contact
    keys = [Contacts.CNContactBirthdayKey]
    contact, error = store.unifiedContactWithIdentifier_keysToFetch_error_(
        identifier, keys, None
    )

    if error or not contact:
        return False

    # Create mutable copy
    mutable = contact.mutableCopy()

    # Create birthday without year
    birthday = NSDateComponents.alloc().init()
    birthday.setMonth_(month)
    birthday.setDay_(day)

    mutable.setBirthday_(birthday)

    # Save
    save_request = Contacts.CNSaveRequest.alloc().init()
    save_request.updateContact_(mutable)

    success, save_error = store.executeSaveRequest_error_(save_request, None)

    return success


def main():
    parser = argparse.ArgumentParser(description="Push LifeOS birthdays to Apple Contacts")
    parser.add_argument('--execute', action='store_true', help='Actually update contacts (default is dry run)')
    args = parser.parse_args()

    dry_run = not args.execute

    if dry_run:
        logger.info("DRY RUN - no changes will be made")

    # Get all people with birthdays from LifeOS
    person_store = get_person_entity_store()
    all_people = person_store.get_all()

    people_with_birthdays = [p for p in all_people if p.birthday]
    logger.info(f"Found {len(people_with_birthdays)} people with birthdays in LifeOS")

    # Get Apple Contacts indexed by email
    logger.info("Loading Apple Contacts...")
    contacts_by_email = get_apple_contacts_with_email()
    logger.info(f"Found {len(contacts_by_email)} Apple Contacts with email addresses")

    # Match and update
    stats = {
        'matched': 0,
        'already_has_birthday': 0,
        'updated': 0,
        'failed': 0,
        'no_match': 0,
    }

    for person in people_with_birthdays:
        # Parse birthday (MM-DD format)
        try:
            month, day = map(int, person.birthday.split('-'))
        except (ValueError, AttributeError):
            continue

        # Find matching contact by email
        matched_contact = None
        for email in person.emails:
            email_lower = email.lower()
            if email_lower in contacts_by_email:
                matched_contact = contacts_by_email[email_lower]
                break

        if not matched_contact:
            stats['no_match'] += 1
            continue

        stats['matched'] += 1

        # Check if contact already has this birthday
        if matched_contact['has_birthday']:
            if matched_contact['birthday_month'] == month and matched_contact['birthday_day'] == day:
                stats['already_has_birthday'] += 1
                continue
            else:
                # Different birthday - skip to avoid overwriting
                logger.warning(
                    f"  {person.canonical_name}: Contact has different birthday "
                    f"({matched_contact['birthday_month']}/{matched_contact['birthday_day']} vs {month}/{day}) - skipping"
                )
                stats['already_has_birthday'] += 1
                continue

        # Update contact
        if dry_run:
            logger.info(f"  Would update: {person.canonical_name} -> {month:02d}/{day:02d}")
            stats['updated'] += 1
        else:
            success = update_contact_birthday(matched_contact['identifier'], month, day)
            if success:
                logger.info(f"  Updated: {person.canonical_name} ({matched_contact['name']}) -> {month:02d}/{day:02d}")
                stats['updated'] += 1
            else:
                logger.error(f"  Failed to update: {person.canonical_name}")
                stats['failed'] += 1

    # Summary
    logger.info("")
    logger.info("=== Push Birthdays Summary ===")
    logger.info(f"People with birthdays in LifeOS: {len(people_with_birthdays)}")
    logger.info(f"Matched to Apple Contacts: {stats['matched']}")
    logger.info(f"Already had birthday: {stats['already_has_birthday']}")
    logger.info(f"Updated: {stats['updated']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"No matching contact: {stats['no_match']}")

    if dry_run:
        logger.info("")
        logger.info("DRY RUN - no changes made. Use --execute to update contacts.")

    return 0 if stats['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
