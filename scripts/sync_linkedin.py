#!/usr/bin/env python3
"""
Sync LinkedIn connections from CSV to PersonEntity store.

Reads the LinkedIn connections CSV export and creates/updates PersonEntity
records for each connection. This provides company, position, and LinkedIn
URL data that enriches the people database.

Data source: LinkedIn "Export Your Data" → Connections.csv
Expected location: ./data/LinkedInConnections.csv
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_CSV_PATH = "./data/LinkedInConnections.csv"


def sync_linkedin(csv_path: str = DEFAULT_CSV_PATH, dry_run: bool = True) -> dict:
    """
    Sync LinkedIn connections to PersonEntity store.

    Args:
        csv_path: Path to LinkedIn connections CSV
        dry_run: If True, just report what would happen

    Returns:
        Stats dict
    """
    from api.services.people_aggregator import sync_linkedin_to_v2
    from api.services.entity_resolver import get_entity_resolver

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.warning(f"LinkedIn CSV not found: {csv_path}")
        return {"status": "skipped", "reason": "csv_not_found"}

    if dry_run:
        logger.info("DRY RUN - would sync LinkedIn connections")
        logger.info(f"  CSV path: {csv_path}")

        # Count connections in file
        import csv
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                count = sum(1 for _ in reader)
            logger.info(f"  Connections in file: {count}")
        except Exception as e:
            logger.warning(f"  Could not read CSV: {e}")

        return {"status": "dry_run"}

    logger.info(f"Syncing LinkedIn connections from {csv_path}...")

    resolver = get_entity_resolver()
    results = sync_linkedin_to_v2(
        csv_path=csv_path,
        entity_resolver=resolver,
    )

    logger.info(f"\n=== LinkedIn Sync Results ===")
    logger.info(f"  Connections processed: {results.get('connections_processed', 0)}")
    logger.info(f"  Entities created: {results.get('entities_created', 0)}")
    logger.info(f"  Entities updated: {results.get('entities_updated', 0)}")
    logger.info(f"  Connections skipped: {results.get('connections_skipped', 0)}")

    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync LinkedIn connections to LifeOS')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--csv', default=DEFAULT_CSV_PATH, help='Path to LinkedIn CSV')
    args = parser.parse_args()

    sync_linkedin(csv_path=args.csv, dry_run=not args.execute)
