"""
Unit tests for `POST /api/crm/relationship/tone-analysis`, the compact
combined-score summary that shares its persisted store, chunked-LLM
pipeline, and per-person lock with `/relationship/tone-analysis-detailed`
via `_run_tone_analysis` (see tests/test_tone_analysis_handler.py for that
shared pipeline's own persistence/freshness/batching/failure coverage).

Builds a router-only FastAPI app (see tests/test_route_handlers_concurrency.py
for why: no lifespan side effects) and patches the LLM client, the
interaction store, and the tone analysis store so these tests never touch
`data/` or spend real LLM calls -- they are unit tests, not integration.
"""
import pytest
from fastapi.testclient import TestClient

from api.routes import crm as crm_module
from api.services import tone_analysis_store as tone_analysis_store_module
from api.services.interaction_store import get_interaction_store
from api.services.tone_analysis_store import ToneAnalysisStore
from tests.test_route_handlers_concurrency import _router_only_app
from tests.test_tone_analysis_handler import (
    PARTNER_ID,
    _RecordingLLMClient,
    _interactions_for_months,
    _patch_llm,
    _two_distinct_months,
)

pytestmark = pytest.mark.unit


# tone_store/patch_partner/patch_interactions duplicate
# tests/test_tone_analysis_handler.py's fixtures of the same name rather
# than importing them: a fixture function imported under its own name and
# then used as a same-named test parameter shadows that import binding at
# every use site.
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
    """Patch both interaction_store.get_monthly_interaction_counts_in_range
    (the lightweight, no-rows freshness-check query) and
    get_for_person_in_range (the full row fetch, made only when at least
    one month is stale), deriving both from the same synthetic interaction
    list. Returns a setter the test calls with the desired list."""
    store = get_interaction_store()
    box = {"interactions": []}

    def _get_monthly_interaction_counts_in_range(*args, **kwargs):
        counts: dict = {}
        for interaction in box["interactions"]:
            key = interaction.timestamp.strftime("%Y-%m")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _get_for_person_in_range(*args, **kwargs):
        return box["interactions"]

    monkeypatch.setattr(
        store, "get_monthly_interaction_counts_in_range", _get_monthly_interaction_counts_in_range,
    )
    monkeypatch.setattr(store, "get_for_person_in_range", _get_for_person_in_range)

    def _set(interactions):
        box["interactions"] = interactions

    return _set


@pytest.fixture
def app_client():
    with TestClient(_router_only_app()) as client:
        yield client


def _fail_if_llm_requested(monkeypatch):
    """Patch `get_anthropic_llm` to raise if it's ever called, for asserting
    that `compute=false` never acquires an LLM client at all."""
    def _fail():
        raise AssertionError("compute=false must never acquire an LLM client")
    monkeypatch.setattr("api.services.llm_client.get_anthropic_llm", _fail)


