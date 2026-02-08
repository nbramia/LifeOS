"""
Tests for Local LLM Query Router (P3.5).

Tests the OllamaClient and QueryRouter services.
"""
import pytest

# Most tests in this file are fast unit tests (mocked Ollama)
pytestmark = pytest.mark.unit
import json
from unittest.mock import patch, MagicMock, AsyncMock
import httpx


class TestOllamaClient:
    """Test the Ollama client for local LLM inference."""

    def test_client_initialization(self):
        """Client should initialize with correct settings."""
        from api.services.ollama_client import OllamaClient

        client = OllamaClient()
        assert client.host == "http://localhost:11434"
        assert client.model == "qwen2.5:7b-instruct"
        assert client.timeout == 45

    def test_client_custom_settings(self):
        """Client should accept custom settings."""
        from api.services.ollama_client import OllamaClient

        client = OllamaClient(
            host="http://custom:8080",
            model="custom-model",
            timeout=30
        )
        assert client.host == "http://custom:8080"
        assert client.model == "custom-model"
        assert client.timeout == 30

    @pytest.mark.asyncio
    async def test_generate_success(self):
        """Generate should return response from Ollama."""
        from api.services.ollama_client import OllamaClient

        mock_response = {
            "response": '{"sources": ["vault"], "reasoning": "test"}',
            "done": True
        }

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post.return_value = mock_response_obj

            client = OllamaClient()
            result = await client.generate("test prompt")

            assert result == '{"sources": ["vault"], "reasoning": "test"}'

    @pytest.mark.asyncio
    async def test_generate_timeout(self):
        """Generate should raise timeout error gracefully."""
        from api.services.ollama_client import OllamaClient, OllamaError

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = httpx.TimeoutException("timeout")

            client = OllamaClient()
            with pytest.raises(OllamaError) as exc_info:
                await client.generate("test prompt")

            assert "timeout" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_connection_error(self):
        """Generate should raise connection error gracefully."""
        from api.services.ollama_client import OllamaClient, OllamaError

        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = httpx.ConnectError("connection failed")

            client = OllamaClient()
            with pytest.raises(OllamaError) as exc_info:
                await client.generate("test prompt")

            assert "connection" in str(exc_info.value).lower()

    def test_is_available_true(self):
        """is_available should return True when Ollama is running with models."""
        from api.services.ollama_client import OllamaClient

        with patch('httpx.get') as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "models": [{"name": "llama3.2:3b"}]
            }
            mock_get.return_value = mock_response

            client = OllamaClient()
            assert client.is_available() is True

    def test_is_available_false(self):
        """is_available should return False when Ollama is not running."""
        from api.services.ollama_client import OllamaClient

        with patch('httpx.get') as mock_get:
            mock_get.side_effect = httpx.ConnectError("connection failed")

            client = OllamaClient()
            assert client.is_available() is False


