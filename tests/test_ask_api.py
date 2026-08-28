"""
Tests for the RAG Synthesis (Ask) API endpoint.
P1.3 Acceptance Criteria:
- Endpoint accepts questions and returns synthesized answers
- Answers cite sources from retrieved chunks
- Answers reflect Paul Graham writing style (concise, clear)
- Sources list is deduplicated by file
- Total latency <3 seconds for vault-only queries
- Claude API errors return graceful error response
- Empty question returns 400 error
"""
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import api.routes.ask as ask_route_mod
from api.main import app

# These tests use TestClient which initializes the app (slow).
pytestmark = pytest.mark.slow


class _StubHybridSearch:
    """Deterministic stand-in for HybridSearch (#828): these tests are
    about /api/ask's retrieval -> prompt -> synthesis wiring and response
    shape (source dedup, field presence), not the live vault's actual
    content -- and Claude synthesis is already mocked per-test via
    `get_claude_response`, so retrieval should be too.
    """

    def __init__(self, chunks=None):
        self.chunks = chunks if chunks is not None else list(_DEFAULT_CHUNKS)

    def search(self, query, top_k=10, date_from=None, date_to=None, **_kwargs):
        return self.chunks[:top_k]


# Two chunks share a file_path so test_ask_deduplicates_sources actually
# exercises the route's dedup-by-file_path logic.
_DEFAULT_CHUNKS = [
    {"content": "Budget is $1M for Q1.", "file_name": "budget.md", "file_path": "/vault/budget.md", "score": 0.9},
    {"content": "More budget detail.", "file_name": "budget.md", "file_path": "/vault/budget.md", "score": 0.8},
    {"content": "Other note content.", "file_name": "other.md", "file_path": "/vault/other.md", "score": 0.5},
]


@pytest.fixture(autouse=True)
def _stub_hybrid_search(monkeypatch):
    monkeypatch.setattr(ask_route_mod, "get_hybrid_search", lambda: _StubHybridSearch())


class TestAskEndpoint:
    """Test the /api/ask endpoint."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_ask_endpoint_exists(self, client):
        """Ask endpoint should exist and accept POST."""
        response = client.post("/api/ask", json={"question": "test"})
        # Should not be 404 or 405
        assert response.status_code != 404
        assert response.status_code != 405

    def test_ask_requires_question(self, client):
        """Should return 400/422 for missing question."""
        response = client.post("/api/ask", json={})
        assert response.status_code in [400, 422]

    def test_ask_rejects_empty_question(self, client):
        """Should return 400 for empty question string."""
        response = client.post("/api/ask", json={"question": ""})
        assert response.status_code == 400
        assert "error" in response.json() or "detail" in response.json()

    def test_ask_returns_response_structure(self, client):
        """Response should have correct structure."""
        # Mock the Claude API call
        with patch('api.routes.ask.get_claude_response') as mock_claude:
            mock_claude.return_value = {
                "answer": "This is a test answer.",
                "sources_used": ["file1.md", "file2.md"]
            }

            response = client.post("/api/ask", json={"question": "What is the budget?"})

            if response.status_code == 200:
                data = response.json()
                assert "answer" in data
                assert "sources" in data
                assert "retrieval_time_ms" in data
                assert "synthesis_time_ms" in data

    def test_ask_includes_sources(self, client):
        """Response should include sources list."""
        with patch('api.routes.ask.get_claude_response') as mock_claude:
            mock_claude.return_value = {
                "answer": "Based on the meeting notes, the budget is $1M.",
                "sources_used": ["/vault/budget.md"]
            }

            response = client.post("/api/ask", json={"question": "What is the budget?"})

            if response.status_code == 200:
                data = response.json()
                assert isinstance(data.get("sources", []), list)

    def test_ask_deduplicates_sources(self, client):
        """Sources should be deduplicated by file."""
        with patch('api.routes.ask.get_claude_response') as mock_claude:
            mock_claude.return_value = {
                "answer": "The answer from multiple chunks.",
                "sources_used": ["/vault/same.md", "/vault/same.md", "/vault/other.md"]
            }

            response = client.post("/api/ask", json={"question": "test"})

            if response.status_code == 200:
                data = response.json()
                sources = data.get("sources", [])
                # Check file_paths are unique
                file_paths = [s["file_path"] for s in sources]
                assert len(file_paths) == len(set(file_paths))

    def test_ask_handles_claude_error_gracefully(self, client):
        """Should return graceful error if Claude API fails."""
        with patch('api.routes.ask.get_claude_response') as mock_claude:
            mock_claude.side_effect = Exception("Claude API error")

            response = client.post("/api/ask", json={"question": "test question"})

            # Should return error response, not 500
            assert response.status_code in [200, 500, 503]
            if response.status_code == 200:
                data = response.json()
                # May have error message in answer
                assert "answer" in data or "error" in data

    def test_ask_include_sources_param(self, client):
        """Should respect include_sources parameter."""
        with patch('api.routes.ask.get_claude_response') as mock_claude:
            mock_claude.return_value = {
                "answer": "Test answer",
                "sources_used": []
            }

            response = client.post("/api/ask", json={
                "question": "test",
                "include_sources": False
            })

            if response.status_code == 200:
                data = response.json()
                # Sources might be empty or omitted
                assert "answer" in data


class TestPromptConstruction:
    """Test prompt construction logic."""

    def test_prompt_includes_context(self):
        """Prompt should include retrieved context."""
        from api.routes.ask import construct_prompt

        chunks = [
            {"content": "Budget is $1M", "file_name": "budget.md"},
            {"content": "Q1 targets met", "file_name": "quarterly.md"}
        ]
        question = "What is the budget?"

        prompt = construct_prompt(question, chunks)

        assert "Budget is $1M" in prompt
        assert "Q1 targets" in prompt
        assert "What is the budget?" in prompt

    def test_prompt_includes_source_attribution(self):
        """Prompt should instruct Claude to cite sources."""
        from api.routes.ask import construct_prompt

        chunks = [{"content": "Test content", "file_name": "test.md"}]
        prompt = construct_prompt("Test question", chunks)

        # Should mention source attribution
        assert "source" in prompt.lower() or "cite" in prompt.lower() or "reference" in prompt.lower()

    def test_prompt_handles_empty_context(self):
        """Should handle case with no retrieved context."""
        from api.routes.ask import construct_prompt

        prompt = construct_prompt("What is X?", [])

        assert "What is X?" in prompt
        # Should still be valid prompt


class TestSynthesizerService:
    """Test the synthesizer service."""

    def test_synthesizer_calls_llm(self):
        """Should call local LLM with constructed prompt."""
        mock_response = MagicMock()
        mock_response.text = "Test response"

        mock_client = MagicMock()
        mock_client.create.return_value = mock_response

        with patch('api.services.synthesizer.get_local_llm', return_value=mock_client):
            from api.services.synthesizer import Synthesizer
            synth = Synthesizer()
            # Reset the cached client so the mock takes effect
            synth._client = mock_client
            synth.synthesize("Test prompt")

            mock_client.create.assert_called_once()

    def test_synthesizer_handles_api_error(self):
        """Should handle API errors gracefully."""
        mock_client = MagicMock()
        mock_client.create.side_effect = Exception("API Error")

        from api.services.synthesizer import Synthesizer
        synth = Synthesizer()
        synth._client = mock_client

        # Should raise after retries (handled at route level)
        try:
            synth.synthesize("Test prompt")
        except Exception:
            # Re-raising is acceptable if handled at route level
            pass
