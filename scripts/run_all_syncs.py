#!/usr/bin/env python3
"""
Run all CRM data source syncs with health monitoring.

This script should be run daily via launchd or cron. It:
1. Syncs all configured data sources
2. Records sync status and errors in sync_health.db
3. Logs all output for debugging
4. Exits with non-zero status if any critical sync fails

Usage:
    python scripts/run_all_syncs.py [--source SOURCE] [--dry-run] [--force]

Options:
    --source SOURCE   Run only this specific source
    --dry-run         Don't actually sync, just report what would run
    --force           Run even if sync was run recently
"""
# Load environment variables from .env FIRST, before any other imports
# This is critical for launchd/cron which don't have access to shell environment
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import argparse
import logging
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.sync_health import (
    SYNC_SOURCES,
    SyncStatus,
    record_sync_start,
    record_sync_complete,
    record_sync_error,
    get_sync_health,
    get_sync_summary,
    check_sync_health,
)
from config.settings import settings

# Markdown error log in Notes directory (for visibility)
NOTES_ERROR_LOG = Path.home() / "Notes 2025" / "LifeOS" / "sync_errors.md"


def log_error_to_markdown(source: str, error_msg: str, error_type: str = "error"):
    """
    Log an error to the markdown file in Notes for visibility.

    Errors are prepended so the most recent appear at the top.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entry = f"""
## {timestamp} - {source.upper()} - {error_type}

```
{error_msg[:2000]}
```

---
"""

    _write_to_markdown_log(entry)


def log_sync_summary_to_markdown(result: dict):
    """
    Log a sync run summary to the markdown file.

    Only logs if there were failures, to avoid noise from successful runs.
    """
    if result["failed"] == 0:
        return  # Don't log successful runs

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    failed_list = ", ".join(result["failed_sources"]) if result["failed_sources"] else "none"

    entry = f"""
## {timestamp} - SYNC RUN SUMMARY

- **Total sources:** {result["sources_run"]}
- **Succeeded:** {result["succeeded"]}
- **Failed:** {result["failed"]}
- **Failed sources:** {failed_list}

---
"""

    _write_to_markdown_log(entry)


def _write_to_markdown_log(entry: str):
    """Write an entry to the markdown log file, prepending after the header."""
    try:
        # Ensure directory exists
        NOTES_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)

        if NOTES_ERROR_LOG.exists():
            existing = NOTES_ERROR_LOG.read_text()
        else:
            existing = """# LifeOS Sync Errors

This file tracks errors from the nightly sync process. Most recent errors appear first.

