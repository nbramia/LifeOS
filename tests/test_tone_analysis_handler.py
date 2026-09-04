"""
Unit tests for `POST /api/crm/relationship/tone-analysis-detailed` persistence,
freshness, batching, and failure handling, covering cross-month contamination,
batching, freshness drift, concurrent dedup, and trend-derivation.

Builds a router-only FastAPI app (see tests/test_route_handlers_concurrency.py
for why: no lifespan side effects) and patches the LLM client, the
interaction store, and the tone analysis store so these tests never touch
`data/` or spend real LLM calls -- they are unit tests, not integration.
"""
import json
import sqlite3
import threading
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
    straddles two calendar months, without hardcoding a specific calendar
    year (some, not all, month boundaries have this property; this recurs
    multiple times a year, so a 12-month backward search always finds one).

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


def _distinct_months_back(n: int) -> list:
    """n distinct calendar-month keys, stepping back one full calendar
    month at a time from the current month -- guaranteed distinct, unlike
    day-offset arithmetic (`_months_ago(30 * i)`), which can collide near
    month boundaries (a 31-day month plus a 30-day one can put two offsets
    30 days apart in the same calendar month)."""
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month
    months = []
    for _ in range(n):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return sorted(months)


@pytest.fixture
def patch_interactions(monkeypatch):
    """Patch both interaction_store.get_monthly_interaction_counts_in_range
    (the lightweight, no-rows freshness-check query) and
    get_for_person_in_range (the full row fetch, made only when at
    least one month is stale), deriving both from the same synthetic
    interaction list. Returns a setter the test calls (any number of times)
    with the desired list, so a test can simulate new messages arriving
    between two calls. The setter also exposes `.range_fetch_calls` (a list
    that grows by one on each get_for_person_in_range call) so a test can
    assert the row-loading fetch was, or wasn't, made."""
    store = get_interaction_store()
    box = {"interactions": []}
    range_fetch_calls: list = []

    def _get_monthly_interaction_counts_in_range(*args, **kwargs):
        counts: dict = {}
        for interaction in box["interactions"]:
            key = interaction.timestamp.strftime("%Y-%m")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _get_for_person_in_range(*args, **kwargs):
        range_fetch_calls.append(1)
        return box["interactions"]

    monkeypatch.setattr(
        store, "get_monthly_interaction_counts_in_range", _get_monthly_interaction_counts_in_range,
    )
    monkeypatch.setattr(store, "get_for_person_in_range", _get_for_person_in_range)

    def _set(interactions):
        box["interactions"] = interactions

    _set.range_fetch_calls = range_fetch_calls
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
        # bound below passes comfortably in practice, but it is secondary --
        # a loaded xdist worker is a more plausible source of flake than the
        # call-count check.
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
        # Both missing months are batched into a single LLM call, not one
        # call per month.
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
        # Both months are forced stale, but still batched into one call --
        # a full refresh costs one call, not one per month.
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
        """A %W week spanning a month boundary must not leak messages
        between the two months: bucketing is month-first, then
        week-within-month, so each month's prompt and sample count reflect
        only that month's messages."""
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
        """With the window snapped to a calendar-month boundary and fetched
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


class TestWindowStart:
    """The tone-analysis window start snaps to a calendar-month boundary,
    never a rolling day count -- exercised directly here since the drift
    test above (test_no_drift_for_a_fully_elapsed_...) protects the
    row-cap removal but can't pin down the boundary calculation itself."""

    def test_twelve_months_back_lands_on_the_first_of_the_month(self):
        now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        start = crm_module._tone_analysis_window_start(now, 12)
        assert start == datetime(2025, 10, 1, tzinfo=timezone.utc)

    def test_start_is_never_a_rolling_day_count(self):
        now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        start = crm_module._tone_analysis_window_start(now, 12)
        rolling_equivalent = now - timedelta(days=12 * 30)
        assert start != rolling_equivalent
        assert start.day == 1
        assert start.hour == 0 and start.minute == 0 and start.second == 0

    def test_crosses_a_year_boundary_correctly(self):
        now = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        start = crm_module._tone_analysis_window_start(now, 3)
        # 3 months back from Feb 2026, inclusive of Feb itself: Dec, Jan, Feb.
        assert start == datetime(2025, 12, 1, tzinfo=timezone.utc)

    def test_stable_across_the_same_calendar_month(self):
        # Two "now"s on different days of the same month must produce the
        # identical start_date -- the whole point of a calendar-aligned
        # boundary instead of one that drifts daily.
        start_a = crm_module._tone_analysis_window_start(
            datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc), 12,
        )
        start_b = crm_module._tone_analysis_window_start(
            datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc), 12,
        )
        assert start_a == start_b


