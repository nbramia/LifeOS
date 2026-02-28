#!/usr/bin/env python3
"""
Post-sync consistency verification (Phase 7).

Checks cross-store data consistency after all sync phases complete.
Auto-fixes small issues, flags large ones for manual review.

Checks performed:
  1. Person stats — cached counts vs computed from interactions
  2. Orphaned interactions — person_id not in PersonEntity store
  3. Stale merged IDs — interactions pointing to merged person IDs
  4. Orphaned CRM records — relationships, facts, overrides, source_entities
     with invalid person_ids

Usage:
    python scripts/sync_consistency_verify.py --dry-run          # report only
    python scripts/sync_consistency_verify.py --execute           # auto-fix below threshold
    python scripts/sync_consistency_verify.py --execute --fix-threshold 20
"""
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

AUTO_FIX_THRESHOLD = 10


def _get_valid_person_ids():
    """Load valid person IDs from the PersonEntity store."""
    from api.services.person_entity import get_person_entity_store
    store = get_person_entity_store()
    all_people = store.get_all(include_hidden=True, include_merged=True)
    return {p.id for p in all_people}, store


def _check_person_stats(dry_run: bool) -> dict:
    """Check 1: Verify cached PersonEntity counts match computed counts."""
    from api.services.person_stats import verify_person_stats
    discrepancies = verify_person_stats(fix=not dry_run)
    return {
        "count": len(discrepancies),
        "fixed": len(discrepancies) if not dry_run and discrepancies else 0,
        "details": f"{len(discrepancies)} mismatched" if discrepancies else "all consistent",
    }


def _check_orphaned_interactions(valid_ids: set, dry_run: bool, fix_threshold: int) -> dict:
    """Check 2: Find interactions whose person_id is not in PersonEntity store."""
    from api.services.interaction_store import get_interaction_db_path
    conn = sqlite3.connect(get_interaction_db_path(), timeout=60.0)

    # Create temp table for efficient lookup
    conn.execute("CREATE TEMP TABLE valid_ids (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO valid_ids (id) VALUES (?)", [(id,) for id in valid_ids])

    cursor = conn.execute("""
        SELECT COUNT(*) FROM interactions i
        WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = i.person_id)
    """)
    count = cursor.fetchone()[0]

    fixed = 0
    if not dry_run and count > 0 and count <= fix_threshold:
        conn.execute("""
            DELETE FROM interactions
            WHERE person_id NOT IN (SELECT id FROM valid_ids)
        """)
        fixed = count
        conn.commit()

    conn.close()
    return {"count": count, "fixed": fixed}


def _check_stale_merged_ids(valid_ids: set, store, dry_run: bool, fix_threshold: int) -> dict:
    """Check 3: Find interactions pointing to merged (old) person IDs that can be re-pointed."""
    from api.services.interaction_store import get_interaction_db_path
    conn = sqlite3.connect(get_interaction_db_path(), timeout=60.0)

    cursor = conn.execute("SELECT DISTINCT person_id FROM interactions")
    all_interaction_pids = {row[0] for row in cursor.fetchall()}

    stale_ids = all_interaction_pids - valid_ids
    repoint_map = {}
    for pid in stale_ids:
        canonical = store.get_canonical_id(pid)
        if canonical != pid and canonical in valid_ids:
            repoint_map[pid] = canonical

    # Count interactions that can be re-pointed
    count = 0
    if repoint_map:
        placeholders = ','.join(['?'] * len(repoint_map))
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM interactions WHERE person_id IN ({placeholders})",
            list(repoint_map.keys())
        )
        count = cursor.fetchone()[0]

    fixed = 0
    if not dry_run and count > 0 and count <= fix_threshold:
        for old_id, new_id in repoint_map.items():
            conn.execute(
                "UPDATE interactions SET person_id = ? WHERE person_id = ?",
                (new_id, old_id)
            )
        fixed = count
        conn.commit()

    conn.close()
    return {"count": count, "fixed": fixed}


