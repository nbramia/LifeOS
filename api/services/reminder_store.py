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
import time as _time_mod
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yaml

from croniter import croniter
from zoneinfo import ZoneInfo

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
    timezone: str = ""  # Resolved from settings.timezone when empty

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
            "timezone": self.timezone,
        }


def compute_next_trigger(reminder: Reminder) -> Optional[str]:
    """
    Compute the next trigger time for a reminder.

    For cron expressions, times are interpreted in the reminder's timezone
    (defaults to America/New_York) and then converted to UTC for storage.
    This ensures "daily at 6pm" means 6pm Eastern, not 6pm UTC.
    """
    now_utc = datetime.now(timezone.utc)

    if reminder.schedule_type == "once":
        try:
            trigger_time = datetime.fromisoformat(reminder.schedule_value)
            if trigger_time.tzinfo is None:
                # Assume the reminder's timezone if not specified
                tz = ZoneInfo(reminder.timezone or settings.timezone)
                trigger_time = trigger_time.replace(tzinfo=tz)
            # Convert to UTC for comparison and storage
            trigger_time_utc = trigger_time.astimezone(timezone.utc)
            if trigger_time_utc > now_utc:
                return trigger_time_utc.isoformat()
            return None  # Past one-time reminder
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid datetime for reminder {reminder.id}: {e}")
            return None

    elif reminder.schedule_type == "cron":
        try:
            # Interpret cron expression in the reminder's timezone
            tz = ZoneInfo(reminder.timezone or settings.timezone)
            now_local = datetime.now(tz)

            cron = croniter(reminder.schedule_value, now_local)
            next_time_local = cron.get_next(datetime)

            # Ensure the result has timezone info
            if next_time_local.tzinfo is None:
                next_time_local = next_time_local.replace(tzinfo=tz)

            # Convert to UTC for storage
            next_time_utc = next_time_local.astimezone(timezone.utc)
            return next_time_utc.isoformat()
        except (ValueError, KeyError) as e:
            logger.error(f"Invalid cron expression for reminder {reminder.id}: {reminder.schedule_value} - {e}")
            return None

    return None


def _format_cron_human(cron_expr: str, tz_name: str = "") -> str:
    """Convert a cron expression to a human-readable string with timezone."""
    tz_name = tz_name or settings.timezone
    parts = cron_expr.split()
    if len(parts) < 5:
        return cron_expr
    minute, hour, _, _, dow = parts

    # Timezone abbreviation
    tz_abbrev = "ET" if "New_York" in tz_name else tz_name.split("/")[-1]

    dow_map = {"*": "daily", "1-5": "weekdays", "0,6": "weekends",
               "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
               "4": "Thu", "5": "Fri", "6": "Sat"}
    day_str = dow_map.get(dow, dow)

    # Handle interval patterns like */15 with hour ranges like 8-18
    if minute.startswith("*/"):
        interval = minute[2:]
        if "-" in hour:
            # Hour range: e.g. 8-18
            h_start, h_end = hour.split("-", 1)
            try:
                hs = int(h_start)
                he = int(h_end)
                s_ampm = "AM" if hs < 12 else "PM"
                e_ampm = "AM" if he < 12 else "PM"
                hs12 = hs % 12 or 12
                he12 = he % 12 or 12
                return f"{day_str} every {interval}m, {hs12}{s_ampm}\u2013{he12}{e_ampm} {tz_abbrev}"
            except ValueError:
                pass
        return f"{day_str} every {interval}m {tz_abbrev}"

    try:
        h = int(hour)
        m = int(minute)
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        time_str = f"{h12}:{m:02d} {ampm}"
    except ValueError:
        return cron_expr

    return f"{day_str} at {time_str} {tz_abbrev}"


