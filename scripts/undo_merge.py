#!/usr/bin/env python3
"""
Undo a person merge by restoring the soft-deleted secondary entity.

Reverses the merge by:
1. Restoring the secondary PersonEntity (unhiding)
2. Re-pointing source_entities back to the secondary based on identifier matching
3. Re-pointing interactions back based on source_entity reversal
4. Removing the secondary's identifiers from the primary
5. Removing the merge chain entry from merged_person_ids.json
6. Refreshing stats for both entities

Usage:
    python scripts/undo_merge.py --secondary <id> [--execute]
    python scripts/undo_merge.py --list-recoverable
"""
import sys
import json
import sqlite3
import logging
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import get_person_entity_store
from api.services.interaction_store import get_interaction_db_path
from api.services.source_entity import get_crm_db_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MERGED_IDS_FILE = Path(__file__).parent.parent / "data" / "merged_person_ids.json"


def load_merged_ids() -> dict:
    if MERGED_IDS_FILE.exists():
        with open(MERGED_IDS_FILE) as f:
            return json.load(f)
    return {}


def save_merged_ids(merged_ids: dict):
    MERGED_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MERGED_IDS_FILE, "w") as f:
        json.dump(merged_ids, f, indent=2)


def list_recoverable():
    """List all soft-deleted merge entities that can be recovered."""
    crm_path = get_crm_db_path()
    conn = sqlite3.connect(crm_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, canonical_name, hidden_at, hidden_reason
        FROM person_entities
        WHERE hidden = 1 AND hidden_reason LIKE 'merged_into:%'
        ORDER BY hidden_at DESC
    """).fetchall()
    conn.close()

    if not rows:
        logger.info("No recoverable merged entities found.")
        return

    logger.info(f"Found {len(rows)} recoverable merged entities:\n")
    for row in rows:
        primary_id = row["hidden_reason"].replace("merged_into:", "")
        # Look up primary name
        store = get_person_entity_store()
        primary = store.get_by_id(primary_id)
        primary_name = primary.canonical_name if primary else "(deleted)"

        logger.info(
            f"  {row['id'][:12]}...  {row['canonical_name']}"
            f"  -> merged into {primary_name} ({primary_id[:12]}...)"
            f"  on {row['hidden_at']}"
        )


def undo_merge(secondary_id: str, dry_run: bool = True) -> dict:
    """
    Undo a merge by restoring the soft-deleted secondary entity.

    Args:
        secondary_id: ID of the secondary (merged) entity to restore
        dry_run: If True, show what would happen without making changes

    Returns:
        Stats dict with counts of changes made
    """
    stats = {
        "source_entities_moved": 0,
        "interactions_moved": 0,
        "emails_removed_from_primary": 0,
        "phones_removed_from_primary": 0,
    }

    crm_path = get_crm_db_path()
    int_path = get_interaction_db_path()

    # 1. Find the soft-deleted secondary (raw query to bypass merge chain)
    conn = sqlite3.connect(crm_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM person_entities WHERE id = ? AND hidden = 1 AND hidden_reason LIKE 'merged_into:%'",
        (secondary_id,),
    ).fetchone()
    conn.close()

    if not row:
        logger.error(f"No soft-deleted merge entity found with ID: {secondary_id}")
        return stats

    secondary_name = row["canonical_name"]
    primary_id = row["hidden_reason"].replace("merged_into:", "")
    secondary_emails = json.loads(row["emails"]) if row["emails"] else []
    secondary_phones = json.loads(row["phone_numbers"]) if row["phone_numbers"] else []

    logger.info(f"Undoing merge: '{secondary_name}' ({secondary_id[:12]}...)")
    logger.info(f"  Was merged into: {primary_id[:12]}...")

    # 2. Verify primary exists
    store = get_person_entity_store()
    primary = store.get_by_id(primary_id)
    if not primary:
        logger.error(f"Primary entity {primary_id} not found. Cannot undo.")
        return stats

    logger.info(f"  Primary: '{primary.canonical_name}'")

    # 3. Find source_entities to move back (those on primary matching secondary's identifiers)
    conn = sqlite3.connect(crm_path)
    se_to_move = []
    secondary_emails_lower = {e.lower() for e in secondary_emails}
    secondary_phones_set = set(secondary_phones)

    rows = conn.execute(
        "SELECT id, observed_email, observed_phone, observed_name FROM source_entities WHERE canonical_person_id = ?",
        (primary_id,),
    ).fetchall()

    for se_row in rows:
        se_id, obs_email, obs_phone, obs_name = se_row
        match = False
        if obs_email and obs_email.lower() in secondary_emails_lower:
            match = True
        elif obs_phone and obs_phone in secondary_phones_set:
            match = True
        if match:
            se_to_move.append(se_id)

    logger.info(f"  Source entities to move back: {len(se_to_move)}")
    stats["source_entities_moved"] = len(se_to_move)

    # 4. Find interactions to move back (matching source_entities)
    int_conn = sqlite3.connect(int_path)
    int_to_move = []
    if se_to_move:
        # Get source_ids for the source_entities we're moving back
        placeholders = ",".join("?" for _ in se_to_move)
        se_source_ids = conn.execute(
            f"SELECT source_id FROM source_entities WHERE id IN ({placeholders})",
            se_to_move,
        ).fetchall()
        source_id_set = {r[0] for r in se_source_ids if r[0]}

        if source_id_set:
            # Find interactions on the primary with matching source_ids
            for source_id in source_id_set:
                int_rows = int_conn.execute(
                    "SELECT id FROM interactions WHERE person_id = ? AND source_id = ?",
                    (primary_id, source_id),
                ).fetchall()
                int_to_move.extend([r[0] for r in int_rows])

    logger.info(f"  Interactions to move back: {len(int_to_move)}")
    stats["interactions_moved"] = len(int_to_move)

    # 5. Determine identifiers to remove from primary
    primary_emails_lower = {e.lower() for e in (primary.emails or [])}
    primary_phones_set = set(primary.phone_numbers or [])

    emails_to_remove = secondary_emails_lower & primary_emails_lower
    phones_to_remove = secondary_phones_set & primary_phones_set
    stats["emails_removed_from_primary"] = len(emails_to_remove)
    stats["phones_removed_from_primary"] = len(phones_to_remove)

    logger.info(f"  Emails to remove from primary: {emails_to_remove or 'none'}")
    logger.info(f"  Phones to remove from primary: {phones_to_remove or 'none'}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")
        conn.close()
        int_conn.close()
        return stats

    # --- Execute changes ---
    logger.info("\nApplying changes...")

    # 5a. Restore secondary entity
    conn.execute(
        "UPDATE person_entities SET hidden = 0, hidden_at = NULL, hidden_reason = '' WHERE id = ?",
        (secondary_id,),
    )
    # Rebuild lookup tables for secondary
    for email in secondary_emails:
        conn.execute(
            "INSERT OR REPLACE INTO person_emails (email, person_id) VALUES (?, ?)",
            (email.lower(), secondary_id),
        )
    for phone in secondary_phones:
        conn.execute(
            "INSERT OR REPLACE INTO person_phones (phone, person_id) VALUES (?, ?)",
            (phone, secondary_id),
        )
    conn.execute(
        "INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
        (secondary_name.lower(), secondary_id),
    )
    logger.info(f"  Restored secondary entity: {secondary_name}")

    # 5b. Move source_entities back
    if se_to_move:
        for se_id in se_to_move:
            conn.execute(
                "UPDATE source_entities SET canonical_person_id = ? WHERE id = ?",
                (secondary_id, se_id),
            )
        logger.info(f"  Moved {len(se_to_move)} source_entities back to secondary")

    # 5c. Move interactions back
    if int_to_move:
        for int_id in int_to_move:
            int_conn.execute(
                "UPDATE interactions SET person_id = ? WHERE id = ?",
                (secondary_id, int_id),
            )
        int_conn.commit()
        logger.info(f"  Moved {len(int_to_move)} interactions back to secondary")
    int_conn.close()

    # 5d. Remove secondary's identifiers from primary
    remaining_emails = [e for e in (primary.emails or []) if e.lower() not in emails_to_remove]
    remaining_phones = [p for p in (primary.phone_numbers or []) if p not in phones_to_remove]
    conn.execute(
        "UPDATE person_entities SET emails = ?, phone_numbers = ? WHERE id = ?",
        (json.dumps(remaining_emails), json.dumps(remaining_phones), primary_id),
    )
    # Rebuild primary lookup tables
    conn.execute("DELETE FROM person_emails WHERE person_id = ?", (primary_id,))
    for email in remaining_emails:
        conn.execute(
            "INSERT OR REPLACE INTO person_emails (email, person_id) VALUES (?, ?)",
            (email.lower(), primary_id),
        )
    conn.execute("DELETE FROM person_phones WHERE person_id = ?", (primary_id,))
    for phone in remaining_phones:
        conn.execute(
            "INSERT OR REPLACE INTO person_phones (phone, person_id) VALUES (?, ?)",
            (phone, primary_id),
        )
    logger.info("  Updated primary identifiers")

    conn.commit()
    conn.close()

    # 5e. Remove from merged_person_ids.json
    merged_ids = load_merged_ids()
    if secondary_id in merged_ids:
        del merged_ids[secondary_id]
        save_merged_ids(merged_ids)
        logger.info("  Removed merge chain entry")

    # 5f. Refresh stats
    from api.services.person_stats import refresh_person_stats
    refresh_person_stats([primary_id, secondary_id])
    logger.info("  Refreshed stats for both entities")

    logger.info("\nUndo merge complete.")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Undo a person merge")
    parser.add_argument("--secondary", help="ID of the merged (secondary) entity to restore")
    parser.add_argument("--list-recoverable", action="store_true", help="List all recoverable merges")
    parser.add_argument("--execute", action="store_true", help="Actually make changes (default is dry run)")

    args = parser.parse_args()

    if args.list_recoverable:
        list_recoverable()
        return

    if not args.secondary:
        parser.error("--secondary is required (or use --list-recoverable)")

    undo_merge(args.secondary, dry_run=not args.execute)


if __name__ == "__main__":
    main()
