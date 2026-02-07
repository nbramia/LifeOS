#!/usr/bin/env python3
"""
Backfill source_account and attendee_count for existing interactions.

This script updates historical interaction records with:
1. source_account: "personal" or "work" (for account-based weight multipliers)
2. attendee_count: number of other attendees (for calendar subtype classification)

For calendar events: Re-fetches from Google Calendar API to get accurate attendee counts.
For gmail: Queries both Gmail accounts to determine which has access to each message.

Uses a sampling approach for gmail - checks a few messages per person and applies
the result to all their emails (since people typically email from one account consistently).

Run AFTER your regular nightly sync to avoid conflicts.

Usage:
    python scripts/backfill_interaction_metadata.py --status             # Show current state
    python scripts/backfill_interaction_metadata.py                      # Dry-run preview
    python scripts/backfill_interaction_metadata.py --execute            # Apply changes
    python scripts/backfill_interaction_metadata.py --execute --calendar-only
    python scripts/backfill_interaction_metadata.py --execute --gmail-only
"""
import sqlite3
import argparse
import logging
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from api.services.google_auth import GoogleAccount
from api.services.gmail import GmailService
from api.services.calendar import CalendarService
from api.services.interaction_store import get_interaction_db_path
from api.services.person_entity import get_person_entity_store
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
    """Check if an email address indicates marketing/automated sender."""
    if sender_name:
        name_lower = sender_name.lower()
        for pattern in MARKETING_NAME_PATTERNS:
            if pattern in name_lower:
                return True
        for substring in COMMERCIAL_SENDER_SUBSTRINGS:
            if substring in name_lower:
                return True

    if not email or '@' not in email:
        return False

    email_lower = email.lower()

    for substring in COMMERCIAL_SENDER_SUBSTRINGS:
        if substring in email_lower:
            return True

    prefix = email_lower.split('@')[0]
    domain = email_lower.split('@')[1]

    prefix_base = prefix.split('+')[0]
    for pattern in MARKETING_EMAIL_PREFIXES:
        if prefix_base == pattern or prefix_base.startswith(pattern + '-') or prefix_base.startswith(pattern + '_'):
            return True

    domain_parts = domain.split('.')
    for i in range(len(domain_parts) - 1):
        check_domain = '.'.join(domain_parts[i:])
        if check_domain in MARKETING_DOMAINS:
            return True

    return False


def check_message_in_account(gmail_service: GmailService, message_id: str) -> bool:
    """Check if a message exists in the given Gmail account."""
    try:
        # Try to get just the message metadata (minimal API cost)
        result = gmail_service.service.users().messages().get(
            userId="me",
            id=message_id,
            format="minimal"
        ).execute()
        return result is not None
    except Exception:
        return False


def determine_account_for_messages(
    message_ids: list[str],
    personal_gmail: GmailService,
    work_gmail: GmailService,
) -> str | None:
    """
    Determine which account a set of messages belongs to.

    Checks messages against both accounts. Returns "personal", "work", or None.
    Uses majority voting if results are mixed.
    """
    personal_count = 0
    work_count = 0

    for msg_id in message_ids:
        # Extract base message ID (strip any :email suffix for sent emails)
        base_id = msg_id.split(':')[0] if ':' in msg_id else msg_id

        in_personal = check_message_in_account(personal_gmail, base_id)
        in_work = check_message_in_account(work_gmail, base_id)

        if in_personal and not in_work:
            personal_count += 1
        elif in_work and not in_personal:
            work_count += 1
        # If in both or neither, skip (shouldn't happen but handle gracefully)

    if personal_count > work_count:
        return "personal"
    elif work_count > personal_count:
        return "work"
    return None


