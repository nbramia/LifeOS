"""
Vector Search API endpoint.

POST /api/search - Search the indexed vault for relevant content.
"""
import logging
import time
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.services.vectorstore import VectorStore
from api.services.hybrid_search import HybridSearch
from api.utils.date_parser import resolve_effective_dates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])

# Initialize vector store (singleton)
_vector_store: VectorStore | None = None
_hybrid_search: HybridSearch | None = None


def get_vector_store() -> VectorStore:
    """Get or create vector store instance."""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store


def get_hybrid_search() -> HybridSearch:
    """Get or create hybrid search instance."""
    global _hybrid_search
    if _hybrid_search is None:
        _hybrid_search = HybridSearch(vector_store=get_vector_store())
    return _hybrid_search


class SearchFilters(BaseModel):
    """Search filter parameters."""
    note_type: Optional[list[str]] = None
    people: Optional[list[str]] = None
    date_from: Optional[str] = None  # ISO date string
    date_to: Optional[str] = None


class SearchRequest(BaseModel):
    """Search request schema."""
    query: str = Field(..., min_length=1, description="Search query text")
    filters: Optional[SearchFilters] = None
    top_k: int = Field(default=20, ge=1, le=100)
    date_from: Optional[str] = Field(
        default=None,
        description="Inclusive lower bound (YYYY-MM-DD). When omitted, a relative-time "
                    "phrase in the query (e.g. 'last week') is auto-resolved.")
    date_to: Optional[str] = Field(
        default=None, description="Inclusive upper bound (YYYY-MM-DD).")

    @field_validator('query')
    @classmethod
    def query_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('Query cannot be empty')
        return v.strip()


class SearchResult(BaseModel):
    """Individual search result."""
    content: str
    file_path: str
    file_name: str
    note_type: Optional[str] = None
    modified_date: Optional[str] = None
    people: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    score: float
    semantic_score: Optional[float] = None
    recency_score: Optional[float] = None


class SearchResponse(BaseModel):
    """Search response schema."""
    results: list[SearchResult]
    query_time_ms: int


@router.post("/search", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    """
    **Search the Obsidian vault** using hybrid semantic + keyword search.

    This searches indexed notes, meeting notes, daily logs, and documents in the vault.
    Uses both vector similarity (semantic) and BM25 (keyword) matching for best results.

    Use this for:
    - "What did we discuss about project X?" → searches meeting notes
    - "Find notes about quarterly planning" → semantic search
    - "John's phone number" → keyword search for specific info

    Returns matching content chunks with file path, note type, and relevance score.
    Results are ranked by combined semantic + keyword + recency score.
    """
    start_time = time.time()

    # Build ChromaDB filter from request filters
    chroma_filter = None
    if request.filters:
        chroma_filter = {}

        # Note type filter (single value for ChromaDB)
        if request.filters.note_type and len(request.filters.note_type) == 1:
            chroma_filter["note_type"] = request.filters.note_type[0]

        # Date range filtering would require custom handling
        # ChromaDB doesn't support range queries directly on strings
        # For now, we'll filter in post-processing if needed

    # Resolve the effective date window: explicit params win; otherwise try to
    # infer one from a bounded relative-time phrase in the query ("last week")
    # against the current date. Keeps recency working even when the caller
    # didn't pass explicit bounds.
    #
    # NOTE: independent of the legacy ``request.filters.date_from/to`` post-filter
    # below. Two separate knobs — the top-level params push the window down into
    # hybrid_search (pre-ranking); ``filters`` filters already-ranked results
    # here. Both let undated docs pass through, so they compose without conflict.
    date_from, date_to = resolve_effective_dates(
        request.query, request.date_from, request.date_to
    )

    # Search using hybrid search (vector + BM25 keyword)
    try:
        hybrid_search = get_hybrid_search()
        raw_results = hybrid_search.search(
            query=request.query,
            top_k=request.top_k,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as e:
        logger.error(f"Hybrid search error: {e}")
        raise HTTPException(status_code=503, detail="Search service unavailable")

    # Post-process results
    results = []
    for r in raw_results:
        # Apply additional filters that ChromaDB can't handle natively
        if request.filters:
            # Filter by people (check if any requested person is in result)
            if request.filters.people:
                result_people = r.get("people", [])
                if isinstance(result_people, str):
                    result_people = [result_people]
                if not any(p in result_people for p in request.filters.people):
                    continue

            # Filter by date range
            if request.filters.date_from or request.filters.date_to:
                result_date = r.get("modified_date", "")
                if result_date:
                    try:
                        # Parse result date (ISO format)
                        result_dt = datetime.fromisoformat(result_date.replace("Z", "+00:00"))
                        result_date_str = result_dt.strftime("%Y-%m-%d")

                        if request.filters.date_from:
                            if result_date_str < request.filters.date_from:
                                continue
                        if request.filters.date_to:
                            if result_date_str > request.filters.date_to:
                                continue
                    except (ValueError, TypeError):
                        pass  # Skip date filtering for invalid dates

            # Filter by note_type if multiple types requested
            if request.filters.note_type and len(request.filters.note_type) > 1:
                if r.get("note_type") not in request.filters.note_type:
                    continue

        # Build result object
        people = r.get("people", [])
        if isinstance(people, str):
            try:
                import json
                people = json.loads(people)
            except (json.JSONDecodeError, TypeError):
                people = [people] if people else []

        tags = r.get("tags", [])
        if isinstance(tags, str):
            try:
                import json
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = [tags] if tags else []

        results.append(SearchResult(
            content=r.get("content", ""),
            file_path=r.get("file_path", ""),
            file_name=r.get("file_name", ""),
            note_type=r.get("note_type"),
            modified_date=r.get("modified_date"),
            people=people,
            tags=tags,
            score=r.get("hybrid_score", r.get("score", 0.0)),
            semantic_score=r.get("semantic_score"),
            recency_score=r.get("recency_score")
        ))

    elapsed_ms = int((time.time() - start_time) * 1000)

    return SearchResponse(
        results=results[:request.top_k],  # Ensure we don't exceed top_k after filtering
        query_time_ms=elapsed_ms
    )
