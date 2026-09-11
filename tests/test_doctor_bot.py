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
            "restart-worker-detached", "--base", "verify-deployed",
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
    async def test_doctor_fresh_message_routes_through_chat_pipeline(self, monkeypatch):
        """#684: doctor's direct-CC spawn entry (`_handle_orchestration_message`)
        is retired — a fresh message now flows through the same chat pipeline
        as any other bot, resolving its persona via `persona_id` so it reaches
        Hermes (which supervises its own workers via `lifeos_agent_spawn`,
        config/personas/doctor.hermes.md) exactly like fitness/therapist/
        finance/journal."""
        monkeypatch.setattr("api.services.telegram.settings.hermes_backend_url", "http://hermes")
        listener = self._listener("doctor", "999", persona="DOCTOR CONTRACT", orchestrates=True)
        update = {"message": {"text": "search is broken", "chat": {"id": 999}, "message_id": 1}}

        with patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock), \
             patch("api.services.telegram.TypingIndicator", _DummyTyping), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"answer": "On it.", "conversation_id": "c1"}
            await listener._handle_update(update)

        mock_chat.assert_awaited_once()
        assert mock_chat.call_args.kwargs.get("persona_id") == "doctor"
        assert mock_chat.call_args.kwargs.get("backend") == "hermes"
        assert not hasattr(listener, "_handle_orchestration_message")

    @pytest.mark.asyncio
    async def test_doctor_native_fallback_uses_persona_id_gated_spawn(self, monkeypatch):
        """With Hermes unavailable, doctor's fallback turn still goes to the
        native pipeline via `persona_id="doctor"` (not a raw preamble) — that
        is what makes `POST /api/ask/stream`'s own persona_id-gated
        orchestrating-persona spawn (api/routes/chat.py) fire, tagging the
        spawned session `bot="doctor"` for Telegram-thread parity, instead of
        an ordinary inline chat reply."""
        monkeypatch.setattr("api.services.telegram.settings.hermes_backend_url", "")
        listener = self._listener("doctor", "999", persona="DOCTOR CONTRACT", orchestrates=True)
        update = {"message": {"text": "search is broken", "chat": {"id": 999}, "message_id": 1}}

        with patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock), \
             patch("api.services.telegram.TypingIndicator", _DummyTyping), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"answer": "🩺 On it", "conversation_id": "c1"}
            await listener._handle_update(update)

        mock_chat.assert_awaited_once()
        assert mock_chat.call_args.kwargs.get("persona_id") == "doctor"
        assert mock_chat.call_args.kwargs.get("backend") == "lifeos"

    # ------------------------------------------------------------------
    # #453 guard, re-pinned for #684 (Codex adversarial review): the native
    # fallback path still reaches chat.py's persona_id-gated orchestration
    # spawn, which fires unconditionally for ANY message once persona_id
    # names an orchestrating bot — so a bare affirmative reaching the
    # NATIVE fallback (unlike the Hermes path, where spawning is the
    # model's own deliberate tool call) must still be intercepted before it
    # looks like a fresh "report". These replace the three tests removed
    # earlier in this file that exercised the retired
    # `_handle_orchestration_message` directly; the property they guarded
    # is the same, only the call path changed (`_native_turn` /
    # `_maybe_consume_bare_affirmative` in api/services/telegram.py).
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_doctor_native_fallback_bare_yes_routes_to_open_gate(self, tmp_path, monkeypatch):
        """A bare 'Yes' on the native fallback path, with an open
        goal_approval gate, resolves that gate — it must never reach
        chat_via_api (which would trigger chat.py's unconditional spawn)."""
        monkeypatch.setattr("api.services.telegram.settings.hermes_backend_url", "")
        store = self._seed_goal_question(tmp_path, message_id=5000)
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        update = {"message": {"text": "Yes", "chat": {"id": 999}, "message_id": 1}}

        sent: list[str] = []

        def _capture_ids(text, chat_id=None, bot=None):
            sent.append(text)
            return [8001]

        with patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=store),              patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock),              patch("api.services.telegram.send_message_capture_ids",
                   side_effect=_capture_ids),              patch("api.services.telegram.send_message_async", new_callable=AsyncMock),              patch("api.services.telegram.TypingIndicator", _DummyTyping),              patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            await listener._handle_update(update)

        mock_chat.assert_not_called()
        assert any("Goal locked" in s for s in sent)
        answered = store.list_answered_unprocessed_questions()
        assert [q["answer"] for q in answered] == ["Yes"]

    @pytest.mark.asyncio
    async def test_doctor_native_fallback_bare_approved_no_gate_consumed(self, tmp_path, monkeypatch):
        """A bare 'approved' on the native fallback path with NOTHING
        awaiting approval is consumed with a "nothing waiting" notice — it
        must never spawn a session whose entire "report" is the word
        approved."""
        monkeypatch.setattr("api.services.telegram.settings.hermes_backend_url", "")
        from api.services.agent_worker.session_store import SessionStore
        store = SessionStore(db_path=tmp_path / "sessions.db")
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        update = {"message": {"text": "approved", "chat": {"id": 999}, "message_id": 2}}

        sent: list[str] = []

        async def _capture(text, chat_id=None):
            sent.append(text)

        with patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=store),              patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock),              patch("api.services.telegram.send_message_async", side_effect=_capture),              patch("api.services.telegram.TypingIndicator", _DummyTyping),              patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            await listener._handle_update(update)

        mock_chat.assert_not_called()
        assert any("nothing here waiting" in s for s in sent)

    @pytest.mark.asyncio
    async def test_doctor_native_fallback_real_report_still_dispatches(self, monkeypatch):
        """A real report that merely STARTS with an affirmative word exceeds
        the bare-affirmative bound (25 chars) and proceeds to the chat
        pipeline normally, exactly as before #684."""
        monkeypatch.setattr("api.services.telegram.settings.hermes_backend_url", "")
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        update = {"message": {
            "text": "yes the calendar tool is broken again",
            "chat": {"id": 999}, "message_id": 3,
        }}

        with patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock),              patch("api.services.telegram.send_message_async", new_callable=AsyncMock),              patch("api.services.telegram.TypingIndicator", _DummyTyping),              patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"answer": "🩺 On it", "conversation_id": "c1"}
            await listener._handle_update(update)

        mock_chat.assert_awaited_once()
        assert mock_chat.call_args.kwargs.get("persona_id") == "doctor"
        assert mock_chat.call_args.kwargs.get("backend") == "lifeos"

    @pytest.mark.asyncio
    async def test_doctor_threaded_reply_runs_resume_hook(self):
        """Threaded-reply resume still short-circuits before the chat
        pipeline is ever reached — unaffected by #684's retirement of the
        direct-CC spawn entry for FRESH messages."""
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        update = {"message": {
            "text": "yes",
            "chat": {"id": 999},
            "message_id": 2,
            "reply_to_message": {"message_id": 5000},
        }}
        with patch.object(listener, "_maybe_handle_claude_code_reply",
                          new_callable=AsyncMock, return_value=True) as mock_resume, \
             patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            await listener._handle_update(update)
        # The reply resumed the session; the chat pipeline was never reached.
        mock_resume.assert_awaited_once()
        mock_chat.assert_not_called()

    def _seed_goal_question(self, tmp_path, message_id=5000):
        """A BLOCKED doctor session with an open goal_approval question
        anchored to `message_id`, on an isolated store."""
        from api.services.agent_worker.session_store import STATUS_BLOCKED, SessionStore

        store = SessionStore(db_path=tmp_path / "sessions.db")
        session = store.create(
            task_id="t-goal", routing="claude_code", origin="operator",
            bot="doctor", status=STATUS_BLOCKED,
        )
        store.create_pending_question(
            session_id=session.session_id, task_id="t-goal",
            question="goal body + reply instructions", sent_message_id=message_id,
            sent_message_ids=[message_id], kind="goal_approval", bot="doctor",
        )
        return store

    @pytest.mark.asyncio
    async def test_goal_approval_yes_acks_immediately(self, tmp_path):
        """A threaded 'yes' on the goal message deposits the answer AND gets a
        deposit-time ack. The worker only drains answers on its next tick (up
        to poll_seconds later) and the agent may then work silently for
        minutes — without this ack the operator can't tell the 'yes' landed."""
        store = self._seed_goal_question(tmp_path)
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        sent: list[str] = []

        async def _capture(text, chat_id=None):
            sent.append(text)

        def _capture_ids(text, chat_id=None, bot=None):
            sent.append(text)
            return [8001]

        with patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=store), \
             patch("api.services.telegram.send_message_capture_ids",
                   side_effect=_capture_ids), \
             patch("api.services.telegram.send_message_async", side_effect=_capture):
            consumed = await listener._maybe_handle_claude_code_reply(5000, "yes", "999")

        assert consumed is True
        assert any("Goal locked" in s for s in sent)
        # The answer is queued for the worker's goal_approval resume path.
        answered = store.list_answered_unprocessed_questions()
        assert [q["answer"] for q in answered] == ["yes"]

    @pytest.mark.asyncio
    async def test_goal_approval_refinement_acks_with_rework(self, tmp_path):
        """A non-affirmative threaded reply is a refinement: deposited for the
        worker, acked as a rework (not as a lock)."""
        store = self._seed_goal_question(tmp_path)
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        sent: list[str] = []

        async def _capture(text, chat_id=None):
            sent.append(text)

        def _capture_ids(text, chat_id=None, bot=None):
            sent.append(text)
            return [8001]

        with patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=store), \
             patch("api.services.telegram.send_message_capture_ids",
                   side_effect=_capture_ids), \
             patch("api.services.telegram.send_message_async", side_effect=_capture):
            consumed = await listener._maybe_handle_claude_code_reply(
                5000, "make it also require lint", "999")

        assert consumed is True
        assert any("reworking the goal" in s for s in sent)
        assert not any("Goal locked" in s for s in sent)
        answered = store.list_answered_unprocessed_questions()
        assert [q["answer"] for q in answered] == ["make it also require lint"]

    @pytest.mark.asyncio
    async def test_goal_approval_duplicate_reply_consumed_not_respawned(self, tmp_path):
        """A second reply to an already-answered goal message is consumed with
        an "already have an answer" notice — it must NOT fall through to the
        orchestration handler and spawn a fresh session (the yes/yes/approved
        fan-out failure mode)."""
        store = self._seed_goal_question(tmp_path)
        listener = self._listener("doctor", "999", persona="P", orchestrates=True)
        sent: list[str] = []

        async def _capture(text, chat_id=None):
            sent.append(text)

        with patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=store), \
             patch("api.services.telegram.send_message_async", side_effect=_capture):
            first = await listener._maybe_handle_claude_code_reply(5000, "yes", "999")
            second = await listener._maybe_handle_claude_code_reply(5000, "yes", "999")

        assert first is True and second is True
        assert any("already have your answer" in s for s in sent)
        # Only ONE answer reached the queue.
        answered = store.list_answered_unprocessed_questions()
        assert len(answered) == 1

    # #684 removed `_handle_orchestration_message`, the direct-CC entry for a
    # FRESH message — including its bare-affirmative-routes-to-open-goal-gate
    # special case (#453's "yes/approved orphan factory" guard). That guard
    # existed only because every non-threaded message unconditionally spawned
    # a session; on the new chat-pipeline path (Hermes, or the persona_id-
    # gated native fallback) spawning is the model's own tool call, not an
    # automatic per-message action, so the failure mode the guard protected
    # against can't recur the same way. The three tests that exercised that
    # method directly (`test_bare_affirmative_routes_to_open_goal_gate_not_spawn`,
    # `test_bare_affirmative_with_no_gate_is_consumed_not_spawned`,
    # `test_report_starting_with_yes_still_spawns`) were removed with it.
    # `_maybe_handle_claude_code_reply`'s own goal_approval handling (a
    # THREADED reply to the goal message, exercised above and in
    # test_session_thread_replies.py) is unaffected and still covers the
    # in-thread approval flow this retirement doesn't touch.

    @pytest.mark.asyncio
    async def test_pure_chat_bot_never_spawns_directly(self, monkeypatch):
        monkeypatch.setattr("api.services.telegram.settings.hermes_backend_url", "http://hermes")
        listener = self._listener("fitness", "999", persona="P", orchestrates=False)
        update = {"message": {"text": "bench 135x8", "chat": {"id": 999}, "message_id": 3}}
        spawn = MagicMock()
        with patch("api.services.agent_worker.claude_code_spawn.spawn_claude_code_session", spawn), \
             patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock), \
             patch("api.services.telegram.TypingIndicator", _DummyTyping), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"answer": "Logged", "conversation_id": "c1"}
            await listener._handle_update(update)
        spawn.assert_not_called()
        mock_chat.assert_awaited_once()  # pure chat, as before
        assert mock_chat.call_args.kwargs.get("persona_id") == "fitness"


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
        """A goal-approval BLOCKED outcome sends ONE anchored message — the
        goal body plus the threaded-reply instructions — and registers it as a
        kind='goal_approval' pending question scoped to the doctor bot. (The
        goal used to stream as its own message with a separate instruction
        message as the reply anchor, which made "reply yes — to which
        message?" ambiguous.)"""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        result = spawn_claude_code_session(
            w.session_store, "make the suite green", chat_id="123", bot="doctor",
        )
        w._dispatch_spawned_sessions()

        assert w._sent_with_ids, "no id-captured message was sent"
        msg_id, text, bot = w._sent_with_ids[-1]
        assert bot == "doctor"
        # The goal itself is ON the anchored message the operator replies to.
        assert "all tests pass" in text
        assert "lock this goal" in text.lower()
        assert "reply to this message" in text.lower()
        q = w.session_store.get_open_question_by_message_id(msg_id, bot="doctor")
        assert q is not None
        assert q["kind"] == "goal_approval"
        assert q["session_id"] == result["session_id"]

        # The prompt this dispatch built is one the adoption path can split:
        # the worker writes the lead-in, `condition_from_question` splits on
        # it, and a recorded approval that has to recover its condition from
        # the question row alone depends on those two agreeing.
        from api.services.agent_worker import doctor_repair

        assert doctor_repair.condition_from_question(q["question"]) == "all tests pass"

        # The same dispatch records the durable revision that question gates:
        # version 1, the goal body alone as the condition, and a resume action
        # that replays the executor's own goal command. The reply mechanics
        # appended to the sent message are not part of either.
        proposal = w.session_store.get_proposal_by_question_id(q["id"])
        assert proposal is not None
        assert proposal["version"] == 1
        assert proposal["condition"] == "all tests pass"
        assert proposal["resume_action"]["payload"] == "/goal all tests pass"
        assert proposal["workflow_id"] == (
            w.session_store.get_by_session_id(result["session_id"]).workflow_id
        )

    def test_a_doctor_session_owns_a_repair_from_diagnosis(self, tmp_path):
        """The repair record exists from the moment the session is created —
        before any goal is proposed — so the gate covers the diagnosis window
        rather than starting at the first `[GOAL]`."""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import REPAIR_DIAGNOSIS

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        result = spawn_claude_code_session(
            w.session_store, "the calendar view is wrong", chat_id="123", bot="doctor",
        )

        workflow_id = w.session_store.get_by_session_id(
            result["session_id"],
        ).workflow_id
        assert workflow_id
        assert w.session_store.get_repair(workflow_id)["phase"] == REPAIR_DIAGNOSIS

    def test_an_ordinary_claude_code_session_owns_no_repair(self, tmp_path):
        """`[GOAL]` is a generic protocol tag. A session that is not the
        doctor's gets an ordinary approval prompt and no repair record, so
        nothing about its dispatch changes."""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        result = spawn_claude_code_session(
            w.session_store, "make the suite green", chat_id="123",
        )
        w._dispatch_spawned_sessions()

        session = w.session_store.get_by_session_id(result["session_id"])
        assert session.workflow_id is None
        msg_id, _, _ = w._sent_with_ids[-1]
        q = w.session_store.get_open_question_by_message_id(msg_id)
        assert q["kind"] == "goal_approval"
        assert w.session_store.get_proposal_by_question_id(q["id"]) is None

    def _seed_blocked_goal_session(self, w, *, condition="all tests pass"):
        """Drop a BLOCKED doctor session with an open goal_approval question
        and the goal revision that question gates — the shape left behind
        after a goal-block round-trips through dispatch."""
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
        w._record_goal_proposal(session, condition, qid)
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
        """An affirmative reply that resolves to no goal revision must NOT
        forward a bare 'yes' — it asks the agent to re-emit the [GOAL], and
        records a goal_lock_failed event."""
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
        # Neither the prompt text nor the transcript carries a condition, so
        # no revision can be adopted for this question.
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
        """The approved revision is durable: a fresh Worker over the SAME
        paths (simulating a restart) still injects the stored resume action."""
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

    @staticmethod
    def _redeliver(w, question_id):
        """Return an already-consumed answered reply to the worker's
        unprocessed set, the shape a redelivered reply arrives in."""
        import sqlite3

        conn = sqlite3.connect(str(w.session_store.db_path))
        try:
            conn.execute(
                "UPDATE pending_questions SET processed = 0 WHERE id = ?",
                (int(question_id),),
            )
            conn.commit()
        finally:
            conn.close()

    def test_duplicate_reply_enqueues_no_second_resume(self, tmp_path):
        """A redelivered approval resolves to a revision that is not in
        `proposed` state: no second resume action and no second dispatch."""
        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session, qid = self._seed_blocked_goal_session(
            w, condition="merge the parser fix and verify the service health check",
        )

        assert w.session_store.deposit_answer(9100, "yes", bot="doctor") is True
        w._process_clarification_answers()
        assert len(w.session_store.drain_pending_messages(session.session_id)) == 1

        self._redeliver(w, qid)
        w._process_clarification_answers()
        assert w.session_store.drain_pending_messages(session.session_id) == []
        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "doctor_goal_reply_ignored" in kinds

    def test_stale_reply_cannot_lock_a_superseded_revision(self, tmp_path):
        """A refinement retires the revision it answered. A later reply aimed
        at that same revision locks nothing."""
        from api.services.agent_worker.session_store import PROPOSAL_SUPERSEDED

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session, qid = self._seed_blocked_goal_session(
            w, condition="merge the parser fix and verify the service health check",
        )
        proposal = w.session_store.get_proposal_by_question_id(qid)

        assert w.session_store.deposit_answer(9100, "also require lint", bot="doctor") is True
        w._process_clarification_answers()
        assert w.session_store.get_proposal(proposal["proposal_id"])["status"] == (
            PROPOSAL_SUPERSEDED
        )
        w.session_store.drain_pending_messages(session.session_id)

        # A stale "yes" on the same, now-superseded revision.
        self._redeliver(w, qid)
        w.session_store.deposit_answer_by_id(qid, "yes")
        w._process_clarification_answers()
        assert w.session_store.drain_pending_messages(session.session_id) == []
        assert w.session_store.get_proposal(proposal["proposal_id"])["status"] == (
            PROPOSAL_SUPERSEDED
        )

    def test_a_decline_closes_the_repair_and_launches_nothing(self, tmp_path):
        from api.services.agent_worker.session_store import (
            PROPOSAL_DECLINED, REPAIR_DECLINED,
        )
        from api.services.agent_worker import doctor_repair

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session, qid = self._seed_blocked_goal_session(
            w, condition="merge the parser fix and verify the service health check",
        )
        proposal = w.session_store.get_proposal_by_question_id(qid)

        assert w.session_store.deposit_answer(9100, "leave it", bot="doctor") is True
        w._process_clarification_answers()

        assert w.session_store.get_proposal(proposal["proposal_id"])["status"] == (
            PROPOSAL_DECLINED
        )
        repair = w.session_store.get_repair(proposal["workflow_id"])
        assert repair["phase"] == REPAIR_DECLINED
        assert repair["approved_proposal_id"] is None
        # Nothing may be dispatched for a declined repair, but the doctor is
        # resumed with the reply so it can file the work as an issue and stop.
        assert doctor_repair.dispatch_allowed(repair, "implement").allowed is False
        assert [m["content"] for m in
                w.session_store.drain_pending_messages(session.session_id)] == [
            "leave it",
        ]

    def test_a_refinement_is_not_read_as_a_decline(self, tmp_path):
        """Only a reply that is nothing but a decline declines. A reply that
        carries changes retires the revision for re-proposal instead."""
        from api.services.agent_worker.session_store import (
            PROPOSAL_SUPERSEDED, REPAIR_DIAGNOSIS,
        )

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        _, qid = self._seed_blocked_goal_session(
            w, condition="merge the parser fix and verify the service health check",
        )
        proposal = w.session_store.get_proposal_by_question_id(qid)

        w.session_store.deposit_answer(9100, "no, also require lint", bot="doctor")
        w._process_clarification_answers()

        assert w.session_store.get_proposal(proposal["proposal_id"])["status"] == (
            PROPOSAL_SUPERSEDED
        )
        assert w.session_store.get_repair(
            proposal["workflow_id"],
        )["phase"] == REPAIR_DIAGNOSIS

    def test_a_reply_refused_by_a_shipped_repair_leaves_no_blocked_session(self, tmp_path):
        """The repair ships while a later revision's question is still open —
        the child of the approved revision landed its result. The reply that
        follows starts nothing, and the doctor it was gating is released
        instead of sitting blocked on an anchor nothing will answer."""
        from api.services.agent_worker.session_store import (
            PROPOSAL_PROPOSED, REPAIR_SHIPPED, STATUS_CLAIMED,
        )

        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        session, qid = self._seed_blocked_goal_session(
            w, condition="merge the parser fix and verify the service health check",
        )
        proposal = w.session_store.get_proposal_by_question_id(qid)
        w.session_store.set_repair_phase(proposal["workflow_id"], REPAIR_SHIPPED)

        assert w.session_store.deposit_answer(9100, "yes", bot="doctor") is True
        w._process_clarification_answers()

        assert w.session_store.get_proposal(proposal["proposal_id"])["status"] == (
            PROPOSAL_PROPOSED
        )
        resumed = [m["content"] for m in
                   w.session_store.drain_pending_messages(session.session_id)]
        assert len(resumed) == 1
        assert "shipped" in resumed[0]
        assert not resumed[0].startswith("/goal")
        assert w.session_store.get(session.task_id).status == STATUS_CLAIMED
        assert w.session_store.get_open_question_by_message_id(9100) is None

    def test_an_approval_after_a_cancel_approves_nothing(self, tmp_path):
        """A cancel lands while the goal question is still outstanding. The
        approval reply that follows leaves the revision unconsumed and starts
        nothing; the operator is told, and the doctor — still blocked on an
        anchor that is now consumed — is resumed with the same news."""
        from api.services.agent_worker.session_store import (
            PROPOSAL_PROPOSED, REPAIR_CANCELLED,
        )

        notices: list[tuple] = []
        w = self._make_worker(tmp_path, self._goal_blocked_stub())
        w._telegram_send = lambda text, chat_id=None, bot=None: (
            notices.append((text, bot)) or True
        )
        session, qid = self._seed_blocked_goal_session(
            w, condition="merge the parser fix and verify the service health check",
        )
        proposal = w.session_store.get_proposal_by_question_id(qid)
        w.session_store.cancel_repair(proposal["workflow_id"], "operator kill")

        assert w.session_store.deposit_answer(9100, "yes", bot="doctor") is True
        w._process_clarification_answers()

        assert w.session_store.get_proposal(proposal["proposal_id"])["status"] == (
            PROPOSAL_PROPOSED
        )
        repair = w.session_store.get_repair(proposal["workflow_id"])
        assert repair["phase"] == REPAIR_CANCELLED
        assert repair["approved_proposal_id"] is None
        # The news, not a resume action: nothing that would start work.
        resumed = [m["content"] for m in
                   w.session_store.drain_pending_messages(session.session_id)]
        assert resumed == [notices[-1][0]]
        assert not any(m.startswith("/goal") for m in resumed)
        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "doctor_goal_reply_ignored" in kinds
        assert "claude_code_goal_locked" not in kinds
        assert notices and notices[-1][1] == "doctor"


