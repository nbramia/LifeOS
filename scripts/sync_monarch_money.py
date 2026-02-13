#!/usr/bin/env python3
"""
Sync Monarch Money financial data to the Obsidian vault.

Generates a monthly Markdown summary at Personal/Finance/Monarch/YYYY-MM.md.
Designed to run on the 1st of each month for the previous month's data.

Usage:
    python scripts/sync_monarch_money.py                      # Dry run (default)
    python scripts/sync_monarch_money.py --execute            # Sync previous month
    python scripts/sync_monarch_money.py --execute --month 2026-01  # Specific month
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import asyncio
import logging
from datetime import date

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def sync_monarch(dry_run: bool = True, month: str | None = None) -> dict:
    """
    Sync Monarch Money data to vault.

    Args:
        dry_run: If True, just report what would happen
        month: Target month as YYYY-MM (defaults to previous month)

    Returns:
        Stats dict
    """
    from api.services.monarch import get_monarch_client

    # Determine target month
    if month:
        parts = month.split("-")
        year, mon = int(parts[0]), int(parts[1])
    else:
        today = date.today()
        if today.month == 1:
            year, mon = today.year - 1, 12
        else:
            year, mon = today.year, today.month - 1

    period = f"{year}-{mon:02d}"

    if dry_run:
        logger.info(f"DRY RUN — would sync Monarch Money data for {period}")
        logger.info(f"  Output: Personal/Finance/Monarch/{period}.md")
        return {"status": "dry_run", "period": period}

    logger.info(f"Starting Monarch Money sync for {period}...")

    client = get_monarch_client()
    result = await client.write_monthly_report(year, mon, dry_run=False)

    logger.info(f"\n=== Monarch Money Sync Results ===")
    logger.info(f"  Period: {period}")
    logger.info(f"  File: {result.get('file', 'N/A')}")
    logger.info(f"  Size: {result.get('size', 0)} chars")

    return result


def main():
    parser = argparse.ArgumentParser(description='Sync Monarch Money to vault')
    parser.add_argument('--execute', action='store_true', help='Actually sync (default is dry run)')
    parser.add_argument('--month', type=str, default=None, help='Target month as YYYY-MM (default: previous month)')
    args = parser.parse_args()

    # Health tracking
    run_id = None
    if args.execute:
        try:
            from api.services.sync_health import record_sync_start, record_sync_complete, SyncStatus
            run_id = record_sync_start("monarch_money")
        except Exception as e:
            logger.warning(f"Could not record sync start: {e}")

    try:
        result = asyncio.run(sync_monarch(dry_run=not args.execute, month=args.month))

        if run_id is not None:
            try:
                from api.services.sync_health import record_sync_complete, SyncStatus
                record_sync_complete(
                    run_id,
                    status=SyncStatus.SUCCESS,
                    records_processed=1,
                    records_created=1 if result.get("status") == "success" else 0,
                )
            except Exception as e:
                logger.warning(f"Could not record sync completion: {e}")

    except Exception as e:
        logger.error(f"Monarch Money sync failed: {e}")
        if run_id is not None:
            try:
                from api.services.sync_health import record_sync_complete, SyncStatus
                record_sync_complete(run_id, status=SyncStatus.FAILED, error_message=str(e))
            except Exception:
                pass
        sys.exit(1)


if __name__ == '__main__':
    main()
