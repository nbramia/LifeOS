"""
Scheduler Store and Scheduler for LifeOS.

A *schedule* binds a trigger (one-off ``at`` or recurring ``cron``) to an
**action**. When it fires it can:

- ``notify``   — send a static message via Telegram (legacy ``static``)
- ``prompt``   — run a prompt through the full LifeOS chat pipeline and send the result
- ``endpoint`` — call a LifeOS API endpoint and send the formatted result
- ``agent``    — hand work off to the agent worker (behaviour lands in #245)

Source of truth: ``LifeOS/Scheduler/Inbox.md`` in the vault. Schedules are
checkbox lines with stable ``<!-- id:xxxx -->`` IDs and Dataview-style
``[key:: value]`` inline fields, exactly like tasks — so they can be viewed
and edited in Obsidian and a watcher reindexes on change.

The markdown line carries the user-editable *definition* (enabled checkbox,
name, trigger, timezone, action, message type, executor tag). The
human-editable instruction text (``message_content``) is written beneath the
line as an indented ``> `` blockquote body, so it can be read and edited in
Obsidian and round-trips back through reindexing. Indented blockquote lines
directly following a schedule line are reserved for this body — a standalone
note placed there will be absorbed into the schedule's ``message_content`` on
reindex. The structured
``endpoint_config`` and computed ``next_trigger_at`` (plus run history) live in
a rebuildable index cache (``data/scheduler_index.json``) and are merged back
by ID when markdown is reindexed. The cache is never the source of truth — it
can be deleted and rebuilt from the vault.
"""
import asyncio
import json
import logging
import re
import threading
import time as _time_mod
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yaml

from croniter import croniter
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path("data/scheduler_index.json")

SCHEDULER_FOLDER = "LifeOS/Scheduler"
INBOX_FILE = "Inbox.md"
DASHBOARD_FILE = "Dashboard.md"
CONTROL_FILE = "Scheduler.md"

VALID_ACTIONS = ("notify", "prompt", "endpoint", "agent")
# Legacy message_type → action mapping (static notifications became "notify").
_LEGACY_TYPE_TO_ACTION = {"static": "notify", "prompt": "prompt", "endpoint": "endpoint"}


@dataclass
class ScheduleEntry:
    """A scheduled trigger bound to an action."""
    id: str
    name: str
    schedule_type: str  # "once" or "cron"
    schedule_value: str  # ISO datetime (once) or cron expression (cron)
    action: str = "notify"  # notify / prompt / endpoint / agent
    message_type: str = "static"  # legacy: static / prompt / endpoint
    message_content: str = ""  # static text or natural-language prompt
    endpoint_config: Optional[dict] = None  # {endpoint, method, params}
    executor: str = ""  # agent executor tag without '#': local / cloud / cloud-haiku ...
    bot: str = ""  # Telegram bot name for outbound sends (e.g. "fitness", "therapy"); empty = primary
    enabled: bool = True
    created_at: str = ""
    last_triggered_at: Optional[str] = None
    next_trigger_at: Optional[str] = None
    timezone: str = ""  # resolved from settings.timezone when empty
    last_status: str = ""  # outcome of the most recent fire (sent/suppressed/handed-off/failed)
    last_result: str = ""  # short snippet of the most recent fire's result

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleEntry":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def compute_next_trigger(entry: ScheduleEntry) -> Optional[str]:
    """
    Compute the next trigger time for a schedule.

    For cron expressions, times are interpreted in the schedule's timezone
    (defaults to settings.timezone) and converted to UTC for storage, so
    "daily at 6pm" means 6pm local, not 6pm UTC.
    """
    now_utc = datetime.now(timezone.utc)

    if entry.schedule_type == "once":
        try:
            trigger_time = datetime.fromisoformat(entry.schedule_value)
            if trigger_time.tzinfo is None:
                tz = ZoneInfo(entry.timezone or settings.timezone)
                trigger_time = trigger_time.replace(tzinfo=tz)
            trigger_time_utc = trigger_time.astimezone(timezone.utc)
            if trigger_time_utc > now_utc:
                return trigger_time_utc.isoformat()
            return None  # past one-time schedule
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid datetime for schedule {entry.id}: {e}")
            return None

    elif entry.schedule_type == "cron":
        try:
            tz = ZoneInfo(entry.timezone or settings.timezone)
            now_local = datetime.now(tz)
            cron = croniter(entry.schedule_value, now_local)
            next_time_local = cron.get_next(datetime)
            if next_time_local.tzinfo is None:
                next_time_local = next_time_local.replace(tzinfo=tz)
            return next_time_local.astimezone(timezone.utc).isoformat()
        except (ValueError, KeyError) as e:
            logger.error(
                f"Invalid cron expression for schedule {entry.id}: "
                f"{entry.schedule_value} - {e}"
            )
            return None

    return None


