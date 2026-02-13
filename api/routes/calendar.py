"""
Calendar API endpoints for LifeOS.

Provides access to Google Calendar events.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.services.calendar import (
    get_calendar_service,
    CalendarEvent,
    format_event_time,
)
from api.services.google_auth import GoogleAccount
from api.services.meeting_prep import get_meeting_prep, MeetingPrep, RelatedNote

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


class CreateEventRequest(BaseModel):
    """Request to create a calendar event."""
    title: str
    start_time: str  # ISO datetime
    end_time: str    # ISO datetime
    attendees: list[str] = []
    description: Optional[str] = None
    location: Optional[str] = None
    account: str = "personal"


class UpdateEventRequest(BaseModel):
    """Request to update a calendar event."""
    title: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    attendees: Optional[list[str]] = None
    description: Optional[str] = None
    location: Optional[str] = None
    account: str = "personal"


class EventResponse(BaseModel):
    """Response model for a calendar event."""
    event_id: str
    title: str
    start_time: str
    end_time: str
    start_formatted: str
    end_formatted: str
    attendees: list[str]
    description: Optional[str] = None
    location: Optional[str] = None
    is_all_day: bool
    source_account: str


class UpcomingResponse(BaseModel):
    """Response for upcoming events endpoint."""
    events: list[EventResponse]
    count: int
    account: str


class SearchResponse(BaseModel):
    """Response for search endpoint."""
    events: list[EventResponse]
    count: int
    query: Optional[str] = None
    attendee: Optional[str] = None


class RelatedNoteResponse(BaseModel):
    """A note related to a meeting."""
    title: str
    path: str
    relevance: str  # "attendee", "past_meeting", "topic"
    date: Optional[str] = None
    snippet: Optional[str] = None


class AttachmentResponse(BaseModel):
    """A file attachment from the calendar event."""
    title: str
    url: str
    mime_type: Optional[str] = None


class MeetingPrepResponse(BaseModel):
    """Preparation context for a single meeting."""
    event_id: str
    title: str
    start_time: str
    end_time: str
    html_link: Optional[str] = None
    attendees: list[str]
    description: Optional[str] = None
    location: Optional[str] = None
    is_all_day: bool
    source_account: str
    related_notes: list[RelatedNoteResponse]
    attachments: list[AttachmentResponse] = []
    agenda_summary: Optional[str] = None


class MeetingPrepListResponse(BaseModel):
    """Response containing all meeting preps for a date."""
    date: str
    meetings: list[MeetingPrepResponse]
    count: int


def _event_to_response(event: CalendarEvent) -> EventResponse:
    """Convert CalendarEvent to API response."""
    return EventResponse(
        event_id=event.event_id,
        title=event.title,
        start_time=event.start_time.isoformat(),
        end_time=event.end_time.isoformat(),
        start_formatted=format_event_time(event.start_time, event.is_all_day),
        end_formatted=format_event_time(event.end_time, event.is_all_day),
        attendees=event.attendees,
        description=event.description,
        location=event.location,
        is_all_day=event.is_all_day,
        source_account=event.source_account,
    )


@router.get("/upcoming", response_model=UpcomingResponse)
async def get_upcoming_events(
    days: int = Query(default=7, ge=1, le=30, description="Number of days to look ahead"),
    account: str = Query(default="personal", description="Account: personal or work"),
    max_results: int = Query(default=50, ge=1, le=100, description="Maximum events to return"),
):
    """
    **Get upcoming calendar events** from Google Calendar.

    Use this to answer questions like:
    - "What's on my calendar today/this week?"
    - "Do I have any meetings tomorrow?"
    - "What's my schedule for the next few days?"

    Returns event title, start/end times, attendees, location, and description.
    Query both `account=personal` AND `account=work` for complete schedule.
    """
    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_calendar_service(account_type)
        events = service.get_upcoming_events(days=days, max_results=max_results)

        return UpcomingResponse(
            events=[_event_to_response(e) for e in events],
            count=len(events),
            account=account,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch calendar events: {e}")


@router.get("/search", response_model=SearchResponse)
async def search_events(
    q: Optional[str] = Query(default=None, description="Search query for title/description"),
    attendee: Optional[str] = Query(default=None, description="Filter by attendee name/email"),
    account: str = Query(default="personal", description="Account: personal or work"),
    days_back: int = Query(default=30, ge=1, le=365, description="Days to search in the past"),
    days_forward: int = Query(default=30, ge=1, le=365, description="Days to search in the future"),
):
    """
    **Search calendar events** by keyword or attendee.

    Use this for:
    - "When did I last meet with John?" → `attendee=john@email.com`
    - "Find meetings about project X" → `q=project X`
    - "When is my next 1:1 with Sarah?" → `attendee=sarah@email.com`

    **TIP**: Use `people_v2_resolve` first to get attendee's email for accurate filtering.

    Searches past 30 days and future 30 days by default.
    Query both personal and work accounts for complete results.
    """
    if not q and not attendee:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'q' (query) or 'attendee' is required"
        )

    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_calendar_service(account_type)
        events = service.search_events(
            query=q,
            attendee=attendee,
            days_back=days_back,
            days_forward=days_forward,
        )

        return SearchResponse(
            events=[_event_to_response(e) for e in events],
            count=len(events),
            query=q,
            attendee=attendee,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search calendar events: {e}")


@router.get("/events/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: str,
    account: str = Query(default="personal", description="Account: personal or work"),
):
    """Get a specific calendar event by ID."""
    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_calendar_service(account_type)

        # Fetch from API directly
        result = service.service.events().get(
            calendarId="primary",
            eventId=event_id
        ).execute()

        event = service._parse_event(result)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")

        return _event_to_response(event)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch event: {e}")


@router.post("/events", response_model=EventResponse)
async def create_event(request: CreateEventRequest):
    """
    **Create a Google Calendar event.**

    Creates an event on the specified account. Invite emails are
    automatically sent to attendees.
    """
    try:
        account_type = GoogleAccount.PERSONAL if request.account == "personal" else GoogleAccount.WORK
        service = get_calendar_service(account_type)
        event = service.create_event(
            title=request.title,
            start_time=request.start_time,
            end_time=request.end_time,
            attendees=request.attendees or None,
            description=request.description,
            location=request.location,
        )
        return _event_to_response(event)
    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create event: {e}")


@router.put("/events/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: str,
    request: UpdateEventRequest,
):
    """
    **Update an existing calendar event.**

    Only provided fields are updated; others remain unchanged.
    Update emails are sent to attendees.
    """
    try:
        account_type = GoogleAccount.PERSONAL if request.account == "personal" else GoogleAccount.WORK
        service = get_calendar_service(account_type)
        event = service.update_event(
            event_id=event_id,
            title=request.title,
            start_time=request.start_time,
            end_time=request.end_time,
            attendees=request.attendees,
            description=request.description,
            location=request.location,
        )
        return _event_to_response(event)
    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update event: {e}")


@router.delete("/events/{event_id}")
async def delete_event(
    event_id: str,
    account: str = Query(default="personal", description="Account: personal or work"),
):
    """
    **Delete a calendar event.**

    Cancellation emails are sent to attendees.
    """
    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_calendar_service(account_type)
        service.delete_event(event_id=event_id)
        return {"deleted": True, "event_id": event_id}
    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete event: {e}")


@router.get("/meeting-prep", response_model=MeetingPrepListResponse)
async def get_meeting_prep_endpoint(
    date: str = Query(
        description="Date in YYYY-MM-DD format (defaults to today)",
        default=None,
    ),
    include_all_day: bool = Query(
        default=False,
        description="Include all-day events",
    ),
    max_related_notes: int = Query(
        default=4,
        ge=1,
        le=10,
        description="Maximum related notes per meeting",
    ),
):
    """
    **Get meeting preparation context** for a specific date.

    Returns calendar events with intelligent prep material:
    - **People notes** for meeting attendees
    - **Past meetings** with similar titles (recurring meeting history)
    - **Related notes** mentioning meeting topics or attendees

    Use this to:
    - Prepare for today's meetings with relevant context
    - Auto-populate daily note with meeting prep section
    - Review what to discuss before a meeting

    Fetches from BOTH work and personal calendars automatically.
    """
    # Default to today if no date provided
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        result = get_meeting_prep(
            date=date,
            include_all_day=include_all_day,
            max_related_notes=max_related_notes,
        )

        # Convert dataclasses to Pydantic models
        meetings = []
        for m in result.meetings:
            related_notes = [
                RelatedNoteResponse(
                    title=n.title,
                    path=n.path,
                    relevance=n.relevance,
                    date=n.date,
                    snippet=n.snippet,
                )
                for n in m.related_notes
            ]
            attachments = [
                AttachmentResponse(
                    title=a.title,
                    url=a.url,
                    mime_type=a.mime_type,
                )
                for a in m.attachments
            ]
            meetings.append(MeetingPrepResponse(
                event_id=m.event_id,
                title=m.title,
                start_time=m.start_time,
                end_time=m.end_time,
                html_link=m.html_link,
                attendees=m.attendees,
                description=m.description,
                location=m.location,
                is_all_day=m.is_all_day,
                source_account=m.source_account,
                related_notes=related_notes,
                attachments=attachments,
                agenda_summary=m.agenda_summary,
            ))

        return MeetingPrepListResponse(
            date=result.date,
            meetings=meetings,
            count=result.count,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get meeting prep: {e}")
