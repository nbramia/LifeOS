#!/usr/bin/env python3
"""
Run syncs that require Full Disk Access (phone calls, iMessage).

This script is called by run_sync_with_fda.sh which runs via cron at 2:50 AM,
10 minutes before the main sync. It uses Terminal.app's FDA permission to
access CallHistoryDB and chat.db.

The script records sync status in the same health database as run_all_syncs.py,
so the main sync can detect these were recently run and skip them.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import logging
import os
import subprocess
from datetime import datetime

from api.services.sync_health import (
    SyncStatus,
    record_sync_start,
    record_sync_complete,
    record_sync_error,
)

# Configure logging to match main sync format
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"fda_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# FDA-required syncs: these need Terminal.app's Full Disk Access
FDA_SYNCS = [
    ("phone", "scripts/sync_phone_calls.py"),
    ("imessage", "scripts/sync_imessage_interactions.py"),
]


def run_fda_sync(source: str, script_path: str) -> tuple[bool, dict]:
    """Run a single FDA-required sync with health tracking."""
    full_path = Path(__file__).parent.parent / script_path

    if not full_path.exists():
        logger.error(f"Script not found: {full_path}")
        return False, {"error": f"Script not found: {script_path}"}

    run_id = record_sync_start(source)
    logger.info(f"Started sync for {source} (run_id={run_id})")

    try:
        # Use the same Python that's running this script
        # This ensures child scripts use the correct venv (e.g., ~/.venvs/lifeos)
        cmd = [sys.executable, str(full_path), "--execute"]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout per sync
            cwd=str(Path(__file__).parent.parent),
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parent.parent),
            }
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"Sync failed for {source}: {error_msg}")

            record_sync_complete(run_id, SyncStatus.FAILED, error_message=error_msg[:500])
            record_sync_error(source, error_msg[:1000], error_type="fda_sync_error")

            return False, {"error": error_msg}

        # Parse basic stats from output
        stats = {"processed": 0, "created": 0, "updated": 0}
        for line in result.stdout.split('\n'):
            if 'synced' in line.lower() or 'indexed' in line.lower():
                logger.info(f"  {line.strip()}")

        logger.info(f"Sync completed for {source}")
        record_sync_complete(run_id, SyncStatus.SUCCESS)

        return True, stats

    except subprocess.TimeoutExpired:
        error_msg = f"Sync timed out after 5 minutes"
        logger.error(f"{source}: {error_msg}")
        record_sync_complete(run_id, SyncStatus.FAILED, error_message=error_msg)
        return False, {"error": error_msg}

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Sync failed for {source}: {error_msg}")
        record_sync_complete(run_id, SyncStatus.FAILED, error_message=error_msg[:500])
        return False, {"error": error_msg}


def main():
    """Run all FDA-required syncs."""
    logger.info("=" * 60)
    logger.info("FDA SYNC STARTED")
    logger.info(f"Log file: {log_file}")
    logger.info("=" * 60)

    results = {}
    failed = []

    for source, script_path in FDA_SYNCS:
        success, stats = run_fda_sync(source, script_path)
        results[source] = {"success": success, **stats}
        if not success:
            failed.append(source)

    logger.info("=" * 60)
    logger.info("FDA SYNC COMPLETE")
    logger.info(f"Succeeded: {len(FDA_SYNCS) - len(failed)}")
    logger.info(f"Failed: {len(failed)}")
    if failed:
        logger.error(f"Failed sources: {', '.join(failed)}")
    logger.info("=" * 60)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
