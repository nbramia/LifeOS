"""
Scheduler API routes for LifeOS.

CRUD endpoints for schedules (trigger + action) plus ad-hoc Telegram messaging.
Replaces the legacy ``/api/reminders`` surface (kept as a deprecated alias in
``api/routes/reminders.py``). A schedule binds a trigger (``once``/``cron``) to
an ``action`` (notify / prompt / endpoint / agent).
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.scheduler_store import (
    get_scheduler_store,
    get_scheduler,
    ScheduleEntry,
    VALID_ACTIONS,
)
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

# Legacy message_type ↔ action, for callers still sending message_type.
_TYPE_TO_ACTION = {"static": "notify", "prompt": "prompt", "endpoint": "endpoint"}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateScheduleRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Human-readable name")
    schedule_type: str = Field(..., description="'once' or 'cron'")
    schedule_value: str = Field(..., description="ISO datetime (once) or cron expression (cron)")
    action: Optional[str] = Field(default=None, description="notify | prompt | endpoint | agent")
    message_type: Optional[str] = Field(default=None, description="Legacy: static | prompt | endpoint")
    message_content: str = Field(default="", description="Static text or natural-language prompt")
    endpoint_config: Optional[dict] = Field(default=None, description="For endpoint action: {endpoint, method, params}")
    executor: str = Field(default="", description="For agent action: local | cloud | cloud-haiku | cloud-sonnet")
    bot: str = Field(default="", description="Telegram bot to notify from — a name from the "
                                             "registry (config/telegram_bots.json) or 'primary'; empty = primary")
    enabled: bool = Field(default=True)
    timezone: str = Field(default_factory=lambda: settings.timezone, description="IANA timezone for the schedule")


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    schedule_type: Optional[str] = None
    schedule_value: Optional[str] = None
    action: Optional[str] = None
    message_type: Optional[str] = None
    message_content: Optional[str] = None
    endpoint_config: Optional[dict] = None
    executor: Optional[str] = None
    bot: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None


class ScheduleResponse(BaseModel):
    id: str
    name: str
    schedule_type: str
    schedule_value: str
    action: str
    message_type: str
    message_content: str
    endpoint_config: Optional[dict]
    executor: str
    bot: str
    enabled: bool
    created_at: str
    last_triggered_at: Optional[str]
    next_trigger_at: Optional[str]
    last_status: str
    timezone: str

    @classmethod
    def from_entry(cls, e: ScheduleEntry) -> "ScheduleResponse":
        return cls(
            id=e.id,
            name=e.name,
            schedule_type=e.schedule_type,
            schedule_value=e.schedule_value,
            action=e.action,
            message_type=e.message_type,
            message_content=e.message_content,
            endpoint_config=e.endpoint_config,
            executor=e.executor,
            bot=e.bot,
            enabled=e.enabled,
            created_at=e.created_at or "",
            last_triggered_at=e.last_triggered_at,
            next_trigger_at=e.next_trigger_at,
            last_status=e.last_status,
            timezone=e.timezone or settings.timezone,
        )


class ScheduleListResponse(BaseModel):
    schedules: list[ScheduleResponse]
    total: int


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Message text to send via Telegram")
    bot: Optional[str] = Field(
        default=None,
        description="Optional bot name to send from — a name from the registry "
                    "(config/telegram_bots.json). Falls back to the primary bot "
                    "if unset or unrecognised.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_action(action: Optional[str], message_type: Optional[str]) -> str:
    """Resolve the action from an explicit value or the legacy message_type."""
    if action:
        return action
    return _TYPE_TO_ACTION.get(message_type or "", "notify")


def _require_known_bot(bot: Optional[str]) -> None:
    """Reject a bot name the registry doesn't know (#575).

    An orphaned name — usually the residue of a bot rename — otherwise stores
    fine and then silently delivers to the primary chat at every fire. The
    registry is read here, at request time, because it reflects the current
    environment; empty or unset stays valid and means the primary bot.
    """
    from api.services.telegram import validate_bot_name

    try:
        validate_bot_name(bot)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Routes (static paths MUST come before {schedule_id} to avoid capture)
# ---------------------------------------------------------------------------

@router.post("", response_model=ScheduleResponse)
async def create_schedule(request: CreateScheduleRequest):
    """Create a new schedule."""
    if request.schedule_type not in ("once", "cron"):
        raise HTTPException(status_code=400, detail="schedule_type must be 'once' or 'cron'")
    action = _resolve_action(request.action, request.message_type)
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {VALID_ACTIONS}")
    _require_known_bot(request.bot)

    store = get_scheduler_store()
    entry = store.create(
        name=request.name,
        schedule_type=request.schedule_type,
        schedule_value=request.schedule_value,
        action=action,
        message_type=request.message_type or ("static" if action == "notify" else action),
        message_content=request.message_content,
        endpoint_config=request.endpoint_config,
        executor=request.executor,
        bot=request.bot,
        enabled=request.enabled,
        timezone=request.timezone,
    )
    return ScheduleResponse.from_entry(entry)


@router.get("", response_model=ScheduleListResponse)
async def list_schedules():
    """List all schedules."""
    store = get_scheduler_store()
    entries = store.list_all()
    return ScheduleListResponse(
        schedules=[ScheduleResponse.from_entry(e) for e in entries],
        total=len(entries),
    )


@router.post("/send")
async def send_adhoc_message(request: SendMessageRequest):
    """Send an ad-hoc message via Telegram."""
    from api.services.telegram import send_message_async

    if not settings.telegram_enabled:
        raise HTTPException(status_code=400, detail="Telegram not configured")

    success = await send_message_async(request.text, bot=request.bot)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send Telegram message")
    return {"status": "sent"}


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(schedule_id: str):
    """Get a specific schedule by ID."""
    store = get_scheduler_store()
    entry = store.get(schedule_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleResponse.from_entry(entry)


@router.put("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(schedule_id: str, request: UpdateScheduleRequest):
    """Update an existing schedule."""
    _require_known_bot(request.bot)
    store = get_scheduler_store()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    entry = store.update(schedule_id, **updates)
    if not entry:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleResponse.from_entry(entry)


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str):
    """Delete a schedule."""
    store = get_scheduler_store()
    if not store.delete(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"status": "deleted", "id": schedule_id}


@router.post("/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: str):
    """Manually fire a schedule (for testing)."""
    store = get_scheduler_store()
    entry = store.get(schedule_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Schedule not found")

    await get_scheduler()._fire_entry(entry)
    return {"status": "triggered", "id": schedule_id}
