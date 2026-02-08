"""
Reminder Store and Scheduler for LifeOS.

Stores scheduled reminders that can:
- Send static messages via Telegram
- Run prompts through the full LifeOS chat pipeline and send results
- Call LifeOS API endpoints and send formatted results

Storage: JSON file at ~/.lifeos/reminders.json
Follows the same pattern as memory_store.py.
"""
import asyncio
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from croniter import croniter

from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_REMINDERS_PATH = Path.home() / ".lifeos" / "reminders.json"


@dataclass
class Reminder:
    """A scheduled reminder."""
    id: str
    name: str
    schedule_type: str  # "once" or "cron"
    schedule_value: str  # ISO datetime or cron expression
    message_type: str  # "static", "prompt", or "endpoint"
    message_content: str  # Static text or natural language prompt
    endpoint_config: Optional[dict] = None  # For endpoint type: {endpoint, method, params}
    enabled: bool = True
    created_at: str = ""
    last_triggered_at: Optional[str] = None
    next_trigger_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value,
            "message_type": self.message_type,
            "message_content": self.message_content,
            "endpoint_config": self.endpoint_config,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_triggered_at": self.last_triggered_at,
            "next_trigger_at": self.next_trigger_at,
        }


def compute_next_trigger(reminder: Reminder) -> Optional[str]:
    """Compute the next trigger time for a reminder."""
    now = datetime.now(timezone.utc)

    if reminder.schedule_type == "once":
        try:
            trigger_time = datetime.fromisoformat(reminder.schedule_value)
            if trigger_time.tzinfo is None:
                trigger_time = trigger_time.replace(tzinfo=timezone.utc)
            if trigger_time > now:
                return trigger_time.isoformat()
            return None  # Past one-time reminder
        except (ValueError, TypeError):
            return None

    elif reminder.schedule_type == "cron":
        try:
            cron = croniter(reminder.schedule_value, now)
            next_time = cron.get_next(datetime)
            return next_time.isoformat()
        except (ValueError, KeyError):
            logger.error(f"Invalid cron expression for reminder {reminder.id}: {reminder.schedule_value}")
            return None

    return None


def _format_cron_human(cron_expr: str) -> str:
    """Convert a cron expression to a human-readable string."""
    parts = cron_expr.split()
    if len(parts) < 5:
        return cron_expr
    minute, hour, _, _, dow = parts
    try:
        h = int(hour)
        m = int(minute)
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        time_str = f"{h12}:{m:02d} {ampm}"
    except ValueError:
        return cron_expr

    dow_map = {"*": "daily", "1-5": "weekdays", "0,6": "weekends",
               "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
               "4": "Thu", "5": "Fri", "6": "Sat"}
    day_str = dow_map.get(dow, dow)
    return f"{day_str} at {time_str}"


