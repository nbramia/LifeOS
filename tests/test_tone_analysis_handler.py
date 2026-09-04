"""
Unit tests for `POST /api/crm/relationship/tone-analysis-detailed` persistence,
freshness, and failure handling (#873).

Builds a router-only FastAPI app (see tests/test_route_handlers_concurrency.py
for why: no lifespan side effects) and patches the LLM client, the
interaction store, and the tone analysis store so these tests never touch
`data/` or spend real LLM calls -- they are unit tests, not integration.
"""
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.routes import crm as crm_module
from api.services import tone_analysis_store as tone_analysis_store_module
from api.services.interaction_store import Interaction, get_interaction_store
from api.services.tone_analysis_store import ToneAnalysisStore
from tests.test_route_handlers_concurrency import (
    SLEEP_SECONDS,
    _assert_fast_request_not_blocked,
    _router_only_app,
)

pytestmark = pytest.mark.unit

PARTNER_ID = "synthetic-partner-1"


class _RecordingLLMClient:
    """Fake LLM client: records every prompt it's called with and returns a
    fixed, deterministic weekly-scores payload (or raises, if configured)."""

    def __init__(self, user_score=80.0, partner_score=60.0, raises: Exception = None):
        self.calls: list[str] = []
        self._user_score = user_score
        self._partner_score = partner_score
        self._raises = raises

    def create(self, messages, max_tokens=4096):
        self.calls.append(messages[0]["content"])
        if self._raises is not None:
            raise self._raises
        text = (
            '{"weekly_scores": [{"week": "2026-W01", '
            f'"user_score": {self._user_score}, "partner_score": {self._partner_score}}}], '
            '"user_trend": "stable-positive", "partner_trend": "stable-positive"}'
        )
        return SimpleNamespace(text=text, model="fake-tone-model")


def _months_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m")


def _two_distinct_months():
    """Two calendar-month keys guaranteed distinct regardless of today's date."""
    month_a = _months_ago(100)
    month_b = _months_ago(20)
    if month_a == month_b:
        month_a = _months_ago(131)  # push back another lunar month's worth
    return month_a, month_b


def _interactions_for_months(months: list[str]) -> list[Interaction]:
    """Build synthetic imessage interactions, one per month key, each inside
    that calendar month (mid-month, to stay clear of boundaries)."""
    out = []
    for i, month_key in enumerate(months):
        year, mon = (int(p) for p in month_key.split("-"))
        ts = datetime(year, mon, 15, 12, 0, tzinfo=timezone.utc)
        out.append(Interaction(
            id=f"synthetic-msg-{i}-user",
            person_id=PARTNER_ID,
            timestamp=ts,
            source_type="imessage",
            title="→ synthetic outgoing message",
        ))
        out.append(Interaction(
            id=f"synthetic-msg-{i}-partner",
            person_id=PARTNER_ID,
            timestamp=ts + timedelta(hours=1),
            source_type="imessage",
            title="← synthetic incoming message",
        ))
    return out


@pytest.fixture
def tone_store(tmp_path, monkeypatch):
    """A fresh on-disk tone store, installed as the module singleton."""
    store = ToneAnalysisStore(str(tmp_path / "crm.db"))
    monkeypatch.setattr(tone_analysis_store_module, "_tone_analysis_store", store)
    return store


@pytest.fixture
def patch_partner(monkeypatch):
    monkeypatch.setattr(crm_module, "PARTNER_PERSON_ID", PARTNER_ID)


@pytest.fixture
def patch_interactions(monkeypatch):
    """Patch interaction_store.get_for_person; returns a setter the test
    calls with the desired synthetic interaction list."""
    store = get_interaction_store()

    def _set(interactions):
        monkeypatch.setattr(store, "get_for_person", lambda *a, **kw: interactions)

    return _set


def _patch_llm(monkeypatch, client):
    import api.services.llm_client as llm_client_module
    monkeypatch.setattr(llm_client_module, "get_anthropic_llm", lambda: client)
    return client