def _format_cron_human(cron_expr: str, tz_name: str = "") -> str:
    """Convert a cron expression to a human-readable string with timezone."""
    tz_name = tz_name or settings.timezone
    parts = cron_expr.split()
    if len(parts) < 5:
        return cron_expr
    minute, hour, _, _, dow = parts

    tz_abbrev = "ET" if "New_York" in tz_name else tz_name.split("/")[-1]

    dow_map = {"*": "daily", "1-5": "weekdays", "0,6": "weekends",
               "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
               "4": "Thu", "5": "Fri", "6": "Sat"}
    day_str = dow_map.get(dow, dow)

    if minute.startswith("*/"):
        interval = minute[2:]
        if "-" in hour:
            h_start, h_end = hour.split("-", 1)
            try:
                hs = int(h_start)
                he = int(h_end)
                s_ampm = "AM" if hs < 12 else "PM"
                e_ampm = "AM" if he < 12 else "PM"
                hs12 = hs % 12 or 12
                he12 = he % 12 or 12
                return f"{day_str} every {interval}m, {hs12}{s_ampm}–{he12}{e_ampm} {tz_abbrev}"
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


# ======================================================================
# Markdown round-trip (mirrors task_manager._format/_parse)
# ======================================================================

_INLINE_FIELD_RE = re.compile(r'\[(\w+)::\s*([^\]]*)\]')
_TAG_RE = re.compile(r'#([\w-]+)')
_ID_RE = re.compile(r'<!--\s*id:(\w+)\s*-->')
_CHECKBOX_RE = re.compile(r'^- \[(.)\]\s+(.*)$')
# Instruction-text body: an indented blockquote line beneath a schedule line.
_BODY_LINE_RE = re.compile(r'^\s+>\s?(.*)$')
_BODY_INDENT = "    "


def _format_entry_line(entry: ScheduleEntry) -> str:
    """ScheduleEntry → Dataview-style markdown line (lossless for definition fields)."""
    symbol = " " if entry.enabled else "x"
    parts = [f"- [{symbol}] {entry.name}"]

    trig_key = "cron" if entry.schedule_type == "cron" else "at"
    parts.append(f"[{trig_key}:: {entry.schedule_value}]")
    if entry.timezone:
        parts.append(f"[tz:: {entry.timezone}]")
    parts.append(f"[action:: {entry.action}]")
    parts.append(f"[mtype:: {entry.message_type}]")
    if entry.bot:
        parts.append(f"[bot:: {entry.bot}]")
    if entry.executor:
        parts.append(f"#{entry.executor}")
    if entry.created_at:
        parts.append(f"[created:: {entry.created_at}]")
    if entry.last_triggered_at:
        parts.append(f"[last:: {entry.last_triggered_at}]")
    parts.append(f"<!-- id:{entry.id} -->")
    return " ".join(parts)


def _parse_entry_line(line: str) -> Optional[ScheduleEntry]:
    """Parse one checkbox line into a ScheduleEntry, or None if not a schedule line."""
    m = _CHECKBOX_RE.match(line)
    if not m:
        return None
    symbol, rest = m.group(1), m.group(2)

    fields = {fm.group(1): fm.group(2).strip() for fm in _INLINE_FIELD_RE.finditer(rest)}

    # A schedule line must carry a trigger field.
    if "cron" in fields:
        schedule_type, schedule_value = "cron", fields["cron"]
    elif "at" in fields:
        schedule_type, schedule_value = "once", fields["at"]
    else:
        return None

    id_match = _ID_RE.search(rest)
    entry_id = id_match.group(1) if id_match else uuid.uuid4().hex[:8]

    tags = _TAG_RE.findall(rest)
    executor = tags[0] if tags else ""

    # Name = leading text with inline fields, tags and id comment removed.
    name = rest
    name = _ID_RE.sub("", name)
    name = _INLINE_FIELD_RE.sub("", name)
    name = re.sub(r'#[\w-]+', "", name)
    name = name.strip()

    action = fields.get("action") or "notify"
    message_type = fields.get("mtype") or "static"

    return ScheduleEntry(
        id=entry_id,
        name=name,
        schedule_type=schedule_type,
        schedule_value=schedule_value,
        action=action,
        message_type=message_type,
        executor=executor,
        bot=fields.get("bot", ""),
        enabled=(symbol == " "),
        created_at=fields.get("created", ""),
        last_triggered_at=fields.get("last") or None,
        timezone=fields.get("tz", ""),
    )


