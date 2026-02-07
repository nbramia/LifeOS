#!/usr/bin/env python3
"""
Fix orphaned Gmail interactions by deleting those that reference non-existent person_ids.

This script:
1. Loads all valid person IDs from people_entities.json
2. Finds Gmail interactions referencing invalid person_ids
3. Deletes the orphaned interactions (they'll be recreated on next sync)

Run with --execute to actually delete. Default is dry-run.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.interaction_store import get_interaction_db_path


def main():
    parser = argparse.ArgumentParser(description='Fix orphaned Gmail interactions')
    parser.add_argument('--execute', action='store_true', help='Actually delete orphaned interactions')
    args = parser.parse_args()

    # Load valid person IDs
    entities_path = Path(__file__).parent.parent / 'data' / 'people_entities.json'
    with open(entities_path) as f:
        people = json.load(f)
    valid_ids = {p['id'] for p in people}
    print(f"Loaded {len(valid_ids):,} valid person IDs")

    # Find orphaned Gmail interactions
    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)

    # Get all unique person_ids from Gmail interactions
    cursor = conn.execute("""
        SELECT DISTINCT person_id FROM interactions WHERE source_type = 'gmail'
    """)
    gmail_person_ids = {row[0] for row in cursor}

    orphaned_ids = gmail_person_ids - valid_ids
    print(f"Found {len(orphaned_ids):,} orphaned person_ids in Gmail interactions")

    if not orphaned_ids:
        print("No orphaned interactions to fix!")
        conn.close()
        return 0

    # Count interactions per orphaned ID
    placeholders = ','.join('?' * len(orphaned_ids))
    cursor = conn.execute(f"""
        SELECT person_id, COUNT(*) as cnt
        FROM interactions
        WHERE source_type = 'gmail' AND person_id IN ({placeholders})
        GROUP BY person_id
        ORDER BY cnt DESC
    """, list(orphaned_ids))

    total_orphaned = 0
    print("\nTop 20 orphaned person_ids by interaction count:")
    for i, (person_id, count) in enumerate(cursor):
        total_orphaned += count
        if i < 20:
            print(f"  {person_id[:8]}...: {count:,} interactions")

    print(f"\nTotal orphaned Gmail interactions: {total_orphaned:,}")

    if args.execute:
        # Delete orphaned interactions
        print(f"\nDeleting {total_orphaned:,} orphaned Gmail interactions...")
        conn.execute(f"""
            DELETE FROM interactions
            WHERE source_type = 'gmail' AND person_id IN ({placeholders})
        """, list(orphaned_ids))
        conn.commit()

        # Note: orphaned_ids reference non-existent people, so no stats refresh needed
        # (they don't exist in PersonEntity). Stats will be correct when interactions
        # are recreated with valid person_ids on next sync.

        print("Done! Run sync_gmail_calendar_interactions.py --execute to recreate them.")
    else:
        print("\nDRY RUN - no changes made. Use --execute to delete orphaned interactions.")

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
