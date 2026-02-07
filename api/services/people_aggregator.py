"""
People Aggregator - Multi-source people tracking for LifeOS.

Aggregates people from:
- LinkedIn connections CSV
- Gmail contacts (last 2 years)
- Calendar attendees
- Granola meeting notes (via Obsidian)
- Obsidian note mentions
"""
import csv
import json
import logging
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

from api.services.people import (
    PEOPLE_DICTIONARY,
    ALIAS_MAP,
    resolve_person_name,
    extract_people_from_text,
)
from api.services.entity_resolver import get_entity_resolver, EntityResolver
from api.services.interaction_store import (
    get_interaction_store,
    InteractionStore,
    create_gmail_interaction,
    create_calendar_interaction,
)
from api.services.source_entity import (
    get_source_entity_store,
    create_linkedin_source_entity,
)
from api.services.google_auth import GoogleAccount

from api.utils.datetime_utils import make_aware as _make_aware

logger = logging.getLogger(__name__)


def _is_newer(new_dt: datetime, old_dt: datetime) -> bool:
    """Safely compare datetimes, handling mixed timezone awareness."""
    if old_dt is None:
        return True
    if new_dt is None:
        return False
    return _make_aware(new_dt) > _make_aware(old_dt)


# Patterns for filtering out commercial/automated emails
EXCLUDED_EMAIL_PATTERNS = [
    r".*noreply.*",
    r".*no-reply.*",
    r".*notifications?@.*",
    r".*marketing@.*",
    r".*support@.*",
    r".*@mailchimp\.com",
    r".*@sendgrid\..*",
    r".*@intercom\..*",
    r".*@zendesk\..*",
]

# Compiled patterns for efficiency
_EXCLUDED_EMAIL_REGEX = [re.compile(p, re.IGNORECASE) for p in EXCLUDED_EMAIL_PATTERNS]


def is_excluded_email(email: str) -> bool:
    """
    Check if an email should be excluded from sync (commercial/automated).

    Args:
        email: Email address to check

    Returns:
        True if email matches exclusion patterns
    """
    if not email:
        return True

    email_lower = email.lower()

    for pattern in _EXCLUDED_EMAIL_REGEX:
        if pattern.match(email_lower):
            return True

    return False


def parse_email_recipient(recipient: str) -> tuple[str, str]:
    """
    Parse a recipient string into name and email.

    Args:
        recipient: Raw recipient string like "Name <email@example.com>" or just "email@example.com"

    Returns:
        Tuple of (name, email)
    """
    recipient = recipient.strip()

    # Pattern: "Name <email@example.com>" or just "email@example.com"
    match = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>$', recipient)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    # Just email address
    if "@" in recipient:
        # Extract name from email prefix
        prefix = recipient.split("@")[0]
        name = re.sub(r"[._-]", " ", prefix).title()
        return name, recipient.strip()

    return recipient, ""


