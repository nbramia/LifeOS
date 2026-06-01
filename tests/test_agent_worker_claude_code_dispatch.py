"""Worker dispatch wiring for routing='claude_code' sessions.

Verifies that ``_dispatch_spawned_sessions`` invokes the injected
``ClaudeCodeExecutor`` for operator-origin sessions with ``routing='claude_code'``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker


pytestmark = pytest.mark.unit


@dataclass
class _StubClaudeCodeExecutor:
    """Minimal ClaudeCodeExecutor stand-in: records calls + returns a canned outcome."""
    outcome: ExecutorOutcome
    calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome


def _make_worker(tmp_path: Path, claude_code_executor):
    # No #agent task pickup happens in these tests — the worker only runs
    # spawned-session dispatch. A 404-everywhere transport is enough so the
    # `list_agent_tasks` and `_fetch_task` calls don't raise.
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    return Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [1],
        http_client=client,
        claude_code_executor=claude_code_executor,
    )


def _seed_code_session(store: SessionStore, *, task_id: str = "code-1"):
    session = store.create(
        task_id=task_id,
        routing="claude_code",
        origin="operator",
    )
    # Mirror the spawn surface contract: the prompt for the first
    # turn lives in pending_messages on the session row.
    store.enqueue_message(session.session_id, sender_id="operator", content="print hello")
    return session


def _recording_worker(tmp_path: Path, claude_code_executor):
    """Like ``_make_worker`` but records the vault-mutating HTTP calls so a
    test can assert the task was completed + tag-swapped (or not)."""
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/swap-tag"):
            return httpx.Response(200, json={"swapped": True})
        return httpx.Response(200, json={"tasks": []})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api")
    worker = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [1],
        http_client=client,
        claude_code_executor=claude_code_executor,
    )
    return worker, calls


def test_vault_claude_task_marked_complete_on_finish(tmp_path: Path):
    """A vault-routed ``#agent #claude`` task (origin != 'operator', no parent)
    must be reconciled in the vault when the Claude Code session completes:
    PUT .../complete and a #agent-running → #agent-completed swap. Regression
    for CLI tasks stranded at ``[/]`` / ``#agent-running`` forever (the CLI
    dispatch path bypasses ``_handle_outcome``)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done.")
    )
    worker, calls = _recording_worker(tmp_path, claude_code_executor=stub)
    # origin defaults to None → vault-backed task (not an operator spawn).
    session = worker.session_store.create(task_id="vault-1", routing="claude_code")

    worker._dispatch_claude_code_session(session, [{"content": "print hello"}])

    assert ("PUT", "/api/tasks/vault-1/complete") in calls
    assert ("POST", "/api/tasks/vault-1/swap-tag") in calls


def test_operator_spawn_does_not_touch_vault_on_finish(tmp_path: Path):
    """Operator-spawned /claude sessions have no backing #agent vault row, so
    completion must NOT issue complete / swap-tag calls (they would 404)."""
    stub = _StubClaudeCodeExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done.")
    )
    worker, calls = _recording_worker(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="spawn-1", routing="claude_code", origin="operator"
    )

    worker._dispatch_claude_code_session(session, [{"content": "print hello"}])

    assert not any(path.endswith(("/complete", "/swap-tag")) for _, path in calls)


def test_dispatch_calls_claude_code_executor(tmp_path: Path):
    """``_dispatch_spawned_sessions`` invokes the injected ClaudeCodeExecutor for
    an operator-origin routing='claude_code' session, draining the prompt from
    ``pending_messages`` as the task description.

    Status transitions are the executor's responsibility (the real
    ``ClaudeCodeExecutor`` calls ``update_status``; the stub doesn't, so we don't
    assert on ``session.status`` here).
    """
    # The dispatch path expects the spawn payload (a JSON-encoded dict)
    # produced by ``claude_code_spawn.spawn_claude_code_session``; the stub call's task
    # description is the decoded ``prompt`` field.
    stub = _StubClaudeCodeExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    w = _make_worker(tmp_path, claude_code_executor=stub)
    _seed_code_session(w.session_store, task_id="code-1")

    w._dispatch_spawned_sessions()

    # The seeded pending message is the bare string "print hello"; the
    # JSON-decode falls back to treating the whole content as the prompt.
    assert stub.calls == [("code-1", "print hello")]
