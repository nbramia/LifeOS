#!/usr/bin/env python3
"""
Post-sync consistency verification (Phase 7).

Checks cross-store data consistency after all sync phases complete.
Auto-fixes small issues, flags large ones for manual review.

Checks performed:
  1. Person stats — cached counts vs computed from interactions
  2. Orphaned interactions — person_id not in PersonEntity store
  2b. Hidden-person interactions — interactions for hidden (soft-deleted) people
  3. Stale merged IDs (interactions) — interactions pointing to merged person IDs
  3b. Stale merged IDs (relationships) — relationships pointing to merged person IDs
      (always auto-fixes, not gated by threshold — re-pointing is non-destructive)
  4. Self-loop and hidden-person relationships — always cleaned up
  5. Orphaned CRM records — per-table threshold for relationships, facts,
     overrides, source_entities with invalid person_ids

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
    """Load valid person IDs and hidden IDs from the PersonEntity store."""
    from api.services.person_entity import get_person_entity_store
    store = get_person_entity_store()
    all_people = store.get_all(include_hidden=True, include_merged=True)
    valid_ids = {p.id for p in all_people}
    hidden_ids = {p.id for p in all_people if p.hidden}
    return valid_ids, hidden_ids, store


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


def _check_hidden_interactions(hidden_ids: set, valid_ids: set, dry_run: bool) -> dict:
    """Check 2b: Delete interactions for hidden (soft-deleted) people.

    These interactions drive unnecessary relationship discovery work and serve
    no purpose since the person is hidden.  Always cleaned up (no threshold).
    """
    if not hidden_ids:
        return {"count": 0, "fixed": 0}
    if len(hidden_ids) > len(valid_ids) * 0.5:
        logger.warning("hidden_ids > 50%% of valid_ids (%d/%d) — skipping hidden cleanup as safety guard",
                        len(hidden_ids), len(valid_ids))
        return {"count": 0, "fixed": 0}

    from api.services.interaction_store import get_interaction_db_path
    conn = sqlite3.connect(get_interaction_db_path(), timeout=60.0)

    conn.execute("CREATE TEMP TABLE hidden_ids (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO hidden_ids (id) VALUES (?)", [(id,) for id in hidden_ids])

    count = conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE person_id IN (SELECT id FROM hidden_ids)"
    ).fetchone()[0]

    fixed = 0
    if not dry_run and count > 0:
        conn.execute("DELETE FROM interactions WHERE person_id IN (SELECT id FROM hidden_ids)")
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

    # Count interactions that can be re-pointed (use temp table to avoid 999-var limit)
    count = 0
    if repoint_map:
        conn.execute("CREATE TEMP TABLE stale_ids (old_id TEXT PRIMARY KEY, new_id TEXT)")
        conn.executemany(
            "INSERT INTO stale_ids (old_id, new_id) VALUES (?, ?)",
            list(repoint_map.items())
        )
        cursor = conn.execute(
            "SELECT COUNT(*) FROM interactions i "
            "JOIN stale_ids s ON i.person_id = s.old_id"
        )
        count = cursor.fetchone()[0]

    fixed = 0
    if not dry_run and count > 0 and count <= fix_threshold:
        conn.execute(
            "UPDATE interactions SET person_id = ("
            "  SELECT s.new_id FROM stale_ids s WHERE s.old_id = interactions.person_id"
            ") WHERE person_id IN (SELECT old_id FROM stale_ids)"
        )
        fixed = count
        conn.commit()

    conn.close()
    return {"count": count, "fixed": fixed}


def _check_stale_merged_relationships(valid_ids: set, store, dry_run: bool) -> dict:
    """Check 3b: Find relationships pointing to merged (old) person IDs that can be re-pointed."""
    from config.settings import settings
    crm_path = Path(settings.chroma_path).parent / "crm.db"
    if not crm_path.exists():
        return {"count": 0, "fixed": 0, "repointed": 0, "deleted_dupes": 0}

    conn = sqlite3.connect(str(crm_path), timeout=60.0)

    # Collect all distinct person IDs referenced in relationships
    cursor = conn.execute(
        "SELECT DISTINCT person_a_id FROM relationships "
        "UNION SELECT DISTINCT person_b_id FROM relationships"
    )
    all_rel_pids = {row[0] for row in cursor.fetchall()}

    stale_ids = all_rel_pids - valid_ids
    repoint_map = {}
    for pid in stale_ids:
        canonical = store.get_canonical_id(pid)
        if canonical != pid and canonical in valid_ids:
            repoint_map[pid] = canonical

    if not repoint_map:
        conn.close()
        return {"count": 0, "fixed": 0, "repointed": 0, "deleted_dupes": 0}

    # Count affected relationships
    conn.execute("CREATE TEMP TABLE stale_rel_ids (old_id TEXT PRIMARY KEY, new_id TEXT)")
    conn.executemany(
        "INSERT INTO stale_rel_ids (old_id, new_id) VALUES (?, ?)",
        list(repoint_map.items())
    )
    cursor = conn.execute(
        "SELECT COUNT(*) FROM relationships r "
        "WHERE EXISTS (SELECT 1 FROM stale_rel_ids s WHERE s.old_id = r.person_a_id) "
        "   OR EXISTS (SELECT 1 FROM stale_rel_ids s WHERE s.old_id = r.person_b_id)"
    )
    count = cursor.fetchone()[0]

    repointed = 0
    deleted_dupes = 0
    # Re-pointing merged IDs is always safe (follows known merge chains),
    # so it's not gated by fix_threshold (which guards against mass deletions).
    if not dry_run and count > 0:
        # Re-point person_a_id and person_b_id.  Use OR IGNORE so rows that
        # would violate UNIQUE(person_a_id, person_b_id) are skipped.
        conn.execute(
            "UPDATE OR IGNORE relationships SET person_a_id = ("
            "  SELECT s.new_id FROM stale_rel_ids s WHERE s.old_id = relationships.person_a_id"
            ") WHERE person_a_id IN (SELECT old_id FROM stale_rel_ids)"
        )
        conn.execute(
            "UPDATE OR IGNORE relationships SET person_b_id = ("
            "  SELECT s.new_id FROM stale_rel_ids s WHERE s.old_id = relationships.person_b_id"
            ") WHERE person_b_id IN (SELECT old_id FROM stale_rel_ids)"
        )

        # Delete rows that still reference stale IDs (skipped due to UNIQUE conflicts —
        # the canonical equivalent already exists).
        conn.execute(
            "DELETE FROM relationships "
            "WHERE person_a_id IN (SELECT old_id FROM stale_rel_ids) "
            "   OR person_b_id IN (SELECT old_id FROM stale_rel_ids)"
        )
        repointed = count

        # Normalize pair order (person_a_id < person_b_id) for any swapped pairs
        conn.execute(
            "UPDATE relationships SET "
            "  person_a_id = person_b_id, person_b_id = person_a_id "
            "WHERE person_a_id > person_b_id"
        )

        # Delete self-loops (person merged with someone they had a relationship with)
        conn.execute(
            "DELETE FROM relationships WHERE person_a_id = person_b_id"
        )

        # Delete duplicate pairs (keep the one with highest total shared counts)
        cursor = conn.execute("""
            DELETE FROM relationships WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY person_a_id, person_b_id
                        ORDER BY (
                            COALESCE(shared_events_count, 0) +
                            COALESCE(shared_threads_count, 0) +
                            COALESCE(shared_messages_count, 0) +
                            COALESCE(shared_whatsapp_count, 0) +
                            COALESCE(shared_slack_count, 0) +
                            COALESCE(shared_phone_calls_count, 0) +
                            COALESCE(shared_photos_count, 0)
                        ) DESC
                    ) as rn
                    FROM relationships
                ) WHERE rn = 1
            )
        """)
        deleted_dupes = cursor.rowcount
        conn.commit()

    conn.close()
    return {"count": count, "fixed": repointed, "repointed": repointed, "deleted_dupes": deleted_dupes}


def _check_relationship_hygiene(hidden_ids: set, valid_ids: set, dry_run: bool) -> dict:
    """Check 4: Delete self-loop and hidden-person relationships.

    Self-loops are data errors. Relationships involving hidden people are
    waste — relationship discovery would just recreate them every night.
    Always cleaned up (no threshold).
    """
    from config.settings import settings
    crm_path = Path(settings.chroma_path).parent / "crm.db"
    if not crm_path.exists():
        return {"count": 0, "fixed": 0, "self_loops": 0, "hidden": 0}

    conn = sqlite3.connect(str(crm_path), timeout=60.0)

    self_loops = conn.execute(
        "SELECT COUNT(*) FROM relationships WHERE person_a_id = person_b_id"
    ).fetchone()[0]

    hidden_rels = 0
    skip_hidden = hidden_ids and len(hidden_ids) > len(valid_ids) * 0.5
    if hidden_ids and not skip_hidden:
        conn.execute("CREATE TEMP TABLE hidden_ids (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO hidden_ids (id) VALUES (?)", [(id,) for id in hidden_ids])
        hidden_rels = conn.execute(
            "SELECT COUNT(*) FROM relationships r "
            "WHERE (EXISTS (SELECT 1 FROM hidden_ids h WHERE h.id = r.person_a_id) "
            "    OR EXISTS (SELECT 1 FROM hidden_ids h WHERE h.id = r.person_b_id)) "
            "  AND r.person_a_id != r.person_b_id"
        ).fetchone()[0]

    count = self_loops + hidden_rels
    fixed = 0
    if not dry_run and count > 0:
        if self_loops > 0:
            conn.execute("DELETE FROM relationships WHERE person_a_id = person_b_id")
            fixed += self_loops
        if hidden_rels > 0:
            conn.execute(
                "DELETE FROM relationships "
                "WHERE person_a_id IN (SELECT id FROM hidden_ids) "
                "   OR person_b_id IN (SELECT id FROM hidden_ids)"
            )
            fixed += hidden_rels
        conn.commit()

    conn.close()
    return {"count": count, "fixed": fixed, "self_loops": self_loops, "hidden": hidden_rels}


def _check_orphaned_crm_records(valid_ids: set, dry_run: bool, fix_threshold: int) -> dict:
    """Check 5: Find orphaned relationships, facts, overrides, source_entities in CRM DB."""
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

    # Auto-fix per table independently (each table's count checked against threshold)
    if not dry_run:
        if 0 < rel_count <= fix_threshold:
            conn.execute("""
                DELETE FROM relationships WHERE id IN (
                    SELECT r.id FROM relationships r
                    WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = r.person_a_id)
                       OR NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = r.person_b_id)
                )
            """)
            total_fixed += rel_count
        if 0 < facts_count <= fix_threshold:
            conn.execute("""
                DELETE FROM person_facts WHERE id IN (
                    SELECT f.id FROM person_facts f
                    WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = f.person_id)
                )
            """)
            total_fixed += facts_count
        if 0 < overrides_count <= fix_threshold:
            try:
                conn.execute("""
                    DELETE FROM link_overrides WHERE id IN (
                        SELECT o.id FROM link_overrides o
                        WHERE NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = o.preferred_person_id)
                    )
                """)
                total_fixed += overrides_count
            except sqlite3.OperationalError:
                pass  # Table may not exist — don't count as fixed
        if 0 < se_count <= fix_threshold:
            conn.execute("""
                UPDATE source_entities
                SET canonical_person_id = NULL, link_status = 'auto'
                WHERE canonical_person_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM valid_ids v WHERE v.id = source_entities.canonical_person_id)
            """)
            total_fixed += se_count
        if total_fixed > 0:
            conn.commit()

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
    valid_ids, hidden_ids, store = _get_valid_person_ids()

    if not valid_ids:
        logger.error("valid_ids is empty — aborting consistency checks to prevent data loss")
        return {
            "person_stats_mismatches": {"count": 0, "fixed": 0, "details": "aborted: no valid person IDs"},
            "orphaned_interactions": {"count": 0, "fixed": 0},
            "hidden_interactions": {"count": 0, "fixed": 0},
            "stale_merged_ids": {"count": 0, "fixed": 0},
            "stale_merged_relationships": {"count": 0, "fixed": 0},
            "relationship_hygiene": {"count": 0, "fixed": 0, "self_loops": 0, "hidden": 0},
            "orphaned_crm_records": {"count": 0, "fixed": 0},
            "total_issues": 0,
            "total_fixed": 0,
            "auto_fix_skipped": False,
        }

    # Interaction-modifying checks first (deletions change counts)
    orphaned_interactions = _check_orphaned_interactions(valid_ids, dry_run, fix_threshold)
    hidden_interactions = _check_hidden_interactions(hidden_ids, valid_ids, dry_run)
    stale_merged_ids = _check_stale_merged_ids(valid_ids, store, dry_run, fix_threshold)
    # Re-point stale merged IDs in relationships BEFORE checking for orphans
    stale_merged_rels = _check_stale_merged_relationships(valid_ids, store, dry_run)
    # Clean up self-loops and hidden-person relationships BEFORE orphan check
    rel_hygiene = _check_relationship_hygiene(hidden_ids, valid_ids, dry_run)
    orphaned_crm_records = _check_orphaned_crm_records(valid_ids, dry_run, fix_threshold)
    # Person stats AFTER interaction-modifying checks so counts reflect final state
    person_stats = _check_person_stats(dry_run)

    checks = [
        person_stats, orphaned_interactions, hidden_interactions,
        stale_merged_ids, stale_merged_rels, rel_hygiene, orphaned_crm_records,
    ]
    total_issues = sum(c["count"] for c in checks)
    total_fixed = sum(c["fixed"] for c in checks)

    auto_fix_skipped = any(
        check["count"] > fix_threshold and check["count"] > 0
        for check in [orphaned_interactions, stale_merged_ids, orphaned_crm_records]
    )

    return {
        "person_stats_mismatches": person_stats,
        "orphaned_interactions": orphaned_interactions,
        "hidden_interactions": hidden_interactions,
        "stale_merged_ids": stale_merged_ids,
        "stale_merged_relationships": stale_merged_rels,
        "relationship_hygiene": rel_hygiene,
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
    print(f"Hidden-person interactions: {result['hidden_interactions']['count']}")
    print(f"Stale merged IDs (interactions): {result['stale_merged_ids']['count']}")
    print(f"Stale merged IDs (relationships): {result['stale_merged_relationships']['count']}")
    rh = result['relationship_hygiene']
    print(f"Relationship hygiene: {rh['count']} (self_loops={rh.get('self_loops', 0)}, hidden={rh.get('hidden', 0)})")
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
