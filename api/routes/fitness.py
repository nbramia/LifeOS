"""
Fitness API routes.

The authenticated Apple Health ingest endpoint (#333) — the on-device
HealthBridge app POSTs its payload here over Tailscale, and it lands in
fitness.db via the same ingest core the nightly file importer uses — plus
`/workouts` (#603), the REST surface behind the `lifeos_workout_manage` MCP
tool so external clients can log/query the fitness bot's own store.
"""
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/fitness", tags=["fitness"])

# Upper bound on items per ingest. Generous enough for a multi-year one-shot
# backfill of high-frequency samples (heart rate, steps); rejects absurd
# payloads (the endpoint takes on-demand POSTs).
_MAX_INGEST_ITEMS = 1_000_000


class HealthIngestRequest(BaseModel):
    """Apple Health payload — matches the health.json schema (see #333 / guide)."""
    workouts: list[dict] = Field(default_factory=list)
    metrics: list[dict] = Field(default_factory=list)


def _check_ingest_auth(request: Request) -> None:
    """Bearer-token gate. Disabled (503) until LIFEOS_HEALTH_INGEST_TOKEN is set —
    a dedicated token, separate from the MCP transport's bearer."""
    expected = settings.health_ingest_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Health ingest disabled: set LIFEOS_HEALTH_INGEST_TOKEN to enable.",
        )
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


@router.post("/health/ingest")
async def health_ingest(request: Request):
    """Ingest an Apple Health payload into the fitness store.

    Idempotent — workouts dedupe on the HKWorkout uuid, metrics on (type, start).
    Returns created/skipped counts. Authenticates BEFORE parsing the body so an
    unauthenticated caller can't probe validation behavior.
    """
    _check_ingest_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        payload = HealthIngestRequest.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    total = len(payload.workouts) + len(payload.metrics)
    if total > _MAX_INGEST_ITEMS:
        raise HTTPException(
            status_code=413,
            detail=f"payload too large: {total} items (max {_MAX_INGEST_ITEMS})",
        )

    from api.services.health_import import ingest_health
    result = ingest_health(payload.model_dump())
    logger.info(f"Health ingest: {result}")
    return result


# ---------------------------------------------------------------------------
# manage_workouts over REST (#603) — the MCP surface has no in-process agent
# loop to call, so this exposes the same action-dispatch tool over HTTP. It
# calls the exact `_tool_manage_workouts` dispatcher the native orchestrator
# uses, so both paths write through the same FitnessStore with no separate
# implementation to drift out of sync.
# ---------------------------------------------------------------------------

class WorkoutSetInput(BaseModel):
    """One exercise entry for the 'log'/'update' actions."""
    exercise: Optional[str] = Field(default=None, description="Exercise name (normalized server-side).")
    reps: Optional[int] = Field(default=None, description="Reps per set — also the count for timed work.")
    weight: Optional[float] = Field(default=None, description="Load.")
    unit: Optional[str] = Field(default=None, description="'lb'/'kg' for weighted sets, or a counted-work unit ('steps', 'm').")
    count: Optional[int] = Field(default=None, description="Number of identical sets (default 1).")
    rpe: Optional[float] = Field(default=None, description="Rate of perceived exertion.")
    duration_seconds: Optional[int] = Field(default=None, description="Elapsed time in seconds for timed work.")
    notes: Optional[str] = Field(default=None, description="Per-exercise note, e.g. cardio distance ('4 mi').")


class ManageWorkoutsRequest(BaseModel):
    """Mirrors the `manage_workouts` orchestrator tool's input (agent_tools.py)."""
    action: str = Field(
        description="log | update | list | history | summary | log_metric | "
                    "metrics | get_profile | set_profile | readiness"
    )
    sets: Optional[list[WorkoutSetInput]] = Field(
        default=None,
        description="Exercises for log/update — a JSON array, one entry per distinct "
                    "exercise/load, each {exercise, reps, weight, unit, count, rpe, "
                    "duration_seconds, notes}.",
    )
    date: Optional[str] = Field(default=None, description="Session date YYYY-MM-DD (log/update). Omit on log to use today.")
    kind: Optional[str] = Field(default=None, description="strength | cardio | mobility | sport | other.")
    title: Optional[str] = Field(default=None, description="Session title, e.g. 'Push day'.")
    notes: Optional[str] = Field(default=None, description="Session notes.")
    session_id: Optional[str] = Field(default=None, description="Target session for 'update' (defaults to most recent).")
    exercise: Optional[str] = Field(default=None, description="Exercise name for 'history' / 'summary'.")
    date_start: Optional[str] = Field(default=None, description="Window start YYYY-MM-DD (list/summary/metrics).")
    date_end: Optional[str] = Field(default=None, description="Window end YYYY-MM-DD (list/summary/metrics).")
    metric_type: Optional[str] = Field(default=None, description="Metric name for log_metric/metrics, e.g. 'body_weight'.")
    value: Optional[str] = Field(default=None, description="Numeric for 'log_metric', free text for 'set_profile'.")
    unit: Optional[str] = Field(default=None, description="Metric unit for 'log_metric', e.g. 'lb'.")
    key: Optional[str] = Field(default=None, description="Training-profile key for 'set_profile'.")
    limit: Optional[int] = Field(default=None, description="Max rows for list/history/metrics — an integer (default varies by action).")


@router.post("/workouts")
async def manage_workouts_endpoint(body: ManageWorkoutsRequest):
    """Log or query the fitness bot's workout log and metrics.

    Thin transport wrapper around `_tool_manage_workouts` — same store, same
    formatting, same error strings ("Error: ...") as the native agent loop.

    A dispatcher failure is raised as a 4xx rather than returned as a 200
    body: the native orchestrator reads the "Error: ..." prose directly, but
    an MCP/HTTP caller checks structured success, not prose inside a
    structurally-successful response. Wrapping a failed write in a 200 is
    exactly the false-confirmation shape this endpoint exists to eliminate —
    both mcp_server.py's `tools/call` and the agent worker's `ToolRegistry`
    already key off "error"/non-2xx to decide isError, so raising here is
    what makes a failed log actually surface as a failure at both layers.
    """
    from api.services.agent_tools import _tool_manage_workouts
    inp = body.model_dump(exclude_none=True)
    result = _tool_manage_workouts(inp)
    if result.startswith("Error"):
        raise HTTPException(status_code=400, detail=result)
    return {"result": result}
