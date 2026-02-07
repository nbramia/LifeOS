#!/usr/bin/env python3
"""
Clean up orphaned records after people_entities.json recovery.

This script removes or nullifies records that reference person IDs
that no longer exist in people_entities.json.

Run this after vault_reindex completes to clean up orphaned data.

Usage:
    python scripts/cleanup_orphaned_records.py [--dry-run]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def cleanup_orphaned_records(dry_run: bool = True) -> dict:
    """
    Clean up orphaned records in CRM database.

    Returns:
        Stats dict with counts of cleaned records
    """
    # Load valid person IDs
    with open('data/people_entities.json') as f:
        entities = json.load(f)
    valid_ids = {e['id'] for e in entities}
    print(f"Valid person IDs: {len(valid_ids)}")

    # Connect to CRM database with timeout
    conn = sqlite3.connect('data/crm.db', timeout=60.0)
    cursor = conn.cursor()

    stats = {}

    # Create temp table with valid IDs for efficient joins
    cursor.execute("CREATE TEMP TABLE valid_person_ids (id TEXT PRIMARY KEY)")
    cursor.executemany("INSERT INTO valid_person_ids (id) VALUES (?)", [(id,) for id in valid_ids])
    print(f"Created temp table with {len(valid_ids)} valid IDs")

    # 1. Count orphaned relationships
    cursor.execute("""
        SELECT COUNT(*) FROM relationships r
        WHERE NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = r.person_a_id)
           OR NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = r.person_b_id)
    """)
    orphan_rels = cursor.fetchone()[0]
    print(f"Orphaned relationships: {orphan_rels}")
    stats['orphan_relationships'] = orphan_rels

    if not dry_run and orphan_rels > 0:
        cursor.execute("""
            DELETE FROM relationships WHERE id IN (
                SELECT r.id FROM relationships r
                WHERE NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = r.person_a_id)
                   OR NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = r.person_b_id)
            )
        """)
        print(f"  Deleted: {cursor.rowcount}")
        stats['deleted_relationships'] = cursor.rowcount

    # 2. Count orphaned person_facts
    cursor.execute("""
        SELECT COUNT(*) FROM person_facts f
        WHERE NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = f.person_id)
    """)
    orphan_facts = cursor.fetchone()[0]
    print(f"Orphaned person_facts: {orphan_facts}")
    stats['orphan_facts'] = orphan_facts

    if not dry_run and orphan_facts > 0:
        cursor.execute("""
            DELETE FROM person_facts WHERE id IN (
                SELECT f.id FROM person_facts f
                WHERE NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = f.person_id)
            )
        """)
        print(f"  Deleted: {cursor.rowcount}")
        stats['deleted_facts'] = cursor.rowcount

    # 3. Count orphaned link_overrides
    cursor.execute("""
        SELECT COUNT(*) FROM link_overrides o
        WHERE NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = o.preferred_person_id)
    """)
    orphan_overrides = cursor.fetchone()[0]
    print(f"Orphaned link_overrides: {orphan_overrides}")
    stats['orphan_overrides'] = orphan_overrides

    if not dry_run and orphan_overrides > 0:
        cursor.execute("""
            DELETE FROM link_overrides WHERE id IN (
                SELECT o.id FROM link_overrides o
                WHERE NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = o.preferred_person_id)
            )
        """)
        print(f"  Deleted: {cursor.rowcount}")
        stats['deleted_overrides'] = cursor.rowcount

    # 4. Count source_entities with invalid canonical_person_id
    cursor.execute("""
        SELECT COUNT(*) FROM source_entities s
        WHERE s.canonical_person_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = s.canonical_person_id)
    """)
    orphan_sources = cursor.fetchone()[0]
    print(f"Source entities with invalid canonical_person_id: {orphan_sources}")
    stats['orphan_sources'] = orphan_sources

    if not dry_run and orphan_sources > 0:
        cursor.execute("""
            UPDATE source_entities
            SET canonical_person_id = NULL, link_status = 'auto'
            WHERE canonical_person_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = source_entities.canonical_person_id)
        """)
        print(f"  Nullified: {cursor.rowcount}")
        stats['nullified_sources'] = cursor.rowcount

    if not dry_run:
        conn.commit()
    conn.close()

    # 5. Count and clean orphaned interactions
    interactions_conn = sqlite3.connect('data/interactions.db', timeout=60.0)
    interactions_cursor = interactions_conn.cursor()

    # Recreate temp table in this connection
    interactions_cursor.execute("CREATE TEMP TABLE valid_person_ids (id TEXT PRIMARY KEY)")
    interactions_cursor.executemany(
        "INSERT INTO valid_person_ids (id) VALUES (?)",
        [(id,) for id in valid_ids]
    )

    interactions_cursor.execute("""
        SELECT COUNT(*) FROM interactions i
        WHERE NOT EXISTS (SELECT 1 FROM valid_person_ids v WHERE v.id = i.person_id)
    """)
    orphan_interactions = interactions_cursor.fetchone()[0]
    print(f"Orphaned interactions: {orphan_interactions}")
    stats['orphan_interactions'] = orphan_interactions

    if not dry_run and orphan_interactions > 0:
        # Create backup first
        from api.services.interaction_store import InteractionStore
        store = InteractionStore()
        backup_path = store.create_backup()
        print(f"  Backup created: {backup_path}")

        interactions_cursor.execute("""
            DELETE FROM interactions
            WHERE person_id NOT IN (SELECT id FROM valid_person_ids)
        """)
        print(f"  Deleted: {interactions_cursor.rowcount}")
        stats['deleted_interactions'] = interactions_cursor.rowcount
        interactions_conn.commit()

    interactions_conn.close()

    if not dry_run:
        print("\nCleanup complete!")
    else:
        print("\nDRY RUN - no changes made. Use --execute to apply.")

    return stats


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Clean up orphaned records in CRM database')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    args = parser.parse_args()

    cleanup_orphaned_records(dry_run=not args.execute)
