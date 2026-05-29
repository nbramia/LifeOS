"""Worker dispatch wiring for routing='code' sessions (#274).

Verifies:
  - `_dispatch_spawned_sessions` invokes the injected CodeExecutor for
    operator-origin sessions with routing='code' when LIFEOS_CODE_ROUTING
    is set to 'worker'.
  - With the flag at its default 'orchestrator' value, the same session is
    left in CLAIMED state and the executor is never called — i.e., the new
    code path is dead and the legacy ClaudeOrchestrator continues to own
    `/code` traffic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker


pytestmark = pytest.mark.unit


@dataclass
class _StubCodeExecutor:
    """Minimal CodeExecutor stand-in: records calls + returns a canned outcome."""
    outcome: ExecutorOutcome
    calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome


def _make_worker(tmp_path: Path, code_executor):
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
        code_executor=code_executor,
    )


def _seed_code_session(store: SessionStore, *, task_id: str = "code-1"):
    session = store.create(
        task_id=task_id,
        routing="code",
        origin="operator",
    )
    # Mirror the spawn surface contract (#275): the prompt for the first
    # turn lives in pending_messages on the session row.
    store.enqueue_message(session.session_id, sender_id="operator", content="print hello")
    return session


def test_dispatch_calls_code_executor_when_flag_is_worker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIFEOS_CODE_ROUTING", "worker")
    monkeypatch.setattr(
        "api.services.agent_worker.worker.settings.code_routing", "worker"
    )
    stub = _StubCodeExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    w = _make_worker(tmp_path, code_executor=stub)
    _seed_code_session(w.session_store, task_id="code-1")

    w._dispatch_spawned_sessions()

    # Executor was invoked exactly once with the seeded prompt drained from
    # pending_messages. Status transitions are the executor's responsibility
    # (the real CodeExecutor calls update_status; the stub doesn't, which is
    # why we don't assert on session.status here).
    assert stub.calls == [("code-1", "print hello")]


def test_dispatch_skips_code_executor_when_flag_is_orchestrator(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "api.services.agent_worker.worker.settings.code_routing", "orchestrator"
    )
    stub = _StubCodeExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    w = _make_worker(tmp_path, code_executor=stub)
    _seed_code_session(w.session_store, task_id="code-2")

    w._dispatch_spawned_sessions()

    assert stub.calls == []
    refreshed = w.session_store.get("code-2")
    # Left CLAIMED so a later flag flip can pick it up. No FAILED transition.
    assert refreshed.status == STATUS_CLAIMED
