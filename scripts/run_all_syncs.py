#!/usr/bin/env python3
"""
Run all CRM data source syncs with health monitoring.

This script should be run daily via launchd or cron. It:
1. Syncs all configured data sources
2. Records sync status and errors in sync_health.db
3. Logs all output for debugging
4. Sends Telegram notification with sync summary
5. Exits with non-zero status if any critical sync fails

Usage:
    python scripts/run_all_syncs.py [--source SOURCE] [--dry-run] [--force] [--trigger TYPE]

Options:
    --source SOURCE   Run only this specific source
    --dry-run         Don't actually sync, just report what would run
    --force           Run even if sync was run recently
    --trigger TYPE    How sync was triggered: scheduled (default), manual, startup
"""
# Load environment variables from .env FIRST, before any other imports
# This is critical for launchd/cron which don't have access to shell environment
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
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
    reap_orphan_sync_runs,
    detect_silent_source_entity_drift,
)
from config.settings import settings

# =============================================================================
# LLM Service Management (memory-gated stop/start for embedding phases)
# =============================================================================

# Embedding phases that need GPU memory — LLM may need to yield
EMBEDDING_SOURCES = {"vault_reindex", "crm_vectorstore"}

# Minimum free GPU memory (MB) required to run embeddings alongside the LLM.
# Modern sentence-transformer embedding models can peak at 15-22 GB transient
# allocations during forward pass on long sequences, well beyond the model's
# resident weights (3-4 GB). If less than this is free, we free the LLM
# rather than risk a HIP/CUDA OOM that falls back to CPU (~10x slower, OOM-risky).
#
# Override with LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB if your embedding model
# / batch size has a different working-set ceiling.
_EMBEDDING_MEMORY_THRESHOLD_MB = int(
    __import__("os").environ.get("LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB", "28000")
)

# Minimum free system RAM (MB) required to run embedding phases.
# Model loading temporarily needs ~4 GB system RAM even when targeting GPU.
# Below this threshold, embedding phases are skipped to avoid OOM-killing
# other processes (Chrome, Claude Code, etc.).
_EMBEDDING_RAM_THRESHOLD_MB = 4_000


def _ollama_host() -> str:
    """Return the configured ollama host, falling back to the standard default."""
    return getattr(settings, "ollama_host", None) or "http://localhost:11434"


def _ollama_loaded_models() -> list[str]:
    """Return names of models currently loaded in ollama VRAM, or [] on error."""
    try:
        req = urllib.request.Request(f"{_ollama_host()}/api/ps")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", []) if m.get("size_vram", 0) > 0]
    except Exception:
        return []