class TestQueryRouter:
    """Test the query router service."""

    def test_router_initialization(self):
        """Router should initialize with Ollama client."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()
        assert router.ollama_client is not None

    @pytest.mark.asyncio
    async def test_route_parses_valid_json(self):
        """Router should parse valid JSON response from LLM."""
        from api.services.query_router import QueryRouter

        with patch('api.services.ollama_client.OllamaClient') as MockClient:
            mock_client = AsyncMock()
            mock_client.generate.return_value = '{"sources": ["calendar", "vault"], "reasoning": "schedule query"}'
            mock_client.is_available.return_value = True
            MockClient.return_value = mock_client

            router = QueryRouter()
            router.ollama_client = mock_client

            result = await router.route("What meetings do I have tomorrow?")

            assert "calendar" in result.sources
            assert result.reasoning == "schedule query"

    @pytest.mark.asyncio
    async def test_route_handles_invalid_json(self):
        """Router should fall back to vault on invalid JSON."""
        from api.services.query_router import QueryRouter

        with patch('api.services.ollama_client.OllamaClient') as MockClient:
            mock_client = AsyncMock()
            mock_client.generate.return_value = 'invalid json response'
            mock_client.is_available.return_value = True
            MockClient.return_value = mock_client

            router = QueryRouter()
            router.ollama_client = mock_client

            result = await router.route("test query")

            assert "vault" in result.sources
            assert "fallback" in result.reasoning.lower()

    @pytest.mark.asyncio
    async def test_route_fallback_when_ollama_unavailable(self):
        """Router should use keyword fallback when Ollama is unavailable."""
        from api.services.query_router import QueryRouter

        with patch('api.services.ollama_client.OllamaClient') as MockClient:
            mock_client = MagicMock()
            mock_client.is_available.return_value = False  # Sync method returns False
            MockClient.return_value = mock_client

            router = QueryRouter()
            router.ollama_client = mock_client

            result = await router.route("What meetings do I have?")

            assert "calendar" in result.sources
            assert "keyword" in result.reasoning.lower()

    @pytest.mark.asyncio
    async def test_route_includes_latency(self):
        """Router result should include latency measurement."""
        from api.services.query_router import QueryRouter

        with patch('api.services.ollama_client.OllamaClient') as MockClient:
            mock_client = AsyncMock()
            mock_client.generate.return_value = '{"sources": ["vault"], "reasoning": "test"}'
            mock_client.is_available.return_value = True
            MockClient.return_value = mock_client

            router = QueryRouter()
            router.ollama_client = mock_client

            result = await router.route("test query")

            assert result.latency_ms >= 0


class TestKeywordFallback:
    """Test the keyword-based fallback routing."""

    def test_calendar_keywords(self):
        """Calendar keywords should route to calendar."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()

        test_queries = [
            "What meetings do I have tomorrow?",
            "What's on my calendar?",
            "When is my next meeting?",
            "Show my schedule for today",
        ]

        for query in test_queries:
            result = router._keyword_fallback(query)
            assert "calendar" in result.sources, f"Failed for query: {query}"

    def test_email_keywords(self):
        """Email keywords should route to gmail."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()

        test_queries = [
            "Did Kevin email me?",
            "Show me emails from last week",
            "What did the gmail say?",
            "Check my inbox",
        ]

        for query in test_queries:
            result = router._keyword_fallback(query)
            assert "gmail" in result.sources, f"Failed for query: {query}"

    def test_drive_keywords(self):
        """Drive keywords should route to drive."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()

        test_queries = [
            "Find the budget spreadsheet",
            "What's in that Google doc?",
            "Show me the drive files",
        ]

        for query in test_queries:
            result = router._keyword_fallback(query)
            assert "drive" in result.sources, f"Failed for query: {query}"

    def test_people_keywords(self):
        """People keywords should route to people."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()

        test_queries = [
            "Tell me about Alex",
            "Prep me for meeting with Sarah",
            "Who is Kevin?",
        ]

        for query in test_queries:
            result = router._keyword_fallback(query)
            assert "people" in result.sources, f"Failed for query: {query}"

    def test_actions_keywords(self):
        """Action keywords should route to actions."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()

        test_queries = [
            "What are my action items?",
            "Show my open todos",
            "What tasks do I have?",
        ]

        for query in test_queries:
            result = router._keyword_fallback(query)
            assert "actions" in result.sources, f"Failed for query: {query}"

    def test_default_to_vault(self):
        """Unknown queries should default to vault."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()

        test_queries = [
            "What did we decide about the rebrand?",
            "Summarize the therapy session",
            "Random question about something",
        ]

        for query in test_queries:
            result = router._keyword_fallback(query)
            assert "vault" in result.sources, f"Failed for query: {query}"


class TestRouterAccuracy:
    """Test router accuracy on the PRD test cases."""

    # Test cases from PRD
    ROUTING_TEST_CASES = [
        # Calendar queries
        ("What meetings do I have tomorrow?", ["calendar"]),
        ("When is my next 1-1 with Alex?", ["calendar", "people"]),
        ("What's on my schedule this week?", ["calendar"]),

        # Email queries
        ("Did Kevin email me about the budget?", ["gmail"]),
        ("What did Sarah say in her last email?", ["gmail", "people"]),
        ("Show me emails from last week", ["gmail"]),

        # Drive queries
        ("Find the Q4 budget spreadsheet", ["drive"]),
        ("What's in the strategy document?", ["drive", "vault"]),

        # People queries
        ("Tell me about Alex", ["people", "vault"]),
        ("Prep me for meeting with Hayley", ["people", "vault", "calendar"]),

        # Action queries
        ("What are my open action items?", ["actions"]),
        ("What did I commit to in the last meeting?", ["actions", "vault"]),

        # Vault queries (default)
        ("What did we decide about the rebrand?", ["vault"]),
        ("Summarize the therapy session themes", ["vault"]),

        # Multi-source queries
        ("What's happening with the ML budget?", ["vault", "drive", "gmail"]),
        ("Prepare me for tomorrow", ["calendar", "actions", "vault"]),
    ]

    def test_keyword_fallback_accuracy(self):
        """Keyword fallback should match at least 70% of expected sources."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()
        correct = 0
        total = len(self.ROUTING_TEST_CASES)

        for query, expected_sources in self.ROUTING_TEST_CASES:
            result = router._keyword_fallback(query)
            # Check if at least one expected source is in result
            if any(src in result.sources for src in expected_sources):
                correct += 1

        accuracy = correct / total
        assert accuracy >= 0.7, f"Keyword accuracy only {accuracy*100:.0f}% ({correct}/{total})"


