"""Worker poll-loop integration tests.

Drives the worker against an in-process fake HTTP server (httpx MockTransport)
and a stub preflight caller. Each test exercises one outcome of the dispatch
machine: local completion, ambiguity → blocked, sanity → failed, daily cap
pause, sleeps wake-up.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_YIELDED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import (
    AGENT_TAG,
    BLOCKED_TAG,
    BUDGET_EXCEEDED_TAG,
    FAILED_TAG,
    RUNNING_TAG,
    Worker,
)


# ---------------------------------------------------------------------------
# Fake API
# ---------------------------------------------------------------------------

class FakeApi:
    """In-memory stand-in for /api/tasks."""

    def __init__(self, tasks=None):
        self.tasks = {t["id"]: t for t in (tasks or [])}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tasks":
            tag = request.url.params.get("tag")
            matched = [
                t for t in self.tasks.values()
                if t.get("status") == "todo" and (tag in t.get("tags", []) if tag else True)
            ]
            return httpx.Response(200, json={"tasks": matched, "total": len(matched)})

        if request.method == "POST" and request.url.path.endswith("/swap-tag"):
            task_id = request.url.path.split("/")[-2]
            from_tag = request.url.params.get("from")
            to_tag = request.url.params.get("to")
            task = self.tasks.get(task_id)
            if not task or from_tag not in task.get("tags", []):
                return httpx.Response(200, json={"swapped": False, "reason": "tag not present"})
            tags = list(task["tags"])
            tags[tags.index(from_tag)] = to_tag
            task["tags"] = tags
            return httpx.Response(200, json={"swapped": True})

        if request.method == "PUT" and request.url.path.endswith("/complete"):
            task_id = request.url.path.split("/")[-2]
            task = self.tasks.get(task_id)
            if not task:
                return httpx.Response(404)
            task["status"] = "done"
            return httpx.Response(200, json=task)

        if request.method == "GET" and "/api/tasks/" in request.url.path:
            task_id = request.url.path.split("/")[-1]
            task = self.tasks.get(task_id)
            if not task:
                return httpx.Response(404)
            return httpx.Response(200, json=task)

        return httpx.Response(404)


def _make_worker(tmp_path: Path, api: FakeApi, *, preflight_caller, local_executor):
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        http_client=client,
        preflight_caller=preflight_caller,
        local_executor=local_executor,
    )
    w._sent_telegram = sent  # type: ignore[attr-defined]
    return w


def _golden_preflight(routing="local", ambiguity=None, sane=True, sane_reason=""):
    payload = {
        "budget": {"wall_seconds": 3600, "max_tokens": 5000, "max_dollars": 5.0},
        "routing": routing,
        "routing_reason": f"test stub: routing={routing}",
        "expected_output": "text",
        "ambiguity": ambiguity,
        "sane": sane,
        "sane_reason": sane_reason,
    }
    return lambda prompt: json.dumps(payload)


@dataclass
class _StubExecutor:
    """Pretends to be a LocalExecutor — returns a canned outcome."""

    outcome: ExecutorOutcome
    calls: list = None

    def __post_init__(self):
        self.calls = []

    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_dispatch_local_completes_and_marks_task_done(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "hello there", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="hi back"))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    assert w.tick() == 1
    assert api.tasks["t1"]["status"] == "done"
    assert executor.calls == [("t1", "hello there")]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent and "completed 'hello there'" in sent[0]
    assert "hi back" in sent[0]


@pytest.mark.unit
def test_ambiguous_title_lands_in_blocked(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "reply to John", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="should not run"))
    preflight = _golden_preflight(
        routing="local",
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    # Executor should not have been invoked.
    assert executor.calls == []
    # Task gets the blocked tag.
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    assert AGENT_TAG not in api.tasks["t1"]["tags"]
    # Session status reflects blocked.
    assert w.session_store.get("t1").status == STATUS_BLOCKED
    # Telegram message includes the question.
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("Which John" in s for s in sent)


@pytest.mark.unit
def test_routing_ask_lands_in_blocked_with_model_question(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "research dolphins", "status": "todo", "tags": ["agent"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    preflight = _golden_preflight(routing="ask")
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls == []
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("local" in s.lower() and "claude" in s.lower() for s in sent)


@pytest.mark.unit
def test_insane_task_lands_in_failed(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "rm -rf /", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    preflight = _golden_preflight(routing="local", sane=False, sane_reason="destructive")
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls == []
    assert FAILED_TAG in api.tasks["t1"]["tags"]
    assert w.session_store.get("t1").status == STATUS_FAILED


@pytest.mark.unit
def test_executor_budget_exceeded_sets_budget_exceeded_tag(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "long task", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_BUDGET_EXCEEDED, reason="budget exceeded (max_tokens)",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    assert BUDGET_EXCEEDED_TAG in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("hit its budget" in s for s in sent)


@pytest.mark.unit
def test_claude_routing_without_managed_credentials_blocks(tmp_path: Path):
    """Without Managed Agents credentials configured the worker parks Claude-
    routed tasks at #agent-blocked. Same UX as ambiguity / sanity / ask."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "summarize", "status": "todo", "tags": ["agent"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    preflight = _golden_preflight(routing="claude")
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    # Sanity check: settings.agent_vault_id is empty in the test env, so
    # _get_managed_executor returns None and the worker takes the not-configured branch.
    w.tick()

    assert executor.calls == []
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    assert AGENT_TAG not in api.tasks["t1"]["tags"]
    assert RUNNING_TAG not in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("Managed Agents" in s for s in sent)


