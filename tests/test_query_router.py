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


class TestStrengthCutoffs:
    """Relationship strength sets how much context a query fetches, and who to disambiguate."""

    @pytest.mark.parametrize(
        ("strength", "depth"),
        [(100, "deep"), (53, "deep"), (52.9, "normal"), (18, "normal"), (17.9, "shallow"), (0, "shallow")],
    )
    def test_fetch_depth(self, strength, depth):
        from api.services.query_router import QueryRouter

        context = {"relationship_strength": strength}
        assert QueryRouter()._determine_fetch_depth("how is Robin doing", context) == depth

    def test_an_unresolved_name_has_no_strength_to_go_on(self):
        from api.services.query_router import QueryRouter

        context = {"resolution_failed": True, "failure_type": "ambiguous", "candidates": []}
        assert QueryRouter()._determine_fetch_depth("how is Robin doing", context) == "normal"


@pytest.fixture
def crm(tmp_path, monkeypatch):
    """Point the real CRM stores at a throwaway database; nothing is stubbed out."""
    from api.services import entity_resolver, interaction_store, person_entity, person_facts
    from api.services.entity_resolver import EntityResolver
    from api.services.interaction_store import InteractionStore
    from api.services.link_override import LinkOverrideStore
    from api.services.person_entity import PersonEntityStore
    from api.services.person_facts import PersonFactStore

    store = PersonEntityStore(str(tmp_path / "crm.db"))
    overrides = LinkOverrideStore(tmp_path / "crm.db")
    monkeypatch.setattr(entity_resolver, "get_link_override_store", lambda: overrides)
    monkeypatch.setattr(person_entity, "_entity_store", store)
    monkeypatch.setattr(entity_resolver, "_entity_resolver", EntityResolver(store))
    monkeypatch.setattr(interaction_store, "_interaction_store", InteractionStore(str(tmp_path / "interactions.db")))
    monkeypatch.setattr(person_facts, "_fact_store", PersonFactStore(str(tmp_path / "crm.db")))
    return store


def _add_person(store, name, strength):
    from datetime import datetime, timedelta, timezone

    from api.services.person_entity import PersonEntity

    person = PersonEntity(canonical_name=name, last_seen=datetime.now(timezone.utc) - timedelta(days=2))
    person.relationship_strength = strength
    return store.add(person)


class TestCrmContext:
    """_fetch_crm_context runs against the real resolver, store and summary modules."""

    def _fetch(self, name):
        from api.services.query_router import QueryRouter

        return QueryRouter()._fetch_crm_context(name)

    def test_resolved_person_carries_their_strength_and_summary(self, crm):
        person = _add_person(crm, "Robin Example", 60.0)

        context = self._fetch("Robin Example")

        assert context["entity_id"] == person.id
        assert context["relationship_strength"] == 60.0
        assert context["total_interactions_90d"] == 0

    def test_two_close_people_with_a_shared_first_name_need_disambiguation(self, crm):
        first = _add_person(crm, "Robin Alpha", 30.0)
        second = _add_person(crm, "Robin Beta", 25.0)

        context = self._fetch("Robin")

        assert context["resolution_failed"] is True
        assert context["failure_type"] == "ambiguous"
        assert [c["id"] for c in context["candidates"]] == [first.id, second.id]

    def test_two_people_at_the_close_cutoff_need_disambiguation(self, crm):
        _add_person(crm, "Robin Alpha", 14.0)
        _add_person(crm, "Robin Beta", 14.0)

        assert self._fetch("Robin")["failure_type"] == "ambiguous"

    def test_one_person_below_the_close_cutoff_does_not(self, crm):
        close = _add_person(crm, "Robin Alpha", 14.0)
        _add_person(crm, "Robin Beta", 13.9)

        assert self._fetch("Robin")["entity_id"] == close.id

    def test_a_clearly_closer_person_is_settled_without_a_question(self, crm):
        close = _add_person(crm, "Robin Alpha", 90.0)
        _add_person(crm, "Robin Beta", 20.0)

        context = self._fetch("Robin")

        assert context["entity_id"] == close.id
        assert "resolution_failed" not in context

    def test_tied_people_who_are_not_close_do_not_need_disambiguation(self, crm):
        _add_person(crm, "Robin Alpha", 5.0)
        _add_person(crm, "Robin Beta", 5.0)

        context = self._fetch("Robin")

        assert context is None or context.get("failure_type") != "ambiguous"

    def test_scored_tie_between_close_people_needs_disambiguation(self, crm):
        _add_person(crm, "Robin Alpha", 30.0)
        _add_person(crm, "Riley Alpha", 30.0)

        assert self._fetch("R Alpha")["failure_type"] == "ambiguous"

    def test_scored_tie_with_one_close_person_settles_on_them(self, crm):
        close = _add_person(crm, "Robin Alpha", 30.0)
        _add_person(crm, "Riley Alpha", 0.0)

        context = self._fetch("R Alpha")

        assert context["entity_id"] == close.id
        assert "resolution_failed" not in context

    def test_near_miss_gets_suggestions(self, crm):
        _add_person(crm, "Robin Example", 60.0)

        context = self._fetch("Robyn Exampl")

        assert context["failure_type"] == "no_match"
        assert context["suggestions"] == ["Robin Example"]

    def test_unknown_name_has_no_context(self, crm):
        _add_person(crm, "Robin Example", 60.0)

        assert self._fetch("Quillon Zzyx") is None

    def test_candidates_are_capped_at_five(self, crm):
        for suffix in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"):
            _add_person(crm, f"Robin {suffix}", 30.0)

        assert len(self._fetch("Robin")["candidates"]) == 5

    def test_lookup_never_writes_a_person(self, crm):
        _add_person(crm, "Robin Alpha", 30.0)
        _add_person(crm, "Robin Beta", 30.0)

        for name in ("Robin", "Quillon", "Quillon Zzyx", "R Alpha"):
            self._fetch(name)

        assert crm.count() == 2

    def test_failure_returns_none_and_logs_no_name(self, crm, caplog):
        from api.services import entity_resolver

        secret = "Quillon Zzyx"

        class Broken:
            def match_name(self, name):
                raise RuntimeError(f"cannot read {name}")

        with patch.object(entity_resolver, "_entity_resolver", Broken()):
            assert self._fetch(secret) is None

        assert "RuntimeError" in caplog.text
        assert secret not in caplog.text


