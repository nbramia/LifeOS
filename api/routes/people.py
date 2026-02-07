"""
People API endpoints for LifeOS.

Provides access to aggregated people from the v2 EntityResolver system.
"""
from datetime import datetime, timezone
from typing import Optional
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.services.entity_resolver import get_entity_resolver
from api.services.person_entity import get_person_entity_store
from api.services.interaction_store import get_interaction_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/people", tags=["people"])


class PersonResponse(BaseModel):
    """Response model for a person."""
    canonical_name: str
    email: Optional[str] = None
    company: Optional[str] = None
    position: Optional[str] = None
    sources: list[str]
    meeting_count: int = 0
    email_count: int = 0
    mention_count: int = 0
    last_seen: Optional[str] = None
    category: str = "unknown"
    entity_id: Optional[str] = None
    linkedin_url: Optional[str] = None
    display_name: Optional[str] = None
    aliases: Optional[list[str]] = None
    birthday: Optional[str] = None  # ISO format date string
    # Relationship context fields (for smart routing)
    relationship_strength: float = 0.0  # 0-100 scale
    active_channels: list[str] = []  # Channels with activity in last 7 days
    days_since_contact: int = 999  # 999 = never contacted


class SearchResponse(BaseModel):
    """Response for search endpoint."""
    people: list[PersonResponse]
    count: int
    query: str


class StatisticsResponse(BaseModel):
    """Response for statistics endpoint."""
    total_people: int
    by_source: dict[str, int]
    by_category: dict[str, int]


class EntityResolveRequest(BaseModel):
    """Request for entity resolution."""
    name: Optional[str] = None
    email: Optional[str] = None
    context_path: Optional[str] = None
    create_if_missing: bool = False


class EntityResolveResponse(BaseModel):
    """Response from entity resolution."""
    found: bool
    is_new: bool = False
    confidence: float = 0.0
    match_type: str = ""
    entity: Optional[PersonResponse] = None


class InteractionResponse(BaseModel):
    """Response model for an interaction."""
    id: str
    person_id: str
    timestamp: str
    source_type: str
    title: str
    snippet: Optional[str] = None
    source_link: str = ""
    source_badge: str = ""


class InteractionsResponse(BaseModel):
    """Response for interactions list."""
    interactions: list[InteractionResponse]
    count: int
    formatted_history: str = ""


def _entity_to_response(entity, include_channels: bool = True) -> PersonResponse:
    """
    Convert PersonEntity to API response.

    Args:
        entity: PersonEntity to convert
        include_channels: If True, fetch active_channels from InteractionStore.
                         Set to False for bulk operations to improve performance.
    """
    # Basic fields from entity
    response = PersonResponse(
        canonical_name=entity.canonical_name,
        email=entity.emails[0] if entity.emails else None,
        company=entity.company,
        position=entity.position,
        sources=entity.sources,
        meeting_count=entity.meeting_count,
        email_count=entity.email_count,
        mention_count=entity.mention_count,
        last_seen=entity.last_seen.isoformat() if entity.last_seen else None,
        category=entity.category,
        entity_id=entity.id,
        linkedin_url=entity.linkedin_url,
        display_name=entity.display_name,
        aliases=entity.aliases,
        birthday=entity.birthday,  # Already "MM-DD" string or None
        # Relationship context - strength comes from entity directly
        relationship_strength=entity.relationship_strength,
    )

    # Compute days_since_contact from last_seen
    if entity.last_seen:
        now = datetime.now(timezone.utc)
        last = entity.last_seen
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        response.days_since_contact = (now - last).days
    else:
        response.days_since_contact = 999

    # Fetch active channels if requested (uses new get_last_interaction_by_source)
    if include_channels and entity.id:
        try:
            interaction_store = get_interaction_store()
            recency_by_source = interaction_store.get_last_interaction_by_source(entity.id)

            now = datetime.now(timezone.utc)
            active = []
            for source_type, last_dt in recency_by_source.items():
                if last_dt:
                    last_aware = last_dt if last_dt.tzinfo else last_dt.replace(tzinfo=timezone.utc)
                    days_ago = (now - last_aware).days
                    if days_ago <= 7:  # Active = within 7 days
                        active.append(source_type)
            response.active_channels = active
        except Exception as e:
            logger.debug(f"Could not get active channels for {entity.id}: {e}")
            response.active_channels = []

    return response


@router.get("/search", response_model=SearchResponse)
async def search_people(
    q: str = Query(..., description="Search query for name or email"),
):
    """Search for people by name or email."""
    store = get_person_entity_store()
    all_entities = store.get_all()

    # Search by name, email, or aliases
    query_lower = q.lower()
    results = []
    for entity in all_entities:
        if query_lower in entity.canonical_name.lower():
            results.append(entity)
        elif any(query_lower in email.lower() for email in entity.emails):
            results.append(entity)
        elif any(query_lower in alias.lower() for alias in entity.aliases):
            results.append(entity)
        elif entity.display_name and query_lower in entity.display_name.lower():
            results.append(entity)

    # Sort by relevance (exact matches first, then by last_seen)
    results.sort(
        key=lambda e: (
            e.canonical_name.lower() != query_lower,  # Exact match first
            -(e.last_seen.timestamp() if e.last_seen else 0)  # Recent first
        )
    )

    return SearchResponse(
        people=[_entity_to_response(e) for e in results],
        count=len(results),
        query=q,
    )


@router.get("/person/{name}", response_model=PersonResponse)
async def get_person(name: str):
    """Get a specific person by name."""
    resolver = get_entity_resolver()
    result = resolver.resolve(name=name)

    if not result or not result.entity:
        raise HTTPException(status_code=404, detail=f"Person '{name}' not found")

    return _entity_to_response(result.entity)


