"""
Unit tests for ToneAnalysisStore (#873) -- the persistence layer behind
`POST /api/crm/relationship/tone-analysis-detailed`.

Uses a throwaway SQLite file per test (never `data/crm.db`), mirroring the
style of the relationship_insights store tests.
"""
import pytest

from api.services.tone_analysis_store import ToneAnalysisStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return ToneAnalysisStore(str(tmp_path / "crm.db"))


def _sample_result(score: float = 72.0) -> dict:
    return {
        "user_score": score,
        "partner_score": score - 5,
        "combined_score": score - 2.5,
        "user_sample_count": 10,
        "partner_sample_count": 8,
    }


def test_upsert_then_get_for_person_round_trips(store):
    store.upsert(
        person_id="synthetic-person-1",
        period_key="2026-01",
        interaction_count=42,
        result=_sample_result(),
        model="claude-sonnet-5",
    )

    results = store.get_for_person("synthetic-person-1")
    assert len(results) == 1
    r = results[0]
    assert r.person_id == "synthetic-person-1"
    assert r.period_key == "2026-01"
    assert r.interaction_count == 42
    assert r.result == _sample_result()
    assert r.model == "claude-sonnet-5"
    assert r.updated_at is not None


def test_upsert_is_idempotent_on_person_and_period(store):
    """Calling upsert twice for the same person+period replaces the row
    instead of creating a duplicate."""
    store.upsert("synthetic-person-1", "2026-01", 10, _sample_result(50.0))
    store.upsert("synthetic-person-1", "2026-01", 15, _sample_result(80.0))

    results = store.get_for_person("synthetic-person-1")
    assert len(results) == 1
    assert results[0].interaction_count == 15
    assert results[0].result["user_score"] == 80.0


def test_get_for_person_only_returns_that_persons_rows(store):
    store.upsert("synthetic-person-1", "2026-01", 10, _sample_result())
    store.upsert("synthetic-person-2", "2026-01", 20, _sample_result())

    results = store.get_for_person("synthetic-person-1")
    assert len(results) == 1
    assert results[0].person_id == "synthetic-person-1"


def test_get_for_person_orders_by_period_ascending(store):
    store.upsert("synthetic-person-1", "2026-03", 1, _sample_result())
    store.upsert("synthetic-person-1", "2026-01", 1, _sample_result())
    store.upsert("synthetic-person-1", "2026-02", 1, _sample_result())

    results = store.get_for_person("synthetic-person-1")
    assert [r.period_key for r in results] == ["2026-01", "2026-02", "2026-03"]


def test_get_month_returns_none_when_missing(store):
    assert store.get_month("synthetic-person-1", "2026-01") is None


def test_get_month_returns_the_stored_row(store):
    store.upsert("synthetic-person-1", "2026-01", 10, _sample_result())
    r = store.get_month("synthetic-person-1", "2026-01")
    assert r is not None
    assert r.period_key == "2026-01"


def test_init_db_is_idempotent(tmp_path):
    """Re-opening a store against the same DB file must not error even
    though the table already exists (CREATE TABLE IF NOT EXISTS)."""
    db_path = str(tmp_path / "crm.db")
    ToneAnalysisStore(db_path)
    store2 = ToneAnalysisStore(db_path)
    store2.upsert("synthetic-person-1", "2026-01", 1, _sample_result())
    assert len(store2.get_for_person("synthetic-person-1")) == 1
