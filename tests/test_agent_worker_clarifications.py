"""Tests for the Telegram clarification round-trip (Issue F).

End-to-end:
  1. Preflight flags ambiguity → worker sends a Telegram question and captures
     `sent_message_id`, parks task at #agent-blocked.
  2. User replies (reply-threaded). The listener's deposit hook updates
     pending_questions.answer.
  3. Worker tick scans answered+unprocessed, injects the answer as a user
     turn, swaps tag back to #agent-running, resumes the local executor.

Also covers `lifeos_agent_user_ask` (agent-initiated clarification) and the
3-day timeout path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import (
    BLOCKED_TAG,
    BUDGET_EXCEEDED_TAG,
    COMPLETED_TAG,
    FAILED_TAG,
    RUNNING_TAG,
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


class _SequenceExecutor:
    """Returns a queued outcome per call (last one repeats once exhausted)."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        if len(self.calls) <= len(self.outcomes):
            return self.outcomes[len(self.calls) - 1]
        return self.outcomes[-1]


def _make_worker(tmp_path, api, *, preflight_caller, local_executor):
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    sent_with_ids: list[tuple[int, str]] = []
    def _send_with_id(text):
        # Mirror send_message_capture_ids: one id per ~4096-char chunk, all
        # returned so a reply to any chunk can be matched.
        sent.append(text)
        n_chunks = max(1, -(-len(text) // 4096))
        chunk_ids = [1000 + len(sent_with_ids) + i for i in range(n_chunks)]
        for cid in chunk_ids:
            sent_with_ids.append((cid, text))
        return chunk_ids
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
def test_stale_answered_question_doesnt_redeposit(tmp_path: Path):
    """Second reply to an already-answered question falls through to chat."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    sess = store.create(
        task_id="t", status=STATUS_BLOCKED, routing="local",
        budget={"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100},
    )
    store.create_pending_question(
        session_id=sess.session_id, task_id="t",
        question="?", sent_message_id=7,
    )
    assert store.deposit_answer(7, "first") is True
    # Second attempt against the same message_id finds nothing open.
    assert store.deposit_answer(7, "second") is False
    q = store.get_question_by_message_id(7)
    assert q["answer"] == "first"


@pytest.mark.unit
def test_routing_ask_local_reply_routes_to_local_executor(tmp_path: Path):
    """Routing-ask resume: user reply 'local' must run the local executor."""

    def routing_ask_preflight():
        import json
        def _caller(prompt):
            return json.dumps({
                "budget": {"wall_seconds": 3600, "max_tokens": 1000, "max_dollars": 5.0},
                "routing": "ask", "routing_reason": "no model hint",
                "expected_output": "text",
                "ambiguity": None, "sane": True, "sane_reason": "",
            })
        return _caller

    api = FakeApi([{"id": "t1", "description": "research dolphins", "status": "todo", "tags": ["agent"]}])
    executor = _StubExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"))
    w = _make_worker(tmp_path, api, preflight_caller=routing_ask_preflight(), local_executor=executor)
    w.tick()

    # Task is blocked, routing=ask.
    blocked = w.session_store.list_sessions(status=STATUS_BLOCKED)[0]
    assert blocked.routing == "ask"

    # User replies "local".
    msg_id, _ = w._sent_with_ids[0]
    w.session_store.deposit_answer(msg_id, "use local please")

    # Resume tick — routing gets resolved to "local" and executor runs.
    w.tick()
    assert executor.calls, "local executor should have been invoked"
    refreshed = w.session_store.get(blocked.task_id)
    assert refreshed.routing == "local"


@pytest.mark.unit
def test_routing_ask_unparseable_reply_reasks(tmp_path: Path):
    """If the user's reply doesn't contain local/claude, ask again."""
    def routing_ask_preflight():
        import json
        def _caller(prompt):
            return json.dumps({
                "budget": {"wall_seconds": 3600, "max_tokens": 1000, "max_dollars": 5.0},
                "routing": "ask", "routing_reason": "x",
                "expected_output": "text",
                "ambiguity": None, "sane": True, "sane_reason": "",
            })
        return _caller

    api = FakeApi([{"id": "t1", "description": "x", "status": "todo", "tags": ["agent"]}])
    executor = _StubExecutor(ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = _make_worker(tmp_path, api, preflight_caller=routing_ask_preflight(), local_executor=executor)
    w.tick()

    msg_id, _ = w._sent_with_ids[0]
    w.session_store.deposit_answer(msg_id, "yeah whatever you like")

    w.tick()
    # Executor not invoked — we re-asked.
    assert executor.calls == []
    # A second Telegram message was sent (the re-ask).
    assert len(w._sent_with_ids) >= 2
    second_text = w._sent_with_ids[1][1].lower()
    assert "local" in second_text and "claude" in second_text


@pytest.mark.unit
def test_routing_answer_parser_combined_replies():
    """Best-effort parse of combined ambiguity+routing replies."""
    from api.services.agent_worker.worker import Worker
    parse = Worker._parse_routing_answer
    assert parse("local") == "local"
    assert parse("CLAUDE please") == "claude"
    assert parse("use opus") == "claude"
    assert parse("gemma is fine") == "local"
    # Combined: ambiguity answer first, model second.
    assert parse("1. John Doe 2. local") == "local"
    assert parse("It's John Doe, and let's use claude") == "claude"
    # Neither keyword → None.
    assert parse("yes do it") is None
    assert parse("") is None


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
def test_lifeos_agent_user_ask_blocks_and_records_question(tmp_path: Path):
    """An agent calling `lifeos_agent_user_ask` should park the session and record
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
        return [msg_id]

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
    result = dispatch(ctx, "lifeos_agent_user_ask", {"question": "Should I delete the file?"})
    assert result["ok"]
    assert result["blocked"]
    assert store.get("active_t").status == STATUS_BLOCKED
    # And the worker recorded a pending question with the captured message id.
    msg_id, _ = sent_with_ids[0]
    q = store.get_question_by_message_id(msg_id)
    assert q is not None
    assert q["task_id"] == "active_t"


@pytest.mark.unit
def test_lifeos_agent_user_ask_fails_when_telegram_unavailable(tmp_path: Path):
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
    result = dispatch(ctx, "lifeos_agent_user_ask", {"question": "anyone home?"})
    assert not result["ok"]
    assert result["error"] == "telegram_unavailable"
    # Session NOT blocked (operator-facing failure mode).
    assert store.get("t").status == STATUS_RUNNING


# =============================================================================
# Replyable terminal states + any-chunk matching (Issue #234)
# =============================================================================


@pytest.mark.unit
def test_failed_task_registers_followup_and_reply_resumes(tmp_path: Path):
    """A FAILED task's notification is replyable: replying resumes the session,
    swapping the failed tag back to running, and a clean completion follows."""
    api = FakeApi([{"id": "t1", "description": "do the thing", "status": "todo", "tags": ["agent"]}])
    executor = _SequenceExecutor([
        ExecutorOutcome(status=STATUS_FAILED, reason="boom"),
        ExecutorOutcome(status=STATUS_COMPLETED, final_text="fixed it"),
    ])
    w = _make_worker(tmp_path, api, preflight_caller=_local_ok_preflight(), local_executor=executor)
    w.tick()

    # Parked at the failed tag, and a follow-up was registered against the
    # terminal notification's message id (kind='followup', task title as label).
    assert FAILED_TAG in api.tasks["t1"]["tags"]
    msg_id, _ = w._sent_with_ids[-1]
    q = w.session_store.get_question_by_message_id(msg_id)
    assert q is not None
    assert q["kind"] == "followup"
    assert q["question"] == "do the thing"

    # Operator replies → deposit → next tick resumes and completes.
    assert w.session_store.deposit_answer(msg_id, "try again")
    w.tick()
    assert len(executor.calls) == 2, "executor should have been re-invoked on resume"
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert RUNNING_TAG not in api.tasks["t1"]["tags"]


@pytest.mark.unit
def test_budget_exceeded_task_registers_followup(tmp_path: Path):
    """BUDGET_EXCEEDED notifications are replyable too."""
    api = FakeApi([{"id": "t1", "description": "big job", "status": "todo", "tags": ["agent"]}])
    executor = _StubExecutor(ExecutorOutcome(status=STATUS_BUDGET_EXCEEDED, reason="out of budget"))
    w = _make_worker(tmp_path, api, preflight_caller=_local_ok_preflight(), local_executor=executor)
    w.tick()

    assert BUDGET_EXCEEDED_TAG in api.tasks["t1"]["tags"]
    msg_id, _ = w._sent_with_ids[-1]
    q = w.session_store.get_question_by_message_id(msg_id)
    assert q is not None and q["kind"] == "followup"


@pytest.mark.unit
def test_reply_to_non_first_chunk_matches_and_resumes(tmp_path: Path):
    """A long terminal notification splits into multiple Telegram chunks;
    replying to a non-first chunk still matches the follow-up and resumes.

    Uses a long FAILED reason (the failure body isn't vault-spilled, so it
    actually exceeds one 4096-char chunk)."""
    api = FakeApi([{"id": "t1", "description": "task", "status": "todo", "tags": ["agent"]}])
    long_reason = "y" * 9000  # forces the notification body across 3 chunks
    executor = _SequenceExecutor([
        ExecutorOutcome(status=STATUS_FAILED, reason=long_reason),
        ExecutorOutcome(status=STATUS_COMPLETED, final_text="done again"),
    ])
    w = _make_worker(tmp_path, api, preflight_caller=_local_ok_preflight(), local_executor=executor)
    w.tick()

    # The notification spanned multiple chunks.
    followup = w.session_store.get_recent_resumable_followup(within_seconds=3600)
    assert followup is not None
    chunk_ids = json.loads(followup["sent_message_ids"])
    assert len(chunk_ids) >= 2, "notification should have spanned multiple chunks"

    # Reply lands on the LAST chunk (not the primary first-chunk id).
    last_chunk = chunk_ids[-1]
    assert last_chunk != followup["sent_message_id"]
    assert w.session_store.deposit_answer(last_chunk, "more please")
    w.tick()
    assert len(executor.calls) == 2, "reply to a later chunk should resume"


@pytest.mark.unit
def test_get_recent_resumable_followup_window(tmp_path: Path):
    """get_recent_resumable_followup honors the time window and open-state."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    sess = store.create(
        task_id="t1", status=STATUS_COMPLETED, routing="local",
        budget={"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100},
    )
    store.register_completion_followup(
        session_id=sess.session_id, task_id="t1",
        sent_message_ids=[500, 501], label="recent task",
    )
    # Within window → found.
    row = store.get_recent_resumable_followup(within_seconds=1800)
    assert row is not None and row["task_id"] == "t1"

    # Once answered, it is no longer resumable via this path.
    assert store.deposit_answer(500, "go")
    assert store.get_recent_resumable_followup(within_seconds=1800) is None


@pytest.mark.unit
def test_get_recent_resumable_followup_excludes_clarifications(tmp_path: Path):
    """Only kind='followup' rows count — open clarifications don't trigger the
    plain-message resume path."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    sess = store.create(
        task_id="t1", status=STATUS_BLOCKED, routing="local",
        budget={"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100},
    )
    store.create_pending_question(
        session_id=sess.session_id, task_id="t1",
        question="which one?", sent_message_id=42,
    )
    assert store.get_recent_resumable_followup(within_seconds=1800) is None


@pytest.mark.unit
def test_get_recent_resumable_followup_respects_cutoff(tmp_path: Path):
    """A follow-up older than the window is not returned (no surprise resume)."""
    import sqlite3

    db = tmp_path / "sessions.db"
    store = SessionStore(db_path=db)
    sess = store.create(
        task_id="t1", status=STATUS_COMPLETED, routing="local",
        budget={"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100},
    )
    qid = store.register_completion_followup(
        session_id=sess.session_id, task_id="t1",
        sent_message_ids=[700], label="old task",
    )
    # Backdate the notification 31 minutes.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_questions SET sent_at = sent_at - ? WHERE id = ?",
            (31 * 60, qid),
        )
    assert store.get_recent_resumable_followup(within_seconds=1800) is None
    # A wider window still finds it.
    assert store.get_recent_resumable_followup(within_seconds=3600) is not None


@pytest.mark.unit
def test_native_reply_targets_specific_older_thread(tmp_path: Path):
    """A native reply to a specific (older) thread's message id targets THAT
    thread, not merely the most recent one."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    budget = {"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100}
    s_old = store.create(task_id="old", status=STATUS_COMPLETED, routing="local", budget=budget)
    s_new = store.create(task_id="new", status=STATUS_COMPLETED, routing="local", budget=budget)
    store.register_completion_followup(
        session_id=s_old.session_id, task_id="old", sent_message_ids=[100], label="old",
    )
    store.register_completion_followup(
        session_id=s_new.session_id, task_id="new", sent_message_ids=[200], label="new",
    )
    # Native reply to the OLD message id deposits into the OLD follow-up only.
    assert store.deposit_answer(100, "revisit old")
    assert store.get_question_by_message_id(100)["answer"] == "revisit old"
    assert store.get_question_by_message_id(200)["answer"] is None
