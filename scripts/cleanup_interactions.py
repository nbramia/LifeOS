#!/usr/bin/env python3
"""
Clean up polluted interactions from the production database.

Three passes:
  a) Delete interactions with temp-dir source_id (test artifacts)
  b) Delete vault/granola interactions where source_id file doesn't exist on disk
  c) Delete interactions whose person_id isn't in PersonEntity store

Usage:
    python scripts/cleanup_interactions.py            # dry-run (default)
    python scripts/cleanup_interactions.py --execute   # apply changes
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.interaction_store import get_interaction_db_path, InteractionStore
from api.services.person_entity import get_person_entity_store


def cleanup_interactions(dry_run: bool = True) -> dict:
    """
    Clean up polluted interactions.

    Returns:
        Stats dict with counts per pass.
    """
    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    stats = {}

    # --- Pass A: temp-dir source_ids ---
    cursor = conn.execute("""
        SELECT COUNT(*) FROM interactions
        WHERE source_id LIKE '/tmp%'
           OR source_id LIKE '/private/var/folders%'
           OR source_id LIKE '/var/folders%'
    """)
    count_a = cursor.fetchone()[0]
    print(f"[A] Temp-dir source_ids: {count_a}")
    stats['temp_dir'] = count_a

    if not dry_run and count_a > 0:
        conn.execute("""
            DELETE FROM interactions
            WHERE source_id LIKE '/tmp%'
               OR source_id LIKE '/private/var/folders%'
               OR source_id LIKE '/var/folders%'
        """)
        print(f"    Deleted: {conn.total_changes}")

    # --- Pass B: vault/granola with missing source_id files ---
    cursor = conn.execute("""
        SELECT id, source_id FROM interactions
        WHERE source_type IN ('vault', 'granola')
          AND source_id IS NOT NULL
          AND source_id != ''
    """)
    missing_files = []
    for row in cursor.fetchall():
        source_id = row['source_id']
        # source_id for vault/granola is an absolute file path
        if source_id and not Path(source_id).exists():
            missing_files.append(row['id'])

    print(f"[B] Vault/granola with missing files: {len(missing_files)}")
    stats['missing_files'] = len(missing_files)

    if not dry_run and missing_files:
        # Delete in batches
        batch_size = 500
        for i in range(0, len(missing_files), batch_size):
            batch = missing_files[i:i + batch_size]
            placeholders = ','.join(['?'] * len(batch))
            conn.execute(f"DELETE FROM interactions WHERE id IN ({placeholders})", batch)
        print(f"    Deleted: {len(missing_files)}")

    # --- Pass C: re-point merged IDs and delete truly orphaned person_ids ---
    store = get_person_entity_store()
    all_people = store.get_all(include_hidden=True, include_merged=True)
    valid_ids = {p.id for p in all_people}

    cursor = conn.execute("SELECT DISTINCT person_id FROM interactions")
    interaction_person_ids = {row['person_id'] for row in cursor.fetchall()}
    stale_ids = interaction_person_ids - valid_ids

    # Separate into resolvable (merged) vs truly orphaned
    repoint_map = {}  # old_id -> canonical_id
    orphan_ids = set()
    for pid in stale_ids:
        canonical = store.get_canonical_id(pid)
        if canonical != pid and canonical in valid_ids:
            repoint_map[pid] = canonical
        else:
            orphan_ids.add(pid)

    # Count re-pointable interactions
    count_repoint = 0
    if repoint_map:
        placeholders = ','.join(['?'] * len(repoint_map))
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM interactions WHERE person_id IN ({placeholders})",
            list(repoint_map.keys())
        )
        count_repoint = cursor.fetchone()[0]

    print(f"[C1] Merged IDs to re-point ({len(repoint_map)} people): {count_repoint} interactions")
    stats['repointed'] = count_repoint

    if not dry_run and repoint_map:
        for old_id, new_id in repoint_map.items():
            conn.execute(
                "UPDATE interactions SET person_id = ? WHERE person_id = ?",
                (new_id, old_id)
            )
        print(f"    Re-pointed: {count_repoint}")

    # Count truly orphaned interactions
    count_c = 0
    if orphan_ids:
        placeholders = ','.join(['?'] * len(orphan_ids))
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM interactions WHERE person_id IN ({placeholders})",
            list(orphan_ids)
        )
        count_c = cursor.fetchone()[0]

    print(f"[C2] Orphaned person_ids ({len(orphan_ids)} people): {count_c} interactions")
    stats['orphaned'] = count_c
    stats['orphaned_people'] = len(orphan_ids)

    if not dry_run and count_c > 0:
        placeholders = ','.join(['?'] * len(orphan_ids))
        conn.execute(
            f"DELETE FROM interactions WHERE person_id IN ({placeholders})",
            list(orphan_ids)
        )
        print(f"    Deleted: {count_c}")

    # Commit and summarize
    total = count_a + len(missing_files) + count_c + count_repoint
    stats['total'] = total

    if not dry_run:
        if total > 0:
            # Backup before committing
            int_store = InteractionStore()
            backup_path = int_store.create_backup()
            print(f"\nBackup created: {backup_path}")
        conn.commit()
        print(f"\nCleanup complete. Removed {total} interactions.")
    else:
        print(f"\nDRY RUN — {total} interactions would be removed. Use --execute to apply.")

    conn.close()
    return stats


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Clean up polluted interactions')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    args = parser.parse_args()

    cleanup_interactions(dry_run=not args.execute)
