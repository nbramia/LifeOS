"""API tests for the /agents Kanban board (#850).

Covers GET /api/agents/board, PUT .../board/cards/{id}/lane,
POST .../board/cards/{id}/accept, GET /api/agents/pending-questions,
POST .../pending-questions/{id}/answer, the Hermes label fix, and the
Codex (`cx:`) transcript stream dispatch. Uses temp-dir-backed stores via
monkeypatch so the real vault/data directories are never touched.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
import api.services.task_manager as task_manager_module
import api.services.scheduler_store as scheduler_store_module
from api.services.task_manager import TaskManager
from api.services.scheduler_store import SchedulerStore
from api.services.agent_worker.session_store import STATUS_BLOCKED, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore

pytestmark = pytest.mark.unit


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Point every store the board endpoint touches at temp-dir fixtures."""
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    agents_route._label_cache.clear()
    # The board cache (finding #5) is module-level and TTL'd — reset it per
    # test so a prior test's cached board can't leak into this one's stores.
    monkeypatch.setattr(agents_route, "_board_cache", None)
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "_codex_snapshot", lambda: ([], []))

    task_manager = TaskManager(
        vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json",
    )
    monkeypatch.setattr(task_manager_module, "_task_manager", task_manager)

    scheduler_store = SchedulerStore(
        vault_path=tmp_path / "vault", index_path=tmp_path / "scheduler_index.json",
    )
    monkeypatch.setattr(scheduler_store_module, "_scheduler_store", scheduler_store)

    yield task_manager, scheduler_store, session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def client():
    return TestClient(api_main.app)


