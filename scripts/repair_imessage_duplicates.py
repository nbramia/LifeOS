#!/usr/bin/env python3
"""
Remove stale duplicate iMessage interactions.

iMessage interactions are identified by (UTC second, direction), not by
chat.db rowid (see api/services/imessage_identity.py). Interactions under
legacy ``imessage_<rowid>`` ids can outnumber the messages they stand for when
rowids shift between exports. This removes the surplus.

The rule is conservative; when in doubt a row is kept:

* Per message key, ``extras`` = interactions at the key minus ALL messages in
  the message store at the key (linked or not, hidden people included).
* A row is deletable only if it is "stale": a legacy ``imessage_<rowid>`` id
  whose store row is missing or whose store row has a different key.
* A stale row is deleted only if another retained row at the same key belongs
  to the same person_id.
* Rows at a key with no store message at all are kept (orphans).
* At most ``extras`` rows are deleted per key.

It never inserts anything; the normal sync adds missing messages. Output is
aggregate counts only.

Usage:
    python3 scripts/repair_imessage_duplicates.py            # dry run
    python3 scripts/repair_imessage_duplicates.py --execute  # backup, then delete
"""
import argparse
import logging
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.imessage_identity import (
    consume,
    interaction_key,
    legacy_rowid,
    message_key,
    resolve_people,
)
from api.services.interaction_store import get_interaction_db_path
from scripts.sync_imessage_interactions import get_imessage_db_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_DELETE_CHUNK = 500


def _connect_read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)


def _load_store(imessage_db: str):
    """Message-store keys: rowid -> key, key -> message count, linked rows, and
    the rowids whose timestamp yields no key."""
    conn = _connect_read_only(imessage_db)
    try:
        key_by_rowid: dict[int, tuple] = {}
        unkeyed_rowids: set[int] = set()
        store_counts: Counter = Counter()
        linked: list[tuple] = []  # (key, person_entity_id)
        for rowid, timestamp, is_from_me, person_id in conn.execute(
            "SELECT rowid, timestamp, is_from_me, person_entity_id FROM messages"
        ):
            key = message_key(timestamp, is_from_me)
            if key is None:
                unkeyed_rowids.add(rowid)
                continue
            key_by_rowid[rowid] = key
            store_counts[key] += 1
            if person_id is not None:
                linked.append((key, person_id))
        return key_by_rowid, unkeyed_rowids, store_counts, linked
    finally:
        conn.close()


def _plan(rows, key_by_rowid, unkeyed_rowids, store_counts):
    """Decide deletions. Returns (ids_to_delete, per-key deleted Counter, stats)."""
    stats = {
        "rows_scanned": len(rows),
        "kept_unkeyed": 0,
        "stale_rows": 0,
        "would_delete": 0,
        "kept_person_mismatch": 0,
        "kept_orphan": 0,
        "kept_over_cap": 0,
    }

    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row_id, person_id, timestamp, title, source_id in rows:
        key = interaction_key(timestamp, title)
        if key is None:
            stats["kept_unkeyed"] += 1
            continue
        rowid = legacy_rowid(source_id)
        # A legacy id is stale when its store row is gone or is another
        # message; a store row with no usable key is not evidence either way.
        stale = (
            rowid is not None
            and rowid not in unkeyed_rowids
            and key_by_rowid.get(rowid) != key
        )
        stats["stale_rows"] += stale
        by_key[key].append(
            {"id": row_id, "person": person_id, "stale": stale, "order": (rowid or 0, row_id)}
        )

    delete_ids: list[str] = []
    deleted_per_key: Counter = Counter()
    for key, group in by_key.items():
        in_store = store_counts.get(key, 0)
        if in_store == 0:
            stats["kept_orphan"] += len(group)
            continue
        extras = len(group) - in_store
        if extras <= 0:
            continue

        retained = {row["id"]: row for row in group}
        for candidate in sorted((r for r in group if r["stale"]), key=lambda r: r["order"]):
            if deleted_per_key[key] >= extras:
                stats["kept_over_cap"] += 1
                continue
            has_same_person = any(
                other["id"] != candidate["id"] and other["person"] == candidate["person"]
                for other in retained.values()
            )
            if not has_same_person:
                stats["kept_person_mismatch"] += 1
                continue
            del retained[candidate["id"]]
            delete_ids.append(candidate["id"])
            deleted_per_key[key] += 1

    stats["would_delete"] = len(delete_ids)
    return delete_ids, deleted_per_key, stats


