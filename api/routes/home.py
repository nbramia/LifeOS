"""
Home automation routes (#1081) — eero is the first provider.

Every route is gated 503 until an eero session token is available (state
file or LIFEOS_EERO_SESSION_TOKEN), checked before anything else — same
"gate before doing any work" convention as fitness.py's `_check_ingest_auth`.
Pause and resume return 200 on success (including a `mismatch`, never a
raised error, when the read-back state differs from what was requested) so
a scheduler-fired resume (`api/services/scheduler_store.py`'s
`_call_endpoint`) treats the call as successful and posts the response's
`scheduler_message` verbatim to Telegram.

Tailscale-only, like the rest of the API — no new public route.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.home import eero

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/home", tags=["home"])


class PauseRequest(BaseModel):
    minutes: Optional[int] = Field(
        default=None, ge=1, le=1440,
        description="Auto-resume after this many minutes (1-1440). Omit for an indefinite pause.",
    )


class ResumeRequest(BaseModel):
    scheduled: bool = Field(
        default=False,
        description="True when this call is the scheduler's own fire of a timed pause's "
                    "auto-resume — enables the retry-and-alert-on-failure path.",
    )


def _require_configured() -> None:
    if not eero.has_session_token():
        raise HTTPException(
            status_code=503,
            detail="Eero not configured: set LIFEOS_EERO_SESSION_TOKEN or run "
                   "scripts/eero_login.py to seed data/home/eero_session.json "
                   "(see docs/guides/home-eero.md).",
        )


@router.get("/eero/status")
async def eero_status():
    _require_configured()
    try:
        targets = await eero.list_status()
    except eero.EeroSessionDead:
        raise HTTPException(status_code=502, detail="Eero session is dead; re-run scripts/eero_login.py")
    except eero.EeroAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"targets": targets}


@router.post("/eero/{name}/pause")
async def eero_pause(name: str, body: PauseRequest = PauseRequest()):
    _require_configured()
    try:
        return await eero.pause(name, body.minutes)
    except eero.EeroUnknownTarget as e:
        raise HTTPException(status_code=404, detail=str(e))
    except eero.EeroSessionDead:
        raise HTTPException(status_code=502, detail="Eero session is dead; re-run scripts/eero_login.py")
    except eero.EeroAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/eero/{name}/resume")
async def eero_resume(name: str, body: ResumeRequest = ResumeRequest()):
    _require_configured()
    try:
        return await eero.resume(name, scheduled=body.scheduled)
    except eero.EeroUnknownTarget as e:
        raise HTTPException(status_code=404, detail=str(e))
    except eero.EeroResumeFailed as e:
        raise HTTPException(status_code=502, detail=str(e))
    except eero.EeroSessionDead:
        raise HTTPException(status_code=502, detail="Eero session is dead; re-run scripts/eero_login.py")
    except eero.EeroAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