# ---------------------------------------------------------------------------
# GET /api/agents/board
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetBoard:
    def test_empty_board_shape(self, client, stores):
        r = client.get("/api/agents/board")
        assert r.status_code == 200
        body = r.json()
        assert set(body["lanes"].keys()) == {
            "unassigned", "assigned", "in_progress", "human_queue",
            "scheduled", "review", "done",
        }
        assert "generated_at" in body

    def test_get_board_never_served_from_stream_cache(self, client, stores):
        """Round-2 finding 6(a): GET /board must always build fresh — it
        must never read the TTL'd cache the stream's own tick uses.
        Poisons the cache with a snapshot missing the task and proves GET
        ignores it rather than serving pre-write data (reproduces the
        drawer's own `await putTask(); await fetchBoard()` staleness)."""
        task_manager, *_ = stores
        task = task_manager.create("Notes card", tags=["me"])
        empty_lanes = {lane: [] for lane in (
            "unassigned", "assigned", "in_progress", "human_queue",
            "scheduled", "review", "done",
        )}
        agents_route._board_cache = (time.monotonic(), {"lanes": empty_lanes, "generated_at": 0})
        body = client.get("/api/agents/board").json()
        assert task.id in [c["id"] for c in body["lanes"]["assigned"]]

    def test_task_lands_in_derived_lane_with_full_card_shape(self, client, stores):
        task_manager, *_ = stores
        task_manager.create("Ping the vendor", context="Work", tags=["me"])
        r = client.get("/api/agents/board")
        assert r.status_code == 200
        assigned = r.json()["lanes"]["assigned"]
        assert len(assigned) == 1
        card = assigned[0]
        for key in (
            "id", "title", "notes", "status", "tags", "assignee", "fields",
            "context", "updated_at", "session", "pending_question",
        ):
            assert key in card
        assert card["title"] == "Ping the vendor"
        assert card["assignee"] == "me"
        assert card["session"] is None
        assert card["pending_question"] is None

    def test_agent_blocked_card_carries_pending_question(self, client, stores):
        task_manager, _sched, session_store, _transcript = stores
        task = task_manager.create("Investigate the outage", tags=["agent-blocked"], status="blocked")
        session = session_store.create(task_id=task.id, status=STATUS_BLOCKED)
        session_store.create_pending_question(
            session_id=session.session_id,
            task_id=task.id,
            question="Which environment — staging or prod?",
            sent_message_id=42,
        )
        r = client.get("/api/agents/board")
        human_queue = r.json()["lanes"]["human_queue"]
        assert len(human_queue) == 1
        pq = human_queue[0]["pending_question"]
        assert pq is not None
        assert pq["question"] == "Which environment — staging or prod?"
        assert pq["session_id"] == session.session_id

    def test_locally_scanned_cc_session_does_not_bogus_link_to_a_task(self, client, stores, monkeypatch):
        """#863: `to_session_dict`'s `task_id` used to be the Claude Code
        UUID, so `_build_board`'s `sessions_by_task` join (keyed purely on
        truthiness) treated every locally scanned CC session as if it had a
        real LifeOS task link. A session with no real link (`task_id: None`,
        the corrected shape) must not surface on any task's card."""
        task_manager, *_ = stores
        task = task_manager.create("Ping the vendor", context="Work", tags=["me"])
        monkeypatch.setattr(
            agents_route, "_claude_code_snapshot",
            lambda: ([{
                "session_id": "cc:unrelated-raw-uuid", "task_id": None,
                "status": "running", "last_activity_at": 200,
            }], []),
        )
        r = client.get("/api/agents/board")
        assigned = r.json()["lanes"]["assigned"]
        assert len(assigned) == 1
        assert assigned[0]["id"] == task.id
        assert assigned[0]["session"] is None

    def test_card_carries_its_linked_session(self, client, stores):
        """Round-1 finding 13: `_task_card`'s session join was untested —
        every prior fixture card had `session: None`."""
        task_manager, _sched, session_store, _transcript = stores
        task = task_manager.create("Draft the memo", tags=["codex"])
        session = session_store.create(task_id=task.id, status="running", routing="claude")
        r = client.get("/api/agents/board")
        card = r.json()["lanes"]["assigned"][0]
        assert card["session"] is not None
        assert card["session"]["session_id"] == session.session_id

    def test_card_session_picks_most_recently_active_of_several(self, client, stores, monkeypatch):
        """Round-1 finding 13: with two sessions on the same task, the card
        must carry the one with the latest `last_activity_at`, not just
        whichever the store happened to return first. `sessions` PRIMARY
        KEYs on `task_id` (at most one LifeOS-worker session per task at a
        time), so the second session here models the realistic case of a
        task later resumed via a Claude Code CLI session — the union
        `_task_card` actually has to pick between."""
        task_manager, _sched, session_store, _transcript = stores
        task = task_manager.create("Draft the memo", tags=["codex"])
        older = session_store.create(task_id=task.id, status="completed", routing="claude")
        with session_store._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_activity_at = ? WHERE session_id = ?",
                (100, older.session_id),
            )
        newer_session_id = "cc:newer-session"
        monkeypatch.setattr(
            agents_route, "_claude_code_snapshot",
            lambda: ([{
                "session_id": newer_session_id, "task_id": task.id,
                "status": "running", "last_activity_at": 200,
            }], []),
        )

        r = client.get("/api/agents/board")
        card = r.json()["lanes"]["assigned"][0]
        assert card["session"]["session_id"] == newer_session_id

    def test_review_lane_agent_completed_not_accepted(self, client, stores):
        task_manager, *_ = stores
        task_manager.create("Draft the memo", tags=["agent-completed"], status="done")
        r = client.get("/api/agents/board")
        lanes = r.json()["lanes"]
        assert len(lanes["review"]) == 1
        assert lanes["done"] == []

    def test_scheduled_cron_entry_with_future_fire(self, client, stores):
        _tm, scheduler_store, *_ = stores
        scheduler_store.create(
            name="Morning briefing", schedule_type="cron", schedule_value="0 9 * * *",
            message_type="static", message_content="Good morning",
        )
        r = client.get("/api/agents/board")
        lanes = r.json()["lanes"]
        assert len(lanes["scheduled"]) == 1
        card = lanes["scheduled"][0]
        assert card["recurring"] is True
        assert card["next_fire_at"] is not None
        assert card["last_run"] is None
        assert lanes["done"] == []

    def test_scheduled_cron_entry_carries_last_run_after_it_fires(self, client, stores):
        """Round-1 finding 16: a recurring entry that has already fired once
        but is still enabled with a future next trigger stays in Scheduled —
        its `last_run` must still be populated from the prior fire."""
        _tm, scheduler_store, *_ = stores
        entry = scheduler_store.create(
            name="Morning briefing", schedule_type="cron", schedule_value="0 9 * * *",
            message_type="static", message_content="Good morning",
        )
        scheduler_store.mark_triggered(entry.id)
        scheduler_store.record_run(entry.id, "sent", "delivered the briefing")

        r = client.get("/api/agents/board")
        lanes = r.json()["lanes"]
        assert lanes["done"] == []
        assert len(lanes["scheduled"]) == 1
        card = lanes["scheduled"][0]
        assert card["last_run"] is not None
        assert card["last_run"]["outcome"] == "sent"
        assert card["last_run"]["snippet"] == "delivered the briefing"

    def test_disabled_recurring_schedule_shows_in_done(self, client, stores):
        _tm, scheduler_store, *_ = stores
        entry = scheduler_store.create(
            name="Retired job", schedule_type="cron", schedule_value="0 9 * * *",
            message_type="static", message_content="x",
        )
        scheduler_store.update(entry.id, enabled=False)
        r = client.get("/api/agents/board")
        lanes = r.json()["lanes"]
        assert lanes["scheduled"] == []
        assert len(lanes["done"]) == 1

    def test_fired_one_off_shows_in_done_not_scheduled(self, client, stores):
        _tm, scheduler_store, *_ = stores
        entry = scheduler_store.create(
            name="One-time nudge", schedule_type="once",
            schedule_value="2020-01-01T09:00:00+00:00",
            message_type="static", message_content="x",
        )
        scheduler_store.mark_triggered(entry.id)
        scheduler_store.record_run(entry.id, "sent", "delivered")
        r = client.get("/api/agents/board")
        lanes = r.json()["lanes"]
        assert lanes["scheduled"] == []
        assert len(lanes["done"]) == 1
        assert lanes["done"][0]["last_run"]["outcome"] == "sent"