@pytest.mark.unit
def test_claude_routing_with_managed_executor_starts_and_polls(tmp_path: Path):
    """When Managed Agents is configured, the worker delegates to the
    managed executor: `start` on first tick (status=RUNNING) and `poll` on
    subsequent ticks until terminal."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "summarize my inbox", "status": "todo", "tags": ["agent"]},
    ])

    class _StubManagedExecutor:
        def __init__(self):
            self.start_calls = 0
            self.poll_calls = 0

        def start(self, session, task):
            self.start_calls += 1
            # Simulate driver attaching a remote id.
            store_for_session.set_managed_session_id(session.task_id, "sess_remote_42")
            return ExecutorOutcome(status="running")

        def poll(self, session):
            self.poll_calls += 1
            if self.poll_calls < 2:
                return ExecutorOutcome(status="running")
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="here's the summary")

    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    store_for_session = SessionStore(db_path=tmp_path / "sessions.db")
    sent: list[str] = []
    managed = _StubManagedExecutor()
    w = Worker(
        api_base="http://api",
        session_store=store_for_session,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        http_client=client,
        preflight_caller=_golden_preflight(routing="claude"),
        local_executor=_StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="")),
        managed_executor=managed,
    )

    # Tick 1: claim → preflight → managed.start. Task still #agent-running.
    w.tick()
    assert managed.start_calls == 1
    assert managed.poll_calls == 0
    assert RUNNING_TAG in api.tasks["t1"]["tags"]
    assert api.tasks["t1"]["status"] == "todo"
    # Tick 2: managed.poll → still running.
    w.tick()
    assert managed.poll_calls == 1
    assert RUNNING_TAG in api.tasks["t1"]["tags"]
    # Tick 3: managed.poll → completed → tag swap + Telegram + mark done.
    w.tick()
    assert managed.poll_calls == 2
    assert api.tasks["t1"]["status"] == "done"
    assert any("here's the summary" in s for s in sent)


@pytest.mark.unit
def test_sleep_yield_does_not_mark_terminal(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "wait for it", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_YIELDED, wake_at=99999999999,  # far future
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()

    # Task should remain at #agent-running while the session sleeps; no Telegram on yield.
    assert RUNNING_TAG in api.tasks["t1"]["tags"]
    assert api.tasks["t1"]["status"] == "todo"
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent == []


@pytest.mark.unit
def test_worker_skips_already_claimed_tasks(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "hi", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    # Re-tag back to #agent and verify the worker doesn't claim it again.
    api.tasks["t1"]["tags"] = ["agent", "local"]
    api.tasks["t1"]["status"] = "todo"
    assert w.tick() == 0
    # Executor was called exactly once across both ticks.
    assert len(executor.calls) == 1


@pytest.mark.unit
def test_worker_pauses_at_daily_cap(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "x", "status": "todo", "tags": ["agent", "local"]},
    ])
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=0.0),
        poll_seconds=0.01,
        telegram_send=lambda *a, **kw: True,
        http_client=client,
        preflight_caller=_golden_preflight(routing="local"),
        local_executor=executor,
    )
    assert w.tick() == 0
    assert executor.calls == []
