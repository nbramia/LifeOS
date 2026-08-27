"""Threaded replies to ANY session message route back into the session (#458).

Covers the three layers of the status-anchor feature:
  * session_store — the per-session ``kind='status_anchor'`` row that
    accumulates operator-facing message ids without behaving like a question.
  * worker — ``_send_session_message`` (footer + id capture + registration)
    and the completed-with-pending reopen at the turn boundary.
  * telegram listener — routing a threaded reply on an anchored message into
    the session as a context note, with the right ack per session state.
"""

from unittest.mock import patch

import httpx
import pytest

from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import (
    NO_REPLY_FOOTER,
    REPLYABLE_FOOTER,
    _with_reply_footer,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# session_store — status_anchor row semantics
# ---------------------------------------------------------------------------


class TestReplyAnchorStore:
    def _store(self, tmp_path):
        return SessionStore(db_path=tmp_path / "sessions.db")

    def test_anchors_accumulate_on_one_row_and_resolve_by_any_id(self, tmp_path):
        store = self._store(tmp_path)
        s = store.create(task_id="t1", routing="claude_code", origin="operator", bot="doctor")
        store.add_reply_anchors(s.session_id, "t1", [100, 101], bot="doctor")
        store.add_reply_anchors(s.session_id, "t1", [102], bot="doctor")

        for mid in (100, 101, 102):
            q = store.get_open_question_by_message_id(mid, bot="doctor")
            assert q is not None and q["kind"] == "status_anchor"
            assert q["session_id"] == s.session_id
        # One row, not three — the ids merged into its sent_message_ids list.
        with store._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM pending_questions WHERE kind='status_anchor'"
            ).fetchone()[0]
        assert n == 1

    def test_deposit_answer_never_matches_an_anchor(self, tmp_path):
        """The anchor row is a routing index, not a question — a stray deposit
        (e.g. the generic reply-deposit fallback) must not 'answer' it, which
        would kill the anchor for the whole session."""
        store = self._store(tmp_path)
        s = store.create(task_id="t1", routing="claude_code", origin="operator", bot="doctor")
        store.add_reply_anchors(s.session_id, "t1", [100], bot="doctor")

        assert store.deposit_answer(100, "yes", bot="doctor") is False
        assert store.get_open_question_by_message_id(100, bot="doctor") is not None

    def test_web_open_question_lookup_skips_anchors(self, tmp_path):
        """The web answer path takes the OLDEST open question for a session —
        the always-open anchor row (created at the first notify, i.e. early)
        must never shadow a real clarification."""
        store = self._store(tmp_path)
        s = store.create(task_id="t1", routing="claude_code", origin="operator")
        store.add_reply_anchors(s.session_id, "t1", [100])
        store.create_pending_question(
            session_id=s.session_id, task_id="t1", question="which file?",
            sent_message_id=200, kind="clarification",
        )
        q = store.get_open_question_by_session_id(s.session_id)
        assert q is not None and q["kind"] == "clarification"

    def test_timeout_sweep_skips_anchors(self, tmp_path):
        """Anchors are open forever by design — the nudge sweep must not
        re-prompt the operator about them or expire them."""
        store = self._store(tmp_path)
        s = store.create(task_id="t1", routing="claude_code", origin="operator")
        store.add_reply_anchors(s.session_id, "t1", [100])
        far_future = 2_000_000_000_000
        assert store.list_timed_out_questions(far_future) == []


# ---------------------------------------------------------------------------
# worker — _send_session_message + completed-with-pending reopen
# ---------------------------------------------------------------------------


def _make_worker(tmp_path, *, executor=None, send_with_id=None):
    from api.services.agent_worker.spend_tracker import SpendTracker
    from api.services.agent_worker.worker import Worker, _SynchronousPool
    from api.services.conversation_store import ConversationStore

    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    sent_with_ids: list[str] = []

    def _default_send_with_id(text, chat_id=None, bot=None):
        sent_with_ids.append(text)
        return [9000 + len(sent_with_ids)]

    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None, bot=None: sent.append(text) or True,
        telegram_send_with_id=send_with_id or _default_send_with_id,
        http_client=client,
        claude_code_executor=executor,
        cli_pool=_SynchronousPool(),
    )
    w._plain_sent = sent  # type: ignore[attr-defined]
    w._id_sent = sent_with_ids  # type: ignore[attr-defined]
    return w


