#!/usr/bin/env python3
"""
Index changed vault notes every 15 minutes: ordinary notes first, each newest first.

Runs from lifeos-vault-index.timer, outside the API server. It shares the
incremental reindex and its saved state with the nightly run, up to a per-run
cap (LIFEOS_VAULT_INDEX_MAX_FILES). LLM summaries are queued for the nightly run.

A run ends on its own after LIFEOS_VAULT_INDEX_TIME_BUDGET_SECONDS, between notes,
well inside the unit's timeout. Notes over LIFEOS_VAULT_INDEX_MAX_NOTE_BYTES are
left for the nightly reindex, which has the time for them; the job logs how many.

A deploy that finds the sync lock held leaves a marker (see DEPLOY_PENDING_NAME).
While it is fresh the job starts no run, and a run in progress ends between
notes, so the lock is free for the deploy's next attempt.

The run exits 0 without indexing when there is nothing to do, when the nightly
vault reindex or a deploy restart holds its lock, when a deploy is waiting, or
when the host lacks memory for embeddings. It never stops the LLM service to make room. A failure is left
in logs/vault-index.log and the unit's status, and the next tick tries again: it
sends no alert and records no sync failure.

Usage:
    vault_index_job.py --dry-run     Report what would be indexed, writing nothing (default)
    vault_index_job.py --execute     Index
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
import sqlite3
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Fixed host-wide path shared with run_all_syncs.py and auto-deploy.sh. The job
# holds it shared, as a sync does, so a deploy does not restart services under it.
SYNC_LOCK_PATH = Path.home() / ".lifeos" / "sync.lock"

# Beside the sync lock. auto-deploy.sh touches it each time it defers because the
# lock is held, and removes it once it holds the lock. A marker older than this
# is ignored, so a deploy that has stopped trying (disabled, host down) does not
# keep the job away. Three deploy intervals (lifeos-autodeploy.timer: 10 min).
DEPLOY_PENDING_NAME = "deploy-pending"
DEPLOY_PENDING_MAX_AGE_SECONDS = 1800

# Suppresses CRITICAL alerts raised inside this process only: the API server keeps
# its own registry and goes on alerting. Longer than the unit's TimeoutStartSec
# allows a run.
ALERT_SUPPRESSION_SECONDS = 3600

DRY_RUN_LISTED_FILES = 20


def _deploy_pending() -> bool:
    """Whether a deploy has recently found the sync lock held and is waiting."""
    try:
        age = time.time() - SYNC_LOCK_PATH.with_name(DEPLOY_PENDING_NAME).stat().st_mtime
    except OSError:
        return False
    return age < DEPLOY_PENDING_MAX_AGE_SECONDS


def _memory_shortfall() -> str | None:
    """Why the host cannot take embedding work now, or None if it can.

    A host with no reading (no AMD GPU, no /proc) is not held back, since it
    would otherwise never index.
    """
    from api.services.gpu_memory import (
        EMBEDDING_MEMORY_THRESHOLD_MB,
        EMBEDDING_RAM_THRESHOLD_MB,
        get_available_gpu_memory_mb,
        get_available_system_ram_mb,
    )

    gpu = get_available_gpu_memory_mb()
    if gpu is not None and gpu < EMBEDDING_MEMORY_THRESHOLD_MB:
        return f"{gpu} MB GPU memory free, embeddings need {EMBEDDING_MEMORY_THRESHOLD_MB} MB"
    ram = get_available_system_ram_mb()
    if ram is not None and ram < EMBEDDING_RAM_THRESHOLD_MB:
        return f"{ram} MB system RAM free, embeddings need {EMBEDDING_RAM_THRESHOLD_MB} MB"
    return None


def _log_skipped_large(count: int, max_note_bytes: int) -> None:
    if count:
        logger.info(f"Left {count} changed notes over {max_note_bytes} bytes for the nightly reindex")


def _report_dry_run(vault_path: Path, cap: int, max_deletions: int, max_note_bytes: int) -> dict:
    from api.services.indexer import IndexerService
    from api.services.vault_tag_store import VaultTagStore, get_vault_tags_db_path

    # The state path follows the working directory and the tag database follows
    # settings.chroma_path, so a run from another checkout can read mismatched
    # files. Naming both makes that visible.
    state_path = Path(IndexerService.INDEX_STATE_FILE).resolve()
    tag_db_path = get_vault_tags_db_path(create_dir=False)
    logger.info(f"Index state: {state_path}{'' if state_path.exists() else ' (not found: every note counts as changed)'}")
    try:
        tag_store = VaultTagStore(read_only=True)
        logger.info(f"Tag store: {tag_db_path}")
    except sqlite3.Error as e:
        tag_store = None
        logger.warning(f"Tag store unreadable at {tag_db_path} ({e}); counting by modification time only, so notes whose tags changed are missed")

    scanner = IndexerService(vault_path=vault_path, connect_stores=False, tag_store=tag_store)
    changes = scanner.scan_changes(compare_tags=tag_store is not None, max_file_bytes=max_note_bytes)
    to_index = changes["to_index"]
    logger.info(
        f"DRY RUN - {changes['file_count']} notes in vault, {len(to_index)} changed, "
        f"{len(changes['deleted'])} deleted; a run would index {min(cap, len(to_index))} (cap {cap}) "
        f"and remove {min(max_deletions, len(changes['deleted']))} (cap {max_deletions})"
    )
    _log_skipped_large(changes["skipped_large"], max_note_bytes)
    for file_path in to_index[:min(cap, DRY_RUN_LISTED_FILES)]:
        logger.info(f"  would index: {file_path}")
    for file_path in changes["deleted"][:DRY_RUN_LISTED_FILES]:
        logger.info(f"  would remove: {file_path}")
    shortfall = _memory_shortfall()
    if shortfall:
        logger.info(f"  a run would stand aside right now: {shortfall}")
    return {
        "status": "dry_run",
        "changed": len(to_index),
        "deleted": len(changes["deleted"]),
        "would_index": min(cap, len(to_index)),
        "skipped_large": changes["skipped_large"],
    }


def run_job(dry_run: bool = True, max_files: int | None = None) -> dict:
    """Run one tick of the job.

    Args:
        dry_run: If True, report what would happen and write nothing
        max_files: Per-run cap (default: settings.vault_index_max_files)

    Returns:
        Stats dict with a ``status`` of indexed, nothing_to_do, skipped, dry_run or error
    """
    from api.services.indexer import IndexerService
    from api.services.vault_index_lock import VAULT_INDEX_LOCK_PATH, try_flock
    from config.settings import settings

    vault_path = settings.vault_path
    cap = settings.vault_index_max_files if max_files is None else max_files

    if not Path(vault_path).exists():
        logger.error(f"Vault path not found: {vault_path}")
        return {"status": "error", "reason": "vault_not_found"}

    max_deletions = settings.vault_index_max_deletions
    max_note_bytes = settings.vault_index_max_note_bytes
    time_budget = settings.vault_index_time_budget_seconds or None

    if dry_run:
        return _report_dry_run(vault_path, cap, max_deletions, max_note_bytes)

    if _deploy_pending():
        logger.info("A deploy is waiting for the sync lock; trying again next tick")
        return {"status": "skipped", "reason": "deploy_pending"}

    # Held for the rest of this call. A crash releases them, since the kernel
    # drops a flock when its process exits.
    vault_lock = try_flock(VAULT_INDEX_LOCK_PATH)
    if vault_lock is None:
        logger.info("Vault reindex holds the vault index lock; trying again next tick")
        return {"status": "skipped", "reason": "vault_reindex_running"}
    sync_lock = try_flock(SYNC_LOCK_PATH, shared=True)
    if sync_lock is None:
        logger.info("A deploy restart holds the sync lock; trying again next tick")
        return {"status": "skipped", "reason": "deploy_running"}

    # Decided before the indexer exists, so an idle tick stays this cheap.
    changes = IndexerService(vault_path=vault_path, connect_stores=False).scan_changes(max_file_bytes=max_note_bytes)
    to_index, deleted, skipped_large = changes["to_index"], changes["deleted"], changes["skipped_large"]
    _log_skipped_large(skipped_large, max_note_bytes)

    if not to_index and not deleted:
        logger.info("Nothing to index")
        return {"status": "nothing_to_do", "files_indexed": 0, "skipped_large": skipped_large}

    if changes["file_count"] == 0:
        # An unmounted or emptied vault looks like every note was deleted.
        logger.warning(f"Vault has no notes but {len(deleted)} are indexed; not removing them")
        return {"status": "skipped", "reason": "vault_empty"}

    if to_index:
        shortfall = _memory_shortfall()
        if shortfall:
            logger.info(f"Standing aside for {len(to_index)} changed notes, LLM left running: {shortfall}")
            return {"status": "skipped", "reason": "insufficient_memory"}

    from api.services.service_health import enter_maintenance
    enter_maintenance(ALERT_SUPPRESSION_SECONDS)

    start_time = time.time()
    indexer = IndexerService(vault_path=vault_path)
    files_indexed = indexer.index_all(
        max_files=cap,
        newest_first=True,
        light_pass=True,
        max_deletions=max_deletions,
        time_budget_seconds=time_budget,
        max_file_bytes=max_note_bytes,
        should_stop=_deploy_pending,
    )
    elapsed = time.time() - start_time

    logger.info(
        f"Indexed {files_indexed} of {len(to_index)} changed notes and removed "
        f"{indexer.last_run['files_pruned']} of {len(deleted)} deleted, in {elapsed:.1f}s"
    )
    from api.services.embeddings import get_embedding_service
    if get_embedding_service().using_cpu_fallback:
        logger.warning(f"This run embedded on the CPU, not the GPU: {files_indexed} notes in {elapsed:.1f}s")
    return {
        "status": "indexed",
        "files_indexed": files_indexed,
        "changed": len(to_index),
        "skipped_large": skipped_large,
        "deleted": len(deleted),
        "elapsed_seconds": round(elapsed, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Index changed vault notes: ordinary notes first, each newest first')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='Actually index')
    mode.add_argument('--dry-run', action='store_true', help='Report what would be indexed and write nothing (default)')
    parser.add_argument('--max-files', type=int, help='Per-run cap (default: LIFEOS_VAULT_INDEX_MAX_FILES)')
    args = parser.parse_args(argv)

    try:
        result = run_job(dry_run=not args.execute, max_files=args.max_files)
    except Exception:
        logger.exception("Vault index job failed; trying again next tick")
        return 1
    return 1 if result["status"] == "error" else 0


if __name__ == '__main__':
    # .env wins over inherited variables, as in run_all_syncs.py. Before any
    # api.* import, because some read the environment when imported.
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=True)
    sys.exit(main())