def sync_gmail_to_v2(
    gmail_service,
    days_back: int = 90,
    max_results: int = 500,
    entity_resolver: EntityResolver = None,
    interaction_store: InteractionStore = None,
) -> dict:
    """
    Sync sent emails to v2 people system (entities + interactions).

    Only processes SENT emails to track who the user actively communicates with.
    Filters out commercial/automated email addresses.

    Args:
        gmail_service: GmailService instance
        days_back: How many days to look back
        max_results: Maximum emails to process
        entity_resolver: EntityResolver instance (default singleton)
        interaction_store: InteractionStore instance (default singleton)

    Returns:
        Stats dict with counts of entities/interactions created
    """
    if gmail_service is None:
        logger.warning("Gmail service not available for v2 sync")
        return {"entities_created": 0, "entities_updated": 0, "interactions_created": 0, "emails_processed": 0, "emails_excluded": 0}

    resolver = entity_resolver or get_entity_resolver()
    store = interaction_store or get_interaction_store()

    stats = {
        "entities_created": 0,
        "entities_updated": 0,
        "interactions_created": 0,
        "emails_processed": 0,
        "emails_excluded": 0,
    }

    after_date = datetime.now(timezone.utc) - timedelta(days=days_back)

    try:
        # Query SENT emails only
        messages = gmail_service.search(
            keywords="in:sent",
            after=after_date,
            max_results=max_results,
        )

        logger.info(f"Gmail v2 sync: found {len(messages)} sent emails to process")

        for msg in messages:
            stats["emails_processed"] += 1

            # Skip if no recipients
            if not hasattr(msg, 'to') or not msg.to:
                continue

            # Parse recipients
            recipients = msg.to.split(',')

            for recipient in recipients:
                name, email = parse_email_recipient(recipient)

                if not email or '@' not in email:
                    continue

                # Filter out commercial/automated emails
                if is_excluded_email(email):
                    stats["emails_excluded"] += 1
                    continue

                # Resolve entity (create if missing)
                result = resolver.resolve(
                    name=name,
                    email=email,
                    create_if_missing=True,
                )

                if not result:
                    continue

                entity = result.entity

                if result.is_new:
                    stats["entities_created"] += 1

                # Create interaction
                interaction = create_gmail_interaction(
                    person_id=entity.id,
                    message_id=msg.message_id,
                    subject=msg.subject or "(no subject)",
                    timestamp=msg.date,
                    snippet=msg.snippet,
                )

                # Add if not duplicate
                _, was_added = store.add_if_not_exists(interaction)

                if was_added:
                    stats["interactions_created"] += 1

                    # Update entity stats
                    entity.email_count = (entity.email_count or 0) + 1
                    if _is_newer(msg.date, entity.last_seen):
                        entity.last_seen = _make_aware(msg.date)
                    if "gmail" not in entity.sources:
                        entity.sources.append("gmail")

                    resolver.store.update(entity)
                    stats["entities_updated"] += 1

    except Exception as e:
        logger.error(f"Failed to sync Gmail to v2: {e}")

    logger.info(f"Gmail v2 sync complete: {stats}")
    return stats


def sync_calendar_to_v2(
    calendar_services: list = None,
    days_back: int = 90,
    max_results: int = 500,
    entity_resolver: EntityResolver = None,
    interaction_store: InteractionStore = None,
) -> dict:
    """
    Sync calendar events to v2 people system (entities + interactions).

    Processes events from both PERSONAL and WORK calendars.
    Skips all-day events without attendees and declined events.

    Args:
        calendar_services: List of CalendarService instances (default: personal + work)
        days_back: How many days to look back
        max_results: Maximum events to process per calendar
        entity_resolver: EntityResolver instance (default singleton)
        interaction_store: InteractionStore instance (default singleton)

    Returns:
        Stats dict with counts of entities/interactions created
    """
    from api.services.calendar import get_calendar_service

    resolver = entity_resolver or get_entity_resolver()
    store = interaction_store or get_interaction_store()

    stats = {
        "entities_created": 0,
        "entities_updated": 0,
        "interactions_created": 0,
        "events_processed": 0,
        "events_skipped": 0,
    }

    # Default to both personal and work calendars
    if calendar_services is None:
        try:
            calendar_services = [
                get_calendar_service(GoogleAccount.PERSONAL),
                get_calendar_service(GoogleAccount.WORK),
            ]
        except Exception as e:
            logger.warning(f"Failed to get calendar services: {e}")
            return stats

    start_date = datetime.now(timezone.utc) - timedelta(days=days_back)
    end_date = datetime.now(timezone.utc)

    for cal_service in calendar_services:
        if cal_service is None:
            continue

        try:
            events = cal_service.get_events_in_range(
                start_date=start_date,
                end_date=end_date,
                max_results=max_results,
            )

            logger.info(f"Calendar v2 sync ({cal_service.account_type.value}): found {len(events)} events to process")

            for event in events:
                stats["events_processed"] += 1

                # Skip all-day events without attendees
                if event.is_all_day and not event.attendees:
                    stats["events_skipped"] += 1
                    continue

                # Skip events without attendees
                if not event.attendees:
                    stats["events_skipped"] += 1
                    continue

                # Process each attendee
                for attendee_str in event.attendees:
                    # Parse attendee (could be email or "Name" format)
                    if '@' in attendee_str:
                        # It's an email
                        email = attendee_str
                        name = attendee_str.split('@')[0]
                        name = re.sub(r"[._-]", " ", name).title()
                    else:
                        # Just a name
                        name = attendee_str
                        email = None

                    # Resolve entity
                    result = resolver.resolve(
                        name=name,
                        email=email,
                        create_if_missing=True,
                    )

                    if not result:
                        continue

                    entity = result.entity

                    if result.is_new:
                        stats["entities_created"] += 1

                    # Create interaction
                    interaction = create_calendar_interaction(
                        person_id=entity.id,
                        event_id=event.event_id,
                        title=event.title,
                        timestamp=event.start_time,
                        snippet=event.description[:200] if event.description else None,
                    )

                    # Add if not duplicate
                    _, was_added = store.add_if_not_exists(interaction)

                    if was_added:
                        stats["interactions_created"] += 1

                        # Update entity stats
                        entity.meeting_count = (entity.meeting_count or 0) + 1
                        # Only update last_seen if event is not in the future
                        now = datetime.now(timezone.utc)
                        event_ts = _make_aware(event.start_time)
                        if event_ts <= now and _is_newer(event_ts, entity.last_seen):
                            entity.last_seen = event_ts
                        if "calendar" not in entity.sources:
                            entity.sources.append("calendar")

                        resolver.store.update(entity)
                        stats["entities_updated"] += 1

        except Exception as e:
            logger.error(f"Failed to sync calendar ({cal_service.account_type.value}) to v2: {e}")

    logger.info(f"Calendar v2 sync complete: {stats}")
    return stats


