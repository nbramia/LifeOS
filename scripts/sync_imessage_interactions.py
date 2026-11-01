#!/usr/bin/env python3
"""
Sync iMessage data to the interactions database.

This script reads linked messages from imessage.db and creates
Interaction records so they appear in person timelines.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
import logging
import uuid
from datetime import datetime, timezone

from api.services.imessage_identity import (
    INCOMING_MARK,
    OUTGOING_MARK,
    claim_source_id,
    NO_TEXT,
    direction_for_is_from_me,
    load_existing,
    make_key,
    match_messages,
    parse_timestamp,
    resolve_people,
)
from api.services.interaction_store import get_interaction_db_path
from api.services.person_entity import get_person_entity_store
from api.services.source_entity import (
    get_source_entity_store,
    create_imessage_source_entity,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def get_imessage_db_path() -> str:
    """Get path to iMessage export database."""
    return str(Path(__file__).parent.parent / "data" / "imessage.db")


def sync_imessage_interactions(dry_run: bool = True, limit: int = None) -> dict:
    """
    Sync iMessage data to interactions database.

    Args:
        dry_run: If True, don't actually insert anything
        limit: Max messages to process (for testing)

    Returns:
        Stats dict
    """
    from api.services.imessage import get_imessage_store, join_imessages_to_entities

    # STEP 1: Export new messages from Apple's Messages database.
    #
    # This only applies when running directly on a macOS host with Full Disk
    # Access granted for ~/Library/Messages/chat.db. On Linux (the
    # maintainer's configured setup) — or a Mac with no Messages database, or
    # no Full Disk Access granted yet — this step is a routine no-op, not an
    # error: on Linux, messages instead arrive via apple_data_import.py's
    # rsync'd imessage.db, and steps 2/3 below process those regardless
    # (the imessage-shaped sibling of the Gmail/Calendar clean-skip pattern).
    #
    # Only these two specific "not configured for this host" signatures are
    # treated as a silent skip. Anything else is left to propagate, so a
    # configured export that genuinely breaks (corrupt db, disk full, ...)
    # still fails the run loud with the real traceback in stderr — not
    # swallowed behind a misleading benign log line.
    logger.info("Exporting new messages from Messages.app database...")
    imessage_store = get_imessage_store()
    export_stats = {}
    if sys.platform != "darwin":
        logger.info(
            "Not running on macOS — iMessage arrives via apple_data_import.py "
            "instead, nothing to export directly here"
        )
    elif not imessage_store.SOURCE_DB_PATH.exists():
        logger.info(f"No Messages.app database at {imessage_store.SOURCE_DB_PATH} — nothing to export")
    else:
        try:
            export_stats = imessage_store.export_from_source()
            logger.info(f"Exported {export_stats['messages_exported']} new messages")
        except PermissionError as e:
            logger.info(
                f"Skipping direct iMessage export — Full Disk Access not granted "
                f"for the Messages database ({e}). Grant Full Disk Access to the "
                "Python binary in System Settings → Privacy & Security, then "
                "see docs/guides/setup.md."
            )

    # STEP 2: Link exported messages to PersonEntity records
    # Always run linking — on Linux, messages arrive via apple_data_import.py
    # (not from step 1), so there may be unlinked messages even when step 1
    # exports 0.
    logger.info("Linking messages to people...")
    try:
        join_stats = join_imessages_to_entities()
        logger.info(f"Linked {join_stats['messages_updated']} messages to people")
    except Exception as e:
        logger.error(f"Failed to link messages: {e}")

    # STEP 3: Sync linked messages to interactions database
    imessage_db = get_imessage_db_path()
    interactions_db = get_interaction_db_path()
    person_store = get_person_entity_store()
    source_entity_store = get_source_entity_store()

    stats = {
        'messages_checked': 0,
        'already_exists': 0,
        'person_not_found': 0,
        'hidden_person': 0,
        'inserted': 0,
        'source_entities_created': 0,
        'errors': 0,
    }

    # Track affected person_ids for stats refresh
    affected_person_ids: set[str] = set()

    # Connect to both databases
    imessage_conn = sqlite3.connect(imessage_db)
    interactions_conn = sqlite3.connect(interactions_db)

    # Existing iMessage interactions, counted per message key (UTC second,
    # direction) and surviving person. Messages are matched to them by key and
    # person, never by source_id.
    existing_counts, taken_source_ids = load_existing(interactions_conn, person_store)

    logger.info(f"Found {sum(existing_counts.values())} existing iMessage interactions")

    # Get linked messages from iMessage database
    query = """
        SELECT rowid, text, timestamp, is_from_me, handle, handle_normalized,
               service, person_entity_id
        FROM messages
        WHERE person_entity_id IS NOT NULL
        ORDER BY timestamp DESC
    """
    if limit:
        query += f" LIMIT {limit}"

    rows = imessage_conn.execute(query).fetchall()

    # One batched lookup for every linked person: the survivor of any merge
    # chain, with its hidden flag.
    people = resolve_people(person_store, (row[7] for row in rows))

    batch = []
    batch_size = 500

    def flush():
        nonlocal batch
        if not batch:
            return
        if dry_run:
            stats['inserted'] += len(batch)
        else:
            inserted = _insert_batch(interactions_conn, batch)
            if inserted != len(batch):
                logger.warning(
                    f"{len(batch) - inserted} of {len(batch)} interactions were not inserted"
                )
            stats['inserted'] += inserted
        logger.info(f"Processed {stats['messages_checked']} messages, inserted {stats['inserted']}")
        batch = []

    # Linked, non-hidden messages with a usable timestamp, in store order.
    eligible = []  # (row, person, ts, key)
    for row in rows:
        _rowid, text, timestamp, is_from_me, handle, handle_normalized, service, person_id = row
        stats['messages_checked'] += 1

        # Verify person still exists; merged ids resolve to the survivor
        person = people.get(person_id)
        if not person:
            stats['person_not_found'] += 1
            continue

        # Parse timestamp
        ts = parse_timestamp(timestamp)
        if ts is None:
            stats['errors'] += 1
            continue

        key = make_key(ts, direction_for_is_from_me(is_from_me))

        # Hidden people get no interactions (consistency verification removes
        # them); their handle is still linked to them.
        if person.hidden:
            stats['hidden_person'] += 1
            if not dry_run and handle:
                stats['source_entities_created'] += _link_source_entity(
                    source_entity_store, person, handle, handle_normalized, service, ts,
                )
            continue

        eligible.append((row, person, ts, key))

    # Messages already synced are matched to existing interactions: same
    # person first, then leftover interactions at the key from any person.
    matched = match_messages(
        [(key, person.id) for _row, person, _ts, key in eligible], existing_counts
    )
    stats['already_exists'] = sum(matched)

    for (row, person, ts, key), is_synced in zip(eligible, matched):
        if is_synced:
            continue
        _rowid, text, _timestamp, is_from_me, handle, handle_normalized, service, _pid = row
        person_id = person.id

        # Create title from message text
        text_preview = (text or "").strip()
        if len(text_preview) > 100:
            text_preview = text_preview[:97] + "..."
        if not text_preview:
            text_preview = NO_TEXT

        # Direction indicator
        mark = OUTGOING_MARK if is_from_me else INCOMING_MARK
        title = f"{mark} {text_preview}"

        # Create interaction record
        interaction_id = str(uuid.uuid4())
        batch.append((
            interaction_id,
            person_id,
            ts.isoformat(),
            'imessage',
            title,
            text[:200] if text else None,  # snippet
            '',  # source_link (no web link for iMessage)
            claim_source_id(key, taken_source_ids),
            datetime.now(timezone.utc).isoformat(),
        ))

        # Track this person for stats refresh
        affected_person_ids.add(person_id)

        # Create source entity for this handle (deduped by add_or_update)
        if not dry_run and handle:
            stats['source_entities_created'] += _link_source_entity(
                source_entity_store, person, handle, handle_normalized, service, ts,
            )

        # Insert in batches
        if len(batch) >= batch_size:
            flush()

    # Insert remaining
    flush()

    if not dry_run:
        interactions_conn.commit()

    imessage_conn.close()
    interactions_conn.close()

    logger.info("\n=== iMessage Sync Summary ===")
    logger.info(f"Messages checked: {stats['messages_checked']}")
    logger.info(f"Already exists: {stats['already_exists']}")
    logger.info(f"Person not found: {stats['person_not_found']}")
    logger.info(f"Hidden person (skipped): {stats['hidden_person']}")
    logger.info(f"Inserted: {stats['inserted']}")
    logger.info(f"Source entities created: {stats['source_entities_created']}")
    logger.info(f"Errors: {stats['errors']}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made")
    else:
        # Refresh PersonEntity stats for all affected people
        if affected_person_ids:
            from api.services.person_stats import refresh_person_stats
            logger.info(f"Refreshing stats for {len(affected_person_ids)} affected people...")
            refresh_person_stats(list(affected_person_ids))

    # Canonical line consumed by run_all_syncs._parse_sync_output. Existing
    # human-readable lines above are kept; the parser uses this dict as truth.
    from api.services.sync_health import emit_sync_stats
    emit_sync_stats({
        "interactions_created": stats["inserted"],
        "source_entities_created": stats["source_entities_created"],
        "errors": stats["errors"],
    })

    return stats


def _link_source_entity(source_entity_store, person, handle, handle_normalized, service, observed_at) -> int:
    """Link this handle's source entity to ``person``; 1 if newly created, else 0."""
    source_entity = create_imessage_source_entity(
        handle=handle_normalized or handle,
        display_name=person.canonical_name,
        observed_at=observed_at,
        metadata={"service": service},
    )
    source_entity.canonical_person_id = person.id
    source_entity.link_confidence = 1.0
    source_entity.link_method = "imessage_handle"
    source_entity.linked_at = datetime.now(timezone.utc)
    _, created = source_entity_store.add_or_update(source_entity)
    return 1 if created else 0


def _insert_batch(conn: sqlite3.Connection, batch: list) -> int:
    """Insert a batch of interactions; returns the number of rows actually inserted."""
    before = conn.total_changes
    conn.executemany("""
        INSERT OR IGNORE INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, batch)
    return conn.total_changes - before


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Sync iMessage to interactions')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--limit', type=int, help='Limit number of messages (for testing)')
    args = parser.parse_args()

    sync_imessage_interactions(dry_run=not args.execute, limit=args.limit)
