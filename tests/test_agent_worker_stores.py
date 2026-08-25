"""Unit tests for the agent-worker stores (session, transcript, spend).

Issue B scope. No LLM calls, no HTTP. Each test uses a temp directory so the
real `data/` is never touched.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
    new_session_id,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_session_store_create_and_get(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    s = store.create(task_id="t1", status=STATUS_CLAIMED, routing="local")
    assert s.task_id == "t1"
    assert s.status == STATUS_CLAIMED
    assert s.routing == "local"

    got = store.get("t1")
    assert got is not None
    assert got.session_id == s.session_id
    assert got.routing == "local"


@pytest.mark.unit
def test_session_store_get_missing_returns_none(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    assert store.get("does-not-exist") is None


@pytest.mark.unit
def test_session_store_rejects_duplicate_task_id(tmp_path: Path):
    import sqlite3
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(task_id="t1")
    with pytest.raises(sqlite3.IntegrityError):
        store.create(task_id="t1")


@pytest.mark.unit
def test_session_store_update_status(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(task_id="t1")
    store.update_status("t1", STATUS_RUNNING)
    assert store.get("t1").status == STATUS_RUNNING
    store.update_status("t1", STATUS_COMPLETED)
    assert store.get("t1").status == STATUS_COMPLETED


@pytest.mark.unit
def test_session_store_list_non_terminal(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(task_id="t1", status=STATUS_RUNNING)
    store.create(task_id="t2", status=STATUS_COMPLETED)
    store.create(task_id="t3", status=STATUS_FAILED)
    store.create(task_id="t4", status=STATUS_CLAIMED)

    pending = {s.task_id for s in store.list_non_terminal()}
    assert pending == {"t1", "t4"}


@pytest.mark.unit
def test_record_spend_accumulates_tokens_and_dollars(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(task_id="t1")
    store.record_spend("t1", tokens_in=100, tokens_out=50, dollars=0.01)
    store.record_spend("t1", tokens_in=100, tokens_out=50, dollars=0.01)
    got = store.get("t1")
    assert got.total_input_tokens == 200
    assert got.total_output_tokens == 100
    assert got.total_dollars == pytest.approx(0.02)
    assert got.unpriced is False


@pytest.mark.unit
def test_record_spend_unpriced_flag_is_sticky(tmp_path: Path):
    """Once a session has recorded any unpriced turn, later priced turns
    don't clear the flag (#669) — the reader needs to know the session's
    total is a lower bound for its whole lifetime, not just its last call."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(task_id="t1")
    store.record_spend("t1", tokens_in=100, tokens_out=50, dollars=0.0, unpriced=True)
    assert store.get("t1").unpriced is True
    store.record_spend("t1", tokens_in=100, tokens_out=50, dollars=0.02, unpriced=False)
    got = store.get("t1")
    assert got.unpriced is True
    assert got.total_dollars == pytest.approx(0.02)


@pytest.mark.unit
def test_unpriced_migration_is_idempotent_and_preserves_existing_rows(tmp_path: Path):
    """Simulates opening a pre-#669 DB (no `unpriced` column) with the new
    code. The migration must add the column without touching existing data,
    and running it again (a second SessionStore against the same file) must
    not error or reset anything — this is exactly the shape of the real
    production `data/agent_sessions.db`, which must stay readable."""
    import sqlite3

    db_path = tmp_path / "legacy_sessions.db"
    # Pre-#669 schema: the real `sessions` table shape (every column that
    # exists today) minus `unpriced`, carrying one pre-existing row with
    # real data — this is the actual shape of production `agent_sessions.db`
    # before this migration runs.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                task_id                   TEXT PRIMARY KEY,
                session_id                TEXT UNIQUE NOT NULL,
                status                    TEXT NOT NULL,
                routing                   TEXT,
                budget_json               TEXT,
                started_at                INTEGER NOT NULL,
                last_activity_at          INTEGER NOT NULL,
                total_input_tokens        INTEGER NOT NULL DEFAULT 0,
                total_output_tokens       INTEGER NOT NULL DEFAULT 0,
                total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                total_cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                total_dollars             REAL    NOT NULL DEFAULT 0.0,
                total_active_seconds      REAL    NOT NULL DEFAULT 0.0,
                expected_output           TEXT,
                parent_session_id         TEXT,
                root_session_id           TEXT,
                spawn_depth               INTEGER NOT NULL DEFAULT 0,
                yield_waiting_for         TEXT,
                managed_agent_session_id  TEXT,
                preset_class              TEXT,
                origin                    TEXT,
                claude_code_session_id    TEXT,
                claude_code_model         TEXT,
                bot                       TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions (task_id, session_id, status, started_at, "
            "last_activity_at, total_dollars) VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-1", "sess_legacy1", "completed", 1000, 1000, 1.23),
        )

    # Opening it runs the migration.
    store = SessionStore(db_path=db_path)
    legacy = store.get("legacy-1")
    assert legacy is not None
    assert legacy.total_dollars == pytest.approx(1.23)  # untouched, not backfilled
    assert legacy.unpriced is False  # old rows default to priced, not reclassified

    # A fresh row on the migrated DB works normally.
    store.create(task_id="t-new")
    store.record_spend("t-new", tokens_in=10, tokens_out=5, dollars=0.0, unpriced=True)
    assert store.get("t-new").unpriced is True

    # Re-opening (second migration pass) must be a no-op, not an error.
    store2 = SessionStore(db_path=db_path)
    assert store2.get("legacy-1").total_dollars == pytest.approx(1.23)
    assert store2.get("t-new").unpriced is True


