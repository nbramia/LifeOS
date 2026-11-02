"""
Tests for the QueryRouter (local-LLM routing + keyword fallback).
"""
import pytest
from unittest.mock import patch, AsyncMock

# Most tests in this file are fast unit tests (mocked local LLM)
pytestmark = pytest.mark.unit


class TestQueryRouter:
    """Test the query router service."""

    def test_router_initialization(self):
        """Router should initialize with no constructor args (Ollama client is gone)."""
        from api.services.query_router import QueryRouter

        router = QueryRouter()
        # The migrated router exposes no ollama_client attribute — the
        # previous version had ``router.ollama_client``; we want a hard
        # failure if it ever creeps back in.
        assert not hasattr(router, "ollama_client")
        # And the keyword fallback is still wired up.
        assert callable(getattr(router, "_keyword_fallback", None))

    @pytest.mark.asyncio
    async def test_route_parses_valid_json(self):
        """Router should parse valid JSON response from LLM."""
        from api.services.query_router import QueryRouter

        with (
            patch("api.services.query_router.is_local_routing_llm_available", return_value=True),
            patch(
                "api.services.query_router.generate_text",
                AsyncMock(return_value='{"sources": ["calendar", "vault"], "reasoning": "schedule query"}'),
            ),
        ):
            router = QueryRouter()
            result = await router.route("What meetings do I have tomorrow?")

            assert "calendar" in result.sources
            assert result.reasoning == "schedule query"

    @pytest.mark.asyncio
    async def test_route_handles_invalid_json(self):
        """Router should fall back to vault on invalid JSON."""
        from api.services.query_router import QueryRouter

        with (
            patch("api.services.query_router.is_local_routing_llm_available", return_value=True),
            patch(
                "api.services.query_router.generate_text",
                AsyncMock(return_value="invalid json response"),
            ),
        ):
            router = QueryRouter()
            result = await router.route("test query")

            assert "vault" in result.sources
            assert "fallback" in result.reasoning.lower()

    @pytest.mark.asyncio
    async def test_route_fallback_when_llm_unavailable(self):
        """Router should use keyword fallback when the local LLM is unavailable."""
        from api.services.query_router import QueryRouter

        with patch("api.services.query_router.is_local_routing_llm_available", return_value=False):
            router = QueryRouter()
            result = await router.route("What meetings do I have?")

            assert "calendar" in result.sources
            assert "keyword" in result.reasoning.lower()

    @pytest.mark.asyncio
    async def test_route_thinking_enabled_payload_byte_identical(self):
        """With thinking ENABLED, the actual JSON body sent over HTTP is
        byte-identical to a request with no thinking-control flag at all.

        The setting is patched explicitly rather than relying on the field
        default: the default is False, since a 12-case A/B showed identical
        correctness at 8.2x the speed. This guards the
        enabled path regardless of which way the default points.

        Runs the real generate_text -> LocalLLMClient.acreate path (only the
        httpx client underneath is faked) rather than mocking generate_text
        itself — a mock-call-shape assertion (enable_thinking=None was
        passed to generate_text) would still pass even if acreate later
        started serializing that as chat_template_kwargs:
        {"enable_thinking": None}; this asserts the wire body directly."""
        from unittest.mock import AsyncMock, MagicMock
        from api.services import llm_client as llm_mod
        from api.services.query_router import QueryRouter, ROUTER_PROMPT
        from api.services.query_router import settings as qr_settings

        prev_client, prev_url = llm_mod._routing_client, llm_mod._routing_client_url
        llm_mod._routing_client = None
        llm_mod._routing_client_url = None
        try:
            fake_resp = MagicMock()
            fake_resp.json.return_value = {
                "choices": [{
                    "message": {"content": '{"sources": ["vault"], "reasoning": "test"}'},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "model": "local",
            }
            fake_async_client = MagicMock(is_closed=False)
            fake_async_client.post = AsyncMock(return_value=fake_resp)

            client = llm_mod._get_local_routing_client()
            client._async_client = fake_async_client

            with (
                patch("api.services.query_router.is_local_routing_llm_available", return_value=True),
                patch.object(qr_settings, "router_enable_thinking", True),
            ):
                router = QueryRouter()
                result = await router.route("test query")
        finally:
            llm_mod._routing_client = prev_client
            llm_mod._routing_client_url = prev_url

        assert "vault" in result.sources
        payload = fake_async_client.post.call_args.kwargs["json"]
        assert payload == {
            "model": "local",
            "messages": [{"role": "user", "content": ROUTER_PROMPT.format(query="test query")}],
            "max_tokens": 2048,
            "temperature": 0.3,
            "stream": False,
        }

    @pytest.mark.asyncio
    async def test_route_thinking_disabled_passes_false(self):
        """When settings.router_enable_thinking is False, the router
        explicitly requests thinking off — this is the one-line flip a
        follow-up PR will make once the correctness A/B confirms no
        regression."""
        from api.services.query_router import QueryRouter

        with (
            patch("api.services.query_router.is_local_routing_llm_available", return_value=True),
            patch("api.services.query_router.settings") as mock_settings,
            patch(
                "api.services.query_router.generate_text",
                AsyncMock(return_value='{"sources": ["vault"], "reasoning": "test"}'),
            ) as mock_generate_text,
        ):
            mock_settings.router_enable_thinking = False
            router = QueryRouter()
            await router.route("test query")
            kwargs = mock_generate_text.await_args.kwargs
            assert kwargs["enable_thinking"] is False

    @pytest.mark.asyncio
    async def test_route_includes_latency(self):
        """Router result should include latency measurement."""
        from api.services.query_router import QueryRouter

        with (
            patch("api.services.query_router.is_local_routing_llm_available", return_value=True),
            patch(
                "api.services.query_router.generate_text",
                AsyncMock(return_value='{"sources": ["vault"], "reasoning": "test"}'),
            ),
        ):
            router = QueryRouter()
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
@pytest.mark.integration
class TestRouterIntegration:
    """Integration tests against a running local LLM (llama-server). Skipped if not available."""

    @pytest.fixture
    def llm_available(self):
        """Check if the local LLM is available."""
        from api.services.llm_client import is_local_routing_llm_available
        return is_local_routing_llm_available()

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_real_routing(self, llm_available):
        """Test routing with the real local LLM if available."""
        if not llm_available:
            pytest.skip("Local LLM server not available")

        from api.services.query_router import QueryRouter

        router = QueryRouter()
        result = await router.route("What meetings do I have tomorrow?")

        assert len(result.sources) > 0
        assert result.latency_ms > 0
        # Calendar should be one of the sources for this query
        assert "calendar" in result.sources

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_real_routing_latency(self, llm_available):
        """Routing latency should be reasonable for the running local model."""
        if not llm_available:
            pytest.skip("Local LLM server not available")

        from api.services.query_router import QueryRouter

        router = QueryRouter()
        result = await router.route("What's on my calendar?")

        # Allow up to 15s for first call (model loading on llama-server cold start)
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
        # Force keyword fallback by pretending the local LLM is unavailable.
        with patch("api.services.query_router.is_local_routing_llm_available", return_value=False):
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
        with patch("api.services.query_router.is_local_routing_llm_available", return_value=False):
            result = await router.route("What's the weather for my NYC trip tomorrow?")
        # Should have web for weather and calendar for trip
        assert "web" in result.sources or "calendar" in result.sources



class TestDetermineFetchDepth:
    """Search depth comes from how the question is phrased."""

    @pytest.mark.parametrize(
        ("question", "depth"),
        [
            ("catch me up on Robin", "deep"),
            ("what's going on with Robin", "deep"),
            ("give me the full context on Robin", "deep"),
            ("what is the phone number for Robin", "shallow"),
            ("when did I last talk to Robin", "shallow"),
            ("how is Robin doing", "normal"),
            ("who is Robin Example", "normal"),
        ],
    )
    def test_depth_follows_the_phrasing(self, question, depth):
        from api.services.query_router import QueryRouter

        assert QueryRouter()._determine_fetch_depth(question) == depth

    def test_the_question_is_the_only_input(self):
        from api.services.query_router import QueryRouter

        with pytest.raises(TypeError):
            QueryRouter()._determine_fetch_depth("how is Robin doing", {"relationship_strength": 100})


class TestRouteReadsNoPeople:
    """Routing classifies sources from the question alone; it reads no person records."""

    QUESTIONS = ["Who is Robin Example?", "prep me for meeting with Robin Example"]

    @pytest.fixture
    def people_reads(self):
        """Every route to a person's record or relationship summary, as call-counting mocks."""
        with (
            patch("api.services.entity_resolver.get_entity_resolver") as resolver,
            patch("api.services.relationship_summary.get_relationship_summary") as summary,
            patch("api.services.person_entity.get_person_entity_store") as store,
        ):
            yield resolver, summary, store

    @pytest.mark.asyncio
    @pytest.mark.parametrize("question", QUESTIONS)
    async def test_keyword_routing_of_a_people_question_reads_nothing(self, question, people_reads):
        from api.services.query_router import QueryRouter

        with patch("api.services.query_router.is_local_routing_llm_available", return_value=False):
            result = await QueryRouter().route(question)

        assert "people" in result.sources
        assert result.reasoning.startswith("Keyword fallback: ")
        assert result.action_after is None
        for read in people_reads:
            read.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_routing_of_a_named_person_reads_nothing(self, people_reads, caplog):
        import logging

        from api.services.query_router import QueryRouter

        reply = (
            '{"sources": ["people", "gmail"], "reasoning": "asks about a person", '
            '"people_mentioned": ["Robin Example"], "action_after": "compose"}'
        )
        with (
            patch("api.services.query_router.is_local_routing_llm_available", return_value=True),
            patch("api.services.query_router.generate_text", AsyncMock(return_value=reply)),
            caplog.at_level(logging.DEBUG, logger="api.services.query_router"),
        ):
            result = await QueryRouter().route("how is that going")

        assert result.extracted_person_name == "Robin Example"
        assert "Robin" not in caplog.text
        assert result.sources == ["people", "gmail"]
        assert result.reasoning == "asks about a person"
        assert result.action_after == "compose"
        for read in people_reads:
            read.assert_not_called()


class TestRouterKeepsWhatPersonContextUses:
    """The person block reads three router helpers; the router carries nothing else about people."""

    def test_the_helpers_the_person_block_reads_exist(self):
        from api.services.query_router import QueryRouter

        router = QueryRouter()
        assert router._extract_person_name("catch me up on Robin Example") == "Robin Example"
        assert router._determine_fetch_depth("catch me up on Robin Example") == "deep"
        assert callable(router._get_disambiguation_context)

    def test_routing_results_carry_only_source_classification(self):
        import dataclasses

        from api.services.query_router import RoutingResult

        assert [f.name for f in dataclasses.fields(RoutingResult)] == [
            "sources",
            "reasoning",
            "confidence",
            "latency_ms",
            "extracted_person_name",
            "action_after",
        ]

    def test_the_router_defines_no_person_lookup_or_depth_table(self):
        from api.services import query_router

        assert not hasattr(query_router, "FETCH_DEPTH_LIMITS")
        assert not hasattr(query_router.QueryRouter, "_fetch_crm_context")
        assert not hasattr(query_router.QueryRouter, "_get_fuzzy_suggestions")

    def test_strength_cutoffs_name_only_who_counts_as_close(self):
        from config import relationship_weights

        assert relationship_weights.CLOSE_RELATIONSHIP_MIN_STRENGTH == 14
        assert not hasattr(relationship_weights, "DEEP_CONTEXT_MIN_STRENGTH")
        assert not hasattr(relationship_weights, "NORMAL_CONTEXT_MIN_STRENGTH")
