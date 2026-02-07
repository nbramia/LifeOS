"""
Tests for api/routes/briefings.py

Tests the briefings API endpoints for stakeholder briefing generation.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from api.main import app


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def client():
    """Create a test client for the API."""
    return TestClient(app)


@pytest.fixture
def mock_briefings_service():
    """Create a mock briefings service."""
    mock_service = MagicMock()
    mock_service.generate_briefing = AsyncMock()
    return mock_service


@pytest.fixture
def successful_briefing_response():
    """A successful briefing response from the service."""
    return {
        "status": "success",
        "briefing": "## John Smith — Briefing\n\nJohn is a key stakeholder...",
        "person_name": "John Smith",
        "metadata": {
            "email": "john@example.com",
            "company": "Acme Corp",
            "position": "CTO",
            "category": "work"
        },
        "sources": ["meeting_notes.md", "projects.md"],
        "action_items_count": 3,
        "notes_count": 5
    }


@pytest.fixture
def error_briefing_response():
    """An error briefing response from the service."""
    return {
        "status": "error",
        "message": "Person not found in system",
        "person_name": "Unknown Person"
    }


# =============================================================================
# POST /api/briefing Tests
# =============================================================================

@pytest.mark.unit
class TestPostBriefingEndpoint:
    """Tests for POST /api/briefing endpoint."""

    def test_post_briefing_success(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test successful briefing generation via POST."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": "John Smith"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["person_name"] == "John Smith"
        assert "briefing" in data
        assert data["metadata"]["company"] == "Acme Corp"
        assert data["action_items_count"] == 3
        assert data["notes_count"] == 5

    def test_post_briefing_with_email(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test briefing generation with optional email parameter."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": "John Smith", "email": "john@example.com"}
            )

        assert response.status_code == 200
        # Verify email was passed to service
        mock_briefings_service.generate_briefing.assert_called_once_with(
            "John Smith", email="john@example.com"
        )

    def test_post_briefing_empty_name(self, client):
        """Test that empty person name returns 400."""
        response = client.post(
            "/api/briefing",
            json={"person_name": ""}
        )

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_post_briefing_whitespace_name(self, client):
        """Test that whitespace-only person name returns 400."""
        response = client.post(
            "/api/briefing",
            json={"person_name": "   "}
        )

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_post_briefing_service_error(
        self, client, mock_briefings_service, error_briefing_response
    ):
        """Test that service errors return 500."""
        mock_briefings_service.generate_briefing.return_value = error_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": "Unknown Person"}
            )

        assert response.status_code == 500
        assert "not found" in response.json()["detail"].lower()

    def test_post_briefing_missing_person_name(self, client):
        """Test that missing person_name field returns error."""
        response = client.post(
            "/api/briefing",
            json={}
        )

        # FastAPI may return 400 or 422 for validation errors
        assert response.status_code in (400, 422)

    def test_post_briefing_invalid_json(self, client):
        """Test that invalid JSON returns error."""
        response = client.post(
            "/api/briefing",
            content="not json",
            headers={"Content-Type": "application/json"}
        )

        # FastAPI may return 400 or 422 for invalid JSON
        assert response.status_code in (400, 422)


# =============================================================================
# GET /api/briefing/{person_name} Tests
# =============================================================================

@pytest.mark.unit
class TestGetBriefingEndpoint:
    """Tests for GET /api/briefing/{person_name} endpoint."""

    def test_get_briefing_success(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test successful briefing generation via GET."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.get("/api/briefing/John%20Smith")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["person_name"] == "John Smith"
        assert "briefing" in data

    def test_get_briefing_with_email_param(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test briefing generation with email query parameter."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.get(
                "/api/briefing/John%20Smith",
                params={"email": "john@example.com"}
            )

        assert response.status_code == 200
        # Verify email was passed to service
        mock_briefings_service.generate_briefing.assert_called_once_with(
            "John Smith", email="john@example.com"
        )

    def test_get_briefing_url_decoded_name(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test that URL-encoded names are properly decoded."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            # Name with special characters
            response = client.get("/api/briefing/John%20O%27Brien")

        assert response.status_code == 200
        mock_briefings_service.generate_briefing.assert_called_once_with(
            "John O'Brien", email=None
        )

    def test_get_briefing_service_error(
        self, client, mock_briefings_service, error_briefing_response
    ):
        """Test that service errors return 500."""
        mock_briefings_service.generate_briefing.return_value = error_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.get("/api/briefing/Unknown%20Person")

        assert response.status_code == 500
        assert "not found" in response.json()["detail"].lower()


# =============================================================================
# Response Model Tests
# =============================================================================

@pytest.mark.unit
class TestBriefingResponse:
    """Tests for BriefingResponse model behavior."""

    def test_response_with_all_fields(
        self, client, mock_briefings_service
    ):
        """Test response includes all expected fields."""
        full_response = {
            "status": "success",
            "briefing": "Full briefing content here",
            "message": None,
            "person_name": "Jane Doe",
            "metadata": {
                "email": "jane@example.com",
                "company": "Tech Inc",
                "position": "VP Engineering",
                "category": "work",
                "linkedin_url": "https://linkedin.com/in/janedoe"
            },
            "sources": ["notes/jane.md", "meetings/q4.md"],
            "action_items_count": 2,
            "notes_count": 8
        }
        mock_briefings_service.generate_briefing.return_value = full_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": "Jane Doe"}
            )

        assert response.status_code == 200
        data = response.json()

        # Verify all fields present
        assert data["status"] == "success"
        assert data["briefing"] == "Full briefing content here"
        assert data["person_name"] == "Jane Doe"
        assert data["metadata"]["email"] == "jane@example.com"
        assert data["metadata"]["company"] == "Tech Inc"
        assert data["sources"] == ["notes/jane.md", "meetings/q4.md"]
        assert data["action_items_count"] == 2
        assert data["notes_count"] == 8

    def test_response_with_minimal_fields(
        self, client, mock_briefings_service
    ):
        """Test response works with minimal fields."""
        minimal_response = {
            "status": "success",
            "briefing": "Brief info about person",
            "person_name": "Test Person"
        }
        mock_briefings_service.generate_briefing.return_value = minimal_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": "Test Person"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["person_name"] == "Test Person"
        # Optional fields should be None
        assert data.get("metadata") is None
        assert data.get("sources") is None


# =============================================================================
# Edge Cases
# =============================================================================

@pytest.mark.unit
class TestBriefingEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_briefing_with_special_characters_in_name(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test briefing for names with special characters."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": "José García-López"}
            )

        assert response.status_code == 200
        mock_briefings_service.generate_briefing.assert_called_once_with(
            "José García-López", email=None
        )

    def test_briefing_with_unicode_name(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test briefing for names with unicode characters."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": "田中太郎"}
            )

        assert response.status_code == 200

    def test_briefing_with_very_long_name(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test briefing for very long names."""
        long_name = "A" * 500
        mock_briefings_service.generate_briefing.return_value = {
            **successful_briefing_response,
            "person_name": long_name
        }

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            response = client.post(
                "/api/briefing",
                json={"person_name": long_name}
            )

        assert response.status_code == 200

    def test_concurrent_briefing_requests(
        self, client, mock_briefings_service, successful_briefing_response
    ):
        """Test that multiple concurrent requests work correctly."""
        mock_briefings_service.generate_briefing.return_value = successful_briefing_response

        with patch("api.routes.briefings.get_briefings_service", return_value=mock_briefings_service):
            # Make multiple requests
            responses = []
            for name in ["Person A", "Person B", "Person C"]:
                response = client.post(
                    "/api/briefing",
                    json={"person_name": name}
                )
                responses.append(response)

        # All should succeed
        for response in responses:
            assert response.status_code == 200
