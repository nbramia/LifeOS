#!/usr/bin/env python3
"""
Link iMessage handles to CRM person entities.

This script updates imessage.db to set person_entity_id based on matching
phone numbers in the CRM source_entities table.
"""
import sqlite3
import logging
import sys
from pathlib import Path

# Running `python scripts/foo.py` puts scripts/ on sys.path, not the project
# root, so `import api` fails without this. Mirrors the sibling sync scripts.
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

IMESSAGE_DB = Path(__file__).parent.parent / "data" / "imessage.db"
CRM_DB = Path(__file__).parent.parent / "data" / "crm.db"


def normalize_phone(phone: str) -> str:
    """Normalize phone number for matching."""
    if not phone:
        return ""
    # Remove all non-digit characters
    digits = ''.join(c for c in phone if c.isdigit())
    # Remove leading 1 for US numbers
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits


def get_phone_to_person_mapping() -> dict[str, str]:
    """Get mapping of normalized phone numbers to person IDs from CRM."""
    conn = sqlite3.connect(CRM_DB)
    cursor = conn.execute("""
        SELECT DISTINCT observed_phone, canonical_person_id
        FROM source_entities
        WHERE observed_phone IS NOT NULL
        AND observed_phone != ''
        AND canonical_person_id IS NOT NULL
    """)

    mapping = {}
    for row in cursor:
        phone = row[0]
        person_id = row[1]
        normalized = normalize_phone(phone)
        if normalized:
            mapping[normalized] = person_id

    conn.close()
    logger.info(f"Loaded {len(mapping)} phone-to-person mappings from CRM")
    return mapping


def link_imessage_entities(dry_run: bool = True) -> dict:
    """
    Link iMessage handles to CRM person entities.

    Args:
        dry_run: If True, don't actually update anything

    Returns:
        Stats dict
    """
    phone_to_person = get_phone_to_person_mapping()

    conn = sqlite3.connect(IMESSAGE_DB)

    stats = {
        'handles_checked': 0,
        'already_linked': 0,
        'newly_linked': 0,
        'no_match': 0,
        'messages_updated': 0,
    }

    # Get all unique handles without person_entity_id
    cursor = conn.execute("""
        SELECT DISTINCT handle_normalized
        FROM messages
        WHERE handle_normalized IS NOT NULL
        AND handle_normalized != ''
    """)

    handles = [row[0] for row in cursor]
    logger.info(f"Found {len(handles)} unique handles to check")

    # Check each handle
    for handle in handles:
        stats['handles_checked'] += 1
        normalized = normalize_phone(handle)

        if not normalized:
            continue

        # Skip only handles with NOTHING left to link. Testing for the
        # presence of a linked message instead (the previous behaviour) meant a
        # handle whose messages were partially linked — one linked row was
        # enough — was skipped forever, so its remaining rows could never be
        # backfilled (#497). The UPDATE below is already scoped to unlinked
        # rows, so re-visiting a partially-linked handle is safe.
        cursor = conn.execute("""
            SELECT 1 FROM messages
            WHERE handle_normalized = ?
            AND (person_entity_id IS NULL OR person_entity_id = '')
            LIMIT 1
        """, (handle,))

        if cursor.fetchone() is None:
            stats['already_linked'] += 1
            continue

        # Try to find matching person
        person_id = phone_to_person.get(normalized)
        if not person_id:
            stats['no_match'] += 1
            continue

        # Update all messages with this handle
        if not dry_run:
            cursor = conn.execute("""
                UPDATE messages
                SET person_entity_id = ?
                WHERE handle_normalized = ?
                AND (person_entity_id IS NULL OR person_entity_id = '')
            """, (person_id, handle))
            stats['messages_updated'] += cursor.rowcount
        else:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM messages
                WHERE handle_normalized = ?
                AND (person_entity_id IS NULL OR person_entity_id = '')
            """, (handle,))
            stats['messages_updated'] += cursor.fetchone()[0]

        stats['newly_linked'] += 1
        logger.debug(f"Linked {handle} -> {person_id}")

    if not dry_run:
        conn.commit()

    conn.close()

    logger.info("\n=== iMessage Entity Linking Summary ===")
    logger.info(f"Handles checked: {stats['handles_checked']}")
    logger.info(f"Already linked: {stats['already_linked']}")
    logger.info(f"Newly linked: {stats['newly_linked']}")
    logger.info(f"No match found: {stats['no_match']}")
    logger.info(f"Messages updated: {stats['messages_updated']}")

    # Canonical line consumed by run_all_syncs._parse_sync_output. "Newly
    # linked" / "Messages updated" match none of the fallback regexes, so a
    # night that linked thousands of rows still reported 0/0/0 (#497).
    from api.services.sync_health import emit_sync_stats
    emit_sync_stats({
        "processed": int(stats.get("handles_checked", 0) or 0),
        "people_updated": int(stats.get("newly_linked", 0) or 0),
        "updated": int(stats.get("messages_updated", 0) or 0),
    })

    if dry_run:
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")

    return stats


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Link iMessage handles to CRM entities')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    args = parser.parse_args()

    link_imessage_entities(dry_run=not args.execute)
