"""Tests for reconciling /code with the unified follow-up table (#237, Phase 4).

Covers: the orchestrator on_complete hook, registering a Claude Code completion
in the shared `pending_questions` table (kind='code_followup'), any-chunk
lookup, the Telegram reply hook routing /code replies to the orchestrator's
resume path, and the worker skipping code_followup rows.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit


def _make_listener(tmp_path):
    from api.services.telegram import TelegramBotListener
    state_file = tmp_path / "telegram_state.json"
    with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
        return TelegramBotListener()


class _FakeSession:
    def __init__(self, session_id="claude_abc", task="fix the bug"):
        self.session_id = session_id
        self.task = task


# ---------------------------------------------------------------------------
# Orchestrator on_complete hook
# ---------------------------------------------------------------------------


def test_on_complete_fires_on_completion():
    from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

    orch = ClaudeOrchestrator()
    sess = ClaudeSession(session_id="s1", task="t", status="running")
    orch._active_session = sess
    called = []
    orch._on_complete = lambda s: called.append(s)

    orch._cleanup("completed")
    assert called == [sess]


def test_on_complete_does_not_fire_on_failure():
    from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

    orch = ClaudeOrchestrator()
    sess = ClaudeSession(session_id="s2", task="t", status="running")
    orch._active_session = sess
    called = []
    orch._on_complete = lambda s: called.append(s)

    orch._cleanup("failed")
    assert called == []


def test_on_complete_skipped_without_session_id():
    from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

    orch = ClaudeOrchestrator()
    sess = ClaudeSession(session_id=None, task="t", status="running")
    orch._active_session = sess
    called = []
    orch._on_complete = lambda s: called.append(s)

    orch._cleanup("completed")
    assert called == []  # no resumable session id → nothing to register


# ---------------------------------------------------------------------------
# Registration + any-chunk lookup
# ---------------------------------------------------------------------------


def test_register_code_followup_and_lookup(tmp_path: Path):
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    listener = _make_listener(tmp_path)
    with patch("api.services.agent_worker.session_store.SessionStore", return_value=store):
        listener._register_code_followup(_FakeSession("claude_abc", "fix the bug"), [10, 11, 12])

    # Reply to any chunk matches; the row carries the Claude session id + kind.
    q = store.get_open_question_by_message_id(12)
    assert q is not None
    assert q["kind"] == "code_followup"
    assert q["session_id"] == "claude_abc"
    assert q["question"] == "fix the bug"


def test_register_code_followup_noop_without_ids_or_session(tmp_path: Path):
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    listener = _make_listener(tmp_path)
    with patch("api.services.agent_worker.session_store.SessionStore", return_value=store):
        listener._register_code_followup(_FakeSession("c", "t"), [])  # no ids
        listener._register_code_followup(_FakeSession(None, "t"), [1])  # no session id
    assert store.get_open_question_by_message_id(1) is None


# ---------------------------------------------------------------------------
# Telegram reply routing
# ---------------------------------------------------------------------------


class _FakeOrch:
    def __init__(self, resume_ok=True):
        self.resume_ok = resume_ok
        self.followup_calls = []

    def followup(self, message, notification_callback=None, on_complete=None):
        self.followup_calls.append(message)
        return object() if self.resume_ok else None


@pytest.mark.asyncio
async def test_code_reply_resumes_via_orchestrator(tmp_path: Path):
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    store.create_pending_question(
        session_id="claude_abc", task_id="code_claude_abc", question="fix the bug",
        sent_message_id=20, sent_message_ids=[20, 21], kind="code_followup",
    )
    listener = _make_listener(tmp_path)
    orch = _FakeOrch(resume_ok=True)
    with patch("api.services.agent_worker.session_store.SessionStore", return_value=store), \
         patch("api.services.claude_orchestrator.get_orchestrator", return_value=orch), \
         patch("api.services.telegram.send_message_async", new_callable=AsyncMock) as mock_send:
        # Reply lands on the second chunk.
        handled = await listener._maybe_handle_code_reply(21, "now add tests", "123")

    assert handled is True
    assert orch.followup_calls == ["now add tests"]
    assert "Resuming Claude Code" in mock_send.await_args.args[0]
    # Row is closed so it isn't reprocessed.
    assert store.get_open_question_by_message_id(20) is None


@pytest.mark.asyncio
async def test_code_reply_unresumable_tells_user(tmp_path: Path):
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    store.create_pending_question(
        session_id="claude_abc", task_id="code_claude_abc", question="t",
        sent_message_id=30, kind="code_followup",
    )
    listener = _make_listener(tmp_path)
    orch = _FakeOrch(resume_ok=False)  # window expired / busy
    with patch("api.services.agent_worker.session_store.SessionStore", return_value=store), \
         patch("api.services.claude_orchestrator.get_orchestrator", return_value=orch), \
         patch("api.services.telegram.send_message_async", new_callable=AsyncMock) as mock_send:
        handled = await listener._maybe_handle_code_reply(30, "continue", "123")

    assert handled is True
    assert "can't be resumed" in mock_send.await_args.args[0]


@pytest.mark.asyncio
async def test_non_code_reply_not_handled(tmp_path: Path):
    """A reply matching an agent-worker follow-up (kind='followup') is NOT
    consumed by the code-reply hook — it falls through to the worker deposit."""
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    store.create_pending_question(
        session_id="sess_x", task_id="t1", question="",
        sent_message_id=40, kind="followup",
    )
    listener = _make_listener(tmp_path)
    with patch("api.services.agent_worker.session_store.SessionStore", return_value=store), \
         patch("api.services.telegram.send_message_async", new_callable=AsyncMock):
        handled = await listener._maybe_handle_code_reply(40, "x", "123")
    assert handled is False
    # Unconsumed — still open for the worker path.
    assert store.get_open_question_by_message_id(40) is not None


@pytest.mark.asyncio
async def test_no_match_returns_false(tmp_path: Path):
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore(db_path=tmp_path / "s.db")
    listener = _make_listener(tmp_path)
    with patch("api.services.agent_worker.session_store.SessionStore", return_value=store):
        handled = await listener._maybe_handle_code_reply(999, "x", "123")
    assert handled is False
