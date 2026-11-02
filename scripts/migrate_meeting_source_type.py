#!/usr/bin/env python3
"""
Run the meeting source type migration against explicitly named databases.

The stores run this migration on initialization. This script exists to run it on
copies and to reverse it. It takes every path as a required argument and loads
the migration module by file, so it can't reach a default database or read
LifeOS settings. Output is counts only.

Copy a live database with sqlite3's backup command, or stop its writers and copy
it together with its -wal and -shm files.

Usage:
    python scripts/migrate_meeting_source_type.py \\
        --interactions-db /copy/interactions.db --crm-db /copy/crm.db --vault /path/to/vault

    python scripts/migrate_meeting_source_type.py \\
        --interactions-db /copy/interactions.db --crm-db /copy/crm.db --rollback
"""
import argparse
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "api" / "services" / "meeting_source.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("meeting_source_standalone", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _per_type(db_path: str, table: str) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return dict(conn.execute(f"SELECT source_type, COUNT(*) FROM {table} GROUP BY 1").fetchall())
    finally:
        conn.close()


def _per_person_meetings(db_path: str, table: str, person_column: str) -> Counter:
    """Meeting rows per person, counting both labels together."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return Counter(
            dict(
                conn.execute(
                    f"SELECT {person_column}, COUNT(*) FROM {table} "
                    "WHERE source_type IN ('meeting', 'granola') GROUP BY 1"
                ).fetchall()
            )
        )
    finally:
        conn.close()


def _person_source_tokens(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tokens: Counter = Counter()
        for (raw,) in conn.execute("SELECT sources FROM person_entities"):
            tokens.update(json.loads(raw or "[]"))
        return {t: tokens[t] for t in ("meeting", "granola")}
    finally:
        conn.close()


def _snapshot(args) -> dict:
    return {
        "interactions": _per_type(args.interactions_db, "interactions"),
        "source_entities": _per_type(args.crm_db, "source_entities"),
        "interactions_per_person": _per_person_meetings(
            args.interactions_db, "interactions", "person_id"
        ),
        "source_entities_per_person": _per_person_meetings(
            args.crm_db, "source_entities", "canonical_person_id"
        ),
        "person_sources": _person_source_tokens(args.crm_db),
    }


def _report(before: dict, after: dict) -> None:
    for store in ("interactions", "source_entities"):
        print(f"{store} rows per source type:")
        for source_type in sorted(set(before[store]) | set(after[store]), key=str):
            old, new = before[store].get(source_type, 0), after[store].get(source_type, 0)
            print(f"  {source_type:16} {old:>8} -> {new:>8}")
        print(f"  {'total':16} {sum(before[store].values()):>8} -> {sum(after[store].values()):>8}")
        old_people = before[f"{store}_per_person"]
        new_people = after[f"{store}_per_person"]
        changed = sum(
            1 for person in set(old_people) | set(new_people)
            if old_people[person] != new_people[person]
        )
        print(f"  people whose meeting-row count changed: {changed}")
    print(f"person sources lists: {before['person_sources']} -> {after['person_sources']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--interactions-db", required=True, help="Path to an interactions.db")
    parser.add_argument("--crm-db", required=True, help="Path to a crm.db")
    parser.add_argument("--vault", type=Path, help="Vault root, for reading note frontmatter")
    parser.add_argument("--rollback", action="store_true", help="Reverse the migration")
    args = parser.parse_args()

    for db_path in (args.interactions_db, args.crm_db):
        if not Path(db_path).is_file():
            parser.error(f"no such database: {db_path}")
    if not args.rollback and args.vault is None:
        parser.error("--vault is required to migrate")

    migration = _load_migration_module()
    before = _snapshot(args)
    if args.rollback:
        print("interactions rows relabelled:", migration.rollback_interactions_db(args.interactions_db))
        print("source entity rows relabelled:", migration.rollback_crm_db(args.crm_db))
    else:
        print("interactions:", asdict(migration.migrate_interactions_db(args.interactions_db, args.vault)))
        print("crm:", asdict(migration.migrate_crm_db(args.crm_db, args.vault)))
    _report(before, _snapshot(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
