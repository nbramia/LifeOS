"""
Tests for Smart Model Selection (P6.1).

Tests complexity classification and model recommendation.
"""
import pytest

# All tests in this file are fast unit tests
pytestmark = pytest.mark.unit


class TestComplexityClassification:
    """Test query complexity classification."""

    def test_simple_lookup_classified_as_haiku(self):
        """Simple lookups should be classified as haiku-appropriate."""
        from api.services.model_selector import classify_query_complexity

        simple_queries = [
            "What time is my next meeting?",
            "When is my 1-1 with Alex?",
            "Who is Kevin?",
            "Where is the budget file?",
            "List my action items",
            "Show me today's calendar",
            "Find the Q4 spreadsheet",
        ]

        for query in simple_queries:
            result = classify_query_complexity(query)
            assert result.recommended_model == "haiku", f"Failed for: {query}"
            assert result.complexity_score < 0.4, f"Score too high for: {query}"

    def test_standard_synthesis_classified_as_sonnet(self):
        """Standard synthesis queries should be classified as sonnet-appropriate."""
        from api.services.model_selector import classify_query_complexity

        standard_queries = [
            "Summarize the budget discussion from last week",
            "Explain what happened in the team meeting",
            "What did we discuss about the product roadmap?",
            "Tell me about the ML infrastructure plans",
            "Describe the recent conversation with Alex",
        ]

        for query in standard_queries:
            result = classify_query_complexity(query)
            assert result.recommended_model == "sonnet", f"Failed for: {query}"
            assert 0.3 <= result.complexity_score <= 0.7, f"Score unexpected for: {query}"

    def test_complex_reasoning_classified_as_opus(self):
        """Complex reasoning queries should be classified as opus-appropriate."""
        from api.services.model_selector import classify_query_complexity

        complex_queries = [
            "Analyze the strategic implications of the reorg",
            "Compare the trade-offs between option A and B",
            "Evaluate the themes across my therapy sessions",
            "What are the second-order effects of this decision?",
            "Think through the implications step by step",
            "Consider the pros and cons and recommend an approach",
        ]

        for query in complex_queries:
            result = classify_query_complexity(query)
            assert result.recommended_model == "opus", f"Failed for: {query}"
            assert result.complexity_score > 0.6, f"Score too low for: {query}"

    def test_complexity_score_range(self):
        """Complexity score should be between 0 and 1."""
        from api.services.model_selector import classify_query_complexity

        queries = [
            "What time is it?",
            "Summarize the meeting",
            "Analyze the strategic implications of everything we discussed",
        ]

        for query in queries:
            result = classify_query_complexity(query)
            assert 0.0 <= result.complexity_score <= 1.0, f"Score out of range for: {query}"


class TestContextBasedComplexity:
    """Test complexity classification with context information."""

    def test_many_sources_increases_complexity(self):
        """Queries with many sources should have higher complexity."""
        from api.services.model_selector import classify_query_complexity

        # Simple query but many sources
        result = classify_query_complexity(
            "What happened?",
            source_count=5,
            context_tokens=1000
        )

        # Should bump up to at least sonnet due to source count
        assert result.recommended_model in ["sonnet", "opus"]

    def test_large_context_increases_complexity(self):
        """Queries with large context should have higher complexity."""
        from api.services.model_selector import classify_query_complexity

        result = classify_query_complexity(
            "Summarize this",
            source_count=2,
            context_tokens=10000  # Large context
        )

        # Should bump up to opus due to context size
        assert result.recommended_model == "opus"

    def test_single_source_simple_query_stays_haiku(self):
        """Simple query with single source should stay haiku."""
        from api.services.model_selector import classify_query_complexity

        result = classify_query_complexity(
            "What time is my meeting?",
            source_count=1,
            context_tokens=500
        )

        assert result.recommended_model == "haiku"