def _ollama_unload_all() -> list[str]:
    """Unload all loaded ollama models from VRAM. Returns list of unloaded model names."""
    logger = logging.getLogger(__name__)
    unloaded: list[str] = []
    for model in _ollama_loaded_models():
        try:
            payload = json.dumps({"model": model, "keep_alive": 0}).encode()
            req = urllib.request.Request(
                f"{_ollama_host()}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30):
                pass
            logger.info(f"Unloaded ollama model from VRAM: {model}")
            unloaded.append(model)
        except Exception as e:
            logger.warning(f"Failed to unload ollama model {model}: {e}")
    return unloaded


def _ollama_warm(model: str) -> bool:
    """Send a tiny request to reload `model` into VRAM. Returns True on success."""
    logger = logging.getLogger(__name__)
    try:
        payload = json.dumps({"model": model, "prompt": "", "keep_alive": -1}).encode()
        req = urllib.request.Request(
            f"{_ollama_host()}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120):
            pass
        logger.info(f"Re-warmed ollama model: {model}")
        return True
    except Exception as e:
        logger.warning(f"Failed to warm ollama model {model}: {e}")
        return False


def _is_llm_running() -> bool:
    """Detect any GPU-resident process that would compete with embeddings.

    Source-agnostic: any process holding most of the VRAM counts — whether it's
    ollama, llama-server (lifeos-llm.service), or another model. If VRAM
    detection is unavailable, returns False (nothing we can free).

    Note: `_stop_llm_for_embeddings` can only unload ollama-held models. If a
    non-ollama LLM (e.g. llama-server) is pinning VRAM, the orchestrator's
    post-stop check will see VRAM is still pinned and the embedding sources
    will fall back to CPU (existing behavior).
    """
    available = _get_available_gpu_memory_mb()
    if available is None:
        return False
    return available < _EMBEDDING_MEMORY_THRESHOLD_MB


def _get_available_gpu_memory_mb() -> int | None:
    """Get available GPU memory in MB from AMDGPU sysfs, or None if unavailable."""
    try:
        # AMDGPU exposes VRAM info via sysfs
        for card_dir in Path("/sys/class/drm").iterdir():
            vram_total = card_dir / "device" / "mem_info_vram_total"
            vram_used = card_dir / "device" / "mem_info_vram_used"
            if vram_total.exists() and vram_used.exists():
                total = int(vram_total.read_text().strip())
                used = int(vram_used.read_text().strip())
                return (total - used) // (1024 * 1024)
    except Exception:
        pass
    return None


def _get_available_system_ram_mb() -> int | None:
    """Get available system RAM in MB from /proc/meminfo, or None if unavailable."""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                # MemAvailable is in kB
                kb = int(line.split()[1])
                return kb // 1024
    except Exception:
        pass
    return None


# Track ollama models we unloaded so _start_llm can re-warm them
_ollama_models_unloaded: list[str] = []


def _stop_llm_for_embeddings() -> bool:
    """Free GPU memory for embeddings by unloading any ollama-held models.

    Returns True if at least one model was unloaded.
    """
    global _ollama_models_unloaded
    logger = logging.getLogger(__name__)

    # Unload any ollama-loaded models. With OLLAMA_KEEP_ALIVE=-1 set
    # service-wide, these otherwise stay pinned in VRAM and prevent
    # embeddings from allocating their working set.
    unloaded = _ollama_unload_all()
    if not unloaded:
        return False
    _ollama_models_unloaded = unloaded

    # Wait for VRAM to actually be freed
    for _ in range(10):
        time.sleep(2)
        available = _get_available_gpu_memory_mb()
        if available is not None and available >= _EMBEDDING_MEMORY_THRESHOLD_MB:
            logger.info(f"GPU memory freed: {available} MB available")
            return True
    logger.warning("Ollama models unloaded but GPU memory not fully freed within timeout")
    return True


def _start_llm() -> None:
    """Restore LLM state after embedding phases — re-warm any ollama models we unloaded."""
    global _ollama_models_unloaded

    # Re-warm exactly the ollama models we unloaded — we restore whatever
    # was actually pinned in VRAM before the sync, not whatever
    # settings.ollama_model currently points to.
    for model in _ollama_models_unloaded:
        _ollama_warm(model)
    _ollama_models_unloaded = []


# Track current sync run for SIGTERM cleanup
_active_run_id: int | None = None
_active_source: str | None = None

# Track LLM state across signal handlers (module-level for SIGTERM access)
_llm_stopped_for_sync: bool = False
_llm_was_running_before_sync: bool = False


def _handle_sigterm(signum, frame):
    """Clean up sync_health.db on SIGTERM so we don't leave stale RUNNING rows."""
    if _active_run_id is not None:
        try:
            record_sync_complete(
                _active_run_id,
                SyncStatus.FAILED,
                error_message=f"Process killed by signal {signum} during {_active_source}",
            )
        except Exception:
            pass  # Best-effort — DB may be locked
    # Restore LLM if we stopped it
    if _llm_stopped_for_sync and _llm_was_running_before_sync:
        _start_llm()
    sys.exit(128 + signum)


signal.signal(signal.SIGTERM, _handle_sigterm)

# Markdown error log in Notes directory (for visibility)
NOTES_ERROR_LOG = settings.vault_path / "LifeOS" / "sync_errors.md"


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


def log_sync_summary_to_markdown(result: dict, trigger: str = "unknown"):
    """
    Log a sync run summary to the markdown file.

    Always logs to provide visibility into sync history.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Format duration
    duration_secs = result.get("duration_seconds", 0)
    if duration_secs:
        mins, secs = divmod(int(duration_secs), 60)
        duration_str = f"{mins}m {secs}s"
    else:
        duration_str = "N/A"

    lines = [
        f"## {timestamp} - SYNC SUMMARY ({trigger})",
        "",
        f"**Status:** {result['succeeded']}/{result['sources_run']} succeeded, {result['failed']} failed",
        f"**Duration:** {duration_str}",
    ]

    # Failed sources
    if result["failed"] > 0:
        lines.append("")
        lines.append("**Failed:**")
        for src in result.get("failed_sources", []):
            error = result.get("results", {}).get(src, {}).get("error", "unknown error")
            # Truncate error for readability
            error_short = error[:80] + "..." if len(error) > 80 else error
            lines.append(f"- {src}: {error_short}")

    # Dependency-skipped sources
    dep_skipped_sources = result.get("dep_skipped_sources", [])
    if dep_skipped_sources:
        lines.append("")
        lines.append(f"**Skipped — dependency failed ({len(dep_skipped_sources)}):**")
        for src in dep_skipped_sources:
            failed_deps = result.get("results", {}).get(src, {}).get("failed_dependencies", [])
            lines.append(f"- {src} (needs: {', '.join(failed_deps)})")

    # New records summary
    people_created = result.get("people_created", 0)
    interactions_created = result.get("interactions_created", 0)

    if people_created > 0 or interactions_created > 0:
        lines.append("")
        lines.append("**New Records:**")
        if people_created > 0:
            people_by_src = result.get("people_by_source", {})
            src_details = ", ".join(f"{s}: {c}" for s, c in people_by_src.items() if c > 0)
            lines.append(f"- People: {people_created}" + (f" ({src_details})" if src_details else ""))
        if interactions_created > 0:
            interactions_by_src = result.get("interactions_by_source", {})
            src_details = ", ".join(f"{s}: {c}" for s, c in interactions_by_src.items() if c > 0)
            lines.append(f"- Interactions: {interactions_created}" + (f" ({src_details})" if src_details else ""))

    lines.append("")
    lines.append("---")

    entry = "\n" + "\n".join(lines) + "\n"
    _write_to_markdown_log(entry)


def send_sync_summary_telegram(result: dict, trigger: str = "unknown"):
    """
    Send formatted sync summary to Telegram after sync completes.

    Sends for all sync runs (manual and scheduled) with categorized stats.
    """
    from api.services.telegram import send_message

    # Format duration
    duration_secs = result.get("duration_seconds", 0)
    if duration_secs:
        mins, secs = divmod(int(duration_secs), 60)
        duration_str = f"{mins}m {secs}s"
    else:
        duration_str = "N/A"

    # Build message
    status_emoji = "✅" if result["failed"] == 0 else "⚠️"
    lines = [
        f"{status_emoji} *LifeOS Sync Complete*",
        f"Trigger: {trigger}",
        f"Status: {result['succeeded']}/{result['sources_run']} succeeded",
        f"Duration: {duration_str}",
    ]

    # Failed sources
    if result["failed"] > 0:
        lines.append("")
        lines.append(f"*Failed ({result['failed']}):*")
        for src in result.get("failed_sources", []):
            lines.append(f"  • {src}")

    # Dependency-skipped sources
    dep_skipped_sources = result.get("dep_skipped_sources", [])
    if dep_skipped_sources:
        lines.append("")
        lines.append(f"*Skipped — dependency failed ({len(dep_skipped_sources)}):*")
        for src in dep_skipped_sources:
            failed_deps = result.get("results", {}).get(src, {}).get("failed_dependencies", [])
            lines.append(f"  • {src} (needs: {', '.join(failed_deps)})")

    # New records summary
    people_created = result.get("people_created", 0)
    interactions_created = result.get("interactions_created", 0)

    if people_created > 0 or interactions_created > 0:
        lines.append("")
        if people_created > 0:
            people_by_src = result.get("people_by_source", {})
            lines.append(f"*New People:* {people_created}")
            for src, count in people_by_src.items():
                if count > 0:
                    lines.append(f"  • {src}: {count}")

        if interactions_created > 0:
            interactions_by_src = result.get("interactions_by_source", {})
            lines.append(f"*New Interactions:* {interactions_created}")
            for src, count in interactions_by_src.items():
                if count > 0:
                    lines.append(f"  • {src}: {count}")

    # Consistency verification results
    consistency = result.get("results", {}).get("consistency_verify", {})
    if consistency.get("success") and not consistency.get("dry_run"):
        total_issues = consistency.get("total_issues", 0)
        total_fixed = consistency.get("total_fixed", 0)
        if total_issues > 0:
            lines.append("")
            if consistency.get("auto_fix_skipped"):
                lines.append(f"⚠️ *Data Issues:* {total_issues} found, {total_fixed} auto-fixed")
                lines.append("  Manual review needed — threshold exceeded")
            else:
                lines.append(f"🔧 *Data Issues:* {total_issues} found, {total_fixed} auto-fixed")

    try:
        success = send_message("\n".join(lines))
        if success:
            logger.info("Sync summary sent to Telegram")
        else:
            logger.warning("Failed to send sync summary to Telegram")
    except Exception as e:
        logger.warning(f"Error sending sync summary to Telegram: {e}")


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
    "gmail_personal",            # Personal Gmail sent + received + CC
    "gmail_work",               # Work Gmail sent + received + CC
    "gmail_work2",              # Second work Gmail sent + received + CC
    "calendar_personal",        # Personal Google Calendar events
    "calendar_work",            # Work Google Calendar events
    "calendar_work2",           # Second work Google Calendar events
    "linkedin",                 # LinkedIn connections CSV
    "contacts",                 # Apple Contacts (native via pyobjc, or imported from Mac Mini)
    # NOTE: phone and imessage are NOT in this list on macOS - they require Full Disk Access
    # which launchd doesn't have. They run via cron at 2:50 AM through Terminal.app:
    #   - scripts/run_sync_with_fda.sh (cron entry, opens Terminal)
    #   - scripts/run_fda_syncs.py (actual sync runner with health tracking)
    # Cron schedule: 50 2 * * * /path/to/run_sync_with_fda.sh
    #
    # On Linux, Apple data is imported from Mac Mini exports.
    # WhatsApp is also bundled into the apple_import step (the Mac Mini runs
    # wacli and rsyncs whatsapp.json alongside the other exports).
    "apple_import",             # Import Apple data + WhatsApp from Mac Mini (Linux only)
    "slack",                    # Slack users + DM messages

    # === Phase 2: Entity Processing ===
    # Link source entities to canonical PersonEntity records
    "link_slack",               # Link Slack users to people by email
    "link_imessage",            # Link iMessage handles to people by phone
    "imessage",                 # Create interactions from linked iMessage data
    "link_source_entities",     # Retroactive linking for all unlinked entities
    "photos",                   # Sync Photos face data to people

    # === Phase 2b: Stale ID Cleanup ===
    # Re-point interactions with stale merged person IDs BEFORE relationship building
    "repoint_stale_ids",        # Fix interactions pointing to old merged person IDs

    # === Phase 3: Relationship Building ===
    # Build relationships using all collected interaction data
    # Each sync script refreshes its own affected stats, but we do a full
    # refresh here to catch anything missed (photos, edge cases, timestamps)
    "person_stats_full",        # Full refresh of all PersonEntity counts + timestamps
    "relationship_discovery",   # Discover relationships, populate edge weights
    "strengths",                # Calculate relationship strength scores
    "push_birthdays",           # Push LifeOS birthdays to Apple Contacts

    # === Phase 4: Vector Store Indexing ===
    # Index content with fresh people data available for entity resolution
    "vault_reindex",            # Full reindex with LLM summaries (no timeout)
    "crm_vectorstore",          # Index CRM people for semantic search

    # === Phase 5: Content Sync ===
    # Pull external content into vault (will be indexed on next run)
    "google_docs",              # Sync Google Docs to vault as markdown
    "google_sheets",            # Sync Google Sheets to vault as markdown
    "monarch_money",            # Monthly financial summary (runs on 1st only)

    # === Phase 6: Post-Sync Cleanup ===
    # Clean up entity data quality issues after all other syncs
    "entity_cleanup",           # Auto-hide non-humans, queue duplicates for review

    # === Phase 7: Consistency Verification ===
    # Verify cross-store data consistency after all syncs complete
    "consistency_verify",       # Check orphans, stale merged IDs, cached counts
]

# Scripts that can be run directly
SYNC_SCRIPTS = {
    # Phase 1: Data Collection
    "gmail_personal": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--gmail-only", "--account", "personal", "--days", "30"]),
    "gmail_work": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--gmail-only", "--account", "work", "--days", "30"]),
    "gmail_work2": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--gmail-only", "--account", "work2", "--days", "30"]),
    "calendar_personal": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--calendar-only", "--account", "personal", "--days", "30"]),
    "calendar_work": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--calendar-only", "--account", "work", "--days", "30"]),
    "calendar_work2": ("scripts/sync_gmail_calendar_interactions.py", ["--execute", "--calendar-only", "--account", "work2", "--days", "30"]),
    "linkedin": ("scripts/sync_linkedin.py", ["--execute"]),
    "contacts": ("scripts/sync_apple_contacts.py", ["--execute"]),
    "apple_import": ("scripts/apple_data_import.py", ["--execute"]),
    "phone": ("scripts/sync_phone_calls.py", ["--execute"]),
    "imessage": ("scripts/sync_imessage_interactions.py", ["--execute"]),
    "slack": ("scripts/sync_slack.py", ["--execute"]),

    # Phase 2: Entity Processing
    "link_slack": ("scripts/link_slack_entities.py", ["--execute"]),
    "link_imessage": ("scripts/link_imessage_entities.py", ["--execute"]),
    "link_source_entities": ("scripts/link_source_entities.py", ["--execute"]),
    "photos": ("scripts/sync_photos.py", ["--execute"]),

    # Phase 2b: Stale ID Cleanup
    "repoint_stale_ids": ("scripts/sync_repoint_stale_ids.py", ["--execute"]),

    # Phase 3: Relationship Building
    "person_stats_full": ("scripts/sync_person_stats.py", ["--full", "--execute"]),
    "relationship_discovery": ("scripts/sync_relationship_discovery.py", ["--execute"]),
    "strengths": ("scripts/sync_strengths.py", ["--execute"]),
    "push_birthdays": ("scripts/push_birthdays_to_contacts.py", ["--execute"]),

    # Phase 4: Vector Store Indexing
    "vault_reindex": ("scripts/sync_vault_reindex.py", ["--execute"]),
    "crm_vectorstore": ("scripts/sync_crm_to_vectorstore.py", ["--execute"]),

    # Phase 5: Content Sync
    "google_docs": ("scripts/sync_google_docs.py", ["--execute"]),
    "google_sheets": ("scripts/sync_google_sheets.py", ["--execute"]),
    "monarch_money": ("scripts/sync_monarch_money.py", ["--execute"]),

    # Phase 6: Post-Sync Cleanup
    "entity_cleanup": ("scripts/sync_entity_cleanup.py", ["--execute"]),

    # Phase 7: Consistency Verification
    "consistency_verify": ("scripts/sync_consistency_verify.py", ["--execute"]),
}

# Per-source timeout overrides (seconds)
# Default is 60 minutes (3600).
DEFAULT_SYNC_TIMEOUT = 3600  # 60 minutes

SYNC_TIMEOUTS = {
    "vault_reindex": 14400,          # 4 hours - incremental is typically 10-30min, but a
                                     #            `--force` full reindex of a ~6K-file vault
                                     #            plus per-file summary calls fits in ~3-4h
    "slack": 7200,                   # 2 hours - ~100 linked DMs + group DMs, rate-limited
    "google_docs": 300,              # 5 minutes - normally takes ~9s, hangs on expired OAuth
    "google_sheets": 300,            # 5 minutes - normally takes ~1s, hangs on expired OAuth
}


def get_disabled_work_sources() -> set[str]:
    """
    Return set of sources that should be skipped because work integrations are disabled.

    Work integrations are disabled by default for safety - work data will only be
    synced if explicitly enabled via environment variables.
    """
    disabled = set()

    has_work_domain = bool(settings.work_email_domain)

    if not settings.sync_work_gmail or not has_work_domain:
        disabled.add("gmail_work")

    if not settings.sync_work_calendar or not has_work_domain:
        disabled.add("calendar_work")

    has_work2_domain = bool(settings.work_email_domain_2)

    if not settings.sync_work2_gmail or not has_work2_domain:
        disabled.add("gmail_work2")

    if not settings.sync_work2_calendar or not has_work2_domain:
        disabled.add("calendar_work2")

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

    # Record sync start — track globally for SIGTERM cleanup.
    # Mask SIGTERM briefly so a signal can't arrive between the DB insert
    # and the global assignment, which would leave a stale RUNNING row.
    global _active_run_id, _active_source
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    run_id = record_sync_start(source)
    _active_run_id = run_id
    _active_source = source
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        logger.info(f"Starting sync for {source}...")

        # Build command - use the same Python that's running this script
        # This ensures child scripts use the correct venv (e.g., ~/.venvs/lifeos)
        cmd = [sys.executable, str(full_path)] + args

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

        # Parse output for stats (check both stdout and stderr — many scripts
        # log stats via Python's logging module which defaults to stderr)
        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
        stats = _parse_sync_output(combined_output)

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
                people_created=stats.get("people_created", 0),
                people_updated=stats.get("people_updated", 0),
                interactions_created=stats.get("interactions_created", 0),
                source_entities_created=stats.get("source_entities_created", 0),
            )
            _active_run_id = None

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
            people_created=stats.get("people_created", 0),
            people_updated=stats.get("people_updated", 0),
            interactions_created=stats.get("interactions_created", 0),
            source_entities_created=stats.get("source_entities_created", 0),
        )
        _active_run_id = None

        return True, stats

    except subprocess.TimeoutExpired as e:
        timeout_minutes = SYNC_TIMEOUTS.get(source, DEFAULT_SYNC_TIMEOUT) // 60

        # Capture partial output from the killed process.
        # NOTE: TimeoutExpired.stdout/.stderr are bytes even when subprocess.run
        # was called with text=True (CPython quirk), so decode defensively.
        def _decode(buf):
            if buf is None:
                return ""
            if isinstance(buf, bytes):
                return buf.decode("utf-8", errors="replace")
            return buf

        partial_stdout = _decode(e.stdout)
        partial_stderr = _decode(e.stderr)
        combined_partial = partial_stdout + "\n" + partial_stderr

        # Parse what was accomplished before timeout
        stats = _parse_sync_output(combined_partial)

        # Build error message with partial progress info
        error_msg = f"Sync timed out after {timeout_minutes} minutes"
        if stats.get("processed", 0) > 0 or stats.get("created", 0) > 0:
            error_msg += f" (partial progress: {stats.get('processed', 0)} processed, {stats.get('created', 0)} created)"

        logger.error(f"Sync timeout for {source}")
        if combined_partial.strip():
            # Log last 50 lines of output to see progress (most scripts use
            # Python logging which goes to stderr, so check both streams)
            last_lines = "\n".join(combined_partial.strip().split("\n")[-50:])
            logger.info(f"Partial output before timeout:\n{last_lines}")

        record_sync_complete(
            run_id,
            SyncStatus.FAILED,
            records_processed=stats.get("processed", 0),
            records_created=stats.get("created", 0),
            records_updated=stats.get("updated", 0),
            errors=1,
            error_message=error_msg,
            people_created=stats.get("people_created", 0),
            people_updated=stats.get("people_updated", 0),
            interactions_created=stats.get("interactions_created", 0),
            source_entities_created=stats.get("source_entities_created", 0),
        )
        _active_run_id = None

        # Include partial output in markdown log for visibility
        full_error_msg = error_msg
        if combined_partial.strip():
            full_error_msg += f"\n\nLast output before timeout:\n{combined_partial[-2000:]}"

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
        _active_run_id = None

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

    finally:
        _active_run_id = None
        _active_source = None


def _parse_sync_output(output: str) -> dict:
    """Parse sync script output for statistics.

    Scripts can emit a single canonical line ``SYNC_STATS:{json}`` with their
    final tallies, which takes precedence over the regex fallbacks below. The
    regex fallback exists for scripts that haven't been migrated yet and for
    backwards compatibility with older log captures.
    """
    import re

    stats = {
        "processed": 0,
        "created": 0,
        "updated": 0,
        "errors": 0,
        # Categorized stats
        "people_created": 0,
        "people_updated": 0,
        "interactions_created": 0,
        "source_entities_created": 0,
    }

    # Authoritative path: a single SYNC_STATS:{json} line emitted by the
    # script. If present, treat it as ground truth and skip regex inference.
    # Last occurrence wins, so a top-level wrapper script (e.g.
    # apple_data_import.py aggregating across sub-imports) can override
    # earlier emissions.
    sync_stats_matches = re.findall(r"SYNC_STATS:(\{[^\n]*\})", output)
    if sync_stats_matches:
        try:
            parsed = json.loads(sync_stats_matches[-1])
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if isinstance(value, (int, float)):
                        stats[key] = int(value)
                # Aggregate generic counters from categorized ones for
                # backwards compatibility with downstream consumers.
                stats["created"] = max(
                    stats["created"],
                    stats["people_created"]
                    + stats["interactions_created"]
                    + stats["source_entities_created"],
                )
                stats["updated"] = max(stats["updated"], stats["people_updated"])
                # Still parse CONSISTENCY_SUMMARY below — it's orthogonal.
                consistency_match = re.search(r"CONSISTENCY_SUMMARY:(\{.*\})", output)
                if consistency_match:
                    try:
                        consistency_data = json.loads(consistency_match.group(1))
                        stats.update(consistency_data)
                    except (json.JSONDecodeError, ValueError):
                        pass
                return stats
        except (json.JSONDecodeError, ValueError):
            pass  # Fall through to regex parsing

    # Generic patterns (for backwards compatibility)
    generic_patterns = [
        (r"(\d+)\s*(?:records?|items?|entities?)\s*(?:read|processed|found)", "processed"),
        (r"(?:errors?)\s*[:\s]*(\d+)", "errors"),
    ]

    # Categorized patterns - people
    people_patterns = [
        (r"persons?[_\s]?created\s*[:\s]*(\d+)", "people_created"),
        (r"people[_\s]?created\s*[:\s]*(\d+)", "people_created"),
        (r"new\s+(?:people|persons?)\s*[:\s]*(\d+)", "people_created"),
        (r"created\s+(\d+)\s+(?:people|persons?)", "people_created"),
        (r"persons?[_\s]?updated\s*[:\s]*(\d+)", "people_updated"),
        (r"persons?[_\s]?linked\s*[:\s]*(\d+)", "people_updated"),
        (r"linked\s+(\d+)\s+(?:people|persons?)", "people_updated"),
    ]

    # Categorized patterns - interactions
    interaction_patterns = [
        (r"interactions?[_\s]?created\s*[:\s]*(\d+)", "interactions_created"),
        (r"inserted\s*[:\s]*(\d+)", "interactions_created"),
        (r"new\s+interactions?\s*[:\s]*(\d+)", "interactions_created"),
        (r"created\s+(\d+)\s+interactions?", "interactions_created"),
    ]

    # Categorized patterns - source entities
    source_entity_patterns = [
        (r"source[_\s]?entities?[_\s]?created\s*[:\s]*(\d+)", "source_entities_created"),
        (r"new\s+source[_\s]?entities?\s*[:\s]*(\d+)", "source_entities_created"),
    ]

    all_patterns = generic_patterns + people_patterns + interaction_patterns + source_entity_patterns

    for pattern, key in all_patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            stats[key] = max(stats[key], int(match.group(1)))

    # Aggregate into generic created/updated for backwards compatibility
    stats["created"] = max(
        stats["created"],
        stats["people_created"] + stats["interactions_created"] + stats["source_entities_created"]
    )
    stats["updated"] = max(stats["updated"], stats["people_updated"])

    # Parse consistency verification summary (Phase 7)
    consistency_match = re.search(r"CONSISTENCY_SUMMARY:(\{.*\})", output)
    if consistency_match:
        try:
            consistency_data = json.loads(consistency_match.group(1))
            stats.update(consistency_data)
        except (json.JSONDecodeError, ValueError):
            pass

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
    trigger: str = "scheduled",
) -> dict:
    """
    Run all syncs in order.

    Args:
        sources: List of sources to sync (default: all in SYNC_ORDER)
        dry_run: If True, don't actually run syncs
        force: If True, run even if recently synced
        trigger: How sync was triggered: scheduled, manual, or startup

    Returns:
        Summary dict with results
    """
    sources = sources or SYNC_ORDER
    results = {}
    failed = []
    dep_skipped = set()  # Sources skipped because a dependency failed
    start_time = datetime.now()

    # Check for disabled work integrations
    disabled_sources = get_disabled_work_sources()
    if disabled_sources:
        logger.info(f"Work integration sources disabled: {', '.join(sorted(disabled_sources))}")
        logger.info("Enable via LIFEOS_SYNC_SLACK=true, etc. in .env")

    logger.info(f"Sync triggered: {trigger}")
    logger.info(f"Starting sync run for {len(sources)} sources...")
    logger.info(f"Log file: {log_file}")

    # Clean up sync_runs rows left in status='running' by killed/crashed
    # processes. Otherwise they pin the dashboard's "last completed" timestamp
    # and make recently-failed sources look healthy.
    if not dry_run:
        try:
            reap_orphan_sync_runs()
        except Exception as e:
            logger.warning(f"Failed to reap orphan sync_runs: {e}")

        # Monarch session expiry check (issue #199 §3 acceptance criterion).
        # Surfaces re-auth need in the nightly log *before* the monthly
        # sync hits a 401/525. Cheap — just stats the pickle's mtime.
        try:
            from api.services.monarch import get_session_status
            mstatus = get_session_status()
            if mstatus["status"] in ("expiring_soon", "expired", "missing"):
                logger.warning(f"Monarch session: {mstatus['message']}")
        except Exception as e:
            logger.warning(f"Monarch session-status check failed: {e}")

    # Trigger Photos.app to open and start iCloud sync in background (macOS only)
    # This runs at the beginning so Photos can sync throughout the entire process
    if not dry_run and sys.platform == "darwin":
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

    # Suppress CRITICAL alerts during sync (ChromaDB may have transient SQLite issues
    # during heavy indexing). 4 hours covers even the longest full reindex.
    # Uses HTTP API since sync runs as a separate process from the API server.
    if not dry_run:
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://localhost:8000/api/admin/maintenance?duration_seconds=14400",
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info("Entered maintenance mode — CRITICAL alerts suppressed")
        except Exception as e:
            logger.warning(f"Could not enter maintenance mode (server may not be running): {e}")

    global _llm_stopped_for_sync, _llm_was_running_before_sync
    _llm_stopped_for_sync = False
    _llm_was_running_before_sync = False
    # One-shot: only check GPU memory once for the first embedding source,
    # since conditions won't change mid-sync (LLM is either stopped or not).
    llm_memory_checked = False
    _skip_embedding_phases = False
    available_ram = None

    try:
        for source_idx, source in enumerate(sources):
            if source not in SYNC_SOURCES:
                logger.warning(f"Unknown source: {source}, skipping")
                continue

            # Skip sources disabled by work integration settings
            if source in disabled_sources:
                logger.info(f"Skipping {source}: work integration disabled")
                results[source] = {"skipped": True, "reason": "work_integration_disabled"}
                continue

            # Skip monthly sources unless it's the 1st of the month (or forced)
            source_info = SYNC_SOURCES.get(source, {})
            if source_info.get("frequency") == "monthly" and not force and not dry_run:
                if datetime.now().day != 1:
                    logger.info(f"Skipping {source}: monthly sync, not the 1st (use --force to override)")
                    results[source] = {"skipped": True, "reason": "monthly_not_due"}
                    continue

            # Check if recently synced (unless forced)
            if not force and not dry_run:
                health = get_sync_health(source)
                if health.hours_since_sync is not None and health.hours_since_sync < 1:
                    if health.last_status == SyncStatus.SUCCESS:
                        logger.info(f"Skipping {source}: recently synced ({health.hours_since_sync*60:.0f}m ago)")
                        results[source] = {"skipped": True, "reason": "recently_synced"}
                        continue

            # Check if any dependency failed or was dependency-skipped
            deps = source_info.get("depends_on", [])
            if deps:
                failed_deps = [d for d in deps if d in failed or d in dep_skipped]
                if failed_deps:
                    logger.warning(f"Skipping {source}: dependency failed ({', '.join(failed_deps)})")
                    results[source] = {
                        "skipped": True,
                        "reason": "dependency_failed",
                        "failed_dependencies": failed_deps,
                    }
                    dep_skipped.add(source)
                    continue

            # Memory-gated LLM management for embedding phases (after skip guards)
            if source in EMBEDDING_SOURCES and not dry_run and not llm_memory_checked:
                llm_memory_checked = True

                # Check system RAM first — insufficient RAM causes kernel OOM kills
                available_ram = _get_available_system_ram_mb()
                if available_ram is not None and available_ram < _EMBEDDING_RAM_THRESHOLD_MB:
                    logger.warning(
                        f"Insufficient system RAM ({available_ram} MB free, need {_EMBEDDING_RAM_THRESHOLD_MB} MB) "
                        f"— skipping embedding phases to avoid OOM"
                    )
                    _skip_embedding_phases = True
                else:
                    _skip_embedding_phases = False
                    if available_ram is not None:
                        logger.info(f"System RAM check passed: {available_ram} MB available")

                _llm_was_running_before_sync = _is_llm_running()
                if _llm_was_running_before_sync and not _skip_embedding_phases:
                    available = _get_available_gpu_memory_mb()
                    if available is None or available < _EMBEDDING_MEMORY_THRESHOLD_MB:
                        logger.info(f"Insufficient GPU memory ({available or 'unknown'} MB) for embeddings — stopping LLM")
                        if _stop_llm_for_embeddings():
                            _llm_stopped_for_sync = True
                    else:
                        logger.info(f"Sufficient GPU memory ({available} MB) — LLM stays running")

            if source in EMBEDDING_SOURCES and not dry_run and _skip_embedding_phases:
                logger.warning(f"Skipping {source}: insufficient system RAM")
                results[source] = {
                    "skipped": True,
                    "reason": "insufficient_ram",
                    "available_ram_mb": available_ram,
                    "error": f"Insufficient system RAM ({available_ram} MB free, need {_EMBEDDING_RAM_THRESHOLD_MB} MB)",
                }
                failed.append(source)
                continue

            success, stats = run_sync(source, dry_run=dry_run)
            results[source] = {"success": success, **stats}

            if not success:
                failed.append(source)

            # Restart LLM after last embedding phase completes (if we stopped it)
            if source in EMBEDDING_SOURCES and _llm_stopped_for_sync:
                next_is_embedding = (
                    source_idx + 1 < len(sources)
                    and sources[source_idx + 1] in EMBEDDING_SOURCES
                )
                if not next_is_embedding:
                    _start_llm()
                    _llm_stopped_for_sync = False
    finally:
        # Safety net: restart LLM if we stopped it and it's still down
        if _llm_stopped_for_sync and _llm_was_running_before_sync:
            logger.warning("LLM was stopped for embeddings but not restarted — restarting now")
            _start_llm()
            _llm_stopped_for_sync = False

    # Log summary
    logger.info("=" * 60)
    logger.info("SYNC RUN COMPLETE")
    logger.info(f"Total sources: {len(sources)}")
    logger.info(f"Succeeded: {len(sources) - len(failed)}")
    logger.info(f"Failed: {len(failed)}")
    if failed:
        logger.error(f"Failed sources: {', '.join(failed)}")
    if dep_skipped:
        logger.warning(f"Dependency-skipped sources: {', '.join(sorted(dep_skipped))}")
    logger.info("=" * 60)

    # Check overall health
    is_healthy, health_msg = check_sync_health()
    logger.info(f"Overall health: {health_msg}")

    # Silent-regression check: warn if a source keeps creating interactions
    # but stopped persisting source_entities (issue #199 §2). Run after the
    # sync so the data we look at is post-tonight, not pre-tonight.
    try:
        drift = detect_silent_source_entity_drift()
        for w in drift:
            logger.warning(
                f"source_entity drift: {w['source']} interactions through {w['last_interaction']} "
                f"but last source_entity={w['last_source_entity']} (gap={w['gap_days']}d)"
            )
    except Exception as e:
        logger.warning(f"Source-entity drift detector failed: {e}")

    # Calculate duration
    end_time = datetime.now()
    duration_seconds = (end_time - start_time).total_seconds()

    # Aggregate categorized stats across all sources
    people_created = 0
    people_updated = 0
    interactions_created = 0
    source_entities_created = 0
    people_by_source = {}
    interactions_by_source = {}

    for source, stats in results.items():
        if stats.get("skipped") or stats.get("dry_run"):
            continue
        pc = stats.get("people_created", 0)
        pu = stats.get("people_updated", 0)
        ic = stats.get("interactions_created", 0)
        sec = stats.get("source_entities_created", 0)

        people_created += pc
        people_updated += pu
        interactions_created += ic
        source_entities_created += sec

        if pc > 0:
            people_by_source[source] = pc
        if ic > 0:
            interactions_by_source[source] = ic

    result = {
        "sources_run": len(sources),
        "succeeded": len(sources) - len(failed),
        "failed": len(failed),
        "failed_sources": failed,
        "results": results,
        "is_healthy": is_healthy,
        "health_message": health_msg,
        "duration_seconds": duration_seconds,
        "trigger": trigger,
        # Categorized stats
        "people_created": people_created,
        "people_updated": people_updated,
        "interactions_created": interactions_created,
        "source_entities_created": source_entities_created,
        "people_by_source": people_by_source,
        "interactions_by_source": interactions_by_source,
        "dep_skipped_sources": sorted(dep_skipped),
    }

    # Exit maintenance mode now that sync is complete
    if not dry_run:
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://localhost:8000/api/admin/maintenance",
                method="DELETE",
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info("Exited maintenance mode — alerts re-enabled")
        except Exception as e:
            logger.warning(f"Could not exit maintenance mode (server may not be running): {e}")

    # Log summary to markdown (always, not just on failure)
    log_sync_summary_to_markdown(result, trigger=trigger)

    # Send Telegram notification (skip for dry run)
    if not dry_run:
        send_sync_summary_telegram(result, trigger=trigger)

    # Restart server to pick up any changes and clear stale state
    if not dry_run:
        try:
            restart_result = subprocess.run(
                [str(Path(__file__).parent / "server.sh"), "restart"],
                capture_output=True, text=True, timeout=300,
                cwd=str(Path(__file__).parent.parent),
            )
            if restart_result.returncode == 0:
                logger.info("Server restarted successfully after sync")
            else:
                logger.warning(f"Server restart failed: {restart_result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning("Server restart timed out (300s)")
        except Exception as e:
            logger.warning(f"Could not restart server post-sync: {e}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Run CRM data source syncs")
    parser.add_argument("--source", help="Run only this specific source")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually sync")
    parser.add_argument("--execute", action="store_true", help="Actually run syncs (required for non-dry-run)")
    parser.add_argument("--force", action="store_true", help="Run even if recently synced")
    parser.add_argument("--status", action="store_true", help="Just show sync status")
    parser.add_argument("--trigger", choices=["scheduled", "manual", "startup"], default="scheduled",
                        help="How sync was triggered (default: scheduled)")
    args = parser.parse_args()

    if args.status:
        summary = get_sync_summary()
        print("\nSync Health Summary:")
        print(f"  Total sources: {summary['total_sources']} ({summary.get('enabled_sources', summary['total_sources'])} enabled)")
        print(f"  Healthy: {summary['healthy']}")
        print(f"  Stale: {summary['stale']} {summary['stale_sources']}")
        print(f"  Failed: {summary['failed']} {summary['failed_sources']}")
        print(f"  Never run: {summary['never_run']} {summary['never_run_sources']}")
        if summary.get('disabled', 0):
            print(f"  Disabled (expected): {summary['disabled']} {summary.get('disabled_sources', [])}")
        print(f"  All healthy: {summary['all_healthy']}")
        return 0 if summary['all_healthy'] else 1

    sources = [args.source] if args.source else None

    # Require --execute for actual syncs (safety measure)
    dry_run = args.dry_run or not args.execute
    if not args.execute and not args.dry_run:
        logger.info("Note: Running in dry-run mode. Use --execute to actually run syncs.")

    result = run_all_syncs(sources=sources, dry_run=dry_run, force=args.force, trigger=args.trigger)

    # Exit with error if any sync failed
    if result["failed"] > 0:
        sys.exit(1)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
