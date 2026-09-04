"""
Tests for CRM API endpoints.

Tests are organized by endpoint group:
- Person endpoints (get, update)
- Person details (timeline, connections, facts)
- Discovery and network
- Sync health and status
- Statistics
"""
import logging
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# In-process TestClient against real production data (no mocks) — needs a
# populated CRM DB, so this is integration, not unit (#682).
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    """Create test client for CRM API."""
    from api.main import app
    return TestClient(app)


@pytest.fixture
def sample_person_id(client):
    """Get a person ID for testing."""
    response = client.get("/api/crm/people?limit=1")
    if response.status_code == 200 and response.json()["people"]:
        return response.json()["people"][0]["id"]
    pytest.skip("No people in database to test")


class TestCRMConfig:
    """Tests for GET /api/crm/config."""

    def test_photos_enabled_reflects_settings_when_false(self, client):
        """#907 review round 2, finding 2: the server side of #875's
        photos_enabled field (web/crm.html's loadCRMConfig() reads it to
        decide whether to ever request an avatar) had no test -- deleting
        `photos_enabled=settings.photos_enabled` from get_crm_config()
        left every other test in this repo green, because
        CRMConfigResponse.photos_enabled defaults to False and this host
        (at authoring time) had no Photos library configured -- the
        mutation and the real answer coincided. Forces False explicitly
        (rather than asserting against whatever settings.photos_enabled
        happens to be on the machine running the suite, e.g. a Mac with a
        real Photos library configured) so this pins the False case
        unconditionally; the next test below forces the True case, so the
        field can't be silently hardcoded to one value and still pass
        both."""
        from unittest.mock import patch, PropertyMock
        from config.settings import Settings

        with patch.object(Settings, "photos_enabled", new_callable=PropertyMock, return_value=False):
            response = client.get("/api/crm/config")

        assert response.status_code == 200
        assert response.json()["photos_enabled"] is False

    def test_photos_enabled_reflects_settings_when_true(self, client):
        from unittest.mock import patch, PropertyMock
        from config.settings import Settings

        with patch.object(Settings, "photos_enabled", new_callable=PropertyMock, return_value=True):
            response = client.get("/api/crm/config")

        assert response.status_code == 200
        assert response.json()["photos_enabled"] is True