class TestRepairSpawnGate:
    """`lifeos_agent_spawn` is how a repair dispatches implementation work, so
    it is the LifeOS-owned boundary the single human gate sits on."""

    def _ctx(self, tmp_path, caller_session_id):
        from api.services.agent_worker import inter_agent
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.transcript_store import TranscriptStore

        return inter_agent.InterAgentContext(
            session_store=SessionStore(db_path=tmp_path / "sessions.db"),
            transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
            caller_session_id=caller_session_id,
            caps=inter_agent.Caps(),
        )

    def _doctor_root(self, tmp_path):
        """A doctor root session created the way production creates one, in
        the diagnosis window: a repair on record, no goal proposed yet."""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import SessionStore

        store = SessionStore(db_path=tmp_path / "sessions.db")
        result = spawn_claude_code_session(
            store, "the calendar view is wrong", chat_id="123", bot="doctor",
        )
        caller = store.get_by_session_id(result["session_id"])
        return store, caller.workflow_id, caller

    def _propose(self, store, workflow_id):
        from api.services.agent_worker import doctor_repair

        condition = "merge the parser fix and verify the service health check"
        return store.propose_goal(
            workflow_id,
            condition=condition,
            resume_action=doctor_repair.goal_resume_action(condition),
        )

    def test_spawn_is_refused_during_the_diagnosis_window(self, tmp_path):
        """Before any goal is proposed — the window the doctor spends reading
        the codebase — an implementation dispatch is already refused."""
        from api.services.agent_worker import inter_agent
        from api.services.agent_worker.session_store import REPAIR_DIAGNOSIS

        store, workflow_id, caller = self._doctor_root(tmp_path)
        assert store.get_repair(workflow_id)["phase"] == REPAIR_DIAGNOSIS

        ctx = self._ctx(tmp_path, caller.session_id)
        result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
            "prompt": "implement the parser fix", "model": "claude_code",
        })

        assert result["ok"] is False
        assert result["error"] == "repair_awaiting_approval"
        # No child session was created: the gate refuses the dispatch itself.
        assert [s.task_id for s in store.list_repair_sessions(workflow_id)] == [
            caller.task_id,
        ]

    def test_spawn_is_refused_while_the_goal_awaits_approval(self, tmp_path):
        from api.services.agent_worker import inter_agent

        store, workflow_id, caller = self._doctor_root(tmp_path)
        self._propose(store, workflow_id)
        ctx = self._ctx(tmp_path, caller.session_id)
        result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
            "prompt": "implement the parser fix", "model": "claude_code",
        })

        assert result["ok"] is False
        assert result["error"] == "repair_awaiting_approval"
        assert [s.task_id for s in store.list_repair_sessions(workflow_id)] == [
            caller.task_id,
        ]
        # The supervisor's own session is untouched — the gate governs what
        # LifeOS dispatches, not what the running CLI process may do.
        assert store.get(caller.task_id).status == caller.status

    def test_spawn_is_allowed_once_the_goal_is_approved(self, tmp_path):
        from api.services.agent_worker import inter_agent

        store, workflow_id, caller = self._doctor_root(tmp_path)
        proposal = self._propose(store, workflow_id)
        store.approve_goal(proposal["proposal_id"])

        ctx = self._ctx(tmp_path, caller.session_id)
        result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
            "prompt": "implement the parser fix", "model": "claude_code",
        })

        assert result["ok"] is True, result
        child = store.get_by_session_id(result["child_session_id"])
        assert child.workflow_id == workflow_id

    def test_the_hermes_anchor_carries_the_same_gate(self, tmp_path):
        """Hermes has no shell, so the worker it spawns is the only way it
        changes anything — and that spawn reads the repair off its anchor."""
        from api.services.agent_worker import inter_agent
        from api.services.agent_worker.hermes_session import (
            resolve_hermes_caller_session_id,
        )
        from api.services.agent_worker.session_store import (
            REPAIR_DIAGNOSIS, SessionStore,
        )

        store = SessionStore(db_path=tmp_path / "sessions.db")
        anchor_id = resolve_hermes_caller_session_id(store, "conv-doctor", bot="doctor")
        anchor = store.get_by_session_id(anchor_id)
        assert anchor.workflow_id
        assert store.get_repair(anchor.workflow_id)["phase"] == REPAIR_DIAGNOSIS

        ctx = self._ctx(tmp_path, anchor_id)
        refused = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
            "prompt": "implement the parser fix", "model": "claude_code",
        })
        assert refused["ok"] is False
        assert refused["error"] == "repair_awaiting_approval"

        proposal = self._propose(store, anchor.workflow_id)
        store.approve_goal(proposal["proposal_id"])
        allowed = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
            "prompt": "implement the parser fix", "model": "claude_code",
        })
        assert allowed["ok"] is True, allowed
        assert store.get_by_session_id(
            allowed["child_session_id"],
        ).workflow_id == anchor.workflow_id

    def test_a_non_doctor_hermes_anchor_owns_no_repair(self, tmp_path):
        from api.services.agent_worker.hermes_session import (
            resolve_hermes_caller_session_id,
        )
        from api.services.agent_worker.session_store import SessionStore

        store = SessionStore(db_path=tmp_path / "sessions.db")
        anchor_id = resolve_hermes_caller_session_id(store, "conv-fitness", bot="fitness")
        assert store.get_by_session_id(anchor_id).workflow_id is None

    def test_an_ordinary_agent_task_that_proposes_a_goal_is_not_gated(self, tmp_path):
        """An ordinary session emitting the generic `[GOAL]` tag keeps the
        spawn it has and the approval it asked for: no repair is opened for
        it, so no gate applies and its operator's yes still locks the goal."""
        from api.services.agent_worker import inter_agent
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import SessionStore

        store = SessionStore(db_path=tmp_path / "sessions.db")
        w = TestGoalApproval()._make_worker(tmp_path, TestGoalApproval()._goal_blocked_stub())
        spawned = spawn_claude_code_session(
            w.session_store, "make the suite green", chat_id="123",
        )
        w._dispatch_spawned_sessions()

        caller = store.get_by_session_id(spawned["session_id"])
        assert caller.workflow_id is None
        # The session really did register a goal-approval gate of its own.
        msg_id, _, _ = w._sent_with_ids[-1]
        assert w.session_store.get_open_question_by_message_id(
            msg_id,
        )["kind"] == "goal_approval"

        ctx = self._ctx(tmp_path, caller.session_id)
        result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
            "prompt": "summarize a file", "model": "claude_code",
        })
        assert result["ok"] is True, result
        assert store.get_by_session_id(result["child_session_id"]).workflow_id is None

        # Approving that gate locks the condition the executor proposed —
        # a session with no repair record is not stranded in a reprompt loop.
        assert w.session_store.deposit_answer(msg_id, "yes") is True
        w._process_clarification_answers()
        pending = w.session_store.drain_pending_messages(caller.session_id)
        assert [p["content"] for p in pending] == ["/goal all tests pass"]

    def test_a_repair_child_cannot_launder_a_dispatch_past_the_gate(self, tmp_path):
        """A grandchild spawn reads the repair from the lineage root, so an
        intermediate session cannot dispatch work the gate has not opened."""
        from api.services.agent_worker import inter_agent
        from api.services.agent_worker.session_store import STATUS_RUNNING

        store, workflow_id, caller = self._doctor_root(tmp_path)
        proposal = self._propose(store, workflow_id)
        store.approve_goal(proposal["proposal_id"])
        child = store.create(
            task_id="task-doctor-child",
            routing="claude_code",
            status=STATUS_RUNNING,
            parent_session_id=caller.session_id,
            root_session_id=caller.session_id,
            spawn_depth=1,
            budget={"max_dollars": 5.0, "max_tokens": 200_000, "wall_seconds": 1800},
        )
        store.cancel_repair(workflow_id)

        ctx = self._ctx(tmp_path, child.session_id)
        result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
            "prompt": "keep going anyway", "model": "claude_code",
        })
        assert result["ok"] is False
        assert result["error"] == "repair_cancelled"