def _format_entry_block(entry: ScheduleEntry) -> list[str]:
    """ScheduleEntry → its markdown checkbox line plus an indented ``> ``
    blockquote body carrying the human-editable ``message_content`` (one line
    per content line). Empty content emits no body lines.
    """
    lines = [_format_entry_line(entry)]
    if entry.message_content:
        for content_line in entry.message_content.split("\n"):
            lines.append(f"{_BODY_INDENT}> {content_line}" if content_line else f"{_BODY_INDENT}>")
    return lines


def _iter_entry_blocks(lines: list[str]):
    """Yield ``(start, end, entry)`` for each schedule block in ``lines``.

    ``end`` is exclusive and includes any indented blockquote body lines that
    immediately follow the checkbox line; the body is attached to the entry's
    ``message_content``.
    """
    i, n = 0, len(lines)
    while i < n:
        entry = _parse_entry_line(lines[i])
        if entry is None:
            i += 1
            continue
        start, j, body = i, i + 1, []
        while j < n:
            m = _BODY_LINE_RE.match(lines[j])
            if not m:
                break
            body.append(m.group(1))
            j += 1
        if body:
            entry.message_content = "\n".join(body)
        yield start, j, entry
        i = j


def _find_block_span(lines: list[str], entry_id: str) -> Optional[tuple[int, int]]:
    """Return the ``(start, end)`` line span of the block for ``entry_id``."""
    for start, end, entry in _iter_entry_blocks(lines):
        if entry.id == entry_id:
            return start, end
    return None


