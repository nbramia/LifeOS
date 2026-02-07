#!/usr/bin/env python3
"""
Sync Gmail and Calendar interactions to the interactions database.

Creates Interaction records for:
- Emails (both sent and received)
- Calendar events (for each attendee)

Syncs from both personal and work Google accounts.
"""
import sqlite3
import uuid
import logging
import argparse
import time
from datetime import datetime, timedelta, timezone

from api.services.gmail import GmailService
from api.services.calendar import CalendarService
from api.services.google_auth import GoogleAccount
from api.services.entity_resolver import get_entity_resolver
from api.services.person_entity import get_person_entity_store
from api.services.interaction_store import get_interaction_db_path
from api.services.source_entity import (
    get_source_entity_store,
    create_gmail_source_entity,
    create_calendar_source_entity,
)
from config.settings import settings
from config.marketing_patterns import (
    MARKETING_EMAIL_PREFIXES,
    MARKETING_NAME_PATTERNS,
    COMMERCIAL_SENDER_SUBSTRINGS,
    MARKETING_DOMAINS,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def is_marketing_email(email: str, sender_name: str = None) -> bool:
    """
    Check if an email address or sender name indicates marketing/automated sender.

    Args:
        email: Email address to check
        sender_name: Optional sender display name to check

    Returns:
        True if likely marketing/automated, False if likely a real person
    """
    # Check sender name for marketing patterns
    if sender_name:
        name_lower = sender_name.lower()
        for pattern in MARKETING_NAME_PATTERNS:
            if pattern in name_lower:
                return True
        # Check for commercial sender substrings in name
        for substring in COMMERCIAL_SENDER_SUBSTRINGS:
            if substring in name_lower:
                return True

    if not email or '@' not in email:
        return False

    email_lower = email.lower()

    # Check for commercial sender substrings anywhere in email
    for substring in COMMERCIAL_SENDER_SUBSTRINGS:
        if substring in email_lower:
            return True
    prefix = email_lower.split('@')[0]
    domain = email_lower.split('@')[1]

    # Check if prefix matches marketing patterns
    # Handle variations like "noreply+abc@" or "newsletter123@"
    prefix_base = prefix.split('+')[0]  # Strip plus addressing
    for pattern in MARKETING_EMAIL_PREFIXES:
        if prefix_base == pattern or prefix_base.startswith(pattern + '-') or prefix_base.startswith(pattern + '_'):
            return True

    # Check if domain is a known marketing domain
    # Also check parent domain (e.g., mail.company.com -> company.com)
    domain_parts = domain.split('.')
    for i in range(len(domain_parts) - 1):
        check_domain = '.'.join(domain_parts[i:])
        if check_domain in MARKETING_DOMAINS:
            return True

    return False


def sync_gmail_interactions(
    account_type: GoogleAccount,
    days_back: int = 365,
    dry_run: bool = True,
    batch_size: int = 100,
    domain_filter: str | None = None,
    before_days: int | None = None,
) -> dict:
    """
    Sync Gmail interactions for an account.

    Args:
        account_type: Which Google account to use
        days_back: How many days back to sync
        dry_run: If True, don't actually insert
        domain_filter: Optional email domain to filter (e.g., "gmail.com")

    Returns:
        Stats dict
    """
    stats = {
        'fetched': 0,
        'inserted': 0,
        'already_exists': 0,
        'no_person': 0,
        'errors': 0,
        'source_entities_created': 0,
        'marketing_skipped': 0,
    }

    # Track affected person_ids for stats refresh
    affected_person_ids: set[str] = set()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    resolver = get_entity_resolver()
    source_entity_store = get_source_entity_store()
    my_person_id = settings.my_person_id

    # Get existing interactions to avoid duplicates
    # We track both full source_ids AND base message_ids for efficient skipping
    existing = set()  # Full source_ids (for sent emails with :recipient suffix)
    existing_base_ids = set()  # Base message_ids (for fast initial skip check)
    cursor = conn.execute(
        "SELECT source_id FROM interactions WHERE source_type = 'gmail'"
    )
    for row in cursor.fetchall():
        if row[0]:
            source_id = row[0]
            existing.add(source_id)
            # Extract base message_id (strip :email suffix if present)
            base_id = source_id.split(':')[0] if ':' in source_id else source_id
            existing_base_ids.add(base_id)
    logger.info(f"Found {len(existing)} existing gmail interactions ({len(existing_base_ids)} unique message IDs)")

    gmail = GmailService(account_type=account_type)
    after_date = datetime.now(timezone.utc) - timedelta(days=days_back)
    before_date = datetime.now(timezone.utc) - timedelta(days=before_days) if before_days else None

    # Fetch emails in batches using search
    # Gmail API limits to 500 results per query, so we paginate
    try:
        # Build search query
        query = f"after:{after_date.strftime('%Y/%m/%d')}"
        if before_date:
            query += f" before:{before_date.strftime('%Y/%m/%d')}"
        if domain_filter:
            # Filter to emails from/to the specified domain
            query += f" (from:*@{domain_filter} OR to:*@{domain_filter})"

        # Log what we're syncing
        if before_date:
            logger.info(f"Fetching emails from {account_type.value} account ({after_date.strftime('%Y-%m-%d')} to {before_date.strftime('%Y-%m-%d')})...")
        elif domain_filter:
            logger.info(f"Fetching emails from {account_type.value} account (last {days_back} days, domain: {domain_filter})...")
        else:
            logger.info(f"Fetching emails from {account_type.value} account (last {days_back} days)...")

        # Search for emails matching query
        result = gmail.service.users().messages().list(
            userId="me",
            q=query,
            maxResults=500,
        ).execute()

        messages = result.get("messages", [])
        next_page_token = result.get("nextPageToken")

        while next_page_token:
            result = gmail.service.users().messages().list(
                userId="me",
                q=query,
                maxResults=500,
                pageToken=next_page_token,
            ).execute()
            messages.extend(result.get("messages", []))
            next_page_token = result.get("nextPageToken")

            if len(messages) % 1000 == 0:
                logger.info(f"  Fetched {len(messages)} message IDs...")

        logger.info(f"Found {len(messages)} total messages")
        stats['fetched'] = len(messages)

        batch = []
        processed = 0

        logger.info(f"Starting to process messages (existing base IDs: {len(existing_base_ids)})...")
        checked = 0
        for msg_data in messages:
            message_id = msg_data["id"]
            checked += 1

            # Log first message to confirm loop started
            if checked == 1:
                logger.info(f"  Processing first message: {message_id[:8]}...")
                in_existing = message_id in existing_base_ids
                logger.info(f"    Message in existing_base_ids: {in_existing}")

            # Progress logging for checking phase
            if checked % 5000 == 0:
                logger.info(f"  Checked {checked}/{len(messages)} messages (skipped {stats['already_exists']} existing)...")

            # Skip if this message was already processed (check base ID for sent emails too)
            if message_id in existing_base_ids:
                stats['already_exists'] += 1
                continue

            # Log when fetching new message
            if checked <= 5:
                logger.info(f"    Fetching new message {message_id[:8]}...")

            try:
                # Fetch message details (metadata only for speed)
                email = gmail.get_message(message_id, include_body=False)
                if not email:
                    stats['errors'] += 1
                    continue

                # Skip marketing/promotional emails (check both email and sender name)
                if is_marketing_email(email.sender, email.sender_name):
                    stats['marketing_skipped'] += 1
                    continue

                # Resolve the sender
                sender_result = resolver.resolve(
                    name=email.sender_name if email.sender_name != email.sender else None,
                    email=email.sender,
                    create_if_missing=True,
                )
                sender_person_id = sender_result.entity.id if sender_result and sender_result.entity else None

                timestamp = email.date.isoformat()
                source_link = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"
                subject = email.subject or "(No Subject)"
                snippet = email.snippet[:200] if email.snippet else None

                # Determine if this is a received email (sender != me) or sent email (sender == me)
                if sender_person_id and sender_person_id != my_person_id:
                    # RECEIVED EMAIL: Create interactions with sender AND all To/CC recipients
                    # This captures everyone involved in the email thread

                    # 1. Create interaction with the sender (use base message_id)
                    if message_id not in existing:
                        batch.append((
                            str(uuid.uuid4()),
                            sender_person_id,
                            timestamp,
                            'gmail',
                            f"← {subject}",
                            snippet,
                            source_link,
                            message_id,
                            datetime.now(timezone.utc).isoformat(),
                            account_type.value,  # source_account: "personal" or "work"
                            None,  # attendee_count: N/A for gmail
                        ))
                        affected_person_ids.add(sender_person_id)
                        existing.add(message_id)

                        # Create source entity for the sender
                        if not dry_run:
                            source_entity = create_gmail_source_entity(
                                message_id=message_id,
                                sender_email=email.sender,
                                sender_name=email.sender_name if email.sender_name != email.sender else None,
                                observed_at=email.date,
                                metadata={"subject": subject[:100] if subject else None},
                            )
                            source_entity.canonical_person_id = sender_person_id
                            source_entity.link_confidence = sender_result.confidence if sender_result else 1.0
                            source_entity.linked_at = datetime.now(timezone.utc)
                            source_entity_store.add_or_update(source_entity)
                            stats['source_entities_created'] += 1

                    # 2. Create interactions with To/CC recipients (excluding myself)
                    # These are people I was on an email thread with
                    other_participants = []
                    if email.to:
                        other_participants.extend(_parse_email_addresses(email.to))
                    if email.cc:
                        other_participants.extend(_parse_email_addresses(email.cc))

                    for participant_name, participant_email in other_participants:
                        # Skip marketing/automated addresses
                        if is_marketing_email(participant_email, participant_name):
                            continue

                        # Use source_id format: message_id:cc:email for CC participants
                        participant_source_id = f"{message_id}:cc:{participant_email}"
                        if participant_source_id in existing:
                            continue

                        participant_result = resolver.resolve(
                            name=participant_name,
                            email=participant_email,
                            create_if_missing=True,
                        )
                        if not participant_result or not participant_result.entity:
                            continue
                        if participant_result.entity.id == my_person_id:
                            continue  # Skip myself
                        if participant_result.entity.id == sender_person_id:
                            continue  # Already created interaction with sender

                        # Use ↔ arrow to indicate shared thread (not direct send/receive)
                        batch.append((
                            str(uuid.uuid4()),
                            participant_result.entity.id,
                            timestamp,
                            'gmail',
                            f"↔ {subject}",
                            snippet,
                            source_link,
                            participant_source_id,
                            datetime.now(timezone.utc).isoformat(),
                            account_type.value,  # source_account: "personal" or "work"
                            None,  # attendee_count: N/A for gmail
                        ))
                        affected_person_ids.add(participant_result.entity.id)
                        existing.add(participant_source_id)

                        # Create source entity for the participant
                        if not dry_run:
                            source_entity = create_gmail_source_entity(
                                message_id=participant_source_id,
                                sender_email=participant_email,
                                sender_name=participant_name,
                                observed_at=email.date,
                                metadata={"subject": subject[:100] if subject else None, "role": "cc"},
                            )
                            source_entity.canonical_person_id = participant_result.entity.id
                            source_entity.link_confidence = participant_result.confidence if participant_result else 1.0
                            source_entity.linked_at = datetime.now(timezone.utc)
                            source_entity_store.add_or_update(source_entity)
                            stats['source_entities_created'] += 1
                else:
                    # SENT EMAIL: Create interactions for ALL recipients (To + CC)
                    recipients = []

                    # Parse To recipients
                    if email.to:
                        recipients.extend(_parse_email_addresses(email.to))

                    # Parse CC recipients
                    if email.cc:
                        recipients.extend(_parse_email_addresses(email.cc))

                    if not recipients:
                        stats['no_person'] += 1
                        continue

                    created_for_this_email = 0
                    for recipient_name, recipient_email in recipients:
                        # Skip marketing/promotional recipients
                        if is_marketing_email(recipient_email, recipient_name):
                            stats['marketing_skipped'] += 1
                            continue

                        # Skip duplicates within same email
                        source_id = f"{message_id}:{recipient_email}"
                        if source_id in existing:
                            stats['already_exists'] += 1
                            continue

                        recipient_result = resolver.resolve(
                            name=recipient_name,
                            email=recipient_email,
                            create_if_missing=True,
                        )
                        if not recipient_result or not recipient_result.entity:
                            continue
                        if recipient_result.entity.id == my_person_id:
                            continue  # Skip myself

                        # Use → arrow to indicate sent (like iMessage)
                        batch.append((
                            str(uuid.uuid4()),
                            recipient_result.entity.id,
                            timestamp,
                            'gmail',
                            f"→ {subject}",
                            snippet,
                            source_link,
                            source_id,  # Use email-specific source_id for deduplication
                            datetime.now(timezone.utc).isoformat(),
                            account_type.value,  # source_account: "personal" or "work"
                            None,  # attendee_count: N/A for gmail
                        ))
                        created_for_this_email += 1
                        # Track for stats refresh
                        affected_person_ids.add(recipient_result.entity.id)

                        # Create source entity for the recipient
                        if not dry_run:
                            source_entity = create_gmail_source_entity(
                                message_id=source_id,  # Use email-specific source_id
                                sender_email=recipient_email,
                                sender_name=recipient_name,
                                observed_at=email.date,
                                metadata={"subject": subject[:100] if subject else None},
                            )
                            source_entity.canonical_person_id = recipient_result.entity.id
                            source_entity.link_confidence = recipient_result.confidence
                            source_entity.linked_at = datetime.now(timezone.utc)
                            source_entity_store.add_or_update(source_entity)
                            stats['source_entities_created'] += 1

                    if created_for_this_email == 0:
                        stats['no_person'] += 1
                        continue

                if len(batch) >= batch_size:
                    if not dry_run:
                        _insert_batch(conn, batch)
                        conn.commit()  # Commit after each batch to avoid losing progress
                    stats['inserted'] += len(batch)
                    batch = []

                processed += 1
                if processed % 500 == 0:
                    logger.info(f"  Processed {processed} new emails (skipped {stats['already_exists']} existing, {stats['marketing_skipped']} marketing)...")

            except Exception as e:
                logger.warning(f"Error processing email {message_id}: {e}")
                stats['errors'] += 1

        # Insert remaining
        if batch:
            if not dry_run:
                _insert_batch(conn, batch)
            stats['inserted'] += len(batch)

        if not dry_run:
            conn.commit()

    except Exception as e:
        logger.error(f"Failed to sync Gmail: {e}")
        stats['errors'] += 1

    conn.close()

    # Add affected person_ids to stats for refresh
    stats['affected_person_ids'] = affected_person_ids

    return stats


def sync_calendar_interactions(
    account_type: GoogleAccount,
    days_back: int = 365,
    dry_run: bool = True,
) -> dict:
    """
    Sync Calendar interactions for an account.

    Creates one interaction per attendee per event.

    Args:
        account_type: Which Google account to use
        days_back: How many days back to sync
        dry_run: If True, don't actually insert

    Returns:
        Stats dict
    """
    stats = {
        'events_fetched': 0,
        'interactions_inserted': 0,
        'already_exists': 0,
        'no_person': 0,
        'errors': 0,
        'source_entities_created': 0,
    }

    # Track affected person_ids for stats refresh
    affected_person_ids: set[str] = set()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    resolver = get_entity_resolver()
    source_entity_store = get_source_entity_store()
    my_person_id = settings.my_person_id

    # Get existing interactions to avoid duplicates
    existing = set()
    cursor = conn.execute(
        "SELECT source_id FROM interactions WHERE source_type = 'calendar'"
    )
    for row in cursor.fetchall():
        if row[0]:
            existing.add(row[0])
    logger.info(f"Found {len(existing)} existing calendar interactions")

    calendar = CalendarService(account_type=account_type)
    start_date = datetime.now(timezone.utc) - timedelta(days=days_back)
    end_date = datetime.now(timezone.utc) + timedelta(days=30)  # Include upcoming

    try:
        logger.info(f"Fetching calendar events from {account_type.value} account ({days_back} days back)...")
        api_start = time.time()
        events = calendar.get_events_in_range(
            start_date=start_date,
            end_date=end_date,
            max_results=2500,
        )
        api_elapsed = time.time() - api_start
        logger.info(f"Found {len(events)} events (API call took {api_elapsed:.1f}s)")
        stats['events_fetched'] = len(events)

        batch = []
        process_start = time.time()
        events_processed = 0

        for event in events:
            # Process each attendee
            attendees = event.attendees if event.attendees else []

            # Also try to parse attendees from title (Task 3: parse meeting titles)
            title_attendees = _parse_attendees_from_title(event.title)
            for ta in title_attendees:
                if ta not in attendees:
                    attendees.append(ta)

            if not attendees:
                # No attendees - skip
                continue

            # Count other attendees (excluding self) for meeting size classification
            # This is used to determine calendar_1on1 vs calendar_small_group vs calendar_large_meeting
            other_attendee_count = len(attendees)  # All attendees here are "other" (self excluded by resolver)

            for attendee in attendees:
                # Create unique source_id per event+attendee
                source_id = f"{event.event_id}:{attendee}"

                if source_id in existing:
                    stats['already_exists'] += 1
                    continue

                # Parse attendee (could be "Name <email>" or just email)
                attendee_name = None
                attendee_email = None
                if "<" in attendee and ">" in attendee:
                    import re
                    match = re.match(r'^([^<]+)<([^>]+)>$', attendee.strip())
                    if match:
                        attendee_name = match.group(1).strip()
                        attendee_email = match.group(2).strip()
                elif "@" in attendee:
                    attendee_email = attendee
                else:
                    attendee_name = attendee

                # Resolve to PersonEntity
                result = resolver.resolve(
                    name=attendee_name,
                    email=attendee_email,
                    create_if_missing=True,
                )

                if not result or not result.entity:
                    stats['no_person'] += 1
                    continue

                person_id = result.entity.id

                # Skip if the attendee is myself
                if person_id == my_person_id:
                    continue

                # Track for stats refresh
                affected_person_ids.add(person_id)

                # Create interaction
                interaction_id = str(uuid.uuid4())
                timestamp = event.start_time.isoformat()
                source_link = event.html_link or ""

                batch.append((
                    interaction_id,
                    person_id,
                    timestamp,
                    'calendar',
                    event.title,
                    event.description[:200] if event.description else None,
                    source_link,
                    source_id,
                    datetime.now(timezone.utc).isoformat(),
                    account_type.value,  # source_account: "personal" or "work"
                    other_attendee_count,  # attendee_count: for calendar size classification
                ))
                stats['interactions_inserted'] += 1

                # Create source entity for the attendee
                if not dry_run and attendee_email:
                    source_entity = create_calendar_source_entity(
                        event_id=event.event_id,
                        attendee_email=attendee_email,
                        attendee_name=attendee_name,
                        observed_at=event.start_time,
                        metadata={"event_title": event.title[:100] if event.title else None},
                    )
                    source_entity.canonical_person_id = person_id
                    source_entity.link_confidence = result.confidence if result else 1.0
                    source_entity.linked_at = datetime.now(timezone.utc)
                    source_entity_store.add_or_update(source_entity)
                    stats['source_entities_created'] += 1

            # Progress logging every 100 events
            events_processed += 1
            if events_processed % 100 == 0:
                elapsed = time.time() - process_start
                logger.info(f"  Processed {events_processed}/{len(events)} events ({elapsed:.1f}s elapsed)")

        # Final timing
        process_elapsed = time.time() - process_start
        logger.info(f"Processed all {len(events)} events in {process_elapsed:.1f}s")

        # Insert batch
        if batch and not dry_run:
            _insert_batch(conn, batch)
            conn.commit()

    except Exception as e:
        logger.error(f"Failed to sync Calendar: {e}")
        import traceback
        traceback.print_exc()
        stats['errors'] += 1

    conn.close()

    # Add affected person_ids to stats for refresh
    stats['affected_person_ids'] = affected_person_ids

    return stats


def _parse_email_addresses(field: str) -> list[tuple[str | None, str]]:
    """
    Parse email addresses from a To or CC field.

    Handles formats:
    - "Name <email@domain.com>"
    - "email@domain.com"
    - "Name1 <email1>, Name2 <email2>"

    Returns:
        List of (name, email) tuples
    """
    if not field:
        return []

    results = []
    # Split by comma, handling quoted names with commas
    parts = []
    current = ""
    in_quotes = False
    for char in field:
        if char == '"':
            in_quotes = not in_quotes
        elif char == ',' and not in_quotes:
            if current.strip():
                parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())

    for part in parts:
        part = part.strip()
        if '<' in part and '>' in part:
            # Format: "Name <email>"
            name = part.split('<')[0].strip().strip('"').strip()
            email = part.split('<')[1].rstrip('>').strip()
            results.append((name if name else None, email))
        elif '@' in part:
            # Just email
            results.append((None, part))

    return results


