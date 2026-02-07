"""
Briefings API endpoints.
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from api.services.briefings import get_briefings_service

router = APIRouter(prefix="/api", tags=["briefings"])


class BriefingRequest(BaseModel):
    """Request for stakeholder briefing."""
    person_name: str
    email: Optional[str] = None  # v2: Optional email for better resolution


class BriefingResponse(BaseModel):
    """Response with stakeholder briefing."""
    status: str
    briefing: Optional[str] = None
    message: Optional[str] = None
    person_name: str
    metadata: Optional[dict] = None
    sources: Optional[list[str]] = None
    action_items_count: Optional[int] = None
    notes_count: Optional[int] = None


@router.post("/briefing", response_model=BriefingResponse)
async def get_briefing(request: BriefingRequest) -> BriefingResponse:
    """
    **Generate a comprehensive briefing about a person** for meeting prep or relationship context.

    Use this when you need:
    - "Tell me about John Smith before my meeting"
    - "What's my history with Sarah?"
    - "Prepare me for my 1:1 with Mike"

    Aggregates context from:
    - People metadata (LinkedIn, Gmail, Calendar)
    - Vault notes mentioning them
    - Action items involving them
    - Recent interaction history

    For just resolving a name to email/contact info, use `people_v2_resolve` instead.
    """
    if not request.person_name.strip():
        raise HTTPException(status_code=400, detail="Person name cannot be empty")

    service = get_briefings_service()
    result = await service.generate_briefing(request.person_name, email=request.email)

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("message"))

    return BriefingResponse(**result)


@router.get("/briefing/{person_name}", response_model=BriefingResponse)
async def get_briefing_by_name(
    person_name: str,
    email: Optional[str] = Query(default=None, description="Optional email for better resolution")
) -> BriefingResponse:
    """
    **Generate a briefing by person name** (convenience GET endpoint).

    Same as POST /briefing but with name in URL. Use for:
    - "Tell me about {person_name}"
    - Quick lookup by name

    Provide email parameter for better resolution if you have it.
    """
    if not person_name.strip():
        raise HTTPException(status_code=400, detail="Person name cannot be empty")

    service = get_briefings_service()
    result = await service.generate_briefing(person_name, email=email)

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("message"))

    return BriefingResponse(**result)