# ---------------------------------------------------------------------------
# GET /api/agents/board/stream
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBoardStream:
    async def test_stream_emits_a_second_frame_after_a_task_mutation(self, stores):
        """Round-1 finding 12(a): the SSE path itself (GET
        /api/agents/board/stream) was never opened by any test — only
        _build_board() was called directly. Drive the real generator
        directly rather than through TestClient — its `_TestClientTransport`
        fully drains an ASGI call before returning a response, and this
        generator never completes on its own — and prove a task mutation
        produces a second, different frame on the path the page actually
        uses ("The board updates within three seconds of an external vault
        edit without a page reload.")."""
        task_manager, *_ = stores
        task = task_manager.create("Ping the vendor")

        resp = await agents_route.stream_board()
        gen = resp.body_iterator
        next_task = None
        try:
            first = await gen.__anext__()
            assert first == ": ok\n\n"

            second = await gen.__anext__()
            assert second.startswith("event: board\n")
            first_board = json.loads(second.split("data: ", 1)[1])
            assert task.id in [c["id"] for c in first_board["lanes"]["unassigned"]]

            # Round-2 finding 10: without a mutation, ticks must not emit —
            # the signature-diff suppression, not "any frame that shows up".
            # Use asyncio.wait (not wait_for) so a timeout leaves the pending
            # __anext__() task running rather than cancelling it — cancelling
            # an async generator's __anext__() closes the generator, which
            # would make every subsequent __anext__() raise StopAsyncIteration.
            next_task = asyncio.ensure_future(gen.__anext__())
            done, _pending = await asyncio.wait(
                {next_task}, timeout=2 * agents_route._BOARD_STREAM_INTERVAL,
            )
            assert next_task not in done, (
                "stream emitted a frame despite no board mutation "
                "(signature-diff suppression broken)"
            )

            task_manager.update(task.id, tags=["me"])

            third = await next_task
            assert third.startswith("event: board\n")
            second_board = json.loads(third.split("data: ", 1)[1])
            assert task.id in [c["id"] for c in second_board["lanes"]["assigned"]]
            assert task.id not in [c["id"] for c in second_board["lanes"]["unassigned"]]
        finally:
            # If anything between asyncio.wait and `await next_task` raises,
            # next_task is still pending — closing the generator while its
            # own __anext__() is still outstanding raises "aclose(): asynchronous
            # generator is already running" and masks the real error
            # (#850 round-3 finding 4). Cancel it first so aclose() sees a
            # generator that isn't mid-iteration.
            if next_task is not None and not next_task.done():
                next_task.cancel()
                await asyncio.gather(next_task, return_exceptions=True)
            await gen.aclose()


