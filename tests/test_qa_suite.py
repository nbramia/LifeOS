"""
QA Test Suite for LifeOS.

Tests search quality, recency bias, and response correctness.
Run with: pytest tests/test_qa_suite.py -v

Requires: API server running at localhost:8000
"""
import pytest

# These tests require the API server to be running
pytestmark = [pytest.mark.integration, pytest.mark.requires_server]
import httpx
from datetime import datetime

BASE_URL = "http://localhost:8000"


class TestSearchQuality:
    """Test search result quality and recency bias."""

    @pytest.fixture
    def client(self):
        """HTTP client for API requests."""
        return httpx.Client(base_url=BASE_URL, timeout=30.0)

    def test_recency_bias_recent_dates_ranked_higher(self, client):
        """Recent documents should rank higher than old ones."""
        response = client.post("/api/search", json={
            "query": "meeting notes discussion",
            "limit": 20
        })
        assert response.status_code == 200
        results = response.json()["results"]

        # Count how many of top 10 are from current year
        current_year = datetime.now().year
        recent_count = sum(
            1 for r in results[:10]
            if r.get("modified_date", "").startswith(str(current_year))
            or r.get("note_type") == "ML"  # ML folder = current job
        )

        # At least 50% should be recent
        assert recent_count >= 5, f"Only {recent_count}/10 results are from {current_year} or ML folder"

    def test_no_ancient_results_in_top_5(self, client):
        """Very old documents (pre-2020) should not appear in top 5 for general queries."""
        response = client.post("/api/search", json={
            "query": "action items tasks todo",
            "limit": 10
        })
        assert response.status_code == 200
        results = response.json()["results"]

        for i, r in enumerate(results[:5]):
            date = r.get("modified_date", "")
            if date:
                year = int(date[:4]) if date[:4].isdigit() else 2025
                assert year >= 2020, f"Result {i+1} is from {year}, too old for top 5"

    def test_ml_folder_gets_boost(self, client):
        """ML folder items should be boosted (recency_score near 0.95)."""
        response = client.post("/api/search", json={
            "query": "campaign operations work",
            "limit": 20
        })
        assert response.status_code == 200
        results = response.json()["results"]

        ml_results = [r for r in results if r.get("note_type") == "ML"]
        if ml_results:
            # ML items should have high recency scores
            for r in ml_results:
                recency = r.get("recency_score", 0)
                assert recency >= 0.90, f"ML item has low recency score: {recency}"

    def test_semantic_relevance_maintained(self, client):
        """Semantic relevance should still work - related terms should match."""
        # Search for meetings
        response = client.post("/api/search", json={
            "query": "calendar schedule appointment",
            "limit": 10
        })
        assert response.status_code == 200
        results = response.json()["results"]

        # At least some results should have meeting-related content
        meeting_terms = ["meeting", "calendar", "schedule", "sync", "call", "chat"]
        found_relevant = any(
            any(term in r.get("content", "").lower() for term in meeting_terms)
            for r in results[:5]
        )
        assert found_relevant, "No meeting-related results found for calendar query"

    def test_search_returns_scores(self, client):
        """Search should return both semantic and recency scores."""
        response = client.post("/api/search", json={
            "query": "test query",
            "limit": 5
        })
        assert response.status_code == 200
        results = response.json()["results"]

        if results:
            r = results[0]
            assert "score" in r, "Missing combined score"
            assert "semantic_score" in r, "Missing semantic score"
            assert "recency_score" in r, "Missing recency score"


class TestStreamingEndpoint:
    """Test the streaming ask endpoint."""

    @pytest.fixture
    def client(self):
        """HTTP client for API requests."""
        return httpx.Client(base_url=BASE_URL, timeout=60.0)

    def test_streaming_returns_response(self, client):
        """Streaming endpoint should return content."""
        response = client.post(
            "/api/ask/stream",
            json={"question": "What meetings have I had recently?"},
            headers={"Accept": "text/event-stream"}
        )
        assert response.status_code == 200

        # Check we got some content
        content = response.text
        assert "data:" in content, "No SSE data received"
        assert "content" in content, "No content in response"

    def test_streaming_mentions_recent_content(self, client):
        """Response should reference recent dates/content, not old stuff."""
        response = client.post(
            "/api/ask/stream",
            json={"question": "What are my recent action items?"},
            headers={"Accept": "text/event-stream"}
        )
        assert response.status_code == 200

        content = response.text.lower()

        # Should mention recent years
        recent_year_mentioned = "2025" in content or "2024" in content

        # Should NOT prominently mention very old years in action items
        ancient_mentions = content.count("2017") + content.count("2016") + content.count("2015")

        # Allow some old mentions but shouldn't dominate
        assert recent_year_mentioned or ancient_mentions < 3, \
            "Response focuses too much on old content"


class TestSourceAttribution:
    """Test that sources are properly attributed."""

    @pytest.fixture
    def client(self):
        """HTTP client for API requests."""
        return httpx.Client(base_url=BASE_URL, timeout=30.0)

    def test_search_returns_file_info(self, client):
        """Search results should include file path and name."""
        response = client.post("/api/search", json={
            "query": "any query",
            "limit": 5
        })
        assert response.status_code == 200
        results = response.json()["results"]

        if results:
            r = results[0]
            assert "file_path" in r, "Missing file_path"
            assert "file_name" in r, "Missing file_name"
            assert r["file_name"].endswith(".md"), "File should be markdown"


class TestDateExtraction:
    """Test that dates are extracted from filenames correctly."""

    @pytest.fixture
    def client(self):
        """HTTP client for API requests."""
        return httpx.Client(base_url=BASE_URL, timeout=30.0)

    def test_dated_files_have_correct_date(self, client):
        """Files with dates in filename should have correct modified_date."""
        response = client.post("/api/search", json={
            "query": "daily notes journal",
            "limit": 20
        })
        assert response.status_code == 200
        results = response.json()["results"]

        for r in results:
            fname = r.get("file_name", "")
            date = r.get("modified_date", "")

            # Check YYYY-MM-DD pattern files
            if fname and len(fname) >= 10:
                # e.g., "2025-11-03.md"
                if fname[:4].isdigit() and fname[4] == "-" and fname[7] == "-":
                    expected_date = fname[:10]
                    assert date == expected_date, \
                        f"Date mismatch for {fname}: expected {expected_date}, got {date}"

    def test_yyyymmdd_pattern_extracted(self, client):
        """Files with YYYYMMDD pattern should have date extracted (or at least be recent)."""
        response = client.post("/api/search", json={
            "query": "meeting sync call",
            "limit": 30
        })
        assert response.status_code == 200
        results = response.json()["results"]

        # Find results with YYYYMMDD pattern in filename
        import re
        extracted_count = 0
        checked_count = 0

        for r in results:
            fname = r.get("file_name", "")
            date = r.get("modified_date", "")

            # e.g., "Meeting 20251210.md"
            match = re.search(r"(\d{4})(\d{2})(\d{2})", fname)
            if match:
                expected = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
                year = int(match.group(1))
                if 2000 <= year <= 2100:
                    checked_count += 1
                    # Date should either match exactly or start with expected date
                    if date == expected or date.startswith(expected):
                        extracted_count += 1

        # At least 70% of dated files should have correct date extraction
        # (some edge cases exist for recently created files)
        if checked_count > 0:
            extraction_rate = extracted_count / checked_count
            assert extraction_rate >= 0.7, \
                f"Only {extraction_rate*100:.0f}% of dated files have correct dates ({extracted_count}/{checked_count})"
