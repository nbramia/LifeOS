"""Spawned CLI children (claude_code/codex) dispatch off the tick (#299).

A long-running CLI subprocess must not block the worker's poll loop from
claiming new tasks or dispatching siblings. These tests use a capturing pool
(records submissions without running them) to prove the dispatch is handed off
rather than executed inline, and that the in-flight guard prevents a re-scan
from submitting the same child twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import STATUS_COMPLETED, SessionStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker


pytestmark = pytest.mark.unit


class _CapturingPool:
    """Records submitted callables without running them, so a test can assert
    the dispatch was handed off (not run inline) and then run it on demand."""

    def __init__(self):
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return None

    def shutdown(self, wait: bool = True) -> None:
        pass

    def run_all(self) -> None:
        for fn, args, kwargs in self.submitted:
            fn(*args, **kwargs)


@dataclass
class _StubCliExecutor:
    outcome: ExecutorOutcome
    calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome


def _worker(tmp_path: Path, pool, *, claude_code_executor=None, codex_executor=None):
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
        codex_executor=codex_executor,
        cli_pool=pool,
    )


def _seed_spawned_child(store: SessionStore, routing: str, *, task_id="cli-child"):
    """A spawned CLI child: parent set, claimed, prompt queued."""
    session = store.create(task_id=task_id, routing=routing, parent_session_id="sess_parent")
    store.enqueue_message(session.session_id, sender_id="sess_parent", content="do work")
    return session


def test_claude_code_child_dispatched_off_tick(tmp_path: Path):
    stub = _StubCliExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    pool = _CapturingPool()
    w = _worker(tmp_path, pool, claude_code_executor=stub)
    _seed_spawned_child(w.session_store, "claude_code")

    w._dispatch_spawned_sessions()

    # Handed to the pool, NOT run inline — the tick returned without blocking on
    # the (would-be long) subprocess.
    assert len(pool.submitted) == 1
    assert stub.calls == []
    # Running the submitted work executes the child and clears the in-flight mark.
    pool.run_all()
    assert stub.calls == [("cli-child", "do work")]
    assert w._cli_inflight == set()


def test_codex_child_dispatched_off_tick(tmp_path: Path):
    stub = _StubCliExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    pool = _CapturingPool()
    w = _worker(tmp_path, pool, codex_executor=stub)
    _seed_spawned_child(w.session_store, "codex")

    w._dispatch_spawned_sessions()

    assert len(pool.submitted) == 1
    assert stub.calls == []
    pool.run_all()
    assert stub.calls == [("cli-child", "do work")]


def test_inflight_guard_prevents_double_dispatch_across_ticks(tmp_path: Path):
    """While a child is in flight (submitted, not yet finished), a subsequent
    tick must not submit it again."""
    stub = _StubCliExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    pool = _CapturingPool()  # never runs → child stays in-flight
    w = _worker(tmp_path, pool, claude_code_executor=stub)
    _seed_spawned_child(w.session_store, "claude_code")

    w._dispatch_spawned_sessions()
    w._dispatch_spawned_sessions()  # next tick, child still CLAIMED + in-flight

    assert len(pool.submitted) == 1  # submitted exactly once


def test_local_child_still_runs_inline(tmp_path: Path):
    """The local route is intentionally not pooled — it must still run inline
    (capped at one concurrent session)."""
    stub = _StubCliExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="done."))
    pool = _CapturingPool()
    w = _worker(tmp_path, pool)
    # Inject the local executor via the lazy accessor seam.
    w._local_executor = stub
    _seed_spawned_child(w.session_store, "local")

    w._dispatch_spawned_sessions()

    assert pool.submitted == []        # local never touches the CLI pool
    assert stub.calls == [("cli-child", "do work")]  # ran inline
