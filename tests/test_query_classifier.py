"""Tests for query type classification."""
import pytest

pytestmark = pytest.mark.unit


class TestQueryClassifier:
    """Test query type detection."""

    def test_possessive_with_identifier_is_factual(self):
        """Possessive + identifier keyword = factual."""
        from api.services.query_classifier import classify_query

        assert classify_query("Jane's KTN") == "factual"
        assert classify_query("Alex's phone number") == "factual"
        assert classify_query("What is John's passport?") == "factual"

    def test_possessive_with_person_name_is_factual(self):
        """Possessive with known person name = factual."""
        from api.services.query_classifier import classify_query

        # Uses ALIAS_MAP to detect known names
        assert classify_query("Jane's birthday") == "factual"
        assert classify_query("What is Alex's email?") == "factual"

    def test_discovery_queries_are_semantic(self):
        """Discovery and preparation queries = semantic."""
        from api.services.query_classifier import classify_query

        assert classify_query("prepare me for meeting with Sarah") == "semantic"
        assert classify_query("what files discuss the Q4 budget") == "semantic"
        assert classify_query("summarize my notes about the project") == "semantic"

    def test_action_verbs_are_semantic(self):
        """Action verbs indicate semantic queries."""
        from api.services.query_classifier import classify_query

        assert classify_query("brief me on the Johnson account") == "semantic"
        assert classify_query("analyze the sales trends") == "semantic"

    def test_short_lookup_is_factual(self):
        """Short queries with proper nouns = factual."""
        from api.services.query_classifier import classify_query

        assert classify_query("Jane birthday") == "factual"
        assert classify_query("Alex phone") == "factual"

    def test_complex_semantic_query(self):
        """Long conceptual queries = semantic."""
        from api.services.query_classifier import classify_query

        result = classify_query(
            "what are the key takeaways from our strategic planning session"
        )
        assert result == "semantic"

    def test_identifier_keywords_are_factual(self):
        """Queries with identifier keywords = factual."""
        from api.services.query_classifier import classify_query

        assert classify_query("passport number") == "factual"
        assert classify_query("SSN") == "factual"
        assert classify_query("phone number") == "factual"
        assert classify_query("email address") == "factual"
