#!/usr/bin/env python3
"""
Create PersonEntity records for Apple Contacts that have iMessage evidence
but no person yet (#700).

The chicken-and-egg this closes: a contacts SourceEntity never creates a
PersonEntity on its own (correctly -- most of the address book shouldn't
become CRM people), and `link_imessage_entities.py` only links handles to
*existing* people. So a contact who only ever texts (no WhatsApp, no email
correspondence) can never get a person entity, even with a deep iMessage
history.

This script closes the gap without inventing a new creation pattern: for
every unlinked `contacts` SourceEntity, it checks whether the contact's
normalized phone or (lowercased) email appears as an iMessage handle at
least `settings.contact_person_min_messages` times in data/imessage.db. If
so, it creates a person the same way scripts/apple_data_import.py's WhatsApp
import does -- `EntityResolver.resolve(..., create_if_missing=True)` -- then
links both the contacts SourceEntity and the matching message rows to it.
Categorization (family/work rules) is deliberately left to the normal
`compute_person_category` pass that already runs on every person during the
nightly "strengths" step; this script's only job is to make sure the person
exists and is linked.

Idempotent: a contacts SourceEntity that already has a canonical_person_id
(created by this script or anything else) is never revisited.

Usage:
    # Dry run (default) - see what would be created
    python scripts/create_contact_persons.py

    # Actually apply changes
    python scripts/create_contact_persons.py --execute
"""
import sqlite3
import logging
import sys
from contextlib import closing
from pathlib import Path
from typing import Optional

# Running `python scripts/foo.py` puts scripts/ on sys.path, not the project
# root, so `import api` fails without this. Mirrors the sibling sync scripts.
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.entity_resolver import EntityResolver
from api.services.person_entity import PersonEntityStore, get_person_entity_store
from api.services.phone_utils import normalize_phone
from api.services.source_entity import (
    SourceEntityStore,
    get_source_entity_store,
    LINK_STATUS_AUTO,
)
from config.settings import settings

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

IMESSAGE_DB = Path(__file__).parent.parent / "data" / "imessage.db"

# A contacts SE without at least this many messages on a handle is treated
# as address-book noise, not evidence of a real relationship worth a person.
DEFAULT_LIMIT = 10000


def _count_messages_for_phone(conn: sqlite3.Connection, phone: str) -> int:
    """Count iMessages whose normalized handle matches an E.164 phone."""
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE handle_normalized = ?",
        (phone,),
    ).fetchone()
    return row[0] if row else 0


