"""Worker dispatch wiring for routing='codex' sessions.

Verifies the two behaviors issue #295 hardened on the Codex completion path:
1. The final agent message is sent to Telegram exactly once (no duplicate) via
   the id-capturing sender — the executor no longer streams it separately.
2. That single completion message is registered as a ``kind='followup'``
   anchor so a threaded Telegram reply resumes the session (parity with
   ``#claude``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker
from api.services.conversation_store import ConversationStore


pytestmark = pytest.mark.unit


@dataclass
class _StubCodexExecutor:
    """Minimal CodexExecutor stand-in: records calls + returns a canned outcome."""
    outcome: ExecutorOutcome
    calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome


def _make_worker(tmp_path: Path, codex_executor, *, plain_sends, withid_sends):
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")

    def plain(text, chat_id=None):
        plain_sends.append(text)
        return True

    def with_id(text):
        withid_sends.append(text)
        return [777]

    return Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        # Isolate the conversation DB (default resolves to prod data/conversations.db).
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=plain,
        telegram_send_with_id=with_id,
        http_client=client,
        codex_executor=codex_executor,
    )


def test_codex_completion_sends_final_once_and_registers_anchor(tmp_path: Path):
    plain_sends: list[str] = []
    withid_sends: list[str] = []
    stub = _StubCodexExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="Done: 3 events.")
    )
    worker = _make_worker(
        tmp_path, codex_executor=stub,
        plain_sends=plain_sends, withid_sends=withid_sends,
    )
    session = worker.session_store.create(
        task_id="cx-1", routing="codex", origin="operator",
    )

    worker._dispatch_codex_session(session, [{"content": "events?"}])

    # Final message sent exactly once, via the id-capturing sender, and never
    # duplicated through the plain sender.
    assert withid_sends == ["Done: 3 events."]
    assert "Done: 3 events." not in plain_sends

    # The completion message is registered as a followup anchor keyed on the
    # sent message id, so a threaded reply round-trips through the resume path.
    q = worker.session_store.get_open_question_by_message_id(777)
    assert q is not None
    assert q["kind"] == "followup"
    assert q["session_id"] == session.session_id


def test_codex_completion_empty_final_registers_no_anchor(tmp_path: Path):
    """An empty final message produces no Telegram send and no anchor — there
    is nothing for the operator to reply to."""
    plain_sends: list[str] = []
    withid_sends: list[str] = []
    stub = _StubCodexExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="")
    )
    worker = _make_worker(
        tmp_path, codex_executor=stub,
        plain_sends=plain_sends, withid_sends=withid_sends,
    )
    session = worker.session_store.create(
        task_id="cx-2", routing="codex", origin="operator",
    )

    worker._dispatch_codex_session(session, [{"content": "events?"}])

    assert withid_sends == []
    assert worker.session_store.get_open_question_by_message_id(777) is None


# ---------------------------------------------------------------------------
# Crash-before-init re-execute guard + terminal-status persistence (#411,
# mirroring #400/#408 for the claude_code path)
# ---------------------------------------------------------------------------


@dataclass
class _ResumableStubCodexExecutor(_StubCodexExecutor):
    resume_calls: list = field(default_factory=list)

    def resume(self, session, message):
        self.resume_calls.append((session.task_id, message))
        return self.outcome


def test_codex_crash_before_init_does_not_reexecute(tmp_path: Path):
    """A codex session whose subprocess launched once (a `codex_spawn` event is
    present) but never persisted its session id must NOT re-run the original
    prompt on redispatch — re-execution could repeat side effects."""
    plain_sends: list[str] = []
    withid_sends: list[str] = []
    # COMPLETED makes a regression obvious: if the guard fails to fire, execute()
    # runs and the session would end COMPLETED instead of FAILED.
    stub = _ResumableStubCodexExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="should not run")
    )
    worker = _make_worker(tmp_path, codex_executor=stub, plain_sends=plain_sends, withid_sends=withid_sends)
    session = worker.session_store.create(task_id="cx-crash", routing="codex", origin="operator")
    worker.transcript_store.append(session.session_id, "codex_spawn", {"resume": False})

    worker._dispatch_codex_session(session, [{"content": "file the report"}])

    assert stub.calls == [] and stub.resume_calls == []  # neither re-ran the prompt
    assert worker.session_store.get("cx-crash").status == STATUS_FAILED
    kinds = [e["kind"] for e in worker.transcript_store.read(session.session_id)]
    assert "codex_reexecute_averted" in kinds
    assert any("already started" in s for s in plain_sends)


def test_codex_binary_not_found_does_not_trip_guard(tmp_path: Path):
    """A missing codex binary writes `codex_spawn` then `codex_binary_not_found`,
    but no subprocess launched — the guard must NOT misread that as a crashed run.
    Execute still runs; FAILED is persisted to the row (FIX A)."""
    plain_sends: list[str] = []
    withid_sends: list[str] = []
    stub = _StubCodexExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason="binary_not_found"))
    worker = _make_worker(tmp_path, codex_executor=stub, plain_sends=plain_sends, withid_sends=withid_sends)
    session = worker.session_store.create(task_id="cx-bn", routing="codex", origin="operator")
    worker.transcript_store.append(session.session_id, "codex_spawn", {"resume": False})
    worker.transcript_store.append(session.session_id, "codex_binary_not_found", {"error": "no codex"})

    worker._dispatch_codex_session(session, [{"content": "do it"}])

    assert stub.calls == [("cx-bn", "do it")]  # guard skipped → execute ran
    assert worker.session_store.get("cx-bn").status == STATUS_FAILED
    kinds = [e["kind"] for e in worker.transcript_store.read(session.session_id)]
    assert "codex_reexecute_averted" not in kinds


def test_codex_failed_finalizes_session_row(tmp_path: Path):
    """A FAILED codex outcome persists the terminal status to the session row, so
    an operator codex session isn't left CLAIMED and re-dispatched every tick."""
    plain_sends: list[str] = []
    withid_sends: list[str] = []
    stub = _StubCodexExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason="boom"))
    worker = _make_worker(tmp_path, codex_executor=stub, plain_sends=plain_sends, withid_sends=withid_sends)
    session = worker.session_store.create(task_id="cx-fail", routing="codex", origin="operator")

    worker._dispatch_codex_session(session, [{"content": "do a thing"}])

    assert stub.calls == [("cx-fail", "do a thing")]
    assert worker.session_store.get("cx-fail").status == STATUS_FAILED
    assert any("failed" in s.lower() for s in plain_sends)


