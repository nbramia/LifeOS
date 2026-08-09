#!/usr/bin/env python3
"""
Run relationship discovery to populate edge weights in the relationships table.

This script runs all discovery methods to populate:
- shared_events_count (calendar)
- shared_threads_count (email)
- shared_messages_count (iMessage/SMS)
- shared_whatsapp_count (WhatsApp)
- shared_slack_count (Slack DMs)
- shared_phone_calls_count (phone calls)
- is_linkedin_connection (LinkedIn)

Must run AFTER all interaction syncs (gmail, calendar, imessage, whatsapp, slack, phone)
and AFTER link_slack (to have Slack entities linked to people).
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def run_relationship_discovery(dry_run: bool = True, days_back: int = 3650) -> dict:
    """
    Run relationship discovery.

    Args:
        dry_run: If True, just report what would happen
        days_back: Days to look back for interactions

    Returns:
        Stats dict
    """
    from api.services.relationship_discovery import run_full_discovery

    if dry_run:
        logger.info("DRY RUN - would run relationship discovery")
        logger.info(f"  Days back: {days_back}")
        logger.info("\nThis would discover/update relationships from:")
        logger.info("  - Calendar events (shared_events_count)")
        logger.info("  - Email threads (shared_threads_count)")
        logger.info("  - iMessage/SMS (shared_messages_count)")
        logger.info("  - WhatsApp (shared_whatsapp_count)")
        logger.info("  - Slack DMs (shared_slack_count)")
        logger.info("  - Phone calls (shared_phone_calls_count)")
        logger.info("  - LinkedIn connections (is_linkedin_connection)")
        return {"status": "dry_run"}

    logger.info(f"Running relationship discovery (days_back={days_back})...")
    results = run_full_discovery(days_back=days_back)

    logger.info("\n=== Relationship Discovery Results ===")
    for source, count in results.get("by_source", {}).items():
        logger.info(f"  {source}: {count} relationships updated")
    logger.info(f"  Total: {results.get('total', 0)} relationships updated")

    # Canonical line consumed by run_all_syncs._parse_sync_output. Without it
    # this phase reported 0/0/0 nightly despite ~40 minutes of real work (#496).
    from api.services.sync_health import emit_sync_stats
    emit_sync_stats({"people_updated": int(results.get("total", 0) or 0)})

    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run relationship discovery')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--days-back', type=int, default=3650, help='Days to look back (default: ~10 years)')
    args = parser.parse_args()

    run_relationship_discovery(dry_run=not args.execute, days_back=args.days_back)
