"""Append-only JSONL transcript store for agent sessions.

Each session has one file at `data/agent_transcripts/{session_id}.jsonl`.
Events from both the local executor (Issue C) and Managed Agents (Issue D)
land here in the same shape so inter-agent tools (Issue E) can read across
transports without branching.

Issue B writes only a minimal subset of events (`claim`, `noop_complete`).
Later issues append `llm_turn`, `tool_call`, `tool_result`, `error`, etc.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable


DEFAULT_TRANSCRIPTS_DIR = Path("data/agent_transcripts")


class TranscriptStore:
    def __init__(self, transcripts_dir: Path | str = DEFAULT_TRANSCRIPTS_DIR):
        self.dir = Path(transcripts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        # Defense in depth: refuse path-traversal-ish session ids. Reject all
        # separators (Unix `/`, Windows `\`), `..` segments, and absolute paths.
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            raise ValueError(f"invalid session_id: {session_id!r}")
        return self.dir / f"{session_id}.jsonl"

    def append(self, session_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        """Append one event. `kind` is a short label (e.g. "claim", "tool_call")."""
        event = {
            "ts": time.time(),
            "kind": kind,
            "payload": payload or {},
        }
        path = self._path(session_id)
        # Open in append mode each call so multiple processes / restarts work.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read(self, session_id: str) -> list[dict[str, Any]]:
        path = self._path(session_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # Skip malformed lines rather than crash the reader.
                    continue
        return events

    def iter_events(self, session_id: str) -> Iterable[dict[str, Any]]:
        """Generator variant for transcripts too large to materialize."""
        path = self._path(session_id)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
