#!/usr/bin/env python3
"""
Sync WhatsApp data to LifeOS CRM via wacli.

Requires wacli to be installed and authenticated:
  brew install steipete/tap/wacli
  wacli auth

Syncs:
1. WhatsApp contacts as SourceEntities (with phone numbers and names)
2. WhatsApp messages as interactions (DMs and group chats)
3. Group memberships (for relationship discovery)
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import subprocess
import json
import uuid
import sqlite3
import logging
import argparse
import re
from datetime import datetime, timezone

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


def normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format."""
    if not phone:
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


def extract_phone_from_jid(jid: str) -> str:
    """
    Extract phone number from WhatsApp JID.

    JID format: {phone}@s.whatsapp.net for individuals
                {groupid}@g.us for groups
                {lid}@lid for linked devices (not real phones)

    Args:
        jid: WhatsApp JID string

    Returns:
        E.164 formatted phone number or empty string
    """
    if not jid or is_group_jid(jid):
        return ""
    # Skip linked device IDs - they're not real phone numbers
    if '@lid' in jid:
        return ""
    # Extract the part before @
    phone_part = jid.split('@')[0] if '@' in jid else jid
    return normalize_phone(phone_part)


def is_group_jid(jid: str) -> bool:
    """
    Check if JID is a group chat.

    Args:
        jid: WhatsApp JID string

    Returns:
        True if group JID, False otherwise
    """
    if not jid:
        return False
    return '@g.us' in jid