class TestSendSessionMessage:
    def test_footer_and_anchor_registration(self, tmp_path):
        w = _make_worker(tmp_path)
        s = w.session_store.create(
            task_id="t1", routing="claude_code", origin="operator", bot="doctor",
        )

        w._send_session_message(s, "Running the tests now.")

        assert len(w._id_sent) == 1
        assert w._id_sent[0].endswith(REPLYABLE_FOOTER)
        assert "Running the tests now." in w._id_sent[0]
        # The captured id resolves back to the session as a status anchor.
        q = w.session_store.get_open_question_by_message_id(9001, bot="doctor")
        assert q is not None and q["kind"] == "status_anchor"
        assert q["session_id"] == s.session_id

    def test_fallback_plain_send_has_no_footer(self, tmp_path):
        """When id capture yields nothing, the reply route doesn't exist — a
        'reply in thread' footer would be a lie, so the fallback is bare."""
        w = _make_worker(tmp_path, send_with_id=lambda text, chat_id=None, bot=None: [])
        s = w.session_store.create(task_id="t1", routing="claude_code", origin="operator")

        w._send_session_message(s, "Running the tests now.")

        assert w._plain_sent == ["Running the tests now."]


class TestCompletedWithPendingReopens:
    def _stub(self, store):
        from dataclasses import dataclass
        from api.services.agent_worker.local_executor import ExecutorOutcome

        @dataclass
        class _Stub:
            store: SessionStore
            enqueue_mid_run: bool

            def execute(self, session, task):
                # Simulate the CLI id persisting during the run, a mid-run
                # operator reply arriving via the status-anchor route (when
                # asked), and the real executor's terminal status write.
                self.store.set_claude_code_session_id(session.task_id, "cli-99")
                if self.enqueue_mid_run:
                    self.store.enqueue_message(
                        session.session_id, "operator", "(operator note) also check X",
                    )
                self.store.update_status(session.task_id, STATUS_COMPLETED)
                # notifications_sent=1 earns the completion (#760) — these
                # tests are about the reopen-for-pending-messages tail, not
                # the earned-completion gate itself.
                return ExecutorOutcome(
                    status=STATUS_COMPLETED, final_text="All done.", notifications_sent=1,
                )

            def resume(self, session, message, working_dir=None):
                return self.execute(session, {})

        return _Stub

    def test_mid_run_note_reopens_completed_session(self, tmp_path):
        w = _make_worker(tmp_path)
        stub_cls = self._stub(w.session_store)
        w._claude_code_executor = stub_cls(w.session_store, enqueue_mid_run=True)
        s = w.session_store.create(task_id="t1", routing="claude_code", origin="operator")
        w.session_store.enqueue_message(s.session_id, "operator", "do the thing")

        w._dispatch_spawned_sessions()

        # The mid-run note reopened the session at the turn boundary so the
        # next tick resumes with it.
        assert w.session_store.get("t1").status == STATUS_CLAIMED
        kinds = [e["kind"] for e in w.transcript_store.read(s.session_id)]
        assert "code_reopened_for_pending_messages" in kinds
        pending = w.session_store.drain_pending_messages(s.session_id)
        assert [m["content"] for m in pending] == ["(operator note) also check X"]

    def test_no_pending_messages_stays_completed(self, tmp_path):
        w = _make_worker(tmp_path)
        stub_cls = self._stub(w.session_store)
        w._claude_code_executor = stub_cls(w.session_store, enqueue_mid_run=False)
        s = w.session_store.create(task_id="t1", routing="claude_code", origin="operator")
        w.session_store.enqueue_message(s.session_id, "operator", "do the thing")

        w._dispatch_spawned_sessions()

        assert w.session_store.get("t1").status == STATUS_COMPLETED

    def test_completion_message_carries_replyable_footer(self, tmp_path):
        w = _make_worker(tmp_path)
        stub_cls = self._stub(w.session_store)
        w._claude_code_executor = stub_cls(w.session_store, enqueue_mid_run=False)
        s = w.session_store.create(task_id="t1", routing="claude_code", origin="operator")
        w.session_store.enqueue_message(s.session_id, "operator", "do the thing")

        w._dispatch_spawned_sessions()

        completion = [t for t in w._id_sent if "All done." in t]
        assert completion and completion[0].endswith(REPLYABLE_FOOTER)


