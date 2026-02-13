"""
Google Calendar integration service for LifeOS.

Fetches and indexes calendar events from Google Calendar.
"""
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from api.services.google_auth import get_google_auth, GoogleAccount

logger = logging.getLogger(__name__)

# Local timezone
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


@dataclass
class CalendarAttachment:
    """Represents a file attachment on a calendar event."""
    file_url: str
    title: str
    mime_type: Optional[str] = None
    icon_link: Optional[str] = None


@dataclass
class CalendarEvent:
    """Represents a calendar event."""
    event_id: str
    title: str
    start_time: datetime
    end_time: datetime
    source_account: str  # "personal" or "work"
    attendees: list[str] = field(default_factory=list)
    description: Optional[str] = None
    location: Optional[str] = None
    is_all_day: bool = False
    calendar_id: str = "primary"
    html_link: Optional[str] = None  # Google Calendar URL for this event
    attachments: list[CalendarAttachment] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dict for indexing."""
        return {
            "event_id": self.event_id,
            "title": self.title,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "attendees": self.attendees,
            "description": self.description or "",
            "location": self.location or "",
            "is_all_day": self.is_all_day,
            "source": "google_calendar",
            "source_account": self.source_account,
            "html_link": self.html_link or "",
        }

    def to_text(self) -> str:
        """Convert to searchable text for embedding."""
        parts = [self.title]
        if self.description:
            parts.append(self.description)
        if self.attendees:
            parts.append(f"Attendees: {', '.join(self.attendees)}")
        if self.location:
            parts.append(f"Location: {self.location}")
        return "\n".join(parts)


def format_event_time(dt: datetime, is_all_day: bool = False) -> str:
    """
    Format event time for display.

    Args:
        dt: Datetime to format
        is_all_day: Whether this is an all-day event

    Returns:
        Formatted time string
    """
    # Convert to local timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_dt = dt.astimezone(LOCAL_TZ)

    if is_all_day:
        return local_dt.strftime("%A, %B %d, %Y")
    else:
        return local_dt.strftime("%A, %B %d, %Y at %I:%M %p")


def parse_attendees(raw_attendees: Optional[list]) -> list[str]:
    """
    Parse attendees from Google Calendar API response.

    Args:
        raw_attendees: List of attendee dicts from API

    Returns:
        List of attendee names/emails
    """
    if not raw_attendees:
        return []

    attendees = []
    for attendee in raw_attendees:
        # Prefer display name, fall back to email
        name = attendee.get("displayName") or attendee.get("email", "")
        if name:
            attendees.append(name)

    return attendees


class CalendarService:
    """
    Google Calendar service.

    Provides methods to fetch and search calendar events.
    """

    def __init__(self, account_type: GoogleAccount = GoogleAccount.PERSONAL):
        """
        Initialize calendar service.

        Args:
            account_type: Which Google account to use
        """
        self.account_type = account_type
        self._service = None

    @property
    def service(self):
        """Get or create Google Calendar API service."""
        if self._service is None:
            auth = get_google_auth(self.account_type)
            credentials = auth.get_credentials()
            self._service = build("calendar", "v3", credentials=credentials)
        return self._service

    def get_upcoming_events(
        self,
        days: int = 7,
        max_results: int = 50,
        calendar_id: str = "primary"
    ) -> list[CalendarEvent]:
        """
        Get upcoming events.

        Args:
            days: Number of days to look ahead
            max_results: Maximum events to return
            calendar_id: Calendar ID to query

        Returns:
            List of CalendarEvent objects
        """
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(days=days)

        return self._fetch_events(
            time_min=now,
            time_max=time_max,
            max_results=max_results,
            calendar_id=calendar_id
        )

    def get_events_in_range(
        self,
        start_date: datetime,
        end_date: datetime,
        max_results: int = 100,
        calendar_id: str = "primary"
    ) -> list[CalendarEvent]:
        """
        Get events within a date range.

        Args:
            start_date: Start of range
            end_date: End of range
            max_results: Maximum events to return
            calendar_id: Calendar ID to query

        Returns:
            List of CalendarEvent objects
        """
        # Ensure timezone aware
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        return self._fetch_events(
            time_min=start_date,
            time_max=end_date,
            max_results=max_results,
            calendar_id=calendar_id
        )

    def search_events(
        self,
        query: Optional[str] = None,
        attendee: Optional[str] = None,
        days_back: int = 30,
        days_forward: int = 30,
        calendar_id: str = "primary"
    ) -> list[CalendarEvent]:
        """
        Search events by keyword or attendee.

        Args:
            query: Keyword to search in title/description
            attendee: Filter by attendee name/email
            days_back: How many days in the past to search
            days_forward: How many days in the future to search
            calendar_id: Calendar ID to query

        Returns:
            List of matching CalendarEvent objects
        """
        now = datetime.now(timezone.utc)
        time_min = now - timedelta(days=days_back)
        time_max = now + timedelta(days=days_forward)

        # Fetch all events in range
        events = self._fetch_events(
            time_min=time_min,
            time_max=time_max,
            max_results=250,
            calendar_id=calendar_id,
            query=query  # Google Calendar API supports q parameter
        )

        # Filter by attendee if specified
        if attendee:
            attendee_lower = attendee.lower()
            events = [
                e for e in events
                if any(attendee_lower in a.lower() for a in e.attendees)
                or attendee_lower in e.title.lower()
            ]

        return events

    def _fetch_events(
        self,
        time_min: datetime,
        time_max: datetime,
        max_results: int,
        calendar_id: str,
        query: Optional[str] = None
    ) -> list[CalendarEvent]:
        """
        Fetch events from Google Calendar API.

        Args:
            time_min: Start time
            time_max: End time
            max_results: Maximum results
            calendar_id: Calendar to query
            query: Optional search query

        Returns:
            List of CalendarEvent objects
        """
        try:
            request_params = {
                "calendarId": calendar_id,
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "maxResults": max_results,
                "singleEvents": True,
                "orderBy": "startTime",
            }

            if query:
                request_params["q"] = query

            result = self.service.events().list(**request_params).execute()
            items = result.get("items", [])

            events = []
            for item in items:
                event = self._parse_event(item)
                if event:
                    events.append(event)

            return events

        except Exception as e:
            logger.error(f"Failed to fetch calendar events: {e}")
            return []

    def create_event(
        self,
        title: str,
        start_time: str,
        end_time: str,
        attendees: list[str] | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        """
        Create a new calendar event.

        Args:
            title: Event title
            start_time: ISO datetime string
            end_time: ISO datetime string
            attendees: List of email addresses
            description: Event description
            location: Event location
            calendar_id: Calendar to create in

        Returns:
            Created CalendarEvent
        """
        body: dict = {
            "summary": title,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        }
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        result = self.service.events().insert(
            calendarId=calendar_id,
            body=body,
            sendUpdates="all",
        ).execute()

        event = self._parse_event(result)
        if not event:
            raise RuntimeError("Failed to parse created event")
        return event

    def update_event(
        self,
        event_id: str,
        title: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        attendees: list[str] | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        """
        Update an existing calendar event.

        Fetches the current event, merges changes, and updates.

        Args:
            event_id: ID of the event to update
            title: New title (None = keep existing)
            start_time: New start ISO datetime (None = keep existing)
            end_time: New end ISO datetime (None = keep existing)
            attendees: New attendee list (None = keep existing)
            description: New description (None = keep existing)
            location: New location (None = keep existing)
            calendar_id: Calendar containing the event

        Returns:
            Updated CalendarEvent
        """
        existing = self.service.events().get(
            calendarId=calendar_id, eventId=event_id
        ).execute()

        if title is not None:
            existing["summary"] = title
        if start_time is not None:
            existing["start"] = {"dateTime": start_time}
        if end_time is not None:
            existing["end"] = {"dateTime": end_time}
        if attendees is not None:
            existing["attendees"] = [{"email": e} for e in attendees]
        if description is not None:
            existing["description"] = description
        if location is not None:
            existing["location"] = location

        result = self.service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=existing,
            sendUpdates="all",
        ).execute()

        event = self._parse_event(result)
        if not event:
            raise RuntimeError("Failed to parse updated event")
        return event

    def delete_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
    ) -> bool:
        """
        Delete a calendar event.

        Args:
            event_id: ID of the event to delete
            calendar_id: Calendar containing the event

        Returns:
            True on success
        """
        self.service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates="all",
        ).execute()
        return True

    def _parse_event(self, item: dict) -> Optional[CalendarEvent]:
        """
        Parse a raw API event into CalendarEvent.

        Args:
            item: Raw event dict from API

        Returns:
            CalendarEvent or None if parsing fails
        """
        try:
            event_id = item.get("id", "")
            title = item.get("summary", "No Title")

            # Parse start/end times
            start = item.get("start", {})
            end = item.get("end", {})

            # Check if all-day event
            is_all_day = "date" in start and "dateTime" not in start

            if is_all_day:
                # All-day events use date format
                start_time = datetime.strptime(start["date"], "%Y-%m-%d")
                start_time = start_time.replace(tzinfo=timezone.utc)
                end_time = datetime.strptime(end["date"], "%Y-%m-%d")
                end_time = end_time.replace(tzinfo=timezone.utc)
            else:
                # Timed events use dateTime format
                start_str = start.get("dateTime", "")
                end_str = end.get("dateTime", "")
                start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

            # Parse attendees
            attendees = parse_attendees(item.get("attendees"))

            # Parse attachments (Google Drive files attached to event)
            attachments = []
            for att in item.get("attachments", []):
                attachments.append(CalendarAttachment(
                    file_url=att.get("fileUrl", ""),
                    title=att.get("title", "Attachment"),
                    mime_type=att.get("mimeType"),
                    icon_link=att.get("iconLink"),
                ))

            return CalendarEvent(
                event_id=event_id,
                title=title,
                start_time=start_time,
                end_time=end_time,
                attendees=attendees,
                description=item.get("description"),
                location=item.get("location"),
                is_all_day=is_all_day,
                source_account=self.account_type.value,
                html_link=item.get("htmlLink"),
                attachments=attachments,
            )

        except Exception as e:
            logger.warning(f"Failed to parse event: {e}")
            return None


# Singleton services per account
_calendar_services: dict[GoogleAccount, CalendarService] = {}


def get_calendar_service(account_type: GoogleAccount = GoogleAccount.PERSONAL) -> CalendarService:
    """Get or create calendar service for an account."""
    if account_type not in _calendar_services:
        _calendar_services[account_type] = CalendarService(account_type)
    return _calendar_services[account_type]