def _format_dt_short(iso_str: str, tz_name: str = "") -> str:
    """Format an ISO datetime string to a short display in local timezone."""
    tz_name = tz_name or settings.timezone
    try:
        dt = datetime.fromisoformat(iso_str)
        tz = ZoneInfo(tz_name)
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz)
        else:
            dt = dt.replace(tzinfo=tz)
        return dt.strftime("%b %d, %I:%M %p")
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
                lines.append("| Name | Schedule | Next Fire | Last Triggered | Type |")
                lines.append("|------|----------|-----------|----------------|------|")
                for r in active_recurring:
                    tz = r.timezone or settings.timezone
                    sched = _format_cron_human(r.schedule_value, tz)
                    nxt = _format_dt_short(r.next_trigger_at, tz) if r.next_trigger_at else "—"
                    last = _format_dt_short(r.last_triggered_at, tz) if r.last_triggered_at else "—"
                    lines.append(f"| {r.name} | {sched} | {nxt} | {last} | {r.message_type} |")
            else:
                lines.append("\n_No recurring reminders._")
            lines.append("")

            # Upcoming one-time
            lines.append("## Upcoming")
            if upcoming_once:
                lines.append("")
                lines.append("| Name | Scheduled For | Created | Type |")
                lines.append("|------|---------------|---------|------|")
                for r in upcoming_once:
                    tz = r.timezone or settings.timezone
                    trigger = _format_dt_short(r.next_trigger_at, tz) if r.next_trigger_at else "—"
                    created = _format_dt_short(r.created_at, tz) if r.created_at else "—"
                    lines.append(f"| {r.name} | {trigger} | {created} | {r.message_type} |")
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
        """Get all enabled reminders that are due to fire.

        Includes a 90-second cooldown: reminders triggered within the last
        90 seconds are skipped. This prevents duplicate fires on server
        restart or scheduler race conditions.
        """
        now = datetime.now(timezone.utc)
        due = []
        for reminder in self._reminders.values():
            if not reminder.enabled or not reminder.next_trigger_at:
                continue
            try:
                next_time = datetime.fromisoformat(reminder.next_trigger_at)
                if next_time.tzinfo is None:
                    next_time = next_time.replace(tzinfo=timezone.utc)
                if next_time > now:
                    continue
                # Cooldown: skip if triggered less than 90 seconds ago
                if reminder.last_triggered_at:
                    last = datetime.fromisoformat(reminder.last_triggered_at)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (now - last).total_seconds() < 90:
                        continue
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

    Crash recovery:
    - Auto-restarts with exponential backoff (5s → 60s cap)
    - Sends Telegram alert on each crash
    - After 5 consecutive crashes, stops retrying and sends final alert
    - Crash counter resets after 10 minutes of healthy operation

    Vault toggle:
    - Reads {vault}/LifeOS/Reminders/Scheduler.md frontmatter each iteration
    - If `enabled: false`, skips firing but keeps thread alive
    """

    MAX_CONSECUTIVE_CRASHES = 5
    BACKOFF_BASE = 5  # seconds
    BACKOFF_CAP = 60  # seconds
    HEALTHY_RESET_SECONDS = 600  # 10 minutes

    def __init__(self, store: ReminderStore):
        self.store = store
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._crash_count = 0
        self._last_crash_time: Optional[float] = None

    def start(self):
        if not settings.telegram_enabled:
            logger.info("Telegram not configured, reminder scheduler not started")
            return

        self._stop_event.clear()
        self._crash_count = 0
        self._last_crash_time = None
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

    def is_alive(self) -> bool:
        """Check if the scheduler thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        """Main scheduler loop with crash recovery."""
        while not self._stop_event.is_set():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._schedule_loop())
                break  # Clean exit (stop_event was set)
            except Exception as e:
                loop.close()
                if self._stop_event.is_set():
                    break

                # Reset crash counter if healthy for HEALTHY_RESET_SECONDS
                now = _time_mod.monotonic()
                if (self._last_crash_time is not None
                        and now - self._last_crash_time > self.HEALTHY_RESET_SECONDS):
                    self._crash_count = 0

                self._crash_count += 1
                self._last_crash_time = now
                logger.error(f"Reminder scheduler crashed (attempt {self._crash_count}): {e}")

                # Send Telegram alert
                try:
                    from api.services.telegram import send_message
                    if self._crash_count >= self.MAX_CONSECUTIVE_CRASHES:
                        send_message(
                            f"*Reminder Scheduler DOWN*\n\n"
                            f"Crashed {self._crash_count} times consecutively. "
                            f"Giving up. Last error: {str(e)[:200]}\n\n"
                            f"Restart the server to recover."
                        )
                    else:
                        send_message(
                            f"*Reminder Scheduler Crashed*\n\n"
                            f"Error: {str(e)[:200]}\n"
                            f"Restarting (attempt {self._crash_count}/{self.MAX_CONSECUTIVE_CRASHES})..."
                        )
                except Exception:
                    logger.error("Failed to send scheduler crash alert")

                if self._crash_count >= self.MAX_CONSECUTIVE_CRASHES:
                    logger.error("Reminder scheduler permanently down after max crashes")
                    break

                # Exponential backoff
                backoff = min(
                    self.BACKOFF_BASE * (2 ** (self._crash_count - 1)),
                    self.BACKOFF_CAP,
                )
                self._stop_event.wait(timeout=backoff)
            finally:
                if not loop.is_closed():
                    loop.close()

    def _read_control_file(self) -> bool:
        """Read the vault control file and return whether the scheduler is enabled.

        Also updates the status section of the control file.
        Returns True if enabled (default), False if paused.
        """
        try:
            vault_path = settings.vault_path
            control_dir = vault_path / "LifeOS" / "Reminders"
            control_file = control_dir / "Scheduler.md"

            enabled = True

            if control_file.exists():
                content = control_file.read_text(encoding="utf-8")
                # Parse YAML frontmatter
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            frontmatter = yaml.safe_load(parts[1])
                            if isinstance(frontmatter, dict):
                                enabled = frontmatter.get("enabled", True)
                        except yaml.YAMLError:
                            pass
            else:
                # Create control file with defaults
                control_dir.mkdir(parents=True, exist_ok=True)

            # Update status section
            status = "running" if enabled else "paused"
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            new_content = (
                f"---\nenabled: {'true' if enabled else 'false'}\n---\n"
                f"# Reminder Scheduler\n\n"
                f"Toggle the scheduler by changing `enabled` to `false`.\n"
                f"Status updates below are auto-generated.\n\n"
                f"**Status:** {status}\n"
                f"**Last check:** {now_str}\n"
                f"**Crashes:** {self._crash_count}\n"
            )
            control_file.write_text(new_content, encoding="utf-8")
            return enabled
        except Exception as e:
            logger.debug(f"Failed to read scheduler control file: {e}")
            return True  # Default to enabled on error

    async def _schedule_loop(self):
        """Check for due reminders every 60 seconds."""
        while not self._stop_event.is_set():
            try:
                # Check vault control file
                enabled = self._read_control_file()
                if enabled:
                    due = self.store.get_due_reminders()
                    for reminder in due:
                        await self._fire_reminder(reminder)
                else:
                    logger.debug("Reminder scheduler paused via control file")
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Wait 60 seconds, checking stop event every second
            for _ in range(60):
                if self._stop_event.is_set():
                    return
                await asyncio.sleep(1)

    def _should_skip_pre_meeting(self, reminder: Reminder) -> bool:
        """Check if a pre-meeting prep reminder can be skipped (no upcoming meeting)."""
        if "pre-meeting" not in reminder.name.lower():
            return False
        try:
            from api.services.calendar import get_calendar_service
            from api.services.google_auth import get_configured_accounts
            for account in get_configured_accounts():
                try:
                    cal = get_calendar_service(account)
                    if cal.has_upcoming_meeting(20):
                        return False
                except Exception:
                    continue
            logger.info(f"Reminder {reminder.name}: no upcoming meeting, skipping pipeline")
            return True
        except Exception as e:
            logger.debug(f"Pre-meeting check failed, running pipeline: {e}")
            return False

    async def _fire_reminder(self, reminder: Reminder):
        """Execute a single reminder with retry and execution logging."""
        import time as _time
        logger.info(f"Firing reminder: {reminder.name} ({reminder.id})")

        # Advance next_trigger_at BEFORE generating/sending to prevent
        # duplicate fires on server restart or scheduler re-entry.
        self.store.mark_triggered(reminder.id)

        # Lightweight pre-check for pre-meeting prep reminders
        if reminder.message_type == "prompt" and self._should_skip_pre_meeting(reminder):
            return

        start = _time.monotonic()

        try:
            message, exec_log = await self._generate_message(reminder)
            elapsed = _time.monotonic() - start

            if exec_log:
                exec_log["elapsed_seconds"] = round(elapsed, 1)
                logger.info(
                    f"Reminder {reminder.name} executed: "
                    f"{exec_log.get('tool_calls', 0)} tools, "
                    f"{exec_log.get('cost_usd', 0):.4f} USD, "
                    f"{elapsed:.1f}s"
                )

            if message and not self._should_suppress(message):
                from api.services.telegram import send_message_async
                full_message = f"*{reminder.name}*\n\n{message}"
                await send_message_async(full_message)
            elif message and self._should_suppress(message):
                logger.info(f"Reminder {reminder.name}: suppressed (no actionable content)")
        except Exception as e:
            elapsed = _time.monotonic() - start
            logger.error(f"Failed to fire reminder {reminder.id} after {elapsed:.1f}s: {e}")
            # Never silently fail — send error notification
            try:
                from api.services.telegram import send_message_async
                await send_message_async(
                    f"*{reminder.name}* (failed)\n\n"
                    f"Reminder could not execute: {str(e)[:200]}"
                )
            except Exception:
                logger.error(f"Failed to send error notification for reminder {reminder.id}")

    async def _generate_message(self, reminder: Reminder) -> tuple[Optional[str], Optional[dict]]:
        """Generate the message content for a reminder.

        Returns (message, execution_log) where execution_log contains
        tool_calls, cost_usd, model, etc. for prompt-type reminders.
        """
        if reminder.message_type == "static":
            return reminder.message_content, None

        elif reminder.message_type == "prompt":
            return await self._execute_prompt_reminder(reminder)

        elif reminder.message_type == "endpoint":
            result = await self._call_endpoint(reminder.endpoint_config)
            return result, None

        else:
            logger.warning(f"Unknown message type: {reminder.message_type}")
            return None, None

    async def _execute_prompt_reminder(
        self, reminder: Reminder, max_retries: int = 2
    ) -> tuple[Optional[str], Optional[dict]]:
        """Execute a prompt-type reminder with retry and execution logging.

        Calls the full agentic chat pipeline via chat_via_api() and captures
        tool execution status, usage, and cost from SSE events.

        Returns (answer_text, execution_log).
        """
        from api.services.telegram import chat_via_api_with_log

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                result = await chat_via_api_with_log(reminder.message_content)
                answer = result.get("answer", "").strip()
                exec_log = {
                    "tool_calls": len(result.get("tool_statuses", [])),
                    "tools_used": result.get("tool_statuses", []),
                    "cost_usd": result.get("cost_usd", 0),
                    "model": result.get("model", ""),
                    "input_tokens": result.get("input_tokens", 0),
                    "output_tokens": result.get("output_tokens", 0),
                    "attempt": attempt,
                }
                if answer:
                    return answer, exec_log
                # Empty answer — retry
                last_error = "Empty response from chat pipeline"
                logger.warning(f"Reminder {reminder.name} attempt {attempt}: empty response, retrying")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Reminder {reminder.name} attempt {attempt} failed: {e}")

        # All retries exhausted — return partial result
        logger.error(f"Reminder {reminder.name}: all {max_retries} attempts failed: {last_error}")
        return f"(Reminder execution failed after {max_retries} attempts: {last_error})", {
            "tool_calls": 0,
            "error": last_error,
            "attempt": max_retries,
        }

    _SUPPRESS_SENTINELS = ("NO_MEETING", "NO_MEETINGS", "NOTHING_TO_REPORT", "NO_ACTION")

    @staticmethod
    def _should_suppress(message: str) -> bool:
        """Check if a prompt response indicates nothing to report.

        Prompt-type reminders can return sentinel values (e.g. 'NO_MEETING')
        to signal that no notification should be sent. This prevents noisy
        messages from high-frequency reminders like pre-meeting prep.

        Matches sentinels exactly or followed by punctuation/separator chars
        (period, dash, em-dash, comma, colon, newline) to handle variants like
        "NO_MEETING." and "NO_MEETING — nothing scheduled" while not
        matching "NO_MEETING but here's something useful".
        """
        stripped = message.strip().upper()
        _SEP = set(".,;:!?\n\r-\u2014\u2013")  # punctuation and dashes
        for sentinel in ReminderScheduler._SUPPRESS_SENTINELS:
            if stripped == sentinel:
                return True
            if stripped.startswith(sentinel) and len(stripped) > len(sentinel):
                next_char = stripped[len(sentinel)]
                if next_char in _SEP:
                    return True
        return False

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
