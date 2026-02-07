#!/usr/bin/env python3
"""
Link Slack users to CRM person entities by email match.

This script updates source_entities to set canonical_person_id for Slack users
by matching their email addresses to existing PersonEntities.

Note: Only uses exact email matching. Slack emails often have different formats
(e.g., firstnamelastname@company.com vs firstname@company.com) which represent
different people and should NOT be matched by name.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

CRM_DB = Path(__file__).parent.parent / "data" / "crm.db"


def get_email_to_person_mapping() -> dict[str, str]:
    """
    Get mapping of email addresses to person IDs from existing source entities.

    Uses emails from sources that are already linked (gmail, calendar, contacts, etc.)
    to build a lookup table for matching Slack users.
    """
    conn = sqlite3.connect(CRM_DB)
    cursor = conn.execute("""
        SELECT DISTINCT LOWER(observed_email), canonical_person_id
        FROM source_entities
        WHERE observed_email IS NOT NULL
        AND observed_email != ''
        AND canonical_person_id IS NOT NULL
        AND source_type != 'slack'
    """)

    mapping = {}
    for row in cursor:
        email = row[0]
        person_id = row[1]
        if email:
            mapping[email] = person_id

    conn.close()
    logger.info(f"Loaded {len(mapping)} email-to-person mappings from CRM")
    return mapping


def link_slack_entities(dry_run: bool = True) -> dict:
    """
    Link Slack source entities to PersonEntities by exact email match only.

    Args:
        dry_run: If True, don't actually update anything

    Returns:
        Stats dict
    """
    email_to_person = get_email_to_person_mapping()

    conn = sqlite3.connect(CRM_DB)

    stats = {
        'slack_entities_checked': 0,
        'already_linked': 0,
        'newly_linked': 0,
        'no_email': 0,
        'no_match': 0,
    }

    unmatched = []

    # Get all Slack source entities
    cursor = conn.execute("""
        SELECT id, source_id, observed_name, observed_email
        FROM source_entities
        WHERE source_type = 'slack'
    """)

    slack_entities = cursor.fetchall()
    logger.info(f"Found {len(slack_entities)} Slack source entities")

    updates = []

    for entity_id, source_id, name, email in slack_entities:
        stats['slack_entities_checked'] += 1

        # Check if already linked
        cursor = conn.execute("""
            SELECT canonical_person_id FROM source_entities
            WHERE id = ?
            AND canonical_person_id IS NOT NULL
        """, (entity_id,))
        existing = cursor.fetchone()

        if existing and existing[0]:
            stats['already_linked'] += 1
            continue

        # Try to match by email
        if not email:
            stats['no_email'] += 1
            continue

        email_lower = email.lower()
        person_id = email_to_person.get(email_lower)

        if not person_id:
            stats['no_match'] += 1
            unmatched.append((name, email))
            continue

        # Queue for update
        updates.append((person_id, entity_id))
        stats['newly_linked'] += 1

    # Apply updates
    if not dry_run and updates:
        cursor.executemany("""
            UPDATE source_entities
            SET canonical_person_id = ?
            WHERE id = ?
        """, updates)
        conn.commit()

    conn.close()

    logger.info(f"\n=== Slack Entity Linking Summary ===")
    logger.info(f"Slack entities checked: {stats['slack_entities_checked']}")
    logger.info(f"Already linked: {stats['already_linked']}")
    logger.info(f"Newly linked: {stats['newly_linked']}")
    logger.info(f"No email address: {stats['no_email']}")
    logger.info(f"No matching person: {stats['no_match']}")

    if unmatched and len(unmatched) <= 20:
        logger.info(f"\nUnmatched Slack users (need manual linking):")
        for name, email in unmatched:
            logger.info(f"  - {name}: {email}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")

    return stats


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Link Slack users to CRM entities')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    args = parser.parse_args()

    link_slack_entities(dry_run=not args.execute)
