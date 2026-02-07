"""
Meeting Prep Service for LifeOS.

Generates intelligent meeting preparation context by combining calendar events
with relevant notes, past meetings, and attendee information from the vault.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from api.services.calendar import get_calendar_service, CalendarEvent, format_event_time
from api.services.google_auth import GoogleAccount
from api.services.hybrid_search import get_hybrid_search

logger = logging.getLogger(__name__)

# Local timezone for date handling
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


@dataclass
class RelatedNote:
    """A note related to a meeting."""
    title: str
    path: str
    relevance: str  # "attendee", "past_meeting", "topic"
    date: Optional[str] = None
    snippet: Optional[str] = None


@dataclass
class Attachment:
    """A file attachment from the calendar event."""
    title: str
    url: str
    mime_type: Optional[str] = None


@dataclass
class MeetingPrep:
    """Preparation context for a single meeting."""
    event_id: str
    title: str
    start_time: str  # Formatted time like "10:00 AM"
    end_time: str
    html_link: Optional[str]
    attendees: list[str]
    description: Optional[str]
    location: Optional[str]
    is_all_day: bool
    source_account: str
    related_notes: list[RelatedNote] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    agenda_summary: Optional[str] = None


@dataclass
class MeetingPrepResponse:
    """Response containing all meeting preps for a date."""
    date: str
    meetings: list[MeetingPrep]
    count: int


def _extract_time(dt: datetime) -> str:
    """Extract just the time portion formatted nicely."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_dt = dt.astimezone(LOCAL_TZ)
    return local_dt.strftime("%I:%M %p").lstrip("0")


def _truncate_description(description: Optional[str], max_length: int = 200) -> Optional[str]:
    """Truncate description to a reasonable length for display."""
    if not description:
        return None
    # Clean up whitespace
    clean = " ".join(description.split())
    if len(clean) <= max_length:
        return clean
    return clean[:max_length].rsplit(" ", 1)[0] + "..."


def _normalize_title_for_search(title: str) -> str:
    """
    Normalize meeting title for searching past instances.

    Removes date patterns, common prefixes, and extracts key identifiers.
    Examples:
        "1:1 with Alex - 2023-01-28" -> "1:1 with Alex"
        "Weekly Team Standup" -> "Team Standup"
        "Nathan-Sam" -> "Nathan Sam"
    """
    # Remove date patterns
    title = re.sub(r"\s*[-–]\s*\d{4}[-/]\d{2}[-/]\d{2}", "", title)
    title = re.sub(r"\s*\d{4}[-/]\d{2}[-/]\d{2}", "", title)
    title = re.sub(r"\s*\d{1,2}/\d{1,2}/\d{2,4}", "", title)

    # Remove common temporal words
    title = re.sub(r"\b(weekly|biweekly|monthly|daily)\b", "", title, flags=re.IGNORECASE)

    # Replace hyphens between names with spaces (Nathan-Brandon -> Nathan Brandon)
    title = re.sub(r"(\w)-(\w)", r"\1 \2", title)

    return title.strip()


def _extract_person_names(attendees: list[str], title: str) -> list[str]:
    """
    Extract person names from attendees and meeting title.

    Returns list of names suitable for searching People notes.
    """
    names = []

    # From attendees (prefer display names over emails)
    for attendee in attendees:
        if "@" in attendee:
            # Extract name part from email
            name_part = attendee.split("@")[0]
            # Convert john.smith to John Smith
            name = " ".join(word.capitalize() for word in name_part.replace(".", " ").split())
            if len(name) > 2:  # Skip initials
                names.append(name)
        else:
            names.append(attendee)

    # From title (e.g., "1:1 with Alex" -> "Alex")
    with_match = re.search(r"\bwith\s+(\w+(?:\s+\w+)?)", title, re.IGNORECASE)
    if with_match:
        names.append(with_match.group(1))

    # Deduplicate while preserving order
    seen = set()
    unique_names = []
    for name in names:
        name_lower = name.lower()
        if name_lower not in seen:
            seen.add(name_lower)
            unique_names.append(name)

    return unique_names