# ---------------------------------------------------------------------------
# PUT /api/agents/board/cards/{id}/lane
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMoveBoardCard:
    def test_missing_card_is_404(self, client, stores):
        r = client.put("/api/agents/board/cards/does-not-exist/lane", json={"lane": "done"})
        assert r.status_code == 404

    def test_done_marks_task_done(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Ship the release")
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": "done"})
        assert r.status_code == 200
        assert r.json()["lane"] == "done"
        assert task_manager.get(task.id).status == "done"

    def test_unassigned_removes_assignee_tag(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Review the PR", tags=["codex", "urgent-work"])
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": "unassigned"})
        assert r.status_code == 200
        assert r.json()["lane"] == "unassigned"
        assert sorted(task_manager.get(task.id).tags) == ["urgent-work"]

    def test_assigned_sets_codex_and_removes_other_assignee(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Fix the flaky test", tags=["me"])
        r = client.put(
            f"/api/agents/board/cards/{task.id}/lane",
            json={"lane": "assigned", "assignee": "codex"},
        )
        assert r.status_code == 200
        updated = task_manager.get(task.id)
        assert sorted(updated.tags) == ["codex"]

    def test_in_progress_on_agent_assigned_task_is_409(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Write the migration", tags=["codex"])
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": "in_progress"})
        assert r.status_code == 409
        # Unmodified — a rejected move must not have written anything.
        assert task_manager.get(task.id).status == "todo"

    def test_in_progress_on_me_assigned_task_succeeds(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Send the invoice", tags=["me"])
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": "in_progress"})
        assert r.status_code == 200
        assert task_manager.get(task.id).status == "in_progress"

    def test_review_lane_is_not_directly_settable(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Something")
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": "review"})
        assert r.status_code == 400

    def test_human_queue_card_dropped_into_done_lands_in_done(self, client, stores):
        """Round-1 finding 1: a #human + blocked card dropped into Done must
        actually leave Human queue — the stale `human` tag used to keep it
        there even after `status` was written to `done`."""
        task_manager, *_ = stores
        task = task_manager.create("Escalated to the operator", tags=["human"], status="blocked")
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": "done"})
        assert r.status_code == 200
        assert r.json()["lane"] == "done"
        updated = task_manager.get(task.id)
        assert updated.status == "done"
        assert "human" not in updated.tags

        board = client.get("/api/agents/board").json()
        done_ids = [c["id"] for c in board["lanes"]["done"]]
        human_queue_ids = [c["id"] for c in board["lanes"]["human_queue"]]
        assert task.id in done_ids
        assert task.id not in human_queue_ids

    @pytest.mark.parametrize("worker_tag", ["agent-running", "agent-blocked"])
    @pytest.mark.parametrize("target_lane", ["in_progress", "done"])
    def test_worker_owned_card_cannot_be_dropped_on_in_progress_or_done(
        self, client, stores, worker_tag, target_lane,
    ):
        """Round-2 finding 1: a worker-owned card (agent-running or
        agent-blocked) must 409 for In progress and Done, with NO write at
        all — round-1's tag-strip silently detached these from a live
        worker task instead."""
        task_manager, *_ = stores
        task = task_manager.create("Being worked by the agent", tags=["agent", worker_tag])
        inbox = task_manager.tasks_dir / "Inbox.md"
        before = inbox.read_bytes()

        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": target_lane})
        assert r.status_code == 409
        assert r.json()["detail"] == (
            "the worker owns this task while it is running or waiting on an "
            "answer — answer or kill the session first"
        )
        # No write at all — the vault file is byte-for-byte unchanged.
        assert inbox.read_bytes() == before
        updated = task_manager.get(task.id)
        assert sorted(updated.tags) == sorted(["agent", worker_tag])

    @pytest.mark.parametrize("target_lane", ["in_progress", "human_queue"])
    def test_review_card_cannot_be_moved_to_in_progress_or_human_queue(
        self, client, stores, target_lane,
    ):
        """Round-2 finding 2(a): a pending review (agent-completed, not yet
        accepted) must 409 rather than silently writing status/tags while
        the card stays in Review — only Done still doubles as accept."""
        task_manager, *_ = stores
        task = task_manager.create("Reviewed by the operator", tags=["me", "agent-completed"], status="done")
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": target_lane})
        assert r.status_code == 409
        assert r.json()["detail"] == "accept the review first"
        updated = task_manager.get(task.id)
        assert updated.status == "done"
        assert sorted(updated.tags) == ["agent-completed", "me"]

        board = client.get("/api/agents/board").json()
        review_ids = [c["id"] for c in board["lanes"]["review"]]
        assert task.id in review_ids

    def test_review_card_dropped_on_done_still_accepts(self, client, stores):
        """Done keeps acting as the accept path for a Review card — only
        In progress and Human queue were narrowed to 409 (round-2 finding 2a)."""
        task_manager, *_ = stores
        task = task_manager.create("Reviewed by the operator", tags=["me", "agent-completed"], status="done")
        r = client.put(f"/api/agents/board/cards/{task.id}/lane", json={"lane": "done"})
        assert r.status_code == 200
        assert r.json()["lane"] == "done"
        updated = task_manager.get(task.id)
        assert "accepted" in updated.tags
        assert updated.status == "done"


# ---------------------------------------------------------------------------
# POST /api/agents/board/cards/{id}/accept
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAcceptBoardCard:
    def test_accept_moves_review_to_done(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Refactor the parser", tags=["agent-completed"], status="done")
        r = client.post(f"/api/agents/board/cards/{task.id}/accept")
        assert r.status_code == 200
        assert r.json()["lane"] == "done"
        updated = task_manager.get(task.id)
        assert "accepted" in updated.tags
        assert updated.status == "done"

    def test_accept_is_idempotent(self, client, stores):
        task_manager, *_ = stores
        task = task_manager.create("Refactor the parser", tags=["agent-completed"], status="done")
        r1 = client.post(f"/api/agents/board/cards/{task.id}/accept")
        updated_at_1 = task_manager.get(task.id).updated_at
        r2 = client.post(f"/api/agents/board/cards/{task.id}/accept")
        assert r1.status_code == 200
        assert r2.status_code == 200
        updated = task_manager.get(task.id)
        assert updated.tags.count("accepted") == 1
        # Second call is a true no-op — no write, so updated_at is unchanged.
        assert updated.updated_at == updated_at_1

    def test_accept_missing_card_is_404(self, client, stores):
        r = client.post("/api/agents/board/cards/nope/accept")
        assert r.status_code == 404

    def test_accept_on_non_review_card_is_409(self, client, stores):
        """Round-1 finding 6: /accept had no Review-lane guard — it would
        happily mark any todo card done."""
        task_manager, *_ = stores
        task = task_manager.create("A plain todo, never touched by the worker")
        r = client.post(f"/api/agents/board/cards/{task.id}/accept")
        assert r.status_code == 409
        updated = task_manager.get(task.id)
        assert updated.status == "todo"
        assert "accepted" not in updated.tags


# ---------------------------------------------------------------------------
# Pending questions
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPendingQuestions:
    def test_answer_invalidates_the_stream_cache(self, client, stores):
        """Round-2 finding 6(c): answering a question must invalidate
        `_board_cache` like a lane-move/accept write does, so the stream's
        next tick doesn't keep serving a pre-answer board for the rest of
        the TTL."""
        task_manager, _sched, session_store, _transcript = stores
        task = task_manager.create("Investigate the outage", tags=["agent-blocked"], status="blocked")
        session = session_store.create(task_id=task.id, status=STATUS_BLOCKED)
        qid = session_store.create_pending_question(
            session_id=session.session_id, task_id=task.id, question="Staging or prod?",
            sent_message_id=1,
        )
        agents_route._board_cache = (time.monotonic(), {"stale": True})
        r = client.post(f"/api/agents/pending-questions/{qid}/answer", json={"answer": "staging"})
        assert r.status_code == 200
        assert agents_route._board_cache is None

    def test_list_and_answer_matches_deposit_answer_columns(self, client, stores):
        _tm, _sched, session_store, _transcript = stores
        session = session_store.create(task_id="t1", status=STATUS_BLOCKED)
        qid = session_store.create_pending_question(
            session_id=session.session_id,
            task_id="t1",
            question="Staging or prod?",
            sent_message_id=99,
        )

        r = client.get("/api/agents/pending-questions")
        assert r.status_code == 200
        questions = r.json()["questions"]
        assert len(questions) == 1
        assert questions[0]["id"] == qid
        assert questions[0]["question"] == "Staging or prod?"
        assert questions[0]["task_id"] == "t1"
        assert questions[0]["session_id"] == session.session_id

        r2 = client.post(f"/api/agents/pending-questions/{qid}/answer", json={"answer": "staging"})
        assert r2.status_code == 200

        # The row now looks exactly like it would after a Telegram reply via
        # deposit_answer: answer + answered_at set, nothing else touched.
        with session_store._connect() as conn:
            row = dict(conn.execute(
                "SELECT * FROM pending_questions WHERE id = ?", (qid,),
            ).fetchone())
        assert row["answer"] == "staging"
        assert row["answered_at"] is not None
        assert row["processed"] == 0
        assert row["timed_out"] == 0

        # Answered questions drop off the open list.
        r3 = client.get("/api/agents/pending-questions")
        assert r3.json()["questions"] == []

    def test_answer_matches_deposit_answer_effect_exactly(self, stores):
        """Same row state whether answered via deposit_answer (Telegram) or
        deposit_answer_by_id (board) — the shape worker.py consumes."""
        _tm, _sched, session_store, _transcript = stores
        s1 = session_store.create(task_id="t1", status=STATUS_BLOCKED)
        s2 = session_store.create(task_id="t2", status=STATUS_BLOCKED)
        qid1 = session_store.create_pending_question(
            session_id=s1.session_id, task_id="t1", question="Q1", sent_message_id=1,
        )
        qid2 = session_store.create_pending_question(
            session_id=s2.session_id, task_id="t2", question="Q2", sent_message_id=2,
        )

        session_store.deposit_answer(1, "via telegram")
        session_store.deposit_answer_by_id(qid2, "via board")

        with session_store._connect() as conn:
            row1 = dict(conn.execute("SELECT * FROM pending_questions WHERE id = ?", (qid1,)).fetchone())
            row2 = dict(conn.execute("SELECT * FROM pending_questions WHERE id = ?", (qid2,)).fetchone())
        assert row1["answer"] == "via telegram"
        assert row2["answer"] == "via board"
        for row in (row1, row2):
            assert row["answered_at"] is not None
            assert row["processed"] == 0
            assert row["timed_out"] == 0

    def test_answer_empty_string_is_400(self, client, stores):
        _tm, _sched, session_store, _transcript = stores
        session = session_store.create(task_id="t1", status=STATUS_BLOCKED)
        qid = session_store.create_pending_question(
            session_id=session.session_id, task_id="t1", question="Q", sent_message_id=1,
        )
        r = client.post(f"/api/agents/pending-questions/{qid}/answer", json={"answer": "  "})
        assert r.status_code == 400

    def test_answer_unknown_question_is_404(self, client, stores):
        r = client.post("/api/agents/pending-questions/999999/answer", json={"answer": "x"})
        assert r.status_code == 404

    def test_answer_bare_empty_string_is_400_via_min_length(self, client, stores):
        """Round-1 finding 10: `answer` now has a min_length, so a bare empty
        string is rejected by Pydantic validation (whitespace still 400s via
        the handler's own strip check — see test_answer_empty_string_is_400
        above). This app's `RequestValidationError` handler (api/main.py)
        converts every validation failure to 400, not FastAPI's default 422
        — matching every other `min_length` field in this codebase — so both
        paths land on the same status code."""
        _tm, _sched, session_store, _transcript = stores
        session = session_store.create(task_id="t1", status=STATUS_BLOCKED)
        qid = session_store.create_pending_question(
            session_id=session.session_id, task_id="t1", question="Q", sent_message_id=1,
        )
        r = client.post(f"/api/agents/pending-questions/{qid}/answer", json={"answer": ""})
        assert r.status_code == 400

    def test_status_anchor_row_excluded_from_pending_questions(self, client, stores):
        """Round-1 finding 14: `status_anchor` rows are routing plumbing (see
        `add_reply_anchors`), never a real question."""
        _tm, _sched, session_store, _transcript = stores
        session = session_store.create(task_id="t1", status=STATUS_BLOCKED)
        session_store.add_reply_anchors(
            session_id=session.session_id, task_id="t1", message_ids=[5],
        )
        r = client.get("/api/agents/pending-questions")
        assert r.json()["questions"] == []

    def test_answering_already_answered_question_is_404(self, client, stores):
        """Round-1 finding 14: the second answer attempt on the same
        question must 404, not silently overwrite the first answer."""
        _tm, _sched, session_store, _transcript = stores
        session = session_store.create(task_id="t1", status=STATUS_BLOCKED)
        qid = session_store.create_pending_question(
            session_id=session.session_id, task_id="t1", question="Q", sent_message_id=1,
        )
        r1 = client.post(f"/api/agents/pending-questions/{qid}/answer", json={"answer": "first"})
        assert r1.status_code == 200
        r2 = client.post(f"/api/agents/pending-questions/{qid}/answer", json={"answer": "second"})
        assert r2.status_code == 404
        with session_store._connect() as conn:
            row = dict(conn.execute(
                "SELECT answer FROM pending_questions WHERE id = ?", (qid,),
            ).fetchone())
        assert row["answer"] == "first"

    def test_followup_row_excluded_from_pending_questions(self, client, stores):
        """Round-1 finding 7: `kind='followup'` rows are completion notices,
        not real questions — they must not render a fake pending-question
        badge on a Review card."""
        task_manager, _sched, session_store, _transcript = stores
        task = task_manager.create(
            "Draft the memo", tags=["codex", "agent-completed"], status="done",
        )
        session = session_store.create(task_id=task.id, status="completed")
        session_store.register_completion_followup(
            session_id=session.session_id, task_id=task.id,
            sent_message_ids=[7], label="Draft the memo",
        )

        r = client.get("/api/agents/pending-questions")
        assert r.json()["questions"] == []

        board = client.get("/api/agents/board").json()
        review_cards = board["lanes"]["review"]
        assert len(review_cards) == 1
        assert review_cards[0]["pending_question"] is None


# ---------------------------------------------------------------------------
# Hermes label fix + Codex stream dispatch (#850)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHermesLabelAndCodexStream:
    def test_hermes_routing_labeled_hermes_not_claude(self, client, stores):
        session_store = stores[2]
        s = session_store.create(task_id="t1", status="running", routing="hermes")
        r = client.get("/api/agents/snapshot")
        assert r.status_code == 200
        sessions = {sd["session_id"]: sd for sd in r.json()["sessions"]}
        assert sessions[s.session_id]["model_label"] == "Hermes"

    def test_codex_stream_dispatches_to_codex_ingest_not_lifeos_store(self, client, stores, monkeypatch):
        """Before #850, /sessions/{id}/stream only special-cased `cc:` — a
        `cx:` id fell through to the LifeOS transcript store's path-traversal
        guard and 400'd. It must now dispatch to the Codex ingest path."""
        called = {}

        async def fake_stream_codex(session_id, backfill):
            called["session_id"] = session_id
            yield ": ok\n\n"
            yield "event: transcript_event\ndata: {\"kind\": \"codex_test\"}\n\n"

        monkeypatch.setattr(agents_route, "_stream_codex_session", fake_stream_codex)
        monkeypatch.setattr(agents_route, "_codex_enabled", lambda: True)
        monkeypatch.setattr(
            "api.services.codex.session_ingest.validate_session_id",
            lambda sid: sid[len("cx:"):],
        )

        with client.stream("GET", "/api/agents/sessions/cx:abc123/stream") as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert called["session_id"] == "cx:abc123"
        assert "codex_test" in body

    def test_codex_stream_disabled_is_404(self, client, stores, monkeypatch):
        monkeypatch.setattr(agents_route, "_codex_enabled", lambda: False)
        r = client.get("/api/agents/sessions/cx:abc123/stream")
        assert r.status_code == 404

    async def test_stream_codex_session_actually_reads_a_rollout_file(self, stores, monkeypatch, tmp_path):
        """Round-1 finding 17: `_stream_codex_session` itself was never
        executed — the dispatch test above monkeypatches the whole generator
        away. Point `settings.codex_sessions_dir` at a synthetic rollout and
        drive the real generator directly (not through TestClient — its
        `_TestClientTransport` fully drains an ASGI call before returning a
        response, and this generator only ends on a 300s idle timeout, so a
        real HTTP round trip through it can't complete inside a unit test);
        it should backfill the file's events unchanged."""
        from tests.test_codex_ingest import _write_rollout, _session_meta, _agent_event_msg
        from config.settings import settings

        sessions_dir = tmp_path / "codex_sessions"
        _write_rollout(sessions_dir, "cafe1234", [
            _session_meta(),
            _agent_event_msg(text="hello from a synthetic codex rollout"),
        ])
        monkeypatch.setattr(settings, "codex_sessions_dir", str(sessions_dir))

        gen = agents_route._stream_codex_session("cx:cafe1234", 50)
        chunks = [await gen.__anext__() for _ in range(3)]
        await gen.aclose()

        body = "".join(chunks)
        assert chunks[0] == ": ok\n\n"
        assert body.count("event: transcript_event") == 2
        assert '"kind": "system_session_meta"' in body
        assert '"kind": "assistant_message"' in body
        assert "hello from a synthetic codex rollout" in body