class TestRepairResultsAdvancePhases:
    """The worker folds a session's structured result into its repair. Fake or
    partial results never reach `shipped`."""

    def _worker_with_repair(self, tmp_path):
        from api.services.agent_worker import doctor_repair
        from api.services.agent_worker.session_store import STATUS_RUNNING

        w = TestGoalApproval()._make_worker(tmp_path, None)
        workflow_id = w.session_store.create_repair()["workflow_id"]
        condition = "merge the parser fix and verify the service health check"
        proposal = w.session_store.propose_goal(
            workflow_id,
            condition=condition,
            resume_action=doctor_repair.goal_resume_action(condition),
        )
        w.session_store.approve_goal(proposal["proposal_id"])
        session = w.session_store.create(
            task_id="task-repair-run",
            routing="claude_code",
            origin="operator",
            bot="doctor",
            status=STATUS_RUNNING,
            workflow_id=workflow_id,
        )
        return w, workflow_id, session

    @staticmethod
    def _result_text(**overrides):
        from tests.test_doctor_repair_record import shipped_evidence

        return "Done.\nLIFEOS_REPAIR_RESULT:" + json.dumps(shipped_evidence(**overrides))

    def test_complete_evidence_marks_the_repair_shipped(self, tmp_path):
        from api.services.agent_worker.session_store import REPAIR_SHIPPED

        w, workflow_id, session = self._worker_with_repair(tmp_path)
        w._apply_repair_result(session, self._result_text())
        assert w.session_store.get_repair(workflow_id)["phase"] == REPAIR_SHIPPED

    def test_prose_only_success_advances_nothing(self, tmp_path):
        from api.services.agent_worker.session_store import REPAIR_IMPLEMENTING

        w, workflow_id, session = self._worker_with_repair(tmp_path)
        w._apply_repair_result(session, "Shipped it! Everything is merged and live.")
        assert w.session_store.get_repair(workflow_id)["phase"] == REPAIR_IMPLEMENTING

    def test_a_duplicate_result_event_is_applied_once(self, tmp_path):
        w, workflow_id, session = self._worker_with_repair(tmp_path)
        w._apply_repair_result(session, self._result_text())
        w._apply_repair_result(session, self._result_text())

        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert kinds.count("doctor_repair_phase") == 1
        assert "doctor_repair_event_duplicate" in kinds

    def test_a_cancelled_repair_is_not_revived_by_a_late_result(self, tmp_path):
        from api.services.agent_worker.session_store import REPAIR_CANCELLED

        w, workflow_id, session = self._worker_with_repair(tmp_path)
        w.session_store.cancel_repair(workflow_id, "operator kill")
        w._apply_repair_result(session, self._result_text())

        assert w.session_store.get_repair(workflow_id)["phase"] == REPAIR_CANCELLED
        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "doctor_repair_result_rejected" in kinds

    def test_a_session_outside_a_repair_records_nothing(self, tmp_path):
        from api.services.agent_worker.session_store import STATUS_RUNNING

        w, _, _ = self._worker_with_repair(tmp_path)
        loose = w.session_store.create(
            task_id="task-loose", routing="claude_code", status=STATUS_RUNNING,
        )
        w._apply_repair_result(loose, self._result_text())
        assert w.transcript_store.read(loose.session_id) == []

    def test_a_terminal_worker_turn_advances_the_repair(self, tmp_path):
        """The result is drained by the worker's own terminal path, driven by
        an executor that completes carrying the result line — not by calling
        the fold directly."""
        from dataclasses import dataclass

        from api.services.agent_worker import doctor_repair
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.local_executor import ExecutorOutcome
        from api.services.agent_worker.session_store import (
            REPAIR_SHIPPED, STATUS_COMPLETED,
        )

        @dataclass
        class _Completes:
            outcome: ExecutorOutcome

            def execute(self, session, task):
                return self.outcome

            def resume(self, session, message, working_dir=None):
                return self.outcome

        final_text = (
            "Merged the parser fix as "
            "https://github.com/example/synthetic/pull/4321 and verified the "
            "service health check. Revert with: gh pr revert 4321.\n"
            + self._result_text()
        )
        w = TestGoalApproval()._make_worker(tmp_path, _Completes(ExecutorOutcome(
            status=STATUS_COMPLETED, final_text=final_text,
        )))
        spawned = spawn_claude_code_session(
            w.session_store, "implement the parser fix", chat_id="123", bot="doctor",
        )
        session = w.session_store.get_by_session_id(spawned["session_id"])
        workflow_id = session.workflow_id
        condition = "merge the parser fix and verify the service health check"
        proposal = w.session_store.propose_goal(
            workflow_id, condition=condition,
            resume_action=doctor_repair.goal_resume_action(condition),
        )
        w.session_store.approve_goal(proposal["proposal_id"])

        w._dispatch_spawned_sessions()

        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "code_handled_completion" in kinds
        assert w.session_store.get_repair(workflow_id)["phase"] == REPAIR_SHIPPED

    def test_a_terminal_codex_turn_advances_the_repair(self, tmp_path):
        """A repair's implementation worker can be codex-routed, so the codex
        terminal path folds the result in exactly as the claude_code one
        does."""
        from dataclasses import dataclass

        from api.services.agent_worker import doctor_repair
        from api.services.agent_worker.codex_spawn import spawn_codex_session
        from api.services.agent_worker.local_executor import ExecutorOutcome
        from api.services.agent_worker.session_store import (
            REPAIR_SHIPPED, STATUS_COMPLETED,
        )

        @dataclass
        class _Completes:
            outcome: ExecutorOutcome

            def execute(self, session, task):
                return self.outcome

            def resume(self, session, message, working_dir=None):
                return self.outcome

        final_text = (
            "Merged the parser fix as "
            "https://github.com/example/synthetic/pull/4321 and verified the "
            "service health check. Revert with: gh pr revert 4321.\n"
            + self._result_text()
        )
        w = TestGoalApproval()._make_worker(tmp_path, None)
        w._codex_executor = _Completes(ExecutorOutcome(
            status=STATUS_COMPLETED, final_text=final_text,
        ))
        spawned = spawn_codex_session(
            w.session_store, "implement the parser fix", chat_id="123",
            persona_id="doctor",
        )
        session = w.session_store.get_by_session_id(spawned["session_id"])
        workflow_id = session.workflow_id
        assert workflow_id
        condition = "merge the parser fix and verify the service health check"
        proposal = w.session_store.propose_goal(
            workflow_id, condition=condition,
            resume_action=doctor_repair.goal_resume_action(condition),
        )
        w.session_store.approve_goal(proposal["proposal_id"])

        w._dispatch_spawned_sessions()

        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "codex_handled_completion" in kinds
        assert w.session_store.get_repair(workflow_id)["phase"] == REPAIR_SHIPPED

    def test_a_result_on_a_turn_that_pauses_advances_nothing(self, tmp_path):
        """The repair reads the result from the turn that ends the session,
        which is what both personas instruct. A turn that stops to ask the
        operator something is still mid-run: its text is a question, not a
        report, and it leaves the phase where it was."""
        from dataclasses import dataclass

        from api.services.agent_worker import doctor_repair
        from api.services.agent_worker.claude_code_executor import (
            REASON_AWAITING_CLARIFICATION,
        )
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.local_executor import ExecutorOutcome
        from api.services.agent_worker.session_store import (
            REPAIR_IMPLEMENTING, STATUS_BLOCKED,
        )

        @dataclass
        class _Blocks:
            outcome: ExecutorOutcome

            def execute(self, session, task):
                return self.outcome

            def resume(self, session, message, working_dir=None):
                return self.outcome

        w = TestGoalApproval()._make_worker(tmp_path, _Blocks(ExecutorOutcome(
            status=STATUS_BLOCKED,
            reason=REASON_AWAITING_CLARIFICATION,
            final_text=(
                "Which service should the health check cover?\n"
                + self._result_text()
            ),
        )))
        spawned = spawn_claude_code_session(
            w.session_store, "implement the parser fix", chat_id="123", bot="doctor",
        )
        session = w.session_store.get_by_session_id(spawned["session_id"])
        condition = "merge the parser fix and verify the service health check"
        proposal = w.session_store.propose_goal(
            session.workflow_id, condition=condition,
            resume_action=doctor_repair.goal_resume_action(condition),
        )
        w.session_store.approve_goal(proposal["proposal_id"])

        w._dispatch_spawned_sessions()

        kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
        assert "code_block_prompt_registered" in kinds
        assert "doctor_repair_phase" not in kinds
        repair = w.session_store.get_repair(session.workflow_id)
        assert repair["phase"] == REPAIR_IMPLEMENTING
        assert repair["evidence"] == {}