def _parse_attendees_from_title(title: str) -> list[str]:
    """
    Parse attendee names from meeting titles.

    Handles patterns like:
    - "1:1 with John Smith"
    - "Sync: Nathan/Taylor"
    - "Meeting with Sarah Chen"
    - "Nathan <> Rushi"

    Args:
        title: Meeting title

    Returns:
        List of extracted names
    """
    import re

    names = []
    title_lower = title.lower()

    # Pattern: "1:1 with <name>"
    match = re.search(r'1:1\s+with\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', title, re.IGNORECASE)
    if match:
        names.append(match.group(1))

    # Pattern: "Meeting with <name>"
    match = re.search(r'meeting\s+with\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', title, re.IGNORECASE)
    if match:
        names.append(match.group(1))

    # Pattern: "Sync: Name1/Name2" or "Sync: Name1 / Name2"
    match = re.search(r'sync[:\s]+([A-Z][a-z]+)\s*/\s*([A-Z][a-z]+)', title, re.IGNORECASE)
    if match:
        names.append(match.group(1))
        names.append(match.group(2))

    # Pattern: "Name1 <> Name2" or "Name1 <-> Name2"
    match = re.search(r'([A-Z][a-z]+)\s*<-?>\s*([A-Z][a-z]+)', title)
    if match:
        names.append(match.group(1))
        names.append(match.group(2))

    # Pattern: "Call with <name>"
    match = re.search(r'call\s+with\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', title, re.IGNORECASE)
    if match:
        names.append(match.group(1))

    # Pattern: "Intro: Name1 <> Name2"
    match = re.search(r'intro[:\s]+([A-Z][a-z]+)\s*(?:<-?>|/|&)\s*([A-Z][a-z]+)', title, re.IGNORECASE)
    if match:
        names.append(match.group(1))
        names.append(match.group(2))

    return list(set(names))