@pytest.mark.slow
@pytest.mark.requires_ollama
class TestRouterIntegration:
    """Integration tests with real Ollama (skip if not available)."""

    @pytest.fixture
    def ollama_available(self):
        """Check if Ollama is available."""
        from api.services.ollama_client import OllamaClient
        client = OllamaClient()
        return client.is_available()

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_real_ollama_routing(self, ollama_available):
        """Test routing with real Ollama if available."""
        if not ollama_available:
            pytest.skip("Ollama not available")

        from api.services.query_router import QueryRouter

        router = QueryRouter()
        result = await router.route("What meetings do I have tomorrow?")

        assert len(result.sources) > 0
        assert result.latency_ms > 0
        # Calendar should be one of the sources for this query
        assert "calendar" in result.sources

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_real_ollama_latency(self, ollama_available):
        """Routing latency should be reasonable for 7B model."""
        if not ollama_available:
            pytest.skip("Ollama not available")

        from api.services.query_router import QueryRouter

        router = QueryRouter()
        result = await router.route("What's on my calendar?")

        # 7B model is slower than 3B, allow up to 15s for first call (model loading)
        assert result.latency_ms < 15000, f"Latency too high: {result.latency_ms}ms"


class TestPeopleRouting:
    """Tests for people query routing."""

    @pytest.fixture
    def router(self):
        from api.services.query_router import QueryRouter
        return QueryRouter()

    def test_extract_person_name_prep_for_meeting(self, router):
        """Test extracting name from 'prep me for meeting with X'."""
        name = router._extract_person_name("prep me for meeting with Kevin")
        assert name == "Kevin"

    def test_extract_person_name_full_name(self, router):
        """Test extracting full name."""
        name = router._extract_person_name("tell me about Kevin Chen")
        assert name == "Kevin Chen"

    def test_extract_person_name_who_is(self, router):
        """Test extracting name from 'who is X'."""
        name = router._extract_person_name("who is Sarah Miller")
        assert name == "Sarah Miller"

    def test_extract_person_name_no_match(self, router):
        """Test that non-people queries return None."""
        name = router._extract_person_name("what meetings do I have tomorrow")
        assert name is None

    @pytest.mark.asyncio
    async def test_people_keywords_route_to_people_source(self, router):
        """Test that people keywords route to people source."""
        # Mock Ollama as unavailable to use keyword fallback
        with patch.object(router.ollama_client, 'is_available', return_value=False):
            result = await router.route("prep me for meeting with Kevin")
        assert "people" in result.sources
        assert "calendar" in result.sources


class TestWebSearchRouting:
    """Tests for web search and general knowledge routing."""

    @pytest.fixture
    def router(self):
        from api.services.query_router import QueryRouter
        return QueryRouter()

    def test_routes_to_web_for_current_info(self, router):
        """Current/local info should include web source."""
        result = router._keyword_fallback("What's the weather in NYC?")
        assert "web" in result.sources

    def test_routes_to_web_for_prices(self, router):
        """Price queries should include web source."""
        result = router._keyword_fallback("What's the current price of Bitcoin?")
        assert "web" in result.sources

    def test_routes_to_web_for_local_services(self, router):
        """Local service queries should include web source."""
        result = router._keyword_fallback("When is trash pickup in 22043?")
        assert "web" in result.sources

    def test_routes_empty_for_general_knowledge(self, router):
        """General knowledge should return empty sources."""
        result = router._keyword_fallback("What's the capital of France?")
        assert result.sources == []

    def test_routes_empty_for_coding_questions(self, router):
        """Coding questions Claude knows should be empty sources."""
        result = router._keyword_fallback("How do I sort a list in Python?")
        assert result.sources == []

    def test_routes_empty_for_creative(self, router):
        """Creative tasks should return empty sources."""
        result = router._keyword_fallback("Write a haiku about coffee")
        assert result.sources == []

    def test_routes_empty_for_math(self, router):
        """Math questions should return empty sources."""
        result = router._keyword_fallback("What's 15% of 200?")
        assert result.sources == []


class TestActionAfterRouting:
    """Tests for compound query action_after detection."""

    @pytest.fixture
    def router(self):
        from api.services.query_router import QueryRouter
        return QueryRouter()

    def test_detects_action_after_reminder(self, router):
        """Compound queries should set action_after for reminders."""
        result = router._keyword_fallback("When does trash get picked up? Remind me.")
        assert result.action_after == "reminder_create"

    def test_detects_action_after_reminder_with_set(self, router):
        """'Set a reminder' should trigger action_after."""
        result = router._keyword_fallback("Look up the weather and set a reminder for tomorrow")
        assert result.action_after == "reminder_create"

    def test_detects_action_after_task(self, router):
        """Task creation compound queries should set action_after."""
        result = router._keyword_fallback("Explain how to fix this. Add it to my tasks.")
        assert result.action_after == "task_create"

    def test_detects_action_after_compose(self, router):
        """Email compose compound queries should set action_after."""
        result = router._keyword_fallback("Look up the info and draft an email about it")
        assert result.action_after == "compose"

    def test_no_action_after_for_simple_queries(self, router):
        """Simple queries should not have action_after."""
        result = router._keyword_fallback("What's the weather?")
        assert result.action_after is None

    @pytest.mark.asyncio
    async def test_combines_web_and_personal_sources(self, router):
        """Can combine web with personal sources."""
        with patch.object(router.ollama_client, 'is_available', return_value=False):
            result = await router.route("What's the weather for my NYC trip tomorrow?")
        # Should have web for weather and calendar for trip
        assert "web" in result.sources or "calendar" in result.sources
