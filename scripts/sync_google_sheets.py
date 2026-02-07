#!/usr/bin/env python3
"""
Sync configured Google Sheets to the Obsidian vault as Markdown.

Pulls content from Google Sheets (e.g., daily journals, form responses)
and saves as markdown files in the vault.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def sync_google_sheets(dry_run: bool = True) -> dict:
    """
    Sync Google Sheets to vault.

    Args:
        dry_run: If True, just report what would happen

    Returns:
        Stats dict
    """
    from api.services.gsheet_sync import sync_gsheets

    if dry_run:
        logger.info("DRY RUN - would sync configured Google Sheets to vault")
        return {"status": "dry_run"}

    logger.info("Starting Google Sheets sync...")

    try:
        results = sync_gsheets()
        logger.info(f"\n=== Google Sheets Sync Results ===")
        logger.info(f"  Sheets synced: {results.get('synced', 0)}")
        logger.info(f"  Rows processed: {results.get('rows', 0)}")
        return results
    except Exception as e:
        logger.error(f"Google Sheets sync failed: {e}")
        return {"status": "error", "error": str(e)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync Google Sheets to vault')
    parser.add_argument('--execute', action='store_true', help='Actually sync sheets')
    args = parser.parse_args()

    sync_google_sheets(dry_run=not args.execute)
