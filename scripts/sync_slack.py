#!/usr/bin/env python3
"""
Sync Slack data to LifeOS CRM.

This script:
1. Syncs Slack users to SourceEntity
2. Indexes DM messages to ChromaDB
3. Creates Interaction records
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# override=True: the .env file deterministically wins over inherited env vars.
# A present-but-empty inherited SLACK_USER_TOKEN would otherwise shadow the
# file value and silently disable the sync — issue #438.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def run_slack_sync(full: bool = False, dry_run: bool = True) -> dict:
    """
    Run Slack sync.

    Args:
        full: If True, run full sync; otherwise incremental
        dry_run: If True, just report what would happen

    Returns:
        Stats dict
    """
    from api.services.slack_integration import is_slack_enabled
    from api.services.slack_sync import get_slack_sync

    if not is_slack_enabled():
        logger.warning("Slack integration not enabled (check SLACK_USER_TOKEN)")
        return {"status": "skipped", "reason": "not_enabled"}

    if dry_run:
        logger.info("DRY RUN - would sync Slack data")
        logger.info(f"  Mode: {'full' if full else 'incremental'}")
        return {"status": "dry_run"}

    sync = get_slack_sync()

    if full:
        logger.info("Running full Slack sync...")
        results = sync.full_sync()
    else:
        logger.info("Running incremental Slack sync...")
        results = sync.incremental_sync()

    # Log results
    logger.info("\n=== Slack Sync Results ===")

    if "users" in results and results["users"]:
        users = results["users"]
        logger.info("Users:")
        # ``sync_slack_users`` returns ``total`` (count of fetched users),
        # not ``synced`` — the historical 'Synced:' key always logged 0.
        logger.info(f"  Total: {users.get('total', 0)}")
        logger.info(f"  Created: {users.get('created', 0)}")
        logger.info(f"  Updated: {users.get('updated', 0)}")
        logger.info(f"  Skipped (bots): {users.get('skipped_bots', 0)}")
        logger.info(f"  Skipped (deleted): {users.get('skipped_deleted', 0)}")

    if "messages" in results and results["messages"]:
        msgs = results["messages"]
        logger.info("Messages:")
        logger.info(f"  Channels synced: {msgs.get('channels_synced', 0)}")
        logger.info(f"  Messages indexed: {msgs.get('messages_indexed', 0)}")
        logger.info(f"  Interactions created: {msgs.get('interactions_created', 0)}")

    if results.get("errors"):
        logger.warning(f"Errors: {len(results['errors'])}")
        for err in results["errors"]:
            logger.warning(f"  - {err}")

    logger.info(f"Status: {results.get('status', 'unknown')}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")

    # Canonical line consumed by run_all_syncs._parse_sync_output.
    from api.services.sync_health import emit_sync_stats
    users = results.get("users") or {}
    msgs = results.get("messages") or {}
    emit_sync_stats({
        "source_entities_created": int(users.get("created", 0) or 0),
        "people_updated": int(users.get("updated", 0) or 0),
        "interactions_created": int(msgs.get("interactions_created", 0) or 0),
    })

    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sync Slack data to LifeOS')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--full', action='store_true', help='Run full sync (default: incremental)')
    args = parser.parse_args(argv)

    results = run_slack_sync(full=args.full, dry_run=not args.execute)

    # A skipped sync (e.g. SLACK_USER_TOKEN missing/empty) must exit nonzero
    # so run_all_syncs records FAILED and alerts, instead of the silent
    # success-with-zeros that hid the July 2026 outage — issue #438.
    if results.get("status") == "skipped":
        logger.error(
            f"Slack sync skipped ({results.get('reason', 'unknown')}) — "
            f"exiting nonzero so the orchestrator records a failure"
        )
        sys.exit(2)

    # Errors with zero work done = total failure (e.g. a dead token at
    # API-call time). full_sync/incremental_sync report that as status
    # "partial", so status alone can't distinguish it from a run that mostly
    # worked. Partial errors WITH real work remain success, consistent with
    # gmail's per-message error tolerance — issue #438.
    msgs = results.get("messages") or {}
    if results.get("errors") and not msgs.get("channels_synced") and not msgs.get("messages_indexed"):
        logger.error(
            "Slack sync reported errors and completed no work — "
            "exiting nonzero so the orchestrator records a failure"
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
