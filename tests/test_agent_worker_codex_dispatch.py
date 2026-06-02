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
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker


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
