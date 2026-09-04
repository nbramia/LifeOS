"""
Performance trace query endpoints.

Exposes perf trace data collected during request processing.
"""
from typing import Optional

from fastapi import APIRouter

from api.services.perf_trace import get_perf_trace_store
from api.services.route_timing import get_route_timing_store

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


@router.get("/routes")
def get_route_stats():
    """Rolling per-route request timing summary (#877).

    Backed by `RouteTimingMiddleware` (api/services/route_timing.py), which
    records every HTTP request's duration/status/size in-process. A plain
    `def` handler -- the store's own lock, not the event loop, is what
    makes this safe under concurrent requests.

    `routes` never includes streaming (`text/event-stream`) responses --
    those show up in `streams` instead, with count and total bytes only.
    An SSE connection's duration is however long the client kept it open,
    not a latency signal, so timing it like a normal request would let a
    page-open artifact dominate this table and fire a false slow-request
    warning on every disconnect (#877 review).
    """
    store = get_route_timing_store()
    routes = store.summary()
    streams = store.stream_summary()
    return {
        "routes": routes,
        "count": len(routes),
        "streams": streams,
        "stream_count": len(streams),
    }
