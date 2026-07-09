"""
Tests for the web search service (real search on every backend — #467).
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

pytestmark = pytest.mark.unit


class TestWebSearch:
    """Tests for the result formatter."""

    def test_format_web_results_empty(self):
        from api.services.web_search import format_web_results_for_context
        result = format_web_results_for_context([])
        assert "No web search results found" in result

    def test_format_web_results_single(self):
        from api.services.web_search import format_web_results_for_context
        results = [
            {"title": "Test Title", "url": "https://example.com", "snippet": "Test snippet"}
        ]
        result = format_web_results_for_context(results)
        assert "Test Title" in result
        assert "https://example.com" in result
        assert "Test snippet" in result

    def test_format_web_results_multiple(self):
        from api.services.web_search import format_web_results_for_context
        results = [
            {"title": "First", "url": "https://first.com", "snippet": "First result"},
            {"title": "Second", "url": "https://second.com", "snippet": "Second result"},
        ]
        result = format_web_results_for_context(results)
        assert "1. **First**" in result
        assert "2. **Second**" in result

    def test_format_web_results_missing_fields(self):
        from api.services.web_search import format_web_results_for_context
        results = [{"title": "Title Only", "url": "", "snippet": ""}]
        result = format_web_results_for_context(results)
        assert "Title Only" in result


def _fake_ddgs(hits):
    """Build a DDGS() context manager whose .text() returns `hits`."""
    inner = MagicMock()
    inner.text.return_value = hits
    cm = MagicMock()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = False
    return cm


class TestDuckDuckGoSearch:
    """The backend-independent DuckDuckGo path."""

    def test_ddg_search_maps_fields(self):
        from api.services import web_search
        cm = _fake_ddgs([{"title": "T", "href": "https://x.com", "body": "snip"}])
        with patch("ddgs.DDGS", return_value=cm):
            out = web_search._ddg_search("q")
        assert out == [{"title": "T", "url": "https://x.com", "snippet": "snip"}]

    def test_ddg_search_non_fatal_on_error(self):
        from api.services import web_search
        with patch("ddgs.DDGS", side_effect=Exception("rate limited")):
            assert web_search._ddg_search("q") == []

    @pytest.mark.asyncio
    async def test_search_web_offloads_to_ddg(self):
        from api.services import web_search
        with patch.object(web_search, "_ddg_search", return_value=[{"title": "T", "url": "u", "snippet": "s"}]):
            out = await web_search.search_web("q")
        assert out == [{"title": "T", "url": "u", "snippet": "s"}]


class TestSearchWebWithSynthesis:
    """The hybrid entry point: native on Anthropic, DuckDuckGo elsewhere."""

    @pytest.mark.asyncio
    async def test_anthropic_backend_uses_native(self):
        from api.services import web_search
        with patch.object(web_search, "_use_native_anthropic", return_value=True), \
             patch.object(web_search, "_anthropic_native_search", new=AsyncMock(return_value="Live answer, sourced.")):
            synthesized, results = await web_search.search_web_with_synthesis("who won today")
        assert synthesized == "Live answer, sourced."
        assert isinstance(results, list) and results

    @pytest.mark.asyncio
    async def test_local_backend_uses_ddg(self):
        from api.services import web_search
        native = AsyncMock()
        with patch.object(web_search, "_use_native_anthropic", return_value=False), \
             patch.object(web_search, "_anthropic_native_search", new=native), \
             patch.object(web_search, "search_web", new=AsyncMock(return_value=[
                 {"title": "AMD price", "url": "https://finance.example/amd", "snippet": "AMD is up 5.8%"}])):
            synthesized, results = await web_search.search_web_with_synthesis("amd price")
        native.assert_not_awaited()                     # local backend never calls Claude
        assert "AMD price" in synthesized and "finance.example" in synthesized
        assert results and results[0]["title"] == "AMD price"

    @pytest.mark.asyncio
    async def test_native_failure_falls_back_to_ddg(self):
        from api.services import web_search
        with patch.object(web_search, "_use_native_anthropic", return_value=True), \
             patch.object(web_search, "_anthropic_native_search", new=AsyncMock(side_effect=Exception("anthropic down"))), \
             patch.object(web_search, "search_web", new=AsyncMock(return_value=[
                 {"title": "Fallback", "url": "https://x", "snippet": "ddg result"}])):
            synthesized, results = await web_search.search_web_with_synthesis("q")
        assert "Fallback" in synthesized                # degraded to DuckDuckGo, not an error string
        assert results and results[0]["title"] == "Fallback"

    @pytest.mark.asyncio
    async def test_empty_search_returns_empty_not_fabricated(self):
        from api.services import web_search
        with patch.object(web_search, "_use_native_anthropic", return_value=False), \
             patch.object(web_search, "search_web", new=AsyncMock(return_value=[])):
            synthesized, results = await web_search.search_web_with_synthesis("q")
        assert synthesized == ""                          # honest "nothing found", never invented
        assert results == []