def sync_linkedin_to_v2(
    csv_path: str,
    entity_resolver: EntityResolver = None,
) -> dict:
    """
    Sync LinkedIn connections from CSV to v2 people system.

    Processes the LinkedIn connections CSV export and creates/updates
    PersonEntity records for each connection.

    Args:
        csv_path: Path to LinkedIn connections CSV file
        entity_resolver: EntityResolver instance (default singleton)

    Returns:
        Stats dict with counts of entities created/updated
    """
    from pathlib import Path

    resolver = entity_resolver or get_entity_resolver()

    stats = {
        "entities_created": 0,
        "entities_updated": 0,
        "connections_processed": 0,
        "connections_skipped": 0,
        "source_entities_created": 0,
    }

    source_entity_store = get_source_entity_store()

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.warning(f"LinkedIn CSV not found: {csv_path}")
        return stats

    try:
        connections = load_linkedin_connections(csv_path)
        logger.info(f"LinkedIn v2 sync: found {len(connections)} connections to process")

        for conn in connections:
            stats["connections_processed"] += 1

            first_name = conn.get("first_name", "").strip()
            last_name = conn.get("last_name", "").strip()

            if not first_name and not last_name:
                stats["connections_skipped"] += 1
                continue

            # Use resolve_from_linkedin for proper entity resolution
            result = resolver.resolve_from_linkedin(
                first_name=first_name,
                last_name=last_name,
                email=conn.get("email") or None,
                company=conn.get("company") or None,
                position=conn.get("position") or None,
                linkedin_url=conn.get("linkedin_url") or None,
            )

            if result:
                if result.is_new:
                    stats["entities_created"] += 1
                else:
                    stats["entities_updated"] += 1

                # Create source entity for this LinkedIn connection
                linkedin_url = conn.get("linkedin_url")
                if linkedin_url and result.entity:
                    full_name = f"{first_name} {last_name}".strip()
                    connected_on = conn.get("connected_on")
                    try:
                        connected_at = datetime.strptime(connected_on, "%d %b %Y") if connected_on else None
                        if connected_at:
                            connected_at = connected_at.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        connected_at = None

                    source_entity = create_linkedin_source_entity(
                        profile_url=linkedin_url,
                        name=full_name,
                        email=conn.get("email"),
                        observed_at=connected_at,
                        metadata={
                            "company": conn.get("company"),
                            "position": conn.get("position"),
                        },
                    )
                    source_entity.canonical_person_id = result.entity.id
                    source_entity.link_confidence = result.confidence
                    source_entity.linked_at = datetime.now(timezone.utc)
                    source_entity_store.add_or_update(source_entity)
                    stats["source_entities_created"] += 1

    except Exception as e:
        logger.error(f"Failed to sync LinkedIn to v2: {e}")

    logger.info(f"LinkedIn v2 sync complete: {stats}")
    return stats


