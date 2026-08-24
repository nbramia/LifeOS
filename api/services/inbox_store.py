"""Durable Life Inbox for messages that still need interpretation."""

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_INBOX_PATH = Path.home() / ".lifeos" / "inbox.json"
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("LIFEOS_INBOX_PATH", str(DEFAULT_INBOX_PATH)))


def add_item(content: str, *, conversation_id: str = "", source: str | dict = "chat") -> dict:
    """Append a raw capture before model interpretation can lose it."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        try:
            data = json.loads(path.read_text()) if path.exists() else {"items": []}
        except (OSError, json.JSONDecodeError):
            data = {"items": []}
        item = {
            "id": str(uuid.uuid4()),
            "content": content,
            "conversation_id": conversation_id,
            "source": source,
            "status": "unreviewed",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data.setdefault("items", []).append(item)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return item


def list_items(
    status: str | None = "unreviewed",
    limit: int = 100,
    since_days: int | None = None,
) -> list[dict]:
    path = _path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items", [])
    if status:
        items = [item for item in items if item.get("status") == status]
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, since_days))
        items = [
            item for item in items
            if item.get("created_at", "") >= cutoff.isoformat()
        ]
    return list(reversed(items[-limit:]))


def update_item(
    item_id: str,
    *,
    status: str,
    category: str = "",
    linked_id: str = "",
    proposal: dict | None = None,
) -> dict | None:
    """Mark an inbox item as reviewed while retaining its raw content."""
    path = _path()
    with _LOCK:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        for item in data.get("items", []):
            if item.get("id") == item_id:
                item["status"] = status
                if category:
                    item["category"] = category
                if linked_id:
                    item["linked_id"] = linked_id
                if proposal is not None:
                    item["proposal"] = proposal
                item["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                return item
    return None