---
"""

        # Prepend new entry after the header
        header_end = existing.find("---\n")
        if header_end != -1:
            header = existing[:header_end + 4]
            body = existing[header_end + 4:]
            new_content = header + entry + body
        else:
            new_content = existing + entry

        NOTES_ERROR_LOG.write_text(new_content)
        logger.info(f"Entry logged to {NOTES_ERROR_LOG}")

    except Exception as e:
        logger.warning(f"Failed to write to markdown error log: {e}")

# Configure logging
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# UNIFIED SYNC ORDER - Organized by Phase
# =============================================================================
#
# Phase 1: Data Collection - Pull fresh data from all external sources
# Phase 2: Entity Processing - Link source entities to canonical people
# Phase 3: Relationship Building - Build relationships and compute metrics
# Phase 4: Vector Store Indexing - Index content with fresh people data
# Phase 5: Content Sync - Pull external content into vault
#
# This order ensures downstream processes have access to fresh upstream data.
# =============================================================================

SYNC_ORDER = [
    # === Phase 1: Data Collection ===
    # Pull fresh data from all external sources (no dependencies on each other)
    "gmail",                    # Gmail sent + received + CC
    "calendar",                 # Google Calendar events
    "linkedin",                 # LinkedIn connections CSV
    "contacts",                 # Apple Contacts CSV
    "phone",                    # Phone call history
    "whatsapp",                 # WhatsApp contacts + messages
    "imessage",                 # iMessage/SMS
    "slack",                    # Slack users + DM messages

    # === Phase 2: Entity Processing ===
    # Link source entities to canonical PersonEntity records
    "link_slack",               # Link Slack users to people by email
    "link_imessage",            # Link iMessage handles to people by phone
    "link_source_entities",     # Retroactive linking for all unlinked entities
    "photos",                   # Sync Photos face data to people

    # === Phase 3: Relationship Building ===
    # Build relationships using all collected interaction data
    # Note: person_stats is no longer in sync order - each sync script refreshes
    # its own affected PersonEntity stats via refresh_person_stats()
    "relationship_discovery",   # Discover relationships, populate edge weights
    "strengths",                # Calculate relationship strength scores

    # === Phase 4: Vector Store Indexing ===
    # Index content with fresh people data available for entity resolution
    "vault_reindex",            # Full reindex with LLM summaries (no timeout)
    "crm_vectorstore",          # Index CRM people for semantic search

    # === Phase 5: Content Sync ===
    # Pull external content into vault (will be indexed on next run)
    "google_docs",              # Sync Google Docs to vault as markdown
    "google_sheets",            # Sync Google Sheets to vault as markdown

    # === Phase 6: Post-Sync Cleanup ===
    # Clean up entity data quality issues after all other syncs
    "entity_cleanup",           # Auto-hide non-humans, queue duplicates for review
]

# Scripts that can be run directly
SYNC_SCRIPTS = {
    # Phase 1: Data Collection
    "gmail": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--gmail-only", "--days", "30"]),
    "calendar": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--calendar-only", "--days", "30"]),
    "linkedin": ("scripts/sync_linkedin.py", ["--execute"]),
    "contacts": ("scripts/sync_contacts_csv.py", ["--execute"]),
    "phone": ("scripts/sync_phone_calls.py", ["--execute"]),
    "whatsapp": ("scripts/sync_whatsapp.py", ["--execute"]),
    "imessage": ("scripts/sync_imessage_interactions.py", ["--execute"]),
    "slack": ("scripts/sync_slack.py", ["--execute"]),

    # Phase 2: Entity Processing
    "link_slack": ("scripts/link_slack_entities.py", ["--execute"]),
    "link_imessage": ("scripts/link_imessage_entities.py", ["--execute"]),
    "link_source_entities": ("scripts/link_source_entities.py", ["--execute"]),
    "photos": ("scripts/sync_photos.py", ["--execute"]),

    # Phase 3: Relationship Building
    # Note: person_stats removed - each sync script now refreshes its own stats
    "relationship_discovery": ("scripts/sync_relationship_discovery.py", ["--execute"]),
    "strengths": ("scripts/sync_strengths.py", ["--execute"]),
    "push_birthdays": ("scripts/push_birthdays_to_contacts.py", ["--execute"]),

    # Phase 4: Vector Store Indexing
    "vault_reindex": ("scripts/sync_vault_reindex.py", ["--execute"]),
    "crm_vectorstore": ("scripts/sync_crm_to_vectorstore.py", ["--execute"]),

    # Phase 5: Content Sync
    "google_docs": ("scripts/sync_google_docs.py", ["--execute"]),
    "google_sheets": ("scripts/sync_google_sheets.py", ["--execute"]),

    # Phase 6: Post-Sync Cleanup
    "entity_cleanup": ("scripts/sync_entity_cleanup.py", ["--execute"]),
}

# Per-source timeout overrides (seconds)
# Default is 60 minutes (3600).
# Note: vault_reindex has no timeout - it runs as long as needed
DEFAULT_SYNC_TIMEOUT = 3600  # 60 minutes

SYNC_TIMEOUTS = {
    "vault_reindex": None,           # No timeout - runs as long as needed
}


def get_disabled_work_sources() -> set[str]:
    """
    Return set of sources that should be skipped because work integrations are disabled.

    Work integrations are disabled by default for safety - work data will only be
    synced if explicitly enabled via environment variables.
    """
    disabled = set()

    # Gmail/Calendar work accounts require both the toggle AND work domain to be set
    has_work_domain = bool(settings.work_email_domain)

    if not settings.sync_work_gmail or not has_work_domain:
        # Gmail sync will still run for personal, but sync script handles work filtering
        # We just log a warning here - actual filtering happens in sync_gmail_calendar_interactions.py
        pass

    if not settings.sync_work_calendar or not has_work_domain:
        # Same as above - sync script handles work filtering
        pass

    # Slack requires explicit opt-in
    if not settings.sync_slack:
        disabled.add("slack")
        disabled.add("link_slack")

    return disabled


def run_sync(source: str, dry_run: bool = False) -> tuple[bool, dict]:
    """
    Run a single sync operation.

    Returns:
        Tuple of (success, stats_dict)
    """
    if source not in SYNC_SCRIPTS:
        logger.warning(f"No script configured for source: {source}")
        return False, {"error": f"No script for {source}"}

    script_path, args = SYNC_SCRIPTS[source]
    full_path = Path(__file__).parent.parent / script_path

    if not full_path.exists():
        logger.error(f"Script not found: {full_path}")
        return False, {"error": f"Script not found: {script_path}"}

    if dry_run:
        logger.info(f"[DRY RUN] Would run: python {script_path} {' '.join(args)}")
        return True, {"dry_run": True}

    # Record sync start
    run_id = record_sync_start(source)

    try:
        logger.info(f"Starting sync for {source}...")

        # Build command
        venv_python = Path(__file__).parent.parent / ".venv" / "bin" / "python"
        if not venv_python.exists():
            venv_python = sys.executable

        cmd = [str(venv_python), str(full_path)] + args

        # Get per-source timeout (default 60 minutes)
        timeout_seconds = SYNC_TIMEOUTS.get(source, DEFAULT_SYNC_TIMEOUT)

        # Run subprocess
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(Path(__file__).parent.parent),
            env={
                **dict(__import__('os').environ),
                "PYTHONPATH": str(Path(__file__).parent.parent),
            }
        )

        # Parse output for stats
        stats = _parse_sync_output(result.stdout)

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"Sync failed for {source}: {error_msg}")

            record_sync_complete(
                run_id,
                SyncStatus.FAILED,
                records_processed=stats.get("processed", 0),
                records_created=stats.get("created", 0),
                records_updated=stats.get("updated", 0),
                errors=1,
                error_message=error_msg[:500],
            )

            record_sync_error(
                source,
                error_msg[:1000],
                error_type="subprocess_error",
                context=f"Command: {' '.join(cmd)}"
            )

            # Log to markdown for visibility
            log_error_to_markdown(source, error_msg, "subprocess_error")

            return False, {"error": error_msg, **stats}

        logger.info(f"Sync completed for {source}: {stats}")

        record_sync_complete(
            run_id,
            SyncStatus.SUCCESS,
            records_processed=stats.get("processed", 0),
            records_created=stats.get("created", 0),
            records_updated=stats.get("updated", 0),
            errors=stats.get("errors", 0),
        )

        return True, stats

    except subprocess.TimeoutExpired as e:
        timeout_minutes = SYNC_TIMEOUTS.get(source, DEFAULT_SYNC_TIMEOUT) // 60

        # Capture partial output from the killed process
        partial_stdout = e.stdout or ""
        partial_stderr = e.stderr or ""

        # Parse what was accomplished before timeout
        stats = _parse_sync_output(partial_stdout)

        # Build error message with partial progress info
        error_msg = f"Sync timed out after {timeout_minutes} minutes"
        if stats.get("processed", 0) > 0 or stats.get("created", 0) > 0:
            error_msg += f" (partial progress: {stats.get('processed', 0)} processed, {stats.get('created', 0)} created)"

        logger.error(f"Sync timeout for {source}")
        if partial_stdout:
            # Log last 50 lines of output to see progress
            last_lines = "\n".join(partial_stdout.strip().split("\n")[-50:])
            logger.info(f"Partial output before timeout:\n{last_lines}")

        record_sync_complete(
            run_id,
            SyncStatus.FAILED,
            records_processed=stats.get("processed", 0),
            records_created=stats.get("created", 0),
            records_updated=stats.get("updated", 0),
            errors=1,
            error_message=error_msg,
        )

        # Include partial output in markdown log for visibility
        full_error_msg = error_msg
        if partial_stdout:
            full_error_msg += f"\n\nLast output before timeout:\n{partial_stdout[-2000:]}"

        record_sync_error(source, full_error_msg[:1000], error_type="timeout")
        log_error_to_markdown(source, full_error_msg, "timeout")
        return False, {"error": error_msg, **stats}

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Sync exception for {source}: {error_msg}")
        logger.error(traceback.format_exc())

        record_sync_complete(
            run_id,
            SyncStatus.FAILED,
            errors=1,
            error_message=error_msg[:500],
        )

        record_sync_error(
            source,
            error_msg,
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc(),
        )

        # Log to markdown with full stack trace
        full_error = f"{error_msg}\n\n{traceback.format_exc()}"
        log_error_to_markdown(source, full_error, type(e).__name__)

        return False, {"error": error_msg}


def _parse_sync_output(output: str) -> dict:
    """Parse sync script output for statistics."""
    stats = {
        "processed": 0,
        "created": 0,
        "updated": 0,
        "errors": 0,
    }

    # Common patterns in sync outputs
    import re

    patterns = [
        (r"(\d+)\s*(?:records?|items?|entities?)\s*(?:read|processed|found)", "processed"),
        (r"(?:created|new)\s*[:\s]*(\d+)", "created"),
        (r"(?:updated)\s*[:\s]*(\d+)", "updated"),
        (r"(?:errors?)\s*[:\s]*(\d+)", "errors"),
        (r"source.entities.created\s*[:\s]*(\d+)", "created"),
        (r"source.entities.updated\s*[:\s]*(\d+)", "updated"),
        (r"interactions.created\s*[:\s]*(\d+)", "created"),
        (r"persons?.created\s*[:\s]*(\d+)", "created"),
    ]

    for pattern, key in patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            stats[key] = max(stats[key], int(match.group(1)))

    return stats


def backup_interactions():
    """Create interactions backup before sync operations."""
    from api.services.interaction_store import InteractionStore
    logger.info("Creating pre-sync interactions backup...")
    store = InteractionStore()
    backup_path = store.create_backup()
    if backup_path:
        logger.info(f"Interactions backup: {backup_path}")
    return backup_path


def run_all_syncs(
    sources: list[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """
    Run all syncs in order.

    Returns:
        Summary dict with results
    """
    sources = sources or SYNC_ORDER
    results = {}
    failed = []

    # Check for disabled work integrations
    disabled_sources = get_disabled_work_sources()
    if disabled_sources:
        logger.info(f"Work integration sources disabled: {', '.join(sorted(disabled_sources))}")
        logger.info("Enable via LIFEOS_SYNC_SLACK=true, etc. in .env")

    logger.info(f"Starting sync run for {len(sources)} sources...")
    logger.info(f"Log file: {log_file}")

    # Trigger Photos.app to open and start iCloud sync in background
    # This runs at the beginning so Photos can sync throughout the entire process
    if not dry_run:
        try:
            logger.info("Opening Photos.app to trigger iCloud sync in background...")
            subprocess.run(
                ["osascript", "-e", 'tell application "Photos" to activate'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            logger.info("Photos.app opened - will sync in background during data collection")
        except Exception as e:
            logger.warning(f"Could not open Photos.app: {e}")

    # Create interactions backup before any syncs
    # (person entities backup happens automatically on save)
    if not dry_run:
        backup_interactions()

    for source in sources:
        if source not in SYNC_SOURCES:
            logger.warning(f"Unknown source: {source}, skipping")
            continue

        # Skip sources disabled by work integration settings
        if source in disabled_sources:
            logger.info(f"Skipping {source}: work integration disabled")
            results[source] = {"skipped": True, "reason": "work_integration_disabled"}
            continue

        # Check if recently synced (unless forced)
        if not force and not dry_run:
            health = get_sync_health(source)
            if health.hours_since_sync is not None and health.hours_since_sync < 1:
                logger.info(f"Skipping {source}: synced {health.hours_since_sync:.1f}h ago")
                results[source] = {"skipped": True, "reason": "recently_synced"}
                continue

        success, stats = run_sync(source, dry_run=dry_run)
        results[source] = {"success": success, **stats}

        if not success:
            failed.append(source)

    # Log summary
    logger.info("=" * 60)
    logger.info("SYNC RUN COMPLETE")
    logger.info(f"Total sources: {len(sources)}")
    logger.info(f"Succeeded: {len(sources) - len(failed)}")
    logger.info(f"Failed: {len(failed)}")
    if failed:
        logger.error(f"Failed sources: {', '.join(failed)}")
    logger.info("=" * 60)

    # Check overall health
    is_healthy, health_msg = check_sync_health()
    logger.info(f"Overall health: {health_msg}")

    result = {
        "sources_run": len(sources),
        "succeeded": len(sources) - len(failed),
        "failed": len(failed),
        "failed_sources": failed,
        "results": results,
        "is_healthy": is_healthy,
        "health_message": health_msg,
    }

    # Log summary to markdown file if there were failures
    log_sync_summary_to_markdown(result)

    return result


def main():
    parser = argparse.ArgumentParser(description="Run CRM data source syncs")
    parser.add_argument("--source", help="Run only this specific source")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually sync")
    parser.add_argument("--execute", action="store_true", help="Actually run syncs (required for non-dry-run)")
    parser.add_argument("--force", action="store_true", help="Run even if recently synced")
    parser.add_argument("--status", action="store_true", help="Just show sync status")
    args = parser.parse_args()

    if args.status:
        summary = get_sync_summary()
        print(f"\nSync Health Summary:")
        print(f"  Total sources: {summary['total_sources']}")
        print(f"  Healthy: {summary['healthy']}")
        print(f"  Stale: {summary['stale']} {summary['stale_sources']}")
        print(f"  Failed: {summary['failed']} {summary['failed_sources']}")
        print(f"  Never run: {summary['never_run']} {summary['never_run_sources']}")
        print(f"  All healthy: {summary['all_healthy']}")
        return 0 if summary['all_healthy'] else 1

    sources = [args.source] if args.source else None

    # Require --execute for actual syncs (safety measure)
    dry_run = args.dry_run or not args.execute
    if not args.execute and not args.dry_run:
        logger.info("Note: Running in dry-run mode. Use --execute to actually run syncs.")

    result = run_all_syncs(sources=sources, dry_run=dry_run, force=args.force)

    # Exit with error if any sync failed
    if result["failed"] > 0:
        sys.exit(1)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
