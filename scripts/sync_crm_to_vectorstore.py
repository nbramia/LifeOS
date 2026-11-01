#!/usr/bin/env python3
"""
Sync CRM people data to vector store for semantic search.

This script makes the people index in ChromaDB hold exactly the significant
contacts, enabling natural language searches like "my important contacts" or
"people I work with". Anyone else already in the index is removed, unless that
would remove more than half the index or no one is eligible; the removal is then
skipped and reported as an error.

Usage:
    # Dry run (show what would be indexed)
    uv run python scripts/sync_crm_to_vectorstore.py

    # Execute indexing
    uv run python scripts/sync_crm_to_vectorstore.py --execute

    # With custom thresholds
    uv run python scripts/sync_crm_to_vectorstore.py --execute --min-strength 20 --min-interactions 5

    # Accept a removal over the limit after checking the drop is expected
    uv run python scripts/sync_crm_to_vectorstore.py --execute --allow-large-removal
"""
import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import get_person_entity_store
from api.services.relationship_summary import get_relationship_summary
from api.services.person_indexer import (
    is_index_eligible,
    plan_index_removal,
    sync_people_to_vectorstore,
    MIN_STRENGTH_FOR_INDEX,
    MIN_INTERACTIONS_FOR_INDEX,
    PERSON_COLLECTION,
)
from api.services.vectorstore import VectorStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def dry_run(min_strength: float, min_interactions: int, allow_large_removal: bool = False) -> dict:
    """
    Show what would be indexed and removed, changing nothing.

    Returns statistics about eligible people and stale index entries.
    """
    person_store = get_person_entity_store()
    people = person_store.get_all()

    eligible = []
    eligible_ids = set()
    skipped_hidden = 0
    skipped_low_activity = 0

    for person in people:
        if person.hidden:
            skipped_hidden += 1
            continue

        summary = get_relationship_summary(person.id)
        if not summary:
            skipped_low_activity += 1
            continue

        if not is_index_eligible(person, summary, min_strength, min_interactions):
            skipped_low_activity += 1
            continue

        eligible_ids.add(person.id)
        eligible.append({
            "name": person.display_name or person.canonical_name,
            "strength": summary.relationship_strength,
            "interactions_90d": summary.total_interactions_90d,
            "category": person.category,
            "active_channels": summary.active_channels,
        })

    # Sort by strength descending
    eligible.sort(key=lambda x: x["strength"], reverse=True)

    stale, skip_reason = set(), None
    try:
        indexed_paths = VectorStore(collection_name=PERSON_COLLECTION).get_all_file_paths()
        stale, skip_reason = plan_index_removal(indexed_paths, eligible_ids, allow_large_removal)
    except Exception as e:
        skip_reason = f"the index could not be listed ({type(e).__name__})"

    return {
        "stale": len(stale),
        "removal_skipped_reason": skip_reason,
        "eligible": eligible,
        "skipped_hidden": skipped_hidden,
        "skipped_low_activity": skipped_low_activity,
        "total": len(people),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Sync CRM people to vector store for semantic search"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute indexing (default is dry run)"
    )
    parser.add_argument(
        "--min-strength",
        type=float,
        default=MIN_STRENGTH_FOR_INDEX,
        help=f"Minimum relationship strength to index (default: {MIN_STRENGTH_FOR_INDEX})"
    )
    parser.add_argument(
        "--min-interactions",
        type=int,
        default=MIN_INTERACTIONS_FOR_INDEX,
        help=f"Minimum 90-day interactions to index (default: {MIN_INTERACTIONS_FOR_INDEX})"
    )
    parser.add_argument(
        "--allow-large-removal",
        action="store_true",
        help="Accept removing more than half the people index. An empty eligible set is refused regardless."
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("CRM to Vector Store Sync")
    print(f"{'='*60}")
    print(f"Thresholds: strength >= {args.min_strength} OR interactions >= {args.min_interactions}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"{'='*60}\n")

    if args.execute:
        # Actually index
        result = sync_people_to_vectorstore(
            min_strength=args.min_strength,
            min_interactions=args.min_interactions,
            allow_large_removal=args.allow_large_removal,
        )
        skip_reason = result["removal_skipped_reason"]

        print("\nResults:")
        print(f"  Indexed: {result['indexed']}")
        print(f"  Skipped: {result['skipped']}")
        print(f"  Errors:  {result['errors']}")
        print(f"  Total checked: {result['total_checked']}")
        print(f"  Stale in index: {result['stale']}")
        print(f"  Removed: {result['removed']}")
        print(f"  Removal errors: {result['removal_errors']}")
        if skip_reason:
            print(f"\nREMOVAL SKIPPED, {result['stale']} stale entries kept: {skip_reason}")

        # Canonical line consumed by run_all_syncs._parse_sync_output.
        from api.services.sync_health import emit_sync_stats, record_sync_error
        emit_sync_stats({
            "processed": int(result.get("total_checked", 0) or 0),
            "people_updated": int(result.get("indexed", 0) or 0),
            "people_removed": int(result.get("removed", 0) or 0),
            "people_stale": int(result.get("stale", 0) or 0),
            "removal_skipped": 1 if skip_reason else 0,
            "errors": (
                int(result.get("errors", 0) or 0)
                + int(result.get("removal_errors", 0) or 0)
                + (1 if skip_reason else 0)
            ),
        })
        if skip_reason:
            record_sync_error(
                "crm_vectorstore",
                f"People index removal skipped: {skip_reason}",
                error_type="removal_skipped",
            )

    else:
        # Dry run
        result = dry_run(args.min_strength, args.min_interactions, args.allow_large_removal)

        print(f"Would index {len(result['eligible'])} people:\n")

        # Show top 20
        for i, p in enumerate(result["eligible"][:20]):
            channels = ", ".join(p["active_channels"]) if p["active_channels"] else "none"
            print(f"  {i+1}. {p['name']}")
            print(f"     Strength: {p['strength']:.1f} | Interactions (90d): {p['interactions_90d']} | Category: {p['category']}")
            print(f"     Active channels: {channels}")

        if len(result["eligible"]) > 20:
            print(f"\n  ... and {len(result['eligible']) - 20} more")

        print("\nSummary:")
        print(f"  Eligible for indexing: {len(result['eligible'])}")
        print(f"  Skipped (hidden): {result['skipped_hidden']}")
        print(f"  Skipped (low activity): {result['skipped_low_activity']}")
        print(f"  Total people: {result['total']}")
        print(f"  Would remove from the index: {0 if result['removal_skipped_reason'] else result['stale']}")
        if result["removal_skipped_reason"]:
            print(f"  Removal would be skipped, {result['stale']} stale entries kept: {result['removal_skipped_reason']}")

        print("\nTo execute, run with --execute flag")


if __name__ == "__main__":
    main()
