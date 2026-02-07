#!/usr/bin/env python3
"""
Re-link unlinked source entities to existing person entities.

This script processes source entities where canonical_person_id IS NULL and
attempts to link them to existing PersonEntities using the EntityResolver.

The problem it solves:
- When a person gains a new email, existing source entities with that email
  aren't retroactively linked.
- 91,000+ gmail source entities remain unlinked because they were created
  before the person had that email address in the system.

Example:
- Alex Johnson has emails: alex@company.com, alex.j@gmail.com
- 29 gmail source entities with alex@oldcompany.com are UNLINKED
- This script will find and link those entities to Alex's person record.

Usage:
    # Dry run (default) - see what would be linked
    python scripts/relink_source_entities.py

    # Dry run for specific source type
    python scripts/relink_source_entities.py --source-type gmail

    # Actually apply changes
    python scripts/relink_source_entities.py --execute

    # Create new people for unmatched entities (use with caution)
    python scripts/relink_source_entities.py --execute --create-if-missing
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

from api.services.source_entity import (
    SourceEntityStore,
    SourceEntity,
    get_source_entity_store,
    LINK_STATUS_AUTO,
)
from api.services.entity_resolver import EntityResolver, get_entity_resolver
from api.utils.db_paths import get_crm_db_path

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def get_all_unlinked_entity_ids(
    source_type: Optional[str] = None,
    db_path: str = None,
) -> list[str]:
    """
    Get ALL unlinked entity IDs upfront.

    This avoids re-processing issues when entities get linked during execution.
    """
    db_path = db_path or get_crm_db_path()
    conn = sqlite3.connect(db_path)
    try:
        if source_type:
            cursor = conn.execute("""
                SELECT id FROM source_entities
                WHERE canonical_person_id IS NULL AND source_type = ?
                ORDER BY observed_at DESC
            """, (source_type,))
        else:
            cursor = conn.execute("""
                SELECT id FROM source_entities
                WHERE canonical_person_id IS NULL
                ORDER BY observed_at DESC
            """)
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_entities_by_ids(entity_ids: list[str], db_path: str = None) -> list[SourceEntity]:
    """
    Get source entities by their IDs.
    """
    if not entity_ids:
        return []

    db_path = db_path or get_crm_db_path()
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ','.join('?' * len(entity_ids))
        cursor = conn.execute(f"""
            SELECT id, source_type, source_id, observed_name, observed_email,
                   observed_phone, metadata, canonical_person_id, link_confidence,
                   link_status, linked_at, observed_at, created_at
            FROM source_entities
            WHERE id IN ({placeholders})
        """, entity_ids)
        return [SourceEntity.from_row(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def count_unlinked_by_source(db_path: str = None) -> dict[str, int]:
    """
    Count unlinked source entities by source type.

    Returns:
        Dict mapping source_type to count of unlinked entities
    """
    db_path = db_path or get_crm_db_path()
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("""
            SELECT source_type, COUNT(*) as count
            FROM source_entities
            WHERE canonical_person_id IS NULL
            GROUP BY source_type
            ORDER BY count DESC
        """)
        return dict(cursor.fetchall())
    finally:
        conn.close()


def get_unlinked_entities_batch(
    source_type: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
    db_path: str = None,
) -> list[SourceEntity]:
    """
    Get a batch of unlinked source entities.

    Args:
        source_type: Optional filter by source type
        limit: Maximum entities to return
        offset: Offset for pagination
        db_path: Path to CRM database

    Returns:
        List of unlinked SourceEntity objects
    """
    db_path = db_path or get_crm_db_path()
    conn = sqlite3.connect(db_path)
    try:
        if source_type:
            cursor = conn.execute("""
                SELECT id, source_type, source_id, observed_name, observed_email,
                       observed_phone, metadata, canonical_person_id, link_confidence,
                       link_status, linked_at, observed_at, created_at
                FROM source_entities
                WHERE canonical_person_id IS NULL AND source_type = ?
                ORDER BY observed_at DESC
                LIMIT ? OFFSET ?
            """, (source_type, limit, offset))
        else:
            cursor = conn.execute("""
                SELECT id, source_type, source_id, observed_name, observed_email,
                       observed_phone, metadata, canonical_person_id, link_confidence,
                       link_status, linked_at, observed_at, created_at
                FROM source_entities
                WHERE canonical_person_id IS NULL
                ORDER BY observed_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))

        return [SourceEntity.from_row(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def relink_source_entities(
    source_type: Optional[str] = None,
    create_if_missing: bool = False,
    dry_run: bool = True,
    batch_size: int = 1000,
    max_entities: Optional[int] = None,
) -> dict:
    """
    Re-run entity resolution on unlinked source entities.

    Args:
        source_type: Filter to specific source type (e.g., 'gmail')
        create_if_missing: Whether to create new people for unmatched entities
        dry_run: If True, don't actually update anything
        batch_size: Number of entities to process per batch
        max_entities: Maximum total entities to process (None for all)

    Returns:
        Statistics dict with counts of linked, created, still-unlinked
    """
    source_store = get_source_entity_store()
    resolver = get_entity_resolver()

    stats = {
        'entities_processed': 0,
        'already_linked': 0,  # Shouldn't happen, but just in case
        'newly_linked': 0,
        'new_people_created': 0,
        'still_unlinked': 0,
        'errors': 0,
        'by_match_type': {},
    }

    # Get initial counts
    unlinked_counts = count_unlinked_by_source()
    if source_type:
        total_unlinked = unlinked_counts.get(source_type, 0)
        logger.info(f"Found {total_unlinked:,} unlinked {source_type} entities")
    else:
        total_unlinked = sum(unlinked_counts.values())
        logger.info(f"Found {total_unlinked:,} total unlinked entities")
        logger.info("Breakdown by source:")
        for st, count in sorted(unlinked_counts.items(), key=lambda x: -x[1]):
            logger.info(f"  {st}: {count:,}")

    if max_entities:
        total_to_process = min(total_unlinked, max_entities)
        logger.info(f"Will process up to {total_to_process:,} entities (--max-entities)")
    else:
        total_to_process = total_unlinked

    if total_to_process == 0:
        logger.info("No unlinked entities to process!")
        return stats

    # Fetch ALL unlinked entity IDs upfront to avoid re-processing issues
    # when entities get linked during execution
    logger.info("Fetching all unlinked entity IDs...")
    all_entity_ids = get_all_unlinked_entity_ids(source_type=source_type)
    if max_entities:
        all_entity_ids = all_entity_ids[:max_entities]
    logger.info(f"Will process {len(all_entity_ids):,} entities")

    # Process in batches
    batch_num = 0

    for i in range(0, len(all_entity_ids), batch_size):
        batch_num += 1
        batch_ids = all_entity_ids[i:i + batch_size]
        entities = get_entities_by_ids(batch_ids)

        if not entities:
            break

        logger.info(f"Processing batch {batch_num}: {len(entities)} entities "
                    f"({stats['entities_processed']}/{len(all_entity_ids)} done)")

        for entity in entities:
            stats['entities_processed'] += 1

            # Skip if somehow already linked (shouldn't happen)
            if entity.canonical_person_id:
                stats['already_linked'] += 1
                continue

            try:
                # Try to resolve using the entity's observed data
                result = resolver.resolve(
                    name=entity.observed_name,
                    email=entity.observed_email,
                    phone=entity.observed_phone,
                    create_if_missing=create_if_missing,
                )

                if result and result.entity:
                    # Found a match
                    if result.is_new:
                        stats['new_people_created'] += 1
                        match_type = f"new_person_{result.match_type}"
                    else:
                        stats['newly_linked'] += 1
                        match_type = result.match_type

                    # Track match types
                    stats['by_match_type'][match_type] = \
                        stats['by_match_type'].get(match_type, 0) + 1

                    # Update the source entity
                    if not dry_run:
                        source_store.link_to_person(
                            entity_id=entity.id,
                            canonical_person_id=result.entity.id,
                            confidence=result.confidence,
                            status=LINK_STATUS_AUTO,
                        )

                    # Log some examples
                    if stats['newly_linked'] <= 5:
                        logger.info(
                            f"  LINKED: {entity.observed_name or entity.observed_email} "
                            f"-> {result.entity.canonical_name} "
                            f"(type={result.match_type}, conf={result.confidence:.2f})"
                        )
                else:
                    stats['still_unlinked'] += 1

            except Exception as e:
                stats['errors'] += 1
                if stats['errors'] <= 5:
                    logger.warning(
                        f"Error processing entity {entity.id}: {e}"
                    )

    # Print summary
    logger.info(f"\n{'='*50}")
    logger.info(f"Entity Re-linking Summary")
    logger.info(f"{'='*50}")
    logger.info(f"Entities processed:   {stats['entities_processed']:,}")
    logger.info(f"Newly linked:         {stats['newly_linked']:,}")
    if create_if_missing:
        logger.info(f"New people created:   {stats['new_people_created']:,}")
    logger.info(f"Still unlinked:       {stats['still_unlinked']:,}")
    logger.info(f"Errors:               {stats['errors']:,}")

    if stats['by_match_type']:
        logger.info(f"\nMatches by type:")
        for match_type, count in sorted(stats['by_match_type'].items(), key=lambda x: -x[1]):
            logger.info(f"  {match_type}: {count:,}")

    if dry_run:
        logger.info(f"\nDRY RUN - no changes made. Use --execute to apply.")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Re-link unlinked source entities to existing person entities',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # See what would be linked (dry run)
    python scripts/relink_source_entities.py

    # Only process gmail entities
    python scripts/relink_source_entities.py --source-type gmail

    # Actually apply changes
    python scripts/relink_source_entities.py --execute

    # Process only first 1000 entities (for testing)
    python scripts/relink_source_entities.py --max-entities 1000

    # Create new people for unmatched entities (use with caution!)
    python scripts/relink_source_entities.py --execute --create-if-missing
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
        '--create-if-missing',
        action='store_true',
        help='Create new PersonEntity if no match found (use with caution!)'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=1000,
        help='Number of entities to process per batch (default: 1000)'
    )
    parser.add_argument(
        '--max-entities',
        type=int,
        help='Maximum total entities to process (for testing)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    relink_source_entities(
        source_type=args.source_type,
        create_if_missing=args.create_if_missing,
        dry_run=not args.execute,
        batch_size=args.batch_size,
        max_entities=args.max_entities,
    )


if __name__ == '__main__':
    main()
