"""Snapshot-union tests for cross-machine CLI sessions (issue #849).

`GET /api/agents/snapshot` merges the `cli_sessions` table (posted by
scripts/lifeos-agent-hook.sh, from any machine) with the local transcript
scan. A session known from both sources collapses into one row with
event-driven status; a session known only from `cli_sessions` (a remote
host, or a hook post that raced ahead of the transcript cache) becomes a
synthetic row. Every row, including LifeOS worker sessions, carries `host`.

Fakes the transcript scan via `_claude_code_snapshot` rather than writing
real ~/.claude/projects/ jsonl fixtures — the union logic under test only
cares about the dict shape `to_session_dict` produces, not how it's parsed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


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
def client():
    return TestClient(api_main.app)


def _fake_cc_transcript_row(session_id="cc:merge-target", **overrides):
    row = {
        "session_id": session_id,
        # (#863 review) `to_session_dict` always sets `task_id: None` for a
        # locally scanned session now — a real LifeOS task link only ever
        # arrives via `_apply_cli_session_to_dict`'s overlay below. This
        # fixture used to build the pre-#863 shape (the raw session id
        # leaking into `task_id`), which no production code emits any more.
        "task_id": None,
        "status": "inactive",
        "routing": "claude_code",
        "parent_session_id": None,
        "root_session_id": session_id,
        "spawn_depth": 0,
        "yield_waiting_for": [],
        "managed_agent_session_id": None,
        "started_at": 1000,
        "last_activity_at": 2000,
        "total_input_tokens": 111,
        "total_output_tokens": 222,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_dollars": 0.05,
        "total_active_seconds": 0.0,
        "expected_output": None,
        "label": "merge-target",
        "model_label": "Sonnet",
        "last_event_kind": "assistant",
        "tool_call_count": 1,
        "error_count": 0,
        "source": "claude_code",
        "status_inferred": True,
        "project_key": "-home-x",
        "decoded_cwd": "/home/x",
    }
    row.update(overrides)
    return row


@pytest.mark.unit
def test_merged_row_is_single_with_event_status_and_transcript_tokens(client, stores, monkeypatch):
    session_store, _ = stores
    monkeypatch.setattr(agents_route, "_claude_code_snapshot",
                        lambda: ([_fake_cc_transcript_row()], []))

    session_store.record_cli_session_event(
        engine="claude_code", event="user_prompt_submit",
        session_id="merge-target", host="this-api-host",
        cwd="/home/x", prompt="do the thing",
    )

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    matches = [s for s in sessions if s["session_id"] == "cc:merge-target"]
    assert len(matches) == 1
    row = matches[0]
    # Event-driven status wins over the transcript's inferred "inactive".
    assert row["status"] == "running"
    assert row["status_inferred"] is False
    # Token/dollar fields stay transcript-derived — the hook posts none.
    assert row["total_input_tokens"] == 111
    assert row["total_output_tokens"] == 222
    assert row["total_dollars"] == 0.05
    assert row["host"] == "this-api-host"
    assert row["prompt_preview"] == "do the thing"


@pytest.mark.unit
def test_remote_row_with_no_local_transcript(client, stores):
    session_store, _ = stores
    session_store.record_cli_session_event(
        engine="codex", event="session_start",
        session_id="remote-only", host="a-different-laptop",
        cwd="/home/laptop/proj", branch="feat/x",
    )

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    matches = [s for s in sessions if s["session_id"] == "cx:remote-only"]
    assert len(matches) == 1
    row = matches[0]
    assert row["host"] == "a-different-laptop"
    assert row["source"] == "codex"
    assert row["status_inferred"] is False
    assert row["status"] == "idle"
    assert row["branch"] == "feat/x"


@pytest.mark.unit
def test_remote_row_label_prefers_prompt_preview(client, stores):
    """#863: a remote CLI row's `label` used to always be the session id —
    with no local transcript there was no human label at all, even though
    `prompt_preview` was right there on the same row."""
    session_store, _ = stores
    session_store.record_cli_session_event(
        engine="codex", event="user_prompt_submit",
        session_id="labelled-remote", host="a-different-laptop",
        cwd="/home/laptop/proj", prompt="refactor the synthetic widget",
    )

    r = client.get("/api/agents/snapshot")
    sessions = r.json()["sessions"]
    row = next(s for s in sessions if s["session_id"] == "cx:labelled-remote")
    assert row["label"] == "refactor the synthetic widget"


@pytest.mark.unit
def test_remote_row_label_falls_back_to_session_id_without_prompt_preview(client, stores):
    session_store, _ = stores
    session_store.record_cli_session_event(
        engine="codex", event="session_start",
        session_id="unlabelled-remote", host="a-different-laptop",
        cwd="/home/laptop/proj",
    )

    r = client.get("/api/agents/snapshot")
    sessions = r.json()["sessions"]
    row = next(s for s in sessions if s["session_id"] == "cx:unlabelled-remote")
    assert row["label"] == "cx:unlabelled-remote"


@pytest.mark.unit
def test_cli_session_to_dict_unknown_engine_uses_engine_name_not_claude_tier():
    """#863: an unrecognized `engine` on a `cli_sessions` row (defensive
    only — the route only ever writes claude_code/codex) used to hardcode
    `model_label = "Claude"`, a misleading Claude-tier guess."""
    from api.services.agent_worker.session_store import CliSession
    from api.routes.agents import _cli_session_to_dict

    cli = CliSession(
        session_id="xy:synthetic", engine="some_new_engine", host="a-laptop",
        status="idle", started_at=1000, last_event_at=1000,
    )
    d = _cli_session_to_dict(cli)
    assert d["model_label"] == "Some New Engine"


@pytest.mark.unit
def test_task_id_exposed_on_merged_and_remote_rows(client, stores, monkeypatch):
    session_store, _ = stores
    monkeypatch.setattr(agents_route, "_claude_code_snapshot",
                        lambda: ([_fake_cc_transcript_row(session_id="cc:with-task")], []))
    session_store.record_cli_session_event(
        engine="claude_code", event="session_start",
        session_id="with-task", host="this-api-host", task_id="task-99",
    )
    session_store.record_cli_session_event(
        engine="codex", event="session_start",
        session_id="remote-task", host="other-host", task_id="task-100",
    )

    r = client.get("/api/agents/snapshot")
    sessions = {s["session_id"]: s for s in r.json()["sessions"]}
    assert sessions["cc:with-task"]["task_id"] == "task-99"
    assert sessions["cx:remote-task"]["task_id"] == "task-100"


@pytest.mark.unit
def test_worker_and_all_rows_carry_host(client, stores):
    session_store, _ = stores
    session_store.create(task_id="t1", session_id="sess_worker1")
    session_store.record_cli_session_event(
        engine="claude_code", event="session_start",
        session_id="hostcheck", host="a-different-laptop",
    )

    r = client.get("/api/agents/snapshot")
    sessions = r.json()["sessions"]
    assert len(sessions) >= 2
    for s in sessions:
        assert s.get("host"), f"row {s['session_id']} missing host"
    worker_row = next(s for s in sessions if s["session_id"] == "sess_worker1")
    assert worker_row["host"] == "this-api-host"


@pytest.mark.unit
def test_focus_409_for_session_on_different_host(client, stores):
    """#849: focus isn't implemented for remote hosts yet — it should 409
    with the host name rather than trying (and failing) a local wezterm
    probe for a pane that was never on this machine."""
    session_store, _ = stores
    session_store.record_cli_session_event(
        engine="claude_code", event="session_start",
        session_id="remote-focus", host="a-different-laptop",
    )
    r = client.post("/api/agents/sessions/cc:remote-focus/focus")
    assert r.status_code == 409
    assert "a-different-laptop" in r.json()["detail"]


@pytest.mark.unit
def test_resume_409_for_session_on_different_host(client, stores):
    session_store, _ = stores
    session_store.record_cli_session_event(
        engine="codex", event="session_start",
        session_id="remote-resume", host="a-different-laptop",
    )
    r = client.post("/api/agents/sessions/cx:remote-resume/resume")
    assert r.status_code == 409
    assert "a-different-laptop" in r.json()["detail"]


@pytest.mark.unit
def test_focus_local_host_unaffected_when_no_cli_session_row(client, stores, monkeypatch):
    """A session_id with no `cli_sessions` row (never registered via the
    hook) must fall through to the existing local-only resolution — the
    409 check should not fire just because the id is unknown."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = client.post("/api/agents/sessions/cc:never-registered/focus")
    # No wezterm mapping and no cli_sessions row -> falls through to the
    # probe, which finds nothing -> 404, not 409.
    assert r.status_code == 404


@pytest.mark.unit
def test_local_transcript_row_without_cli_event_still_gets_local_host(client, stores, monkeypatch):
    """A transcript-only row (no cli_sessions post yet) still needs a host —
    it ran here, so it gets the API host directly rather than being left
    without one."""
    monkeypatch.setattr(agents_route, "_claude_code_snapshot",
                        lambda: ([_fake_cc_transcript_row(session_id="cc:no-event-yet")], []))
    r = client.get("/api/agents/snapshot")
    sessions = {s["session_id"]: s for s in r.json()["sessions"]}
    assert sessions["cc:no-event-yet"]["host"] == "this-api-host"
    # Unmerged rows keep the transcript's own inferred status.
    assert sessions["cc:no-event-yet"]["status_inferred"] is True