def sync_people_v2(
    gmail_service=None,
    calendar_services: list = None,
    linkedin_csv_path: str = None,
    days_back: int = 90,
    max_results: int = 500,
) -> dict:
    """
    Orchestrate v2 sync for all sources (LinkedIn, Gmail, Calendar).

    This is the main entry point for syncing to the v2 people system
    which uses EntityResolver and InteractionStore.

    Args:
        gmail_service: GmailService instance for email sync
        calendar_services: List of CalendarService instances (default: personal + work)
        linkedin_csv_path: Path to LinkedIn connections CSV (optional)
        days_back: How many days to look back
        max_results: Maximum items to process per source

    Returns:
        Combined stats dict from all syncs
    """
    logger.info(f"Starting v2 people sync (days_back={days_back}, max_results={max_results})")

    # Get shared resolver and store for consistency
    resolver = get_entity_resolver()
    store = get_interaction_store()

    combined_stats = {
        "linkedin": {},
        "gmail": {},
        "calendar": {},
        "total_entities_created": 0,
        "total_interactions_created": 0,
    }

    # Sync LinkedIn (first, as it provides company/position context)
    if linkedin_csv_path:
        linkedin_stats = sync_linkedin_to_v2(
            csv_path=linkedin_csv_path,
            entity_resolver=resolver,
        )
        combined_stats["linkedin"] = linkedin_stats
        combined_stats["total_entities_created"] += linkedin_stats.get("entities_created", 0)

    # Sync Gmail
    if gmail_service:
        gmail_stats = sync_gmail_to_v2(
            gmail_service=gmail_service,
            days_back=days_back,
            max_results=max_results,
            entity_resolver=resolver,
            interaction_store=store,
        )
        combined_stats["gmail"] = gmail_stats
        combined_stats["total_entities_created"] += gmail_stats.get("entities_created", 0)
        combined_stats["total_interactions_created"] += gmail_stats.get("interactions_created", 0)

    # Sync Calendar
    calendar_stats = sync_calendar_to_v2(
        calendar_services=calendar_services,
        days_back=days_back,
        max_results=max_results,
        entity_resolver=resolver,
        interaction_store=store,
    )
    combined_stats["calendar"] = calendar_stats
    combined_stats["total_entities_created"] += calendar_stats.get("entities_created", 0)
    combined_stats["total_interactions_created"] += calendar_stats.get("interactions_created", 0)

    logger.info(f"v2 people sync complete: {combined_stats['total_entities_created']} entities, {combined_stats['total_interactions_created']} interactions created")

    return combined_stats


@dataclass
class PersonRecord:
    """Comprehensive record for a person from multiple sources."""
    canonical_name: str
    email: Optional[str] = None
    sources: list[str] = field(default_factory=list)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    company: Optional[str] = None
    position: Optional[str] = None
    linkedin_url: Optional[str] = None
    meeting_count: int = 0
    email_count: int = 0
    mention_count: int = 0
    related_notes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    category: str = "unknown"  # work, personal, family, etc.

    def merge(self, other: "PersonRecord") -> "PersonRecord":
        """Merge another record into this one."""
        # Combine sources
        sources = list(set(self.sources + other.sources))

        # Take earliest first_seen
        first_seen = self.first_seen
        if other.first_seen:
            if first_seen is None or other.first_seen < first_seen:
                first_seen = other.first_seen

        # Take latest last_seen
        last_seen = self.last_seen
        if other.last_seen:
            if last_seen is None or other.last_seen > last_seen:
                last_seen = other.last_seen

        # Sum counts
        meeting_count = self.meeting_count + other.meeting_count
        email_count = self.email_count + other.email_count
        mention_count = self.mention_count + other.mention_count

        # Combine related notes
        related_notes = list(set(self.related_notes + other.related_notes))

        # Take first non-None values for single fields
        company = self.company or other.company
        position = self.position or other.position
        linkedin_url = self.linkedin_url or other.linkedin_url
        email = self.email or other.email

        # Combine aliases
        aliases = list(set(self.aliases + other.aliases))

        return PersonRecord(
            canonical_name=self.canonical_name,
            email=email,
            sources=sources,
            first_seen=first_seen,
            last_seen=last_seen,
            company=company,
            position=position,
            linkedin_url=linkedin_url,
            meeting_count=meeting_count,
            email_count=email_count,
            mention_count=mention_count,
            related_notes=related_notes,
            aliases=aliases,
            category=self.category if self.category != "unknown" else other.category,
        )

    def to_dict(self) -> dict:
        """Convert to dict for serialization."""
        data = asdict(self)
        if self.first_seen:
            data['first_seen'] = self.first_seen.isoformat()
        if self.last_seen:
            data['last_seen'] = self.last_seen.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PersonRecord":
        """Create from dict."""
        if data.get('first_seen'):
            data['first_seen'] = datetime.fromisoformat(data['first_seen'])
        if data.get('last_seen'):
            data['last_seen'] = datetime.fromisoformat(data['last_seen'])
        return cls(**data)


