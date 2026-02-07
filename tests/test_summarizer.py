"""
Tests for Document Summarization (P9.4).

Tests the summarizer service for generating document summaries.
"""
import pytest
from unittest.mock import patch, MagicMock

# Most tests in this file are fast unit tests
pytestmark = pytest.mark.unit


class TestGenerateSummary:
    """Test the generate_summary function."""

    def test_returns_none_for_short_content(self):
        """Should return None for content < 100 chars."""
        from api.services.summarizer import generate_summary

        summary, success = generate_summary("Short content.", "test.md")
        assert summary is None
        assert success is True  # Not a failure, just skipped

    def test_returns_none_for_empty_content(self):
        """Should return None for empty content."""
        from api.services.summarizer import generate_summary

        summary, success = generate_summary("", "test.md")
        assert summary is None
        assert success is True  # Not a failure, just skipped

    @patch("api.services.summarizer.httpx.Client")
    def test_calls_ollama_with_prompt(self, mock_client_class):
        """Should call Ollama with the summary prompt."""
        from api.services.summarizer import generate_summary

        # Setup mock
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "response": "This is a meeting note about Q4 budget planning with Kevin and Sarah."
        }
        mock_client.post.return_value = mock_response

        # Call function
        content = "Long content " * 20  # > 100 chars
        summary, success = generate_summary(content, "test.md")

        # Verify
        assert summary is not None
        assert "meeting note" in summary.lower() or "budget" in summary.lower()
        mock_client.post.assert_called_once()

    @patch("api.services.summarizer.httpx.Client")
    def test_truncates_long_content(self, mock_client_class):
        """Should truncate content to max_content_chars."""
        from api.services.summarizer import generate_summary

        # Setup mock
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.json.return_value = {"response": "A valid summary text here."}
        mock_client.post.return_value = mock_response

        # Call with very long content
        long_content = "A" * 5000
        result = generate_summary(long_content, "test.md", max_content_chars=2000)

        # Verify the call was made with truncated content
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        prompt = payload["prompt"]
        assert "[... content truncated ...]" in prompt

    @patch("api.services.summarizer.httpx.Client")
    def test_returns_none_on_timeout(self, mock_client_class):
        """Should return (None, False) on Ollama timeout for retry tracking."""
        import httpx
        from api.services.summarizer import generate_summary

        # Setup mock to raise timeout
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.TimeoutException("timeout")

        # Content must be > 100 chars to avoid early return
        content = "This is some real content about meeting notes with Kevin and Sarah. " * 3 + "\n\nMore content here."
        summary, success = generate_summary(content, "meeting.md")

        # Should return None with failure flag for retry
        assert summary is None
        assert success is False

    @patch("api.services.summarizer.httpx.Client")
    def test_returns_none_on_connection_error(self, mock_client_class):
        """Should return (None, False) on connection error for retry tracking."""
        import httpx
        from api.services.summarizer import generate_summary

        # Setup mock to raise connection error
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.ConnectError("connection failed")

        # Content must be > 100 chars to avoid early return
        content = "Document content here with enough length to process. " * 3 + "This is important project documentation."
        summary, success = generate_summary(content, "notes.md")

        # Should return None with failure flag for retry
        assert summary is None
        assert success is False


class TestFallbackSummary:
    """Test the _fallback_summary function."""

    def test_extracts_first_meaningful_line(self):
        """Should use first non-header, non-frontmatter line."""
        from api.services.summarizer import _fallback_summary

        content = """---
tags: [test]
---

# Header

This is the first real content line that should be used for the fallback summary.

More content here.
"""
        result = _fallback_summary(content, "test.md")

        assert "test.md" in result
        assert "first real content line" in result

    def test_skips_headers(self):
        """Should skip header lines starting with #."""
        from api.services.summarizer import _fallback_summary

        content = """# Main Header
## Subheader
This is the actual content.
"""
        result = _fallback_summary(content, "test.md")
        assert "actual content" in result

    def test_handles_only_headers(self):
        """Should return generic fallback if only headers."""
        from api.services.summarizer import _fallback_summary

        content = """# Header
## Subheader
### Another header
"""
        result = _fallback_summary(content, "test.md")
        assert "test.md" in result
        assert "various notes" in result

    def test_truncates_long_lines(self):
        """Should truncate lines longer than 150 chars."""
        from api.services.summarizer import _fallback_summary

        long_line = "A" * 200
        content = f"# Header\n\n{long_line}"
        result = _fallback_summary(content, "test.md")

        # Should have ellipsis and not the full line
        assert "..." in result
        assert len(result) < 200


class TestIsOllamaAvailable:
    """Test the is_ollama_available function."""

    @patch("api.services.summarizer.httpx.Client")
    def test_returns_true_when_available(self, mock_client_class):
        """Should return True when Ollama responds."""
        from api.services.summarizer import is_ollama_available

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response

        assert is_ollama_available() is True

    @patch("api.services.summarizer.httpx.Client")
    def test_returns_false_on_error(self, mock_client_class):
        """Should return False when Ollama fails."""
        from api.services.summarizer import is_ollama_available

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("connection failed")

        assert is_ollama_available() is False


class TestCreateSummaryChunk:
    """Test the create_summary_chunk function."""

    def test_creates_chunk_with_correct_fields(self):
        """Should create chunk with all required fields."""
        from api.services.summarizer import create_summary_chunk

        chunk = create_summary_chunk(
            summary="This is a test summary.",
            file_path="/path/to/test.md",
            file_name="test.md",
            metadata={"people": ["Alice"], "tags": ["test"]}
        )

        assert chunk["content"] == "Document summary for test.md: This is a test summary."
        assert chunk["chunk_index"] == -1
        assert chunk["is_summary"] is True
        assert chunk["file_path"] == "/path/to/test.md"
        assert chunk["file_name"] == "test.md"
        assert chunk["metadata"]["is_summary"] is True
        assert chunk["metadata"]["chunk_type"] == "summary"
        assert chunk["metadata"]["people"] == ["Alice"]


class TestSummarizerIntegration:
    """Integration tests with actual Ollama (requires running server)."""

    @pytest.mark.slow
    @pytest.mark.integration
    def test_real_summary_generation(self):
        """Test actual summary generation with Ollama."""
        from api.services.summarizer import generate_summary, is_ollama_available

        if not is_ollama_available():
            pytest.skip("Ollama not available")

        content = """# Meeting Notes - Q4 Budget Review

Date: 2025-01-13
Attendees: Kevin, Sarah, John

## Summary
We discussed the Q4 budget projections and identified cost reduction opportunities.

## Key Decisions
- Budget for 2025 increased by 15%
- New ML infrastructure approved
- Headcount freeze extended to Q2

## Action Items
- [ ] Kevin: Finalize budget spreadsheet
- [ ] Sarah: Review vendor contracts
"""

        summary, success = generate_summary(content, "Q4 Budget Review.md")

        # Should return a summary
        assert summary is not None
        assert success is True
        assert len(summary) >= 20
        assert len(summary) <= 500
        # Should mention key topics
        assert any(word in summary.lower() for word in ["budget", "meeting", "q4", "review"])
