"""Durable Life Inbox for messages that still need interpretation."""

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_INBOX_PATH = Path.home() / ".lifeos" / "inbox.json"
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("LIFEOS_INBOX_PATH", str(DEFAULT_INBOX_PATH)))


def _read(path: Path) -> dict:
    if not path.exists():
        return {"items": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"Invalid Life Inbox structure in {path}")
    return data


def _write(path: Path, data: dict) -> None:
    """Atomically replace the Inbox so an interrupted write cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="inbox-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def add_item(content: str, *, conversation_id: str = "", source: str | dict = "chat") -> dict:
    """Append a raw capture before model interpretation can lose it."""
    path = _path()
    with _LOCK:
        data = _read(path)
        # Telegram and webhook transports may retry delivery. A stable source
        # identity is stronger than content equality: the same text in two
        # different messages is still two captures, while one message should
        # never become duplicate Inbox records.
        if isinstance(source, dict) and source.get("type") and source.get("message_id") is not None:
            for existing in data.get("items", []):
                existing_source = existing.get("source")
                if (
                    isinstance(existing_source, dict)
                    and existing_source.get("type") == source.get("type")
                    and existing_source.get("chat_id") == source.get("chat_id")
                    and existing_source.get("message_id") == source.get("message_id")
                ):
                    return existing
        item = {
            "id": str(uuid.uuid4()),
            "content": content,
            "conversation_id": conversation_id,
            "source": source,
            "status": "unreviewed",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data.setdefault("items", []).append(item)
        _write(path, data)
    return item


def list_items(
    status: str | None = "unreviewed",
    limit: int = 100,
    since_days: int | None = None,
) -> list[dict]:
    path = _path()
    # Missing is a genuinely empty Inbox; unreadable or malformed is not.
    # Let those failures reach the tool boundary so a review cannot report a
    # confident "nothing unresolved" after silently discarding its evidence.
    data = _read(path)
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
    person_fact_id: str = "",
) -> dict | None:
    """Mark an inbox item as reviewed while retaining its raw content."""
    path = _path()
    with _LOCK:
        try:
            data = _read(path)
        except (OSError, ValueError, json.JSONDecodeError):
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
                if person_fact_id:
                    item["person_fact_id"] = person_fact_id
                item["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                _write(path, data)
                return item
    return None
