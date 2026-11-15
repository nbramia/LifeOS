#!/usr/bin/env python3
"""Sync Pendant conversations into `in_person` interactions.

Reconciles the last `LIFEOS_PENDANT_INTERACTIONS_WINDOW_DAYS` days (or
`--days N`) against the Pendant feed: writes one row per person per day and
removes that window's other Pendant in-person rows. See
api/services/pendant_interactions.py for the counting and dedup rules.

    --days N     reconcile the N days ending today
    --backfill   reconcile from the feed's first day (`GET /days`) to today
    --dry-run    compute and print counts, write nothing

Writes by default. With no feed token configured it prints `SYNC_SKIPPED` and
exits 0. It always prints one `SYNC_STATS:{json}` line. A feed failure leaves
the failed days' rows untouched and is reported as errors; the exit code is 1
only when no day could be reconciled.
"""
import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services import pendant_feed, pendant_interactions  # noqa: E402
from api.services.interaction_store import get_interaction_store  # noqa: E402
from api.services.sync_health import emit_sync_stats  # noqa: E402
from config.settings import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _stats(result, processed: int = 0) -> dict:
    return {
        "processed": processed,
        "created": result.created if result else 0,
        "updated": result.updated if result else 0,
        "deleted": result.deleted if result else 0,
        "errors": result.errors if result else 1,
        "interactions_created": result.created if result else 0,
    }


def main(argv=None, today: date = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=None, help="Days ending today to reconcile")
    group.add_argument("--backfill", action="store_true", help="Reconcile every day the feed holds")
    parser.add_argument("--dry-run", action="store_true", help="Print counts only; write nothing")
    args = parser.parse_args(argv)

    if not pendant_feed.feed_token():
        print(
            "SYNC_SKIPPED: Pendant feed not configured (set LIFEOS_PENDANT_FEED_TOKEN "
            "or LIFEOS_PENDANT_FEED_TOKEN_FILE)",
            flush=True,
        )
        return 0

    today = today or date.today()
    if args.backfill:
        try:
            first = pendant_interactions.fetch_first_day()
        except pendant_feed.PendantFeedError as e:
            logger.error("Pendant feed day list failed: %s", e)
            emit_sync_stats(_stats(None))
            return 1
        if first is None:
            logger.info("Pendant feed holds no days")
            emit_sync_stats(_stats(None) | {"errors": 0})
            return 0
        start, end = min(first, today), today
    else:
        start, end = pendant_interactions.window_days(
            args.days if args.days is not None else settings.pendant_interactions_window_days, today
        )

    store = get_interaction_store()
    from api.services.person_entity import get_person_entity_store

    result = pendant_interactions.reconcile(
        start, end,
        db_path=store.db_path,
        person_store=get_person_entity_store(),
        dry_run=args.dry_run,
    )

    summary = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "dry_run": args.dry_run,
        "days_reconciled": result.days_reconciled,
        "people": result.people,
        "rows": result.rows,
        "would_create" if args.dry_run else "created": result.created,
        "would_update" if args.dry_run else "updated": result.updated,
        "would_delete" if args.dry_run else "deleted": result.deleted,
        **dict(result.counters),
        "errors": result.errors,
    }
    print("PENDANT_INTERACTIONS:" + json.dumps(summary), flush=True)

    if not args.dry_run and result.affected_person_ids:
        from api.services.person_stats import refresh_person_stats

        logger.info("Refreshing stats for %d affected people", len(result.affected_person_ids))
        refresh_person_stats(sorted(result.affected_person_ids))

    if args.dry_run:
        stats = _stats(result, result.counters["conversations"]) | {
            "created": 0, "updated": 0, "deleted": 0, "interactions_created": 0,
        }
    else:
        stats = _stats(result, result.counters["conversations"])
    emit_sync_stats(stats)
    return 1 if result.errors and not result.days_reconciled else 0


if __name__ == "__main__":
    sys.exit(main())
