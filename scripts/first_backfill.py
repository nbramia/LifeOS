#!/usr/bin/env python3
"""
One-time deep backfill for full-history-capable sync sources.

The nightly sync (run_all_syncs.py) deliberately looks back only a narrow
window each night (e.g. Gmail/Calendar's --days 30) to keep the nightly run
fast. A fresh install therefore starts with an empty interaction history
that the nightly job never fills in on its own — someone has to notice the
gap and know to run a deeper pass by hand. On a real second-user install,
this went unnoticed long enough that months of history simply never
arrived (#778).

This script runs the same sources, in the same phase order, as the nightly
pipeline's SYNC_ORDER (Phases 1-4: collection, entity processing,
relationship building, indexing) — but any source whose nightly invocation
narrows its own lookback window is instead run at that script's full-history
default. It is a thin driver: the actual sync logic, idempotence, and
unconfigured-source clean-skip behavior (#687) all live in the underlying
per-source scripts, reused unmodified — this script only decides which
sources to run, in what order, with what arguments, and prints a coverage
report when it's done.

Deliberately excluded, even though present in the nightly SYNC_ORDER:
  - push_birthdays: pushes LifeOS data outward (to Apple Contacts) — no
    "full history" concept to backfill.
  - Phase 5 (google_docs, google_sheets, monarch_money), Phase 6
    (entity_cleanup), Phase 7 (consistency_verify): content sync, cleanup,
    and verification, not interaction-history depth. #778's acceptance
    criteria name exactly four phases to mirror — collection, entity
    processing, relationship building, indexing — so this stops at the end
    of Phase 4.
  - phone: not part of the nightly SYNC_ORDER at all (it's macOS-FDA-only,
    invoked separately by scripts/run_fda_syncs.py's own cron path, always
    at its own full-history default already — see sync_phone_calls.py).
    It has no configured/unconfigured clean-skip today, so wiring it into
    a skip-safe backfill entry point is a separate follow-up.

Safe to re-run: every underlying script is already idempotent (each upserts
by its own source_id rather than appending), and this script writes nothing
of its own — it only reads data/interactions.db for the coverage report.

Usage:
    python scripts/first_backfill.py --dry-run     # show what would run
    python scripts/first_backfill.py --execute     # run the full backfill
"""
import argparse
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.run_all_syncs import SYNC_ORDER, SYNC_SCRIPTS, _parse_sync_output
from api.services.interaction_store import get_interaction_db_path, VALID_SOURCE_TYPES

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent

_BACKFILL_EXCLUDE = {
    "push_birthdays",
    "google_docs", "google_sheets", "monarch_money",
    "entity_cleanup", "consistency_verify",
}

# Same relative order as the nightly pipeline's SYNC_ORDER (Phases 1-4),
# derived rather than duplicated so this can't silently drift out of step
# with a future reordering there.
BACKFILL_ORDER = [s for s in SYNC_ORDER if s not in _BACKFILL_EXCLUDE]

# Much longer than the nightly job's per-source timeouts (run_all_syncs.py's
# SYNC_TIMEOUTS default to 1 hour, tuned for a narrow nightly window) —
# ten years of Gmail/Calendar history can take substantially longer than 30
# days' worth. Not source-specific: this script runs once, by hand, not on
# a tight schedule, so a single generous default is simpler than per-source
# tuning with no data yet to tune it against.
BACKFILL_TIMEOUT_SECONDS = 6 * 3600  # 6 hours


def _full_depth_args(nightly_args: list) -> list:
    """Strip the nightly ``--days N`` override, if present, so the
    underlying script's own full-history default applies instead (3650
    days as of this writing — see sync_gmail_calendar_interactions.py and
    sync_phone_calls.py). Never hardcode that default here: if it ever
    changes, this backfill should track it automatically rather than
    needing its own update. Sources with no such override (most of them)
    are returned unchanged.
    """
    if "--days" not in nightly_args:
        return list(nightly_args)
    args = list(nightly_args)
    idx = args.index("--days")
    del args[idx:idx + 2]
    return args