class TestPersonEndpoints:
    """Tests for /api/crm/people endpoints."""

    def test_get_people_list(self, client):
        """GET /people returns paginated list."""
        response = client.get("/api/crm/people?limit=5")
        assert response.status_code == 200
        data = response.json()
        assert "people" in data
        assert "total" in data
        assert "offset" in data
        assert "count" in data

    def test_get_people_list_has_profile_photo_field(self, client):
        """#875: every row must carry `has_profile_photo` as a plain bool, so
        the client can decide whether to request an avatar without probing
        each person first."""
        response = client.get("/api/crm/people?limit=50")
        assert response.status_code == 200
        people = response.json()["people"]
        if not people:
            pytest.skip("No people in database to test")
        for person in people:
            assert "has_profile_photo" in person
            assert isinstance(person["has_profile_photo"], bool)

    def test_person_detail_has_profile_photo_matches_list(self, client):
        """#875: the detail endpoint must agree with the list endpoint for
        the same person -- both derive `has_profile_photo` from
        `person.photo_count` via the same `_person_to_detail_response`
        helper, so a divergence here would mean one path stopped setting it."""
        response = client.get("/api/crm/people?limit=20")
        people = response.json()["people"]
        if not people:
            pytest.skip("No people in database to test")
        for person in people[:10]:
            detail = client.get(f"/api/crm/people/{person['id']}")
            assert detail.status_code == 200
            assert detail.json()["has_profile_photo"] == person["has_profile_photo"]

    def test_list_people_never_logs_the_search_query(self, client, caplog):
        """#904: the search box's contents are personal data (names, partial
        emails) -- the list handler's own log line must carry counts and
        timing only, never the raw `q` (or other filter values)."""
        distinctive_query = "zzz-synthetic-search-marker-not-a-real-name-9f3c"
        with caplog.at_level(logging.INFO, logger="api.routes.crm"):
            response = client.get(f"/api/crm/people?q={distinctive_query}&limit=5")
        assert response.status_code == 200
        assert distinctive_query not in caplog.text

    def test_get_people_with_search(self, client):
        """GET /people with search query works."""
        response = client.get("/api/crm/people?search=john&limit=5")
        assert response.status_code == 200
        data = response.json()
        assert "people" in data

    def test_get_people_with_category_filter(self, client):
        """GET /people with category filter works."""
        response = client.get("/api/crm/people?category=work&limit=5")
        assert response.status_code == 200
        data = response.json()
        assert "people" in data

    def test_get_people_with_sources_filter(self, client):
        """GET /people with sources filter works."""
        response = client.get("/api/crm/people?sources=gmail&limit=5")
        assert response.status_code == 200
        data = response.json()
        assert "people" in data

    def test_get_person_detail(self, client, sample_person_id):
        """GET /people/{id} returns person details."""
        response = client.get(f"/api/crm/people/{sample_person_id}")
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "canonical_name" in data
        assert "emails" in data
        assert "sources" in data

    def test_get_person_not_found(self, client):
        """GET /people/{id} returns 404 for invalid ID."""
        response = client.get("/api/crm/people/invalid-id-12345")
        assert response.status_code == 404

    def test_update_person_not_found(self, client):
        """#609: PATCH /people/{id} for a missing person is a 404, never a
        200 that merely omits the update."""
        response = client.patch(
            "/api/crm/people/invalid-id-12345", json={"notes": "test"}
        )
        assert response.status_code == 404

    def _large_db_people_count(self):
        db_path = Path("data/crm.db")
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0]
        finally:
            conn.close()

    def _warm_latency_ms(self, client, path, samples=5):
        client.get(path)  # warm
        elapsed_samples = []
        for _ in range(samples):
            start = time.perf_counter()
            response = client.get(path)
            elapsed_samples.append((time.perf_counter() - start) * 1000)
            assert response.status_code == 200
        return min(elapsed_samples)

    def test_get_people_list_warm_latency_large_db(self, client):
        """Warm GET /people (limit=50, the smaller of the CRM UI's two page
        sizes) stays under the large-database latency target."""
        people_count = self._large_db_people_count()
        if people_count is None:
            pytest.skip("data/crm.db not present")
        if people_count <= 5000:
            pytest.skip("large CRM database not present")

        elapsed = self._warm_latency_ms(client, "/api/crm/people?limit=50&sort=strength")

        # #869/#880 review finding 1: this bound was originally set to 150ms
        # against a category-computation bug (compute_person_category() was
        # called with `[]` instead of `None`, silently skipping its
        # source-entity fallback fetch for the ~40 of every 50 returned
        # people who don't qualify as "work" via their own email domain).
        # With that fetch restored (correct behavior) and then batched across
        # the page in one query (SourceEntityStore.get_for_people_batch(),
        # #880 follow-up) instead of one query per person, warm latency for
        # this page is back under 100ms on the real dataset.
        assert elapsed < 100

    def test_get_people_list_warm_latency_large_db_limit_300(self, client):
        """Warm GET /people at limit=300 -- the CRM UI's actual page size
        (web/crm.html's loadPeople requests limit=300)."""
        people_count = self._large_db_people_count()
        if people_count is None:
            pytest.skip("data/crm.db not present")
        if people_count <= 5000:
            pytest.skip("large CRM database not present")

        elapsed = self._warm_latency_ms(client, "/api/crm/people?limit=300&sort=strength")

        # #880 follow-up round: batching the per-page source-entity fetch
        # into one query (SourceEntityStore.get_for_people_batch()) cut warm
        # latency here from ~600-1100ms (one SourceEntityStore query per
        # person, each opening its own connection) to ~200-430ms measured on
        # the real dataset. A 150ms bound (matching the limit=50 case
        # proportionally) was requested but is NOT achievable while
        # preserving exact category semantics: a strength-sorted page of 300
        # is dominated by the highest-interaction people, many of whom have
        # 10,000-65,000+ source entities each on this real dataset, and
        # SQLite has no per-group "top-K" query optimization -- a single
        # combined query (WHERE IN (...) plus either a window function or a
        # Python-side group/sort) must fully rank/materialize every matching
        # row before applying any per-person cap, which is measurably SLOWER
        # here than 300 individually-bounded queries (confirmed via direct
        # profiling: a window-function query over the same IDs took longer
        # than this batched approach, even with a composite index added
        # experimentally). This batched approach's own per-arm/per-query
        # overhead (~0.7-0.9ms x 300 people) sets a floor around 200ms that
        # doesn't move with a smaller per-person cap. See PR #880 discussion
        # for the full measurement trail and the deeper fix that would be
        # needed to go lower (e.g. reading the already-persisted
        # PersonEntity.category field -- refreshed nightly by
        # update_all_strengths() -- instead of recomputing dynamically on
        # every list request).
        assert elapsed < 350


