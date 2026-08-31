#!/usr/bin/env python3
"""
Purge calendar interactions belonging to mass meetings.

Sitting in the same 90-person standing call is no evidence that two people know
each other, so `sync_gmail_calendar_interactions.py` no longer creates rows for
events above `InteractionConfig.MASS_MEETING_ATTENDEE_LIMIT`. This removes the
rows that were written before that guard existed.

The read paths already ignore these rows, so this is a housekeeping step —
it reclaims space and makes raw interaction counts honest.

Usage:
    python3 scripts/purge_mass_meeting_interactions.py            # dry run
    python3 scripts/purge_mass_meeting_interactions.py --execute
"""
import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.interaction_store import get_interaction_db_path
from config.people_config import InteractionConfig

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def purge(limit: int, dry_run: bool = True) -> dict:
    """Delete calendar interactions for events with more than `limit` attendees.

    Returns a stats dict; `SYNC_STATS:` is not emitted because this is a manual
    tool rather than a registered sync source.
    """
    conn = sqlite3.connect(get_interaction_db_path())
    try:
        where = "source_type = 'calendar' AND COALESCE(attendee_count, 0) > ?"

        total = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        doomed = conn.execute(
            f"SELECT COUNT(*) FROM interactions WHERE {where}", (limit,)
        ).fetchone()[0]
        events = conn.execute(
            f"SELECT COUNT(DISTINCT substr(source_id, 1, instr(source_id, ':') - 1)) "
            f"FROM interactions WHERE {where} AND source_id LIKE '%:%'",
            (limit,),
        ).fetchone()[0]
        people = conn.execute(
            f"SELECT COUNT(DISTINCT person_id) FROM interactions WHERE {where}",
            (limit,),
        ).fetchone()[0]

        logger.info(f"Attendee limit           : {limit}")
        logger.info(f"Interactions in database : {total:,}")
        logger.info(f"Mass-meeting rows        : {doomed:,} ({doomed / total:.1%})")
        logger.info(f"  across events          : {events:,}")
        logger.info(f"  touching people        : {people:,}")

        if dry_run:
            logger.info("Dry run — nothing deleted. Re-run with --execute to apply.")
        elif doomed:
            with conn:
                conn.execute(f"DELETE FROM interactions WHERE {where}", (limit,))
            logger.info(f"Deleted {doomed:,} rows.")
            logger.info("Run scripts/sync_person_stats.py --execute to refresh person stats.")
        else:
            logger.info("Nothing to delete.")

        return {
            "total": total,
            "mass_meeting_rows": doomed,
            "events": events,
            "people": people,
            "deleted": 0 if dry_run else doomed,
        }
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually delete (default: dry run)")
    parser.add_argument(
        "--limit",
        type=int,
        default=InteractionConfig.MASS_MEETING_ATTENDEE_LIMIT,
        help="Other-attendee count above which a meeting is ignored",
    )
    args = parser.parse_args(argv)

    purge(limit=args.limit, dry_run=not args.execute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
