"""
Reminders API routes for LifeOS.

CRUD endpoints for scheduled reminders + ad-hoc Telegram messaging.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.reminder_store import get_reminder_store, get_reminder_scheduler, Reminder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateReminderRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Human-readable name")
    schedule_type: str = Field(..., description="'once' or 'cron'")
    schedule_value: str = Field(..., description="ISO datetime (once) or cron expression (cron)")
    message_type: str = Field(..., description="'static', 'prompt', or 'endpoint'")
    message_content: str = Field(default="", description="Static text or natural language prompt")
    endpoint_config: Optional[dict] = Field(default=None, description="For endpoint type: {endpoint, method, params}")
    enabled: bool = Field(default=True)


class UpdateReminderRequest(BaseModel):
    name: Optional[str] = None
    schedule_type: Optional[str] = None
    schedule_value: Optional[str] = None
    message_type: Optional[str] = None
    message_content: Optional[str] = None
    endpoint_config: Optional[dict] = None
    enabled: Optional[bool] = None


class ReminderResponse(BaseModel):
    id: str
    name: str
    schedule_type: str
    schedule_value: str
    message_type: str
    message_content: str
    endpoint_config: Optional[dict]
    enabled: bool
    created_at: str
    last_triggered_at: Optional[str]
    next_trigger_at: Optional[str]

    @classmethod
    def from_reminder(cls, r: Reminder) -> "ReminderResponse":
        return cls(
            id=r.id,
            name=r.name,
            schedule_type=r.schedule_type,
            schedule_value=r.schedule_value,
            message_type=r.message_type,
            message_content=r.message_content,
            endpoint_config=r.endpoint_config,
            enabled=r.enabled,
            created_at=r.created_at or "",
            last_triggered_at=r.last_triggered_at,
            next_trigger_at=r.next_trigger_at,
        )


class ReminderListResponse(BaseModel):
    reminders: list[ReminderResponse]
    total: int


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Message text to send via Telegram")


# ---------------------------------------------------------------------------
# Routes (static paths MUST come before {reminder_id} to avoid capture)
# ---------------------------------------------------------------------------

@router.post("", response_model=ReminderResponse)
async def create_reminder(request: CreateReminderRequest):
    """Create a new scheduled reminder."""
    if request.schedule_type not in ("once", "cron"):
        raise HTTPException(status_code=400, detail="schedule_type must be 'once' or 'cron'")
    if request.message_type not in ("static", "prompt", "endpoint"):
        raise HTTPException(status_code=400, detail="message_type must be 'static', 'prompt', or 'endpoint'")

    store = get_reminder_store()
    reminder = store.create(
        name=request.name,
        schedule_type=request.schedule_type,
        schedule_value=request.schedule_value,
        message_type=request.message_type,
        message_content=request.message_content,
        endpoint_config=request.endpoint_config,
        enabled=request.enabled,
    )
    return ReminderResponse.from_reminder(reminder)


@router.get("", response_model=ReminderListResponse)
async def list_reminders():
    """List all reminders."""
    store = get_reminder_store()
    reminders = store.list_all()
    return ReminderListResponse(
        reminders=[ReminderResponse.from_reminder(r) for r in reminders],
        total=len(reminders),
    )


@router.post("/send")
async def send_adhoc_message(request: SendMessageRequest):
    """Send an ad-hoc message via Telegram."""
    from api.services.telegram import send_message_async
    from config.settings import settings

    if not settings.telegram_enabled:
        raise HTTPException(status_code=400, detail="Telegram not configured")

    success = await send_message_async(request.text)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send Telegram message")
    return {"status": "sent"}


@router.get("/{reminder_id}", response_model=ReminderResponse)
async def get_reminder(reminder_id: str):
    """Get a specific reminder by ID."""
    store = get_reminder_store()
    reminder = store.get(reminder_id)
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return ReminderResponse.from_reminder(reminder)


@router.put("/{reminder_id}", response_model=ReminderResponse)
async def update_reminder(reminder_id: str, request: UpdateReminderRequest):
    """Update an existing reminder."""
    store = get_reminder_store()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    reminder = store.update(reminder_id, **updates)
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return ReminderResponse.from_reminder(reminder)


@router.delete("/{reminder_id}")
async def delete_reminder(reminder_id: str):
    """Delete a reminder."""
    store = get_reminder_store()
    deleted = store.delete(reminder_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"status": "deleted", "id": reminder_id}


@router.post("/{reminder_id}/trigger")
async def trigger_reminder(reminder_id: str):
    """Manually trigger a reminder (for testing)."""
    store = get_reminder_store()
    reminder = store.get(reminder_id)
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")

    scheduler = get_reminder_scheduler()
    await scheduler._fire_reminder(reminder)
    return {"status": "triggered", "id": reminder_id}