class TestPersonTimeline:
    """Tests for person timeline endpoints."""

    def test_get_timeline(self, client, sample_person_id):
        """GET /people/{id}/timeline returns interactions."""
        response = client.get(f"/api/crm/people/{sample_person_id}/timeline?days=30")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "count" in data

    def test_get_timeline_with_type_filter(self, client, sample_person_id):
        """GET /people/{id}/timeline with type filter works."""
        response = client.get(
            f"/api/crm/people/{sample_person_id}/timeline?days=90&type=email"
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data

    def test_get_aggregated_timeline(self, client, sample_person_id):
        """GET /people/{id}/timeline/aggregated returns grouped data."""
        response = client.get(
            f"/api/crm/people/{sample_person_id}/timeline/aggregated?days=90"
        )
        assert response.status_code == 200
        data = response.json()
        assert "days" in data or "total_interactions" in data


class TestPersonConnections:
    """Tests for person connections endpoint."""

    def test_get_connections(self, client, sample_person_id):
        """GET /people/{id}/connections returns related people."""
        response = client.get(f"/api/crm/people/{sample_person_id}/connections")
        assert response.status_code == 200
        data = response.json()
        assert "connections" in data

    def test_get_connections_with_limit(self, client, sample_person_id):
        """GET /people/{id}/connections with limit works."""
        response = client.get(
            f"/api/crm/people/{sample_person_id}/connections?limit=5"
        )
        assert response.status_code == 200
        data = response.json()
        assert "connections" in data
        assert len(data["connections"]) <= 5


class TestPersonStrength:
    """Tests for relationship strength endpoint."""

    def test_get_strength(self, client, sample_person_id):
        """GET /people/{id}/strength returns strength data."""
        response = client.get(f"/api/crm/people/{sample_person_id}/strength")
        assert response.status_code == 200
        data = response.json()
        # Should contain strength calculation details
        assert isinstance(data, dict)


class TestPersonFacts:
    """Tests for person facts endpoints."""

    def test_get_facts(self, client, sample_person_id):
        """GET /people/{id}/facts returns fact list."""
        response = client.get(f"/api/crm/people/{sample_person_id}/facts")
        assert response.status_code == 200
        data = response.json()
        assert "facts" in data

    def test_update_fact_not_found(self, client, sample_person_id):
        """#609: PUT .../facts/{id} for a missing fact is a 404, never a
        200 that merely omits the update."""
        response = client.put(
            f"/api/crm/people/{sample_person_id}/facts/invalid-fact-12345",
            json={"value": "test"},
        )
        assert response.status_code == 404

    def test_confirm_fact_not_found(self, client, sample_person_id):
        """#609: POST .../facts/{id}/confirm for a missing fact is a 404."""
        response = client.post(
            f"/api/crm/people/{sample_person_id}/facts/invalid-fact-12345/confirm"
        )
        assert response.status_code == 404

    def test_delete_fact_not_found(self, client, sample_person_id):
        """#609: DELETE .../facts/{id} for a missing fact is a 404, never a
        200 'deleted' for a fact that was never there."""
        response = client.delete(
            f"/api/crm/people/{sample_person_id}/facts/invalid-fact-12345"
        )
        assert response.status_code == 404


class TestContactSources:
    """Tests for contact sources endpoint."""

    def test_get_contact_sources(self, client, sample_person_id):
        """GET /people/{id}/contact-sources returns source details."""
        response = client.get(f"/api/crm/people/{sample_person_id}/contact-sources")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_get_source_entities(self, client, sample_person_id):
        """GET /people/{id}/source-entities returns raw sources."""
        response = client.get(f"/api/crm/people/{sample_person_id}/source-entities")
        assert response.status_code == 200
        data = response.json()
        assert "source_entities" in data


class TestNetworkGraph:
    """Tests for network graph endpoint."""

    def test_get_network_graph(self, client, sample_person_id):
        """GET /network returns graph data."""
        response = client.get(f"/api/crm/network?center_on={sample_person_id}")
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "links" in data or "edges" in data

    def test_get_network_with_depth(self, client, sample_person_id):
        """GET /network with depth parameter works."""
        response = client.get(
            f"/api/crm/network?center_on={sample_person_id}&depth=2"
        )
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data

    def test_network_requires_center_on_or_flag(self, client):
        """GET /network without center_on returns 400 unless allow_full_graph is set."""
        response = client.get("/api/crm/network")
        assert response.status_code == 400
        assert "center_on" in response.json()["detail"]

        # With allow_full_graph=true, it should work
        response = client.get("/api/crm/network?allow_full_graph=true")
        assert response.status_code == 200


class TestNetworkGraphPruning:
    """Tests for the bounded/pruned centered neighborhood (#870)."""

    def test_max_nodes_validation(self, client, sample_person_id):
        """max_nodes outside 1..500 is rejected.

        api/main.py's app-wide RequestValidationError handler converts
        FastAPI's default 422 to 400 for all endpoints.
        """
        for value in (0, 501):
            response = client.get(
                f"/api/crm/network?center_on={sample_person_id}&max_nodes={value}"
            )
            assert response.status_code == 400

    def test_max_second_degree_per_node_validation(self, client, sample_person_id):
        """max_second_degree_per_node outside 0..50 is rejected (see above:
        validation errors surface as 400, not 422, app-wide)."""
        for value in (-1, 51):
            response = client.get(
                f"/api/crm/network?center_on={sample_person_id}"
                f"&max_second_degree_per_node={value}"
            )
            assert response.status_code == 400

    def test_max_edges_validation(self, client, sample_person_id):
        """max_edges outside 1..20000 is rejected (#896 review finding 2/4)."""
        for value in (0, 20001):
            response = client.get(
                f"/api/crm/network?center_on={sample_person_id}&max_edges={value}"
            )
            assert response.status_code == 400

    def test_edges_bounded_by_max_edges(self, client, sample_person_id):
        """The edge count stays near max_edges — allowing for the fact that
        every centre-touching edge is always included even if there are
        more first-degree nodes than max_edges (#896 review finding 2/4)."""
        response = client.get(
            f"/api/crm/network?center_on={sample_person_id}&depth=2&max_edges=50"
        )
        assert response.status_code == 200
        data = response.json()
        first_degree_count = sum(1 for n in data["nodes"] if n["degree"] == 1)
        assert len(data["edges"]) <= max(50, first_degree_count)

    def test_response_never_exceeds_max_nodes(self, client, sample_person_id):
        """The returned node count never exceeds max_nodes."""
        response = client.get(
            f"/api/crm/network?center_on={sample_person_id}&depth=2"
            f"&max_nodes=20&max_second_degree_per_node=5"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["nodes"]) <= 20

    def test_first_degree_nodes_are_the_true_top_n_by_rendered_weight(self, client, sample_person_id):
        """When a center has more first-degree connections than fit, the
        kept set equals the TRUE top-N by rendered edge weight — computed
        independently here from every one of the center's relationship
        rows, not by calling the endpoint's own ranking helper (#896 review
        finding 5: the old version of this test compared against
        RelationshipStore.get_top_neighbors(), the very function the
        endpoint used to rank candidates, so it could not fail on a wrong
        ranking). Skips cleanly without the real dataset."""
        from api.services.relationship import get_relationship_store
        from api.services.person_entity import get_person_entity_store
        from api.routes.crm import _rendered_edge_weight
        from config.settings import settings

        rel_store = get_relationship_store()
        person_store = get_person_entity_store()

        raw_rels = rel_store.get_all_for_person(sample_person_id)
        if len(raw_rels) < 5:
            pytest.skip("Person has fewer than 5 relationships; can't test top-N ordering meaningfully")

        all_people_dict = {p.id: p for p in person_store.get_all()}
        my_person_id = person_store.get_canonical_id(settings.my_person_id)

        # Independently compute the true top-N (canonicalised, de-duped,
        # ranked by the exact rendered-weight formula) the same way a
        # correct implementation must.
        candidates: dict[str, float] = {}
        for rel in raw_rels:
            other_raw = rel.other_person(sample_person_id)
            if not other_raw:
                continue
            other_canonical = person_store.get_canonical_id(other_raw)
            if other_canonical == sample_person_id:
                continue
            weight = _rendered_edge_weight(
                rel, sample_person_id, other_canonical, my_person_id, all_people_dict
            )
            if other_canonical not in candidates or weight > candidates[other_canonical]:
                candidates[other_canonical] = weight

        n = 5
        expected_top_n = {
            cid for cid, _ in sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
        }

        response = client.get(
            f"/api/crm/network?center_on={sample_person_id}&depth=1&max_nodes={n + 1}"
        )
        assert response.status_code == 200
        data = response.json()
        returned_first_degree = {n_["id"] for n_ in data["nodes"] if n_["degree"] == 1}
        assert returned_first_degree == expected_top_n

    def test_edge_closure_invariants(self, client, sample_person_id):
        """Center present; every edge's endpoints are both in the node set;
        every first-degree node has an edge to the center."""
        response = client.get(
            f"/api/crm/network?center_on={sample_person_id}&depth=2"
            f"&max_nodes=50&max_second_degree_per_node=5"
        )
        assert response.status_code == 200
        data = response.json()

        node_ids = {n["id"] for n in data["nodes"]}
        assert sample_person_id in node_ids

        edge_pairs = set()
        for edge in data["edges"]:
            assert edge["source"] in node_ids
            assert edge["target"] in node_ids
            edge_pairs.add(frozenset((edge["source"], edge["target"])))

        first_degree_ids = {n["id"] for n in data["nodes"] if n["degree"] == 1}
        for fid in first_degree_ids:
            assert frozenset((sample_person_id, fid)) in edge_pairs

    def test_centered_request_never_loads_all_relationships(
        self, client, sample_person_id, monkeypatch
    ):
        """A centered request must use the indexed neighbor/edge queries,
        never RelationshipStore.get_all_relationships() (the 5+ second,
        546k-row scan this issue exists to avoid)."""
        from api.services.relationship import RelationshipStore

        def _must_not_be_called(self, limit=None):
            raise AssertionError(
                "get_all_relationships() must not be called for a centered request"
            )

        monkeypatch.setattr(RelationshipStore, "get_all_relationships", _must_not_be_called)

        response = client.get(f"/api/crm/network?center_on={sample_person_id}&depth=2")
        assert response.status_code == 200

    def test_allow_full_graph_still_calls_get_all_relationships(self, client, monkeypatch):
        """The opt-in full-graph path is untouched: it still calls
        get_all_relationships(). Stubbed to return [] instead of calling the
        original so this runs in milliseconds rather than materializing the
        full ~546k-edge graph in memory (#896 review finding 8)."""
        from api.services.relationship import RelationshipStore

        calls = []

        def _stub(self, limit=None):
            calls.append(1)
            return []

        monkeypatch.setattr(RelationshipStore, "get_all_relationships", _stub)

        response = client.get("/api/crm/network?allow_full_graph=true")
        assert response.status_code == 200
        assert calls, "allow_full_graph=true should still call get_all_relationships()"


class TestNetworkGraphLatency:
    """Latency regression test for #870 - skips cleanly on a small/fresh dataset."""

    def test_centered_depth2_under_500ms(self, client, sample_person_id):
        """A centered depth=2 request completes in well under 500ms on the
        production-sized dataset (the pre-#870 endpoint took 10+ seconds)."""
        import time
        from api.services.relationship import get_relationship_store

        rel_store = get_relationship_store()
        if rel_store.count() < 10000:
            pytest.skip(
                "Relationship table is too small to be a meaningful latency "
                "check (not the production dataset)"
            )

        start = time.monotonic()
        response = client.get(f"/api/crm/network?center_on={sample_person_id}&depth=2")
        elapsed_ms = (time.monotonic() - start) * 1000

        assert response.status_code == 200
        assert elapsed_ms < 500, f"network graph took {elapsed_ms:.1f}ms (limit 500ms)"


class TestRelationshipDetail:
    """Tests for relationship detail endpoint."""

    def test_get_relationship_detail(self, client, sample_person_id):
        """GET /relationship/{a}/{b} returns relationship data."""
        # Get a connection to have a valid second person
        conn_response = client.get(
            f"/api/crm/people/{sample_person_id}/connections?limit=1"
        )
        if conn_response.status_code == 200:
            connections = conn_response.json().get("connections", [])
            if connections:
                other_id = connections[0].get("person", {}).get("id") or connections[0].get("id")
                if other_id:
                    response = client.get(
                        f"/api/crm/relationship/{sample_person_id}/{other_id}"
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert "relationship" in data or "edge_weight" in data or isinstance(data, dict)
                    return

        pytest.skip("No connections found to test relationship detail")


class TestDiscovery:
    """Tests for discovery endpoint."""

    def test_get_discover(self, client):
        """GET /discover returns suggested connections."""
        response = client.get("/api/crm/discover?limit=5")
        assert response.status_code == 200
        data = response.json()
        assert "suggestions" in data or "people" in data


class TestStatistics:
    """Tests for statistics endpoint."""

    def test_get_statistics(self, client):
        """GET /statistics returns CRM stats."""
        response = client.get("/api/crm/statistics")
        assert response.status_code == 200
        data = response.json()
        assert "total_people" in data
        assert "total_relationships" in data


class TestSyncHealth:
    """Tests for sync health endpoints."""

    def test_get_sync_health_list(self, client):
        """GET /sync/health returns list of sources."""
        response = client.get("/api/crm/sync/health")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_sync_health_summary(self, client):
        """GET /sync/health/summary returns overall status."""
        response = client.get("/api/crm/sync/health/summary")
        assert response.status_code == 200
        data = response.json()
        assert "healthy" in data or "all_healthy" in data

    def test_get_sync_health_for_source(self, client):
        """GET /sync/health/{source} returns source-specific health."""
        response = client.get("/api/crm/sync/health/gmail")
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            data = response.json()
            assert "source" in data or "source_type" in data

    def test_get_sync_errors(self, client):
        """GET /sync/errors returns error list."""
        response = client.get("/api/crm/sync/errors")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_sync_stale(self, client):
        """GET /sync/stale returns stale sources."""
        response = client.get("/api/crm/sync/stale")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestDataHealth:
    """Tests for data health endpoints."""

    def test_get_data_health(self, client):
        """GET /data-health returns data quality info."""
        response = client.get("/api/crm/data-health")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_get_data_health_summary(self, client):
        """GET /data-health/summary returns summary stats."""
        response = client.get("/api/crm/data-health/summary")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)


class TestSlackIntegration:
    """Tests for Slack integration endpoints."""

    def test_get_slack_status(self, client):
        """GET /slack/status returns Slack connection status."""
        response = client.get("/api/crm/slack/status")
        assert response.status_code == 200
        data = response.json()
        assert "connected" in data


class TestContactsIntegration:
    """Tests for Contacts integration endpoints."""

    def test_get_contacts_status(self, client):
        """GET /contacts/status returns contacts sync status."""
        response = client.get("/api/crm/contacts/status")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)


