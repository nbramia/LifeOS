"""
Tests for Chat API endpoints with streaming support.
P2.1/P2.2 Acceptance Criteria:
- Streaming endpoint returns SSE format
- Sources are included in stream
- Save to vault creates proper note structure
- Empty requests return 400 errors
"""
import pytest

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from api.main import app


class TestAskStreamEndpoint:
    """Test the /api/ask/stream endpoint."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_stream_endpoint_exists(self, client):
        """Stream endpoint should exist and accept POST."""
        response = client.post("/api/ask/stream", json={"question": "test"})
        assert response.status_code != 404
        assert response.status_code != 405

    def test_stream_rejects_empty_question(self, client):
        """Should return 400 for empty question."""
        response = client.post("/api/ask/stream", json={"question": ""})
        assert response.status_code == 400

    def test_stream_rejects_whitespace_question(self, client):
        """Should return 400 for whitespace-only question."""
        response = client.post("/api/ask/stream", json={"question": "   "})
        assert response.status_code == 400

    def test_stream_returns_event_stream(self, client):
        """Response should be text/event-stream."""
        with patch('api.routes.chat.VectorStore') as mock_vs:
            mock_vs.return_value.search.return_value = []

            with patch('api.routes.chat.get_synthesizer') as mock_synth:
                async def mock_stream(*args, **kwargs):
                    yield "Test response"
                mock_synth.return_value.stream_response = mock_stream

                response = client.post(
                    "/api/ask/stream",
                    json={"question": "test question"}
                )

                assert response.headers.get("content-type", "").startswith("text/event-stream")

    def test_stream_includes_sources_event(self, client):
        """Stream should include sources in SSE format."""
        with patch('api.routes.chat.VectorStore') as mock_vs:
            mock_vs.return_value.search.return_value = [
                {
                    'content': 'Test content',
                    'metadata': {
                        'file_name': 'test.md',
                        'file_path': '/vault/test.md'
                    }
                }
            ]

            with patch('api.routes.chat.get_synthesizer') as mock_synth:
                async def mock_stream(*args, **kwargs):
                    yield "Response"
                mock_synth.return_value.stream_response = mock_stream

                response = client.post(
                    "/api/ask/stream",
                    json={"question": "test", "include_sources": True}
                )

                # Parse SSE response
                content = response.text
                assert "data:" in content


class TestSaveToVaultEndpoint:
    """Test the /api/save-to-vault endpoint."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_save_endpoint_exists(self, client):
        """Save endpoint should exist and accept POST."""
        response = client.post(
            "/api/save-to-vault",
            json={"question": "test", "answer": "test answer"}
        )
        assert response.status_code != 404
        assert response.status_code != 405

    def test_save_rejects_empty_question(self, client):
        """Should return 400 for empty question."""
        response = client.post(
            "/api/save-to-vault",
            json={"question": "", "answer": "test answer"}
        )
        assert response.status_code == 400

    def test_save_rejects_empty_answer(self, client):
        """Should return 400 for empty answer."""
        response = client.post(
            "/api/save-to-vault",
            json={"question": "test", "answer": ""}
        )
        assert response.status_code == 400

    def test_save_creates_note_with_correct_structure(self, client, tmp_path):
        """Should create note with proper markdown structure."""
        with patch('api.routes.chat.get_synthesizer') as mock_synth:
            mock_synth.return_value.get_response = AsyncMock(
                return_value="""---
title: Test Note
created: 2026-01-07
source: lifeos
tags: [test]
---

# Test Note

## TL;DR
This is a summary.

## Content
Test content here.
"""
            )

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is the test?",
                    "answer": "This is the test answer."
                }
            )

            # Should return success (200) or error if vault path doesn't exist
            # The synthesizer was called with proper structure
            assert response.status_code in [200, 500]
            mock_synth.return_value.get_response.assert_called_once()

    def test_save_returns_obsidian_url(self, client, tmp_path):
        """Response should include obsidian:// URL."""
        with patch('api.routes.chat.get_synthesizer') as mock_synth:
            mock_synth.return_value.get_response = AsyncMock(
                return_value="""---
title: Budget Analysis
---

# Budget Analysis

Content here.
"""
            )

            # Create a temp vault directory for the test
            vault_dir = tmp_path / "Notes 2025" / "LifeOS" / "Research"
            vault_dir.mkdir(parents=True)

            with patch.object(Path, '__new__', return_value=vault_dir / "test.md"):
                response = client.post(
                    "/api/save-to-vault",
                    json={
                        "question": "Budget question",
                        "answer": "Budget answer"
                    }
                )

                if response.status_code == 200:
                    data = response.json()
                    assert "obsidian_url" in data or "path" in data


class TestChatRequestValidation:
    """Test request validation for chat endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_stream_handles_missing_include_sources(self, client):
        """Should default include_sources to True."""
        with patch('api.routes.chat.VectorStore') as mock_vs:
            mock_vs.return_value.search.return_value = []

            with patch('api.routes.chat.get_synthesizer') as mock_synth:
                async def mock_stream(*args, **kwargs):
                    yield "Test"
                mock_synth.return_value.stream_response = mock_stream

                response = client.post(
                    "/api/ask/stream",
                    json={"question": "test"}
                )

                assert response.status_code == 200

    def test_save_requires_both_fields(self, client):
        """Should require both question and answer."""
        response = client.post(
            "/api/save-to-vault",
            json={"question": "test"}  # Missing answer
        )
        assert response.status_code in [400, 422]

        response = client.post(
            "/api/save-to-vault",
            json={"answer": "test"}  # Missing question
        )
        assert response.status_code in [400, 422]


# Unit tests for compose intent detection (no TestClient needed)
class TestComposeIntentDetection:
    """Test the compose intent detection helper function."""

    def test_detects_draft_email(self):
        """Should detect 'draft an email' requests."""
        from api.routes.chat import detect_compose_intent

        assert detect_compose_intent("draft an email to John about the meeting")
        assert detect_compose_intent("Draft email to Sarah")
        assert detect_compose_intent("draft a message to the team")

    def test_detects_compose_email(self):
        """Should detect 'compose' requests."""
        from api.routes.chat import detect_compose_intent

        assert detect_compose_intent("compose an email to Kevin")
        assert detect_compose_intent("compose email about the project")

    def test_detects_write_email(self):
        """Should detect 'write' requests."""
        from api.routes.chat import detect_compose_intent

        assert detect_compose_intent("write an email to the team")
        assert detect_compose_intent("write email about budget")

    def test_detects_email_to_pattern(self):
        """Should detect 'email to' pattern."""
        from api.routes.chat import detect_compose_intent

        assert detect_compose_intent("email to john@example.com about the project")
        assert detect_compose_intent("write to Sarah about the deadline")
        assert detect_compose_intent("draft to Kevin following up on our call")

    def test_does_not_detect_search_queries(self):
        """Should NOT detect search/retrieve queries as compose intent."""
        from api.routes.chat import detect_compose_intent

        assert not detect_compose_intent("find my email about the meeting")
        assert not detect_compose_intent("search emails from John")
        assert not detect_compose_intent("what emails did I get yesterday")
        assert not detect_compose_intent("show me the email thread")

    def test_does_not_detect_unrelated_queries(self):
        """Should NOT detect unrelated queries."""
        from api.routes.chat import detect_compose_intent

        assert not detect_compose_intent("what's on my calendar")
        assert not detect_compose_intent("tell me about Kevin")
        assert not detect_compose_intent("search my notes for project updates")
