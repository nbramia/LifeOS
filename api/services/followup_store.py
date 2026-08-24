"""Portable conditional follow-ups (for example, no reply after seven days)."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock


_LOCK = RLock()


def _path() -> Path:
    return Path(os.environ.get("LIFEOS_FOLLOWUPS_PATH", "~/.lifeos/followups.json")).expanduser()


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"followups": []}
    if not isinstance(data, dict) or not isinstance(data.get("followups"), list):
        data = {"followups": []}
    return data


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="followups-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def create(
    person_name: str,
    subject: str,
    *,
    wait_days: int = 7,
    source: dict | None = None,
    schedule_id: str = "",
) -> dict:
    person_name = " ".join(str(person_name or "").split()).strip()
    subject = " ".join(str(subject or "").split()).strip()
    if not person_name or not subject:
        raise ValueError("person_name and subject are required")
    try:
        wait_days = int(wait_days)
    except (TypeError, ValueError):
        raise ValueError("wait_days must be an integer")
    if wait_days < 1 or wait_days > 365:
        raise ValueError("wait_days must be between 1 and 365")
    now = datetime.now(timezone.utc)
    item = {
        "id": uuid.uuid4().hex,
        "person_name": person_name,
        "subject": subject,
        "condition": "no_response",
        "wait_days": wait_days,
        "status": "open",
        "created_at": now.isoformat(),
        "check_at": (now + timedelta(days=wait_days)).isoformat(),
        "source": source or {"type": "conversation"},
        "schedule_id": schedule_id,
    }
    with _LOCK:
        data = _read()
        # Idempotence: a repeated model/tool call should not create duplicate
        # conditional reminders for the same person and subject.
        for existing in data["followups"]:
            if (
                existing.get("status") == "open"
                and existing.get("person_name", "").casefold() == person_name.casefold()
                and existing.get("subject", "").casefold() == subject.casefold()
            ):
                return existing
        data["followups"].append(item)
        _write(data)
    return item


def attach_schedule(followup_id: str, schedule_id: str) -> dict | None:
    with _LOCK:
        data = _read()
        for item in data["followups"]:
            if item.get("id") == followup_id:
                item["schedule_id"] = schedule_id
                _write(data)
                return item
    return None


def list_followups(*, status: str = "open", limit: int = 50) -> list[dict]:
    with _LOCK:
        rows = list(_read()["followups"])
    if status:
        rows = [row for row in rows if row.get("status") == status]
    rows.sort(key=lambda row: row.get("check_at", ""))
    return rows[: max(1, min(int(limit or 50), 500))]


def update_status(followup_id: str, status: str) -> dict | None:
    if status not in {"open", "reminded", "completed", "cancelled"}:
        raise ValueError("invalid follow-up status")
    with _LOCK:
        data = _read()
        for item in data["followups"]:
            if item.get("id") == followup_id:
                item["status"] = status
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                _write(data)
                return item
    return None