def run_wacli(args: list, timeout: int = 300) -> dict | list | None:
    """
    Run wacli command and return JSON output.

    Args:
        args: Command arguments (without 'wacli' prefix)
        timeout: Timeout in seconds

    Returns:
        Parsed JSON output or None on error
    """
    cmd = ["wacli", "--json"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            if "no store found" in result.stderr.lower() or "not authenticated" in result.stderr.lower():
                logger.error("wacli not authenticated. Run 'wacli auth' first.")
                return None
            logger.error(f"wacli error: {result.stderr}")
            return None

        if not result.stdout.strip():
            return []

        parsed = json.loads(result.stdout)
        # wacli wraps responses in {"success": true, "data": [...], "error": null}
        if isinstance(parsed, dict) and "data" in parsed:
            return parsed["data"] if parsed["data"] is not None else []
        return parsed
    except subprocess.TimeoutExpired:
        logger.error(f"wacli command timed out: {' '.join(cmd)}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse wacli output: {e}")
        return None
    except FileNotFoundError:
        logger.error("wacli not found. Install with: brew install steipete/tap/wacli")
        return None


def check_wacli_auth() -> bool:
    """Check if wacli is authenticated."""
    result = run_wacli(["chats", "list", "--limit", "1"])
    return result is not None


def sync_whatsapp(dry_run: bool = True) -> dict:
    """
    Sync WhatsApp contacts to CRM.

    Args:
        dry_run: If True, don't actually insert

    Returns:
        Stats dict
    """
    stats = {
        'contacts_read': 0,
        'source_entities_created': 0,
        'source_entities_updated': 0,
        'persons_linked': 0,
        'persons_created': 0,
        'persons_updated': 0,
        'skipped': 0,
        'errors': 0,
    }

    # Check authentication
    if not check_wacli_auth():
        stats['error'] = "wacli not authenticated"
        return stats

    source_store = get_source_entity_store()
    person_store = get_person_entity_store()
    resolver = get_entity_resolver()

    # Get all WhatsApp contacts
    logger.info("Fetching WhatsApp contacts...")

    # Search with "." pattern and high limit to get all contacts
    contacts = run_wacli(["contacts", "search", ".", "--limit", "10000"])
    if contacts is None:
        stats['error'] = "Failed to fetch contacts"
        return stats

    stats['contacts_read'] = len(contacts)
    logger.info(f"Found {len(contacts)} contacts")

    for contact in contacts:
        try:
            jid = contact.get("JID", "")
            phone_raw = contact.get("Phone", "")
            name = contact.get("Name", "").strip()
            alias = contact.get("Alias", "").strip()

            # Skip contacts without valid phone
            phone = normalize_phone(phone_raw)
            if not phone:
                stats['skipped'] += 1
                continue

            # Skip contacts without meaningful name
            display_name = name or alias
            if not display_name or display_name == phone_raw:
                stats['skipped'] += 1
                continue

            # Create unique source_id
            source_id = f"whatsapp_{jid}"

            # Check for existing source entity
            existing_source = source_store.get_by_source('whatsapp', source_id)

            source_entity = SourceEntity(
                source_type='whatsapp',
                source_id=source_id,
                observed_name=display_name,
                observed_phone=phone,
                metadata={
                    'jid': jid,
                    'alias': alias,
                    'raw_phone': phone_raw,
                },
                observed_at=datetime.now(timezone.utc),
            )

            if existing_source:
                if not dry_run:
                    existing_source.observed_name = source_entity.observed_name
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

            # Resolve to PersonEntity
            result = resolver.resolve(
                name=display_name,
                phone=phone,
                create_if_missing=True,
            )

            if result and result.entity:
                person = result.entity
                person_updated = False

                # Link source entity to person
                if not existing_source or existing_source.canonical_person_id != person.id:
                    if not dry_run:
                        source_store.link_to_person(
                            source_entity.id,
                            person.id,
                            confidence=0.95,  # High confidence for phone match
                            status=LINK_STATUS_AUTO,
                        )
                    stats['persons_linked'] += 1

                # Add phone to person if not present
                if phone and phone not in person.phone_numbers:
                    person.phone_numbers.append(phone)
                    if not person.phone_primary:
                        person.phone_primary = phone
                    person_updated = True

                # Add source
                if 'whatsapp' not in person.sources:
                    person.sources.append('whatsapp')
                    person_updated = True

                # Update source_entity_count
                if not dry_run:
                    new_count = source_store.count_for_person(person.id)
                    if person.source_entity_count != new_count:
                        person.source_entity_count = new_count
                        person_updated = True

                if person_updated:
                    if not dry_run:
                        person_store.update(person)
                    stats['persons_updated'] += 1

                if result.is_new:
                    stats['persons_created'] += 1

        except Exception as e:
            logger.error(f"Error processing contact: {e}")
            stats['errors'] += 1

    # Save person store
    if not dry_run:
        person_store.save()

    # Log summary
    logger.info(f"\n=== WhatsApp Sync Summary ===")
    logger.info(f"Contacts read: {stats['contacts_read']}")
    logger.info(f"Source entities created: {stats['source_entities_created']}")
    logger.info(f"Source entities updated: {stats['source_entities_updated']}")
    logger.info(f"Persons linked: {stats['persons_linked']}")
    logger.info(f"Persons created: {stats['persons_created']}")
    logger.info(f"Persons updated: {stats['persons_updated']}")
    logger.info(f"Skipped: {stats['skipped']}")
    logger.info(f"Errors: {stats['errors']}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made")

    return stats


def sync_whatsapp_messages(dry_run: bool = True) -> dict:
    """
    Sync WhatsApp messages from wacli.db to interactions.

    Args:
        dry_run: If True, don't actually insert

    Returns:
        Stats dict
    """
    stats = {
        'messages_read': 0,
        'interactions_created': 0,
        'interactions_skipped': 0,
        'skipped_lid': 0,
        'skipped_large_group': 0,
        'persons_not_found': 0,
        'errors': 0,
    }

    # Track affected person_ids for stats refresh
    affected_person_ids: set[str] = set()

    wacli_db_path = Path.home() / ".wacli" / "wacli.db"
    if not wacli_db_path.exists():
        logger.error(f"wacli database not found at {wacli_db_path}")
        stats['error'] = "wacli database not found"
        return stats

    resolver = get_entity_resolver()
    interaction_db = get_interaction_db_path()

    # Connect to wacli database
    wacli_conn = sqlite3.connect(str(wacli_db_path))
    wacli_conn.row_factory = sqlite3.Row
    wacli_cursor = wacli_conn.cursor()

    # Build group size lookup for filtering large groups
    wacli_cursor.execute("""
        SELECT group_jid, COUNT(*) as participant_count
        FROM group_participants
        GROUP BY group_jid
    """)
    group_sizes = {row['group_jid']: row['participant_count'] for row in wacli_cursor.fetchall()}
    logger.info(f"Loaded sizes for {len(group_sizes)} groups")

    # Connect to interaction database
    int_conn = sqlite3.connect(interaction_db)
    int_cursor = int_conn.cursor()

    # Get existing WhatsApp interactions to avoid duplicates
    int_cursor.execute("SELECT source_id FROM interactions WHERE source_type = 'whatsapp'")
    existing_ids = {row[0] for row in int_cursor.fetchall()}
    logger.info(f"Found {len(existing_ids)} existing WhatsApp interactions")

    # Fetch messages from wacli (excluding messages from self)
    wacli_cursor.execute("""
        SELECT
            m.msg_id,
            m.chat_jid,
            m.chat_name,
            m.sender_jid,
            m.sender_name,
            m.ts,
            m.from_me,
            m.text,
            m.display_text,
            m.media_type
        FROM messages m
        WHERE m.from_me = 0
        ORDER BY m.ts DESC
    """)

    messages = wacli_cursor.fetchall()
    stats['messages_read'] = len(messages)
    logger.info(f"Found {len(messages)} incoming WhatsApp messages")

    batch_size = 500
    batch = []

    for msg in messages:
        try:
            msg_id = msg['msg_id']
            source_id = f"whatsapp_{msg_id}"

            # Skip if already exists
            if source_id in existing_ids:
                stats['interactions_skipped'] += 1
                continue

            sender_jid = msg['sender_jid']
            sender_name = msg['sender_name']
            chat_jid = msg['chat_jid']
            chat_name = msg['chat_name']
            timestamp = msg['ts']
            text = msg['display_text'] or msg['text'] or ''
            is_group = is_group_jid(chat_jid)

            # Skip messages from large groups (>20 participants)
            if is_group:
                group_size = group_sizes.get(chat_jid, 0)
                if group_size > 20:
                    stats['skipped_large_group'] += 1
                    continue

            # Skip linked device IDs (not real phone numbers)
            if sender_jid and '@lid' in sender_jid:
                stats['skipped_lid'] += 1
                continue

            # Extract phone from sender JID
            phone = extract_phone_from_jid(sender_jid)
            if not phone:
                stats['interactions_skipped'] += 1
                continue

            # Resolve to PersonEntity
            result = resolver.resolve(
                name=sender_name if sender_name else None,
                phone=phone,
                create_if_missing=True,  # Create new person if not found (matches contact sync)
            )

            if not result or not result.entity:
                stats['persons_not_found'] += 1
                continue

            person_id = result.entity.id

            # Track for stats refresh
            affected_person_ids.add(person_id)

            # Parse timestamp
            try:
                if isinstance(timestamp, str):
                    ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                else:
                    ts = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except Exception:
                ts = datetime.now(timezone.utc)

            # Create interaction record
            interaction_id = str(uuid.uuid4())
            title = f"WhatsApp {'group' if is_group else 'DM'}: {chat_name or sender_name or phone}"
            snippet = text[:500] if text else ""

            batch.append((
                interaction_id,
                person_id,
                ts.isoformat(),
                'whatsapp',
                title,
                snippet,
                None,  # source_link
                source_id,
                datetime.now(timezone.utc).isoformat(),
            ))

            stats['interactions_created'] += 1

            # Insert in batches
            if len(batch) >= batch_size and not dry_run:
                int_cursor.executemany("""
                    INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)
                int_conn.commit()
                logger.info(f"Inserted batch of {len(batch)} interactions")
                batch = []

        except Exception as e:
            logger.error(f"Error processing message {msg['msg_id']}: {e}")
            stats['errors'] += 1

    # Insert remaining batch
    if batch and not dry_run:
        int_cursor.executemany("""
            INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        int_conn.commit()
        logger.info(f"Inserted final batch of {len(batch)} interactions")

    wacli_conn.close()
    int_conn.close()

    # Log summary
    logger.info(f"\n=== WhatsApp Message Sync Summary ===")
    logger.info(f"Messages read: {stats['messages_read']}")
    logger.info(f"Interactions created: {stats['interactions_created']}")
    logger.info(f"Interactions skipped (duplicates): {stats['interactions_skipped']}")
    logger.info(f"Skipped (linked device IDs): {stats['skipped_lid']}")
    logger.info(f"Skipped (large groups >20): {stats['skipped_large_group']}")
    logger.info(f"Persons not found: {stats['persons_not_found']}")
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync WhatsApp contacts and messages to CRM via wacli')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--messages-only', action='store_true', help='Only sync messages, not contacts')
    parser.add_argument('--contacts-only', action='store_true', help='Only sync contacts, not messages')
    args = parser.parse_args()

    if not args.messages_only:
        sync_whatsapp(dry_run=not args.execute)

    if not args.contacts_only:
        sync_whatsapp_messages(dry_run=not args.execute)