def _would_insert(linked, store_counts_after, person_store) -> int:
    """Messages the sync would insert after the deletes: linked, non-hidden,
    and with no unused interaction at their key."""
    people = resolve_people(person_store, (person_id for _, person_id in linked))
    counts = Counter(store_counts_after)
    would_insert = 0
    for key, person_id in linked:
        person = people.get(person_id)
        if person is None or person.hidden:
            continue
        if not consume(counts, key):
            would_insert += 1
    return would_insert


def _backup(db_path: str) -> str:
    """Copy the database beside itself with SQLite's backup API (WAL-safe)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.bak-{stamp}"
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return backup_path


def repair(
    interactions_db: str,
    imessage_db: str,
    dry_run: bool = True,
    person_store=None,
) -> dict:
    """Plan (and with ``dry_run=False`` apply) the stale-duplicate deletes.

    Returns aggregate counts only.
    """
    if person_store is None:
        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()

    key_by_rowid, unkeyed_rowids, store_counts, linked = _load_store(imessage_db)

    reader = _connect_read_only(interactions_db) if dry_run else sqlite3.connect(interactions_db)
    try:
        rows = reader.execute(
            "SELECT id, person_id, timestamp, title, source_id FROM interactions "
            "WHERE source_type = 'imessage'"
        ).fetchall()
    finally:
        reader.close()

    delete_ids, _deleted_per_key, stats = _plan(rows, key_by_rowid, unkeyed_rowids, store_counts)

    # Interaction counts per key after the deletes, as the sync will see them.
    deleted = set(delete_ids)
    after: Counter = Counter()
    for row_id, _person, timestamp, title, _source in rows:
        key = interaction_key(timestamp, title)
        if key is not None and row_id not in deleted:
            after[key] += 1
    stats["would_be_inserted"] = _would_insert(linked, after, person_store)

    stats["deleted"] = 0
    stats["backup"] = None
    if not dry_run:
        stats["backup"] = _backup(interactions_db)
        conn = sqlite3.connect(interactions_db)
        try:
            with conn:
                for start in range(0, len(delete_ids), _DELETE_CHUNK):
                    chunk = delete_ids[start:start + _DELETE_CHUNK]
                    marks = ",".join("?" * len(chunk))
                    stats["deleted"] += conn.execute(
                        f"DELETE FROM interactions WHERE source_type = 'imessage' AND id IN ({marks})",
                        chunk,
                    ).rowcount
        finally:
            conn.close()

    logger.info(f"Rows scanned           : {stats['rows_scanned']:,}")
    logger.info(f"Stale rows             : {stats['stale_rows']:,}")
    label = "Would delete" if dry_run else "Deleted"
    logger.info(f"{label:<23}: {(stats['would_delete'] if dry_run else stats['deleted']):,}")
    logger.info(f"Kept (person mismatch) : {stats['kept_person_mismatch']:,}")
    logger.info(f"Kept (orphan)          : {stats['kept_orphan']:,}")
    logger.info(f"Kept (over extras cap) : {stats['kept_over_cap']:,}")
    logger.info(f"Kept (no usable key)   : {stats['kept_unkeyed']:,}")
    logger.info(f"Would be inserted      : {stats['would_be_inserted']:,}")
    if stats["backup"]:
        logger.info(f"Backup written         : {stats['backup']}")
    if dry_run:
        logger.info("Dry run — nothing changed. Re-run with --execute to apply.")
    elif stats["deleted"]:
        logger.info("Run scripts/sync_person_stats.py --execute to refresh person stats.")
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Back up, then delete (default: dry run)")
    parser.add_argument("--interactions-db", default=None, help="Interactions database (default: the sync's path)")
    parser.add_argument("--imessage-db", default=None, help="iMessage export database (default: the sync's path)")
    parser.add_argument("--people-db", default=None, help="People database used for hidden/merge lookups (default: the sync's path)")
    args = parser.parse_args(argv)

    person_store = None
    if args.people_db:
        from api.services.person_entity import PersonEntityStore
        person_store = PersonEntityStore(args.people_db)

    repair(
        interactions_db=args.interactions_db or get_interaction_db_path(),
        imessage_db=args.imessage_db or get_imessage_db_path(),
        dry_run=not args.execute,
        person_store=person_store,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