class SchedulerStore:
    """
    CRUD store for schedules backed by ``LifeOS/Scheduler/Inbox.md``.

    Markdown is the source of truth; ``data/scheduler_index.json`` is a
    rebuildable query cache holding the full entry (including payload fields
    and computed ``next_trigger_at``). Thread-safe.

    For backward compatibility a ``file_path=`` argument (the old JSON path)
    is accepted: it becomes the index path and its parent directory becomes an
    isolated vault root, so existing callers and tests keep working.
    """

    def __init__(
        self,
        vault_path: Optional[Path] = None,
        index_path: Optional[Path] = None,
        file_path: Optional[str] = None,
    ):
        if file_path is not None:
            # Back-compat: file_path was the JSON store; treat it as the index
            # cache and isolate the vault alongside it.
            self.index_path = Path(file_path)
            base = self.index_path.parent
        else:
            self.index_path = Path(index_path) if index_path else DEFAULT_INDEX_PATH
            base = Path(vault_path) if vault_path else Path(settings.vault_path)

        self.scheduler_dir = base / SCHEDULER_FOLDER
        self.inbox_path = self.scheduler_dir / INBOX_FILE
        self._entries: dict[str, ScheduleEntry] = {}
        self._lock = threading.Lock()

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler_dir.mkdir(parents=True, exist_ok=True)

        self._load()

    # ------------------------------------------------------------------
    # Load / persist
    # ------------------------------------------------------------------

    def _load(self):
        """Load the index cache; rebuild from markdown if missing/stale."""
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                for item in data.get("schedules", data.get("reminders", [])):
                    entry = ScheduleEntry.from_dict(item)
                    self._entries[entry.id] = entry
                logger.info(f"Loaded {len(self._entries)} schedules from index")
                return
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Error loading scheduler index: {e}. Rebuilding from vault.")
        self.rebuild_index()

    def _save(self):
        """Persist the index cache and regenerate the dashboard."""
        data = {
            "description": "LifeOS Scheduler Index (cache — regenerated from vault markdown)",
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "schedules": [e.to_dict() for e in self._entries.values()],
        }
        self.index_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        self._write_dashboard()

    # ------------------------------------------------------------------
    # Markdown helpers (operate by ID on the single Inbox.md)
    # ------------------------------------------------------------------

    def _read_inbox_lines(self) -> list[str]:
        if self.inbox_path.exists():
            return self.inbox_path.read_text(encoding="utf-8").splitlines()
        return []

    def _write_inbox_lines(self, lines: list[str]):
        self.inbox_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ensure_inbox(self):
        if not self.inbox_path.exists():
            self.inbox_path.write_text(
                "---\ntype: scheduler\n---\n# Scheduler Inbox\n\n", encoding="utf-8"
            )

    def _insert_block_at_top(self, block: list[str]):
        """Insert a schedule block above the first existing schedule line."""
        self._ensure_inbox()
        lines = self._read_inbox_lines()
        first_idx = next(
            (i for i, ln in enumerate(lines) if _parse_entry_line(ln) is not None),
            None,
        )
        if first_idx is None:
            lines.extend(block)
        else:
            lines = lines[:first_idx] + block + lines[first_idx:]
        self._write_inbox_lines(lines)

    def _rewrite_block(self, entry: ScheduleEntry):
        """Replace the markdown block for ``entry`` (by ID); insert if absent."""
        block = _format_entry_block(entry)
        lines = self._read_inbox_lines()
        span = _find_block_span(lines, entry.id)
        if span is None:
            self._insert_block_at_top(block)
            return
        start, end = span
        self._write_inbox_lines(lines[:start] + block + lines[end:])

    def _remove_block(self, entry_id: str):
        lines = self._read_inbox_lines()
        span = _find_block_span(lines, entry_id)
        if span is None:
            return
        start, end = span
        self._write_inbox_lines(lines[:start] + lines[end:])

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, action: Optional[str] = None, **kwargs) -> ScheduleEntry:
        with self._lock:
            # Default action from legacy message_type when not given explicitly.
            if action is None:
                action = _LEGACY_TYPE_TO_ACTION.get(kwargs.get("message_type", "static"), "notify")
            entry = ScheduleEntry(
                id=uuid.uuid4().hex[:8],
                created_at=datetime.now(timezone.utc).isoformat(),
                action=action,
                **kwargs,
            )
            entry.next_trigger_at = compute_next_trigger(entry)
            self._insert_block_at_top(_format_entry_block(entry))
            self._entries[entry.id] = entry
            self._save()
            logger.info(f"Created schedule: {entry.id} - {entry.name}")
            return entry

    def get(self, entry_id: str) -> Optional[ScheduleEntry]:
        return self._entries.get(entry_id)

    def list_all(self) -> list[ScheduleEntry]:
        return sorted(
            self._entries.values(),
            key=lambda e: e.created_at or "",
            reverse=True,
        )

    def update(self, entry_id: str, **kwargs) -> Optional[ScheduleEntry]:
        with self._lock:
            entry = self._entries.get(entry_id)
            if not entry:
                return None
            for key, value in kwargs.items():
                if hasattr(entry, key) and value is not None:
                    setattr(entry, key, value)
            if any(k in kwargs for k in ("schedule_type", "schedule_value", "enabled", "timezone")):
                entry.next_trigger_at = compute_next_trigger(entry) if entry.enabled else None
            self._rewrite_block(entry)
            self._save()
            return entry

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            if entry_id in self._entries:
                del self._entries[entry_id]
                self._remove_block(entry_id)
                self._save()
                return True
            return False

    def mark_triggered(self, entry_id: str):
        """Mark a schedule as triggered and advance/disable it."""
        with self._lock:
            entry = self._entries.get(entry_id)
            if not entry:
                return
            entry.last_triggered_at = datetime.now(timezone.utc).isoformat()
            if entry.schedule_type == "once":
                entry.enabled = False
                entry.next_trigger_at = None
            else:
                entry.next_trigger_at = compute_next_trigger(entry)
            self._rewrite_block(entry)
            self._save()

    def record_run(self, entry_id: str, status: str, result: str = ""):
        """Record the outcome of the most recent fire for the dashboard.

        ``last_status``/``last_result`` live only in the index cache (not the
        markdown definition), so this just refreshes the cache and dashboard.
        """
        with self._lock:
            entry = self._entries.get(entry_id)
            if not entry:
                return
            entry.last_status = status
            entry.last_result = (result or "")[:200]
            self._save()

    def get_due_reminders(self) -> list[ScheduleEntry]:
        """Return enabled schedules due to fire (90s cooldown to dedupe restarts).

        Missed-fire policy — **run-once catch-up**: a schedule whose trigger
        elapsed while the server was down is past-due on the next tick after
        startup, so it fires exactly once (the 90s cooldown dedupes overlapping
        restarts). Cron schedules then advance to their next future slot; a
        missed one-off fires once and disables. We deliberately do not replay
        every window missed during an outage — at most one catch-up per
        schedule.
        """
        now = datetime.now(timezone.utc)
        due = []
        for entry in self._entries.values():
            if not entry.enabled or not entry.next_trigger_at:
                continue
            try:
                next_time = datetime.fromisoformat(entry.next_trigger_at)
                if next_time.tzinfo is None:
                    next_time = next_time.replace(tzinfo=timezone.utc)
                if next_time > now:
                    continue
                if entry.last_triggered_at:
                    last = datetime.fromisoformat(entry.last_triggered_at)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (now - last).total_seconds() < 90:
                        continue
                due.append(entry)
            except (ValueError, TypeError):
                continue
        return due

    # Alias under the new name (clearer; old name kept for the scheduler tick).
    get_due_schedules = get_due_reminders

    # ------------------------------------------------------------------
    # Reindex (markdown → cache), merging preserved payload by ID
    # ------------------------------------------------------------------

    def reindex_file(self, file_path: str):
        """Re-parse a scheduler markdown file and refresh the index for it."""
        path = Path(file_path)
        if path.name in (DASHBOARD_FILE, CONTROL_FILE):
            return  # generated/control files are never schedule sources

        if not path.exists():
            with self._lock:
                if self._entries:
                    self._entries.clear()
                    self._save()
            return

        with self._lock:
            # Preserve payload + history from the current cache, merged by ID.
            prior = {eid: e for eid, e in self._entries.items()}
            self._entries.clear()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                logger.warning(f"Could not read {file_path}: {e}")
                return
            for _start, _end, entry in _iter_entry_blocks(lines):
                self._merge_prior(entry, prior.get(entry.id))
                entry.next_trigger_at = compute_next_trigger(entry) if entry.enabled else None
                self._entries[entry.id] = entry
            self._save()

    def rebuild_index(self):
        """Full re-parse of Inbox.md into the cache."""
        prior = {eid: e for eid, e in self._entries.items()}
        self._entries.clear()
        for _start, _end, entry in _iter_entry_blocks(self._read_inbox_lines()):
            self._merge_prior(entry, prior.get(entry.id))
            entry.next_trigger_at = compute_next_trigger(entry) if entry.enabled else None
            self._entries[entry.id] = entry
        self._save()
        logger.info(f"Rebuilt scheduler index: {len(self._entries)} schedules")

    @staticmethod
    def _merge_prior(entry: ScheduleEntry, prior: Optional[ScheduleEntry]):
        """Carry cached fields forward by ID when markdown doesn't supply them.

        ``message_content`` now lives in the markdown body, so markdown wins when
        a body is present; the cache fallback only fills entries written before
        bodies existed (lazy migration). ``endpoint_config`` and run history have
        no markdown representation and are always carried from the cache.
        """
        if not prior:
            return
        if not entry.message_content:
            entry.message_content = prior.message_content
        if entry.endpoint_config is None:
            entry.endpoint_config = prior.endpoint_config
        if not entry.created_at:
            entry.created_at = prior.created_at
        if not entry.last_triggered_at:
            entry.last_triggered_at = prior.last_triggered_at
        if not entry.last_status:
            entry.last_status = prior.last_status
        if not entry.last_result:
            entry.last_result = prior.last_result

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _write_dashboard(self):
        """Regenerate Dashboard.md with Recurring / Upcoming / Recently-fired sections."""
        try:
            content = self._build_dashboard_content()
        except Exception as e:
            logger.debug(f"Failed to build scheduler dashboard: {e}")
            return
        dashboard = self.scheduler_dir / DASHBOARD_FILE
        try:
            if dashboard.exists() and dashboard.read_text(encoding="utf-8") == content:
                return  # no-op write avoids triggering the watcher
        except Exception:
            pass
        dashboard.write_text(content, encoding="utf-8")

    def _build_dashboard_content(self) -> str:
        now = datetime.now(timezone.utc)
        entries = list(self._entries.values())

        recurring = [e for e in entries if e.enabled and e.schedule_type == "cron"]
        upcoming = [e for e in entries if e.enabled and e.schedule_type == "once" and e.next_trigger_at]
        fired = [e for e in entries if e.last_triggered_at]

        upcoming.sort(key=lambda e: e.next_trigger_at or "")
        fired.sort(key=lambda e: e.last_triggered_at or "", reverse=True)

        lines = [
            "---",
            "type: dashboard",
            "---",
            "<!-- AUTO-GENERATED by LifeOS scheduler. Manual edits are overwritten on the next change. -->",
            "# Scheduler Dashboard",
            "",
            f"> Auto-generated by LifeOS — {now.strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "## Recurring",
        ]
        if recurring:
            lines += [
                "",
                "| Name | Schedule | Action | Next Fire | Last Fired |",
                "|------|----------|--------|-----------|------------|",
            ]
            for e in recurring:
                tz = e.timezone or settings.timezone
                sched = _format_cron_human(e.schedule_value, tz)
                nxt = _format_dt_short(e.next_trigger_at, tz) if e.next_trigger_at else "—"
                last = _format_dt_short(e.last_triggered_at, tz) if e.last_triggered_at else "—"
                lines.append(f"| {e.name} | {sched} | {self._action_label(e)} | {nxt} | {last} |")
        else:
            lines.append("\n_No recurring schedules._")
        lines += ["", "## Upcoming"]
        if upcoming:
            lines += [
                "",
                "| Name | Scheduled For | Action |",
                "|------|---------------|--------|",
            ]
            for e in upcoming:
                tz = e.timezone or settings.timezone
                when = _format_dt_short(e.next_trigger_at, tz) if e.next_trigger_at else "—"
                lines.append(f"| {e.name} | {when} | {self._action_label(e)} |")
        else:
            lines.append("\n_No upcoming schedules._")
        lines += ["", "## Recently Fired"]
        if fired:
            lines += [
                "",
                "| Name | Fired | Action | Outcome | Result |",
                "|------|-------|--------|---------|--------|",
            ]
            for e in fired[:20]:
                tz = e.timezone or settings.timezone
                when = _format_dt_short(e.last_triggered_at, tz) if e.last_triggered_at else "never"
                outcome = e.last_status or "—"
                result = (e.last_result or "").replace("|", "\\|").replace("\n", " ")[:80] or "—"
                lines.append(f"| {e.name} | {when} | {self._action_label(e)} | {outcome} | {result} |")
        else:
            lines.append("\n_Nothing fired yet._")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _action_label(entry: ScheduleEntry) -> str:
        if entry.action == "agent" and entry.executor:
            return f"agent (#{entry.executor})"
        return entry.action