def backfill_gmail_metadata(
    dry_run: bool = True,
    samples_per_person: int = 3,
) -> dict:
    """
    Backfill gmail interactions with source_account using API verification.

    Uses sampling: checks a few messages per person and applies result to all.
    Filters out marketing/automated emails first.
    """
    stats = {
        'total_people': 0,
        'marketing_skipped': 0,
        'determined_personal': 0,
        'determined_work': 0,
        'could_not_determine': 0,
        'interactions_updated': 0,
        'api_calls': 0,
    }

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)

    # Get person store for name lookup
    person_store = get_person_entity_store()

    # Get gmail interactions grouped by person, with sample source_ids
    cursor = conn.execute("""
        SELECT
            person_id,
            GROUP_CONCAT(source_id) as source_ids,
            COUNT(*) as interaction_count
        FROM interactions
        WHERE source_type = 'gmail' AND source_account IS NULL
        GROUP BY person_id
        ORDER BY interaction_count DESC
    """)

    people_to_process = []
    for row in cursor.fetchall():
        person_id, source_ids_str, count = row
        source_ids = source_ids_str.split(',') if source_ids_str else []

        # Get person info
        person = person_store.get_by_id(person_id)
        if not person:
            continue

        # Check if this person looks like marketing
        is_marketing = False
        if person.emails:
            for email in person.emails:
                if is_marketing_email(email, person.canonical_name):
                    is_marketing = True
                    break

        if is_marketing:
            stats['marketing_skipped'] += count
            continue

        # Take sample of source_ids for checking
        sample_ids = source_ids[:samples_per_person]

        people_to_process.append({
            'person_id': person_id,
            'name': person.canonical_name,
            'sample_ids': sample_ids,
            'total_count': count,
        })

    stats['total_people'] = len(people_to_process)
    logger.info(f"Found {stats['total_people']} people to process (skipped {stats['marketing_skipped']} marketing interactions)")

    if not people_to_process:
        conn.close()
        return stats

    # Initialize Gmail services
    try:
        personal_gmail = GmailService(account_type=GoogleAccount.PERSONAL)
        work_gmail = GmailService(account_type=GoogleAccount.WORK)
    except Exception as e:
        logger.error(f"Failed to initialize Gmail services: {e}")
        conn.close()
        return stats

    # Process each person
    updates_by_account = defaultdict(list)  # account -> list of person_ids

    for i, person_data in enumerate(people_to_process):
        if i > 0 and i % 50 == 0:
            logger.info(f"  Processed {i}/{len(people_to_process)} people...")

        # Check which account has these messages
        stats['api_calls'] += len(person_data['sample_ids']) * 2  # 2 accounts per message

        account = determine_account_for_messages(
            person_data['sample_ids'],
            personal_gmail,
            work_gmail,
        )

        if account == "personal":
            stats['determined_personal'] += 1
            stats['interactions_updated'] += person_data['total_count']
            updates_by_account['personal'].append(person_data['person_id'])
        elif account == "work":
            stats['determined_work'] += 1
            stats['interactions_updated'] += person_data['total_count']
            updates_by_account['work'].append(person_data['person_id'])
        else:
            stats['could_not_determine'] += 1

        # Small delay to avoid rate limiting
        if i > 0 and i % 10 == 0:
            time.sleep(0.1)

    # Apply updates
    if not dry_run:
        for account, person_ids in updates_by_account.items():
            if person_ids:
                placeholders = ','.join('?' * len(person_ids))
                conn.execute(f"""
                    UPDATE interactions
                    SET source_account = ?
                    WHERE source_type = 'gmail'
                      AND source_account IS NULL
                      AND person_id IN ({placeholders})
                """, [account] + person_ids)
        conn.commit()

    conn.close()
    return stats


