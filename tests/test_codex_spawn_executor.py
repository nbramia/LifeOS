"""Tests for codex_spawn + CodexExecutor."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from api.services.agent_worker import codex_spawn
from api.services.agent_worker.codex_executor import (
    CodexExecutor,
    REASON_BINARY_NOT_FOUND,
)
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(tmp_path: Path):
    """Isolated session/transcript stores backed by tmp_path."""
    session_db = tmp_path / "sessions.db"
    transcripts_dir = tmp_path / "transcripts"
    return SessionStore(db_path=str(session_db)), TranscriptStore(transcripts_dir=transcripts_dir)


@pytest.mark.unit
def test_spawn_codex_session_creates_row(stores):
    sess_store, _ = stores
    result = codex_spawn.spawn_codex_session(
        sess_store, "do a thing", working_dir="/tmp", chat_id="42",
    )
    assert result["ok"] is True
    session = sess_store.get_by_session_id(result["session_id"])
    assert session.routing == "codex"
    assert session.origin == "operator"
    assert session.parent_session_id is None
    # Pending message holds the JSON payload.
    msgs = sess_store.drain_pending_messages(session.session_id)
    assert len(msgs) == 1
    payload = json.loads(msgs[0]["content"])
    assert payload["prompt"] == "do a thing"
    assert payload["working_dir"] == "/tmp"
    assert payload["chat_id"] == "42"


@pytest.mark.unit
def test_spawn_codex_session_rejects_empty(stores):
    sess_store, _ = stores
    result = codex_spawn.spawn_codex_session(sess_store, "")
    assert result["ok"] is False
    assert "required" in result["error"]


@pytest.mark.unit
def test_parse_codex_spawn_payload_round_trips():
    encoded = json.dumps({"prompt": "p", "working_dir": "/w", "chat_id": "c"})
    decoded = codex_spawn.parse_codex_spawn_payload(encoded)
    assert decoded == {"prompt": "p", "working_dir": "/w", "chat_id": "c"}


@pytest.mark.unit
def test_parse_codex_spawn_payload_falls_back_to_string():
    decoded = codex_spawn.parse_codex_spawn_payload("just a string")
    assert decoded == {"prompt": "just a string", "working_dir": None, "chat_id": None}


# ---------------------------------------------------------------------------
# executor — fake spawn that returns a canned JSONL stream
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal subprocess.Popen substitute for stream-parsing tests."""
    def __init__(self, lines: list[dict], returncode: int = 0, stderr_text: str = ""):
        self.stdout = io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n")
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.pid = 12345

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.mark.unit
def test_executor_handles_full_stream(stores, monkeypatch, tmp_path):
    """End-to-end stream parse → session marked completed, final_text captured."""
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t1",
        session_id="sess_codex_1",
        status="claimed",
        routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text",
        origin="operator",
    )

    lines = [
        {"type": "thread.started", "thread_id": "thread-abc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello world"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100, "cached_input_tokens": 0,
                "output_tokens": 10, "reasoning_output_tokens": 0,
            },
        },
    ]

    fake_proc = _FakeProc(lines, returncode=0)

    def fake_spawn(cmd, **kwargs):
        # Write the "final message" file the executor expects.
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("Hello world\n")
        return fake_proc

    notifications: list[str] = []
    executor = CodexExecutor(
        session_store=sess_store,
        transcript_store=tr_store,
        notification_callback=notifications.append,
        spawn_fn=fake_spawn,
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,  # don't fire in tests
    )

    outcome = executor.execute(session, {"description": "say hi", "working_dir": str(tmp_path)})
    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "Hello world"
    assert notifications == ["Hello world"]
    # Thread id persisted for future resume.
    reloaded = sess_store.get_by_session_id(session.session_id)
    assert reloaded.claude_code_session_id == "thread-abc"


@pytest.mark.unit
def test_executor_binary_not_found(stores, tmp_path):
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t2",
        session_id="sess_codex_2",
        status="claimed",
        routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text",
        origin="operator",
    )

    def fail_spawn(*args, **kwargs):
        raise FileNotFoundError("nope")

    executor = CodexExecutor(
        session_store=sess_store,
        transcript_store=tr_store,
        spawn_fn=fail_spawn,
        binary_resolver=lambda: "/nonexistent/codex",
        heartbeat_interval=9999,
    )
    outcome = executor.execute(session, {"description": "x", "working_dir": str(tmp_path)})
    assert outcome.status == STATUS_FAILED
    assert outcome.reason == REASON_BINARY_NOT_FOUND


@pytest.mark.unit
def test_executor_rejects_empty_prompt(stores, tmp_path):
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t3",
        session_id="sess_codex_3",
        status="claimed",
        routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text",
        origin="operator",
    )
    executor = CodexExecutor(
        session_store=sess_store,
        transcript_store=tr_store,
        spawn_fn=lambda *a, **k: _FakeProc([]),
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )
    outcome = executor.execute(session, {"description": "  "})
    assert outcome.status == STATUS_FAILED
    assert outcome.reason == "empty prompt"
