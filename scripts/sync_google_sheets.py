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

        if results.get("status") == "skipped":
            # No config/gsheet_sync.yaml (or an empty/disabled one) — e.g.
            # the gsheet-journal Form -> Sheet pipeline was never set up.
            # Declare the skip so the parent records SKIPPED instead of a
            # zero-count "success" — same pattern as Photos/Apple Contacts/
            # Monarch.
            reason = results.get("reason", "not configured")
            logger.info(f"GSheet sync skipped: {reason}")
            if reason.startswith("dailies_db_"):
                # Only the Dailies database source is enabled, and it could
                # not run (no credential, unreachable, ambiguous members).
                print(f"SYNC_SKIPPED: Dailies database source skipped ({reason})", flush=True)
            else:
                print(
                    "SYNC_SKIPPED: No Google Sheets configured — copy "
                    "config/gsheet_sync.example.yaml to config/gsheet_sync.yaml "
                    "to enable",
                    flush=True,
                )
            return results

        if results.get("status") == "error":
            # The config file exists but could not be loaded: a real failure,
            # distinct from "not configured". main() turns it into a nonzero
            # exit.
            logger.error(f"GSheet sync failed: {results.get('reason', 'unknown')}")
            return {**results, "error": results.get("reason", "unknown")}

        new_rows = int(results.get("synced", 0) or 0)
        skipped_rows = int(results.get("skipped", 0) or 0)
        logger.info("\n=== Google Sheets Sync Results ===")
        logger.info(f"  New rows written: {new_rows}")
        logger.info(f"  Rows already synced: {skipped_rows}")
        logger.info(f"  Sheets failed: {results.get('failed', 0)}")

        # Canonical line consumed by run_all_syncs._parse_sync_output. Rows
        # examined count as processed so a sheet with no new responses tonight
        # is still visibly working; only new rows count as created.
        from api.services.sync_health import emit_sync_stats
        emit_sync_stats({
            "processed": new_rows + skipped_rows,
            "created": new_rows,
            "errors": int(results.get("failed", 0) or 0),
        })
        return results
    except Exception as e:
        logger.error(f"Google Sheets sync failed: {e}")
        return {"status": "error", "error": str(e)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sync Google Sheets to vault')
    parser.add_argument('--execute', action='store_true', help='Actually sync sheets')
    args = parser.parse_args(argv)

    results = sync_google_sheets(dry_run=not args.execute)

    # A sync that failed outright (an unreadable config, expired OAuth) must
    # exit nonzero so run_all_syncs records FAILED instead of success. A
    # "not configured" skip stays a clean exit.
    if results.get("status") == "error":
        logger.error(
            f"Google Sheets sync failed ({results.get('error', 'unknown')}) — "
            f"exiting nonzero so the orchestrator records a failure"
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