def load_linkedin_connections(csv_path: str) -> list[dict]:
    """
    Load LinkedIn connections from CSV export.

    Args:
        csv_path: Path to LinkedIn connections CSV

    Returns:
        List of connection dicts
    """
    if not csv_path or not Path(csv_path).exists():
        logger.warning(f"LinkedIn CSV not found: {csv_path}")
        return []

    connections = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                connections.append({
                    'first_name': row.get('First Name', ''),
                    'last_name': row.get('Last Name', ''),
                    'email': row.get('Email Address', ''),
                    'company': row.get('Company', ''),
                    'position': row.get('Position', ''),
                    'linkedin_url': row.get('URL', ''),
                    'connected_on': row.get('Connected On', ''),
                })
    except Exception as e:
        logger.error(f"Failed to load LinkedIn CSV: {e}")
        return []

    return connections


def extract_gmail_contacts(gmail_service, days_back: int = 730) -> list[dict]:
    """
    Extract contacts from Gmail messages.

    Args:
        gmail_service: GmailService instance
        days_back: How many days to look back (default 2 years)

    Returns:
        List of contact dicts with email, name, last_contact
    """
    if gmail_service is None:
        return []

    contacts = {}
    after_date = datetime.now(timezone.utc) - timedelta(days=days_back)

    try:
        # Search for sent emails (people we emailed)
        messages = gmail_service.search(
            keywords="in:sent",
            after=after_date,
            max_results=500,
        )

        for msg in messages:
            # Track recipients from sent emails
            if hasattr(msg, 'to') and msg.to:
                for recipient in msg.to.split(','):
                    email = recipient.strip()
                    if '@' in email:
                        # Extract name from "Name <email>" format
                        match = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', email)
                        if match:
                            name = match.group(1).strip()
                            email = match.group(2).strip()
                        else:
                            name = email.split('@')[0]

                        if email not in contacts:
                            contacts[email] = {
                                'email': email,
                                'name': name,
                                'last_contact': _make_aware(msg.date),
                                'email_count': 0,
                            }
                        contacts[email]['email_count'] += 1
                        if _is_newer(msg.date, contacts[email]['last_contact']):
                            contacts[email]['last_contact'] = _make_aware(msg.date)

        # Also search received emails
        messages = gmail_service.search(
            keywords="in:inbox",
            after=after_date,
            max_results=500,
        )

        for msg in messages:
            email = msg.sender
            name = msg.sender_name

            if email and '@' in email:
                if email not in contacts:
                    contacts[email] = {
                        'email': email,
                        'name': name,
                        'last_contact': _make_aware(msg.date),
                        'email_count': 0,
                    }
                contacts[email]['email_count'] += 1
                if _is_newer(msg.date, contacts[email]['last_contact']):
                    contacts[email]['last_contact'] = _make_aware(msg.date)

    except Exception as e:
        logger.error(f"Failed to extract Gmail contacts: {e}")

    return list(contacts.values())


