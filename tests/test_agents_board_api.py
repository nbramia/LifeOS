"""API tests for the /agents Kanban board (#850).

Covers GET /api/agents/board, PUT .../board/cards/{id}/lane,
POST .../board/cards/{id}/accept, GET /api/agents/pending-questions,
POST .../pending-questions/{id}/answer, the Hermes label fix, and the
Codex (`cx:`) transcript stream dispatch. Uses temp-dir-backed stores via
monkeypatch so the real vault/data directories are never touched.
"""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Pending questions
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPendingQuestions:
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
