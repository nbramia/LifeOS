"""Tests for mirrored-transcript resolution on the events/stream/summary
routes: `_read_cli_transcript_events` (the shared local-then-mirrored
lookup) and its use by `GET /sessions/{id}/events`, `/stream`, and
`/summary`.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services import agent_transcript_mirror
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


def _iso(offset: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_cc_transcript(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": _iso(-10), "message": {"role": "user", "content": "mirrored hello"}},
        {
            "type": "assistant", "timestamp": _iso(-5),
            "message": {
                "role": "assistant", "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "mirrored reply"}],
                "usage": {"input_tokens": 10, "output_tokens": 20,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    agents_route._label_cache.clear()
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "_codex_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    yield session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def mirror_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "mirror-root"
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: root)
    return root


@pytest.fixture
def client():
    return TestClient(api_main.app)


# ---------------------------------------------------------------------------
# The shared helper directly
# ---------------------------------------------------------------------------


def test_read_cli_transcript_events_falls_back_to_mirrored_dir(mirror_root):
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-e1.jsonl")

    events = agents_route._read_cli_transcript_events("cc:sess-e1")

    assert len(events) == 2
    assert events[0]["kind"] == "user_message"
    assert events[0]["payload"]["text"] == "mirrored hello"
    assert events[1]["kind"] == "assistant_message"


def test_read_cli_transcript_events_local_wins_over_mirrored(mirror_root, tmp_path, monkeypatch):
    """A local transcript exists for the same id — the mirrored copy must
    never be consulted (local-first)."""
    from config.settings import settings

    local_dir = tmp_path / "local-claude-projects"
    _write_cc_transcript(local_dir / "-home-user-proj" / "sess-e2.jsonl")
    # Overwrite the local copy's user text so it's distinguishable.
    local_file = local_dir / "-home-user-proj" / "sess-e2.jsonl"
    lines = local_file.read_text().splitlines()
    lines[0] = json.dumps({"type": "user", "timestamp": _iso(-10),
                            "message": {"role": "user", "content": "LOCAL hello"}})
    local_file.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(local_dir), raising=False)

    # A DIFFERENT mirrored copy for the same id, distinguishable by text.
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-e2.jsonl")

    events = agents_route._read_cli_transcript_events("cc:sess-e2")
    assert events[0]["payload"]["text"] == "LOCAL hello"


def test_read_cli_transcript_events_returns_empty_when_nowhere_found(mirror_root):
    assert agents_route._read_cli_transcript_events("cc:does-not-exist") == []


# ---------------------------------------------------------------------------
# _lookup_cc_session_meta / _lookup_cx_session_meta mirrored fallback
# (used by /focus's FD-probe resolution and by _resume_command_text's cwd
# lookup for a "resume here" target on a session with no local transcript
# and no cli_sessions row).
# ---------------------------------------------------------------------------


def test_lookup_cc_session_meta_finds_mirrored_session(mirror_root):
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-lookup-1.jsonl")

    meta, bare = agents_route._lookup_cc_session_meta("cc:sess-lookup-1")

    assert bare == "sess-lookup-1"
    assert meta.decoded_cwd == "/home/user/proj"
    assert meta.jsonl_path.endswith("sess-lookup-1.jsonl")


def test_lookup_cx_session_meta_finds_mirrored_session(mirror_root):
    cx_dir = mirror_root / "studio" / "codex" / "2026" / "08" / "01"
    cx_dir.mkdir(parents=True)
    rollout = cx_dir / "rollout-2026-08-01T00-00-00-cx-lookup-1.jsonl"
    lines = [{"type": "session_meta", "timestamp": _iso(-10), "payload": {"cwd": "/home/user/proj3"}}]
    with rollout.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    meta = agents_route._lookup_cx_session_meta("cx:cx-lookup-1")

    assert meta.decoded_cwd == "/home/user/proj3"


def test_read_cli_transcript_events_codex_mirrored(mirror_root):
    cx_dir = mirror_root / "studio" / "codex" / "2026" / "08" / "01"
    cx_dir.mkdir(parents=True)
    rollout = cx_dir / "rollout-2026-08-01T00-00-00-cx-e3.jsonl"
    lines = [
        {"type": "event_msg", "timestamp": _iso(-5), "payload": {"type": "user_message", "message": "codex hi"}},
    ]
    with rollout.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    events = agents_route._read_cli_transcript_events("cx:cx-e3")
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "codex hi"


# ---------------------------------------------------------------------------
# GET /sessions/{id}/events
# ---------------------------------------------------------------------------


def test_events_endpoint_returns_mirrored_transcript_for_remote_session(client, stores, mirror_root):
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-e4.jsonl")

    r = client.get("/api/agents/sessions/cc:sess-e4/events")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["events"][0]["payload"]["text"] == "mirrored hello"


# ---------------------------------------------------------------------------
# GET /sessions/{id}/stream
# ---------------------------------------------------------------------------


def test_stream_endpoint_returns_mirrored_transcript_for_remote_session(client, stores, mirror_root):
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-e5.jsonl")

    lines: list[str] = []
    with client.stream("GET", "/api/agents/sessions/cc:sess-e5/stream?backfill=5") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            lines.append(line)
            if "mirrored reply" in line:
                break  # got the assistant backfill event — no need to wait for idle-close

    joined = "\n".join(lines)
    assert "mirrored hello" in joined
    assert "mirrored reply" in joined


# ---------------------------------------------------------------------------
# GET /sessions/{id}/summary — produces a real label for a mirrored session
# ---------------------------------------------------------------------------


def test_summary_endpoint_resolves_label_for_mirrored_session(client, stores, mirror_root, monkeypatch):
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-e6.jsonl")

    captured = {}

    async def fake_summarize(session_id, *, label, last_activity_at, events, status):
        captured["label"] = label
        captured["last_activity_at"] = last_activity_at
        captured["status"] = status
        captured["n_events"] = len(events)

        class _Result:
            def as_dict(self_inner):
                return {"short_label": "mirrored summary", "body": "..."}
        return _Result()

    monkeypatch.setattr(agents_route.agent_viz_summary, "summarize_session", fake_summarize)

    r = client.get("/api/agents/sessions/cc:sess-e6/summary")
    assert r.status_code == 200
    assert r.json()["short_label"] == "mirrored summary"
    # The label passed to summarize_session came from the MIRRORED snapshot
    # row (not the bare session_id fallback), proving the mirrored-snapshot
    # lookup fired.
    assert captured["label"] != "cc:sess-e6"
    assert captured["n_events"] == 2