@pytest.fixture
def app_client():
    with TestClient(_router_only_app()) as client:
        yield client


def _age_month(tone_store: ToneAnalysisStore, person_id: str, period_key: str, days_old: int):
    """Backdate a stored row's updated_at, bypassing upsert (which always
    stamps 'now'), to simulate a result outside the freshness window."""
    old = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    conn = sqlite3.connect(tone_store.db_path)
    try:
        conn.execute(
            "UPDATE tone_analysis_results SET updated_at = ? WHERE person_id = ? AND period_key = ?",
            (old, person_id, period_key),
        )
        conn.commit()
    finally:
        conn.close()


class TestCacheAndFreshness:
    def test_cache_hit_avoids_the_llm_call(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a, month_b = _two_distinct_months()
        interactions = _interactions_for_months([month_a, month_b])
        patch_interactions(interactions)

        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 91.0, "partner_score": 88.0, "combined_score": 89.5,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        tone_store.upsert(PARTNER_ID, month_b, 2, {
            "user_score": 40.0, "partner_score": 45.0, "combined_score": 42.5,
            "user_sample_count": 1, "partner_sample_count": 1,
        })

        client = _patch_llm(monkeypatch, _RecordingLLMClient())

        start = time.time()
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        elapsed = time.time() - start

        assert response.status_code == 200
        assert client.calls == []  # never called the LLM
        assert elapsed < 0.2  # acceptance criterion: under 200ms on a full cache hit

        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        assert by_month[month_a]["user_score"] == 91.0
        assert by_month[month_b]["user_score"] == 40.0
        assert all(t.get("status") is None for t in data["monthly_tones"])

    def test_no_stored_results_computes_then_second_call_is_cached(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))
        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=77.0, partner_score=66.0))

        first = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert first.status_code == 200
        assert len(client.calls) == 2  # one LLM call per missing month

        data = first.json()
        for t in data["monthly_tones"]:
            assert t["user_score"] == 77.0
            assert t["partner_score"] == 66.0

        # Second call must be served entirely from storage now.
        second = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert second.status_code == 200
        assert len(client.calls) == 2  # unchanged -- no new LLM calls

    def test_partial_refresh_recomputes_only_the_missing_month(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))

        # month_a is already fresh and correct; month_b has never been stored.
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 91.0, "partner_score": 88.0, "combined_score": 89.5,
            "user_sample_count": 1, "partner_sample_count": 1,
        })

        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=30.0, partner_score=35.0))
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        assert len(client.calls) == 1  # only the stale/missing month

        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        assert by_month[month_a]["user_score"] == 91.0  # untouched
        assert by_month[month_b]["user_score"] == 30.0  # freshly computed

    def test_stale_month_outside_freshness_window_is_recomputed(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a = _two_distinct_months()[0]
        interactions = _interactions_for_months([month_a])
        patch_interactions(interactions)

        # Correct interaction_count, but updated_at is far outside the
        # freshness window -- must still be treated as stale.
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 10.0, "partner_score": 10.0, "combined_score": 10.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        _age_month(tone_store, PARTNER_ID, month_a, days_old=crm_module.TONE_FRESHNESS_DAYS + 5)

        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=99.0, partner_score=99.0))
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        assert len(client.calls) == 1
        data = response.json()
        assert data["monthly_tones"][0]["user_score"] == 99.0

    def test_interaction_count_change_makes_a_fresh_month_stale(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a = _two_distinct_months()[0]
        # Two interactions now exist for this month, but storage only knows
        # about one -- the count mismatch must force a recompute even
        # though updated_at is brand new.
        patch_interactions(_interactions_for_months([month_a]))
        tone_store.upsert(PARTNER_ID, month_a, 1, {
            "user_score": 10.0, "partner_score": 10.0, "combined_score": 10.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })

        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=55.0, partner_score=55.0))
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        assert len(client.calls) == 1
        assert response.json()["monthly_tones"][0]["user_score"] == 55.0

    def test_refresh_true_recomputes_every_month_even_if_fresh(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 91.0, "partner_score": 88.0, "combined_score": 89.5,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        tone_store.upsert(PARTNER_ID, month_b, 2, {
            "user_score": 40.0, "partner_score": 45.0, "combined_score": 42.5,
            "user_sample_count": 1, "partner_sample_count": 1,
        })

        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=15.0, partner_score=15.0))
        response = app_client.post(
            "/api/crm/relationship/tone-analysis-detailed?refresh=true"
        )

        assert response.status_code == 200
        assert len(client.calls) == 2  # both months recomputed despite being fresh
        for t in response.json()["monthly_tones"]:
            assert t["user_score"] == 15.0

    def test_no_interactions_returns_insufficient_data_without_calling_llm(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        patch_interactions([])
        client = _patch_llm(monkeypatch, _RecordingLLMClient())

        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"] == []
        assert data["user_trend"] == "insufficient-data"
        assert client.calls == []


class TestFailureHandling:
    def test_llm_failure_yields_partial_results_and_never_a_500(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))

        # month_a is cached and fine; month_b is missing and the LLM call
        # for it will raise.
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 91.0, "partner_score": 88.0, "combined_score": 89.5,
            "user_sample_count": 1, "partner_sample_count": 1,
        })

        _patch_llm(monkeypatch, _RecordingLLMClient(raises=RuntimeError("synthetic LLM outage")))
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        assert by_month[month_a]["user_score"] == 91.0
        assert by_month[month_a].get("status") is None
        assert by_month[month_b]["status"] == "error"

        # The failed month must not have been persisted as if it succeeded.
        assert tone_store.get_month(PARTNER_ID, month_b) is None

    def test_llm_backend_unavailable_yields_error_status_not_500(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        from api.services.llm_client import LLMBackendNotConfiguredError
        import api.services.llm_client as llm_client_module

        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))

        def _raise_not_configured():
            raise LLMBackendNotConfiguredError("no backend configured")

        monkeypatch.setattr(llm_client_module, "get_anthropic_llm", _raise_not_configured)

        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"][0]["status"] == "error"