class TestBlockedClarifyPromptMerged:
    def test_clarify_prompt_carries_question_and_footer(self, tmp_path):
        """The clarification question and its reply instructions arrive as ONE
        anchored message (#458), same treatment [GOAL] got in #456."""
        from dataclasses import dataclass
        from api.services.agent_worker.claude_code_executor import (
            REASON_AWAITING_CLARIFICATION,
        )
        from api.services.agent_worker.local_executor import ExecutorOutcome

        @dataclass
        class _Stub:
            def execute(self, session, task):
                return ExecutorOutcome(
                    status=STATUS_BLOCKED,
                    reason=REASON_AWAITING_CLARIFICATION,
                    final_text="Which file did you mean?",
                )

            def resume(self, session, message, working_dir=None):
                return self.execute(session, {})

        w = _make_worker(tmp_path, executor=_Stub())
        s = w.session_store.create(task_id="t1", routing="claude_code", origin="operator")
        w.session_store.enqueue_message(s.session_id, "operator", "edit the file")

        w._dispatch_spawned_sessions()

        assert len(w._id_sent) == 1
        text = w._id_sent[0]
        assert "Which file did you mean?" in text
        assert "replying to this message" in text.lower()
        assert text.endswith(REPLYABLE_FOOTER)


# ---------------------------------------------------------------------------
# telegram listener — threaded reply on an anchored message
# ---------------------------------------------------------------------------


class TestStatusAnchorReplies:
    def _listener(self):
        from api.services.telegram import TelegramBotListener
        from config.settings import TelegramBotConfig
        return TelegramBotListener(TelegramBotConfig(
            name="doctor", token="TOK", chat_id="999", persona="P", orchestrates=True,
        ))

    def _seed(self, tmp_path, *, status, cli_id="cli-1"):
        store = SessionStore(db_path=tmp_path / "sessions.db")
        s = store.create(
            task_id="t1", routing="claude_code", origin="operator",
            bot="doctor", status=status,
        )
        if cli_id:
            store.set_claude_code_session_id("t1", cli_id)
            store.update_status("t1", status)  # set_claude_code_session_id keeps status
        store.add_reply_anchors(s.session_id, "t1", [7000], bot="doctor")
        return store, s

    async def _reply(self, listener, store, text, quoted=None):
        sent: list[str] = []

        def _capture_ids(t, chat_id=None, bot=None):
            sent.append(t)
            return [8000 + len(sent)]

        async def _capture_async(t, chat_id=None, bot=None):
            sent.append(t)
            return True

        with patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=store), \
             patch("api.services.telegram.send_message_capture_ids",
                   side_effect=_capture_ids), \
             patch("api.services.telegram.send_message_async",
                   side_effect=_capture_async):
            consumed = await listener._maybe_handle_claude_code_reply(
                7000, text, "999", quoted_text=quoted,
            )
        return consumed, sent

    @pytest.mark.asyncio
    async def test_reply_on_running_session_queues_note_with_context(self, tmp_path):
        listener = self._listener()
        store, s = self._seed(tmp_path, status=STATUS_RUNNING)

        consumed, sent = await self._reply(
            listener, store, "also check the cron logs",
            quoted="Still working — running tests (5m elapsed)",
        )

        assert consumed is True
        assert any("next checkpoint" in t for t in sent)
        pending = store.drain_pending_messages(s.session_id)
        assert len(pending) == 1
        assert "also check the cron logs" in pending[0]["content"]
        # The quoted status update rides along so the agent knows what the
        # operator was replying to.
        assert "Still working — running tests" in pending[0]["content"]
        # RUNNING session: no status flip — the note rides the next boundary.
        assert store.get("t1").status == STATUS_RUNNING

    @pytest.mark.asyncio
    async def test_reply_on_completed_session_reopens_it(self, tmp_path):
        listener = self._listener()
        store, s = self._seed(tmp_path, status=STATUS_COMPLETED)

        consumed, sent = await self._reply(listener, store, "one more thing")

        assert consumed is True
        assert any("waking the session" in t for t in sent)
        assert store.get("t1").status == STATUS_CLAIMED

    @pytest.mark.asyncio
    async def test_reply_on_dead_unresumable_session_says_so(self, tmp_path):
        listener = self._listener()
        store, s = self._seed(tmp_path, status=STATUS_COMPLETED, cli_id=None)

        consumed, sent = await self._reply(listener, store, "one more thing")

        assert consumed is True
        assert any("can't be" in t for t in sent)
        assert store.get("t1").status == STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_ack_is_itself_an_anchor(self, tmp_path):
        """The ack joins the work thread: its captured id registers as another
        anchor, so replying to the ack also reaches the session."""
        listener = self._listener()
        store, s = self._seed(tmp_path, status=STATUS_RUNNING)

        consumed, sent = await self._reply(listener, store, "noted-worthy reply")

        assert consumed is True
        ack_q = store.get_open_question_by_message_id(8001, bot="doctor")
        assert ack_q is not None and ack_q["kind"] == "status_anchor"
        assert ack_q["session_id"] == s.session_id
        # And the ack text carries the replyable footer.
        assert any(t.endswith(REPLYABLE_FOOTER) for t in sent)


