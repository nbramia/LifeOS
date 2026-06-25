"""Tests for the doctor bot — the self-repair orchestration surface (#348).

Covers the four moving parts of bot-identity threading:
  1. Registry: the `doctor` entry loads with `orchestrates=True`.
  2. session_store: `bot` round-trips on sessions; pending-question reply
     matching is scoped by bot (no cross-bot message-id collisions).
  3. spawn: `spawn_claude_code_session(bot=...)` persists the bot on the row
     and in the pending-message payload.
  4. worker: a doctor session's BLOCKED notice routes through a bot-bound
     sender and registers a `bot='doctor'` pending question.
  5. telegram listener: the doctor bot spawns/owns a Claude Code session
     instead of redirecting to chat; pure-chat bots are unaffected.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import httpx
import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Registry — the doctor entry and the `orchestrates` flag
# ---------------------------------------------------------------------------

class TestDoctorRegistry:
    def test_real_registry_has_orchestrating_doctor(self):
        """The shipped config/telegram_bots.json declares the doctor bot as an
        orchestration bot (so a fresh clone wires it correctly once the token
        is set)."""
        entries = json.loads(Path("config/telegram_bots.json").read_text())
        doctor = next((e for e in entries if e.get("name") == "doctor"), None)
        assert doctor is not None, "doctor entry missing from telegram_bots.json"
        assert doctor.get("orchestrates") is True
        assert doctor.get("token_env") == "TELEGRAM_DOCTOR_BOT_TOKEN"
        assert doctor.get("persona_file") == "config/personas/doctor.md"

    def test_loader_reads_orchestrates(self, tmp_path, monkeypatch):
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([
            {"name": "doctor", "token_env": "TG_DOC", "orchestrates": True},
            {"name": "fitness", "token_env": "TG_FIT"},  # defaults to False
        ]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_DOC", "doc-token")
        monkeypatch.setenv("TG_FIT", "fit-token")
        from config.settings import settings
        bots = {b.name: b for b in settings.telegram_bots}
        assert bots["doctor"].orchestrates is True
        assert bots["fitness"].orchestrates is False

    def test_persona_file_encodes_orchestration_contract(self):
        """The doctor persona must carry the actual goal-first workflow, not a stub.

        Beyond the original pipeline anchors, the goal-first rewrite (#397) wires
        in the enabler primitives: a [GOAL] gate, the integration-branch flow,
        pre-flight worktree cleanup, the detached restart primitive, and the
        configurable /implement base. These needles guard against the contract
        silently regressing to the old inline-implement shape.
        """
        text = Path("config/personas/doctor.md").read_text().lower()
        for needle in (
            "/draft-issue", "/implement", "worktree", "[clarify]", "[notify]", "restart",
            "[goal]", "integration branch", "cleanup-worktrees.sh",
            "restart-worker-detached", "--base",
        ):
            assert needle in text, f"doctor persona missing '{needle}'"


# ---------------------------------------------------------------------------
# 2. session_store — bot round-trip + bot-scoped reply matching
# ---------------------------------------------------------------------------

class TestSessionStoreBotScoping:
    def _store(self, tmp_path):
        from api.services.agent_worker.session_store import SessionStore
        return SessionStore(db_path=tmp_path / "sessions.db")

    def test_bot_round_trips_on_session(self, tmp_path):
        store = self._store(tmp_path)
        store.create(task_id="t1", routing="claude_code", origin="operator", bot="doctor")
        s = store.get_by_session_id(store.get("t1").session_id)
        assert s.bot == "doctor"

    def test_bot_defaults_null_for_primary(self, tmp_path):
        store = self._store(tmp_path)
        store.create(task_id="t1", routing="claude_code", origin="operator")
        assert store.get("t1").bot is None

    def test_reply_matching_is_bot_scoped(self, tmp_path):
        """Two questions share a numeric message id across bots; a reply must
        only match its own bot's row."""
        store = self._store(tmp_path)
        store.create(task_id="doc", routing="claude_code", origin="operator", bot="doctor")
        store.create(task_id="pri", routing="claude_code", origin="operator")
        doc_sid = store.get("doc").session_id
        pri_sid = store.get("pri").session_id
        store.create_pending_question(
            session_id=doc_sid, task_id="doc", question="q", sent_message_id=500,
            kind="followup", bot="doctor",
        )
        store.create_pending_question(
            session_id=pri_sid, task_id="pri", question="q", sent_message_id=500,
            kind="followup", bot=None,  # primary / legacy
        )
        # A doctor reply to msg 500 matches only the doctor row.
        q = store.get_open_question_by_message_id(500, bot="doctor")
        assert q is not None and q["session_id"] == doc_sid
        # A primary reply to msg 500 matches the NULL-bot (primary) row.
        q = store.get_open_question_by_message_id(500, bot="primary")
        assert q is not None and q["session_id"] == pri_sid

    def test_primary_matches_legacy_null_bot(self, tmp_path):
        """bot='primary' must still match pre-#348 rows that have NULL bot."""
        store = self._store(tmp_path)
        store.create(task_id="pri", routing="claude_code", origin="operator")
        sid = store.get("pri").session_id
        store.create_pending_question(
            session_id=sid, task_id="pri", question="q", sent_message_id=42,
            kind="followup", bot=None,
        )
        assert store.deposit_answer(42, "yes", bot="primary") is True

    def test_doctor_reply_does_not_match_primary_question(self, tmp_path):
        store = self._store(tmp_path)
        store.create(task_id="pri", routing="claude_code", origin="operator")
        sid = store.get("pri").session_id
        store.create_pending_question(
            session_id=sid, task_id="pri", question="q", sent_message_id=99,
            kind="followup", bot=None,
        )
        # Doctor reply must NOT consume the primary's question.
        assert store.deposit_answer(99, "yes", bot="doctor") is False
        assert store.get_open_question_by_message_id(99, bot="doctor") is None

    def test_unscoped_lookup_preserves_legacy_behavior(self, tmp_path):
        """bot=None (no scoping) matches regardless of the row's bot — the
        contract existing callers rely on."""
        store = self._store(tmp_path)
        store.create(task_id="doc", routing="claude_code", origin="operator", bot="doctor")
        sid = store.get("doc").session_id
        store.create_pending_question(
            session_id=sid, task_id="doc", question="q", sent_message_id=7,
            kind="followup", bot="doctor",
        )
        assert store.get_open_question_by_message_id(7) is not None


# ---------------------------------------------------------------------------
# 3. spawn — bot persisted on the row and in the payload
# ---------------------------------------------------------------------------

class TestSpawnCarriesBot:
    def test_spawn_persists_bot_on_session_and_payload(self, tmp_path):
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.claude_code_spawn import (
            spawn_claude_code_session, parse_claude_code_spawn_payload,
        )
        store = SessionStore(db_path=tmp_path / "sessions.db")
        result = spawn_claude_code_session(
            store, "fix the sync bug", working_dir="/tmp/x", chat_id="123", bot="doctor",
        )
        assert result["ok"]
        session = store.get_by_session_id(result["session_id"])
        assert session.bot == "doctor"
        # The enqueued operator message carries the bot for the worker dispatch.
        pending = store.drain_pending_messages(result["session_id"])
        payload = parse_claude_code_spawn_payload(pending[0]["content"])
        assert payload["bot"] == "doctor"

    def test_spawn_without_bot_defaults_primary(self, tmp_path):
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        store = SessionStore(db_path=tmp_path / "sessions.db")
        result = spawn_claude_code_session(store, "do a thing", chat_id="1")
        assert store.get_by_session_id(result["session_id"]).bot is None

    def test_parse_payload_defaults_bot_none_for_bare_prompt(self):
        from api.services.agent_worker.claude_code_spawn import parse_claude_code_spawn_payload
        assert parse_claude_code_spawn_payload("just a prompt")["bot"] is None


# ---------------------------------------------------------------------------
# 4. worker — doctor session notices route through a bot-bound sender
# ---------------------------------------------------------------------------

class TestWorkerRoutesByBot:
    def _make_worker(self, tmp_path, executor):
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.spend_tracker import SpendTracker
        from api.services.agent_worker.transcript_store import TranscriptStore
        from api.services.agent_worker.worker import Worker, _SynchronousPool

        transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
        client = httpx.Client(transport=transport, base_url="http://api")
        sent_with_ids: list[tuple] = []
        sent: list[tuple] = []

        def _send_with_id(text, chat_id=None, bot=None):
            msg_id = len(sent_with_ids) + 5000
            sent_with_ids.append((msg_id, text, bot))
            return [msg_id]

        def _send(text, chat_id=None, bot=None):
            sent.append((text, bot))
            return True

        w = Worker(
            api_base="http://api",
            session_store=SessionStore(db_path=tmp_path / "sessions.db"),
            transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
            spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
            poll_seconds=0.01,
            telegram_send=_send,
            telegram_send_with_id=_send_with_id,
            http_client=client,
            claude_code_executor=executor,
            cli_pool=_SynchronousPool(),
        )
        w._sent = sent  # type: ignore[attr-defined]
        w._sent_with_ids = sent_with_ids  # type: ignore[attr-defined]
        return w

    def test_blocked_doctor_session_routes_to_doctor_bot(self, tmp_path):
        from dataclasses import dataclass
        from api.services.agent_worker.claude_code_executor import (
            REASON_AWAITING_CLARIFICATION,
        )
        from api.services.agent_worker.local_executor import ExecutorOutcome
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import STATUS_BLOCKED

        @dataclass
        class _Stub:
            outcome: ExecutorOutcome
            def execute(self, session, task):
                return self.outcome
            def resume(self, session, message, working_dir=None):
                return self.outcome

        stub = _Stub(ExecutorOutcome(
            status=STATUS_BLOCKED, reason=REASON_AWAITING_CLARIFICATION, final_text="which file?",
        ))
        w = self._make_worker(tmp_path, stub)
        result = spawn_claude_code_session(
            w.session_store, "fix it", chat_id="123", bot="doctor",
        )
        w._dispatch_spawned_sessions()

        # The reply prompt went out tagged to the doctor bot...
        assert w._sent_with_ids, "no id-captured message was sent"
        assert w._sent_with_ids[-1][2] == "doctor"
        # ...and the registered pending question is scoped to the doctor bot.
        q = w.session_store.get_open_question_by_message_id(
            w._sent_with_ids[-1][0], bot="doctor",
        )
        assert q is not None
        assert q["session_id"] == result["session_id"]

    def test_get_executor_caches_per_bot(self, tmp_path):
        """Without an injected executor, each bot gets its own executor
        instance (so notification callbacks stay bot-bound)."""
        w = self._make_worker(tmp_path, executor=None)
        w._claude_code_executor = None  # force the production lazy path
        doc = w._get_claude_code_executor("doctor")
        pri = w._get_claude_code_executor(None)
        again = w._get_claude_code_executor("doctor")
        assert doc is again
        assert doc is not pri


# ---------------------------------------------------------------------------
# 5. telegram listener — doctor spawns/owns sessions; chat bots unaffected
# ---------------------------------------------------------------------------

class _DummyTyping:
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class TestDoctorListener:
    def _listener(self, name, chat_id, persona="", orchestrates=False):
        from api.services.telegram import TelegramBotListener
        from config.settings import TelegramBotConfig
        bot = TelegramBotConfig(
            name=name, token="TOK", chat_id=chat_id, persona=persona, orchestrates=orchestrates,
        )
        return TelegramBotListener(bot)

    def test_doctor_owns_agent_sessions(self):
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        assert listener._owns_agent_sessions is True
        assert listener._bot.orchestrates is True

    def test_pure_chat_bot_does_not_own_sessions(self):
        listener = self._listener("fitness", "999", persona="P", orchestrates=False)
        assert listener._owns_agent_sessions is False

    @pytest.mark.asyncio
    async def test_doctor_message_spawns_session_not_chat(self):
        listener = self._listener("doctor", "999", persona="DOCTOR CONTRACT", orchestrates=True)
        update = {"message": {"text": "search is broken", "chat": {"id": 999}, "message_id": 1}}

        spawn = MagicMock(return_value={"ok": True, "session_id": "sess_x", "task_id": "t"})
        with patch("api.services.agent_worker.claude_code_spawn.spawn_claude_code_session", spawn), \
             patch("api.services.agent_worker.session_store.SessionStore"), \
             patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock), \
             patch("api.services.telegram.TypingIndicator", _DummyTyping), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            await listener._handle_update(update)

        # Doctor drives a Claude Code session, never the chat pipeline.
        mock_chat.assert_not_called()
        spawn.assert_called_once()
        kwargs = spawn.call_args.kwargs
        assert kwargs["bot"] == "doctor"
        assert kwargs["working_dir"].endswith("/LifeOS")
        # The persona is the orchestration prompt prefix; the report is appended.
        prompt = spawn.call_args.args[1]
        assert "DOCTOR CONTRACT" in prompt
        assert "search is broken" in prompt

    @pytest.mark.asyncio
    async def test_doctor_threaded_reply_runs_resume_hook(self):
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        update = {"message": {
            "text": "yes",
            "chat": {"id": 999},
            "message_id": 2,
            "reply_to_message": {"message_id": 5000},
        }}
        with patch.object(listener, "_maybe_handle_claude_code_reply",
                          new_callable=AsyncMock, return_value=True) as mock_resume, \
             patch.object(listener, "_handle_orchestration_message", new_callable=AsyncMock) as mock_spawn, \
             patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock):
            await listener._handle_update(update)
        # The reply resumed the session; it did NOT spawn a fresh one.
        mock_resume.assert_awaited_once()
        mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_pure_chat_bot_never_spawns(self):
        listener = self._listener("fitness", "999", persona="P", orchestrates=False)
        update = {"message": {"text": "bench 135x8", "chat": {"id": 999}, "message_id": 3}}
        with patch.object(listener, "_handle_orchestration_message", new_callable=AsyncMock) as mock_spawn, \
             patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock), \
             patch("api.services.telegram.TypingIndicator", _DummyTyping), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"answer": "Logged", "conversation_id": "c1"}
            await listener._handle_update(update)
        mock_spawn.assert_not_called()
        mock_chat.assert_awaited_once()  # pure chat, as before