def _check_orphaned_crm_records(valid_ids: set, dry_run: bool, fix_threshold: int) -> dict:
    """Check 4: Find orphaned relationships, facts, overrides, source_entities in CRM DB."""
    from config.settings import settings
    crm_path = Path(settings.chroma_path).parent / "crm.db"
    if not crm_path.exists():
        return {"count": 0, "fixed": 0, "details": "crm.db not found"}

    conn = sqlite3.connect(str(crm_path), timeout=60.0)

    # Create temp table
    conn.execute("CREATE TEMP TABLE valid_ids (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO valid_ids (id) VALUES (?)", [(id,) for id in valid_ids])

    total_count = 0
    total_fixed = 0

    # Orphaned relationships
    cursor = conn.execute("""
        SELECT COUNT(*) FROM relationships r
        WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = r.person_a_id)
           OR NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = r.person_b_id)
    """)
    rel_count = cursor.fetchone()[0]
    total_count += rel_count

    # Orphaned person_facts
    cursor = conn.execute("""
        SELECT COUNT(*) FROM person_facts f
        WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = f.person_id)
    """)
    facts_count = cursor.fetchone()[0]
    total_count += facts_count

    # Orphaned link_overrides
    try:
        cursor = conn.execute("""
            SELECT COUNT(*) FROM link_overrides o
            WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = o.preferred_person_id)
        """)
        overrides_count = cursor.fetchone()[0]
        total_count += overrides_count
    except sqlite3.OperationalError:
        overrides_count = 0  # Table may not exist

    # Source entities with invalid canonical_person_id
    cursor = conn.execute("""
        SELECT COUNT(*) FROM source_entities s
        WHERE s.canonical_person_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = s.canonical_person_id)
    """)
    se_count = cursor.fetchone()[0]
    total_count += se_count

    # Auto-fix if below threshold
    if not dry_run and total_count > 0 and total_count <= fix_threshold:
        if rel_count > 0:
            conn.execute("""
                DELETE FROM relationships WHERE id IN (
                    SELECT r.id FROM relationships r
                    WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = r.person_a_id)
                       OR NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = r.person_b_id)
                )
            """)
        if facts_count > 0:
            conn.execute("""
                DELETE FROM person_facts WHERE id IN (
                    SELECT f.id FROM person_facts f
                    WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = f.person_id)
                )
            """)
        if overrides_count > 0:
            try:
                conn.execute("""
                    DELETE FROM link_overrides WHERE id IN (
                        SELECT o.id FROM link_overrides o
                        WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = o.preferred_person_id)
                    )
                """)
            except sqlite3.OperationalError:
                pass
        if se_count > 0:
            conn.execute("""
                UPDATE source_entities
                SET canonical_person_id = NULL, link_status = 'auto'
                WHERE canonical_person_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = source_entities.canonical_person_id)
            """)
        conn.commit()
        total_fixed = total_count

    conn.close()
    details = f"relationships={rel_count}, facts={facts_count}, overrides={overrides_count}, source_entities={se_count}"
    return {"count": total_count, "fixed": total_fixed, "details": details}


def verify_consistency(dry_run: bool = True, fix_threshold: int = AUTO_FIX_THRESHOLD) -> dict:
    """
    Run all consistency checks.

    Args:
        dry_run: If True, only report issues without fixing.
        fix_threshold: Max issues per check to auto-fix. Above this, skip fixes.

    Returns:
        Dict with per-check results and totals.
    """
    valid_ids, store = _get_valid_person_ids()

    # Person stats always fixes (threshold doesn't apply — it updates cached counts, not deletes)
    person_stats = _check_person_stats(dry_run)
    orphaned_interactions = _check_orphaned_interactions(valid_ids, dry_run, fix_threshold)
    stale_merged_ids = _check_stale_merged_ids(valid_ids, store, dry_run, fix_threshold)
    orphaned_crm_records = _check_orphaned_crm_records(valid_ids, dry_run, fix_threshold)

    total_issues = (
        person_stats["count"] +
        orphaned_interactions["count"] +
        stale_merged_ids["count"] +
        orphaned_crm_records["count"]
    )
    total_fixed = (
        person_stats["fixed"] +
        orphaned_interactions["fixed"] +
        stale_merged_ids["fixed"] +
        orphaned_crm_records["fixed"]
    )

    auto_fix_skipped = any(
        check["count"] > fix_threshold and check["count"] > 0
        for check in [orphaned_interactions, stale_merged_ids, orphaned_crm_records]
    )

    return {
        "person_stats_mismatches": person_stats,
        "orphaned_interactions": orphaned_interactions,
        "stale_merged_ids": stale_merged_ids,
        "orphaned_crm_records": orphaned_crm_records,
        "total_issues": total_issues,
        "total_fixed": total_fixed,
        "auto_fix_skipped": auto_fix_skipped,
    }


def main():
    parser = argparse.ArgumentParser(description="Post-sync consistency verification")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no fixes")
    parser.add_argument("--execute", action="store_true", help="Apply auto-fixes below threshold")
    parser.add_argument("--fix-threshold", type=int, default=AUTO_FIX_THRESHOLD,
                        help=f"Max issues per check to auto-fix (default: {AUTO_FIX_THRESHOLD})")
    args = parser.parse_args()

    dry_run = args.dry_run or not args.execute
    if not args.execute and not args.dry_run:
        print("Note: Running in dry-run mode. Use --execute to apply fixes.")

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    result = verify_consistency(dry_run=dry_run, fix_threshold=args.fix_threshold)

    # Print summary for _parse_sync_output() and human readability
    print(f"\n=== Consistency Verification ===")
    print(f"Person stats mismatches: {result['person_stats_mismatches']['count']}")
    print(f"Orphaned interactions: {result['orphaned_interactions']['count']}")
    print(f"Stale merged IDs: {result['stale_merged_ids']['count']}")
    print(f"Orphaned CRM records: {result['orphaned_crm_records']['count']}")
    if result['orphaned_crm_records'].get('details'):
        print(f"  ({result['orphaned_crm_records']['details']})")
    print(f"Total issues: {result['total_issues']}")
    print(f"Total fixed: {result['total_fixed']}")
    if result['auto_fix_skipped']:
        print(f"WARNING: Some checks exceeded fix threshold ({args.fix_threshold}), manual review needed")

    if result['total_issues'] == 0:
        print("All stores consistent.")

    # Machine-readable summary for run_all_syncs.py parsing
    import json
    summary = {
        "total_issues": result["total_issues"],
        "total_fixed": result["total_fixed"],
        "auto_fix_skipped": result["auto_fix_skipped"],
    }
    print(f"CONSISTENCY_SUMMARY:{json.dumps(summary)}")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
