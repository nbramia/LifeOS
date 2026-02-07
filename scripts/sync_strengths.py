#!/usr/bin/env python3
"""
Update relationship strengths for all people.

This should run after all other syncs to ensure strengths reflect
the latest interaction data.
"""
import sys
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.relationship_metrics import update_all_strengths

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Update relationship strengths')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    args = parser.parse_args()

    if not args.execute:
        logger.info("DRY RUN - use --execute to apply changes")
        logger.info("Would update relationship strengths for all people")
        return

    result = update_all_strengths()
    logger.info(f"\n=== Strength Update Summary ===")
    logger.info(f"Updated: {result['updated']}")
    logger.info(f"Failed: {result['failed']}")
    logger.info(f"Total: {result['total']}")


if __name__ == '__main__':
    main()
