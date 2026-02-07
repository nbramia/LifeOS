"""
Tests for CRM UI requirements (Phase 8.1).

Verifies:
1. People list sorting works correctly
2. Zero-interaction filter works
3. Stats match interaction database
4. Review queue is accessible
"""
import pytest
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import PersonEntity, get_person_entity_store
from api.services.interaction_store import get_interaction_db_path


class TestPeopleListSorting:
    """Tests for people list sorting functionality."""

    def test_api_supports_sort_by_interactions(self):
        """Test that the API accepts sort=interactions parameter."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/people?sort=interactions&limit=5")

        assert response.status_code == 200
        data = response.json()
        assert "people" in data

    def test_sort_by_interactions_orders_correctly(self):
        """Test that sorting by interactions returns highest first."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/people?sort=interactions&limit=10")

        assert response.status_code == 200
        data = response.json()
        people = data["people"]

        # Check that interaction counts are in descending order
        interaction_counts = []
        for p in people:
            total = (
                p.get("email_count", 0) +
                p.get("meeting_count", 0) +
                p.get("mention_count", 0) +
                p.get("message_count", 0)
            )
            interaction_counts.append(total)

        # Should be sorted descending
        assert interaction_counts == sorted(interaction_counts, reverse=True)

    def test_sort_by_name_alphabetical(self):
        """Test that sorting by name returns A-Z order."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/people?sort=name&limit=20")

        assert response.status_code == 200
        data = response.json()
        people = data["people"]

        names = [p["canonical_name"].lower() for p in people]
        assert names == sorted(names)

    def test_sort_by_strength_descending(self):
        """Test that sorting by strength returns highest first."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/people?sort=strength&limit=10")

        assert response.status_code == 200
        data = response.json()
        people = data["people"]

        strengths = [p.get("relationship_strength", 0) for p in people]
        assert strengths == sorted(strengths, reverse=True)


class TestZeroInteractionFilter:
    """Tests for filtering out zero-interaction people."""

    def test_api_supports_has_interactions_param(self):
        """Test that the API accepts has_interactions parameter."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/people?has_interactions=true&limit=5")

        assert response.status_code == 200

    def test_has_interactions_true_filters_zero(self):
        """Test that has_interactions=true excludes zero-interaction people."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/people?has_interactions=true&limit=100")

        assert response.status_code == 200
        data = response.json()

        # All returned people should have at least one interaction
        for p in data["people"]:
            total = (
                p.get("email_count", 0) +
                p.get("meeting_count", 0) +
                p.get("mention_count", 0) +
                p.get("message_count", 0)
            )
            assert total > 0, f"{p['canonical_name']} has zero interactions"

    def test_has_interactions_false_shows_all(self):
        """Test that has_interactions=false or absent shows all people."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        # Get count with filter
        response1 = client.get("/api/crm/people?has_interactions=true&limit=1")
        filtered_total = response1.json()["total"]

        # Get count without filter
        response2 = client.get("/api/crm/people?limit=1")
        unfiltered_total = response2.json()["total"]

        # Unfiltered should have >= filtered count
        assert unfiltered_total >= filtered_total


@pytest.mark.usefixtures("require_db")
class TestStatsMatchDatabase:
    """Tests that PersonEntity stats match the interaction database.

    NOTE: These tests require direct database access and will be skipped if
    the server is running (database locked). Stop the server to run these tests.
    """

    def test_top_person_stats_accurate(self):
        """Test that the person with most interactions has accurate stats."""
        # Get top person by interactions from database
        db_path = get_interaction_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("""
            SELECT person_id, COUNT(*) as cnt
            FROM interactions
            GROUP BY person_id
            ORDER BY cnt DESC
            LIMIT 1
        """)
        top_person_id, db_count = cursor.fetchone()
        conn.close()

        # Get PersonEntity stats
        store = get_person_entity_store()
        person = store.get_by_id(top_person_id)

        if person:
            entity_total = (
                person.email_count +
                person.meeting_count +
                person.mention_count +
                person.message_count
            )

            # Should match within reasonable margin (some interactions may not count)
            # Allow 10% variance
            assert abs(entity_total - db_count) / db_count < 0.1, \
                f"Stats mismatch: entity={entity_total}, db={db_count}"

    def test_stats_by_source_type(self):
        """Test that source-specific stats are accurate for a sample person."""
        # Get a person with diverse interactions
        db_path = get_interaction_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("""
            SELECT person_id,
                   SUM(CASE WHEN source_type = 'gmail' THEN 1 ELSE 0 END) as gmail,
                   SUM(CASE WHEN source_type = 'calendar' THEN 1 ELSE 0 END) as calendar,
                   SUM(CASE WHEN source_type IN ('vault', 'granola') THEN 1 ELSE 0 END) as mentions
            FROM interactions
            GROUP BY person_id
            HAVING gmail > 10 AND calendar > 5
            LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()

        if row:
            person_id, gmail_count, calendar_count, mention_count = row

            store = get_person_entity_store()
            person = store.get_by_id(person_id)

            if person:
                assert person.email_count == gmail_count
                assert person.meeting_count == calendar_count


class TestReviewQueue:
    """Tests for review queue functionality."""

    def test_review_queue_endpoint_exists(self):
        """Test that the review queue endpoint exists."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/review-queue")

        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "count" in data
        assert "total_pending" in data

    def test_review_queue_filters_by_confidence(self):
        """Test that confidence filters work."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        response = client.get("/api/crm/review-queue?max_confidence=0.7")

        assert response.status_code == 200
        data = response.json()

        for item in data["items"]:
            assert item["confidence"] <= 0.7

    def test_review_queue_confirm_endpoint(self):
        """Test that confirm endpoint exists."""
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)

        # First get a review item
        response = client.get("/api/crm/review-queue?limit=1")
        if response.json()["items"]:
            item_id = response.json()["items"][0]["id"]

            # Try to confirm it
            response = client.post(f"/api/crm/review-queue/{item_id}/confirm")
            assert response.status_code in [200, 404]  # 404 if already processed