class TestCacheHitPerformance:
    """The freshness check uses a lightweight, no-rows-loaded query instead
    of loading and bucketing every row in the window, falling through to
    the full row fetch only when at least one month is actually stale."""

    def test_cache_hit_never_calls_the_row_loading_fetch(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        # A fixture large enough that loading rows would be visible even
        # without timing: 12 months, ~50 messages each.
        months = _distinct_months_back(12)
        interactions = []
        for m in months:
            year, mon = (int(p) for p in m.split("-"))
            for j in range(50):
                interactions.append(Interaction(
                    id=f"pad-{m}-{j}", person_id=PARTNER_ID,
                    timestamp=datetime(year, mon, 10, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=j),
                    source_type="imessage",
                    title=("→" if j % 2 == 0 else "←") + f" padding message {j}",
                ))
        patch_interactions(interactions)

        counts: dict = {}
        for interaction in interactions:
            key = interaction.timestamp.strftime("%Y-%m")
            counts[key] = counts.get(key, 0) + 1
        for month, count in counts.items():
            tone_store.upsert(PARTNER_ID, month, count, {
                "user_score": 70.0, "partner_score": 70.0, "combined_score": 70.0,
                "user_sample_count": count // 2, "partner_sample_count": count // 2,
            })

        client = _patch_llm(monkeypatch, _RecordingLLMClient())

        start = time.time()
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed?months=12")
        elapsed = time.time() - start

        assert response.status_code == 200
        assert client.calls == []
        # The load-bearing assertion: the row-loading fetch must never be
        # called on a full cache hit, regardless of how large the window's
        # true row count is.
        assert patch_interactions.range_fetch_calls == []
        assert elapsed < 0.2

        assert len(response.json()["monthly_tones"]) == len(counts)


class TestStaleVsError:
    """A stale month whose recompute fails is served with its last stored
    score and status="stale" -- never discarded. status="error" is reserved
    for a month with no stored data at all."""

    def test_all_stale_months_with_dead_llm_return_stored_scores_marked_stale(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        months = _distinct_months_back(4)
        interactions = []
        for m in months:
            interactions.extend(_interactions_for_months([m]))
        patch_interactions(interactions)

        stored_scores = {}
        for i, m in enumerate(months):
            score = 70.0 + i
            stored_scores[m] = score
            tone_store.upsert(PARTNER_ID, m, 2, {
                "user_score": score, "partner_score": score, "combined_score": score,
                "user_sample_count": 1, "partner_sample_count": 1,
            })
            _age_month(tone_store, PARTNER_ID, m, days_old=crm_module.TONE_FRESHNESS_DAYS + 5)

        _patch_llm(monkeypatch, _RecordingLLMClient(
            raises=TimeoutError("synthetic: local model timed out"),
        ))

        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        assert response.status_code == 200
        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}

        for m in months:
            assert by_month[m]["status"] == "stale"
            assert by_month[m]["user_score"] == stored_scores[m]

        # Stale months' real scores still feed the trend -- not excluded
        # from it the way a true error month (no data at all) is.
        assert data["user_trend"] != "insufficient-data"

        # Still genuinely stored -- not silently "refreshed" behind the
        # scenes -- so a later successful call would still see them as stale.
        for m in months:
            row = tone_store.get_month(PARTNER_ID, m)
            assert row.result["user_score"] == stored_scores[m]

    def test_error_reserved_for_months_with_no_stored_data(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))

        # month_a has a stored value that's since aged out; month_b was
        # never stored at all.
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 65.0, "partner_score": 65.0, "combined_score": 65.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        _age_month(tone_store, PARTNER_ID, month_a, days_old=crm_module.TONE_FRESHNESS_DAYS + 5)

        _patch_llm(monkeypatch, _RecordingLLMClient(raises=RuntimeError("synthetic outage")))
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed")

        assert response.status_code == 200
        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        assert by_month[month_a]["status"] == "stale"
        assert by_month[month_a]["user_score"] == 65.0
        assert by_month[month_b]["status"] == "error"


