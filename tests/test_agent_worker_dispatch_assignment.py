"""Worker `_dispatch()` wiring for card assignment (#851): a task's
`fields` (model/effort/host) are extracted and recorded on the session row
before the executor is invoked, and the new `#hermes` tag routes through
`HermesExecutor`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import STATUS_COMPLETED, SessionStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker, _SynchronousPool
from api.services.conversation_store import ConversationStore


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _redirect_agent_output(tmp_path, monkeypatch):
    """Write completed-task output to a throwaway vault for these tests."""
    from config.settings import settings as _settings

    monkeypatch.setattr(_settings, "vault_path", tmp_path / "vault", raising=False)


@dataclass
class _StubExecutor:
    outcome: ExecutorOutcome
    calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.calls.append((session.task_id, session.host, session.model, session.effort))
        return self.outcome


@dataclass
class _TaskCaptureExecutor:
    outcome: ExecutorOutcome
    tasks: list = field(default_factory=list)

    def execute(self, session, task):
        self.tasks.append(task)
        return self.outcome


def _golden_preflight_reply(routing: str = "local", routing_reason: str = "guess") -> str:
    return json.dumps({
        "budget": {"wall_seconds": 3600, "max_tokens": 100000, "max_dollars": 1.0},
        "routing": routing,
        "routing_reason": routing_reason,
        "routing_explicit": False,
        "expected_output": "text",
        "ambiguity": None,
        "sane": True,
        "sane_reason": "",
    })


def _make_worker(
    tmp_path: Path,
    *,
    backing_task: dict | None = None,
    claude_code_executor=None,
    hermes_executor=None,
):
    def handler(req: httpx.Request) -> httpx.Response:
        if (
            backing_task is not None
            and req.method == "GET"
            and req.url.path == f"/api/tasks/{backing_task['id']}"
        ):
            return httpx.Response(200, json=backing_task)
        return httpx.Response(200, json={"tasks": []})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    return Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [1],
        http_client=client,
        preflight_caller=lambda prompt: _golden_preflight_reply(),
        claude_code_executor=claude_code_executor,
        hermes_executor=hermes_executor,
        cli_pool=_SynchronousPool(),
    )


def test_dispatch_records_assignment_fields_on_session_before_cli_executor_runs(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    task = {
        "id": "board-1",
        "description": "fix the printer",
        "tags": ["agent", "claude"],
        "fields": {"model": "opus", "effort": "high", "assigned_by": "board"},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, claude_code_executor=stub)
    worker.session_store.create(task_id="board-1", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("board-1")
    assert session.host is None
    assert session.model == "opus"
    assert session.effort == "high"
    assert stub.calls == [("board-1", None, "opus", "high")]


def test_dispatch_records_host_field_on_session(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    task = {
        "id": "board-2",
        "description": "deploy the thing",
        "tags": ["agent", "claude"],
        "fields": {"host": "studio"},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, claude_code_executor=stub)
    worker.session_store.create(task_id="board-2", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("board-2")
    assert session.host == "studio"
    assert stub.calls[-1] == ("board-2", "studio", None, None)


def test_dispatch_untagged_task_has_no_assignment(tmp_path, monkeypatch):
    """A task with no fields at all records NULL host/model/effort — the
    #851 write is a no-op for every pre-existing task shape."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    worker = _make_worker(tmp_path, claude_code_executor=stub)
    worker.session_store.create(task_id="plain-1", status="claimed")

    task = {"id": "plain-1", "description": "reindex the vault", "tags": ["agent", "claude"]}
    worker._dispatch(task)

    session = worker.session_store.get("plain-1")
    assert session.host is None
    assert session.model is None
    assert session.effort is None


def test_reassignment_context_reaches_fresh_local_executor_prompt(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    capture = _TaskCaptureExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    backing_task = {
        "id": "reassigned-1", "description": "Continue synthetic task",
        "notes": "Latest synthetic direction", "tags": ["local", "agent-running"],
    }
    worker = _make_worker(tmp_path, backing_task=backing_task)
    worker._local_executor = capture
    session = worker.session_store.create(task_id="reassigned-1", status="claimed", routing="local")
    worker.transcript_store.append(session.session_id, "operator_reassigned", {
        "assignee": "local", "prior_routing": "cloud", "context_preserved": True,
    })
    worker.session_store.append_message(session.session_id, "assistant", "Prior synthetic output")

    worker._dispatch({
        "id": "reassigned-1", "description": "Continue synthetic task",
        "notes": "Latest synthetic direction", "tags": ["local", "agent-reassigned"],
    })

    assert len(capture.tasks) == 1
    notes = capture.tasks[0]["notes"]
    assert "Latest synthetic direction" in notes
    assert "Prior synthetic output" in notes


def test_hermes_tag_dispatches_through_hermes_executor(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="hi there"))
    task = {
        "id": "hermes-1",
        "description": "ask hermes what's on my calendar",
        "tags": ["agent", "hermes"],
        "fields": {},
    }
    backing_task = {**task, "tags": ["agent-running", "hermes"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, hermes_executor=stub)
    worker.session_store.create(task_id="hermes-1", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("hermes-1")
    assert session.routing == "hermes"
    assert len(stub.calls) == 1
    assert stub.calls[0][0] == "hermes-1"