# ---------------------------------------------------------------------------
# 6. worker — a BLOCKED session whose reply-prompt can't be delivered escalates
#    instead of hanging BLOCKED forever (#402)
# ---------------------------------------------------------------------------

class TestBlockedSessionEscalation:
    def _worker(self, tmp_path, monkeypatch, executor, send_with_id):
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.spend_tracker import SpendTracker
        from api.services.agent_worker.transcript_store import TranscriptStore
        from api.services.agent_worker import worker as worker_mod
        from api.services.agent_worker.worker import Worker, _SynchronousPool

        # No real sleeps between retries.
        monkeypatch.setattr(worker_mod, "_BLOCKED_PROMPT_RETRY_DELAY_S", 0)

        transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
        client = httpx.Client(transport=transport, base_url="http://api")
        escalations: list = []

        def _send(text, chat_id=None, bot=None):
            escalations.append((text, bot))
            return True

        w = Worker(
            api_base="http://api",
            session_store=SessionStore(db_path=tmp_path / "sessions.db"),
            transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
            spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
            poll_seconds=0.01,
            telegram_send=_send,
            telegram_send_with_id=send_with_id,
            http_client=client,
            claude_code_executor=executor,
            cli_pool=_SynchronousPool(),
        )
        w._escalations = escalations  # type: ignore[attr-defined]
        return w

    def _blocked_stub(self):
        from dataclasses import dataclass
        from api.services.agent_worker.claude_code_executor import REASON_AWAITING_CLARIFICATION
        from api.services.agent_worker.local_executor import ExecutorOutcome
        from api.services.agent_worker.session_store import STATUS_BLOCKED

        @dataclass
        class _Stub:
            outcome: ExecutorOutcome
            def execute(self, session, task):
                return self.outcome
            def resume(self, session, message, working_dir=None):
                return self.outcome

        return _Stub(ExecutorOutcome(
            status=STATUS_BLOCKED, reason=REASON_AWAITING_CLARIFICATION, final_text="which file?",
        ))

    def test_undeliverable_clarification_escalates_and_fails(self, tmp_path, monkeypatch):
        """A reply-prompt send that always raises is retried the bounded count,
        then the session is marked FAILED (not left silently BLOCKED) and the
        owning bot's surface gets a best-effort escalation."""
        from api.services.agent_worker import worker as worker_mod
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import STATUS_FAILED

        attempts = {"n": 0}

        def _raises(text, chat_id=None, bot=None):
            attempts["n"] += 1
            raise RuntimeError("telegram down")

        w = self._worker(tmp_path, monkeypatch, self._blocked_stub(), _raises)
        result = spawn_claude_code_session(w.session_store, "fix it", chat_id="123", bot="doctor")
        w._dispatch_spawned_sessions()

        assert attempts["n"] == worker_mod._BLOCKED_PROMPT_SEND_ATTEMPTS
        sess = w.session_store.get_by_session_id(result["session_id"])
        # FAILED is the disposition — no resumable BLOCKED zombie left behind.
        assert sess.status == STATUS_FAILED
        assert w._escalations and w._escalations[-1][1] == "doctor"

    def test_empty_send_result_also_escalates(self, tmp_path, monkeypatch):
        """A send that returns no message ids (without raising) is also a
        delivery failure — no reply anchor — so it escalates too."""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import STATUS_FAILED

        def _empty(text, chat_id=None, bot=None):
            return []

        w = self._worker(tmp_path, monkeypatch, self._blocked_stub(), _empty)
        result = spawn_claude_code_session(w.session_store, "fix it", chat_id="123", bot="doctor")
        w._dispatch_spawned_sessions()

        sess = w.session_store.get_by_session_id(result["session_id"])
        assert sess.status == STATUS_FAILED
        assert w._escalations and w._escalations[-1][1] == "doctor"

    def test_retry_succeeds_on_second_attempt_registers_anchor(self, tmp_path, monkeypatch):
        """A transient send failure that recovers on retry registers the reply
        anchor and does NOT escalate or fail the session. (The stub executor
        doesn't set BLOCKED the way the real one does, so we assert on the
        observable worker effects: the anchor exists and nothing escalated.)"""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session

        calls = {"n": 0}

        def _flaky(text, chat_id=None, bot=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient blip")
            return [7000]

        w = self._worker(tmp_path, monkeypatch, self._blocked_stub(), _flaky)
        result = spawn_claude_code_session(w.session_store, "fix it", chat_id="123", bot="doctor")
        w._dispatch_spawned_sessions()

        assert calls["n"] == 2  # failed once, then succeeded
        # The reply anchor was registered (so the operator can resume)...
        assert w.session_store.get_open_question_by_message_id(7000, bot="doctor") is not None
        # ...and the success path did NOT escalate or mark the session failed.
        assert w._escalations == []
        assert w.session_store.get_by_session_id(result["session_id"]).status != "failed"


# ---------------------------------------------------------------------------
# 6. [GOAL] tag → worker /goal injection on approval (#398)
# ---------------------------------------------------------------------------

class TestGoalApproval:
    def _make_worker(self, tmp_path, executor):
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.spend_tracker import SpendTracker
        from api.services.agent_worker.transcript_store import TranscriptStore
        from api.services.agent_worker.worker import Worker, _SynchronousPool

        transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
        client = httpx.Client(transport=transport, base_url="http://api")
        sent_with_ids: list[tuple] = []

        def _send_with_id(text, chat_id=None, bot=None):
            msg_id = len(sent_with_ids) + 6000
            sent_with_ids.append((msg_id, text, bot))
            return [msg_id]

        def _send(text, chat_id=None, bot=None):
            return True

        w = Worker(
            api_base="http://api",
            session_store=SessionStore(db_path=tmp_path / "sessions.db"),
            transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
            spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
            poll_seconds=0.01,
            telegram_send=_send,
            telegram_send_with_id=_send_with_id,
            http_client=client,
            claude_code_executor=executor,
            cli_pool=_SynchronousPool(),
        )
        w._sent_with_ids = sent_with_ids  # type: ignore[attr-defined]
        return w

    def _goal_blocked_stub(self):
        from dataclasses import dataclass
        from api.services.agent_worker.claude_code_executor import REASON_AWAITING_GOAL_APPROVAL
        from api.services.agent_worker.local_executor import ExecutorOutcome
        from api.services.agent_worker.session_store import STATUS_BLOCKED

        @dataclass
        class _Stub:
            outcome: ExecutorOutcome
            def execute(self, session, task):
                return self.outcome
            def resume(self, session, message, working_dir=None):
                return self.outcome

        return _Stub(ExecutorOutcome(
            status=STATUS_BLOCKED,
            reason=REASON_AWAITING_GOAL_APPROVAL,
            final_text="all tests pass",
        ))

    def test_is_affirmative_recognizes_yes_and_approve(self):
        from api.services.agent_worker.worker import _is_affirmative
        assert _is_affirmative("yes")
        assert _is_affirmative("Yes!")
        assert _is_affirmative("approve")
        assert _is_affirmative("Approved.")
        assert _is_affirmative("lock it")
        assert _is_affirmative("go ahead")
        assert _is_affirmative("sounds good")
        assert _is_affirmative("sure")
        assert _is_affirmative("yes please")

    def test_is_affirmative_rejects_refinements(self):
        from api.services.agent_worker.worker import _is_affirmative
        assert not _is_affirmative("no, make it stricter")
        assert not _is_affirmative("change it to all tests AND lint pass")
        assert not _is_affirmative("hmm")
        # "yes but ..." / "approve with changes" must NOT lock the stale goal —
        # the refinement signal wins over the affirmative prefix (#406 review).
        assert not _is_affirmative("yes but make it stricter")
        assert not _is_affirmative("approve with changes: also require lint")
        assert not _is_affirmative("yes, also require lint")

    def test_goal_block_registers_goal_approval_question(self, tmp_path):
        """A goal-approval BLOCKED outcome registers a kind='goal_approval'
        pending question scoped to the doctor bot, with the goal prompt."""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        result = spawn_claude_code_session(
            w.session_store, "make the suite green", chat_id="123", bot="doctor",
        )
        w._dispatch_spawned_sessions()

        assert w._sent_with_ids, "no id-captured message was sent"
        msg_id, text, bot = w._sent_with_ids[-1]
        assert bot == "doctor"
        assert "lock this goal" in text.lower()
        q = w.session_store.get_open_question_by_message_id(msg_id, bot="doctor")
        assert q is not None
        assert q["kind"] == "goal_approval"
        assert q["session_id"] == result["session_id"]

    def _seed_blocked_goal_session(self, w, *, condition="all tests pass"):
        """Drop a BLOCKED doctor session with a pending (proposed-not-locked)
        goal in its transcript, plus an open goal_approval question — the shape
        left behind after a goal-block round-trips through dispatch."""
        from api.services.agent_worker.session_store import STATUS_BLOCKED

        session = w.session_store.create(
            task_id="task-goal-1",
            routing="claude_code",
            origin="operator",
            bot="doctor",
            status=STATUS_BLOCKED,
        )
        w.session_store.set_claude_code_session_id(session.task_id, "cli-goal-1")
        w.transcript_store.append(
            session.session_id, "claude_code_awaiting_goal_approval",
            {"condition": condition, "condition_chars": len(condition)},
        )
        qid = w.session_store.create_pending_question(
            session_id=session.session_id,
            task_id=session.task_id,
            question="Reply 'yes' to lock this goal and start, or send changes to refine it.",
            sent_message_id=9100,
            sent_message_ids=[9100],
            kind="goal_approval",
            bot="doctor",
        )
        return session, qid

    def test_affirmative_reply_injects_slash_goal(self, tmp_path):
        """An affirmative reply enqueues `/goal <condition>` as the resume
        message and records a goal_locked transcript event."""
        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session, _ = self._seed_blocked_goal_session(w, condition="all tests pass")

        # Operator replies 'yes' on the goal-approval message.
        assert w.session_store.deposit_answer(9100, "yes", bot="doctor") is True
        w._process_clarification_answers()

        # The resume message injected is the native /goal command.
        pending = w.session_store.drain_pending_messages(session.session_id)
        assert [m["content"] for m in pending] == ["/goal all tests pass"]
        # The session is flipped back to CLAIMED for re-dispatch.
        from api.services.agent_worker.session_store import STATUS_CLAIMED
        assert w.session_store.get_by_session_id(session.session_id).status == STATUS_CLAIMED
        # A goal_locked event was recorded (the refine event was not).
        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "claude_code_goal_locked" in kinds
        assert "claude_code_goal_refine" not in kinds

    def test_refinement_reply_passes_raw_answer_without_locking(self, tmp_path):
        """A non-affirmative reply is a refinement: the raw answer is enqueued
        (so the doctor re-proposes) and NO goal_locked event is written."""
        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session, _ = self._seed_blocked_goal_session(w, condition="all tests pass")

        answer = "make it all tests AND lint pass"
        assert w.session_store.deposit_answer(9100, answer, bot="doctor") is True
        w._process_clarification_answers()

        pending = w.session_store.drain_pending_messages(session.session_id)
        assert [m["content"] for m in pending] == [answer]
        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "claude_code_goal_locked" not in kinds
        assert "claude_code_goal_refine" in kinds

    def test_affirmative_without_recoverable_condition_reprompts(self, tmp_path):
        """An affirmative reply when the proposed condition can't be recovered
        (e.g. it was already locked) must NOT forward a bare 'yes' — it asks the
        agent to re-emit the [GOAL], and records a goal_lock_failed event."""
        from api.services.agent_worker.session_store import STATUS_BLOCKED

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session = w.session_store.create(
            task_id="task-goal-nofind",
            routing="claude_code",
            origin="operator",
            bot="doctor",
            status=STATUS_BLOCKED,
        )
        w.session_store.set_claude_code_session_id(session.task_id, "cli-goal-nf")
        # A proposal that was already locked → _pending_goal_condition returns None.
        w.transcript_store.append(
            session.session_id, "claude_code_awaiting_goal_approval",
            {"condition": "all tests pass", "condition_chars": 14},
        )
        w.transcript_store.append(
            session.session_id, "claude_code_goal_locked", {"condition_chars": 14},
        )
        w.session_store.create_pending_question(
            session_id=session.session_id,
            task_id=session.task_id,
            question="Reply 'yes' to lock this goal and start, or send changes to refine it.",
            sent_message_id=9200,
            sent_message_ids=[9200],
            kind="goal_approval",
            bot="doctor",
        )

        assert w.session_store.deposit_answer(9200, "yes", bot="doctor") is True
        w._process_clarification_answers()

        pending = w.session_store.drain_pending_messages(session.session_id)
        assert len(pending) == 1
        msg = pending[0]["content"]
        assert not msg.startswith("/goal")  # no stale/bare command forwarded
        assert msg != "yes"
        assert "re-emit" in msg.lower()
        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "claude_code_goal_lock_failed" in kinds

    def test_goal_condition_survives_restart_and_reinjects(self, tmp_path):
        """The #398 acceptance criterion: the proposed condition is durable via
        the transcript (no DB column). A fresh Worker over the SAME paths
        (simulating a restart) still injects `/goal <condition>` on approval."""
        from api.services.agent_worker.session_store import STATUS_BLOCKED, STATUS_CLAIMED

        # First worker: seed the blocked goal + answered approval question.
        w1 = self._make_worker(tmp_path, self._goal_blocked_stub())
        session = w1.session_store.create(
            task_id="task-goal-restart",
            routing="claude_code",
            origin="operator",
            bot="doctor",
            status=STATUS_BLOCKED,
        )
        w1.session_store.set_claude_code_session_id(session.task_id, "cli-goal-rs")
        w1.transcript_store.append(
            session.session_id, "claude_code_awaiting_goal_approval",
            {"condition": "all tests pass", "condition_chars": 14},
        )
        w1.session_store.create_pending_question(
            session_id=session.session_id,
            task_id=session.task_id,
            question="Reply 'yes' to lock this goal and start, or send changes to refine it.",
            sent_message_id=9300,
            sent_message_ids=[9300],
            kind="goal_approval",
            bot="doctor",
        )
        assert w1.session_store.deposit_answer(9300, "yes", bot="doctor") is True

        # Simulate a worker restart: a brand-new Worker over the same DB +
        # transcript dir processes the still-unprocessed answered question.
        w2 = self._make_worker(tmp_path, self._goal_blocked_stub())
        w2._process_clarification_answers()

        pending = w2.session_store.drain_pending_messages(session.session_id)
        assert [m["content"] for m in pending] == ["/goal all tests pass"]
        assert w2.session_store.get_by_session_id(session.session_id).status == STATUS_CLAIMED

    def test_pending_goal_condition_returns_latest_after_relock_cycle(self, tmp_path):
        """A later awaiting_goal_approval supersedes an earlier locked one:
        awaiting{A} → locked → awaiting{B} resolves to B."""
        from api.services.agent_worker.session_store import STATUS_BLOCKED

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session = w.session_store.create(
            task_id="task-goal-cycle",
            routing="claude_code",
            origin="operator",
            bot="doctor",
            status=STATUS_BLOCKED,
        )
        sid = session.session_id
        w.transcript_store.append(sid, "claude_code_awaiting_goal_approval",
                                  {"condition": "A", "condition_chars": 1})
        w.transcript_store.append(sid, "claude_code_goal_locked", {"condition_chars": 1})
        w.transcript_store.append(sid, "claude_code_awaiting_goal_approval",
                                  {"condition": "B", "condition_chars": 1})
        assert w._pending_goal_condition(sid) == "B"