def run_backfill_source(source: str, dry_run: bool = False) -> dict:
    """Run one source at full depth.

    Deliberately does none of run_all_syncs.py's sync_health/retry
    bookkeeping: this is a one-time, out-of-band pass, not part of the
    nightly monitoring loop, and recording it there would pollute the
    duration/yield-collapse baselines nightly runs are compared against.
    """
    if source not in SYNC_SCRIPTS:
        return {"success": False, "skipped": False, "error": f"No script for {source}"}

    script_path, nightly_args = SYNC_SCRIPTS[source]
    args = _full_depth_args(nightly_args)
    full_path = PROJECT_ROOT / script_path

    if not full_path.exists():
        return {"success": False, "skipped": False, "error": f"Script not found: {script_path}"}

    cmd = [sys.executable, str(full_path)] + args

    if dry_run:
        logger.info(f"[DRY RUN] Would run: {' '.join(cmd)}")
        return {"success": True, "skipped": False, "dry_run": True, "cmd": cmd}

    logger.info(f"Running {source} at full depth: {' '.join(args)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=BACKFILL_TIMEOUT_SECONDS,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
    except subprocess.TimeoutExpired:
        logger.error(f"{source}: timed out after {BACKFILL_TIMEOUT_SECONDS}s")
        return {"success": False, "skipped": False, "error": "timeout"}

    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
    stats = _parse_sync_output(combined_output)
    skipped_reason = stats.pop("skipped_reason", None)

    # Mirror run_all_syncs.py's own precedence exactly: exit code decides
    # success/failure first; a clean skip (#687) is only recognized within
    # an already-successful (exit 0) run.
    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        logger.error(f"{source}: failed — {error_msg[:500]}")
        return {"success": False, "skipped": False, "error": error_msg[:500], "stats": stats}

    if skipped_reason:
        logger.info(f"{source}: skipped ({skipped_reason})")
        return {"success": True, "skipped": True, "reason": skipped_reason, "stats": stats}

    logger.info(f"{source}: OK — {stats}")
    return {"success": True, "skipped": False, "stats": stats}


def coverage_report() -> dict:
    """Per-source-type earliest/latest record date and count from the
    interactions store — the acceptance criteria's "coverage report".
    Read-only; safe to call any time, including on a totally fresh
    install where the interactions store doesn't exist yet.

    ``sqlite3.connect`` creates an empty file at the given path if none
    exists, which would make this script's "writes nothing of its own"
    claim false on a fresh/all-unconfigured install — check existence
    first rather than relying on the OperationalError fallback below to
    keep that true.
    """
    report = {}
    db_path = get_interaction_db_path()
    if not Path(db_path).exists():
        return report
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        logger.warning(f"Could not open interactions store for coverage report: {e}")
        return report
    try:
        for source_type in sorted(VALID_SOURCE_TYPES):
            try:
                row = conn.execute(
                    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) "
                    "FROM interactions WHERE source_type = ?",
                    (source_type,),
                ).fetchone()
            except sqlite3.OperationalError:
                # Fresh install: the interactions table doesn't exist yet.
                break
            count, earliest, latest = row
            if count:
                report[source_type] = {
                    "count": count,
                    "earliest": earliest,
                    "latest": latest,
                }
    finally:
        conn.close()
    return report


def print_coverage_report(report: dict) -> None:
    print("\n=== Coverage Report ===")
    if not report:
        print("(no interaction records found)")
        return
    for source_type, info in sorted(report.items()):
        print(
            f"  {source_type:12s} count={info['count']:>7}  "
            f"earliest={info['earliest']}  latest={info['latest']}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="One-time deep backfill for full-history sync sources (#778)"
    )
    parser.add_argument("--execute", action="store_true", help="Actually run the backfill")
    parser.add_argument("--dry-run", action="store_true", help="Preview without running anything")
    args = parser.parse_args()

    if not args.execute and not args.dry_run:
        print("Use --execute to run the backfill or --dry-run to preview.")
        sys.exit(1)

    dry_run = not args.execute

    results = {}
    for source in BACKFILL_ORDER:
        results[source] = run_backfill_source(source, dry_run=dry_run)

    failed = [s for s, r in results.items() if not r.get("success")]
    skipped = [s for s, r in results.items() if r.get("skipped")]

    print(f"\n{len(BACKFILL_ORDER) - len(failed)}/{len(BACKFILL_ORDER)} sources succeeded")
    if skipped:
        print(f"Skipped (not configured): {', '.join(skipped)}")
    if failed:
        print(f"Failed: {', '.join(failed)}")

    if not dry_run:
        print_coverage_report(coverage_report())

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
