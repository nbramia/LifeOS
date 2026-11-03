#!/usr/bin/env python3
"""
Fill each person's past organizations from scraped LinkedIn profiles.

Reads data/linkedin_extracted.json: each profile's `person_id` names the person and its
`experience` list (company, start_year, start_month, end_year, end_month) gives the jobs.
Jobs with an end date become past organizations, except at the person's current company or
a job still open. Several profiles for one person, or for an id merged into another, are
combined. A person with a hand-entered organization is left alone, and `company` and
`position` are never changed.

A dry run opens the database read-only. With --execute the database is copied first with
SQLite's backup API, then each changed person is written. Run --execute only after the code
that reads the new column is deployed and restarted: opening the database adds the column,
and a server on older code fails to read every person once it exists, so --execute needs
--code-deployed. Running again changes nothing. Output is aggregate counts only.

Usage:
    python3 scripts/backfill_person_organizations.py            # dry run
    python3 scripts/backfill_person_organizations.py --execute --code-deployed
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services.person_organizations import (  # noqa: E402
    SOURCE_MANUAL,
    derive_profile_organizations,
    normalize_entries,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_CRM_DB = DATA_DIR / "crm.db"
DEFAULT_PROFILES = DATA_DIR / "linkedin_extracted.json"


def load_people(conn: sqlite3.Connection) -> tuple[dict[str, dict], dict[str, str]]:
    """Visible people other than the CRM owner, and where merged-away people went."""
    has_column = any(row[1] == "past_organizations" for row in conn.execute("PRAGMA table_info(person_entities)"))
    held = "past_organizations" if has_column else "'[]'"
    people, merged_into = {}, {}
    for pid, company, hidden, reason, category, past in conn.execute(
        f"SELECT id, company, hidden, hidden_reason, category, {held} FROM person_entities"
    ):
        if hidden and reason and reason.startswith("merged_into:"):
            merged_into[pid] = reason.split(":", 1)[1]
        elif not hidden and category != "self":
            people[pid] = {"company": company, "past": json.loads(past or "[]")}
    return people, merged_into


def load_profiles(path: Optional[Path]) -> list[dict]:
    """Scraped LinkedIn profiles, or none when the file is absent or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []


def plan(people: dict[str, dict], merged_into: dict[str, str], profiles: list[dict]) -> tuple[dict[str, list[dict]], Counter]:
    """The list each person with a profile should hold, and counts."""
    stats: Counter = Counter()
    jobs: dict[str, list] = {}
    for profile in profiles:
        pid = profile.get("person_id")
        pid = merged_into.get(pid, pid)
        if pid not in people:
            stats["profiles_unmatched"] += 1
            continue
        experience = profile.get("experience")
        jobs.setdefault(pid, []).extend(experience if isinstance(experience, list) else [])
    wanted = {}
    for pid, experience in jobs.items():
        if any(e.get("source") == SOURCE_MANUAL for e in people[pid]["past"]):
            stats["people_with_manual_entries_skipped"] += 1
            continue
        wanted[pid] = derive_profile_organizations({"experience": experience}, people[pid]["company"])
        stats["people_with_entries"] += bool(wanted[pid])
        stats["entries"] += len(wanted[pid])
    return wanted, stats


def _backup(db_path: str) -> str:
    """Copy the database beside itself with SQLite's backup API (WAL-safe)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.bak-past-organizations-{stamp}"
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


def backfill(crm_db: str, profiles_path: Optional[str] = None, dry_run: bool = True) -> dict:
    """Run the backfill; returns aggregate counts, plus the backup path when one was written."""
    conn = sqlite3.connect(f"file:{Path(crm_db).resolve()}?mode=ro", uri=True)
    try:
        people, merged_into = load_people(conn)
    finally:
        conn.close()

    wanted, counts = plan(people, merged_into, load_profiles(profiles_path))
    to_write = {pid: entries for pid, entries in wanted.items() if entries != normalize_entries(people[pid]["past"])}
    stats: dict = dict(counts)
    stats["people_to_change"] = len(to_write)
    if dry_run:
        stats["status"] = "dry_run"
        return stats

    stats["backup"] = _backup(str(crm_db))
    from api.services.person_entity import PersonEntityStore

    store = PersonEntityStore(db_path=str(crm_db))
    for pid, entries in to_write.items():
        person = store.get_by_id(pid)
        person.past_organizations = entries
        store.update(person)
    stats["people_written"] = len(to_write)
    stats["status"] = "executed"
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_CRM_DB), help="Path to crm.db")
    parser.add_argument("--profiles", default=str(DEFAULT_PROFILES), help="Scraped LinkedIn profiles JSON")
    parser.add_argument("--execute", action="store_true", help="Back up the database, then write")
    parser.add_argument("--code-deployed", action="store_true", help="Confirm the code that reads past_organizations is deployed; --execute requires it")
    args = parser.parse_args(argv)
    if args.execute and not args.code_deployed:
        parser.error("--execute adds a column that servers on older code cannot read; deploy and restart first, then pass --code-deployed")
    stats = backfill(args.db, args.profiles, dry_run=not args.execute)
    for key in sorted(stats):
        print(f"{key}={stats[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
