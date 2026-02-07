#!/usr/bin/env python3
"""
Sync CRM people data to vector store for semantic search.

This script indexes significant contacts to ChromaDB, enabling natural
language searches like "my important contacts" or "people I work with".

Usage:
    # Dry run (show what would be indexed)
    uv run python scripts/sync_crm_to_vectorstore.py

    # Execute indexing
    uv run python scripts/sync_crm_to_vectorstore.py --execute

    # With custom thresholds
    uv run python scripts/sync_crm_to_vectorstore.py --execute --min-strength 20 --min-interactions 5
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
    sync_people_to_vectorstore,
    MIN_STRENGTH_FOR_INDEX,
    MIN_INTERACTIONS_FOR_INDEX,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def dry_run(min_strength: float, min_interactions: int) -> dict:
    """
    Show what would be indexed without actually indexing.

    Returns statistics about eligible people.
    """
    person_store = get_person_entity_store()
    people = person_store.get_all()

    eligible = []
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

        if summary.relationship_strength < min_strength and summary.total_interactions_90d < min_interactions:
            skipped_low_activity += 1
            continue

        eligible.append({
            "name": person.display_name or person.canonical_name,
            "strength": summary.relationship_strength,
            "interactions_90d": summary.total_interactions_90d,
            "category": person.category,
            "active_channels": summary.active_channels,
        })

    # Sort by strength descending
    eligible.sort(key=lambda x: x["strength"], reverse=True)

    return {
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
        )

        print(f"\nResults:")
        print(f"  Indexed: {result['indexed']}")
        print(f"  Skipped: {result['skipped']}")
        print(f"  Errors:  {result['errors']}")
        print(f"  Total checked: {result['total_checked']}")

    else:
        # Dry run
        result = dry_run(args.min_strength, args.min_interactions)

        print(f"Would index {len(result['eligible'])} people:\n")

        # Show top 20
        for i, p in enumerate(result["eligible"][:20]):
            channels = ", ".join(p["active_channels"]) if p["active_channels"] else "none"
            print(f"  {i+1}. {p['name']}")
            print(f"     Strength: {p['strength']:.1f} | Interactions (90d): {p['interactions_90d']} | Category: {p['category']}")
            print(f"     Active channels: {channels}")

        if len(result["eligible"]) > 20:
            print(f"\n  ... and {len(result['eligible']) - 20} more")

        print(f"\nSummary:")
        print(f"  Eligible for indexing: {len(result['eligible'])}")
        print(f"  Skipped (hidden): {result['skipped_hidden']}")
        print(f"  Skipped (low activity): {result['skipped_low_activity']}")
        print(f"  Total people: {result['total']}")

        print(f"\nTo execute, run with --execute flag")


if __name__ == "__main__":
    main()
