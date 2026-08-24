"""Portable, provider-independent project state.

Memories remain the raw evidence stream. This store is the small current-state
projection that makes a project actionable without asking an LLM to reconstruct
its state from an undifferentiated memory list every time.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


_LOCK = RLock()


def _path() -> Path:
    return Path(os.environ.get("LIFEOS_PROJECTS_PATH", "~/.lifeos/projects.json")).expanduser()


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"projects": []}
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        data = {"projects": []}
    return data


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="projects-", suffix=".json", dir=path.parent)
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


def upsert(
    name: str,
    *,
    status: str = "active",
    summary: str = "",
    next_action: str = "",
    priority: str = "",
    source: dict | None = None,
    evidence_type: str = "explicit",
) -> dict:
    name = " ".join(str(name or "").split()).strip()
    if not name:
        raise ValueError("name is required")
    status = str(status or "active").strip().lower()
    if status not in {"potential", "active", "paused", "completed", "archived"}:
        raise ValueError("status must be potential, active, paused, completed, or archived")
    evidence_type = str(evidence_type or "explicit").strip().lower()
    if evidence_type not in {"explicit", "inference"}:
        raise ValueError("evidence_type must be explicit or inference")
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        data = _read()
        item = next((p for p in data["projects"] if _normalise(p.get("name")) == _normalise(name)), None)
        if item is None:
            item = {
                "id": str(uuid.uuid4()),
                "name": name,
                "status": status,
                "summary": summary.strip(),
                "next_action": next_action.strip(),
                "priority": priority.strip(),
                "evidence_type": evidence_type,
                "created_at": now,
                "updated_at": now,
                "sources": [source] if source else [],
                "history": [],
            }
            data["projects"].append(item)
        else:
            previous = {
                key: item.get(key, "")
                for key in ("status", "summary", "next_action", "priority", "evidence_type")
            }
            changed = any(
                value not in (None, "") and value != previous[key]
                for key, value in {
                    "status": status,
                    "summary": summary.strip(),
                    "next_action": next_action.strip(),
                    "priority": priority.strip(),
                    "evidence_type": evidence_type,
                }.items()
            )
            if changed:
                item.setdefault("history", []).append({"at": now, **previous})
            item.update({
                "status": status,
                "evidence_type": evidence_type,
                "updated_at": now,
            })
            if summary.strip():
                item["summary"] = summary.strip()
            if next_action.strip():
                item["next_action"] = next_action.strip()
            if priority.strip():
                item["priority"] = priority.strip()
            if source and source not in item.setdefault("sources", []):
                item["sources"].append(source)
        _write(data)
        return item


def list_projects(*, include_archived: bool = False, limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = list(_read()["projects"])
    if not include_archived:
        rows = [row for row in rows if row.get("status") != "archived"]
    rows.sort(key=lambda row: row.get("updated_at", ""), reverse=True)
    return rows[: max(1, min(int(limit or 100), 500))]


def get_project(project_id: str = "", name: str = "") -> dict | None:
    wanted_id = str(project_id or "").strip()
    wanted_name = _normalise(name)
    with _LOCK:
        for item in _read()["projects"]:
            if (wanted_id and item.get("id") == wanted_id) or (wanted_name and _normalise(item.get("name")) == wanted_name):
                return item
    return None


def update_source(project_id: str, source: dict) -> bool:
    if not project_id or not isinstance(source, dict) or not source:
        return False
    with _LOCK:
        data = _read()
        for item in data["projects"]:
            if item.get("id") == project_id:
                if source not in item.setdefault("sources", []):
                    item["sources"].append(source)
                    _write(data)
                return True
    return False