class TestComputeFalseNeverCallsLLM:
    def test_no_stored_data_returns_empty_not_analyzed(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """No stored result anywhere in the window: 200, empty list,
        trend "not-analyzed" -- never a request for an LLM client."""
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))
        _fail_if_llm_requested(monkeypatch)

        response = app_client.post("/api/crm/relationship/tone-analysis")
        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"] == []
        assert data["trend"] == "not-analyzed"
        assert data["analyzed_through"] is None

    def test_stored_months_are_returned_without_computing(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """Every month already has a fresh stored result -- the response
        reflects storage exactly, and no LLM client may be acquired."""
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
        _fail_if_llm_requested(monkeypatch)

        response = app_client.post("/api/crm/relationship/tone-analysis")
        assert response.status_code == 200
        data = response.json()
        by_month = {t["month"]: t for t in data["monthly_tones"]}
        assert by_month[month_a]["score"] == 89.5
        assert by_month[month_b]["score"] == 42.5
        assert all(t["status"] is None for t in data["monthly_tones"])
        assert data["analyzed_through"] == month_b  # chronologically last stored month
        assert data["average"] == pytest.approx((89.5 + 42.5) / 2, abs=0.01)

    def test_stale_month_is_included_with_stale_status_but_not_recomputed(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """A stored month whose interaction count doesn't match the current
        count is still returned (with its last stored score) marked
        `status="stale"` -- compute=false reports staleness but never
        resolves it."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))  # 2 interactions now exist
        tone_store.upsert(PARTNER_ID, month_a, 1, {  # stored count says only 1
            "user_score": 10.0, "partner_score": 10.0, "combined_score": 10.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        _fail_if_llm_requested(monkeypatch)

        response = app_client.post("/api/crm/relationship/tone-analysis")
        assert response.status_code == 200
        data = response.json()
        assert len(data["monthly_tones"]) == 1
        assert data["monthly_tones"][0]["status"] == "stale"
        assert data["monthly_tones"][0]["score"] == 10.0  # last stored value, unchanged

    def test_month_never_stored_is_left_out_not_placeholder_scored(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """A month in the window with no stored result at all is left out
        of `monthly_tones` entirely -- never included with a fabricated
        score, unlike the detailed endpoint's placeholder+status="error"."""
        month_a, month_b = _two_distinct_months()
        patch_interactions(_interactions_for_months([month_a, month_b]))
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 80.0, "partner_score": 80.0, "combined_score": 80.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        # month_b has never been stored.
        _fail_if_llm_requested(monkeypatch)

        response = app_client.post("/api/crm/relationship/tone-analysis")
        assert response.status_code == 200
        data = response.json()
        months = [t["month"] for t in data["monthly_tones"]]
        assert months == [month_a]
        assert data["analyzed_through"] == month_a


class TestComputeTrue:
    def test_compute_true_persists_then_compute_false_reads_it_back(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """compute=true runs the same recompute-and-persist pipeline the
        detailed endpoint uses; a later compute=false call must see the
        persisted result without touching the LLM again."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))
        client = _patch_llm(monkeypatch, _RecordingLLMClient(user_score=77.0, partner_score=66.0))

        first = app_client.post("/api/crm/relationship/tone-analysis", params={"compute": "true"})
        assert first.status_code == 200
        assert len(client.calls) == 1
        data = first.json()
        assert data["monthly_tones"][0]["score"] == pytest.approx(71.5)
        assert data["monthly_tones"][0]["status"] is None

        _fail_if_llm_requested(monkeypatch)
        second = app_client.post("/api/crm/relationship/tone-analysis")
        assert second.status_code == 200
        assert second.json()["monthly_tones"][0]["score"] == pytest.approx(71.5)

    def test_llm_failure_with_prior_data_yields_stale_status_no_500(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """An LLM failure during compute=true never raises: a month with a
        prior stored value that couldn't be refreshed is served that value
        marked `status="stale"`."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))
        tone_store.upsert(PARTNER_ID, month_a, 1, {  # count mismatch forces staleness
            "user_score": 55.0, "partner_score": 55.0, "combined_score": 55.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        _patch_llm(monkeypatch, _RecordingLLMClient(raises=RuntimeError("synthetic LLM failure")))

        response = app_client.post("/api/crm/relationship/tone-analysis", params={"compute": "true"})
        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"][0]["status"] == "stale"
        assert data["monthly_tones"][0]["score"] == 55.0

    def test_llm_failure_with_no_prior_data_leaves_month_out_no_500(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """An LLM failure during compute=true on a month with no prior
        stored result never raises and never fabricates a score -- the
        month is simply absent from `monthly_tones`."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))
        _patch_llm(monkeypatch, _RecordingLLMClient(raises=RuntimeError("synthetic LLM failure")))

        response = app_client.post("/api/crm/relationship/tone-analysis", params={"compute": "true"})
        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"] == []
        assert data["trend"] == "not-analyzed"
        assert tone_store.get_month(PARTNER_ID, month_a) is None


class TestPartnerDefault:
    def test_omitted_person_id_uses_configured_partner(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """Omitting `person_id` resolves to the configured partner
        (`PARTNER_PERSON_ID`), same as the detailed endpoint."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 60.0, "partner_score": 70.0, "combined_score": 65.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        _fail_if_llm_requested(monkeypatch)

        response = app_client.post("/api/crm/relationship/tone-analysis")
        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"][0]["score"] == 65.0


class TestUnknownPersonId:
    def test_unknown_person_id_returns_empty_not_404(
        self, app_client, tone_store, patch_partner, monkeypatch,
    ):
        """A `person_id` with no interactions and nothing stored -- whether
        never seen at all or simply inactive in the window -- returns 200
        with the same empty/`not-analyzed` shape as any other person with
        no data, never a 404: this endpoint doesn't distinguish "no such
        person" from "no data yet for this person"."""
        _fail_if_llm_requested(monkeypatch)

        response = app_client.post(
            "/api/crm/relationship/tone-analysis",
            params={"person_id": "synthetic-nonexistent-person"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["monthly_tones"] == []
        assert data["trend"] == "not-analyzed"


class TestResponseShape:
    def test_response_matches_the_documented_contract(
        self, app_client, tone_store, patch_partner, patch_interactions, monkeypatch,
    ):
        """Schema/shape oracle: every field `ToneAnalysisResponse` /
        `ToneDataPoint` promises is present, and no per-person fields
        leak in from the detailed shape."""
        month_a = _two_distinct_months()[0]
        patch_interactions(_interactions_for_months([month_a]))
        tone_store.upsert(PARTNER_ID, month_a, 2, {
            "user_score": 70.0, "partner_score": 65.0, "combined_score": 67.5,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        _fail_if_llm_requested(monkeypatch)

        response = app_client.post("/api/crm/relationship/tone-analysis")
        assert response.status_code == 200
        data = response.json()

        assert set(data.keys()) == {
            "monthly_tones", "trend", "average", "analyzed_through", "generated_at",
        }
        assert len(data["monthly_tones"]) == 1
        assert set(data["monthly_tones"][0].keys()) == {"month", "score", "status"}