class TestChunking:
    """Chunking at TONE_MAX_MONTHS_PER_LLM_CALL bounds the blast radius of
    one bad call and lets earlier chunks' results survive a later chunk's
    failure, instead of batching every stale month into a single
    all-or-nothing call."""

    def test_more_than_the_chunk_size_is_split_into_multiple_calls(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        months = _distinct_months_back(7)
        assert len(months) > crm_module.TONE_MAX_MONTHS_PER_LLM_CALL
        interactions = []
        for m in months:
            interactions.extend(_interactions_for_months([m]))
        patch_interactions(interactions)

        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=80.0, partner_score=80.0))
        response = app_client.post("/api/crm/relationship/tone-analysis-detailed?months=200")

        assert response.status_code == 200
        expected_chunks = -(-len(months) // crm_module.TONE_MAX_MONTHS_PER_LLM_CALL)  # ceil
        assert len(client.calls) == expected_chunks

        marker = "Months to score: "
        for call_prompt in client.calls:
            idx = call_prompt.find(marker)
            months_line = call_prompt[idx + len(marker):].split("\n", 1)[0]
            requested = [x.strip() for x in months_line.split(",") if x.strip()]
            assert len(requested) <= crm_module.TONE_MAX_MONTHS_PER_LLM_CALL

        for t in response.json()["monthly_tones"]:
            assert t["user_score"] == 80.0
            assert t.get("status") is None

    def test_a_later_chunk_failing_does_not_undo_an_earlier_chunks_success(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        months = _distinct_months_back(5)  # 5 months -> chunks of [4, 1]
        interactions = []
        for m in months:
            interactions.extend(_interactions_for_months([m]))
        patch_interactions(interactions)

        class _FailSecondChunkClient(_RecordingLLMClient):
            def __init__(self):
                super().__init__(user_score=88.0, partner_score=88.0)
                self._n = 0

            def create(self, messages, max_tokens=4096):
                self._n += 1
                if self._n == 2:
                    raise TimeoutError("synthetic: second chunk timed out")
                return super().create(messages, max_tokens=max_tokens)

        client = _FailSecondChunkClient()
        _patch_llm(monkeypatch, client)

        response = app_client.post("/api/crm/relationship/tone-analysis-detailed?months=200")
        assert response.status_code == 200
        assert client.calls  # at least the first (successful) chunk ran

        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        succeeded = [m for m in months if by_month[m].get("status") is None]
        # None of these months were ever stored before, so the chunk that
        # failed leaves its months as "error", not "stale".
        failed = [m for m in months if by_month[m].get("status") == "error"]

        assert len(succeeded) == crm_module.TONE_MAX_MONTHS_PER_LLM_CALL
        assert len(failed) == len(months) - crm_module.TONE_MAX_MONTHS_PER_LLM_CALL
        for m in succeeded:
            assert by_month[m]["user_score"] == 88.0
            # And genuinely persisted, not just present in this response.
            assert tone_store.get_month(PARTNER_ID, m) is not None


class TestLockTimeout:
    def test_lock_timeout_falls_through_to_storage_only_stale_marked_response(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """If the per-person lock can't be acquired within
        TONE_LOCK_TIMEOUT_SECONDS (another request for the same person is
        already computing), the request must not block on it -- it falls
        through to a storage-only response, marking any month that's
        actually stale as "stale" (it has a real stored score) or "error"
        (it doesn't), rather than hanging."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))

        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 55.0, "partner_score": 55.0, "combined_score": 55.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        _age_month(tone_store, PARTNER_ID, month_a, days_old=crm_module.TONE_FRESHNESS_DAYS + 5)

        # Simulate the lock already being held by another in-flight request
        # for this same person.
        lock = crm_module._get_tone_analysis_lock(PARTNER_ID)
        lock.acquire()
        try:
            client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=99.0, partner_score=99.0))
            start = time.time()
            response = app_client.post("/api/crm/relationship/tone-analysis-detailed")
            elapsed = time.time() - start
        finally:
            lock.release()

        assert response.status_code == 200
        # Waits up to TONE_LOCK_TIMEOUT_SECONDS before giving up -- assert
        # it doesn't hang past that (plus a safety margin), not that it
        # returns instantly.
        assert elapsed < crm_module.TONE_LOCK_TIMEOUT_SECONDS + 2
        assert client.calls == []  # never attempted the LLM while the lock was held

        data = response.json()
        assert data["monthly_tones"][0]["status"] == "stale"
        assert data["monthly_tones"][0]["user_score"] == 55.0

    def test_lock_timeout_fallthrough_rereads_storage_for_freshly_upserted_months(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """The fall-through must re-read storage before assembling its
        response: a month the lock holder finishes and upserts *while this
        request is still waiting* on the lock (but before releasing it)
        must be reported with that fresh score, not the stale
        pre-wait snapshot.

        Reproduces the actual race, not just its end state: month_a is
        genuinely unstored (and so genuinely stale) at the moment this
        request starts and takes its initial snapshot; only ~1.5s into
        this request's 5s wait for the lock does a background thread
        upsert a fresh result -- still without releasing the lock, so this
        request's `acquire()` still times out and must take the
        fall-through path, not the "lock acquired" path."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))  # 2 interactions, unstored

        lock = crm_module._get_tone_analysis_lock(PARTNER_ID)
        lock.acquire()

        def _hold_then_upsert_then_release():
            time.sleep(1.5)  # well within TONE_LOCK_TIMEOUT_SECONDS (5s)
            tone_store.upsert(PARTNER_ID, month_a, 2, {
                "user_score": 77.0, "partner_score": 77.0, "combined_score": 77.0,
                "user_sample_count": 1, "partner_sample_count": 1,
            })
            # Keep holding well past this request's own lock-acquire
            # timeout, so it genuinely times out and falls through rather
            # than acquiring the lock itself.
            time.sleep(crm_module.TONE_LOCK_TIMEOUT_SECONDS)
            lock.release()

        holder = threading.Thread(target=_hold_then_upsert_then_release)
        holder.start()
        try:
            client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=99.0, partner_score=99.0))
            response = app_client.post("/api/crm/relationship/tone-analysis-detailed")
        finally:
            holder.join(timeout=crm_module.TONE_LOCK_TIMEOUT_SECONDS + 5)
            if lock.locked():
                lock.release()

        assert response.status_code == 200
        assert client.calls == []  # never attempted the LLM while the lock was held

        data = response.json()
        assert data["monthly_tones"][0]["status"] is None  # fresh, not stale/error
        assert data["monthly_tones"][0]["user_score"] == 77.0


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
        """get_anthropic_llm() itself never raises (per its own docstring)
        -- the real 'LLM unavailable' path is the *returned client's*
        .create() call failing (e.g. the local llama-server being
        unreachable). The handler's broad `except Exception` around both
        must still turn that into a status="error" month rather than a
        500."""
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
        """When the batched call succeeds but its parsed response simply
        omits one of the requested months, only that month is marked
        status="error" -- the other stale month(s) the response did cover
        are stored and returned normally."""
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
        push GET /api/crm/config past the 100ms bound."""
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
        """4 concurrent requests for the same person with stale months must
        call the LLM once, not once per request: a per-person lock plus
        batching means only the request that wins the race computes
        anything, and the rest see fresh storage once they acquire the
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
    """Direct branch/threshold coverage for _derive_trend's classification
    heuristic."""

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
