"""Worker `_dispatch()` wiring for card assignment: a task's
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

from api.services.agent_worker.git_worktree import WorktreeResult
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
    codex_executor=None,
    hermes_executor=None,
    remote_executor=None,
    model_catalog_defaults_provider=None,
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
        codex_executor=codex_executor,
        hermes_executor=hermes_executor,
        remote_executor=remote_executor,
        cli_pool=_SynchronousPool(),
        model_catalog_defaults_provider=model_catalog_defaults_provider,
    )


def test_dispatch_records_assignment_fields_on_session_before_cli_executor_runs(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    # This test is about assignment-field wiring, not worktree provisioning
    # (covered by tests/test_agent_worker_git_worktree.py and
    # tests/test_agent_worker_git_completion_dispatch.py) — a local CLI route
    # would otherwise run `ensure_worktree` for real against whatever
    # directory the task title happens to resolve to on the host machine.
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.ensure_worktree",
        lambda working_dir, task_id, title, host=None: WorktreeResult(
            working_dir=working_dir, is_git=False,
        ),
    )

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
    # This test is about the host field reaching the session, not worktree
    # provisioning — a local CLI route would otherwise run `ensure_worktree`
    # for real against whatever directory the task title happens to resolve
    # to, over ssh to the registered (but unreachable in tests) host.
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.ensure_worktree",
        lambda working_dir, task_id, title, host=None: WorktreeResult(
            working_dir=working_dir, is_git=False,
        ),
    )

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
    write is a no-op for every pre-existing task shape."""
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


def test_no_model_pin_falls_back_to_catalog_default(tmp_path, monkeypatch):
    """A claude_code dispatch with no board model field resolves the
    catalog's `claude` default at dispatch time — the default lands on
    `execution_spec.model_id` (and thus `session.model`, kept in sync by
    `set_execution_snapshot`), so `--model` is passed even though the
    card itself named nothing."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.ensure_worktree",
        lambda working_dir, task_id, title, host=None: WorktreeResult(
            working_dir=working_dir, is_git=False,
        ),
    )

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    task = {
        "id": "board-3",
        "description": "fix the printer",
        "tags": ["agent", "claude"],
        "fields": {},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(
        tmp_path, backing_task=backing_task, claude_code_executor=stub,
        model_catalog_defaults_provider=lambda: {"claude": "claude-opus-5", "codex": "gpt-6-sol"},
    )
    worker.session_store.create(task_id="board-3", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("board-3")
    assert session.model == "claude-opus-5"
    assert session.execution_spec["model_id"] == "claude-opus-5"


def test_explicit_model_pin_wins_over_catalog_default(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.ensure_worktree",
        lambda working_dir, task_id, title, host=None: WorktreeResult(
            working_dir=working_dir, is_git=False,
        ),
    )

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    task = {
        "id": "board-4",
        "description": "fix the printer",
        "tags": ["agent", "claude"],
        "fields": {"model": "claude-sonnet-5"},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(
        tmp_path, backing_task=backing_task, claude_code_executor=stub,
        model_catalog_defaults_provider=lambda: {"claude": "claude-opus-5", "codex": "gpt-6-sol"},
    )
    worker.session_store.create(task_id="board-4", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("board-4")
    assert session.model == "claude-sonnet-5"


def test_null_catalog_default_keeps_no_model_flag_behavior(tmp_path, monkeypatch):
    """The catalog has no default for this engine (e.g. an empty live list)
    — dispatch keeps today's behavior: no model pin at all."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.ensure_worktree",
        lambda working_dir, task_id, title, host=None: WorktreeResult(
            working_dir=working_dir, is_git=False,
        ),
    )

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    task = {
        "id": "board-5",
        "description": "fix the printer",
        "tags": ["agent", "claude"],
        "fields": {},
    }
    backing_task = {**task, "tags": ["agent-running", "claude"]}
    worker = _make_worker(
        tmp_path, backing_task=backing_task, claude_code_executor=stub,
        model_catalog_defaults_provider=lambda: {"claude": None, "codex": None},
    )
    worker.session_store.create(task_id="board-5", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("board-5")
    assert session.model is None
    assert session.execution_spec["model_id"] is None


def test_codex_no_model_pin_falls_back_to_catalog_default(tmp_path, monkeypatch):
    """Same catalog-default fallback as the claude_code case above, for the
    codex executor."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    monkeypatch.setattr(
        "api.services.agent_worker.git_worktree.ensure_worktree",
        lambda working_dir, task_id, title, host=None: WorktreeResult(
            working_dir=working_dir, is_git=False,
        ),
    )

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    task = {
        "id": "board-6",
        "description": "fix the printer",
        "tags": ["agent", "codex"],
        "fields": {},
    }
    backing_task = {**task, "tags": ["agent-running", "codex"]}
    worker = _make_worker(
        tmp_path, backing_task=backing_task, codex_executor=stub,
        model_catalog_defaults_provider=lambda: {"claude": "claude-opus-5", "codex": "gpt-6-sol"},
    )
    worker.session_store.create(task_id="board-6", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("board-6")
    assert session.model == "gpt-6-sol"
    assert session.execution_spec["model_id"] == "gpt-6-sol"


def test_cloud_card_model_field_reaches_remote_executor(tmp_path, monkeypatch):
    """A `#cloud` card whose board `model` field names one of
    LIFEOS_REMOTE_LLM_MODEL_OPTIONS reaches the remote client with that
    model: board field -> Assignment.model -> execution_spec.model_id ->
    the session the remote executor's `execute()` receives — the same
    `session.model` the LocalExecutor(is_remote=True) reads at call time
    (see `local_executor._call_llm`'s `session.execution_spec["model_id"]`
    pin, proven at the LLM-client layer by
    test_agent_worker_local_executor_thinking.py's
    test_execution_snapshot_model_pin_reaches_native_client)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://example.test/v1", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "accounts/fireworks/models/deepseek-v4-flash", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "fw_test", raising=False)

    stub = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    task = {
        "id": "board-7",
        "description": "summarize the report",
        "tags": ["agent", "cloud"],
        "fields": {"model": "accounts/fireworks/models/qwen3-a22b"},
    }
    backing_task = {**task, "tags": ["agent-running", "cloud"]}
    worker = _make_worker(tmp_path, backing_task=backing_task, remote_executor=stub)
    worker.session_store.create(task_id="board-7", status="claimed")

    worker._dispatch(task)

    session = worker.session_store.get("board-7")
    assert session.routing == "remote"
    assert session.execution_spec["model_id"] == "accounts/fireworks/models/qwen3-a22b"
    assert session.model == "accounts/fireworks/models/qwen3-a22b"
    assert stub.calls[-1] == ("board-7", None, "accounts/fireworks/models/qwen3-a22b", None)
