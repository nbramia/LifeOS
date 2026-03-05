"""
Tests for web search service.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

pytestmark = pytest.mark.unit


class TestWebSearch:
    """Tests for web search service functions."""

    def test_format_web_results_empty(self):
        """Empty results should return informative message."""
        from api.services.web_search import format_web_results_for_context
        result = format_web_results_for_context([])
        assert "No web search results found" in result

    def test_format_web_results_single(self):
        """Single result should format correctly."""
        from api.services.web_search import format_web_results_for_context
        results = [
            {"title": "Test Title", "url": "https://example.com", "snippet": "Test snippet"}
        ]
        result = format_web_results_for_context(results)
        assert "Test Title" in result
        assert "https://example.com" in result
        assert "Test snippet" in result

    def test_format_web_results_multiple(self):
        """Multiple results should be numbered."""
        from api.services.web_search import format_web_results_for_context
        results = [
            {"title": "First", "url": "https://first.com", "snippet": "First result"},
            {"title": "Second", "url": "https://second.com", "snippet": "Second result"},
        ]
        result = format_web_results_for_context(results)
        assert "1. **First**" in result
        assert "2. **Second**" in result

    def test_format_web_results_missing_fields(self):
        """Should handle missing optional fields."""
        from api.services.web_search import format_web_results_for_context
        results = [
            {"title": "Title Only", "url": "", "snippet": ""}
        ]
        result = format_web_results_for_context(results)
        assert "Title Only" in result


class TestSearchWeb:
    """Tests for the search_web function."""

    @pytest.mark.asyncio
    async def test_search_web_returns_list(self):
        """search_web should return a list."""
        mock_response = MagicMock()
        mock_response.text = "Here are the results..."

        mock_client = MagicMock()
        mock_client.create.return_value = mock_response

        with patch('api.services.web_search.get_anthropic_llm', return_value=mock_client):
            from api.services.web_search import search_web
            results = await search_web("test query")
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_web_handles_error(self):
        """search_web should handle errors gracefully."""
        mock_client = MagicMock()
        mock_client.create.side_effect = Exception("API Error")

        with patch('api.services.web_search.get_anthropic_llm', return_value=mock_client):
            from api.services.web_search import search_web
            results = await search_web("test query")
            assert results == []


class TestSearchWebWithSynthesis:
    """Tests for the search_web_with_synthesis function."""

    @pytest.mark.asyncio
    async def test_returns_tuple(self):
        """Should return tuple of (synthesized, results)."""
        mock_response = MagicMock()
        mock_response.text = "The answer is 42."

        mock_client = MagicMock()
        mock_client.create.return_value = mock_response

        with patch('api.services.web_search.get_anthropic_llm', return_value=mock_client):
            from api.services.web_search import search_web_with_synthesis
            synthesized, results = await search_web_with_synthesis("test query")
            assert isinstance(synthesized, str)
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_handles_error_gracefully(self):
        """Should return error message on failure."""
        mock_client = MagicMock()
        mock_client.create.side_effect = Exception("API Error")

        with patch('api.services.web_search.get_anthropic_llm', return_value=mock_client):
            from api.services.web_search import search_web_with_synthesis
            synthesized, results = await search_web_with_synthesis("test query")
            assert "couldn't" in synthesized.lower() or "error" in synthesized.lower()
            assert results == []
