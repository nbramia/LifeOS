"""Worker integration tests for /code resume + BLOCKED flow.

Verifies:
- ``_resume_as_followup`` treats a code-routed answer as a fresh pending
  message that flips the session back to CLAIMED (so the spawned-session
  dispatch picks it up on the next tick and calls ``CodeExecutor.resume``).
- ``_dispatch_code_session`` calls ``resume()`` (not ``execute()``) when
  ``session.code_session_id`` is set.
- ``BLOCKED`` outcomes send a reply prompt with ID capture and register a
  ``kind='followup'`` row keyed to those IDs.
- ``COMPLETED`` outcomes with non-empty ``final_text`` register a followup row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.code_executor import (
    REASON_AWAITING_CLARIFICATION,
    REASON_AWAITING_PLAN_APPROVAL,
)
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
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
    execute_outcome: ExecutorOutcome
    resume_outcome: ExecutorOutcome | None = None
    execute_calls: list = field(default_factory=list)
    resume_calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.execute_calls.append((session.task_id, task.get("description")))
        return self.execute_outcome

    def resume(self, session, message, working_dir=None):
        self.resume_calls.append((session.task_id, message))
        return self.resume_outcome or self.execute_outcome


def _make_worker(tmp_path: Path, code_executor, monkeypatch=None):
    # monkeypatch retained as an optional arg for forward-compat with
    # tests that still pass it; the LIFEOS_CODE_ROUTING flag was removed
    # so there's nothing to set anymore.
    del monkeypatch
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    sent_with_ids: list[tuple[int, str]] = []

    def _send_with_id(text):
        msg_id = len(sent_with_ids) + 4000
        sent_with_ids.append((msg_id, text))
        return [msg_id]

    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        telegram_send_with_id=_send_with_id,
        http_client=client,
        code_executor=code_executor,
    )
    w._sent = sent  # type: ignore[attr-defined]
    w._sent_with_ids = sent_with_ids  # type: ignore[attr-defined]
    return w


def _seed_fresh_code_session(store: SessionStore, *, task_id="code-1"):
    """Mirror what spawn_code_session writes."""
    from api.services.agent_worker.code_spawn import spawn_code_session
    result = spawn_code_session(store, "print hello", chat_id="123")
    assert result["ok"]
    # Patch the auto-generated task_id to the deterministic one tests use.
    # Simpler: just return what we got.
    return store.get_by_session_id(result["session_id"])


def test_blocked_outcome_sends_prompt_and_registers_followup(tmp_path: Path, monkeypatch):
    stub = _StubCodeExecutor(execute_outcome=ExecutorOutcome(
        status=STATUS_BLOCKED,
        reason=REASON_AWAITING_PLAN_APPROVAL,
        final_text="step 1; step 2",
    ))
    w = _make_worker(tmp_path, stub, monkeypatch)
    session = _seed_fresh_code_session(w.session_store)

    w._dispatch_spawned_sessions()

    assert len(stub.execute_calls) == 1
    # Reply prompt was sent and captured with an id.
    assert w._sent_with_ids and "approve" in w._sent_with_ids[0][1]
    # pending_questions row registered keyed to the captured message id.
    q = w.session_store.get_open_question_by_message_id(w._sent_with_ids[0][0])
    assert q is not None
    assert q["kind"] == "followup"
    assert q["session_id"] == session.session_id


def test_clarification_block_sends_specific_prompt(tmp_path: Path, monkeypatch):
    stub = _StubCodeExecutor(execute_outcome=ExecutorOutcome(
        status=STATUS_BLOCKED,
        reason=REASON_AWAITING_CLARIFICATION,
        final_text="which file?",
    ))
    w = _make_worker(tmp_path, stub, monkeypatch)
    _seed_fresh_code_session(w.session_store)
    w._dispatch_spawned_sessions()
    assert "reply" in w._sent_with_ids[-1][1].lower()


def test_completed_outcome_registers_followup_for_reply(tmp_path: Path, monkeypatch):
    stub = _StubCodeExecutor(execute_outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="Here is the result.",
    ))
    w = _make_worker(tmp_path, stub, monkeypatch)
    session = _seed_fresh_code_session(w.session_store)
    w._dispatch_spawned_sessions()
    assert any("result" in body for _id, body in w._sent_with_ids)
    q = w.session_store.get_open_question_by_message_id(w._sent_with_ids[-1][0])
    assert q is not None
    assert q["session_id"] == session.session_id


def test_resume_dispatch_calls_resume_not_execute(tmp_path: Path, monkeypatch):
    stub = _StubCodeExecutor(
        execute_outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"),
        resume_outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="resumed"),
    )
    w = _make_worker(tmp_path, stub, monkeypatch)
    session = _seed_fresh_code_session(w.session_store)
    # Drain the spawn payload, mark a code_session_id (as the executor would),
    # then enqueue a reply.
    w.session_store.drain_pending_messages(session.session_id)
    w.session_store.set_code_session_id(session.task_id, "cli-abc")
    w.session_store.enqueue_message(session.session_id, "operator", "follow up please")

    w._dispatch_spawned_sessions()

    assert stub.execute_calls == []
    assert stub.resume_calls == [(session.task_id, "follow up please")]


def test_resume_as_followup_for_code_session_flips_to_claimed(tmp_path: Path, monkeypatch):
    """Telegram reply hook deposits answer → worker.tick processes the
    answered question → _resume_as_followup for routing='code' just
    re-claims the session and queues the reply as a pending message."""
    stub = _StubCodeExecutor(execute_outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="completed",
    ))
    w = _make_worker(tmp_path, stub, monkeypatch)
    session = _seed_fresh_code_session(w.session_store)
    # Pretend the first dispatch ran and completed: drain the prompt, mark
    # code_session_id, then a followup row was created on completion.
    w.session_store.drain_pending_messages(session.session_id)
    w.session_store.set_code_session_id(session.task_id, "cli-xyz")
    # Park the session at COMPLETED to mirror the post-run state.
    w.session_store.update_status(session.task_id, STATUS_COMPLETED)
    w.session_store.create_pending_question(
        session_id=session.session_id,
        task_id=session.task_id,
        question="result",
        sent_message_id=5500,
        sent_message_ids=[5500],
        kind="followup",
    )
    # Operator replies — the telegram hook calls deposit_answer which marks
    # the question as answered.
    w.session_store.deposit_answer(5500, "yes do that")

    w._process_clarification_answers()

    refreshed = w.session_store.get(session.task_id)
    assert refreshed.status == STATUS_CLAIMED
    # The reply was queued for the resume dispatch.
    pending = w.session_store.drain_pending_messages(session.session_id)
    assert [p["content"] for p in pending] == ["yes do that"]
