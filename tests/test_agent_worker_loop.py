"""Worker poll-loop integration tests.

Drives the worker against an in-process fake HTTP server (httpx MockTransport)
so we exercise the full claim → dispatch → complete → notify path without
spinning up FastAPI or hitting the real task store.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import AGENT_TAG, RUNNING_TAG, Worker


class FakeApi:
    """Minimal in-memory stand-in for /api/tasks.

    Exposes only what the worker calls: list, swap-tag, complete. Tracks
    state so the test can assert tag transitions and completion.
    """

    def __init__(self, tasks: list[dict] | None = None):
        # Each task is {id, description, status, tags}
        self.tasks: dict[str, dict] = {t["id"]: t for t in (tasks or [])}
        self.calls: list[tuple[str, str]] = []  # (method, path) for assertions

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
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

        return httpx.Response(404)


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi(tasks=[
        {"id": "task-1", "description": "do a thing", "status": "todo", "tags": ["agent"]},
    ])


@pytest.fixture
def worker(tmp_path: Path, fake_api: FakeApi):
    transport = httpx.MockTransport(fake_api.handler)
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
    )
    w._sent_telegram = sent  # type: ignore[attr-defined] — for test assertions
    return w


@pytest.mark.unit
def test_worker_claims_and_completes_agent_task(worker: Worker, fake_api: FakeApi):
    handled = worker.tick()
    assert handled == 1

    # Tag transitioned through #agent-running and the task is marked done.
    task = fake_api.tasks["task-1"]
    assert task["status"] == "done"
    assert AGENT_TAG not in task["tags"]
    # In Issue B the no-op dispatcher leaves the tag at #agent-running; this
    # is the visible signal that the worker finished without real execution.
    assert RUNNING_TAG in task["tags"]

    # Session row + transcript both present.
    sess = worker.session_store.get("task-1")
    assert sess is not None
    assert sess.status == STATUS_COMPLETED
    events = [e["kind"] for e in worker.transcript_store.read(sess.session_id)]
    assert events == ["claim", "noop_dispatch", "noop_complete"]

    # Telegram notification sent.
    sent = worker._sent_telegram  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert "no-op completed" in sent[0]


@pytest.mark.unit
def test_worker_skips_already_claimed_tasks(worker: Worker, fake_api: FakeApi):
    # First tick claims and completes.
    worker.tick()
    # Add the same task back with #agent tag (simulating a re-tag) and ensure
    # the worker doesn't try to claim it again — its session row is still in
    # the DB.
    fake_api.tasks["task-1"]["tags"] = ["agent"]
    fake_api.tasks["task-1"]["status"] = "todo"

    handled = worker.tick()
    assert handled == 0  # session already exists for task-1
    # Task wasn't re-claimed; tag stays as the operator left it
    assert "agent" in fake_api.tasks["task-1"]["tags"]


@pytest.mark.unit
def test_worker_loses_race_when_swap_tag_fails(worker: Worker, fake_api: FakeApi):
    """If another worker (or operator) removed the #agent tag between list and swap,
    swap_tag returns swapped=false. The worker should skip without creating a session."""
    fake_api.tasks["task-1"]["tags"] = ["something-else"]  # no longer #agent

    handled = worker.tick()
    assert handled == 0
    assert worker.session_store.get("task-1") is None


@pytest.mark.unit
def test_worker_pauses_at_daily_cap(tmp_path: Path, fake_api: FakeApi):
    """Cap of 0 should pause new claims with no other state needed."""
    transport = httpx.MockTransport(fake_api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=0.0),
        poll_seconds=0.01,
        telegram_send=lambda *a, **kw: True,
        http_client=client,
    )

    handled = w.tick()
    assert handled == 0
    assert w.session_store.get("task-1") is None


@pytest.mark.unit
def test_worker_rolls_back_tag_on_complete_failure(tmp_path: Path):
    """If POST /complete fails, the worker should roll #agent-running → #agent so the next tick can retry."""
    calls = {"complete_calls": 0}

    class FailingApi(FakeApi):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT" and request.url.path.endswith("/complete"):
                calls["complete_calls"] += 1
                return httpx.Response(503)
            return super().handler(request)

    api = FailingApi(tasks=[{"id": "task-1", "description": "x", "status": "todo", "tags": ["agent"]}])
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda *a, **kw: True,
        http_client=client,
    )

    w.tick()
    # Tag should be rolled back to #agent (so a future tick can re-attempt).
    assert "agent" in api.tasks["task-1"]["tags"]
    assert "agent-running" not in api.tasks["task-1"]["tags"]
    # Session is marked failed so we don't try to re-resume on startup forever.
    from api.services.agent_worker.session_store import STATUS_FAILED
    assert w.session_store.get("task-1").status == STATUS_FAILED


@pytest.mark.unit
def test_resume_pending_completes_orphaned_session(tmp_path: Path):
    """On restart, a session left in STATUS_RUNNING should be finalized."""
    api = FakeApi(tasks=[{"id": "task-1", "description": "x", "status": "todo", "tags": ["agent-running"]}])
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    store = SessionStore(db_path=tmp_path / "sessions.db")
    # Simulate a crash mid-dispatch: session row exists with STATUS_RUNNING.
    from api.services.agent_worker.session_store import STATUS_RUNNING
    store.create(task_id="task-1", status=STATUS_RUNNING)

    sent: list[str] = []
    w = Worker(
        api_base="http://api",
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        http_client=client,
    )
    recovered = w.resume_pending()
    assert recovered == 1
    assert api.tasks["task-1"]["status"] == "done"
    assert w.session_store.get("task-1").status == STATUS_COMPLETED
    assert any("recovered orphaned task" in s for s in sent)


@pytest.mark.unit
def test_resume_pending_rolls_tag_back_when_completion_fails(tmp_path: Path):
    """If completion still fails on resume, we mark FAILED and roll the tag back."""

    class FailingApi(FakeApi):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT" and request.url.path.endswith("/complete"):
                return httpx.Response(503)
            return super().handler(request)

    api = FailingApi(tasks=[{"id": "task-1", "description": "x", "status": "todo", "tags": ["agent-running"]}])
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    store = SessionStore(db_path=tmp_path / "sessions.db")
    from api.services.agent_worker.session_store import STATUS_FAILED, STATUS_RUNNING
    store.create(task_id="task-1", status=STATUS_RUNNING)

    sent: list[str] = []
    w = Worker(
        api_base="http://api",
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        http_client=client,
    )
    recovered = w.resume_pending()
    assert recovered == 0
    assert w.session_store.get("task-1").status == STATUS_FAILED
    assert "agent" in api.tasks["task-1"]["tags"]
    assert "agent-running" not in api.tasks["task-1"]["tags"]
    assert any("could not finalize" in s for s in sent)