class SchedulerScheduler:
    """
    Background thread that fires due schedules every 60 seconds.

    Firing dispatches on ``entry.action`` (see ``_fire_entry``):
    - notify   → send message_content via Telegram
    - prompt   → run message_content through chat_via_api, send result
    - endpoint → call LifeOS API endpoint, send formatted result
    - agent    → write an #agent task for the agent worker to run

    Crash recovery:
    - Auto-restarts with exponential backoff (5s → 60s cap)
    - Sends Telegram alert on each crash
    - After 5 consecutive crashes, stops retrying and sends a final alert
    - Crash counter resets after 10 minutes of healthy operation

    Vault toggle:
    - Reads {vault}/LifeOS/Scheduler/Scheduler.md frontmatter each iteration
    - If `enabled: false`, skips firing but keeps the thread alive
    """

    MAX_CONSECUTIVE_CRASHES = 5
    BACKOFF_BASE = 5  # seconds
    BACKOFF_CAP = 60  # seconds
    HEALTHY_RESET_SECONDS = 600  # 10 minutes

    def __init__(self, store: SchedulerStore):
        self.store = store
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._crash_count = 0
        self._last_crash_time: Optional[float] = None

    def start(self):
        if not settings.telegram_enabled:
            logger.info("Telegram not configured, scheduler not started")
            return

        self._stop_event.clear()
        self._crash_count = 0
        self._last_crash_time = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="SchedulerScheduler",
        )
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Scheduler stopped")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        """Main scheduler loop with crash recovery."""
        while not self._stop_event.is_set():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._schedule_loop())
                break  # clean exit (stop_event was set)
            except Exception as e:
                loop.close()
                if self._stop_event.is_set():
                    break

                now = _time_mod.monotonic()
                if (self._last_crash_time is not None
                        and now - self._last_crash_time > self.HEALTHY_RESET_SECONDS):
                    self._crash_count = 0

                self._crash_count += 1
                self._last_crash_time = now
                logger.error(f"Scheduler crashed (attempt {self._crash_count}): {e}")

                try:
                    from api.services.telegram import send_message
                    if self._crash_count >= self.MAX_CONSECUTIVE_CRASHES:
                        send_message(
                            f"*Scheduler DOWN*\n\n"
                            f"Crashed {self._crash_count} times consecutively. "
                            f"Giving up. Last error: {str(e)[:200]}\n\n"
                            f"Restart the server to recover."
                        )
                    else:
                        send_message(
                            f"*Scheduler Crashed*\n\n"
                            f"Error: {str(e)[:200]}\n"
                            f"Restarting (attempt {self._crash_count}/{self.MAX_CONSECUTIVE_CRASHES})..."
                        )
                except Exception:
                    logger.error("Failed to send scheduler crash alert")

                if self._crash_count >= self.MAX_CONSECUTIVE_CRASHES:
                    logger.error("Scheduler permanently down after max crashes")
                    break

                backoff = min(
                    self.BACKOFF_BASE * (2 ** (self._crash_count - 1)),
                    self.BACKOFF_CAP,
                )
                self._stop_event.wait(timeout=backoff)
            finally:
                if not loop.is_closed():
                    loop.close()

    def _read_control_file(self) -> bool:
        """Read the vault control file; return whether the scheduler is enabled."""
        try:
            control_dir = self.store.scheduler_dir
            control_file = control_dir / CONTROL_FILE

            enabled = True
            if control_file.exists():
                content = control_file.read_text(encoding="utf-8")
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
                control_dir.mkdir(parents=True, exist_ok=True)

            status = "running" if enabled else "paused"
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            new_content = (
                f"---\nenabled: {'true' if enabled else 'false'}\n---\n"
                f"# Scheduler\n\n"
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
            return True

    async def _schedule_loop(self):
        """Check for due schedules every 60 seconds."""
        while not self._stop_event.is_set():
            try:
                if self._read_control_file():
                    for entry in self.store.get_due_reminders():
                        await self._fire_entry(entry)
                else:
                    logger.debug("Scheduler paused via control file")
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            for _ in range(60):
                if self._stop_event.is_set():
                    return
                await asyncio.sleep(1)

    def _should_skip_pre_meeting(self, entry: ScheduleEntry) -> bool:
        """Skip a pre-meeting prep schedule when no meeting is upcoming."""
        if "pre-meeting" not in entry.name.lower():
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
            logger.info(f"Schedule {entry.name}: no upcoming meeting, skipping pipeline")
            return True
        except Exception as e:
            logger.debug(f"Pre-meeting check failed, running pipeline: {e}")
            return False

    async def _fire_entry(self, entry: ScheduleEntry):
        """Execute a single schedule, dispatching on ``entry.action``.

        Actions:
        - ``notify``   — send the static ``message_content`` via Telegram
        - ``prompt``   — run ``message_content`` through the chat pipeline (with
          retry) and send the result via Telegram
        - ``endpoint`` — call a LifeOS API endpoint and send the formatted result
        - ``agent``    — write an ``#agent`` task (carrying the executor tag) into
          ``LifeOS/Tasks/Inbox.md`` so the existing agent worker runs it

        For ``notify``/``prompt`` the Telegram message is suppressed when the
        result is empty or a suppression sentinel (e.g. ``NO_MEETING``). Every
        fire records ``last_status``/``last_result`` for the dashboard's
        Recently-Fired section.
        """
        import time as _time
        logger.info(f"Firing schedule: {entry.name} ({entry.id}, action={entry.action})")

        # Advance next_trigger_at BEFORE generating/sending to prevent
        # duplicate fires on server restart or scheduler re-entry.
        self.store.mark_triggered(entry.id)

        if entry.action == "prompt" and self._should_skip_pre_meeting(entry):
            self.store.record_run(entry.id, "skipped", "no upcoming meeting")
            return

        start = _time.monotonic()
        try:
            if entry.action == "agent":
                handoff = self._hand_off_to_agent(entry)
                logger.info(f"Schedule {entry.name}: handed off to agent worker — {handoff}")
                self.store.record_run(entry.id, "handed-off", handoff)
                return

            message, exec_log = await self._generate_message(entry)
            elapsed = _time.monotonic() - start

            if exec_log:
                exec_log["elapsed_seconds"] = round(elapsed, 1)
                logger.info(
                    f"Schedule {entry.name} executed: "
                    f"{exec_log.get('tool_calls', 0)} tools, "
                    f"{exec_log.get('cost_usd', 0):.4f} USD, "
                    f"{elapsed:.1f}s"
                )

            # Generalised suppression: notify/prompt with empty or sentinel
            # results produce no notification.
            if entry.action in ("notify", "prompt") and (
                not message or self._should_suppress(message)
            ):
                logger.info(f"Schedule {entry.name}: suppressed (no actionable content)")
                self.store.record_run(entry.id, "suppressed", "")
                return

            if message:
                from api.services.telegram import send_message_async
                await send_message_async(f"*{entry.name}*\n\n{message}", bot=entry.bot or None)
                self.store.record_run(entry.id, "sent", message[:200])
            else:
                self.store.record_run(entry.id, "empty", "")
        except Exception as e:
            elapsed = _time.monotonic() - start
            logger.error(f"Failed to fire schedule {entry.id} after {elapsed:.1f}s: {e}")
            self.store.record_run(entry.id, "failed", str(e)[:200])
            try:
                from api.services.telegram import send_message_async
                await send_message_async(
                    f"*{entry.name}* (failed)\n\n"
                    f"Schedule could not execute: {str(e)[:200]}",
                    bot=entry.bot or None,
                )
            except Exception:
                logger.error(f"Failed to send error notification for schedule {entry.id}")

    # Back-compat alias: the HTTP trigger route and older tests call _fire_reminder.
    _fire_reminder = _fire_entry

    def _hand_off_to_agent(self, entry: ScheduleEntry) -> str:
        """Write an ``#agent`` task so the existing agent worker runs the prompt.

        The schedule's executor tag (``local`` / ``cloud`` / ``cloud-haiku`` /
        ``cloud-sonnet``) is passed straight through as a task tag — the agent
        worker's preflight routes on it, so no new execution path is needed.

        For ``cron`` (recurring) schedules we also stamp a ``sched-<id>`` tag.
        The worker reads it on completion to recognise the task as a recurring
        fire and append its output to one shared note per schedule, rather than
        a new note per fire. One-time (``once``) schedules get no such tag — each
        is a stand-alone task that produces its own note.
        """
        from api.services.task_manager import get_task_manager
        tags = ["agent"]
        if entry.executor:
            tags.append(entry.executor.lstrip("#"))
        if entry.schedule_type == "cron":
            tags.append(f"sched-{entry.id}")
        task = get_task_manager().create(
            description=entry.message_content or entry.name,
            tags=tags,
        )
        return f"task {task.id} (tags: {', '.join('#' + t for t in tags)})"

    async def _generate_message(self, entry: ScheduleEntry) -> tuple[Optional[str], Optional[dict]]:
        """Generate the message content for a schedule, dispatching on action."""
        if entry.action == "notify":
            return entry.message_content, None
        elif entry.action == "prompt":
            return await self._execute_prompt_reminder(entry)
        elif entry.action == "endpoint":
            return await self._call_endpoint(entry.endpoint_config), None
        else:
            logger.warning(f"Unsupported action for message generation: {entry.action}")
            return None, None

    async def _execute_prompt_reminder(
        self, entry: ScheduleEntry, max_retries: int = 2
    ) -> tuple[Optional[str], Optional[dict]]:
        """Execute a prompt-type schedule with retry and execution logging."""
        from api.services.telegram import chat_via_api_with_log

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                result = await chat_via_api_with_log(entry.message_content)
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
                last_error = "Empty response from chat pipeline"
                logger.warning(f"Schedule {entry.name} attempt {attempt}: empty response, retrying")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Schedule {entry.name} attempt {attempt} failed: {e}")

        logger.error(f"Schedule {entry.name}: all {max_retries} attempts failed: {last_error}")
        return f"(Reminder execution failed after {max_retries} attempts: {last_error})", {
            "tool_calls": 0,
            "error": last_error,
            "attempt": max_retries,
        }

    _SUPPRESS_SENTINELS = ("NO_MEETING", "NO_MEETINGS", "NOTHING_TO_REPORT", "NO_ACTION")

    @staticmethod
    def _should_suppress(message: str) -> bool:
        """Return True when a prompt response signals nothing to report.

        Matches sentinels exactly or followed by punctuation/separator chars
        (period, dash, em-dash, comma, colon, newline) to handle variants like
        "NO_MEETING." and "NO_MEETING — nothing scheduled" while not matching
        "NO_MEETING but here's something useful".
        """
        stripped = message.strip().upper()
        _SEP = set(".,;:!?\n\r-—–")
        for sentinel in SchedulerScheduler._SUPPRESS_SENTINELS:
            if stripped == sentinel:
                return True
            if stripped.startswith(sentinel) and len(stripped) > len(sentinel):
                if stripped[len(sentinel)] in _SEP:
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
                return _format_endpoint_result(data)
        except Exception as e:
            return f"Error calling endpoint: {e}"


def _format_endpoint_result(data) -> str:
    """Turn an endpoint's JSON response into the message to send.

    An endpoint may return a ready-to-send ``{"message": "..."}`` — use it
    verbatim, so an empty message suppresses the notification via the fire
    loop's ``if message:`` guard. Otherwise fall back to a pretty JSON dump.
    """
    if isinstance(data, dict) and "message" in data:
        return data["message"]
    return json.dumps(data, indent=2, default=str)[:3500]


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_scheduler_store: Optional[SchedulerStore] = None
_scheduler: Optional[SchedulerScheduler] = None


def get_scheduler_store() -> SchedulerStore:
    global _scheduler_store
    if _scheduler_store is None:
        _scheduler_store = SchedulerStore()
    return _scheduler_store


def get_scheduler() -> SchedulerScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerScheduler(get_scheduler_store())
    return _scheduler