class TestLinkOverrides:
    """Tests for link override endpoints."""

    def test_get_link_overrides(self, client):
        """GET /link-overrides returns override list."""
        response = client.get("/api/crm/link-overrides")
        assert response.status_code == 200
        data = response.json()
        assert "overrides" in data or isinstance(data, list)


class TestMeInteractionsSpan:
    """Tests for GET /api/crm/me/interactions/span (#871)."""

    def test_returns_expected_shape(self, client):
        """Contract: earliest/latest (ISO or null) + an integer years,
        clamped to [1, 10], regardless of whether the DB has data."""
        response = client.get("/api/crm/me/interactions/span")
        assert response.status_code == 200
        data = response.json()
        assert "earliest" in data and "latest" in data and "years" in data
        assert isinstance(data["years"], int)
        assert 1 <= data["years"] <= 10
        if data["earliest"] is None:
            assert data["latest"] is None
        else:
            assert data["latest"] is not None


class TestMeFamilyLatency:
    """
    #871 latency acceptance criteria, measured against the real production
    dataset. Skip cleanly (rather than fail) when data/crm.db or
    data/interactions.db is absent or small, so the suite still passes on a
    fresh clone with no data/ directory.
    """

    def _interaction_count(self):
        db_path = Path("data/interactions.db")
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        finally:
            conn.close()

    def _warm_latency_ms(self, client, path, samples=3):
        client.get(path)  # warm
        elapsed_samples = []
        for _ in range(samples):
            start = time.perf_counter()
            response = client.get(path)
            elapsed_samples.append((time.perf_counter() - start) * 1000)
            assert response.status_code == 200
        return min(elapsed_samples)

    def _require_large_dataset(self):
        count = self._interaction_count()
        if count is None:
            pytest.skip("data/interactions.db not present")
        if count <= 10000:
            pytest.skip("large interactions database not present")
        return count

    def _family_ids(self, client, n=12):
        response = client.get("/api/crm/family/members")
        if response.status_code != 200:
            pytest.skip("family/members endpoint unavailable")
        members = response.json().get("members", [])
        if not members:
            pytest.skip("no family members in database to test with")
        return [m["id"] for m in members[:n]]

    def test_me_interactions_3657_days_under_1200ms(self, client):
        """
        #871's requested bound was 800ms (down from the pre-#871 measured
        6.4s). Reached via #897 review finding 3's proposed path: the
        neglected-contacts widget now fetches only (person_id,
        julianday(timestamp)) — a covering index query, no source_type — and
        the health-score widget fetches only julianday(timestamp), restricted
        to source type, then buckets both with `bisect` in Python instead of
        hydrating full Interaction objects for the "circles 0-3"/"top 25 by
        relationship strength" population (92 people who account for ~220k
        of the ~455k total interactions on the real dataset — close family
        and friends are, unsurprisingly, the highest-volume contacts).

        Warm latency measured on the real dataset (direct handler calls, 7
        samples): min 777ms / median 791ms / max 924ms, even on this shared
        dev host under heavy concurrent load from sibling agents (load
        average 11-21 during measurement) — down from 3.9-6.4s before this
        PR's SQL rewrite. The 1200ms bound below (and this test's name,
        renamed per #897's verification-pass review) carries headroom above
        that observed max for host contention rather than asserting the
        literal 800ms target, which this test does not enforce; tighten it
        if re-measured consistently lower on a quiet host. Best-of-5 samples
        (like TestPeopleLatency's warm-latency tests), rather than this
        class's usual best-of-3, to further reduce flakiness from transient
        host-load spikes.
        """
        self._require_large_dataset()
        elapsed = self._warm_latency_ms(
            client, "/api/crm/me/interactions?days_back=3657", samples=5
        )
        assert elapsed < 1200

    def test_me_timeline_under_300ms(self, client):
        self._require_large_dataset()
        elapsed = self._warm_latency_ms(client, "/api/crm/me/timeline")
        assert elapsed < 300

    def test_family_timeline_under_300ms(self, client):
        self._require_large_dataset()
        ids = self._family_ids(client)
        elapsed = self._warm_latency_ms(
            client, f"/api/crm/family/timeline?person_ids={','.join(ids)}"
        )
        assert elapsed < 300

    def test_me_stats_under_50ms(self, client):
        self._require_large_dataset()
        elapsed = self._warm_latency_ms(client, "/api/crm/me/stats")
        assert elapsed < 50

    def test_me_interactions_span_under_50ms(self, client):
        self._require_large_dataset()
        elapsed = self._warm_latency_ms(client, "/api/crm/me/interactions/span")
        assert elapsed < 50
