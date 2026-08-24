"""Portable structured records for the operator's evolving life model.

This is intentionally separate from LLM providers and from the semantic memory
index.  The records are evidence-backed anchors that help the assistant
understand direction without turning every conversation into a task or a
hallucinated profile.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


SECTIONS = ("identity", "values", "current_state", "ideal_state", "philosophy")
_LOCK = RLock()


def _path() -> Path:
    return Path(os.environ.get("LIFEOS_LIFE_MODEL_PATH", "~/.lifeos/life_model.json")).expanduser()


def _empty() -> dict:
    return {section: [] for section in SECTIONS}


def _read() -> dict:
    path = _path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = _empty()
    if not isinstance(data, dict):
        data = _empty()
    for section in SECTIONS:
        if not isinstance(data.get(section), list):
            data[section] = []
    return data


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="life-model-", suffix=".json", dir=path.parent)
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


def _normalise(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def record(section: str, content: str, *, source: dict | None = None, evidence_type: str = "explicit") -> dict:
    section = str(section or "").strip().lower()
    content = " ".join(str(content or "").split()).strip()
    evidence_type = str(evidence_type or "explicit").strip().lower()
    if section not in SECTIONS:
        raise ValueError(f"section must be one of: {', '.join(SECTIONS)}")
    if not content:
        raise ValueError("content is required")
    if evidence_type not in {"explicit", "inference"}:
        raise ValueError("evidence_type must be explicit or inference")
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        data = _read()
        for item in data[section]:
            if _normalise(item.get("content")) == _normalise(content):
                item["updated_at"] = now
                if source:
                    item["sources"] = [*item.get("sources", []), source]
                item["evidence_type"] = evidence_type
                _write(data)
                return item
        item = {
            "id": str(uuid.uuid4()),
            "content": content,
            "evidence_type": evidence_type,
            "created_at": now,
            "updated_at": now,
            "sources": [source] if source else [],
        }
        data[section].append(item)
        _write(data)
        return item


def list_records(section: str | None = None) -> dict | list:
    with _LOCK:
        data = _read()
    if section:
        if section not in SECTIONS:
            raise ValueError(f"section must be one of: {', '.join(SECTIONS)}")
        return data[section]
    return data


def update_source(record_id: str, source: dict) -> bool:
    """Attach transport provenance to a record created during a chat turn."""
    if not record_id or not isinstance(source, dict) or not source:
        return False
    with _LOCK:
        data = _read()
        for rows in data.values():
            if not isinstance(rows, list):
                continue
            for item in rows:
                if item.get("id") != record_id:
                    continue
                sources = item.setdefault("sources", [])
                if source not in sources:
                    sources.append(source)
                    _write(data)
                return True
    return False


def clear() -> None:
    """Test helper; production callers should never need to delete the model."""
    with _LOCK:
        try:
            _path().unlink()
        except FileNotFoundError:
            pass
