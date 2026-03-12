#!/usr/bin/env python3
"""
Re-point stale merged person IDs in the interactions table.

Runs between entity processing (Phase 2) and relationship discovery (Phase 3)
to ensure all interactions reference canonical person IDs before relationships
are built. This eliminates the root cause of ~400+ consistency issues per night.

Usage:
    python scripts/sync_repoint_stale_ids.py --execute
    python scripts/sync_repoint_stale_ids.py --dry-run
"""
import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


def repoint_stale_interaction_ids(
    db_path: str | None = None,
    person_store=None,
    valid_person_ids: set[str] | None = None,
    dry_run: bool = True,
) -> dict:
    """Re-point interactions with stale merged person_ids to canonical IDs.

    Args:
        db_path: Path to interactions DB (default: from settings).
        person_store: PersonEntityStore (default: singleton).
        valid_person_ids: Set of valid person IDs (default: computed from store).
        dry_run: If True, report only — don't modify.

    Returns:
        Dict with stale_count and repointed count.
    """
    if db_path is None:
        from api.services.interaction_store import get_interaction_db_path
        db_path = get_interaction_db_path()

    if person_store is None:
        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()

    if valid_person_ids is None:
        all_people = person_store.get_all(include_hidden=True, include_merged=True)
        valid_person_ids = {p.id for p in all_people if not p.merged_into}

    conn = sqlite3.connect(db_path, timeout=60.0)

    # Find all distinct person_ids in interactions that are NOT in the valid set
    cursor = conn.execute("SELECT DISTINCT person_id FROM interactions")
    all_pids = {row[0] for row in cursor.fetchall()}

    stale_ids = all_pids - valid_person_ids
    repoint_map = {}
    for pid in stale_ids:
        canonical = person_store.get_canonical_id(pid)
        if canonical != pid and canonical in valid_person_ids:
            repoint_map[pid] = canonical

    stale_count = 0
    if repoint_map:
        conn.execute("CREATE TEMP TABLE repoint_map (old_id TEXT PRIMARY KEY, new_id TEXT)")
        conn.executemany(
            "INSERT INTO repoint_map (old_id, new_id) VALUES (?, ?)",
            list(repoint_map.items()),
        )
        stale_count = conn.execute(
            "SELECT COUNT(*) FROM interactions i "
            "JOIN repoint_map r ON i.person_id = r.old_id"
        ).fetchone()[0]

    repointed = 0
    if not dry_run and stale_count > 0:
        conn.execute(
            "UPDATE interactions SET person_id = ("
            "  SELECT r.new_id FROM repoint_map r WHERE r.old_id = interactions.person_id"
            ") WHERE person_id IN (SELECT old_id FROM repoint_map)"
        )
        repointed = stale_count
        conn.commit()

    conn.close()

    logger.info(
        f"Stale ID repoint: {len(repoint_map)} stale person IDs, "
        f"{stale_count} interactions affected, {repointed} repointed"
    )
    return {
        "stale_person_ids": len(repoint_map),
        "stale_count": stale_count,
        "repointed": repointed,
    }


def main():
    parser = argparse.ArgumentParser(description="Re-point stale merged person IDs in interactions")
    parser.add_argument("--execute", action="store_true", help="Apply changes")
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    args = parser.parse_args()

    dry_run = args.dry_run or not args.execute
    if not args.execute and not args.dry_run:
        print("Note: Running in dry-run mode. Use --execute to apply.")

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    result = repoint_stale_interaction_ids(dry_run=dry_run)

    print(f"\nStale person IDs found: {result['stale_person_ids']}")
    print(f"Interactions affected: {result['stale_count']}")
    print(f"Interactions repointed: {result['repointed']}")


if __name__ == "__main__":
    main()