def backfill_calendar_metadata(
    dry_run: bool = True,
) -> dict:
    """
    Backfill calendar interactions with source_account and attendee_count.
    Re-fetches events from Google Calendar API.
    """
    stats = {
        'total_calendar': 0,
        'updated': 0,
        'not_found_in_api': 0,
        'already_has_data': 0,
        'errors': 0,
    }

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)

    # Get all calendar interactions missing metadata
    cursor = conn.execute("""
        SELECT id, source_id, source_account, attendee_count
        FROM interactions
        WHERE source_type = 'calendar'
          AND (source_account IS NULL OR attendee_count IS NULL)
    """)

    event_ids_needed = set()
    interaction_map = {}  # event_id -> list of interaction records

    for row in cursor.fetchall():
        interaction_id, source_id, existing_account, existing_count = row
        stats['total_calendar'] += 1

        # source_id format: event_id:attendee_email
        event_id = source_id.split(':')[0] if ':' in source_id else source_id
        event_ids_needed.add(event_id)

        if event_id not in interaction_map:
            interaction_map[event_id] = []
        interaction_map[event_id].append({
            'id': interaction_id,
            'source_id': source_id,
            'existing_account': existing_account,
            'existing_count': existing_count,
        })

    logger.info(f"Found {stats['total_calendar']} calendar interactions needing metadata")
    logger.info(f"Need to fetch {len(event_ids_needed)} unique events from API")

    if not event_ids_needed:
        conn.close()
        return stats

    # Fetch events from both calendars
    events_data = {}

    for account_type in [GoogleAccount.PERSONAL, GoogleAccount.WORK]:
        try:
            logger.info(f"Fetching events from {account_type.value} calendar...")
            calendar = CalendarService(account_type=account_type)

            start_date = datetime.now(timezone.utc) - timedelta(days=730)  # 2 years
            end_date = datetime.now(timezone.utc) + timedelta(days=30)

            events = calendar.get_events_in_range(
                start_date=start_date,
                end_date=end_date,
                max_results=2500,
            )

            logger.info(f"  Fetched {len(events)} events from {account_type.value}")

            for event in events:
                if event.event_id in event_ids_needed:
                    attendees = event.attendees or []
                    attendee_count = len(attendees)

                    events_data[event.event_id] = {
                        'attendee_count': attendee_count,
                        'source_account': account_type.value,
                    }

        except Exception as e:
            logger.warning(f"Failed to fetch from {account_type.value} calendar: {e}")
            stats['errors'] += 1

    logger.info(f"Matched {len(events_data)} events from API")

    # Build updates
    updates = []
    for event_id, data in events_data.items():
        for interaction in interaction_map.get(event_id, []):
            new_account = interaction['existing_account'] or data['source_account']
            new_count = interaction['existing_count'] if interaction['existing_count'] is not None else data['attendee_count']

            if new_account != interaction['existing_account'] or new_count != interaction['existing_count']:
                updates.append((new_account, new_count, interaction['id']))
                stats['updated'] += 1
            else:
                stats['already_has_data'] += 1

    # Count not found
    for event_id in event_ids_needed:
        if event_id not in events_data:
            stats['not_found_in_api'] += len(interaction_map.get(event_id, []))

    if not dry_run and updates:
        logger.info(f"Applying {len(updates)} updates...")
        conn.executemany("""
            UPDATE interactions
            SET source_account = ?, attendee_count = ?
            WHERE id = ?
        """, updates)
        conn.commit()

    conn.close()
    return stats


def show_current_status():
    """Show current state of metadata in the database."""
    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)

    print("\n=== Current Interaction Metadata Status ===\n")

    # Gmail status
    cursor = conn.execute("""
        SELECT
            COALESCE(source_account, 'NULL') as account,
            COUNT(*) as count
        FROM interactions
        WHERE source_type = 'gmail'
        GROUP BY source_account
        ORDER BY count DESC
    """)
    print("Gmail source_account distribution:")
    for row in cursor.fetchall():
        print(f"  {row[0]}: {row[1]:,}")

    # Calendar status
    cursor = conn.execute("""
        SELECT
            COALESCE(source_account, 'NULL') as account,
            CASE
                WHEN attendee_count IS NULL THEN 'NULL'
                WHEN attendee_count = 1 THEN '1 (1on1)'
                WHEN attendee_count BETWEEN 2 AND 5 THEN '2-5 (small)'
                ELSE '6+ (large)'
            END as size_bucket,
            COUNT(*) as count
        FROM interactions
        WHERE source_type = 'calendar'
        GROUP BY source_account, size_bucket
        ORDER BY count DESC
    """)
    print("\nCalendar metadata distribution:")
    for row in cursor.fetchall():
        print(f"  account={row[0]}, size={row[1]}: {row[2]:,}")

    conn.close()


