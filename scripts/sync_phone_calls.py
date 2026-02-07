#!/usr/bin/env python3
"""
Sync phone call history to LifeOS CRM.

Reads from macOS CallHistoryDB and creates Interaction records.
Requires Full Disk Access permission for Terminal.
"""
import sqlite3
import uuid
import logging
import argparse
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from api.services.entity_resolver import get_entity_resolver
from api.services.interaction_store import get_interaction_db_path
from api.services.source_entity import (
    get_source_entity_store,
    SourceEntity,
    LINK_STATUS_AUTO,
)
from api.services.person_entity import get_person_entity_store

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# macOS Core Data epoch: 2001-01-01 00:00:00 UTC
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Call types
CALL_TYPE_PHONE = 1
CALL_TYPE_FACETIME_AUDIO = 8
CALL_TYPE_FACETIME_VIDEO = 16

CALL_TYPE_NAMES = {
    CALL_TYPE_PHONE: "Phone",
    CALL_TYPE_FACETIME_AUDIO: "FaceTime Audio",
    CALL_TYPE_FACETIME_VIDEO: "FaceTime Video",
}


def get_callhistory_db_path() -> str:
    """Get path to CallHistoryDB."""
    return str(Path.home() / "Library/Application Support/CallHistoryDB/CallHistory.storedata")


def core_data_to_datetime(timestamp: float) -> datetime:
    """Convert Core Data timestamp to datetime."""
    return CORE_DATA_EPOCH + timedelta(seconds=timestamp)


def normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format."""
    if not phone:
        return ""
    # Skip email addresses (FaceTime)
    if "@" in phone:
        return ""
    # Remove all non-digit characters
    digits = re.sub(r'\D', '', phone)

    # Handle US numbers
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    elif len(digits) > 10:
        return f"+{digits}"
    return ""


def format_duration(seconds: float) -> str:
    """Format duration in human readable format."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def sync_phone_calls(
    days_back: int = 365,
    dry_run: bool = True,
    batch_size: int = 100,
) -> dict:
    """
    Sync phone call history to CRM.

    Args:
        days_back: How many days back to sync
        dry_run: If True, don't actually insert
        batch_size: Batch size for DB inserts

    Returns:
        Stats dict
    """
    stats = {
        'calls_read': 0,
        'interactions_created': 0,
        'source_entities_created': 0,
        'source_entities_updated': 0,
        'persons_linked': 0,
        'already_exists': 0,
        'skipped_facetime_email': 0,
        'no_person': 0,
        'errors': 0,
    }

    # Track affected person_ids for stats refresh
    affected_person_ids: set[str] = set()

    callhistory_path = get_callhistory_db_path()
    if not Path(callhistory_path).exists():
        logger.error(f"CallHistoryDB not found at {callhistory_path}")
        stats['error'] = "CallHistoryDB not found"
        return stats

    try:
        call_conn = sqlite3.connect(f"file:{callhistory_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        if "unable to open" in str(e):
            logger.error("Cannot access CallHistoryDB. Grant Full Disk Access to Terminal.")
            logger.error("System Settings > Privacy & Security > Full Disk Access")
            stats['error'] = "Full Disk Access required"
            return stats
        raise

    db_path = get_interaction_db_path()
    interaction_conn = sqlite3.connect(db_path)
    resolver = get_entity_resolver()
    source_store = get_source_entity_store()
    person_store = get_person_entity_store()

    # Get existing interactions to avoid duplicates
    existing = set()
    cursor = interaction_conn.execute(
        "SELECT source_id FROM interactions WHERE source_type = 'phone'"
    )
    for row in cursor.fetchall():
        if row[0]:
            existing.add(row[0])
    logger.info(f"Found {len(existing)} existing phone interactions")

    # Calculate date range
    after_timestamp = (datetime.now(timezone.utc) - timedelta(days=days_back) - CORE_DATA_EPOCH).total_seconds()

    # Query call records
    query = """
        SELECT
            ZUNIQUE_ID,
            ZDATE,
            ZDURATION,
            ZADDRESS,
            ZNAME,
            ZORIGINATED,
            ZANSWERED,
            ZCALLTYPE
        FROM ZCALLRECORD
        WHERE ZDATE > ?
        ORDER BY ZDATE DESC
    """

    logger.info(f"Reading call history (last {days_back} days)...")
    cursor = call_conn.execute(query, (after_timestamp,))
    calls = cursor.fetchall()
    stats['calls_read'] = len(calls)
    logger.info(f"Found {len(calls)} calls")

    batch = []
    seen_phones = {}  # Track phones for source entity creation

    for row in calls:
        unique_id, zdate, duration, address, name, originated, answered, call_type = row

        # Skip if already exists
        if unique_id in existing:
            stats['already_exists'] += 1
            continue

        # Normalize phone
        phone = normalize_phone(address)
        if not phone:
            # Skip FaceTime email calls for now
            if address and "@" in address:
                stats['skipped_facetime_email'] += 1
            continue

        try:
            # Create/update SourceEntity for this phone
            source_id = f"phone_{phone}"

            if phone not in seen_phones:
                existing_source = source_store.get_by_source('phone', source_id)

                source_entity = SourceEntity(
                    source_type='phone',
                    source_id=source_id,
                    observed_name=name if name else None,
                    observed_phone=phone,
                    metadata={
                        'last_call_type': CALL_TYPE_NAMES.get(call_type, "Unknown"),
                    },
                    observed_at=datetime.now(timezone.utc),
                )

                if existing_source:
                    if not dry_run:
                        existing_source.observed_name = source_entity.observed_name or existing_source.observed_name
                        existing_source.observed_phone = source_entity.observed_phone
                        existing_source.metadata = source_entity.metadata
                        existing_source.observed_at = datetime.now(timezone.utc)
                        source_store.update(existing_source)
                    stats['source_entities_updated'] += 1
                    source_entity = existing_source
                else:
                    if not dry_run:
                        source_entity = source_store.add(source_entity)
                    stats['source_entities_created'] += 1

                seen_phones[phone] = source_entity

            # Resolve to PersonEntity
            result = resolver.resolve(
                name=name if name else None,
                phone=phone,
                create_if_missing=True,
            )

            if not result or not result.entity:
                stats['no_person'] += 1
                continue

            person = result.entity

            # Track for stats refresh
            affected_person_ids.add(person.id)

            # Link source entity to person
            source_entity = seen_phones[phone]
            if source_entity.canonical_person_id != person.id:
                if not dry_run:
                    source_store.link_to_person(
                        source_entity.id,
                        person.id,
                        confidence=0.95,
                        status=LINK_STATUS_AUTO,
                    )
                stats['persons_linked'] += 1

            # Add phone to person if not present
            if phone and phone not in person.phone_numbers:
                person.phone_numbers.append(phone)
                if not person.phone_primary:
                    person.phone_primary = phone
                if not dry_run:
                    person_store.update(person)

            # Add source
            if 'phone' not in person.sources:
                person.sources.append('phone')
                if not dry_run:
                    person_store.update(person)

            # Parse timestamp
            timestamp = core_data_to_datetime(zdate)

            # Create title
            direction = "Outgoing" if originated else "Incoming"
            status = "answered" if answered else "missed"
            call_type_name = CALL_TYPE_NAMES.get(call_type, "Call")
            contact_name = name or person.canonical_name or phone

            if duration and duration > 0:
                title = f"{direction} {call_type_name} with {contact_name} ({format_duration(duration)})"
            else:
                title = f"{direction} {call_type_name} ({status}) - {contact_name}"

            # Create interaction
            interaction_id = str(uuid.uuid4())

            batch.append((
                interaction_id,
                person.id,
                timestamp.isoformat(),
                'phone',
                title,
                None,  # No snippet for calls
                "",    # No source link
                unique_id,
                datetime.now(timezone.utc).isoformat(),
            ))

            if len(batch) >= batch_size:
                if not dry_run:
                    _insert_batch(interaction_conn, batch)
                stats['interactions_created'] += len(batch)
                batch = []

        except Exception as e:
            logger.error(f"Error processing call: {e}")
            stats['errors'] += 1

    # Insert remaining batch
    if batch:
        if not dry_run:
            _insert_batch(interaction_conn, batch)
        stats['interactions_created'] += len(batch)

    # Save person store
    if not dry_run:
        person_store.save()

    call_conn.close()
    interaction_conn.close()

    # Log summary
    logger.info(f"\n=== Phone Calls Sync Summary ===")
    logger.info(f"Calls read: {stats['calls_read']}")
    logger.info(f"Interactions created: {stats['interactions_created']}")
    logger.info(f"Source entities created: {stats['source_entities_created']}")
    logger.info(f"Source entities updated: {stats['source_entities_updated']}")
    logger.info(f"Persons linked: {stats['persons_linked']}")
    logger.info(f"Already exists: {stats['already_exists']}")
    logger.info(f"Skipped FaceTime email: {stats['skipped_facetime_email']}")
    logger.info(f"No person resolved: {stats['no_person']}")
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
    conn.executemany(
        """
        INSERT OR IGNORE INTO interactions
        (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )
    conn.commit()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync phone call history to CRM')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--days', type=int, default=365, help='Days back to sync')
    args = parser.parse_args()

    sync_phone_calls(days_back=args.days, dry_run=not args.execute)
