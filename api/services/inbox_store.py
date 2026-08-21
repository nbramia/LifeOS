"""Durable Life Inbox for messages that still need interpretation."""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_INBOX_PATH = Path.home() / ".lifeos" / "inbox.json"
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("LIFEOS_INBOX_PATH", str(DEFAULT_INBOX_PATH)))


def add_item(content: str, *, conversation_id: str = "", source: str = "chat") -> dict:
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


def list_items(status: str | None = "unreviewed", limit: int = 100) -> list[dict]:
    path = _path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items", [])
    if status:
        items = [item for item in items if item.get("status") == status]
    return list(reversed(items[-limit:]))