def extract_calendar_attendees(calendar_service, days_back: int = 365) -> list[dict]:
    """
    Extract attendees from calendar events.

    Args:
        calendar_service: CalendarService instance
        days_back: How many days to look back

    Returns:
        List of attendee dicts with name, email, meeting_count, last_meeting
    """
    if calendar_service is None:
        return []

    attendees = {}
    start_date = datetime.now(timezone.utc) - timedelta(days=days_back)
    end_date = datetime.now(timezone.utc)

    try:
        events = calendar_service.get_events_in_range(
            start_date=start_date,
            end_date=end_date,
            max_results=500,
        )

        for event in events:
            for attendee in event.attendees:
                # attendee might be email or "Name" format
                if '@' in attendee:
                    email = attendee
                    name = attendee.split('@')[0]
                else:
                    name = attendee
                    email = None

                key = email or name.lower()
                if key not in attendees:
                    attendees[key] = {
                        'name': name,
                        'email': email,
                        'meeting_count': 0,
                        'last_meeting': _make_aware(event.start_time),
                    }
                attendees[key]['meeting_count'] += 1
                if _is_newer(event.start_time, attendees[key]['last_meeting']):
                    attendees[key]['last_meeting'] = _make_aware(event.start_time)

    except Exception as e:
        logger.error(f"Failed to extract calendar attendees: {e}")

    return list(attendees.values())


