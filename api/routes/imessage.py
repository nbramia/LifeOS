"""
iMessage API endpoints for LifeOS.

Provides search and retrieval of iMessage/SMS conversations.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.services.imessage import get_imessage_store, IMessageRecord, resolve_entity_id

router = APIRouter(prefix="/api/imessage", tags=["imessage"])


class MessageResponse(BaseModel):
    """Response model for an iMessage/SMS message."""
    text: str
    timestamp: str
    is_from_me: bool
    handle: str
    handle_normalized: Optional[str] = None
    service: str
    person_entity_id: Optional[str] = None


class SearchResponse(BaseModel):
    """Response for search endpoint."""
    messages: list[MessageResponse]
    count: int
    query: Optional[str] = None


class ConversationResponse(BaseModel):
    """Response for a recent conversation summary."""
    handle_normalized: str
    handle: str
    person_entity_id: Optional[str] = None
    message_count: int
    last_message: str
    sent: int
    received: int


class ConversationsResponse(BaseModel):
    """Response for recent conversations endpoint."""
    conversations: list[ConversationResponse]
    count: int


class StatisticsResponse(BaseModel):
    """Response for statistics endpoint."""
    total_messages: int
    by_service: dict[str, int]
    sent: int
    received: int
    unique_contacts: int
    messages_with_entity: int
    oldest_message: Optional[str] = None
    newest_message: Optional[str] = None


def _record_to_response(record: IMessageRecord) -> MessageResponse:
    """Convert IMessageRecord to API response."""
    return MessageResponse(
        text=record.text,
        timestamp=record.timestamp.isoformat(),
        is_from_me=record.is_from_me,
        handle=record.handle,
        handle_normalized=record.handle_normalized,
        service=record.service,
        person_entity_id=record.person_entity_id,
    )


@router.get("/search", response_model=SearchResponse)
async def search_messages(
    q: Optional[str] = Query(default=None, description="Search query for message text (case-insensitive substring match)"),
    phone: Optional[str] = Query(default=None, description="Filter by phone number (E.164 format, e.g., +15551234567)"),
    entity_id: Optional[str] = Query(default=None, description="Filter by PersonEntity ID"),
    after: Optional[str] = Query(default=None, description="Messages after date (YYYY-MM-DD or ISO format)"),
    before: Optional[str] = Query(default=None, description="Messages before date (YYYY-MM-DD or ISO format)"),
    direction: Optional[str] = Query(default=None, description="Filter by direction: 'sent' or 'received'"),
    max_results: int = Query(default=50, ge=1, le=200, description="Maximum results to return"),
):
    """
    **Search iMessage/SMS history.**

    Search your text message history by content, phone number, or person.
    Results are returned in reverse chronological order (newest first).

    **Search methods:**
    - Text search: `q=dinner` finds messages containing "dinner"
    - Phone filter: `phone=+15551234567` finds messages with that number
    - Person filter: `entity_id=abc123` finds messages with that person
    - Date range: `after=2024-01-01&before=2024-02-01`
    - Direction: `direction=sent` or `direction=received`

    **Tips:**
    - To search messages with a specific person, first use `people_v2_resolve` to get their entity_id
    - Combine filters: `q=meeting&entity_id=abc123&after=2024-01-01`
    - Phone numbers should be in E.164 format (e.g., +15551234567)

    Returns message text, timestamp, direction, and associated person if resolved.
    """
    if not any([q, phone, entity_id]):
        raise HTTPException(
            status_code=400,
            detail="At least one search parameter is required (q, phone, or entity_id)"
        )

    try:
        store = get_imessage_store()

        # Validate direction
        if direction and direction not in ("sent", "received"):
            raise HTTPException(
                status_code=400,
                detail="direction must be 'sent' or 'received'"
            )

        # Resolve entity_id if it's not a UUID (e.g. name slug like "john-doe")
        resolved_entity_id = entity_id
        if entity_id:
            resolved_entity_id = resolve_entity_id(entity_id)
            if not resolved_entity_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not resolve person '{entity_id}'. Use lifeos_people_search first to find the correct entity ID."
                )

        # Date bounds are normalized inside query_messages (date-only values are
        # expanded to whole local days), so pass the raw strings through.
        messages = store.query_messages(
            entity_id=resolved_entity_id,
            phone=phone,
            search_term=q,
            start_date=after,
            end_date=before,
            direction=direction,
            limit=max_results,
        )

        return SearchResponse(
            messages=[_record_to_response(m) for m in messages],
            count=len(messages),
            query=q,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search messages: {e}")


@router.get("/conversations", response_model=ConversationsResponse)
async def get_recent_conversations(
    days: int = Query(default=7, ge=1, le=90, description="Days to look back"),
    limit: int = Query(default=20, ge=1, le=100, description="Maximum conversations to return"),
):
    """
    **Get recent text message conversations.**

    Returns a summary of recent conversations (contacts you've messaged),
    ordered by most recent message. Useful for seeing who you've been
    texting recently.

    Each conversation includes:
    - Phone number (normalized and raw)
    - Associated PersonEntity ID if resolved
    - Message counts (total, sent, received)
    - Last message timestamp
    """
    try:
        store = get_imessage_store()
        conversations = store.get_recent_conversations(days=days, limit=limit)

        return ConversationsResponse(
            conversations=[
                ConversationResponse(
                    handle_normalized=c["handle_normalized"],
                    handle=c["handle"],
                    person_entity_id=c.get("person_entity_id"),
                    message_count=c["message_count"],
                    last_message=c["last_message"],
                    sent=c["sent"],
                    received=c["received"],
                )
                for c in conversations
            ],
            count=len(conversations),
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get conversations: {e}")


@router.get("/statistics", response_model=StatisticsResponse)
async def get_statistics():
    """
    **Get iMessage database statistics.**

    Returns overall statistics about the iMessage export including:
    - Total message count
    - Breakdown by service (iMessage, SMS, RCS)
    - Sent vs received counts
    - Number of unique contacts
    - Date range of messages
    """
    try:
        store = get_imessage_store()
        stats = store.get_statistics()

        return StatisticsResponse(
            total_messages=stats["total_messages"],
            by_service=stats["by_service"],
            sent=stats["sent"],
            received=stats["received"],
            unique_contacts=stats["unique_contacts"],
            messages_with_entity=stats["messages_with_entity"],
            oldest_message=stats["oldest_message"],
            newest_message=stats["newest_message"],
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get statistics: {e}")


@router.get("/person/{entity_id}", response_model=SearchResponse)
async def get_messages_for_person(
    entity_id: str,
    days: int = Query(default=30, ge=1, le=365, description="Days to look back"),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum messages to return"),
):
    """
    **Get message history with a specific person.**

    Retrieves all messages exchanged with a person by their entity ID.
    Results are returned in reverse chronological order.

    Use `people_v2_resolve` first to get the entity_id for a person.
    """
    try:
        # Resolve entity_id if it's not a UUID (e.g. name slug like "john-doe")
        resolved_entity_id = resolve_entity_id(entity_id)
        if not resolved_entity_id:
            raise HTTPException(
                status_code=404,
                detail=f"Could not resolve person '{entity_id}'. Use lifeos_people_search first to find the correct entity ID."
            )

        store = get_imessage_store()
        since = datetime.now(timezone.utc) - __import__('datetime').timedelta(days=days)

        messages = store.get_messages_for_entity(
            entity_id=resolved_entity_id,
            limit=limit,
            since=since,
        )

        return SearchResponse(
            messages=[_record_to_response(m) for m in messages],
            count=len(messages),
            query=f"entity:{entity_id}",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get messages: {e}")
