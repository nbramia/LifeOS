"""Portable local store for relationship commitments and promises."""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PATH = Path.home() / ".lifeos" / "commitments.json"
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("LIFEOS_COMMITMENTS_PATH", str(DEFAULT_PATH)))


def _read() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"commitments": []}


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def create_commitment(
    content: str,
    *,
    direction: str,
    person_name: str = "",
    person_id: str = "",
    due_at: str = "",
    source: dict | str | None = None,
) -> dict:
    direction = direction.strip().lower()
    if direction not in {"owed_by_me", "owed_to_me"}:
        raise ValueError("direction must be owed_by_me or owed_to_me")
    item = {
        "id": str(uuid.uuid4()),
        "content": content.strip(),
        "direction": direction,
        "person_name": person_name.strip(),
        "person_id": person_id.strip(),
        "due_at": due_at.strip(),
        "status": "open",
        "source": source or {"type": "conversation"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _LOCK:
        data = _read()
        # Exact source identity makes retries idempotent without collapsing two
        # separate promises that happen to use the same wording.
        source = item["source"]
        if isinstance(source, dict) and source.get("message_id") is not None:
            for existing in data.get("commitments", []):
                old = existing.get("source")
                if (
                    isinstance(old, dict)
                    and old.get("type") == source.get("type")
                    and old.get("chat_id") == source.get("chat_id")
                    and old.get("message_id") == source.get("message_id")
                ):
                    return existing
        data.setdefault("commitments", []).append(item)
        _write(data)
    return item


def list_commitments(
    *, person_name: str = "", direction: str = "", status: str = "open", limit: int = 50
) -> list[dict]:
    items = _read().get("commitments", [])
    person_name = person_name.strip().lower()
    direction = direction.strip().lower()
    if person_name:
        items = [i for i in items if person_name in i.get("person_name", "").lower()]
    if direction:
        items = [i for i in items if i.get("direction") == direction]
    if status:
        items = [i for i in items if i.get("status") == status]
    return list(reversed(items[-max(1, min(limit, 500)):]))


def complete_commitment(commitment_id: str) -> dict | None:
    with _LOCK:
        data = _read()
        for item in data.get("commitments", []):
            if item.get("id") == commitment_id:
                item["status"] = "completed"
                item["completed_at"] = datetime.now(timezone.utc).isoformat()
                _write(data)
                return item
    return None


def update_source(commitment_id: str, source: dict) -> dict | None:
    with _LOCK:
        data = _read()
        for item in data.get("commitments", []):
            if item.get("id") == commitment_id:
                item["source"] = source
                _write(data)
                return item
    return None