class TestResponseShape:
    def test_response_matches_the_documented_contract(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """Schema/shape oracle: every field the pre-#873 implementation
        promised in ToneAnalysisDetailedResponse / ToneDataPointDetailed is
        still present, plus the new optional `status` marker."""
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))
        _patch_llm(monkeypatch, _RecordingLLMClient(user_score=70.0, partner_score=65.0))

        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert response.status_code == 200
        data = response.json()

        assert set(data.keys()) == {
            "monthly_tones", "user_trend", "partner_trend", "combined_trend",
            "user_average", "partner_average", "generated_at",
        }
        assert len(data["monthly_tones"]) == 2
        for point in data["monthly_tones"]:
            assert set(point.keys()) == {
                "month", "user_score", "partner_score", "combined_score",
                "user_sample_count", "partner_sample_count", "status",
            }


class TestConcurrency:
    def test_fast_config_request_not_blocked_by_concurrent_llm_call(
        self, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """The LLM call itself (not just the interaction fetch) must run off
        the event loop: a slow, patched LLM client in progress must not
        push GET /api/crm/config past the 100ms bound (#873)."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))

        class _SlowLLMClient:
            def create(self, *args, **kwargs):
                time.sleep(SLEEP_SECONDS)
                return SimpleNamespace(
                    text='{"weekly_scores": [], "user_trend": "stable-neutral", "partner_trend": "stable-neutral"}',
                    model="fake-slow-model",
                )

        _patch_llm(monkeypatch, _SlowLLMClient())

        with TestClient(_router_only_app()) as client:
            _assert_fast_request_not_blocked(
                client,
                slow_request=lambda c: c.post("/api/crm/relationship/tone-analysis-detailed"),
            )