@router.get("/statistics", response_model=StatisticsResponse)
async def get_statistics():
    """Get statistics about aggregated people."""
    store = get_person_entity_store()
    stats = store.get_statistics()

    return StatisticsResponse(
        total_people=stats.get('total', 0),
        by_source=stats.get('by_source', {}),
        by_category=stats.get('by_category', {}),
    )


@router.get("/list", response_model=SearchResponse)
async def list_people(
    limit: int = Query(default=50, ge=1, le=500, description="Max results"),
    category: Optional[str] = Query(default=None, description="Filter by category"),
):
    """List all people, optionally filtered by category."""
    store = get_person_entity_store()
    all_entities = store.get_all()

    # Filter by category if specified
    if category:
        all_entities = [e for e in all_entities if e.category == category]

    # Sort by last_seen (most recent first)
    all_entities.sort(
        key=lambda e: e.last_seen or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )

    # Limit results
    all_entities = all_entities[:limit]

    return SearchResponse(
        people=[_entity_to_response(e) for e in all_entities],
        count=len(all_entities),
        query="*",
    )


@router.post("/resolve", response_model=EntityResolveResponse)
async def resolve_entity(request: EntityResolveRequest) -> EntityResolveResponse:
    """
    **PRIMARY TOOL for finding someone's email, full name, and contact info from a nickname or partial name.**

    Use this FIRST when you need to:
    - Find someone's email address (e.g., "john" -> john@example.com)
    - Get someone's full/canonical name (e.g., "john" -> "John Smith")
    - Look up contact details before searching Gmail, Calendar, or other sources

    Example: To find emails to/from "Tay", first call this with {"name": "tay"} to get their email,
    then use that email in gmail_search with "to:email" or "from:email".

    Returns the resolved entity with email, canonical_name, company, position, aliases, and LinkedIn URL.
    """
    if not request.name and not request.email:
        raise HTTPException(status_code=400, detail="Must provide name or email")

    resolver = get_entity_resolver()
    result = resolver.resolve(
        name=request.name,
        email=request.email,
        context_path=request.context_path,
        create_if_missing=request.create_if_missing,
    )

    if not result:
        return EntityResolveResponse(found=False)

    return EntityResolveResponse(
        found=True,
        is_new=result.is_new,
        confidence=result.confidence,
        match_type=result.match_type,
        entity=_entity_to_response(result.entity),
    )


@router.get("/entity/{entity_id}", response_model=PersonResponse)
async def get_entity(entity_id: str):
    """Get a specific entity by ID."""
    store = get_person_entity_store()
    entity = store.get_by_id(entity_id)

    if not entity:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_id}' not found")

    return _entity_to_response(entity)


@router.get("/entity/{entity_id}/interactions", response_model=InteractionsResponse)
async def get_entity_interactions(
    entity_id: str,
    days_back: int = Query(default=90, ge=1, le=365, description="Days to look back"),
    limit: int = Query(default=50, ge=1, le=200, description="Max interactions"),
):
    """
    Get interaction history for a specific entity.

    Returns a list of interactions (emails, meetings, note mentions) with
    links to the original sources.
    """
    # Verify entity exists
    store = get_person_entity_store()
    entity = store.get_by_id(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_id}' not found")

    # Get interactions
    interaction_store = get_interaction_store()
    interactions = interaction_store.get_for_person(entity_id, days_back=days_back, limit=limit)
    formatted = interaction_store.format_interaction_history(entity_id, days_back=days_back, limit=limit)

    return InteractionsResponse(
        interactions=[
            InteractionResponse(
                id=i.id,
                person_id=i.person_id,
                timestamp=i.timestamp.isoformat(),
                source_type=i.source_type,
                title=i.title,
                snippet=i.snippet,
                source_link=i.source_link,
                source_badge=i.source_badge,
            )
            for i in interactions
        ],
        count=len(interactions),
        formatted_history=formatted,
    )


# Legacy v2 endpoints (redirect to main endpoints for backward compatibility)
@router.post("/v2/resolve", response_model=EntityResolveResponse, include_in_schema=False)
async def resolve_entity_v2(request: EntityResolveRequest) -> EntityResolveResponse:
    """Legacy v2 endpoint - redirects to main resolve endpoint."""
    return await resolve_entity(request)


@router.get("/v2/entities", response_model=SearchResponse, include_in_schema=False)
async def list_entities_v2(
    limit: int = Query(default=50, ge=1, le=500),
    category: Optional[str] = Query(default=None),
):
    """Legacy v2 endpoint - redirects to main list endpoint."""
    return await list_people(limit=limit, category=category)


@router.get("/v2/entity/{entity_id}", response_model=PersonResponse, include_in_schema=False)
async def get_entity_v2(entity_id: str):
    """Legacy v2 endpoint - redirects to main entity endpoint."""
    return await get_entity(entity_id)


@router.get("/v2/entity/{entity_id}/interactions", response_model=InteractionsResponse, include_in_schema=False)
async def get_entity_interactions_v2(
    entity_id: str,
    days_back: int = Query(default=90, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Legacy v2 endpoint - redirects to main interactions endpoint."""
    return await get_entity_interactions(entity_id, days_back=days_back, limit=limit)


@router.get("/v2/statistics", include_in_schema=False)
async def get_v2_statistics():
    """Legacy v2 endpoint - redirects to main statistics."""
    store = get_person_entity_store()
    interaction_store = get_interaction_store()

    entity_stats = store.get_statistics()
    interaction_stats = interaction_store.get_statistics()

    return {
        "entities": entity_stats,
        "interactions": interaction_stats,
    }