@pytest.mark.unit
def test_new_session_id_is_unique():
    seen = {new_session_id() for _ in range(100)}
    assert len(seen) == 100
    assert all(s.startswith("sess_") for s in seen)


# ---------------------------------------------------------------------------
# TranscriptStore
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_transcript_store_append_and_read(tmp_path: Path):
    store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    sid = "sess_test123"
    store.append(sid, "claim", {"task_id": "t1"})
    store.append(sid, "noop_complete", {"title": "hello world"})

    events = store.read(sid)
    assert len(events) == 2
    assert events[0]["kind"] == "claim"
    assert events[0]["payload"]["task_id"] == "t1"
    assert events[1]["kind"] == "noop_complete"
    assert events[1]["payload"]["title"] == "hello world"


@pytest.mark.unit
def test_transcript_store_persists_across_instances(tmp_path: Path):
    """Append-only JSONL — reopening the store should see prior events."""
    dirpath = tmp_path / "transcripts"
    a = TranscriptStore(transcripts_dir=dirpath)
    a.append("sess_x", "first", {})

    b = TranscriptStore(transcripts_dir=dirpath)
    b.append("sess_x", "second", {})

    events = b.read("sess_x")
    assert [e["kind"] for e in events] == ["first", "second"]


@pytest.mark.unit
def test_transcript_store_read_missing_returns_empty(tmp_path: Path):
    store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    assert store.read("sess_nope") == []


@pytest.mark.unit
def test_transcript_store_rejects_path_traversal(tmp_path: Path):
    store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    with pytest.raises(ValueError):
        store.append("../escape", "claim", {})
    with pytest.raises(ValueError):
        store.append("sub/dir", "claim", {})


# ---------------------------------------------------------------------------
# SpendTracker
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_spend_tracker_starts_empty(tmp_path: Path):
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0)
    assert tr.today_total() == 0.0
    assert tr.can_start_task(50.0)


@pytest.mark.unit
def test_spend_tracker_can_start_at_exact_boundary(tmp_path: Path):
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=10.0)
    tr.record(5.0)
    # 5 + 5 = 10 exactly = cap → allowed (cap is a ceiling we may reach).
    assert tr.can_start_task(5.0)
    # 5 + 5.01 > 10 → denied
    assert not tr.can_start_task(5.01)


@pytest.mark.unit
def test_spend_tracker_zero_cap_denies_any_claim(tmp_path: Path):
    """LIFEOS_AGENT_DAILY_CAP_DOLLARS=0 is the operator's pause signal."""
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=0.0)
    assert not tr.can_start_task(0.0)
    assert not tr.can_start_task(0.01)


@pytest.mark.unit
def test_spend_tracker_negative_cap_treated_as_paused(tmp_path: Path):
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=-1.0)
    assert not tr.can_start_task(1.0)


@pytest.mark.unit
def test_spend_tracker_record_zero_is_noop(tmp_path: Path):
    """record(0) should not create a daily_spend row or change totals."""
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0)
    assert tr.record(0.0) == 0.0
    assert tr.today_total() == 0.0


@pytest.mark.unit
def test_spend_tracker_record_accumulates(tmp_path: Path):
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0)
    tr.record(1.5)
    tr.record(2.25)
    assert tr.today_total() == pytest.approx(3.75)


@pytest.mark.unit
def test_spend_tracker_per_day_buckets(tmp_path: Path):
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0)
    today = date(2026, 1, 1)
    tomorrow = today + timedelta(days=1)
    tr.record(10.0, today=today)
    tr.record(2.0, today=tomorrow)
    assert tr.today_total(today=today) == pytest.approx(10.0)
    assert tr.today_total(today=tomorrow) == pytest.approx(2.0)


@pytest.mark.unit
def test_spend_tracker_rejects_negative(tmp_path: Path):
    tr = SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0)
    with pytest.raises(ValueError):
        tr.record(-1.0)
    with pytest.raises(ValueError):
        tr.can_start_task(-0.5)
