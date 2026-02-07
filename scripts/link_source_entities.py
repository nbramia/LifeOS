#!/usr/bin/env python3
"""
Link unlinked source entities to existing person entities.

This is a Phase 2 sync script that runs as part of the nightly sync pipeline.
It processes unlinked source entities and attempts to resolve them to existing
PersonEntity records using the EntityResolver.

Key behaviors:
- Does NOT create new people (create_if_missing=False)
- Skips entities from blocklisted domains (marketing, newsletters, etc.)
- Skips entities that were recently attempted (within 30 days)
- Skips entities with 3+ failed attempts (considered unmatchable)
- Records match attempts to avoid re-processing

This script is the "safety net" that catches source entities that:
1. Were created before the person had a matching email/phone
2. Were missed by the immediate retroactive linking in sync scripts
3. Need periodic re-attempts as new people are added to the CRM

Usage:
    # Dry run (default) - see what would be linked
    python scripts/link_source_entities.py

    # Actually apply changes
    python scripts/link_source_entities.py --execute

    # Only process gmail entities
    python scripts/link_source_entities.py --source-type gmail --execute
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from datetime import datetime, timezone
from typing import Optional

from api.services.source_entity import (
    SourceEntityStore,
    SourceEntity,
    get_source_entity_store,
    LINK_STATUS_AUTO,
)
from api.services.entity_resolver import get_entity_resolver
from config.marketing_patterns import is_blocklisted_domain

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def link_source_entities(
    source_type: Optional[str] = None,
    dry_run: bool = True,
    batch_size: int = 1000,
    min_days_since_attempt: int = 30,
    max_attempts: int = 3,
) -> dict:
    """
    Link unlinked source entities to existing person entities.

    This is the Phase 2 "safety net" script that catches entities that weren't
    linked during their original sync.

    Args:
        source_type: Filter to specific source type (e.g., 'gmail')
        dry_run: If True, don't actually update anything
        batch_size: Number of entities to process per batch
        min_days_since_attempt: Skip if attempted within this many days
        max_attempts: Skip if attempted this many times or more

    Returns:
        Statistics dict with counts
    """
    source_store = get_source_entity_store()
    resolver = get_entity_resolver()

    stats = {
        'total_unlinked': 0,
        'eligible_for_matching': 0,
        'entities_processed': 0,
        'newly_linked': 0,
        'blocklist_skipped': 0,
        'no_match_found': 0,
        'already_attempted': 0,
        'errors': 0,
        'by_source': {},
        'by_match_type': {},
    }

    # Get counts
    stats['total_unlinked'] = source_store.count() - sum(
        1 for _ in range(1)  # placeholder - we'll get actual count
    )

    # Get all eligible entities upfront to avoid re-fetching in dry run mode
    # We fetch in large batches to build the complete list
    logger.info("Fetching all eligible entities...")
    all_entities = []
    offset = 0
    fetch_limit = 10000  # Fetch in large chunks

    while True:
        batch = source_store.get_unlinked_for_rematching(
            source_type=source_type,
            min_days_since_attempt=min_days_since_attempt,
            max_attempts=max_attempts,
            limit=fetch_limit,
        )
        if not batch:
            break
        all_entities.extend(batch)
        if len(batch) < fetch_limit:
            break
        # In execute mode, entities get updated so we'll get different ones
        # In dry run, we'd get the same ones - so limit to one fetch
        if dry_run:
            break

    stats['eligible_for_matching'] = len(all_entities)
    logger.info(f"Found {stats['eligible_for_matching']:,} unlinked entities eligible for matching")
    if source_type:
        logger.info(f"Filtering to source type: {source_type}")

    if stats['eligible_for_matching'] == 0:
        logger.info("No unlinked entities to process!")
        return stats

    # Process in batches
    batch_num = 0
    total_processed = 0

    for i in range(0, len(all_entities), batch_size):
        batch_num += 1
        entities = all_entities[i:i + batch_size]

        logger.info(f"Processing batch {batch_num}: {len(entities)} entities "
                   f"({i}/{len(all_entities)} done)")

        for entity in entities:
            stats['entities_processed'] += 1

            # Track by source type
            st = entity.source_type
            stats['by_source'][st] = stats['by_source'].get(st, 0) + 1

            # Check if email domain is blocklisted
            if entity.observed_email and is_blocklisted_domain(entity.observed_email):
                stats['blocklist_skipped'] += 1
                # Record attempt so we don't keep trying
                if not dry_run:
                    source_store.record_match_attempt(entity.id)
                continue

            try:
                # Try to resolve using the entity's observed data
                # create_if_missing=False - we only link to existing people
                result = resolver.resolve(
                    name=entity.observed_name,
                    email=entity.observed_email,
                    phone=entity.observed_phone,
                    create_if_missing=False,
                )

                if result and result.entity:
                    # Found a match!
                    stats['newly_linked'] += 1
                    stats['by_match_type'][result.match_type] = \
                        stats['by_match_type'].get(result.match_type, 0) + 1

                    if not dry_run:
                        source_store.link_to_person(
                            entity_id=entity.id,
                            canonical_person_id=result.entity.id,
                            confidence=result.confidence,
                            status=LINK_STATUS_AUTO,
                        )

                    # Log some examples
                    if stats['newly_linked'] <= 10:
                        logger.info(
                            f"  LINKED: {entity.observed_name or entity.observed_email} "
                            f"-> {result.entity.canonical_name} "
                            f"(type={result.match_type}, conf={result.confidence:.2f})"
                        )
                else:
                    # No match found - record the attempt
                    stats['no_match_found'] += 1
                    if not dry_run:
                        source_store.record_match_attempt(entity.id)

            except Exception as e:
                stats['errors'] += 1
                if stats['errors'] <= 5:
                    logger.warning(f"Error processing entity {entity.id}: {e}")

            total_processed += 1
            if total_processed % 1000 == 0:
                logger.info(f"  Progress: {total_processed:,} processed, "
                           f"{stats['newly_linked']:,} linked, "
                           f"{stats['no_match_found']:,} no match")

    # Print summary
    logger.info(f"\n{'='*50}")
    logger.info(f"Source Entity Linking Summary")
    logger.info(f"{'='*50}")
    logger.info(f"Entities processed:    {stats['entities_processed']:,}")
    logger.info(f"Newly linked:          {stats['newly_linked']:,}")
    logger.info(f"Blocklist skipped:     {stats['blocklist_skipped']:,}")
    logger.info(f"No match found:        {stats['no_match_found']:,}")
    logger.info(f"Errors:                {stats['errors']:,}")

    if stats['by_source']:
        logger.info(f"\nProcessed by source:")
        for source, count in sorted(stats['by_source'].items(), key=lambda x: -x[1]):
            logger.info(f"  {source}: {count:,}")

    if stats['by_match_type']:
        logger.info(f"\nLinked by match type:")
        for match_type, count in sorted(stats['by_match_type'].items(), key=lambda x: -x[1]):
            logger.info(f"  {match_type}: {count:,}")

    if dry_run:
        logger.info(f"\nDRY RUN - no changes made. Use --execute to apply.")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Link unlinked source entities to existing person entities',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # See what would be linked (dry run)
    python scripts/link_source_entities.py

    # Only process gmail entities
    python scripts/link_source_entities.py --source-type gmail

    # Actually apply changes
    python scripts/link_source_entities.py --execute

    # Process entities that haven't been attempted in 14 days
    python scripts/link_source_entities.py --execute --min-days 14
        """
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually apply changes (default is dry run)'
    )
    parser.add_argument(
        '--source-type',
        type=str,
        choices=['gmail', 'calendar', 'slack', 'imessage', 'whatsapp', 'signal',
                 'contacts', 'phone_contacts', 'linkedin', 'vault', 'granola',
                 'phone_call', 'phone', 'photos'],
        help='Only process entities from this source type'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=1000,
        help='Number of entities to process per batch (default: 1000)'
    )
    parser.add_argument(
        '--min-days',
        type=int,
        default=30,
        help='Skip entities attempted within this many days (default: 30)'
    )
    parser.add_argument(
        '--max-attempts',
        type=int,
        default=3,
        help='Skip entities with this many or more attempts (default: 3)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    link_source_entities(
        source_type=args.source_type,
        dry_run=not args.execute,
        batch_size=args.batch_size,
        min_days_since_attempt=args.min_days,
        max_attempts=args.max_attempts,
    )


if __name__ == '__main__':
    main()
