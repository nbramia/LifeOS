"""
Tests for CRM API endpoints.

Tests are organized by endpoint group:
- Person endpoints (get, update)
- Person details (timeline, connections, facts)
- Discovery and network
- Sync health and status
- Statistics
"""
import pytest
from fastapi.testclient import TestClient


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
