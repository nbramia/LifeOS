"""
Fitness API routes.

Currently just the authenticated Apple Health ingest endpoint (#333) — the
on-device HealthBridge app POSTs its payload here over Tailscale, and it lands
in fitness.db via the same ingest core the nightly file importer uses.
"""
import hmac
import logging

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
