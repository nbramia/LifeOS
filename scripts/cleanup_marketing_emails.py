#!/usr/bin/env python3
"""
Clean up marketing/promotional email interactions and orphaned person entities.

This script:
1. Identifies Gmail interactions linked to marketing email addresses
2. Deletes those interactions
3. Identifies person entities that now have zero interactions (orphaned)
4. Deletes those orphaned person entities

Run with --execute to actually delete. Default is dry-run.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.interaction_store import get_interaction_db_path
from api.services.person_entity import get_person_entity_store

# Import marketing filter from sync script
from scripts.sync_gmail_calendar_interactions import is_marketing_email


def main():
    parser = argparse.ArgumentParser(description='Clean up marketing email data')
    parser.add_argument('--execute', action='store_true', help='Actually delete data')
    args = parser.parse_args()

    person_store = get_person_entity_store()
    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)

    # Step 1: Find person entities with marketing emails
    marketing_person_ids = set()
    marketing_person_names = {}

    for entity in person_store.get_all(include_hidden=True):
        for email in entity.emails:
            if is_marketing_email(email):
                marketing_person_ids.add(entity.id)
                marketing_person_names[entity.id] = f"{entity.canonical_name} ({email})"
                break

    print(f"Found {len(marketing_person_ids):,} person entities with marketing emails")

    if not marketing_person_ids:
        print("No marketing entities to clean up!")
        conn.close()
        return 0

    # Show sample of marketing entities
    print("\nSample marketing entities:")
    for i, (pid, name) in enumerate(list(marketing_person_names.items())[:20]):
        print(f"  {name}")
    if len(marketing_person_ids) > 20:
        print(f"  ... and {len(marketing_person_ids) - 20} more")

    # Step 2: Count interactions per marketing person
    placeholders = ','.join('?' * len(marketing_person_ids))
    cursor = conn.execute(f"""
        SELECT person_id, COUNT(*) as cnt
        FROM interactions
        WHERE person_id IN ({placeholders})
        GROUP BY person_id
    """, list(marketing_person_ids))

    interactions_to_delete = 0
    interactions_by_person = {}
    for person_id, count in cursor:
        interactions_to_delete += count
        interactions_by_person[person_id] = count

    print(f"\nTotal marketing interactions to delete: {interactions_to_delete:,}")

    # Step 3: Find which entities would become orphaned (no remaining interactions)
    # Get all person_ids that have interactions (excluding marketing ones)
    cursor = conn.execute(f"""
        SELECT DISTINCT person_id FROM interactions
        WHERE person_id NOT IN ({placeholders})
    """, list(marketing_person_ids))
    persons_with_other_interactions = {row[0] for row in cursor}

    # Marketing persons that have no other interactions can be deleted
    orphaned_person_ids = marketing_person_ids - persons_with_other_interactions

    # But some marketing persons might have interactions from other sources
    # (calendar, vault, etc.) - we should keep those
    cursor = conn.execute(f"""
        SELECT DISTINCT person_id FROM interactions
        WHERE person_id IN ({placeholders}) AND source_type != 'gmail'
    """, list(marketing_person_ids))
    marketing_with_non_gmail = {row[0] for row in cursor}

    # Persons to delete: marketing entities with ONLY Gmail interactions (or no interactions)
    persons_to_delete = marketing_person_ids - marketing_with_non_gmail

    print(f"\nMarketing person entities to delete: {len(persons_to_delete):,}")
    print(f"Marketing entities to keep (have non-Gmail interactions): {len(marketing_with_non_gmail):,}")

    if args.execute:
        # Delete interactions first (to avoid FK issues)
        print(f"\nDeleting {interactions_to_delete:,} marketing interactions...")
        conn.execute(f"""
            DELETE FROM interactions WHERE person_id IN ({placeholders})
        """, list(marketing_person_ids))
        conn.commit()

        # Delete orphaned person entities
        print(f"Deleting {len(persons_to_delete):,} orphaned marketing person entities...")
        deleted_count = 0
        for person_id in persons_to_delete:
            if person_store.delete(person_id):
                deleted_count += 1

        # Save person store
        person_store.save()

        # Refresh stats for marketing entities we kept (those with non-Gmail interactions)
        if marketing_with_non_gmail:
            from api.services.person_stats import refresh_person_stats
            print(f"Refreshing stats for {len(marketing_with_non_gmail)} kept marketing entities...")
            refresh_person_stats(list(marketing_with_non_gmail))

        print(f"\nDone!")
        print(f"  Deleted {interactions_to_delete:,} interactions")
        print(f"  Deleted {deleted_count:,} person entities")
    else:
        print("\nDRY RUN - no changes made. Use --execute to delete.")

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
