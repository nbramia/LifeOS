"""
Unit tests for `POST /api/crm/relationship/tone-analysis-detailed` persistence,
freshness, batching, and failure handling (#873, tightened by the #899
adversarial review: cross-month contamination, batching, freshness drift,
concurrent dedup, and trend-derivation coverage).

Builds a router-only FastAPI app (see tests/test_route_handlers_concurrency.py
for why: no lifespan side effects) and patches the LLM client, the
interaction store, and the tone analysis store so these tests never touch
`data/` or spend real LLM calls -- they are unit tests, not integration.
"""
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.routes import crm as crm_module
from api.routes.crm import _derive_trend
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
    fixed, deterministic monthly-scores payload covering every month the
    prompt actually asked for (parsed from its "Months to score:" line), or
    raises if configured. `omit_months` simulates a response that silently
    drops a requested month rather than the whole call failing.
    """

    def __init__(self, user_score=80.0, partner_score=60.0, raises: Exception = None, omit_months=None):
        self.calls: list[str] = []
        self._user_score = user_score
        self._partner_score = partner_score
        self._raises = raises
        self._omit_months = set(omit_months or [])

    def create(self, messages, max_tokens=4096):
        prompt = messages[0]["content"]
        self.calls.append(prompt)
        if self._raises is not None:
            raise self._raises

        marker = "Months to score: "
        idx = prompt.find(marker)
        months_line = prompt[idx + len(marker):].split("\n", 1)[0]
        requested_months = [m.strip() for m in months_line.split(",") if m.strip()]

        monthly_scores = [
            {"month": m, "user_score": self._user_score, "partner_score": self._partner_score}
            for m in requested_months
            if m not in self._omit_months
        ]
        text = json.dumps({"monthly_scores": monthly_scores})
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


def _interactions_for_months(months: list) -> list:
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


def _find_straddling_month_boundary():
    """Find a month boundary where the last day of one month and the first
    day of the next share the same %Y-W%W key -- i.e. a real week that
    straddles two calendar months, reproducing the #899 review's finding 1
    repro without hardcoding a specific calendar year (some, not all, month
    boundaries have this property; this recurs multiple times a year, so a
    12-month backward search always finds one).

    Returns (last_day_of_month_M, first_day_of_month_M_plus_1).
    """
    probe = datetime.now(timezone.utc).replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    for _ in range(24):
        prev_month_last_day = probe - timedelta(days=1)
        if prev_month_last_day.strftime("%Y-W%W") == probe.strftime("%Y-W%W"):
            return prev_month_last_day, probe
        probe = prev_month_last_day.replace(day=1)
    raise AssertionError("could not find a straddling month boundary in the last 24 months")


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
    """Patch interaction_store.get_for_person_in_range; returns a setter the
    test calls (any number of times) with the desired synthetic interaction
    list, so a test can simulate new messages arriving between two calls."""
    store = get_interaction_store()
    box = {"interactions": []}

    def _get_for_person_in_range(*args, **kwargs):
        return box["interactions"]

    monkeypatch.setattr(store, "get_for_person_in_range", _get_for_person_in_range)

    def _set(interactions):
        box["interactions"] = interactions

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

        # `client.calls == []` is the load-bearing assertion for the "cache
        # hit avoids the LLM call" acceptance criterion. The wall-clock
        # bound below is the issue's literal 200ms figure and passes
        # comfortably in practice, but it is secondary -- a loaded xdist
        # worker is a more plausible source of flake than the call-count
        # check (#899 review finding 11).
        assert response.status_code == 200
        assert client.calls == []
        assert elapsed < 0.2

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
        # Both missing months are batched into a single LLM call (#899
        # review finding 4), not one call per month.
        assert len(client.calls) == 1

        data = first.json()
        for t in data["monthly_tones"]:
            assert t["user_score"] == 77.0
            assert t["partner_score"] == 66.0

        # Second call must be served entirely from storage now.
        second = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert second.status_code == 200
        assert len(client.calls) == 1  # unchanged -- no new LLM calls

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
        # Both months are forced stale, but still batched into one call
        # (#899 review finding 4) -- a full refresh costs one call, not one
        # per month.
        assert len(client.calls) == 1
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

    def test_straddling_week_does_not_cross_contaminate_or_double_count(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """#899 review finding 1 (BLOCKER): a %W week spanning a month
        boundary used to be looked up in a single week-keyed dict shared by
        both months, so both months' prompts got the *other* month's
        messages too and both months' sample counts roughly doubled. Fixed
        by bucketing month-first, then week-within-month."""
        last_day_of_m, first_day_of_m_plus_1 = _find_straddling_month_boundary()
        month_m_key = last_day_of_m.strftime("%Y-%m")
        month_m_plus_1_key = first_day_of_m_plus_1.strftime("%Y-%m")

        def mk(idx, ts, arrow):
            return Interaction(
                id=f"synthetic-straddle-{idx}", person_id=PARTNER_ID, timestamp=ts,
                source_type="imessage", title=f"{arrow} straddling message {idx}",
            )

        interactions = [
            # 2 messages on the last day of month M (both in the shared week).
            mk(0, last_day_of_m, "→"),
            mk(1, last_day_of_m + timedelta(hours=1), "←"),
            # 2 messages on the first day of month M+1 (same shared week).
            mk(2, first_day_of_m_plus_1, "→"),
            mk(3, first_day_of_m_plus_1 + timedelta(hours=1), "←"),
            # 1 message deeper into month M+1, well outside the shared week.
            mk(4, first_day_of_m_plus_1 + timedelta(days=14), "→"),
        ]
        patch_interactions(interactions)

        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=70.0, partner_score=70.0))
        # A large `months` window comfortably covers however far back the
        # straddling boundary search above had to go.
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed?months=30")

        assert response.status_code == 200
        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        assert month_m_key in by_month
        assert month_m_plus_1_key in by_month

        month_m = by_month[month_m_key]
        month_m_plus_1 = by_month[month_m_plus_1_key]

        # No message appears in two months: exactly 2 in M, exactly 3 in M+1.
        assert month_m["user_sample_count"] + month_m["partner_sample_count"] == 2
        assert month_m_plus_1["user_sample_count"] + month_m_plus_1["partner_sample_count"] == 3
        total_reported = (
            month_m["user_sample_count"] + month_m["partner_sample_count"]
            + month_m_plus_1["user_sample_count"] + month_m_plus_1["partner_sample_count"]
        )
        assert total_reported == len(interactions) == 5

        # Both months batched into one LLM call, not one call per month.
        assert len(client.calls) == 1

    def test_no_drift_for_a_fully_elapsed_month_when_new_messages_arrive_elsewhere(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """#899 review finding 5 (MAJOR): with the old `now - months*30
        days` rolling cutoff and a 10,000-row cap, an old (non-current)
        month's *reported* count could drift purely from wall-clock time or
        new messages elsewhere, forcing a needless recompute on every load.
        With the window snapped to a calendar-month boundary and fetched
        without a row cap, a fully-elapsed month's count must be stable
        across calls even as new messages arrive in the current month."""
        now = datetime.now(timezone.utc)
        old_month_dt = (now.replace(day=1) - timedelta(days=90)).replace(day=15, hour=12, minute=0)
        old_month_key = old_month_dt.strftime("%Y-%m")

        old_interactions = [
            Interaction(
                id=f"old-{i}", person_id=PARTNER_ID, timestamp=old_month_dt + timedelta(hours=i),
                source_type="imessage", title=("→" if i % 2 == 0 else "←") + " old msg",
            )
            for i in range(5)
        ]
        current_interactions_day1 = [
            Interaction(
                id=f"cur-{i}", person_id=PARTNER_ID, timestamp=now - timedelta(hours=i + 1),
                source_type="imessage", title=("→" if i % 2 == 0 else "←") + " current msg",
            )
            for i in range(2)
        ]

        patch_interactions(old_interactions + current_interactions_day1)
        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=60.0, partner_score=60.0))

        first = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert first.status_code == 200
        assert len(client.calls) == 1  # both months batched into one call

        old_row_after_first = tone_store.get_month(PARTNER_ID, old_month_key)
        assert old_row_after_first is not None
        assert old_row_after_first.interaction_count == 5

        # "A day passes": 3 new messages arrive, but only in the current month.
        current_interactions_day2 = current_interactions_day1 + [
            Interaction(
                id=f"cur-new-{i}", person_id=PARTNER_ID, timestamp=now - timedelta(minutes=i + 1),
                source_type="imessage", title="→ new current msg",
            )
            for i in range(3)
        ]
        patch_interactions(old_interactions + current_interactions_day2)

        second = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert second.status_code == 200
        # Exactly one more call, for the current month only -- the old
        # month must not have gone stale again.
        assert len(client.calls) == 2

        old_row_after_second = tone_store.get_month(PARTNER_ID, old_month_key)
        assert old_row_after_second.interaction_count == 5  # unchanged -- no drift
        assert old_row_after_second.updated_at == old_row_after_first.updated_at  # never re-touched

        data = second.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        old_month_point = by_month[old_month_key]
        assert old_month_point["user_sample_count"] + old_month_point["partner_sample_count"] == 5
        assert old_month_point.get("status") is None


class TestFailureHandling:
    def test_llm_call_failing_yields_partial_results_and_never_a_500(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))

        # month_a is cached and fine; month_b is missing and the batched
        # call (which would also have covered it) fails entirely.
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

    def test_llm_client_unreachable_yields_error_status_not_500(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """#899 review finding 7: get_anthropic_llm() itself never raises in
        real code (per its own docstring) -- the real 'LLM unavailable'
        path is the *returned client's* .create() call failing (e.g. the
        local llama-server being unreachable), not client acquisition. The
        handler's broad `except Exception` around both must still turn that
        into a status="error" month rather than a 500."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))

        _patch_llm(monkeypatch, _RecordingLLMClient(
            raises=ConnectionError("synthetic: local llama-server unreachable"),
        ))

        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"][0]["status"] == "error"

    def test_llm_response_omitting_a_requested_month_marks_only_that_month_error(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """#899 review finding 4: when the batched call succeeds but its
        parsed response simply omits one of the requested months, only that
        month is marked status="error" -- the other stale month(s) the
        response did cover are stored and returned normally."""
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))

        client = _RecordingLLMClient(user_score=44.0, partner_score=44.0, omit_months=[month_b])
        _patch_llm(monkeypatch, client)

        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert response.status_code == 200
        assert len(client.calls) == 1  # still one batched call

        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        assert by_month[month_a]["user_score"] == 44.0
        assert by_month[month_a].get("status") is None
        assert by_month[month_b]["status"] == "error"

        # The omitted month must not have been persisted as if it succeeded.
        assert tone_store.get_month(PARTNER_ID, month_b) is None


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
                    text=json.dumps({"monthly_scores": []}),
                    model="fake-slow-model",
                )

        _patch_llm(monkeypatch, _SlowLLMClient())

        with TestClient(_router_only_app()) as client:
            _assert_fast_request_not_blocked(
                client,
                slow_request=lambda c: c.post("/api/crm/relationship/tone-analysis-detailed"),
            )

    def test_concurrent_requests_for_same_person_call_llm_once(
        self, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """#899 review finding 9: 4 concurrent requests for the same person
        with stale months must call the LLM once, not once per request --
        the reviewer measured 12 calls (4 requests x 3 stale months each,
        pre-batching) with no in-flight dedup. A per-person lock plus
        batching (finding 4) means only the request that wins the race
        computes anything; the rest see fresh storage once they acquire the
        lock in turn."""
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))

        class _SlowRecordingClient(_RecordingLLMClient):
            def create(self, messages, max_tokens=4096):
                time.sleep(0.3)
                return super().create(messages, max_tokens=max_tokens)

        client = _SlowRecordingClient(user_score=50.0, partner_score=50.0)
        _patch_llm(monkeypatch, client)

        with TestClient(_router_only_app()) as test_client:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(test_client.post, "/api/crm/relationship/tone-analysis-detailed")
                    for _ in range(4)
                ]
                responses = [f.result(timeout=10) for f in futures]

        assert all(r.status_code == 200 for r in responses)
        assert len(client.calls) == 1  # only the request that won the race called the LLM

        for r in responses:
            data = r.json()
            assert all(t.get("status") is None for t in data["monthly_tones"])


class TestDeriveTrend:
    """Direct branch/threshold coverage for _derive_trend (#899 review
    finding 6) -- the only prior trend assertion exercised the pre-existing
    early return for zero interactions, never the heuristic itself."""

    def test_fewer_than_two_scores_is_insufficient_data(self):
        assert _derive_trend([]) == "insufficient-data"
        assert _derive_trend([70.0]) == "insufficient-data"

    def test_second_half_higher_by_more_than_threshold_is_improving(self):
        assert _derive_trend([40.0, 40.0, 70.0, 70.0]) == "improving"

    def test_second_half_lower_by_more_than_threshold_is_declining(self):
        assert _derive_trend([70.0, 70.0, 40.0, 40.0]) == "declining"

    def test_high_variance_with_no_net_shift_is_variable(self):
        # Halves average the same (diff == 0) but individual scores swing
        # widely (stddev well over 15) -- must not read as "stable".
        assert _derive_trend([90.0, 10.0, 90.0, 10.0]) == "variable"

    def test_stable_high_average_is_stable_positive(self):
        assert _derive_trend([65.0, 65.0, 65.0, 65.0]) == "stable-positive"

    def test_stable_low_average_is_stable_neutral(self):
        assert _derive_trend([45.0, 45.0, 45.0, 45.0]) == "stable-neutral"

    def test_boundary_average_of_exactly_60_is_stable_positive(self):
        # overall_avg >= 60 is the documented boundary condition.
        assert _derive_trend([60.0, 60.0]) == "stable-positive"

    def test_diff_just_under_threshold_does_not_count_as_improving(self):
        # diff of exactly 8 is not > 8, so this must fall through to the
        # variance/average branches rather than "improving".
        result = _derive_trend([50.0, 58.0])
        assert result != "improving"
        assert result != "declining"
