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

    # STEP 1: Export new messages from Apple's Messages database
    logger.info("Exporting new messages from Messages.app database...")
    imessage_store = get_imessage_store()
    try:
        export_stats = imessage_store.export_from_source()
        logger.info(f"Exported {export_stats['messages_exported']} new messages")
    except Exception as e:
        logger.error(f"Failed to export messages: {e}")
        # Continue anyway - we can still sync existing messages

    # STEP 2: Link exported messages to PersonEntity records
    if export_stats.get('messages_exported', 0) > 0:
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
        'inserted': 0,
        'source_entities_created': 0,
        'errors': 0,
    }

    # Track affected person_ids for stats refresh
    affected_person_ids: set[str] = set()

    # Connect to both databases
    imessage_conn = sqlite3.connect(imessage_db)
    interactions_conn = sqlite3.connect(interactions_db)

    # Get existing iMessage interactions to avoid duplicates
    existing = set()
    cursor = interactions_conn.execute(
        "SELECT source_id FROM interactions WHERE source_type = 'imessage'"
    )
    for row in cursor.fetchall():
        existing.add(row[0])

    logger.info(f"Found {len(existing)} existing iMessage interactions")

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

    cursor = imessage_conn.execute(query)

    batch = []
    batch_size = 500

    for row in cursor.fetchall():
        rowid, text, timestamp, is_from_me, handle, handle_normalized, service, person_id = row
        stats['messages_checked'] += 1

        # Create unique source_id from rowid
        source_id = f"imessage_{rowid}"

        # Skip if already exists
        if source_id in existing:
            stats['already_exists'] += 1
            continue

        # Verify person still exists
        person = person_store.get_by_id(person_id)
        if not person:
            stats['person_not_found'] += 1
            continue

        # Parse timestamp
        try:
            ts = datetime.fromisoformat(timestamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            stats['errors'] += 1
            continue

        # Create title from message text
        text_preview = (text or "").strip()
        if len(text_preview) > 100:
            text_preview = text_preview[:97] + "..."
        if not text_preview:
            text_preview = "[No text content]"

        # Direction indicator
        direction = "→" if is_from_me else "←"
        title = f"{direction} {text_preview}"

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
            source_id,
            datetime.now(timezone.utc).isoformat(),
        ))

        # Track this person for stats refresh
        affected_person_ids.add(person_id)

        # Create source entity for this handle (deduped by add_or_update)
        if not dry_run and handle:
            source_entity = create_imessage_source_entity(
                handle=handle_normalized or handle,
                display_name=person.canonical_name,
                observed_at=ts,
                metadata={"service": service},
            )
            source_entity.canonical_person_id = person_id
            source_entity.link_confidence = 1.0
            source_entity.linked_at = datetime.now(timezone.utc)
            source_entity_store.add_or_update(source_entity)
            stats['source_entities_created'] += 1

        # Insert in batches
        if len(batch) >= batch_size:
            if not dry_run:
                _insert_batch(interactions_conn, batch)
            stats['inserted'] += len(batch)
            logger.info(f"Processed {stats['messages_checked']} messages, inserted {stats['inserted']}")
            batch = []

    # Insert remaining
    if batch:
        if not dry_run:
            _insert_batch(interactions_conn, batch)
        stats['inserted'] += len(batch)

    if not dry_run:
        interactions_conn.commit()

    imessage_conn.close()
    interactions_conn.close()

    logger.info(f"\n=== iMessage Sync Summary ===")
    logger.info(f"Messages checked: {stats['messages_checked']}")
    logger.info(f"Already exists: {stats['already_exists']}")
    logger.info(f"Person not found: {stats['person_not_found']}")
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

    return stats


def _insert_batch(conn: sqlite3.Connection, batch: list):
    """Insert a batch of interactions."""
    conn.executemany("""
        INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, batch)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Sync iMessage to interactions')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--limit', type=int, help='Limit number of messages (for testing)')
    args = parser.parse_args()

    sync_imessage_interactions(dry_run=not args.execute, limit=args.limit)