def _find_people_notes(names: list[str], search: any) -> list[RelatedNote]:
    """
    Find People notes for the given names.

    Searches for notes in People folders matching attendee names.
    """
    notes = []
    seen_paths = set()

    for name in names[:5]:  # Limit to avoid too many searches
        # Search for person's dedicated note
        query = f"{name} file:People"
        try:
            results = search.search(query, top_k=3, use_reranker=False)
            for result in results:
                file_path = result.get("file_path", "")
                file_name = result.get("file_name", "") or result.get("metadata", {}).get("file_name", "")

                # Skip archived content
                if "zArchive" in file_path:
                    continue

                # Check if this is actually a People note
                if "People" in file_path and file_path not in seen_paths:
                    # Verify the name appears in the filename
                    name_parts = name.lower().split()
                    file_lower = file_name.lower()
                    if any(part in file_lower for part in name_parts):
                        notes.append(RelatedNote(
                            title=file_name.replace(".md", ""),
                            path=file_path,
                            relevance="attendee",
                        ))
                        seen_paths.add(file_path)
                        break  # One note per person
        except Exception as e:
            logger.warning(f"Failed to search for person {name}: {e}")

    return notes


def _find_past_meetings(title: str, event_date: datetime, search: any) -> list[RelatedNote]:
    """
    Find past instances of similar meetings.

    Looks for meeting notes from previous occurrences of recurring meetings,
    or meetings with similar titles/topics.
    """
    notes = []
    seen_paths = set()

    # Normalize title for searching
    search_title = _normalize_title_for_search(title)
    if len(search_title) < 3:
        return notes

    # Search for past meeting notes
    query = f"{search_title} file:Meetings"
    try:
        results = search.search(query, top_k=10, use_reranker=False)

        for result in results:
            file_path = result.get("file_path", "")
            file_name = result.get("file_name", "") or result.get("metadata", {}).get("file_name", "")

            if file_path in seen_paths:
                continue

            # Skip archived content
            if "zArchive" in file_path:
                continue

            # Must be in Meetings folder
            if "Meeting" not in file_path:
                continue

            # Try to extract date from metadata or filename
            note_date = result.get("metadata", {}).get("date")
            if not note_date:
                # Try to extract from filename (pattern: ... YYYYMMDD.md)
                date_match = re.search(r"(\d{8})\.md$", file_name)
                if date_match:
                    try:
                        note_date = datetime.strptime(date_match.group(1), "%Y%m%d").strftime("%Y-%m-%d")
                    except ValueError:
                        pass

            # Skip if this is today's meeting (we want past ones)
            if note_date:
                try:
                    note_datetime = datetime.strptime(note_date[:10], "%Y-%m-%d")
                    event_date_only = event_date.replace(tzinfo=None) if event_date.tzinfo else event_date
                    if note_datetime.date() >= event_date_only.date():
                        continue
                except ValueError:
                    pass

            notes.append(RelatedNote(
                title=file_name.replace(".md", ""),
                path=file_path,
                relevance="past_meeting",
                date=note_date,
            ))
            seen_paths.add(file_path)

            if len(notes) >= 2:  # Limit to 2 past meetings
                break

    except Exception as e:
        logger.warning(f"Failed to search for past meetings: {e}")

    return notes


def _find_topic_notes(title: str, description: Optional[str], attendee_names: list[str], search: any) -> list[RelatedNote]:
    """
    Find notes related to the meeting topic.

    Searches for recent notes mentioning the meeting subject or attendees,
    excluding People notes and already-found meeting notes.
    """
    notes = []
    seen_paths = set()

    # Build search query from title and key description terms
    search_terms = [_normalize_title_for_search(title)]

    # Add attendee names to search
    for name in attendee_names[:2]:
        search_terms.append(name)

    # Extract key terms from description (if present)
    if description:
        # Look for potential topic keywords (capitalized words, project names)
        words = description.split()[:50]  # First 50 words
        for word in words:
            clean = re.sub(r"[^a-zA-Z0-9]", "", word)
            if len(clean) >= 4 and clean[0].isupper():
                search_terms.append(clean)

    query = " ".join(search_terms[:6])  # Limit query length

    try:
        results = search.search(query, top_k=10, use_reranker=False)

        for result in results:
            file_path = result.get("file_path", "")
            file_name = result.get("file_name", "") or result.get("metadata", {}).get("file_name", "")

            if file_path in seen_paths:
                continue

            # Skip archived content
            if "zArchive" in file_path:
                continue

            # Skip Lifelogs (not useful for meeting prep)
            if "Lifelogs" in file_path:
                continue

            # Skip People notes (handled separately)
            if "People" in file_path:
                continue

            # Skip meeting notes (handled separately)
            if "Meeting" in file_path:
                continue

            # Skip templates
            if "Template" in file_path:
                continue

            # Get snippet from content
            content = result.get("content", "")
            snippet = content[:150] + "..." if len(content) > 150 else content

            note_date = result.get("metadata", {}).get("date")

            notes.append(RelatedNote(
                title=file_name.replace(".md", ""),
                path=file_path,
                relevance="topic",
                date=note_date,
                snippet=snippet,
            ))
            seen_paths.add(file_path)

            if len(notes) >= 2:  # Limit to 2 topic notes
                break

    except Exception as e:
        logger.warning(f"Failed to search for topic notes: {e}")

    return notes