def _format_dt_short(iso_str: str) -> str:
    """Format an ISO datetime string to a short display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %Y %I:%M %p")
    except (ValueError, TypeError):
        return iso_str or "—"


class ReminderStore:
    """
    CRUD store for reminders.

    Persists to ~/.lifeos/reminders.json. Thread-safe.
    """

    def __init__(self, file_path: Optional[str] = None):
        self.file_path = Path(file_path) if file_path else DEFAULT_REMINDERS_PATH
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._reminders: dict[str, Reminder] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self.file_path.exists():
            try:
                with open(self.file_path, "r") as f:
                    data = json.load(f)
                for item in data.get("reminders", []):
                    reminder = Reminder(**{
                        k: item.get(k) for k in Reminder.__dataclass_fields__
                        if k in item
                    })
                    self._reminders[reminder.id] = reminder
                logger.info(f"Loaded {len(self._reminders)} reminders from {self.file_path}")
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Error loading reminders: {e}. Starting fresh.")
                self._reminders = {}

    def _save(self):
        data = {
            "description": "LifeOS Scheduled Reminders",
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "reminders": [r.to_dict() for r in self._reminders.values()],
        }
        with open(self.file_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        self._write_dashboard()

    def _write_dashboard(self):
        """Generate LifeOS/Reminders/Dashboard.md in the vault."""
        try:
            vault_path = settings.vault_path
            reminders_dir = vault_path / "LifeOS" / "Reminders"
            reminders_dir.mkdir(parents=True, exist_ok=True)
            dashboard = reminders_dir / "Dashboard.md"

            now = datetime.now(timezone.utc)
            all_reminders = list(self._reminders.values())

            # Partition reminders
            active_recurring = [r for r in all_reminders if r.enabled and r.schedule_type == "cron"]
            upcoming_once = [r for r in all_reminders if r.enabled and r.schedule_type == "once" and r.next_trigger_at]
            past = [r for r in all_reminders if not r.enabled or (r.schedule_type == "once" and not r.next_trigger_at)]

            # Sort
            upcoming_once.sort(key=lambda r: r.next_trigger_at or "")
            past.sort(key=lambda r: r.last_triggered_at or r.created_at or "", reverse=True)

            lines = [
                "---",
                "type: dashboard",
                "---",
                "# Reminder Dashboard",
                "",
                f"> Auto-generated by LifeOS — {now.strftime('%Y-%m-%d %H:%M UTC')}",
                "",
            ]

            # Active recurring
            lines.append("## Recurring")
            if active_recurring:
                lines.append("")
                lines.append("| Name | Schedule | Last Triggered |")
                lines.append("|------|----------|----------------|")
                for r in active_recurring:
                    sched = _format_cron_human(r.schedule_value)
                    last = _format_dt_short(r.last_triggered_at) if r.last_triggered_at else "—"
                    lines.append(f"| {r.name} | {sched} | {last} |")
            else:
                lines.append("\n_No recurring reminders._")
            lines.append("")

            # Upcoming one-time
            lines.append("## Upcoming")
            if upcoming_once:
                lines.append("")
                lines.append("| Name | Scheduled For | Created |")
                lines.append("|------|---------------|---------|")
                for r in upcoming_once:
                    trigger = _format_dt_short(r.next_trigger_at) if r.next_trigger_at else "—"
                    created = _format_dt_short(r.created_at) if r.created_at else "—"
                    lines.append(f"| {r.name} | {trigger} | {created} |")
            else:
                lines.append("\n_No upcoming reminders._")
            lines.append("")

            # Past/completed (last 20)
            lines.append("## Past")
            if past:
                lines.append("")
                lines.append("| Name | Triggered | Type |")
                lines.append("|------|-----------|------|")
                for r in past[:20]:
                    triggered = _format_dt_short(r.last_triggered_at) if r.last_triggered_at else "never"
                    rtype = "recurring" if r.schedule_type == "cron" else "one-time"
                    lines.append(f"| {r.name} | {triggered} | {rtype} |")
                if len(past) > 20:
                    lines.append(f"| _... and {len(past) - 20} more_ | | |")
            else:
                lines.append("\n_No past reminders._")
            lines.append("")

            dashboard.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Failed to write reminder dashboard: {e}")

    def create(self, **kwargs) -> Reminder:
        with self._lock:
            reminder = Reminder(
                id=str(uuid.uuid4()),
                created_at=datetime.now(timezone.utc).isoformat(),
                **kwargs,
            )
            reminder.next_trigger_at = compute_next_trigger(reminder)
            self._reminders[reminder.id] = reminder
            self._save()
            logger.info(f"Created reminder: {reminder.id} - {reminder.name}")
            return reminder

    def get(self, reminder_id: str) -> Optional[Reminder]:
        return self._reminders.get(reminder_id)

    def list_all(self) -> list[Reminder]:
        return sorted(
            self._reminders.values(),
            key=lambda r: r.created_at or "",
            reverse=True,
        )

    def update(self, reminder_id: str, **kwargs) -> Optional[Reminder]:
        with self._lock:
            reminder = self._reminders.get(reminder_id)
            if not reminder:
                return None
            for key, value in kwargs.items():
                if hasattr(reminder, key) and value is not None:
                    setattr(reminder, key, value)
            # Recompute next trigger if schedule changed
            if "schedule_type" in kwargs or "schedule_value" in kwargs or "enabled" in kwargs:
                if reminder.enabled:
                    reminder.next_trigger_at = compute_next_trigger(reminder)
                else:
                    reminder.next_trigger_at = None
            self._save()
            return reminder

    def delete(self, reminder_id: str) -> bool:
        with self._lock:
            if reminder_id in self._reminders:
                del self._reminders[reminder_id]
                self._save()
                return True
            return False

    def mark_triggered(self, reminder_id: str):
        """Mark a reminder as triggered and update next trigger time."""
        with self._lock:
            reminder = self._reminders.get(reminder_id)
            if not reminder:
                return
            reminder.last_triggered_at = datetime.now(timezone.utc).isoformat()
            if reminder.schedule_type == "once":
                reminder.enabled = False
                reminder.next_trigger_at = None
            else:
                reminder.next_trigger_at = compute_next_trigger(reminder)
            self._save()

    def get_due_reminders(self) -> list[Reminder]:
        """Get all enabled reminders that are due to fire."""
        now = datetime.now(timezone.utc)
        due = []
        for reminder in self._reminders.values():
            if not reminder.enabled or not reminder.next_trigger_at:
                continue
            try:
                next_time = datetime.fromisoformat(reminder.next_trigger_at)
                if next_time.tzinfo is None:
                    next_time = next_time.replace(tzinfo=timezone.utc)
                if next_time <= now:
                    due.append(reminder)
            except (ValueError, TypeError):
                continue
        return due


class ReminderScheduler:
    """
    Background thread that checks for due reminders every 60 seconds.

    For each due reminder:
    - static: send message_content via Telegram
    - prompt: run message_content through chat_via_api, send result via Telegram
    - endpoint: call LifeOS API endpoint, format result, send via Telegram
    """

    def __init__(self, store: ReminderStore):
        self.store = store
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not settings.telegram_enabled:
            logger.info("Telegram not configured, reminder scheduler not started")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ReminderScheduler",
        )
        self._thread.start()
        logger.info("Reminder scheduler started")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Reminder scheduler stopped")

    def _run(self):
        """Main scheduler loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._schedule_loop())
        except Exception as e:
            logger.error(f"Reminder scheduler crashed: {e}")
        finally:
            loop.close()

    async def _schedule_loop(self):
        """Check for due reminders every 60 seconds."""
        while not self._stop_event.is_set():
            try:
                due = self.store.get_due_reminders()
                for reminder in due:
                    await self._fire_reminder(reminder)
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Wait 60 seconds, checking stop event every second
            for _ in range(60):
                if self._stop_event.is_set():
                    return
                await asyncio.sleep(1)

    async def _fire_reminder(self, reminder: Reminder):
        """Execute a single reminder."""
        logger.info(f"Firing reminder: {reminder.name} ({reminder.id})")

        try:
            message = await self._generate_message(reminder)
            if message:
                # Prefix with reminder name
                from api.services.telegram import send_message_async
                full_message = f"*{reminder.name}*\n\n{message}"
                await send_message_async(full_message)
            self.store.mark_triggered(reminder.id)
        except Exception as e:
            logger.error(f"Failed to fire reminder {reminder.id}: {e}")

    async def _generate_message(self, reminder: Reminder) -> Optional[str]:
        """Generate the message content for a reminder."""
        if reminder.message_type == "static":
            return reminder.message_content

        elif reminder.message_type == "prompt":
            from api.services.telegram import chat_via_api
            result = await chat_via_api(reminder.message_content)
            return result.get("answer", "No response generated.")

        elif reminder.message_type == "endpoint":
            return await self._call_endpoint(reminder.endpoint_config)

        else:
            logger.warning(f"Unknown message type: {reminder.message_type}")
            return None

    async def _call_endpoint(self, config: Optional[dict]) -> Optional[str]:
        """Call a LifeOS API endpoint and format the result."""
        if not config:
            return "No endpoint configuration provided."

        endpoint = config.get("endpoint", "")
        method = config.get("method", "GET").upper()
        params = config.get("params", {})
        port = settings.port

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                url = f"http://localhost:{port}{endpoint}"
                if method == "GET":
                    resp = await client.get(url, params=params)
                else:
                    resp = await client.post(url, json=params)

                if resp.status_code != 200:
                    return f"API call failed: {resp.status_code}"

                data = resp.json()
                return json.dumps(data, indent=2, default=str)[:3500]
        except Exception as e:
            return f"Error calling endpoint: {e}"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_reminder_store: Optional[ReminderStore] = None
_reminder_scheduler: Optional[ReminderScheduler] = None


def get_reminder_store() -> ReminderStore:
    global _reminder_store
    if _reminder_store is None:
        _reminder_store = ReminderStore()
    return _reminder_store


def get_reminder_scheduler() -> ReminderScheduler:
    global _reminder_scheduler
    if _reminder_scheduler is None:
        _reminder_scheduler = ReminderScheduler(get_reminder_store())
    return _reminder_scheduler
