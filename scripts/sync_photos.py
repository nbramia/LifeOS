#!/usr/bin/env python3
"""
Sync Apple Photos face recognition data to LifeOS CRM.

Creates SourceEntity and Interaction records from Photos face appearances.
Uses Contact UUID matching for reliable person identification.

Usage:
    python scripts/sync_photos.py [--execute] [--dry-run] [--since YYYY-MM-DD]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def run_photos_sync(dry_run: bool = True, since: datetime = None) -> dict:
    """Run Apple Photos sync."""
    from config.settings import settings

    # Check availability FIRST - fail fast if Photos library unavailable
    if not settings.photos_enabled:
        logger.warning(
            f"Photos library not available at {settings.photos_library_path}. "
            "Ensure the external drive is mounted."
        )
        return {
            "status": "skipped",
            "reason": "photos_not_available",
            "errors": 0,
        }

    # Note: Photos.app is already open and syncing from iCloud
    # The nightly sync opens Photos at the very beginning (3 AM) so it can
    # sync in the background while other data sources are being processed

    if dry_run:
        logger.info("DRY RUN - would sync Photos data")
        try:
            from api.services.apple_photos import get_apple_photos_reader
            reader = get_apple_photos_reader()
            stats = reader.get_stats()
            logger.info(f"Photos library stats:")
            logger.info(f"  Named people: {stats['total_named_people']}")
            logger.info(f"  With contacts: {stats['people_with_contacts']}")
            logger.info(f"  Face detections: {stats['total_face_detections']}")
            return {"status": "dry_run", "photos_stats": stats}
        except FileNotFoundError as e:
            logger.warning(f"Photos database not accessible: {e}")
            return {"status": "dry_run", "error": str(e)}

    # Run actual sync
    try:
        from api.services.apple_photos_sync import sync_apple_photos

        logger.info("Running Apple Photos sync...")
        results = sync_apple_photos(since=since)

        # Log in format that run_all_syncs.py can parse
        logger.info(f"\n=== Photos Sync Results ===")
        logger.info(f"Photos people total: {results.get('photos_people_total', 0)}")
        logger.info(f"Photos people with contacts: {results.get('photos_people_with_contacts', 0)}")
        logger.info(f"Person matches: {results.get('person_matches', 0)}")
        logger.info(f"Source entities created: {results.get('source_entities_created', 0)}")
        logger.info(f"Interactions created: {results.get('interactions_created', 0)}")
        logger.info(f"Errors: {results.get('errors', 0)}")

        return results

    except FileNotFoundError as e:
        logger.error(f"Photos database not found: {e}")
        return {"status": "failed", "error": str(e), "errors": 1}
    except Exception as e:
        logger.error(f"Photos sync failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"status": "failed", "error": str(e), "errors": 1}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync Apple Photos to LifeOS CRM')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--dry-run', action='store_true', help='Report what would happen')
    parser.add_argument('--since', type=str, help='Only sync photos after date (YYYY-MM-DD)')
    args = parser.parse_args()

    since = None
    if args.since:
        since = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=timezone.utc)

    dry_run = not args.execute or args.dry_run
    run_photos_sync(dry_run=dry_run, since=since)