# ---------------------------------------------------------------------------
# Web-thread result mirroring (#311). Codex has no rich [NOTIFY] stream, so the
# terminal completion/failure mirror is the whole web round-trip for it.
# ---------------------------------------------------------------------------


def _mirroring_codex_worker(tmp_path: Path, codex_executor):
    conv_store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    worker = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        conversation_store=conv_store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: True,
        telegram_send_with_id=lambda text: [777],
        http_client=client,
        codex_executor=codex_executor,
    )
    return worker, conv_store


def test_codex_completion_mirrors_into_linked_conversation(tmp_path: Path):
    stub = _StubCodexExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="Done: 3 events.")
    )
    worker, conv_store = _mirroring_codex_worker(tmp_path, codex_executor=stub)
    session = worker.session_store.create(task_id="cx-web", routing="codex", origin="operator")
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, session.session_id)

    worker._dispatch_codex_session(session, [{"content": "events?"}])

    msgs = conv_store.get_messages(conv.id)
    assert any(m.role == "assistant" and "Done: 3 events." in m.content for m in msgs)


def test_codex_completion_does_not_mirror_when_unlinked(tmp_path: Path):
    """AC2: a Telegram-origin codex session is unlinked → no conversation write."""
    stub = _StubCodexExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="Done.")
    )
    worker, conv_store = _mirroring_codex_worker(tmp_path, codex_executor=stub)
    session = worker.session_store.create(task_id="cx-tg", routing="codex", origin="operator")
    conv = conv_store.create_conversation(title="Some other thread")  # not linked

    worker._dispatch_codex_session(session, [{"content": "events?"}])

    assert conv_store.get_messages(conv.id) == []


def test_codex_failure_mirrors_notice_into_linked_conversation(tmp_path: Path):
    stub = _StubCodexExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason="boom"))
    worker, conv_store = _mirroring_codex_worker(tmp_path, codex_executor=stub)
    session = worker.session_store.create(task_id="cx-web-fail", routing="codex", origin="operator")
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, session.session_id)

    worker._dispatch_codex_session(session, [{"content": "do it"}])

    msgs = conv_store.get_messages(conv.id)
    assert any(m.role == "assistant" and "failed" in m.content.lower() for m in msgs)


def test_codex_child_completion_does_not_mirror(tmp_path: Path):
    """A codex child (has a parent — codex is in SPAWN_MODELS, so it IS
    dispatchable as a child) must not mirror its result into a conversation,
    even one linked to the child. Parity with the claude_code child gate."""
    stub = _StubCodexExecutor(
        outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="child codex result")
    )
    worker, conv_store = _mirroring_codex_worker(tmp_path, codex_executor=stub)
    parent = worker.session_store.create(task_id="cx-parent", routing="local")
    child = worker.session_store.create(
        task_id="cx-child", routing="codex", parent_session_id=parent.session_id,
    )
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, child.session_id)

    worker._dispatch_codex_session(child, [{"content": "do it"}])

    assert conv_store.get_messages(conv.id) == []


def test_codex_child_failure_does_not_mirror(tmp_path: Path):
    """A codex child's failure/budget notice must not mirror into a linked
    conversation either (parity with the claude_code failure gate)."""
    stub = _StubCodexExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason="boom"))
    worker, conv_store = _mirroring_codex_worker(tmp_path, codex_executor=stub)
    parent = worker.session_store.create(task_id="cx-parent-2", routing="local")
    child = worker.session_store.create(
        task_id="cx-child-2", routing="codex", parent_session_id=parent.session_id,
    )
    conv = conv_store.create_conversation(title="Web thread")
    conv_store.set_agent_session_id(conv.id, child.session_id)

    worker._dispatch_codex_session(child, [{"content": "do it"}])

    assert conv_store.get_messages(conv.id) == []