def _count_messages_for_email(conn: sqlite3.Connection, email: str) -> int:
    """Count iMessages whose (email) handle matches, case-insensitively.

    Email handles never get a handle_normalized (phone_utils.normalize_phone
    returns None for them), so that column being NULL is what distinguishes
    an email handle row from a phone one -- mirrors
    api.services.imessage.join_imessages_to_entities.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE handle_normalized IS NULL AND LOWER(handle) = ?",
        (email.lower(),),
    ).fetchone()
    return row[0] if row else 0


def _link_messages_to_person(
    conn: sqlite3.Connection,
    person_id: str,
    phone: Optional[str],
    email: Optional[str],
) -> int:
    """Point this contact's phone- and/or email-handle messages at the new
    person, so interactions/timeline show up immediately instead of waiting
    on the next retroactive link_imessage/link_source_entities pass.

    Only touches rows that aren't already linked, so re-running is a no-op.
    """
    updated = 0
    if phone:
        cursor = conn.execute(
            """
            UPDATE messages SET person_entity_id = ?
            WHERE handle_normalized = ?
            AND (person_entity_id IS NULL OR person_entity_id = '')
            """,
            (person_id, phone),
        )
        updated += cursor.rowcount
    if email:
        cursor = conn.execute(
            """
            UPDATE messages SET person_entity_id = ?
            WHERE handle_normalized IS NULL AND LOWER(handle) = ?
            AND (person_entity_id IS NULL OR person_entity_id = '')
            """,
            (person_id, email.lower()),
        )
        updated += cursor.rowcount
    return updated


def create_contact_persons(
    dry_run: bool = True,
    min_messages: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
    source_store: Optional[SourceEntityStore] = None,
    person_store: Optional[PersonEntityStore] = None,
    resolver: Optional[EntityResolver] = None,
    imessage_db_path: Optional[str] = None,
) -> dict:
    """
    Create PersonEntity records for contacts with iMessage interaction evidence.

    Args:
        dry_run: If True, don't actually create/link anything
        min_messages: Evidence threshold (default: settings.contact_person_min_messages)
        limit: Max unlinked contacts SourceEntities to consider in one run
        source_store: Override SourceEntityStore (for tests)
        person_store: Override PersonEntityStore (for tests)
        resolver: Override EntityResolver (for tests)
        imessage_db_path: Override path to imessage.db (for tests)

    Returns:
        Stats dict
    """
    threshold = min_messages if min_messages is not None else settings.contact_person_min_messages
    source_store = source_store or get_source_entity_store()
    person_store = person_store or get_person_entity_store()
    resolver = resolver or EntityResolver(entity_store=person_store)
    db_path = imessage_db_path or str(IMESSAGE_DB)

    stats = {
        "contacts_checked": 0,
        "no_evidence": 0,
        "persons_created": 0,
        "persons_matched_existing": 0,
        "messages_linked": 0,
        "skipped_no_name": 0,
        "errors": 0,
    }

    contacts = source_store.get_unlinked(source_type="contacts", limit=limit)
    logger.info(f"Found {len(contacts)} unlinked contacts to check for iMessage evidence")

    if not contacts:
        logger.info("No unlinked contacts to process!")
        from api.services.sync_health import emit_sync_stats
        emit_sync_stats({"processed": 0, "people_updated": 0, "updated": 0})
        return stats

    with closing(sqlite3.connect(db_path)) as conn:
        for contact in contacts:
            stats["contacts_checked"] += 1
            try:
                phone = normalize_phone(contact.observed_phone) if contact.observed_phone else None
                email = contact.observed_email.lower() if contact.observed_email else None

                phone_count = _count_messages_for_phone(conn, phone) if phone else 0
                email_count = _count_messages_for_email(conn, email) if email else 0

                if phone_count < threshold and email_count < threshold:
                    stats["no_evidence"] += 1
                    continue

                if not contact.observed_name:
                    # resolver.resolve() never creates from phone alone (#226)
                    # and this script shouldn't invent a name-less person.
                    stats["skipped_no_name"] += 1
                    continue

                if dry_run:
                    # EntityResolver.resolve(create_if_missing=True) always
                    # persists -- it has no dry-run mode of its own -- so a
                    # true dry run must never call it. Report the evidence
                    # found without creating or linking anything.
                    stats["persons_created"] += 1
                    stats["messages_linked"] += phone_count + email_count
                    continue

                result = resolver.resolve(
                    name=contact.observed_name,
                    email=contact.observed_email,
                    phone=phone,
                    create_if_missing=True,
                )
                if not result or not result.entity:
                    stats["no_evidence"] += 1
                    continue

                person = result.entity
                if result.is_new:
                    stats["persons_created"] += 1
                    logger.info(
                        f"Created person for contact evidence: {contact.observed_name} "
                        f"(phone_msgs={phone_count}, email_msgs={email_count})"
                    )
                else:
                    stats["persons_matched_existing"] += 1

                source_store.link_to_person(
                    contact.id,
                    person.id,
                    confidence=0.9,
                    status=LINK_STATUS_AUTO,
                    method="contact_imessage_evidence",
                )
                stats["messages_linked"] += _link_messages_to_person(
                    conn, person.id, phone, email
                )

            except Exception as e:
                stats["errors"] += 1
                logger.warning(f"Error processing contact {contact.id}: {e}")

        if not dry_run:
            conn.commit()

    logger.info("\n=== Contact Person Creation Summary ===")
    logger.info(f"Contacts checked:         {stats['contacts_checked']}")
    logger.info(f"No evidence (skipped):    {stats['no_evidence']}")
    logger.info(f"Persons created:          {stats['persons_created']}")
    logger.info(f"Persons matched existing: {stats['persons_matched_existing']}")
    logger.info(f"Messages linked:          {stats['messages_linked']}")
    logger.info(f"Skipped (no name):        {stats['skipped_no_name']}")
    logger.info(f"Errors:                   {stats['errors']}")

    from api.services.sync_health import emit_sync_stats
    emit_sync_stats({
        "processed": stats["contacts_checked"],
        "people_updated": stats["persons_created"] + stats["persons_matched_existing"],
        "updated": stats["messages_linked"],
        "errors": stats["errors"],
    })

    if dry_run:
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")

    return stats


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Create PersonEntity records for contacts with iMessage evidence (#700)'
    )
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument(
        '--min-messages', type=int, default=None,
        help='Evidence threshold (default: settings.contact_person_min_messages)',
    )
    args = parser.parse_args()

    create_contact_persons(dry_run=not args.execute, min_messages=args.min_messages)
