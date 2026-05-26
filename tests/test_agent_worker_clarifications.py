"""Tests for the Telegram clarification round-trip (Issue F).

End-to-end:
  1. Preflight flags ambiguity → worker sends a Telegram question and captures
     `sent_message_id`, parks task at #agent-blocked.
  2. User replies (reply-threaded). The listener's deposit hook updates
     pending_questions.answer.
  3. Worker tick scans answered+unprocessed, injects the answer as a user
     turn, swaps tag back to #agent-running, resumes the local executor.

Also covers `lifeos_user_ask` (agent-initiated clarification) and the
3-day timeout path.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import (
    BLOCKED_TAG,
    Worker,
)


class FakeApi:
    def __init__(self, tasks):
        self.tasks = {t["id"]: t for t in tasks}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/tasks":
            tag = request.url.params.get("tag")
            matched = [t for t in self.tasks.values()
                       if t.get("status") == "todo" and (tag in t.get("tags", []) if tag else True)]
            return httpx.Response(200, json={"tasks": matched, "total": len(matched)})
        if request.method == "POST" and path.endswith("/swap-tag"):
            tid = path.split("/")[-2]
            f = request.url.params.get("from")
            to = request.url.params.get("to")
            t = self.tasks.get(tid)
            if not t or f not in t.get("tags", []):
                return httpx.Response(200, json={"swapped": False})
            tags = list(t["tags"])
            tags[tags.index(f)] = to
            t["tags"] = tags
            return httpx.Response(200, json={"swapped": True})
        if request.method == "PUT" and path.endswith("/complete"):
            tid = path.split("/")[-2]
            t = self.tasks.get(tid)
            if not t:
                return httpx.Response(404)
            t["status"] = "done"
            return httpx.Response(200, json=t)
        if request.method == "GET" and "/api/tasks/" in path:
            tid = path.split("/")[-1]
            t = self.tasks.get(tid)
            return httpx.Response(200, json=t) if t else httpx.Response(404)
        return httpx.Response(404)


class _StubExecutor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome


def _make_worker(tmp_path, api, *, preflight_caller, local_executor):
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    sent_with_ids: list[tuple[int, str]] = []
    def _send_with_id(text):
        sent.append(text)
        msg_id = 1000 + len(sent_with_ids)
        sent_with_ids.append((msg_id, text))
        return msg_id
    store = SessionStore(db_path=tmp_path / "sessions.db")
    w = Worker(
        api_base="http://api",
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        telegram_send_with_id=_send_with_id,
        http_client=client,
        preflight_caller=preflight_caller,
        local_executor=local_executor,
    )
    w._sent = sent  # type: ignore[attr-defined]
    w._sent_with_ids = sent_with_ids  # type: ignore[attr-defined]
    return w


def _ambiguity_preflight():
    """Preflight that flags every task as ambiguous."""
    def _caller(prompt):
        import json
        return json.dumps({
            "budget": {"wall_seconds": 3600, "max_tokens": 1000, "max_dollars": 5.0},
            "routing": "local", "routing_reason": "test",
            "expected_output": "text",
            "ambiguity": {"question": "Which John — Doe or Smith?"},
            "sane": True, "sane_reason": "",
        })
    return _caller


def _local_ok_preflight():
    def _caller(prompt):
        import json
        return json.dumps({
            "budget": {"wall_seconds": 3600, "max_tokens": 1000, "max_dollars": 5.0},
            "routing": "local", "routing_reason": "test",
            "expected_output": "text",
            "ambiguity": None, "sane": True, "sane_reason": "",
        })
    return _caller


@pytest.mark.unit
def test_ambiguous_task_sends_clarification_with_tracked_id(tmp_path: Path):
    api = FakeApi([{"id": "t1", "description": "reply to John", "status": "todo", "tags": ["agent"]}])
    executor = _StubExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = _make_worker(tmp_path, api, preflight_caller=_ambiguity_preflight(), local_executor=executor)
    w.tick()

    # Task is blocked, executor was NOT invoked.
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    assert executor.calls == []

    # A pending question is recorded with the captured sent_message_id.
    sessions = w.session_store.list_sessions(status=STATUS_BLOCKED)
    assert sessions
    blocked = sessions[0]
    msg_id, body = w._sent_with_ids[0]
    q = w.session_store.get_question_by_message_id(msg_id)
    assert q is not None
    assert q["session_id"] == blocked.session_id
    assert q["answered_at"] is None
    assert "Which John" in q["question"]


@pytest.mark.unit
def test_reply_threaded_answer_resumes_blocked_session(tmp_path: Path):
    """End-to-end: ambiguous → answered → resumed → completed."""
    api = FakeApi([{"id": "t1", "description": "reply to John", "status": "todo", "tags": ["agent"]}])
    # First call: returns "ambiguous". After resume, preflight isn't called
    # again — the executor handles the rest.
    executor = _StubExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="emailed Doe"))
    w = _make_worker(tmp_path, api, preflight_caller=_ambiguity_preflight(), local_executor=executor)
    w.tick()

    # Operator answers via Telegram (deposit by message_id).
    msg_id, _ = w._sent_with_ids[0]
    deposited = w.session_store.deposit_answer(msg_id, "John Doe")
    assert deposited

    # Next tick should resume the session.
    w.tick()
    assert executor.calls, "executor should have been invoked on resume"
    # Task now done.
    assert api.tasks["t1"]["status"] == "done"
    # And the question is processed.
    q = w.session_store.get_question_by_message_id(msg_id)
    assert q["processed"] == 1


@pytest.mark.unit
def test_unmatched_reply_does_not_deposit(tmp_path: Path):
    """A reply-threaded message that doesn't match any open question is a no-op."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    # No questions in the DB.
    assert store.deposit_answer(999, "stray answer") is False