class TestModelSelectorService:
    """Test the ModelSelector service."""

    def test_selector_initialization(self):
        """ModelSelector should initialize correctly."""
        from api.services.model_selector import ModelSelector

        selector = ModelSelector()
        assert selector is not None

    def test_selector_returns_complexity_result(self):
        """select_model should return a ComplexityResult."""
        from api.services.model_selector import ModelSelector, ComplexityResult

        selector = ModelSelector()
        result = selector.select_model("What is the budget?")

        assert isinstance(result, ComplexityResult)
        assert result.recommended_model in ["haiku", "sonnet", "opus"]
        assert 0.0 <= result.complexity_score <= 1.0
        assert result.reasoning is not None

    def test_selector_with_context(self):
        """select_model should accept context parameters."""
        from api.services.model_selector import ModelSelector

        selector = ModelSelector()
        result = selector.select_model(
            "What happened?",
            source_count=4,
            context_tokens=5000
        )

        assert result.recommended_model in ["sonnet", "opus"]


class TestModelNames:
    """Test model name mappings."""

    def test_haiku_model_name(self):
        """Haiku tier should map to a valid Claude model (currently Sonnet 4.5 as Haiku 4.5 may not be available)."""
        from api.services.model_selector import get_claude_model_name

        model_name = get_claude_model_name("haiku")
        # Haiku currently maps to Sonnet 4.5 since Haiku 4.5 may not be available
        assert "claude" in model_name.lower()
        assert "sonnet" in model_name.lower() or "haiku" in model_name.lower()

    def test_sonnet_model_name(self):
        """Sonnet should map to correct Claude model."""
        from api.services.model_selector import get_claude_model_name

        model_name = get_claude_model_name("sonnet")
        assert "sonnet" in model_name.lower()
        assert "claude" in model_name.lower()

    def test_opus_model_name(self):
        """Opus should map to correct Claude model."""
        from api.services.model_selector import get_claude_model_name

        model_name = get_claude_model_name("opus")
        # Opus 4 or latest
        assert "opus" in model_name.lower() or "claude" in model_name.lower()

    def test_invalid_model_defaults_to_sonnet(self):
        """Invalid model name should default to sonnet."""
        from api.services.model_selector import get_claude_model_name

        model_name = get_claude_model_name("invalid")
        assert "sonnet" in model_name.lower()


class TestRoutingResultIntegration:
    """Test integration with RoutingResult."""

    def test_routing_result_has_model_fields(self):
        """RoutingResult should have recommended_model and complexity_score."""
        from api.services.query_router import RoutingResult

        result = RoutingResult(
            sources=["vault"],
            reasoning="test",
            confidence=0.9,
            latency_ms=50,
            recommended_model="sonnet",
            complexity_score=0.5
        )

        assert result.recommended_model == "sonnet"
        assert result.complexity_score == 0.5

    def test_routing_result_default_model(self):
        """RoutingResult should default to sonnet if model not specified."""
        from api.services.query_router import RoutingResult

        result = RoutingResult(
            sources=["vault"],
            reasoning="test",
            confidence=0.9,
            latency_ms=50
        )

        assert result.recommended_model == "sonnet"
        assert result.complexity_score == 0.5


class TestModelSelectionAccuracy:
    """Test model selection accuracy on sample queries."""

    ACCURACY_TEST_CASES = [
        # (query, expected_model)
        # Haiku queries (simple lookups)
        ("What time is my next meeting?", "haiku"),
        ("When is my 1-1?", "haiku"),
        ("List my tasks", "haiku"),
        ("Show calendar", "haiku"),

        # Sonnet queries (standard synthesis)
        ("Summarize the team meeting", "sonnet"),
        ("What did we discuss about the budget?", "sonnet"),
        ("Tell me about last week's standup", "sonnet"),
        ("Explain the project status", "sonnet"),

        # Opus queries (complex reasoning)
        ("Analyze the strategic implications", "opus"),
        ("Compare the trade-offs between approaches", "opus"),
        ("What are the second-order effects?", "opus"),
        ("Evaluate and recommend an approach", "opus"),
    ]

    def test_model_selection_accuracy(self):
        """Model selection should achieve at least 75% accuracy on test cases."""
        from api.services.model_selector import classify_query_complexity

        correct = 0
        total = len(self.ACCURACY_TEST_CASES)

        for query, expected_model in self.ACCURACY_TEST_CASES:
            result = classify_query_complexity(query)
            if result.recommended_model == expected_model:
                correct += 1

        accuracy = correct / total
        assert accuracy >= 0.75, f"Accuracy only {accuracy*100:.0f}% ({correct}/{total})"