class TestRouteWithCrmContext:
    """route() enriches people queries through the real import path."""

    async def _route(self, question):
        from api.services.query_router import QueryRouter

        with patch("api.services.query_router.is_local_routing_llm_available", return_value=False):
            return await QueryRouter().route(question)

    @pytest.mark.asyncio
    async def test_a_strong_relationship_deepens_the_fetch(self, crm):
        _add_person(crm, "Robin Example", 60.0)

        result = await self._route("Who is Robin Example?")

        assert result.relationship_context["relationship_strength"] == 60.0
        assert result.fetch_depth == "deep"

    @pytest.mark.asyncio
    async def test_a_weak_relationship_narrows_the_fetch(self, crm):
        _add_person(crm, "Robin Example", 3.0)

        result = await self._route("Who is Robin Example?")

        assert result.fetch_depth == "shallow"

    @pytest.mark.asyncio
    async def test_an_ambiguous_name_is_reported_at_normal_depth(self, crm):
        _add_person(crm, "Robin Alpha", 30.0)
        _add_person(crm, "Robin Beta", 30.0)

        result = await self._route("Who is Robin?")

        assert result.relationship_context["failure_type"] == "ambiguous"
        assert result.fetch_depth == "normal"

    @pytest.mark.asyncio
    async def test_an_unknown_name_keeps_the_question_based_depth(self, crm):
        result = await self._route("Who is Quillon Zzyx?")

        assert result.relationship_context is None
        assert result.fetch_depth == "normal"

    @pytest.mark.asyncio
    async def test_a_crm_failure_does_not_fail_routing(self, crm):
        from api.services import entity_resolver

        class Broken:
            def match_name(self, name):
                raise RuntimeError("store unavailable")

        with patch.object(entity_resolver, "_entity_resolver", Broken()):
            result = await self._route("Who is Robin Example?")

        assert result.sources
        assert result.relationship_context is None
        assert result.fetch_depth == "normal"

    @pytest.mark.asyncio
    async def test_a_slow_lookup_counts_in_latency_and_leaves_the_event_loop_free(self, crm):
        import asyncio
        import time

        from api.services.query_router import QueryRouter

        router = QueryRouter()

        def slow_lookup(name):
            time.sleep(0.2)
            return None

        ticks = []

        async def tick():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(tick())
        with patch("api.services.query_router.is_local_routing_llm_available", return_value=False), \
             patch.object(router, "_fetch_crm_context", slow_lookup):
            result = await router.route("Who is Robin Example?")
        ticker.cancel()

        assert result.latency_ms >= 200
        assert len(ticks) >= 5

    @pytest.mark.asyncio
    async def test_routing_logs_no_person_name(self, crm, caplog):
        import logging

        _add_person(crm, "Robin Example", 60.0)

        with caplog.at_level(logging.INFO, logger="api.services.query_router"):
            await self._route("Who is Robin Example?")

        assert "CRM context found" in caplog.text
        assert "Robin" not in caplog.text

    @pytest.mark.asyncio
    async def test_llm_extracted_name_is_not_logged(self, crm, caplog):
        import logging

        from api.services.query_router import QueryRouter

        _add_person(crm, "Robin Example", 60.0)
        reply = '{"sources": ["people"], "reasoning": "x", "people_mentioned": ["Robin Example"]}'

        with patch("api.services.query_router.is_local_routing_llm_available", return_value=True), \
             patch("api.services.query_router.generate_text", new_callable=AsyncMock, return_value=reply), \
             caplog.at_level(logging.INFO, logger="api.services.query_router"):
            result = await QueryRouter().route("how is that going")

        assert result.extracted_person_name == "Robin Example"
        assert result.fetch_depth == "deep"
        assert "Robin" not in caplog.text