def _insert_batch(conn: sqlite3.Connection, batch: list):
    """Insert a batch of interactions.

    Batch tuples should have 11 elements:
    (id, person_id, timestamp, source_type, title, snippet, source_link, source_id,
     created_at, source_account, attendee_count)
    """
    conn.executemany("""
        INSERT OR IGNORE INTO interactions
        (id, person_id, timestamp, source_type, title, snippet, source_link, source_id,
         created_at, source_account, attendee_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, batch)


def main():
    parser = argparse.ArgumentParser(description='Sync Gmail and Calendar to interactions')
    parser.add_argument('--execute', action='store_true', help='Actually apply changes')
    parser.add_argument('--days', type=int, default=365, help='Days back to sync')
    parser.add_argument('--gmail-only', action='store_true', help='Only sync Gmail')
    parser.add_argument('--calendar-only', action='store_true', help='Only sync Calendar')
    parser.add_argument('--personal-only', action='store_true', help='Only sync personal account')
    parser.add_argument('--work-only', action='store_true', help='Only sync work account')
    parser.add_argument('--domain', type=str, help='Filter emails by domain (e.g., gmail.com)')
    parser.add_argument('--before-days', type=int, help='Skip emails newer than this many days ago (for historical backfill)')
    args = parser.parse_args()

    dry_run = not args.execute

    # Determine which accounts to sync based on flags and settings
    # Work integrations are disabled by default for safety
    accounts = []
    if args.personal_only:
        accounts = [GoogleAccount.PERSONAL]
    elif args.work_only:
        accounts = [GoogleAccount.WORK]
    else:
        # Default behavior: check work integration settings
        accounts = [GoogleAccount.PERSONAL]

        # Only add work account if work integration is enabled
        sync_work_gmail = settings.sync_work_gmail and not args.calendar_only
        sync_work_calendar = settings.sync_work_calendar and not args.gmail_only

        if sync_work_gmail or sync_work_calendar:
            accounts.append(GoogleAccount.WORK)
        else:
            logger.info("Work account sync disabled (set LIFEOS_SYNC_WORK_GMAIL=true or LIFEOS_SYNC_WORK_CALENDAR=true to enable)")

    all_stats = {}

    for account in accounts:
        # Check if Gmail sync is allowed for this account
        should_sync_gmail = not args.calendar_only
        if account == GoogleAccount.WORK and not settings.sync_work_gmail:
            should_sync_gmail = False

        if should_sync_gmail:
            logger.info(f"\n=== Syncing Gmail ({account.value}) ===")
            stats = sync_gmail_interactions(
                account_type=account,
                days_back=args.days,
                dry_run=dry_run,
                domain_filter=args.domain,
                before_days=args.before_days,
            )
            all_stats[f'gmail_{account.value}'] = stats
            logger.info(f"Gmail {account.value}: fetched={stats['fetched']}, inserted={stats['inserted']}, marketing_skipped={stats['marketing_skipped']}, source_entities={stats['source_entities_created']}, exists={stats['already_exists']}, errors={stats['errors']}")

        # Check if Calendar sync is allowed for this account
        should_sync_calendar = not args.gmail_only
        if account == GoogleAccount.WORK and not settings.sync_work_calendar:
            should_sync_calendar = False

        if should_sync_calendar:
            logger.info(f"\n=== Syncing Calendar ({account.value}) ===")
            stats = sync_calendar_interactions(
                account_type=account,
                days_back=args.days,
                dry_run=dry_run,
            )
            all_stats[f'calendar_{account.value}'] = stats
            logger.info(f"Calendar {account.value}: events={stats['events_fetched']}, inserted={stats['interactions_inserted']}, source_entities={stats['source_entities_created']}, exists={stats['already_exists']}, errors={stats['errors']}")

    if dry_run:
        logger.info("\nDRY RUN - no changes made. Use --execute to apply.")
    else:
        # Persist newly created person entities to disk
        person_store = get_person_entity_store()
        person_store.save()
        logger.info("Saved person entities to disk")

        # Collect all affected person_ids and refresh their stats
        all_affected = set()
        for stats in all_stats.values():
            affected = stats.get('affected_person_ids', set())
            all_affected.update(affected)

        if all_affected:
            from api.services.person_stats import refresh_person_stats
            logger.info(f"Refreshing stats for {len(all_affected)} affected people...")
            refresh_person_stats(list(all_affected))

    return all_stats


if __name__ == '__main__':
    main()
