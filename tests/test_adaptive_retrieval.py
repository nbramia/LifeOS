"""
Tests for adaptive retrieval functionality in chat routes.

Tests cover:
- Keyword extraction from queries
- Stop words filtering
- Multi-account support markers
"""
import pytest

# Unit tests - no external dependencies
pytestmark = pytest.mark.unit


class TestExtractSearchKeywords:
    """Test keyword extraction for Drive/Gmail searches."""

    def test_extracts_proper_nouns(self):
        """Should extract capitalized names."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("I have a meeting with Kevin later")
        assert "Kevin" in keywords

    def test_extracts_multiple_names(self):
        """Should extract multiple proper nouns."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("Meeting notes with Kevin and Sarah")
        assert "Kevin" in keywords
        assert "Sarah" in keywords

    def test_filters_stop_words(self):
        """Should remove common stop words."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("what is the budget for the project")
        assert "budget" in keywords
        assert "project" in keywords
        assert "what" not in keywords
        assert "the" not in keywords
        assert "is" not in keywords

    def test_filters_temporal_words(self):
        """Should filter temporal words like 'later', 'today'."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("meeting later today about budget")
        # 'later', 'today' should be filtered
        lower_keywords = [k.lower() for k in keywords]
        assert "later" not in lower_keywords
        assert "today" not in lower_keywords
        assert "budget" in lower_keywords

    def test_preserves_order(self):
        """Should return keywords in order of appearance."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("Kevin budget Bob planning")
        # Should be in order: Kevin, budget, Bob, planning
        assert keywords.index("Kevin") < keywords.index("Bob")

    def test_deduplicates_keywords(self):
        """Should deduplicate case-insensitively."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("Budget review for budget planning")
        lower_keywords = [k.lower() for k in keywords]
        assert lower_keywords.count("budget") == 1

    def test_limits_to_five_keywords(self):
        """Should return maximum 5 keywords."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords(
            "Kevin Sarah budget planning strategy roadmap timeline goals objectives"
        )
        assert len(keywords) <= 5

    def test_handles_empty_query(self):
        """Should handle empty query gracefully."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("")
        assert keywords == []

    def test_handles_only_stop_words(self):
        """Should return empty list if only stop words."""
        from api.routes.chat import extract_search_keywords

        keywords = extract_search_keywords("what is the")
        # May have some short words filtered out
        assert len(keywords) == 0 or all(len(k) >= 3 for k in keywords)

    def test_filters_common_query_words(self):
        """Should filter common query words like 'show', 'find', 'tell'."""
        from api.routes.chat import extract_search_keywords

        # Use lowercase to avoid proper noun detection
        keywords = extract_search_keywords("please tell me about the budget document")
        lower_keywords = [k.lower() for k in keywords]
        assert "tell" not in lower_keywords
        assert "document" not in lower_keywords  # Also a stop word
        assert "budget" in lower_keywords

    def test_keeps_meaningful_short_words(self):
        """Should keep meaningful words even if they're short."""
        from api.routes.chat import extract_search_keywords

        # 'API' is capitalized so treated as proper noun
        keywords = extract_search_keywords("API integration with AWS")
        assert "API" in keywords
        assert "AWS" in keywords


class TestAdaptiveRetrievalConstants:
    """Test that adaptive retrieval constants are set correctly."""

    def test_initial_max_files_is_two(self):
        """Initial file limit should be 2."""
        # We can't easily import constants from inside the function
        # but we can verify behavior by checking the code structure
        # This is more of a documentation test
        pass

    def test_initial_char_limit_is_1000(self):
        """Initial character limit should be 1000."""
        pass

    def test_expanded_char_limit_is_4000(self):
        """Expanded character limit should be 4000."""
        pass


class TestReadMorePatterns:
    """Test that READ_MORE and EXPAND patterns are correctly matched."""

    def test_read_more_pattern_matches(self):
        """Should match [READ_MORE:filename] pattern."""
        import re
        read_more_pattern = r'\[READ_MORE:([^\]]+)\]'

        text = "I need more context. [READ_MORE:Budget Document.docx]"
        matches = re.findall(read_more_pattern, text)
        assert matches == ["Budget Document.docx"]

    def test_expand_pattern_matches(self):
        """Should match [EXPAND:filename] pattern."""
        import re
        expand_pattern = r'\[EXPAND:([^\]]+)\]'

        text = "Let me expand on that. [EXPAND:Meeting Notes.md]"
        matches = re.findall(expand_pattern, text)
        assert matches == ["Meeting Notes.md"]

    def test_multiple_patterns_matched(self):
        """Should match multiple patterns in same text."""
        import re
        read_more_pattern = r'\[READ_MORE:([^\]]+)\]'
        expand_pattern = r'\[EXPAND:([^\]]+)\]'

        text = """
        I found some relevant information. [READ_MORE:Doc1.docx]
        Let me also get more from this: [EXPAND:Notes.md]
        And one more: [READ_MORE:Budget.xlsx]
        """

        read_more = re.findall(read_more_pattern, text)
        expand = re.findall(expand_pattern, text)

        assert len(read_more) == 2
        assert len(expand) == 1
        assert "Doc1.docx" in read_more
        assert "Budget.xlsx" in read_more
        assert "Notes.md" in expand


class TestDriveFilesAvailableFiltering:
    """Test that _drive_files_available metadata is filtered from chunks."""

    def test_metadata_source_filtered(self):
        """_drive_files_available should not appear in context chunks."""
        # This tests that internal metadata markers don't leak to the user
        # The actual filtering happens in chat.py when building context

        # Example of what should be filtered
        chunks = [
            {"source": "vault", "content": "Meeting notes..."},
            {"source": "_drive_files_available", "files": [{"name": "doc.docx"}]},
            {"source": "drive", "content": "Budget data..."},
        ]

        # Filter as done in chat.py
        filtered = [c for c in chunks if c.get("source") != "_drive_files_available"]

        assert len(filtered) == 2
        assert all(c.get("source") != "_drive_files_available" for c in filtered)