# #684 retired `_handle_orchestration_message` (the direct-CC entry doctor
# used for a FRESH message, including its "On it" ack + reply-anchor
# registration) in favor of routing fresh messages through the same chat
# pipeline every other bot uses. The worker-side anchor machinery this test
# exercised (`send_message_capture_ids` + `add_reply_anchors`) is unaffected
# and still fully covered elsewhere in this file (e.g.
# TestReplyAnchorStore, TestFooterAndAnchors) — it's exercised there via a
# session the worker itself sends status/completion messages for, which is
# how a Hermes- or native-fallback-spawned doctor session still gets
# anchored replies once the WORKER (not this retired ack) sends its first
# operator-facing message. `test_blocked_session_note_rides_the_goal_answer`
# below is the closest surviving end-to-end case: a spawned doctor session's
# anchored status message resolves a threaded reply back into it.


class TestNoteRidesGateAnswer:
    @pytest.mark.asyncio
    async def test_blocked_session_note_rides_the_goal_answer(self, tmp_path):
        """A status-anchor reply on a BLOCKED session must not bypass the gate:
        the note queues without a status flip, and when the goal answer later
        arrives, BOTH drain in order onto the same resume turn."""
        from api.services.telegram import TelegramBotListener
        from config.settings import TelegramBotConfig

        w = _make_worker(tmp_path)
        store = w.session_store
        s = store.create(
            task_id="t1", routing="claude_code", origin="operator",
            bot="doctor", status=STATUS_BLOCKED,
        )
        store.set_claude_code_session_id("t1", "cli-1")
        store.update_status("t1", STATUS_BLOCKED)
        # The blocked goal gate: proposed condition + its open question.
        w.transcript_store.append(
            s.session_id, "claude_code_awaiting_goal_approval",
            {"condition": "all tests pass", "condition_chars": 14},
        )
        store.create_pending_question(
            session_id=s.session_id, task_id="t1", question="goal + instructions",
            sent_message_id=5000, sent_message_ids=[5000],
            kind="goal_approval", bot="doctor",
        )
        # An earlier status update registered as an anchor.
        store.add_reply_anchors(s.session_id, "t1", [7000], bot="doctor")

        listener = TelegramBotListener(TelegramBotConfig(
            name="doctor", token="TOK", chat_id="999", persona="P", orchestrates=True,
        ))

        def _capture_ids(text, chat_id=None, bot=None):
            return [8001]

        async def _capture_async(text, chat_id=None, bot=None):
            return True

        with patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=store), \
             patch("api.services.telegram.send_message_capture_ids",
                   side_effect=_capture_ids), \
             patch("api.services.telegram.send_message_async",
                   side_effect=_capture_async):
            # 1. Operator replies to the status update while the gate is open.
            noted = await listener._maybe_handle_claude_code_reply(
                7000, "also check the cron logs", "999",
                quoted_text="Investigating the digest",
            )
            # 2. Then answers the goal gate itself.
            approved = await listener._maybe_handle_claude_code_reply(
                5000, "yes", "999",
            )

        assert noted is True and approved is True
        # The note did NOT bypass the gate.
        assert store.get("t1").status == STATUS_BLOCKED
        # The worker processes the gate answer → /goal injected, CLAIMED.
        w._process_clarification_answers()
        assert store.get("t1").status == STATUS_CLAIMED
        pending = store.drain_pending_messages(s.session_id)
        contents = [m["content"] for m in pending]
        assert len(contents) == 2
        assert "also check the cron logs" in contents[0]      # note first
        assert "Investigating the digest" in contents[0]      # with quoted context
        assert contents[1] == "/goal all tests pass"          # gate answer after
        # Both ride the SAME resume turn (dispatch joins drained messages).


def test_footer_helper_shapes():
    assert _with_reply_footer("body").endswith(f"\n\n{REPLYABLE_FOOTER}")
    assert _with_reply_footer("body", replyable=False).endswith(f"\n\n{NO_REPLY_FOOTER}")