class PeopleAggregator:
    """
    Aggregates people from multiple sources into unified registry.
    """

    def __init__(
        self,
        linkedin_csv_path: Optional[str] = None,
        gmail_service=None,
        calendar_service=None,
        storage_path: str = "./data/people_aggregated.json",
    ):
        """
        Initialize aggregator.

        Args:
            linkedin_csv_path: Path to LinkedIn connections CSV
            gmail_service: GmailService instance
            calendar_service: CalendarService instance
            storage_path: Path to store aggregated data
        """
        self.linkedin_csv_path = linkedin_csv_path
        self.gmail_service = gmail_service
        self.calendar_service = calendar_service
        self.storage_path = Path(storage_path)

        # In-memory registry keyed by canonical name
        self._people: dict[str, PersonRecord] = {}
        self._load()

    def _load(self) -> None:
        """Load existing data from disk."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, 'r') as f:
                    data = json.load(f)
                    for item in data:
                        record = PersonRecord.from_dict(item)
                        self._people[record.canonical_name.lower()] = record
            except Exception as e:
                logger.error(f"Failed to load people aggregator data: {e}")

    def save(self) -> None:
        """Persist data to disk."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.storage_path, 'w') as f:
                data = [p.to_dict() for p in self._people.values()]
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save people aggregator data: {e}")

    def add_person_from_source(
        self,
        name: str,
        source: str,
        email: Optional[str] = None,
        company: Optional[str] = None,
        position: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        meeting_count: int = 0,
        email_count: int = 0,
        mention_count: int = 0,
        seen_date: Optional[datetime] = None,
        related_note: Optional[str] = None,
    ) -> None:
        """
        Add or update a person from a specific source.

        Args:
            name: Person's name
            source: Source identifier (gmail, calendar, linkedin, obsidian, granola)
            email: Email address
            company: Company name
            position: Job position
            linkedin_url: LinkedIn profile URL
            meeting_count: Number of meetings
            email_count: Number of emails
            mention_count: Number of mentions
            seen_date: When this person was seen
            related_note: Path to related note
        """
        # Resolve to canonical name
        canonical = resolve_person_name(name)
        key = canonical.lower()

        # Get category from people dictionary
        category = "unknown"
        if canonical in PEOPLE_DICTIONARY:
            category = PEOPLE_DICTIONARY[canonical].get('category', 'unknown')

        # Create new record
        new_record = PersonRecord(
            canonical_name=canonical,
            email=email,
            sources=[source],
            first_seen=seen_date,
            last_seen=seen_date,
            company=company,
            position=position,
            linkedin_url=linkedin_url,
            meeting_count=meeting_count,
            email_count=email_count,
            mention_count=mention_count,
            related_notes=[related_note] if related_note else [],
            category=category,
        )

        # Merge with existing or add new
        if key in self._people:
            self._people[key] = self._people[key].merge(new_record)
        else:
            self._people[key] = new_record

    def sync_all_sources(self) -> dict[str, int]:
        """
        Sync all configured sources.

        Returns:
            Dict of source -> count of people added
        """
        results = {}

        # LinkedIn
        if self.linkedin_csv_path:
            connections = load_linkedin_connections(self.linkedin_csv_path)
            for conn in connections:
                full_name = f"{conn['first_name']} {conn['last_name']}".strip()
                self.add_person_from_source(
                    name=full_name,
                    source='linkedin',
                    email=conn['email'] if conn['email'] else None,
                    company=conn['company'],
                    position=conn['position'],
                    linkedin_url=conn['linkedin_url'],
                )
            results['linkedin'] = len(connections)

        # Gmail
        if self.gmail_service:
            contacts = extract_gmail_contacts(self.gmail_service)
            for contact in contacts:
                self.add_person_from_source(
                    name=contact['name'],
                    source='gmail',
                    email=contact['email'],
                    email_count=contact.get('email_count', 1),
                    seen_date=contact.get('last_contact'),
                )
            results['gmail'] = len(contacts)

        # Calendar
        if self.calendar_service:
            attendees = extract_calendar_attendees(self.calendar_service)
            for att in attendees:
                self.add_person_from_source(
                    name=att['name'],
                    source='calendar',
                    email=att.get('email'),
                    meeting_count=att.get('meeting_count', 1),
                    seen_date=att.get('last_meeting'),
                )
            results['calendar'] = len(attendees)

        self.save()
        return results

    def add_from_obsidian_note(
        self,
        file_path: str,
        content: str,
        note_date: Optional[datetime] = None,
    ) -> list[str]:
        """
        Extract and add people from an Obsidian note.

        Args:
            file_path: Path to the note
            content: Note content
            note_date: Date of the note

        Returns:
            List of people found
        """
        people = extract_people_from_text(content)

        # Determine source based on path
        source = 'granola' if 'granola' in file_path.lower() else 'obsidian'

        for name in people:
            self.add_person_from_source(
                name=name,
                source=source,
                mention_count=1,
                seen_date=note_date,
                related_note=file_path,
            )

        return people

    def get_all_people(self) -> list[PersonRecord]:
        """Get all people records."""
        return list(self._people.values())

    def search(self, query: str) -> list[PersonRecord]:
        """
        Search people by name or email.

        Args:
            query: Search query

        Returns:
            List of matching PersonRecords
        """
        query_lower = query.lower()
        results = []

        for person in self._people.values():
            if query_lower in person.canonical_name.lower():
                results.append(person)
            elif person.email and query_lower in person.email.lower():
                results.append(person)
            elif any(query_lower in alias.lower() for alias in person.aliases):
                results.append(person)

        return results

    def get_person(self, name: str) -> Optional[PersonRecord]:
        """
        Get a person by name.

        Args:
            name: Person name (will be resolved)

        Returns:
            PersonRecord or None
        """
        canonical = resolve_person_name(name)
        return self._people.get(canonical.lower())

    def get_person_summary(self, name: str) -> Optional[dict]:
        """
        Get summary dict for a person.

        Args:
            name: Person name

        Returns:
            Summary dict or None
        """
        person = self.get_person(name)
        if not person:
            return None

        return {
            'name': person.canonical_name,
            'email': person.email,
            'company': person.company,
            'position': person.position,
            'sources': person.sources,
            'meeting_count': person.meeting_count,
            'email_count': person.email_count,
            'mention_count': person.mention_count,
            'last_seen': person.last_seen.isoformat() if person.last_seen else None,
            'related_notes': person.related_notes[:10],  # Limit to 10
            'category': person.category,
        }

    def get_statistics(self) -> dict:
        """Get statistics about aggregated people."""
        total = len(self._people)
        by_source = {}

        for person in self._people.values():
            for source in person.sources:
                by_source[source] = by_source.get(source, 0) + 1

        return {
            'total_people': total,
            'by_source': by_source,
        }


# Singleton instance
_aggregator: Optional[PeopleAggregator] = None


def get_people_aggregator(
    linkedin_csv_path: Optional[str] = "./data/LinkedInConnections.csv",
    gmail_service=None,
    calendar_service=None,
) -> PeopleAggregator:
    """Get or create people aggregator singleton."""
    global _aggregator
    if _aggregator is None:
        _aggregator = PeopleAggregator(
            linkedin_csv_path=linkedin_csv_path,
            gmail_service=gmail_service,
            calendar_service=calendar_service,
        )
    return _aggregator