def get_meeting_prep(
    date: str,
    include_all_day: bool = False,
    max_related_notes: int = 4,
) -> MeetingPrepResponse:
    """
    Get meeting preparation context for a specific date.

    Fetches calendar events and finds relevant prep material for each:
    - People notes for attendees
    - Past instances of recurring meetings
    - Notes related to meeting topics

    Args:
        date: Date in YYYY-MM-DD format
        include_all_day: Whether to include all-day events (default False)
        max_related_notes: Maximum related notes per meeting (default 4)

    Returns:
        MeetingPrepResponse with all meetings and their prep context
    """
    # Parse date
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d")
        target_date = target_date.replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise ValueError(f"Invalid date format: {date}. Expected YYYY-MM-DD")

    # Set time range for the full day
    start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Fetch events from both calendars
    all_events: list[CalendarEvent] = []

    for account in [GoogleAccount.WORK, GoogleAccount.PERSONAL]:
        try:
            service = get_calendar_service(account)
            events = service.get_events_in_range(
                start_date=start_of_day,
                end_date=end_of_day,
                max_results=50,
            )
            all_events.extend(events)
        except Exception as e:
            logger.warning(f"Failed to fetch {account.value} calendar: {e}")

    # Sort by start time
    all_events.sort(key=lambda e: e.start_time)

    # Filter out all-day events if requested
    if not include_all_day:
        all_events = [e for e in all_events if not e.is_all_day]

    # Get hybrid search instance
    search = get_hybrid_search()

    # Build prep for each meeting
    meetings: list[MeetingPrep] = []

    for event in all_events:
        # Extract person names from attendees and title
        person_names = _extract_person_names(event.attendees, event.title)

        # Find related notes
        related_notes: list[RelatedNote] = []

        # 1. People notes for attendees
        people_notes = _find_people_notes(person_names, search)
        related_notes.extend(people_notes)

        # 2. Past meeting notes
        past_meetings = _find_past_meetings(event.title, event.start_time, search)
        related_notes.extend(past_meetings)

        # 3. Topic-related notes (if we have room)
        if len(related_notes) < max_related_notes:
            remaining = max_related_notes - len(related_notes)
            topic_notes = _find_topic_notes(
                event.title,
                event.description,
                person_names,
                search,
            )
            related_notes.extend(topic_notes[:remaining])

        # Truncate to max
        related_notes = related_notes[:max_related_notes]

        # Convert calendar attachments to our Attachment type
        attachments = [
            Attachment(
                title=att.title,
                url=att.file_url,
                mime_type=att.mime_type,
            )
            for att in event.attachments
        ]

        # Build prep object
        prep = MeetingPrep(
            event_id=event.event_id,
            title=event.title,
            start_time=_extract_time(event.start_time),
            end_time=_extract_time(event.end_time),
            html_link=event.html_link,
            attendees=event.attendees,
            description=_truncate_description(event.description),
            location=event.location,
            is_all_day=event.is_all_day,
            source_account=event.source_account,
            related_notes=related_notes,
            attachments=attachments,
            agenda_summary=_truncate_description(event.description, 100),
        )
        meetings.append(prep)

    return MeetingPrepResponse(
        date=date,
        meetings=meetings,
        count=len(meetings),
    )
