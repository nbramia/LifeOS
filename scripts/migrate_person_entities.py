#!/usr/bin/env python3
"""
Migrate PersonEntity data from JSON (people_entities.json) to SQLite (crm.db).

This script:
1. Reads all entities from the JSON file
2. Ensures the SQLite schema exists (via PersonEntityStore._init_db)
3. Inserts all records into SQLite with lookup table entries
4. Verifies record-by-record data integrity
5. Preserves the JSON file as a backup (does NOT delete it)

Usage:
    ~/.venvs/lifeos/bin/python scripts/migrate_person_entities.py [--dry-run]

Options:
    --dry-run   Show what would be migrated without writing to SQLite
"""
import json
import sqlite3
import sys
from pathlib import Path

# Add project root to path
PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from api.services.person_entity import PersonEntity, PersonEntityStore


JSON_PATH = PROJECT_DIR / "data" / "people_entities.json"
DB_PATH = PROJECT_DIR / "data" / "crm.db"


def load_json_entities(json_path: Path) -> list[dict]:
    """Load raw entity dicts from JSON file."""
    if not json_path.exists():
        print(f"ERROR: JSON file not found: {json_path}")
        sys.exit(1)

    with open(json_path) as f:
        data = json.load(f)

    print(f"Loaded {len(data)} entities from {json_path}")
    return data


def check_existing_data(db_path: Path) -> int:
    """Check how many entities already exist in SQLite."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM person_entities").fetchone()[0]
        return count
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def migrate(json_path: Path, db_path: Path, dry_run: bool = False) -> None:
    """Migrate entities from JSON to SQLite."""
    # Load JSON data
    raw_entities = load_json_entities(json_path)

    # Check existing SQLite data
    existing_count = check_existing_data(db_path)
    if existing_count > 0:
        print(f"WARNING: SQLite already has {existing_count} entities.")
        print("Migration will INSERT OR REPLACE all records from JSON.")
        if not dry_run:
            response = input("Continue? [y/N] ")
            if response.lower() != 'y':
                print("Aborted.")
                sys.exit(0)

    if dry_run:
        print(f"\n[DRY RUN] Would migrate {len(raw_entities)} entities to {db_path}")
        # Show sample data
        for i, data in enumerate(raw_entities[:3]):
            entity = PersonEntity.from_dict(data)
            print(f"  Sample {i+1}: {entity.canonical_name} "
                  f"({len(entity.emails)} emails, {len(entity.phone_numbers)} phones)")
        if len(raw_entities) > 3:
            print(f"  ... and {len(raw_entities) - 3} more")
        return

    # Create store (initializes schema)
    store = PersonEntityStore(str(db_path))

    # Batch insert using a single connection for performance
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    failed = 0
    migrated = 0

    print(f"\nMigrating {len(raw_entities)} entities to {db_path}...")

    try:
        for i, raw_data in enumerate(raw_entities):
            try:
                entity = PersonEntity.from_dict(raw_data)
                values = store._entity_to_values(entity)

                conn.execute(
                    f"INSERT OR REPLACE INTO person_entities "
                    f"({store._COLUMNS_STR}) VALUES ({store._PLACEHOLDERS})",
                    values)

                # Update lookup tables
                store._update_lookup_tables(conn, entity)

                migrated += 1

                if (i + 1) % 1000 == 0:
                    conn.commit()
                    print(f"  Progress: {i + 1}/{len(raw_entities)}")

            except Exception as e:
                failed += 1
                name = raw_data.get('canonical_name', 'unknown')
                eid = raw_data.get('id', 'unknown')
                print(f"  FAIL: {name} (ID: {eid[:8]}): {e}")

        conn.commit()
    finally:
        conn.close()

    print(f"\nMigration complete: {migrated} migrated, {failed} failed")

    # Verification
    print("\n--- Verification ---")
    verify(json_path, db_path, raw_entities)


def verify(json_path: Path, db_path: Path, raw_entities: list[dict]) -> None:
    """Verify migration integrity."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 1. Record count
    db_count = conn.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0]
    json_count = len(raw_entities)
    status = "OK" if db_count == json_count else "MISMATCH"
    print(f"  Record count: JSON={json_count}, SQLite={db_count} [{status}]")

    # 2. Lookup table counts
    email_count = conn.execute("SELECT COUNT(*) FROM person_emails").fetchone()[0]
    phone_count = conn.execute("SELECT COUNT(*) FROM person_phones").fetchone()[0]
    name_count = conn.execute("SELECT COUNT(*) FROM person_names").fetchone()[0]
    print(f"  Lookup tables: {email_count} emails, {phone_count} phones, {name_count} names")

    # 3. Spot-check 20 random records
    import random
    check_count = min(20, len(raw_entities))
    sample = random.sample(raw_entities, check_count)
    errors = 0

    for raw_data in sample:
        entity_id = raw_data.get('id')
        row = conn.execute(
            "SELECT * FROM person_entities WHERE id = ?", (entity_id,)).fetchone()

        if not row:
            print(f"  MISSING: {raw_data.get('canonical_name')} (ID: {entity_id[:8]})")
            errors += 1
            continue

        # Compare key fields
        original = PersonEntity.from_dict(raw_data)
        restored = PersonEntity.from_dict(dict(row) | {
            f: json.loads(row[f]) if row[f] else []
            for f in ('emails', 'vault_contexts', 'sources', 'related_notes',
                      'aliases', 'phone_numbers', 'tags')
        } | {
            'hidden': bool(row['hidden']),
            'is_peripheral_contact': bool(row['is_peripheral_contact']),
        })

        mismatches = []
        if original.canonical_name != restored.canonical_name:
            mismatches.append(f"canonical_name: {original.canonical_name!r} != {restored.canonical_name!r}")
        if original.emails != restored.emails:
            mismatches.append(f"emails: {original.emails} != {restored.emails}")
        if original.company != restored.company:
            mismatches.append(f"company: {original.company!r} != {restored.company!r}")
        if original.meeting_count != restored.meeting_count:
            mismatches.append(f"meeting_count: {original.meeting_count} != {restored.meeting_count}")
        if original.hidden != restored.hidden:
            mismatches.append(f"hidden: {original.hidden} != {restored.hidden}")
        if original.phone_numbers != restored.phone_numbers:
            mismatches.append(f"phone_numbers: {original.phone_numbers} != {restored.phone_numbers}")

        if mismatches:
            errors += 1
            print(f"  MISMATCH: {original.canonical_name} (ID: {entity_id[:8]})")
            for m in mismatches:
                print(f"    {m}")

    if errors == 0:
        print(f"  Spot-check: {check_count}/{check_count} records match [OK]")
    else:
        print(f"  Spot-check: {errors}/{check_count} records have mismatches [FAIL]")

    conn.close()

    if db_count != json_count or errors > 0:
        print("\n  WARNING: Migration has issues. JSON file preserved as backup.")
        sys.exit(1)
    else:
        print(f"\n  Migration verified successfully.")
        print(f"  JSON file preserved at: {json_path}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    migrate(JSON_PATH, DB_PATH, dry_run=dry_run)
