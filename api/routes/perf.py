"""
Performance trace query endpoints.

Exposes perf trace data collected during request processing.
"""
from typing import Optional

from fastapi import APIRouter

from api.services.perf_trace import get_perf_trace_store

router = APIRouter(prefix="/api/perf", tags=["performance"])


@router.get("/traces")
async def list_traces(
    conversation_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
):
    """List recent performance traces with summary."""
    store = get_perf_trace_store()
    traces = store.get_traces(
        conversation_id=conversation_id,
        since=since,
        limit=limit,
    )
    return {"traces": traces, "count": len(traces)}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    """Get a single trace with all spans."""
    store = get_perf_trace_store()
    trace = store.get_trace(trace_id)
    if not trace:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace


@router.get("/stats")
async def get_stats(
    since: Optional[str] = None,
    limit: int = 100,
):
    """Aggregate stats: avg/p50/p95/max per stage across recent traces."""
    store = get_perf_trace_store()
    return store.get_stage_stats(since=since, limit=limit)
