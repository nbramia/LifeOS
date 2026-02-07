#!/usr/bin/env python3
"""
Verify and repair PersonEntity stats from interaction database.

This script is a VERIFICATION tool, not a sync step. Each sync script now
calls refresh_person_stats() after modifying interactions, so stats should
always be in sync. This tool catches any discrepancies that slipped through.

Usage:
    # Check for discrepancies (dry run)
    uv run python scripts/sync_person_stats.py

    # Fix any discrepancies found
    uv run python scripts/sync_person_stats.py --execute

    # Full refresh (rebuilds all stats from scratch)
    uv run python scripts/sync_person_stats.py --full --execute
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging

from api.services.person_stats import refresh_person_stats, verify_person_stats

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Verify and repair PersonEntity stats from interactions'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Fix any discrepancies found'
    )
    parser.add_argument(
        '--full',
        action='store_true',
        help='Full refresh - rebuild all stats from scratch (ignores discrepancies)'
    )
    args = parser.parse_args()

    if args.full:
        # Full refresh mode - rebuild all stats
        logger.info("Running full PersonEntity stats refresh...")
        if not args.execute:
            logger.info("DRY RUN - use --execute to apply changes")
            logger.info("Would refresh stats for all people from InteractionStore")
            return

        stats = refresh_person_stats(person_ids=None, save=True)
        logger.info(f"\n=== Full Refresh Complete ===")
        logger.info(f"People updated: {stats['updated']}")
        logger.info(f"Total interactions: {stats['total_interactions']}")
        return

    # Verification mode - check for discrepancies
    logger.info("Verifying PersonEntity stats against InteractionStore...")
    discrepancies = verify_person_stats(fix=args.execute)

    if not discrepancies:
        logger.info("\n=== Verification Complete ===")
        logger.info("All PersonEntity stats are consistent with InteractionStore.")
        return

    logger.info(f"\n=== Found {len(discrepancies)} discrepancies ===")
    for person_id, details in discrepancies.items():
        logger.info(f"\n{details['name']} ({person_id[:8]}...):")
        logger.info(f"  Cached:   email={details['cached']['email']}, meeting={details['cached']['meeting']}, "
                   f"mention={details['cached']['mention']}, message={details['cached']['message']}")
        logger.info(f"  Computed: email={details['computed']['email']}, meeting={details['computed']['meeting']}, "
                   f"mention={details['computed']['mention']}, message={details['computed']['message']}")

    if args.execute:
        logger.info(f"\nFixed {len(discrepancies)} discrepancies.")
    else:
        logger.info("\nDRY RUN - use --execute to fix discrepancies")


if __name__ == '__main__':
    main()
