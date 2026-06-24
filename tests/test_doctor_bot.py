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
        """The doctor persona must carry the actual workflow, not be a stub."""
        text = Path("config/personas/doctor.md").read_text().lower()
        for needle in ("/draft-issue", "/implement", "worktree", "[clarify]", "[notify]", "restart"):
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
    def _worker(self, tmp_path, monkeypatch, executor):
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
        attempts = {"n": 0}

        def _send(text, chat_id=None, bot=None):
            escalations.append((text, bot))
            return True

        def _send_with_id(text, chat_id=None, bot=None):
            attempts["n"] += 1
            raise RuntimeError("telegram down")

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
        w._escalations = escalations  # type: ignore[attr-defined]
        w._send_attempts = attempts  # type: ignore[attr-defined]
        return w

    def test_undeliverable_clarification_escalates_and_fails(self, tmp_path, monkeypatch):
        from dataclasses import dataclass
        from api.services.agent_worker import worker as worker_mod
        from api.services.agent_worker.claude_code_executor import REASON_AWAITING_CLARIFICATION
        from api.services.agent_worker.local_executor import ExecutorOutcome
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import STATUS_BLOCKED, STATUS_FAILED

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
        w = self._worker(tmp_path, monkeypatch, stub)
        result = spawn_claude_code_session(
            w.session_store, "fix it", chat_id="123", bot="doctor",
        )
        w._dispatch_spawned_sessions()

        # The prompt send was retried the bounded number of times...
        assert w._send_attempts["n"] == worker_mod._BLOCKED_PROMPT_SEND_ATTEMPTS
        # ...then the session was marked FAILED rather than left silently BLOCKED.
        sess = w.session_store.get_by_session_id(result["session_id"])
        assert sess.status == STATUS_FAILED
        # No reply anchor was registered (there was no deliverable message id).
        assert w.session_store.get_open_question_by_message_id(5000, bot="doctor") is None
        # The owning (doctor) surface got a best-effort escalation notice.
        assert w._escalations and w._escalations[-1][1] == "doctor"
