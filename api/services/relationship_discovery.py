"""
Relationship Discovery - Discover connections between people.

Analyzes shared contexts to discover and score relationships:
- Shared calendar events (strong signal)
- Shared email threads (medium signal)
- Co-mentions in vault notes (medium signal)
- Shared Slack channels (weak signal)
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.services.person_entity import PersonEntity, get_person_entity_store
from api.services.interaction_store import get_interaction_store, get_interaction_db_path
from api.services.relationship import (
    Relationship,
    get_relationship_store,
    TYPE_COWORKER,
    TYPE_INFERRED,
)
from api.utils.datetime_utils import make_aware as _ensure_tz_aware

logger = logging.getLogger(__name__)


def _datetime_lt(a: datetime | None, b: datetime | None) -> bool:
    """Safely compare datetimes, handling timezone-naive vs aware."""
    if a is None or b is None:
        return False
    return _ensure_tz_aware(a) < _ensure_tz_aware(b)


def _datetime_gt(a: datetime | None, b: datetime | None) -> bool:
    """Safely compare datetimes, handling timezone-naive vs aware."""
    if a is None or b is None:
        return False
    return _ensure_tz_aware(a) > _ensure_tz_aware(b)


# Discovery window (days to look back)
# Use a large number to process all available history
DISCOVERY_WINDOW_DAYS = 3650  # ~10 years - effectively all available data


def discover_from_calendar(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_shared_events: int = 2,
) -> list[Relationship]:
    """
    Discover relationships from shared calendar events.

    People who attend the same meetings are likely connected.

    Args:
        days_back: Days to look back
        min_shared_events: Minimum shared events to create relationship

    Returns:
        List of discovered relationships
    """
    import sqlite3

    relationship_store = get_relationship_store()
    person_store = get_person_entity_store()

    # Build lookup tables from people_entities
    email_to_person: dict[str, str] = {}
    name_to_person: dict[str, str] = {}
    for person in person_store.get_all():
        for email in person.emails:
            email_to_person[email.lower()] = person.id
        name_to_person[person.canonical_name.lower()] = person.id
        for alias in person.aliases:
            name_to_person[alias.lower()] = person.id

    def resolve_participant(participant: str) -> Optional[str]:
        """Resolve participant (email or name) to canonical person ID."""
        if not participant:
            return None
        participant_lower = participant.lower().strip()
        # Try as email first
        if '@' in participant:
            return email_to_person.get(participant_lower)
        # Then try as name
        return name_to_person.get(participant_lower)

    # Query interactions database directly to find shared calendar events
    # source_id format is "event_id:participant" - extract participant
    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Get all calendar interactions with source_id containing participant AND timestamp
    query = """
        SELECT
            substr(source_id, 1, instr(source_id, ':') - 1) as event_id,
            substr(source_id, instr(source_id, ':') + 1) as participant,
            timestamp
        FROM interactions
        WHERE source_type = 'calendar'
          AND source_id LIKE '%:%'
          AND timestamp >= ?
    """

    cursor = conn.execute(query, (cutoff.isoformat(),))

    # Group by event, resolving participants to person IDs
    # Also track event timestamps
    event_attendees: dict[str, set[str]] = defaultdict(set)
    event_timestamps: dict[str, str] = {}  # event_id -> timestamp
    for row in cursor:
        event_id = row['event_id']
        participant = row['participant']
        timestamp = row['timestamp']
        person_id = resolve_participant(participant)
        if person_id:
            event_attendees[event_id].add(person_id)
            # Keep the event timestamp (same for all participants)
            if event_id not in event_timestamps:
                event_timestamps[event_id] = timestamp

    conn.close()

    # Filter to events with 2+ resolved attendees
    event_attendees = {eid: list(pids) for eid, pids in event_attendees.items() if len(pids) >= 2}
    logger.info(f"Found {len(event_attendees)} calendar events with 2+ resolved attendees")

    # Find pairs of people who share events, tracking dates
    pair_events: dict[tuple[str, str], list[str]] = defaultdict(list)

    for event_id, attendees in event_attendees.items():
        # Skip single-attendee events
        if len(attendees) < 2:
            continue

        # Create pairs from attendees
        attendees_list = list(attendees)
        for i, person_a in enumerate(attendees_list):
            for person_b in attendees_list[i + 1:]:
                # Normalize pair order
                pair = (min(person_a, person_b), max(person_a, person_b))
                pair_events[pair].append(event_id)

    # Create/update relationships for pairs with enough shared events
    relationships = []
    for (person_a_id, person_b_id), events in pair_events.items():
        if len(events) >= min_shared_events:
            # Get first and last event dates for this pair
            event_dates = []
            for event_id in events:
                ts = event_timestamps.get(event_id)
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        event_dates.append(dt)
                    except (ValueError, TypeError):
                        pass

            now = datetime.now(timezone.utc)
            first_seen = min(event_dates) if event_dates else now
            # Cap last_seen at today to exclude future events
            last_seen = min(max(event_dates), now) if event_dates else now

            existing = relationship_store.get_between(person_a_id, person_b_id)

            if existing:
                # Update existing relationship
                existing.shared_events_count = len(events)
                # Extend date range if needed
                if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                    existing.first_seen_together = first_seen
                if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                    existing.last_seen_together = last_seen
                if "calendar" not in existing.shared_contexts:
                    existing.shared_contexts.append("calendar")
                relationship_store.update(existing)
                relationships.append(existing)
            else:
                # Create new relationship
                rel = Relationship(
                    person_a_id=person_a_id,
                    person_b_id=person_b_id,
                    relationship_type=TYPE_COWORKER,
                    shared_events_count=len(events),
                    first_seen_together=first_seen,
                    last_seen_together=last_seen,
                    shared_contexts=["calendar"],
                )
                relationship_store.add(rel)
                relationships.append(rel)

    logger.info(f"Discovered {len(relationships)} relationships from calendar")
    return relationships


def discover_from_calendar_direct(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_events: int = 1,
) -> list[Relationship]:
    """
    Discover relationships from MY calendar events with other people.

    This is MY calendar, so I am implicitly at every event.
    Creates relationships between ME and each attendee, with event count as shared_events_count.

    Args:
        days_back: Days to look back
        min_events: Minimum events to count (default 1 - any shared event counts)

    Returns:
        List of discovered/updated relationships
    """
    import sqlite3
    from config.settings import settings

    my_person_id = settings.my_person_id
    if not my_person_id:
        logger.warning("MY_PERSON_ID not configured, skipping calendar direct discovery")
        return []

    relationship_store = get_relationship_store()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Count calendar events per person and get first/last dates
    # Calendar interactions have person_id = the other attendee
    query = """
        SELECT
            person_id,
            COUNT(DISTINCT substr(source_id, 1, instr(source_id, ':') - 1)) as event_count,
            MIN(timestamp) as first_event,
            MAX(timestamp) as last_event
        FROM interactions
        WHERE source_type = 'calendar'
          AND timestamp >= ?
          AND person_id IS NOT NULL
          AND person_id != ?
        GROUP BY person_id
        HAVING COUNT(DISTINCT substr(source_id, 1, instr(source_id, ':') - 1)) >= ?
    """

    cursor = conn.execute(query, (cutoff.isoformat(), my_person_id, min_events))

    relationships = []
    for row in cursor:
        other_person_id = row['person_id']
        event_count = row['event_count']
        first_event = row['first_event']
        last_event = row['last_event']

        # Parse dates, capping last_seen at today to exclude future events
        now = datetime.now(timezone.utc)
        first_seen = datetime.fromisoformat(first_event.replace('Z', '+00:00')) if first_event else now
        last_seen_raw = datetime.fromisoformat(last_event.replace('Z', '+00:00')) if last_event else now
        last_seen = min(last_seen_raw, now)

        # Normalize pair order
        if my_person_id < other_person_id:
            person_a_id, person_b_id = my_person_id, other_person_id
        else:
            person_a_id, person_b_id = other_person_id, my_person_id

        existing = relationship_store.get_between(person_a_id, person_b_id)

        if existing:
            existing.shared_events_count = event_count
            if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                existing.first_seen_together = first_seen
            if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                existing.last_seen_together = last_seen
            if "calendar" not in existing.shared_contexts:
                existing.shared_contexts.append("calendar")
            relationship_store.update(existing)
            relationships.append(existing)
        else:
            rel = Relationship(
                person_a_id=person_a_id,
                person_b_id=person_b_id,
                relationship_type=TYPE_INFERRED,
                shared_events_count=event_count,
                first_seen_together=first_seen,
                last_seen_together=last_seen,
                shared_contexts=["calendar"],
            )
            relationship_store.add(rel)
            relationships.append(rel)

    conn.close()

    logger.info(f"Discovered/updated {len(relationships)} relationships from calendar (direct)")
    return relationships


def discover_from_email_threads(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_shared_threads: int = 2,
) -> list[Relationship]:
    """
    Discover relationships from shared email threads.

    Gmail interactions store the OTHER person's ID, not the account owner.
    Since this is MY gmail, I (my_person_id) am implicitly in every thread.
    This creates relationships between:
    1. ME and each correspondent (direct emails)
    2. Other people who share the same thread (group emails)

    Args:
        days_back: Days to look back
        min_shared_threads: Minimum shared threads to create relationship

    Returns:
        List of discovered relationships
    """
    import sqlite3
    from config.settings import settings

    my_person_id = settings.my_person_id
    if not my_person_id:
        logger.warning("MY_PERSON_ID not configured, skipping email discovery")
        return []

    relationship_store = get_relationship_store()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Group by email subject (title) to find shared threads
    # Also track timestamps for date ranges
    query = """
        SELECT title, person_id, timestamp
        FROM interactions
        WHERE source_type = 'gmail'
          AND timestamp >= ?
          AND title IS NOT NULL
          AND title != ''
    """

    cursor = conn.execute(query, (cutoff.isoformat(),))

    # Build thread -> participants mapping and track dates
    # IMPORTANT: Include my_person_id in every thread since this is MY gmail
    thread_participants: dict[str, set[str]] = defaultdict(set)
    thread_dates: dict[str, list[str]] = defaultdict(list)
    for row in cursor:
        title = row['title']
        person_id = row['person_id']
        timestamp = row['timestamp']
        if person_id:
            # Add both me and the correspondent to the thread
            thread_participants[title].add(my_person_id)
            thread_participants[title].add(person_id)
            if timestamp:
                thread_dates[title].append(timestamp)

    conn.close()

    # Filter to threads with 2+ participants (should be all now since we add my_person_id)
    thread_participants = {t: list(pids) for t, pids in thread_participants.items() if len(pids) >= 2}
    logger.info(f"Found {len(thread_participants)} email threads with 2+ participants")

    # Find pairs who share threads
    pair_threads: dict[tuple[str, str], list[str]] = defaultdict(list)

    for thread_title, participants in thread_participants.items():
        for i, person_a in enumerate(participants):
            for person_b in participants[i + 1:]:
                pair = (min(person_a, person_b), max(person_a, person_b))
                pair_threads[pair].append(thread_title)

    # Create/update relationships
    relationships = []
    for (person_a_id, person_b_id), threads in pair_threads.items():
        if len(threads) >= min_shared_threads:
            # Get all dates from shared threads (ensure all are timezone-aware)
            all_dates = []
            for thread_title in threads:
                for ts in thread_dates.get(thread_title, []):
                    try:
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        all_dates.append(_ensure_tz_aware(dt))
                    except (ValueError, TypeError):
                        pass

            # Use None if no dates available (don't default to now())
            first_seen = min(all_dates) if all_dates else None
            last_seen = max(all_dates) if all_dates else None

            existing = relationship_store.get_between(person_a_id, person_b_id)

            if existing:
                existing.shared_threads_count = len(threads)
                if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                    existing.first_seen_together = first_seen
                if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                    existing.last_seen_together = last_seen
                if "gmail" not in existing.shared_contexts:
                    existing.shared_contexts.append("gmail")
                relationship_store.update(existing)
                relationships.append(existing)
            else:
                rel = Relationship(
                    person_a_id=person_a_id,
                    person_b_id=person_b_id,
                    relationship_type=TYPE_INFERRED,
                    shared_threads_count=len(threads),
                    first_seen_together=first_seen,
                    last_seen_together=last_seen,
                    shared_contexts=["gmail"],
                )
                relationship_store.add(rel)
                relationships.append(rel)

    logger.info(f"Discovered {len(relationships)} relationships from email")
    return relationships


def discover_from_vault_comments(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_co_mentions: int = 2,
) -> list[Relationship]:
    """
    Discover relationships from co-mentions in vault notes.

    People mentioned in the same notes are likely connected.

    Args:
        days_back: Days to look back
        min_co_mentions: Minimum co-mentions to create relationship

    Returns:
        List of discovered relationships
    """
    interaction_store = get_interaction_store()
    relationship_store = get_relationship_store()
    person_store = get_person_entity_store()

    # Group vault interactions by note (source_id = file path)
    note_mentions: dict[str, list[str]] = defaultdict(list)

    for person in person_store.get_all():
        interactions = interaction_store.get_for_person(
            person.id,
            days_back=days_back,
            source_type="vault",
        )
        for interaction in interactions:
            if interaction.source_id:
                note_mentions[interaction.source_id].append(person.id)

    # Find pairs who are mentioned together
    pair_notes: dict[tuple[str, str], list[str]] = defaultdict(list)

    for note_path, mentioned in note_mentions.items():
        if len(mentioned) < 2:
            continue

        for i, person_a in enumerate(mentioned):
            for person_b in mentioned[i + 1:]:
                pair = (min(person_a, person_b), max(person_a, person_b))
                pair_notes[pair].append(note_path)

    # Create/update relationships
    relationships = []
    for (person_a_id, person_b_id), notes in pair_notes.items():
        if len(notes) >= min_co_mentions:
            existing = relationship_store.get_between(person_a_id, person_b_id)

            if existing:
                # Don't overwrite last_seen_together - vault notes don't have interaction timestamps
                # Only update contexts
                if "vault" not in existing.shared_contexts:
                    existing.shared_contexts.append("vault")
                    relationship_store.update(existing)
                relationships.append(existing)
            else:
                # Vault notes don't have interaction timestamps,
                # so we leave first_seen_together and last_seen_together as None
                rel = Relationship(
                    person_a_id=person_a_id,
                    person_b_id=person_b_id,
                    relationship_type=TYPE_INFERRED,
                    first_seen_together=None,
                    last_seen_together=None,
                    shared_contexts=["vault"],
                )
                relationship_store.add(rel)
                relationships.append(rel)

    logger.info(f"Discovered {len(relationships)} relationships from vault")
    return relationships


def discover_from_messaging_groups(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_shared_groups: int = 1,
) -> list[Relationship]:
    """
    Discover relationships from shared messaging groups (WhatsApp and iMessage).

    People in the same messaging groups are likely connected.

    Args:
        days_back: Days to look back
        min_shared_groups: Minimum shared groups to create relationship

    Returns:
        List of discovered relationships
    """
    import sqlite3

    relationship_store = get_relationship_store()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Find all WhatsApp groups and their participants
    # Group ID is extracted from the title field
    query = """
        SELECT
            title as group_id,
            person_id,
            COUNT(*) as message_count
        FROM interactions
        WHERE source_type = 'whatsapp'
          AND title LIKE 'WhatsApp group:%'
          AND timestamp >= ?
        GROUP BY title, person_id
    """

    cursor = conn.execute(query, (cutoff.isoformat(),))

    # Build group -> participants mapping
    group_participants: dict[str, set[str]] = defaultdict(set)
    for row in cursor:
        group_id = row['group_id']
        person_id = row['person_id']
        if person_id:
            group_participants[group_id].add(person_id)

    conn.close()

    # Filter to groups with 2+ participants
    group_participants = {gid: list(pids) for gid, pids in group_participants.items() if len(pids) >= 2}
    logger.info(f"Found {len(group_participants)} WhatsApp groups with 2+ participants")

    # Find pairs of people who share groups
    pair_groups: dict[tuple[str, str], list[str]] = defaultdict(list)

    for group_id, participants in group_participants.items():
        for i, person_a in enumerate(participants):
            for person_b in participants[i + 1:]:
                pair = (min(person_a, person_b), max(person_a, person_b))
                pair_groups[pair].append(group_id)

    # Create/update relationships
    relationships = []
    for (person_a_id, person_b_id), groups in pair_groups.items():
        if len(groups) >= min_shared_groups:
            existing = relationship_store.get_between(person_a_id, person_b_id)

            if existing:
                # Don't overwrite last_seen_together - group membership doesn't have interaction timestamps
                # Only update contexts
                if "whatsapp" not in existing.shared_contexts:
                    existing.shared_contexts.append("whatsapp")
                    relationship_store.update(existing)
                relationships.append(existing)
            else:
                # Group membership doesn't have interaction timestamps,
                # so we leave first_seen_together and last_seen_together as None
                rel = Relationship(
                    person_a_id=person_a_id,
                    person_b_id=person_b_id,
                    relationship_type=TYPE_INFERRED,
                    first_seen_together=None,
                    last_seen_together=None,
                    shared_contexts=["whatsapp"],
                )
                relationship_store.add(rel)
                relationships.append(rel)

    logger.info(f"Discovered {len(relationships)} relationships from WhatsApp groups")
    return relationships


def discover_from_imessage_direct(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_messages: int = 1,
) -> list[Relationship]:
    """
    Discover relationships from direct iMessage/SMS conversations with me.

    Creates/updates relationships between ME and people I message directly.
    The message count is the actual number of messages exchanged.

    Args:
        days_back: Days to look back
        min_messages: Minimum messages to count

    Returns:
        List of updated relationships
    """
    import sqlite3
    from config.settings import settings

    my_person_id = settings.my_person_id
    if not my_person_id:
        logger.warning("MY_PERSON_ID not configured, skipping iMessage discovery")
        return []

    relationship_store = get_relationship_store()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Count iMessage interactions per person AND get first/last dates
    query = """
        SELECT
            person_id,
            COUNT(*) as message_count,
            MIN(timestamp) as first_message,
            MAX(timestamp) as last_message
        FROM interactions
        WHERE source_type IN ('imessage', 'sms')
          AND timestamp >= ?
          AND person_id IS NOT NULL
          AND person_id != ?
        GROUP BY person_id
        HAVING COUNT(*) >= ?
    """

    cursor = conn.execute(query, (cutoff.isoformat(), my_person_id, min_messages))

    relationships = []
    for row in cursor:
        other_person_id = row['person_id']
        message_count = row['message_count']
        first_message = row['first_message']
        last_message = row['last_message']

        # Parse dates - use None if no date, never default to today
        first_seen = datetime.fromisoformat(first_message.replace('Z', '+00:00')) if first_message else None
        last_seen = datetime.fromisoformat(last_message.replace('Z', '+00:00')) if last_message else None

        # Normalize pair order (my_person_id vs other)
        if my_person_id < other_person_id:
            person_a_id, person_b_id = my_person_id, other_person_id
        else:
            person_a_id, person_b_id = other_person_id, my_person_id

        existing = relationship_store.get_between(person_a_id, person_b_id)

        if existing:
            # Update existing relationship
            existing.shared_messages_count = message_count
            # Update dates only if they extend the range
            if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                existing.first_seen_together = first_seen
            if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                existing.last_seen_together = last_seen
            if "imessage" not in existing.shared_contexts:
                existing.shared_contexts.append("imessage")
            relationship_store.update(existing)
            relationships.append(existing)
        else:
            # Create new relationship
            rel = Relationship(
                person_a_id=person_a_id,
                person_b_id=person_b_id,
                relationship_type=TYPE_INFERRED,
                shared_messages_count=message_count,
                first_seen_together=first_seen,
                last_seen_together=last_seen,
                shared_contexts=["imessage"],
            )
            relationship_store.add(rel)
            relationships.append(rel)

    conn.close()

    logger.info(f"Discovered/updated {len(relationships)} relationships from iMessage")
    return relationships


def discover_from_whatsapp_direct(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_messages: int = 1,
) -> list[Relationship]:
    """
    Discover relationships from direct WhatsApp conversations with me.

    Creates/updates relationships between ME and people I message directly.

    Args:
        days_back: Days to look back
        min_messages: Minimum messages to count

    Returns:
        List of updated relationships
    """
    import sqlite3
    from config.settings import settings

    my_person_id = settings.my_person_id
    if not my_person_id:
        logger.warning("MY_PERSON_ID not configured, skipping WhatsApp discovery")
        return []

    relationship_store = get_relationship_store()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Count WhatsApp interactions per person (excluding groups) with dates
    query = """
        SELECT
            person_id,
            COUNT(*) as message_count,
            MIN(timestamp) as first_message,
            MAX(timestamp) as last_message
        FROM interactions
        WHERE source_type = 'whatsapp'
          AND title NOT LIKE 'WhatsApp group:%'
          AND timestamp >= ?
          AND person_id IS NOT NULL
          AND person_id != ?
        GROUP BY person_id
        HAVING COUNT(*) >= ?
    """

    cursor = conn.execute(query, (cutoff.isoformat(), my_person_id, min_messages))

    relationships = []
    for row in cursor:
        other_person_id = row['person_id']
        message_count = row['message_count']
        first_message = row['first_message']
        last_message = row['last_message']

        # Parse dates - use None if no date, never default to today
        first_seen = datetime.fromisoformat(first_message.replace('Z', '+00:00')) if first_message else None
        last_seen = datetime.fromisoformat(last_message.replace('Z', '+00:00')) if last_message else None

        # Normalize pair order
        if my_person_id < other_person_id:
            person_a_id, person_b_id = my_person_id, other_person_id
        else:
            person_a_id, person_b_id = other_person_id, my_person_id

        existing = relationship_store.get_between(person_a_id, person_b_id)

        if existing:
            existing.shared_whatsapp_count = message_count
            if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                existing.first_seen_together = first_seen
            if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                existing.last_seen_together = last_seen
            if "whatsapp" not in existing.shared_contexts:
                existing.shared_contexts.append("whatsapp")
            relationship_store.update(existing)
            relationships.append(existing)
        else:
            rel = Relationship(
                person_a_id=person_a_id,
                person_b_id=person_b_id,
                relationship_type=TYPE_INFERRED,
                shared_whatsapp_count=message_count,
                first_seen_together=first_seen,
                last_seen_together=last_seen,
                shared_contexts=["whatsapp"],
            )
            relationship_store.add(rel)
            relationships.append(rel)

    conn.close()

    logger.info(f"Discovered/updated {len(relationships)} relationships from WhatsApp")
    return relationships


def discover_from_phone_calls(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_calls: int = 1,
) -> list[Relationship]:
    """
    Discover relationships from phone calls with me.

    Creates/updates relationships between ME and people I call directly.
    Phone calls are high-value synchronous interactions.

    Args:
        days_back: Days to look back
        min_calls: Minimum calls to count

    Returns:
        List of updated relationships
    """
    import sqlite3
    from config.settings import settings

    my_person_id = settings.my_person_id
    if not my_person_id:
        logger.warning("MY_PERSON_ID not configured, skipping phone call discovery")
        return []

    relationship_store = get_relationship_store()

    db_path = get_interaction_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Count phone call interactions per person with dates
    query = """
        SELECT
            person_id,
            COUNT(*) as call_count,
            MIN(timestamp) as first_call,
            MAX(timestamp) as last_call
        FROM interactions
        WHERE source_type = 'phone'
          AND timestamp >= ?
          AND person_id IS NOT NULL
          AND person_id != ?
        GROUP BY person_id
        HAVING COUNT(*) >= ?
    """

    cursor = conn.execute(query, (cutoff.isoformat(), my_person_id, min_calls))

    relationships = []
    for row in cursor:
        other_person_id = row['person_id']
        call_count = row['call_count']
        first_call = row['first_call']
        last_call = row['last_call']

        # Parse dates - use None if no date, never default to today
        first_seen = datetime.fromisoformat(first_call.replace('Z', '+00:00')) if first_call else None
        last_seen = datetime.fromisoformat(last_call.replace('Z', '+00:00')) if last_call else None

        # Normalize pair order
        if my_person_id < other_person_id:
            person_a_id, person_b_id = my_person_id, other_person_id
        else:
            person_a_id, person_b_id = other_person_id, my_person_id

        existing = relationship_store.get_between(person_a_id, person_b_id)

        if existing:
            existing.shared_phone_calls_count = call_count
            if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                existing.first_seen_together = first_seen
            if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                existing.last_seen_together = last_seen
            if "phone" not in existing.shared_contexts:
                existing.shared_contexts.append("phone")
            relationship_store.update(existing)
            relationships.append(existing)
        else:
            rel = Relationship(
                person_a_id=person_a_id,
                person_b_id=person_b_id,
                relationship_type=TYPE_INFERRED,
                shared_phone_calls_count=call_count,
                first_seen_together=first_seen,
                last_seen_together=last_seen,
                shared_contexts=["phone"],
            )
            relationship_store.add(rel)
            relationships.append(rel)

    conn.close()

    logger.info(f"Discovered/updated {len(relationships)} relationships from phone calls")
    return relationships


def discover_from_slack_direct(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_messages: int = 1,
) -> list[Relationship]:
    """
    Discover relationships from Slack DMs stored in ChromaDB.

    Slack data is indexed to ChromaDB with metadata including:
    - people: JSON array of Slack user IDs
    - tags: includes "slack" and channel type (im, mpim)
    - modified_date: date string

    Args:
        days_back: Days to look back
        min_messages: Minimum messages to count

    Returns:
        List of updated relationships
    """
    import json
    import sqlite3
    from config.settings import settings
    from api.services.source_entity import get_crm_db_path

    my_person_id = settings.my_person_id
    if not my_person_id:
        logger.warning("MY_PERSON_ID not configured, skipping Slack discovery")
        return []

    relationship_store = get_relationship_store()

    # Build Slack user ID -> PersonEntity ID mapping
    # First from source_entities that are already linked
    crm_db_path = get_crm_db_path()
    conn = sqlite3.connect(crm_db_path)
    cursor = conn.execute("""
        SELECT source_id, canonical_person_id
        FROM source_entities
        WHERE source_type = 'slack'
          AND canonical_person_id IS NOT NULL
    """)

    slack_to_person: dict[str, str] = {}
    for row in cursor.fetchall():
        # source_id format is "workspace:user_id" like "T02F5DW71LY:U02EVV4TRT5"
        source_id = row[0]
        person_id = row[1]
        if ':' in source_id:
            slack_user_id = source_id.split(':')[1]
            slack_to_person[slack_user_id] = person_id

    # Also get unmapped Slack users with their names for name-based matching
    cursor = conn.execute("""
        SELECT source_id, observed_name
        FROM source_entities
        WHERE source_type = 'slack'
          AND canonical_person_id IS NULL
          AND observed_name IS NOT NULL
    """)

    slack_user_names: dict[str, str] = {}
    for row in cursor.fetchall():
        source_id = row[0]
        observed_name = row[1]
        if ':' in source_id and observed_name:
            slack_user_id = source_id.split(':')[1]
            slack_user_names[slack_user_id] = observed_name.lower().strip()

    conn.close()

    # Build name -> person_id mapping from PersonEntity
    person_store = get_person_entity_store()
    name_to_person: dict[str, str] = {}
    for person in person_store.get_all():
        # Canonical name
        name_to_person[person.canonical_name.lower().strip()] = person.id
        # Aliases
        for alias in person.aliases:
            name_to_person[alias.lower().strip()] = person.id

    # Match unmapped Slack users by name
    name_matched = 0
    for slack_user_id, slack_name in slack_user_names.items():
        if slack_user_id not in slack_to_person:
            person_id = name_to_person.get(slack_name)
            if person_id:
                slack_to_person[slack_user_id] = person_id
                name_matched += 1

    if not slack_to_person:
        logger.info("No Slack users mapped to people, skipping Slack discovery")
        return []

    logger.info(f"Found {len(slack_to_person)} Slack users mapped to people ({name_matched} by name)")

    # Query ChromaDB for Slack messages
    try:
        import chromadb
        client = chromadb.HttpClient(host='localhost', port=8001)
        collection = client.get_collection('lifeos_slack')
    except Exception as e:
        logger.warning(f"Could not connect to ChromaDB for Slack: {e}")
        return []

    # Get all Slack messages (limit to recent ones)
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')

    # Query messages with date filter
    try:
        results = collection.get(
            where={"modified_date": {"$gte": cutoff_date}},
            include=["metadatas"],
            limit=50000,  # Reasonable limit
        )
    except Exception as e:
        # Fallback: get all and filter
        logger.info(f"Date filter failed ({e}), getting all messages")
        results = collection.get(include=["metadatas"], limit=50000)

    if not results or not results.get('metadatas'):
        logger.info("No Slack messages found in ChromaDB")
        return []

    # Count messages per person
    person_message_counts: dict[str, int] = defaultdict(int)
    person_first_seen: dict[str, str] = {}
    person_last_seen: dict[str, str] = {}

    for meta in results['metadatas']:
        # Parse people field (JSON array of Slack user IDs)
        people_json = meta.get('people', '[]')
        try:
            slack_user_ids = json.loads(people_json) if isinstance(people_json, str) else people_json
        except json.JSONDecodeError:
            continue

        modified_date = meta.get('modified_date', '')

        # Map each Slack user to PersonEntity
        for slack_user_id in slack_user_ids:
            person_id = slack_to_person.get(slack_user_id)
            if person_id and person_id != my_person_id:
                person_message_counts[person_id] += 1

                # Track first/last seen
                if modified_date:
                    if person_id not in person_first_seen or modified_date < person_first_seen[person_id]:
                        person_first_seen[person_id] = modified_date
                    if person_id not in person_last_seen or modified_date > person_last_seen[person_id]:
                        person_last_seen[person_id] = modified_date

    # Create/update relationships
    relationships = []
    for other_person_id, message_count in person_message_counts.items():
        if message_count < min_messages:
            continue

        # Parse dates
        first_date = person_first_seen.get(other_person_id)
        last_date = person_last_seen.get(other_person_id)

        # Parse dates - use None if no date available (don't default to now())
        first_seen = datetime.strptime(first_date, '%Y-%m-%d').replace(tzinfo=timezone.utc) if first_date else None
        last_seen = datetime.strptime(last_date, '%Y-%m-%d').replace(tzinfo=timezone.utc) if last_date else None

        # Normalize pair order
        if my_person_id < other_person_id:
            person_a_id, person_b_id = my_person_id, other_person_id
        else:
            person_a_id, person_b_id = other_person_id, my_person_id

        existing = relationship_store.get_between(person_a_id, person_b_id)

        if existing:
            existing.shared_slack_count = message_count
            if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                existing.first_seen_together = first_seen
            if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                existing.last_seen_together = last_seen
            if "slack" not in existing.shared_contexts:
                existing.shared_contexts.append("slack")
            relationship_store.update(existing)
            relationships.append(existing)
        else:
            rel = Relationship(
                person_a_id=person_a_id,
                person_b_id=person_b_id,
                relationship_type=TYPE_INFERRED,
                shared_slack_count=message_count,
                first_seen_together=first_seen,
                last_seen_together=last_seen,
                shared_contexts=["slack"],
            )
            relationship_store.add(rel)
            relationships.append(rel)

    logger.info(f"Discovered/updated {len(relationships)} relationships from Slack")
    return relationships


def discover_linkedin_connections() -> list[Relationship]:
    """
    Mark relationships where the OTHER person is in my LinkedIn connections.

    LinkedIn data is MY connection list, so is_linkedin_connection=True
    means "I am connected to this person on LinkedIn".

    Returns:
        List of updated relationships
    """
    import sqlite3
    from api.services.source_entity import get_crm_db_path
    from config.settings import settings

    my_person_id = settings.my_person_id
    if not my_person_id:
        logger.warning("MY_PERSON_ID not configured, skipping LinkedIn discovery")
        return []

    relationship_store = get_relationship_store()

    # Query for all person IDs with LinkedIn source entities
    # These are MY connections (from my LinkedIn export)
    db_path = get_crm_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("""
        SELECT DISTINCT canonical_person_id
        FROM source_entities
        WHERE source_type = 'linkedin'
          AND canonical_person_id IS NOT NULL
    """)

    linkedin_connections: set[str] = {row[0] for row in cursor.fetchall()}
    conn.close()

    logger.info(f"Found {len(linkedin_connections)} LinkedIn connections")

    # Create/update relationships between ME and my LinkedIn connections
    relationships = []

    for other_person_id in linkedin_connections:
        if other_person_id == my_person_id:
            continue  # Skip self

        # Normalize pair order
        if my_person_id < other_person_id:
            person_a_id, person_b_id = my_person_id, other_person_id
        else:
            person_a_id, person_b_id = other_person_id, my_person_id

        existing = relationship_store.get_between(person_a_id, person_b_id)

        if existing:
            if not existing.is_linkedin_connection:
                existing.is_linkedin_connection = True
                if "linkedin" not in existing.shared_contexts:
                    existing.shared_contexts.append("linkedin")
                relationship_store.update(existing)
                relationships.append(existing)
        else:
            # Create new relationship
            # Note: LinkedIn connections don't have interaction timestamps,
            # so we leave first_seen_together and last_seen_together as None
            rel = Relationship(
                person_a_id=person_a_id,
                person_b_id=person_b_id,
                relationship_type=TYPE_INFERRED,
                is_linkedin_connection=True,
                first_seen_together=None,
                last_seen_together=None,
                shared_contexts=["linkedin"],
            )
            relationship_store.add(rel)
            relationships.append(rel)

    logger.info(f"Updated {len(relationships)} relationships with LinkedIn flag")
    return relationships


def discover_from_shared_photos(
    days_back: int = DISCOVERY_WINDOW_DAYS,
    min_shared_photos: int = 3,
) -> list[Relationship]:
    """
    Discover relationships from photo co-appearances.

    People who appear together in photos have a real-world connection.

    Args:
        days_back: Days to look back
        min_shared_photos: Minimum shared photos to create/update relationship

    Returns:
        List of discovered/updated relationships
    """
    from collections import defaultdict
    from config.settings import settings

    if not settings.photos_enabled:
        logger.info("Photos not enabled, skipping photo relationship discovery")
        return []

    relationship_store = get_relationship_store()
    interaction_store = get_interaction_store()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Get all photo interactions grouped by source_id (asset UUID)
    # to find photos with multiple people
    photo_people: dict[str, list[tuple[str, datetime]]] = defaultdict(list)

    # Query all photos interactions
    from api.services.person_entity import get_person_entity_store
    person_store = get_person_entity_store()

    for person in person_store.get_all():
        interactions = interaction_store.get_for_person(
            person.id,
            source_type="photos",
        )
        for interaction in interactions:
            if interaction.source_id and interaction.timestamp:
                if _ensure_tz_aware(interaction.timestamp) >= cutoff:
                    photo_people[interaction.source_id].append(
                        (person.id, interaction.timestamp)
                    )

    # Find photos with 2+ people
    multi_person_photos = {
        photo_id: people
        for photo_id, people in photo_people.items()
        if len(people) >= 2
    }

    logger.info(f"Found {len(multi_person_photos)} photos with 2+ people")

    # Count shared photos for each pair
    pair_photos: dict[tuple[str, str], list[datetime]] = defaultdict(list)

    for photo_id, people in multi_person_photos.items():
        person_ids = [p[0] for p in people]
        timestamp = people[0][1]  # All people in same photo have same timestamp

        # Create pairs from people in this photo
        for i, person_a in enumerate(person_ids):
            for person_b in person_ids[i + 1:]:
                # Normalize pair order
                pair = (min(person_a, person_b), max(person_a, person_b))
                pair_photos[pair].append(timestamp)

    # Update relationships for pairs with enough shared photos
    relationships = []
    for (person_a_id, person_b_id), timestamps in pair_photos.items():
        if len(timestamps) < min_shared_photos:
            continue

        first_seen = min(timestamps)
        last_seen = max(timestamps)

        existing = relationship_store.get_between(person_a_id, person_b_id)

        if existing:
            # Update existing relationship
            existing.shared_photos_count = len(timestamps)
            # Extend date range if needed
            if not existing.first_seen_together or _datetime_lt(first_seen, existing.first_seen_together):
                existing.first_seen_together = first_seen
            if not existing.last_seen_together or _datetime_gt(last_seen, existing.last_seen_together):
                existing.last_seen_together = last_seen
            if "photos" not in existing.shared_contexts:
                existing.shared_contexts.append("photos")
            relationship_store.update(existing)
            relationships.append(existing)
        else:
            # Create new relationship
            rel = Relationship(
                person_a_id=person_a_id,
                person_b_id=person_b_id,
                relationship_type=TYPE_INFERRED,
                shared_photos_count=len(timestamps),
                first_seen_together=first_seen,
                last_seen_together=last_seen,
                shared_contexts=["photos"],
            )
            relationship_store.add(rel)
            relationships.append(rel)

    logger.info(f"Discovered {len(relationships)} relationships from photos")
    return relationships


def run_full_discovery(days_back: int = DISCOVERY_WINDOW_DAYS) -> dict:
    """
    Run all discovery methods and return statistics.

    Args:
        days_back: Days to look back

    Returns:
        Statistics about discovered relationships
    """
    results = {
        "calendar": len(discover_from_calendar(days_back)),
        "calendar_direct": len(discover_from_calendar_direct(days_back)),
        "email": len(discover_from_email_threads(days_back, min_shared_threads=1)),
        "vault": len(discover_from_vault_comments(days_back)),
        "messaging_groups": len(discover_from_messaging_groups(days_back)),
        "imessage_direct": len(discover_from_imessage_direct(days_back)),
        "whatsapp_direct": len(discover_from_whatsapp_direct(days_back)),
        "phone_calls": len(discover_from_phone_calls(days_back)),
        "slack_direct": len(discover_from_slack_direct(days_back)),
        "linkedin": len(discover_linkedin_connections()),
        "photos": len(discover_from_shared_photos(days_back)),
    }

    total = sum(results.values())
    logger.info(f"Full discovery complete: {total} relationships updated")

    return {
        "by_source": results,
        "total": total,
    }


def get_suggested_connections(
    person_id: str,
    limit: int = 10,
) -> list[dict]:
    """
    Get suggested connections for a person.

    Returns people who share contexts but don't have a direct relationship yet.

    Args:
        person_id: Person to find suggestions for
        limit: Maximum suggestions to return

    Returns:
        List of suggested connections with scores
    """
    person_store = get_person_entity_store()
    relationship_store = get_relationship_store()
    interaction_store = get_interaction_store()

    person = person_store.get_by_id(person_id)
    if not person:
        return []

    # Get existing connections
    existing_connections = set(relationship_store.get_connections(person_id))

    # Get person's vault contexts
    person_contexts = set(person.vault_contexts)

    # Find people with overlapping contexts
    suggestions = []
    for other in person_store.get_all():
        if other.id == person_id:
            continue
        if other.id in existing_connections:
            continue

        # Calculate overlap score
        other_contexts = set(other.vault_contexts)
        shared_contexts = person_contexts & other_contexts

        if not shared_contexts:
            continue

        # Score based on context overlap
        overlap_score = len(shared_contexts) / max(len(person_contexts), 1)

        # Boost if they share sources
        shared_sources = set(person.sources) & set(other.sources)
        source_boost = len(shared_sources) * 0.1

        total_score = min(1.0, overlap_score + source_boost)

        suggestions.append({
            "person_id": other.id,
            "name": other.canonical_name,
            "company": other.company,
            "score": round(total_score, 3),
            "shared_contexts": list(shared_contexts),
            "shared_sources": list(shared_sources),
        })

    # Sort by score descending
    suggestions.sort(key=lambda x: x["score"], reverse=True)

    return suggestions[:limit]


def get_connection_overlap(person_a_id: str, person_b_id: str) -> dict:
    """
    Get detailed overlap information between two people.

    Args:
        person_a_id: First person ID
        person_b_id: Second person ID

    Returns:
        Dict with overlap details
    """
    person_store = get_person_entity_store()
    relationship_store = get_relationship_store()
    interaction_store = get_interaction_store()

    person_a = person_store.get_by_id(person_a_id)
    person_b = person_store.get_by_id(person_b_id)

    if not person_a or not person_b:
        return {"error": "Person not found"}

    relationship = relationship_store.get_between(person_a_id, person_b_id)

    # Context overlap
    shared_contexts = set(person_a.vault_contexts) & set(person_b.vault_contexts)

    # Source overlap
    shared_sources = set(person_a.sources) & set(person_b.sources)

    return {
        "person_a": {
            "id": person_a.id,
            "name": person_a.canonical_name,
        },
        "person_b": {
            "id": person_b.id,
            "name": person_b.canonical_name,
        },
        "relationship": {
            "exists": relationship is not None,
            "type": relationship.relationship_type if relationship else None,
            "shared_events_count": relationship.shared_events_count if relationship else 0,
            "shared_threads_count": relationship.shared_threads_count if relationship else 0,
            "first_seen_together": relationship.first_seen_together.isoformat() if relationship and relationship.first_seen_together else None,
            "last_seen_together": relationship.last_seen_together.isoformat() if relationship and relationship.last_seen_together else None,
        },
        "shared_contexts": list(shared_contexts),
        "shared_sources": list(shared_sources),
    }