def estimate_gmail_time(samples_per_person: int = 3) -> dict:
    """Estimate how long gmail backfill will take."""
    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    person_store = get_person_entity_store()

    cursor = conn.execute("""
        SELECT person_id, COUNT(*) as cnt
        FROM interactions
        WHERE source_type = 'gmail' AND source_account IS NULL
        GROUP BY person_id
    """)

    total_people = 0
    marketing_people = 0

    for row in cursor.fetchall():
        person_id = row[0]
        person = person_store.get_by_id(person_id)
        if person and person.emails:
            is_marketing = any(is_marketing_email(e, person.canonical_name) for e in person.emails)
            if is_marketing:
                marketing_people += 1
            else:
                total_people += 1
        else:
            total_people += 1

    conn.close()

    api_calls = total_people * samples_per_person * 2  # 2 accounts per sample
    # Estimate ~10 API calls per second with rate limiting
    estimated_seconds = api_calls / 10

    return {
        'people_to_check': total_people,
        'marketing_filtered': marketing_people,
        'api_calls': api_calls,
        'estimated_minutes': round(estimated_seconds / 60, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description='Backfill source_account and attendee_count for historical interactions'
    )
    parser.add_argument('--execute', action='store_true',
                        help='Actually apply changes (default is dry-run)')
    parser.add_argument('--calendar-only', action='store_true',
                        help='Only backfill calendar interactions')
    parser.add_argument('--gmail-only', action='store_true',
                        help='Only backfill gmail interactions')
    parser.add_argument('--status', action='store_true',
                        help='Show current metadata status and exit')
    parser.add_argument('--estimate', action='store_true',
                        help='Estimate gmail backfill time and exit')
    parser.add_argument('--samples', type=int, default=3,
                        help='Number of messages to sample per person (default: 3)')
    args = parser.parse_args()

    if args.status:
        show_current_status()
        return

    if args.estimate:
        est = estimate_gmail_time(args.samples)
        print(f"\n=== Gmail Backfill Time Estimate ===")
        print(f"People to check: {est['people_to_check']:,}")
        print(f"Marketing filtered out: {est['marketing_filtered']:,}")
        print(f"API calls needed: {est['api_calls']:,}")
        print(f"Estimated time: ~{est['estimated_minutes']} minutes")
        return

    dry_run = not args.execute

    if dry_run:
        logger.info("DRY RUN MODE - no changes will be made")

    all_stats = {}

    # Calendar backfill
    if not args.gmail_only:
        logger.info("\n=== Backfilling Calendar Metadata ===")
        calendar_stats = backfill_calendar_metadata(dry_run=dry_run)
        all_stats['calendar'] = calendar_stats
        logger.info(f"Calendar: {calendar_stats['updated']} updated, "
                   f"{calendar_stats['not_found_in_api']} not found in API")

    # Gmail backfill
    if not args.calendar_only:
        logger.info("\n=== Backfilling Gmail Metadata ===")
        gmail_stats = backfill_gmail_metadata(dry_run=dry_run, samples_per_person=args.samples)
        all_stats['gmail'] = gmail_stats
        logger.info(f"Gmail: {gmail_stats['determined_personal']} personal, "
                   f"{gmail_stats['determined_work']} work, "
                   f"{gmail_stats['could_not_determine']} unknown, "
                   f"{gmail_stats['marketing_skipped']} marketing skipped")
        logger.info(f"  API calls made: {gmail_stats['api_calls']}")
        logger.info(f"  Interactions to update: {gmail_stats['interactions_updated']}")

    if dry_run:
        logger.info("\nDRY RUN complete. Use --execute to apply changes.")
    else:
        logger.info("\nBackfill complete!")
        show_current_status()


if __name__ == '__main__':
    main()
