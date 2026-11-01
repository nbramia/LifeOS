#!/usr/bin/env python3
"""
Record group size on existing WhatsApp interactions.

A WhatsApp interaction carries its group's size in ``attendee_count`` when the
message belongs to a group chat, and NULL when it is a direct message. Relationship
strength reads that to weight group messages below direct ones. This fills in
rows stored without it, using the WhatsApp export to find each row's chat.

* A row's source_id is ``whatsapp_<msg_id>`` or ``whatsapp_<msg_id>:<participant>``;
  the msg_id locates its message in the export.
* The size is the chat's count of ``group_participants``, and 0 for a group whose
  membership the export doesn't list. 0 still marks the row as a group message.
* Direct messages are never written, and neither is a row that already has a value,
  so running this again changes nothing, even against a newer export.
* A row whose message isn't in the export is left alone and reported as unmatched.

Direction isn't stored: only a group message you sent has the fan-out source_id
form. ``group_from_me_without_fanout`` and ``fanout_in_direct_chat`` report rows
that contradict that; both are expected to be 0.

Output is aggregate counts only, never message text or names.

Usage:
    python3 scripts/backfill_whatsapp_group_size.py            # dry run
    python3 scripts/backfill_whatsapp_group_size.py --execute  # backup, then write
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_INTERACTIONS_DB = DATA_DIR / "interactions.db"
DEFAULT_EXPORT_JSON = DATA_DIR / "apple-imports" / "whatsapp.json"

_SOURCE_ID_PREFIX = "whatsapp_"
_GROUP_JID_SUFFIX = "@g.us"


def plan_updates(
    rows: list[tuple[str, str, int | None]],
    export: dict,
) -> tuple[list[tuple[int, str]], Counter]:
    """Decide which rows get a group size.

    Args:
        rows: (id, source_id, attendee_count) for every WhatsApp interaction.
        export: The parsed WhatsApp export.

    Returns:
        (size, id) pairs to write, and aggregate counts.
    """
    messages = {m["msg_id"]: m for m in export.get("messages", [])}
    group_sizes = Counter(p["group_jid"] for p in export.get("group_participants", []))

    updates: list[tuple[int, str]] = []
    stats: Counter = Counter()
    for row_id, source_id, attendee_count in rows:
        stats["rows"] += 1
        is_fanout = ":" in source_id
        msg_id = source_id.removeprefix(_SOURCE_ID_PREFIX).split(":", 1)[0]
        message = messages.get(msg_id)
        if message is None:
            stats["unmatched"] += 1
            stats["unmatched_fanout" if is_fanout else "unmatched_bare"] += 1
            continue

        chat_jid = message.get("chat_jid") or ""
        if not chat_jid.endswith(_GROUP_JID_SUFFIX):
            stats["direct"] += 1
            if is_fanout:
                stats["fanout_in_direct_chat"] += 1
            continue

        stats["group_sent" if is_fanout else "group_received"] += 1
        if message.get("from_me") and not is_fanout:
            stats["group_from_me_without_fanout"] += 1
        if attendee_count is not None:
            stats["already_set"] += 1
            continue

        size = group_sizes.get(chat_jid, 0)
        if size == 0:
            stats["unknown_membership"] += 1
        updates.append((size, row_id))

    stats["to_update"] = len(updates)
    return updates, stats


def _backup(db_path: str) -> str:
    """Copy the database beside itself with SQLite's backup API (WAL-safe)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.bak-whatsapp-group-size-{stamp}"
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.chmod(backup_path, 0o600)
    return backup_path


def backfill(interactions_db: str, export_json: str, dry_run: bool = True) -> dict:
    """Run the backfill.

    Args:
        interactions_db: Path to interactions.db.
        export_json: Path to the WhatsApp export.
        dry_run: If True, report what would change without writing.

    Returns:
        Aggregate counts, plus the backup path when one was written.
    """
    with open(export_json, encoding="utf-8") as f:
        export = json.load(f)

    conn = sqlite3.connect(f"file:{Path(interactions_db).resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, source_id, attendee_count FROM interactions WHERE source_type = 'whatsapp'"
        ).fetchall()
    finally:
        conn.close()

    updates, counts = plan_updates(rows, export)
    stats: dict = dict(counts)
    stats["updated"] = 0
    stats["backup"] = None

    if dry_run or not updates:
        return stats

    stats["backup"] = _backup(interactions_db)
    conn = sqlite3.connect(interactions_db, timeout=60)
    try:
        with conn:
            before = conn.total_changes
            conn.executemany(
                "UPDATE interactions SET attendee_count = ? WHERE id = ? AND attendee_count IS NULL",
                updates,
            )
            stats["updated"] = conn.total_changes - before
    finally:
        conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Record group size on WhatsApp interactions")
    parser.add_argument("--execute", action="store_true", help="Back up, then write (default: dry run)")
    parser.add_argument("--interactions-db", default=str(DEFAULT_INTERACTIONS_DB), help="Path to interactions.db")
    parser.add_argument("--export-json", default=str(DEFAULT_EXPORT_JSON), help="Path to the WhatsApp export")
    args = parser.parse_args()

    for path in (args.interactions_db, args.export_json):
        if not Path(path).is_file():
            logger.error(f"Not found: {path}")
            return 1

    stats = backfill(args.interactions_db, args.export_json, dry_run=not args.execute)
    logger.info("DRY RUN - use --execute to write" if not args.execute else "EXECUTED")
    for key in sorted(stats):
        logger.info(f"{key:32}: {stats[key]}")
    if stats.get("unmatched"):
        logger.warning(f"{stats['unmatched']} rows have no message in the export; they keep NULL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
