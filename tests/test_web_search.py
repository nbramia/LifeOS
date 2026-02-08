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
        # Mock the anthropic client at the import level
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(type="text", text="Here are the results...")
        ]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch.dict("sys.modules", {"anthropic": MagicMock()}):
            import sys
            sys.modules["anthropic"].Anthropic.return_value = mock_client

            from api.services.web_search import search_web
            results = await search_web("test query")
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_web_handles_error(self):
        """search_web should handle errors gracefully."""
        # When anthropic import fails or API errors, should return empty list
        with patch.dict("sys.modules", {"anthropic": MagicMock()}):
            import sys
            sys.modules["anthropic"].Anthropic.side_effect = Exception("API Error")

            # Need to reimport to get the patched version
            import importlib
            import api.services.web_search as ws
            importlib.reload(ws)

            results = await ws.search_web("test query")
            assert results == []


class TestSearchWebWithSynthesis:
    """Tests for the search_web_with_synthesis function."""

    @pytest.mark.asyncio
    async def test_returns_tuple(self):
        """Should return tuple of (synthesized, results)."""
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(type="text", text="The answer is 42.")
        ]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch.dict("sys.modules", {"anthropic": MagicMock()}):
            import sys
            sys.modules["anthropic"].Anthropic.return_value = mock_client

            from api.services.web_search import search_web_with_synthesis
            synthesized, results = await search_web_with_synthesis("test query")
            assert isinstance(synthesized, str)
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_handles_error_gracefully(self):
        """Should return error message on failure."""
        with patch.dict("sys.modules", {"anthropic": MagicMock()}):
            import sys
            sys.modules["anthropic"].Anthropic.side_effect = Exception("API Error")

            import importlib
            import api.services.web_search as ws
            importlib.reload(ws)

            synthesized, results = await ws.search_web_with_synthesis("test query")
            assert "couldn't search" in synthesized.lower()
            assert results == []