class TestRepairReadSurface:
    """Both surfaces read one durable repair state off the agents snapshot."""

    def test_repair_phase_and_evidence_ride_along_on_the_session(self, tmp_path):
        from api.routes.agents import _session_to_dict
        from api.services.agent_worker import doctor_repair
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.transcript_store import TranscriptStore

        store = SessionStore(db_path=tmp_path / "sessions.db")
        transcript = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
        workflow_id = store.create_repair()["workflow_id"]
        condition = "merge the parser fix and verify the service health check"
        proposal = store.propose_goal(
            workflow_id, condition=condition,
            resume_action=doctor_repair.goal_resume_action(condition),
        )
        store.approve_goal(proposal["proposal_id"])
        store.set_repair_phase(
            workflow_id, "verifying", waiting_reason="running_revision_stale",
            evidence={"pull_requests": [4321]},
        )
        session = store.create(
            task_id="task-surface", routing="claude_code", origin="operator",
            bot="doctor", workflow_id=workflow_id,
        )

        repairs = {r["workflow_id"]: r for r in store.list_repairs([workflow_id])}
        payload = _session_to_dict(session, transcript, repairs)

        assert payload["repair"] == {
            "workflow_id": workflow_id,
            "phase": "verifying",
            "waiting_reason": "running_revision_stale",
            "approved_version": 1,
            "evidence": {"pull_requests": [4321]},
        }

    def test_a_session_outside_a_repair_reports_null(self, tmp_path):
        from api.routes.agents import _session_to_dict
        from api.services.agent_worker.session_store import SessionStore
        from api.services.agent_worker.transcript_store import TranscriptStore

        store = SessionStore(db_path=tmp_path / "sessions.db")
        transcript = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
        session = store.create(task_id="task-plain", routing="local")
        assert _session_to_dict(session, transcript, {})["repair"] is None


class TestPersonaResultMarkerContract:
    """The one coupling between persona prose and code: the marker each doctor
    persona instructs its worker to emit has to be the marker the parser
    matches. Everything else about what the personas say is prose, and the
    behavior it describes is pinned where it is enforced."""

    @pytest.mark.parametrize("name", ["doctor.md", "doctor.hermes.md"])
    def test_persona_instructs_the_marker_the_parser_matches(self, name):
        from api.services.agent_worker import doctor_repair

        body = (Path("config/personas") / name).read_text(encoding="utf-8")
        assert doctor_repair.RESULT_MARKER in body
        # The example line the persona shows is one the parser actually reads.
        example = next(
            line for line in body.splitlines()
            if line.strip().startswith(doctor_repair.RESULT_MARKER)
        )
        parsed = doctor_repair.parse_result(
            example.replace("<the JSON scripts/verify_candidate.py printed>", "{}")
                   .replace(
                       "<the JSON that ./scripts/server.sh verify-runtime-evidence "
                       "printed>", "{}",
                   )
                   .replace(
                       "<the JSON ./scripts/server.sh verify-runtime-evidence "
                       "printed>", "{}",
                   )
        )
        assert parsed is not None
        assert parsed["goal_version"] == 1
