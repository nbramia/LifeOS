#!/usr/bin/env python3
"""
Remove WhatsApp interactions that came from bot or broadcast chats.

A message in a bot chat or a status / broadcast list is not correspondence with
a person, and ``process_whatsapp_messages`` skips them. This removes the rows of
that kind that are already stored.

* A row's source_id is ``whatsapp_<msg_id>`` or ``whatsapp_<msg_id>:<participant>``;
  the msg_id locates its message in the WhatsApp export, and the chat's JID
  domain (``@bot`` or ``@broadcast``) decides whether the row goes.
* A row whose message isn't in the export is left alone and reported as unmatched.
* Idempotent: once the rows are gone, running it again matches nothing.

Output is aggregate counts only, never message text, names, or JIDs. Person
counts on the affected people are recomputed by the next stats refresh
(``api.services.person_stats.refresh_person_stats()``).

Usage:
    python3 scripts/purge_whatsapp_non_person_chats.py            # dry run
    python3 scripts/purge_whatsapp_non_person_chats.py --execute  # backup, then delete
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_INTERACTIONS_DB = DATA_DIR / "interactions.db"
DEFAULT_EXPORT_JSON = DATA_DIR / "apple-imports" / "whatsapp.json"

_SOURCE_ID_PREFIX = "whatsapp_"


def plan_deletions(
    rows: list[tuple[str, str, str]],
    export: dict,
) -> tuple[list[str], Counter]:
    """Decide which rows to delete.

    Args:
        rows: (id, source_id, person_id) for every WhatsApp interaction.
        export: The parsed WhatsApp export.

    Returns:
        The interaction ids to delete, and aggregate counts.
    """
    from api.services.whatsapp import is_non_person_chat_jid

    chats = {m["msg_id"]: m.get("chat_jid") or "" for m in export.get("messages", [])}

    doomed: list[str] = []
    people: set[str] = set()
    stats: Counter = Counter()
    for row_id, source_id, person_id in rows:
        stats["rows"] += 1
        msg_id = source_id.removeprefix(_SOURCE_ID_PREFIX).split(":", 1)[0]
        chat_jid = chats.get(msg_id)
        if chat_jid is None:
            stats["unmatched"] += 1
            continue
        if not is_non_person_chat_jid(chat_jid):
            continue
        stats["bot" if chat_jid.endswith("@bot") else "broadcast"] += 1
        doomed.append(row_id)
        people.add(person_id)

    stats["to_delete"] = len(doomed)
    stats["affected_people"] = len(people)
    return doomed, stats


def _backup(db_path: str) -> str:
    """Copy the database beside itself with SQLite's backup API (WAL-safe)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.bak-whatsapp-non-person-{stamp}"
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


def purge(interactions_db: str, export_json: str, dry_run: bool = True) -> dict:
    """Run the purge.

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
            "SELECT id, source_id, person_id FROM interactions WHERE source_type = 'whatsapp'"
        ).fetchall()
    finally:
        conn.close()

    doomed, counts = plan_deletions(rows, export)
    stats: dict = dict(counts)
    stats["deleted"] = 0
    stats["backup"] = None

    if dry_run or not doomed:
        return stats

    stats["backup"] = _backup(interactions_db)
    conn = sqlite3.connect(interactions_db, timeout=60)
    try:
        with conn:
            before = conn.total_changes
            conn.executemany(
                "DELETE FROM interactions WHERE id = ? AND source_type = 'whatsapp'",
                [(row_id,) for row_id in doomed],
            )
            stats["deleted"] = conn.total_changes - before
    finally:
        conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove WhatsApp interactions from bot and broadcast chats")
    parser.add_argument("--execute", action="store_true", help="Back up, then delete (default: dry run)")
    parser.add_argument("--interactions-db", default=str(DEFAULT_INTERACTIONS_DB), help="Path to interactions.db")
    parser.add_argument("--export-json", default=str(DEFAULT_EXPORT_JSON), help="Path to the WhatsApp export")
    args = parser.parse_args()

    for path in (args.interactions_db, args.export_json):
        if not Path(path).is_file():
            logger.error(f"Not found: {path}")
            return 1

    stats = purge(args.interactions_db, args.export_json, dry_run=not args.execute)
    logger.info("DRY RUN - use --execute to delete" if not args.execute else "EXECUTED")
    for key in sorted(stats):
        logger.info(f"{key:32}: {stats[key]}")
    if stats.get("unmatched"):
        logger.warning(f"{stats['unmatched']} rows have no message in the export; they were left alone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