@pytest.mark.unit
def test_timeout_marks_question_and_nudges(tmp_path: Path):
    """A question older than the timeout gets `timed_out=1` + a follow-up nudge."""
    api = FakeApi([{"id": "t1", "description": "x", "status": "todo", "tags": ["agent-blocked"]}])
    executor = _StubExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = _make_worker(tmp_path, api, preflight_caller=_local_ok_preflight(), local_executor=executor)

    # Create a session and an already-stale pending question by hand.
    session = w.session_store.create(
        task_id="t1", status=STATUS_BLOCKED, routing="local",
        budget={"wall_seconds": 60, "max_tokens": 100, "max_dollars": 1.0},
    )
    qid = w.session_store.create_pending_question(
        session_id=session.session_id, task_id="t1",
        question="Which John?", sent_message_id=42,
    )
    # Backdate the sent_at by 4 days so it's past the 72h default cutoff.
    with w.session_store._connect() as conn:
        conn.execute(
            "UPDATE pending_questions SET sent_at = ? WHERE id = ?",
            (int(time.time()) - 4 * 86400, qid),
        )

    w._timeout_stale_clarifications()
    refreshed = w.session_store.get_question_by_message_id(42)
    assert refreshed["timed_out"] == 1
    assert any("still waiting on your reply" in s for s in w._sent)


@pytest.mark.unit
def test_lifeos_user_ask_blocks_and_records_question(tmp_path: Path):
    """An agent calling `lifeos_user_ask` should park the session and record
    a pending_question that the user can reply-thread to."""
    api = FakeApi([])
    store = SessionStore(db_path=tmp_path / "sessions.db")
    session = store.create(
        task_id="active_t", status=STATUS_RUNNING, routing="local",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
    )
    sent_with_ids: list[tuple[int, str]] = []
    def _send_with_id(text):
        msg_id = 5000 + len(sent_with_ids)
        sent_with_ids.append((msg_id, text))
        return msg_id

    w = Worker(
        api_base="http://api",
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda *a, **kw: True,
        telegram_send_with_id=_send_with_id,
        http_client=httpx.Client(transport=httpx.MockTransport(api.handler), base_url="http://api"),
    )

    from api.services.agent_worker.inter_agent import (
        Caps,
        InterAgentContext,
        dispatch,
    )
    ctx = InterAgentContext(
        session_store=store,
        transcript_store=w.transcript_store,
        caller_session_id=session.session_id,
        caps=Caps(),
        worker_handle=w,
    )
    result = dispatch(ctx, "lifeos_user_ask", {"question": "Should I delete the file?"})
    assert result["ok"]
    assert result["blocked"]
    assert store.get("active_t").status == STATUS_BLOCKED
    # And the worker recorded a pending question with the captured message id.
    msg_id, _ = sent_with_ids[0]
    q = store.get_question_by_message_id(msg_id)
    assert q is not None
    assert q["task_id"] == "active_t"


@pytest.mark.unit
def test_lifeos_user_ask_fails_when_telegram_unavailable(tmp_path: Path):
    """If the worker can't send (no Telegram config), the tool returns an error
    rather than blocking the session in an unrecoverable state."""
    api = FakeApi([])
    store = SessionStore(db_path=tmp_path / "sessions.db")
    session = store.create(
        task_id="t", status=STATUS_RUNNING, routing="local",
        budget={"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100},
    )

    w = Worker(
        api_base="http://api",
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda *a, **kw: True,
        telegram_send_with_id=lambda text: None,  # simulates Telegram off
        http_client=httpx.Client(transport=httpx.MockTransport(api.handler), base_url="http://api"),
    )

    from api.services.agent_worker.inter_agent import (
        Caps,
        InterAgentContext,
        dispatch,
    )
    ctx = InterAgentContext(
        session_store=store,
        transcript_store=w.transcript_store,
        caller_session_id=session.session_id,
        caps=Caps(),
        worker_handle=w,
    )
    result = dispatch(ctx, "lifeos_user_ask", {"question": "anyone home?"})
    assert not result["ok"]
    assert result["error"] == "telegram_unavailable"
    # Session NOT blocked (operator-facing failure mode).
    assert store.get("t").status == STATUS_RUNNING
