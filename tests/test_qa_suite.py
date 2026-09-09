"""
QA Test Suite for LifeOS.

Tests search quality, recency bias, and response correctness.
Run with: pytest tests/test_qa_suite.py -v

Requires: an owned candidate supplied by the test-instance runner.
"""
import json
import httpx
import pytest

from tests.synthetic_qa_corpus import (
    SYNTHETIC_DAILY_JOURNAL_RECORD,
    SYNTHETIC_LEGACY_RECORD,
    SYNTHETIC_MEETING_DISCUSSION_RECORD,
    SYNTHETIC_MEETING_SYNC_CALL_RECORD,
    SYNTHETIC_ML_RECORD,
    SYNTHETIC_OLD_ACTIONS_DATE,
    SYNTHETIC_RECENT_ACTIONS_DATE,
    SYNTHETIC_RECENT_ACTIONS_TRIGGER,
    SYNTHETIC_RECENT_CUTOFF_DAYS,
    seed_synthetic_qa_corpus,
)

# These tests require the API server to be running
pytestmark = [pytest.mark.integration, pytest.mark.requires_server]


def _sse_events(response: httpx.Response) -> list[dict]:
    """Decode the JSON SSE events emitted by ``/api/ask/stream``."""

    events = []
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


@pytest.fixture(scope="module", autouse=True)
def synthetic_qa_corpus(candidate_base_url):
    """Seed the corpus through the helper in this module's candidate lane."""

    with seed_synthetic_qa_corpus(candidate_base_url) as corpus:
        yield corpus

class TestSearchQuality:
    """Test search result quality and recency bias."""

    @pytest.fixture
    def client(self, candidate_base_url):
        """HTTP client for API requests."""
        return httpx.Client(base_url=candidate_base_url, timeout=30.0)

    def test_recency_bias_recent_dates_ranked_higher(self, client, synthetic_qa_corpus):
        """Recent documents should rank higher than old ones."""
        response = client.post("/api/search", json={
            "query": "meeting notes discussion",
            "top_k": 20
        })
        assert response.status_code == 200
        results = response.json()["results"]

        top10_names = {r["file_name"] for r in results[:10]}
        expected_recent = synthetic_qa_corpus.recent_file_names
        assert len(top10_names & expected_recent) >= 5, (
            f"Expected at least five synthetic recent (within {SYNTHETIC_RECENT_CUTOFF_DAYS} days) "
            f"notes in the top ten, got {sorted(top10_names & expected_recent)}"
        )

    def test_no_ancient_results_in_top_5(self, client, synthetic_qa_corpus):
        """A document well outside the recency window should not appear in
        the top 5 for a query it's otherwise topically comparable to."""
        response = client.post("/api/search", json={
            "query": "action items tasks todo",
            "top_k": 10
        })
        assert response.status_code == 200
        results = response.json()["results"]
        assert results, "Synthetic action-item corpus returned no results"
        assert all(
            r.get("file_name") != SYNTHETIC_LEGACY_RECORD.file_name
            for r in results[:5]
        ), "The synthetic legacy (well-outside-the-recency-window) action-item note ranked in the top five"

        recent_cutoff = SYNTHETIC_LEGACY_RECORD.modified_date
        for i, r in enumerate(results[:5]):
            date = r.get("modified_date", "")
            assert date, f"Synthetic result {i + 1} has no extracted date"
            assert date > recent_cutoff, (
                f"Result {i + 1} ({date}) is no more recent than the synthetic legacy "
                f"record ({recent_cutoff}), too old for top 5"
            )

    def test_ml_folder_gets_boost(self, client):
        """ML folder items should be boosted (recency_score near 0.95)."""
        response = client.post("/api/search", json={
            "query": "campaign operations work",
            "top_k": 20
        })
        assert response.status_code == 200
        results = response.json()["results"]

        ml_results = [r for r in results if r.get("note_type") == "ML"]
        assert ml_results, "Synthetic ML corpus note was not returned"
        assert ml_results[0]["file_name"] == SYNTHETIC_ML_RECORD.file_name
        # ML folder content is a current-job exception and receives 0.95.
        assert ml_results[0]["recency_score"] == pytest.approx(0.95)

    def test_semantic_relevance_maintained(self, client):
        """Semantic relevance should still work - related terms should match."""
        # Search for meetings
        response = client.post("/api/search", json={
            "query": "calendar schedule appointment",
            "top_k": 10
        })
        assert response.status_code == 200
        results = response.json()["results"]

        expected_files = {
            SYNTHETIC_MEETING_DISCUSSION_RECORD.file_name,
            SYNTHETIC_MEETING_SYNC_CALL_RECORD.file_name,
        }
        assert any(r.get("file_name") in expected_files for r in results[:5]), (
            "Calendar query did not retrieve either synthetic appointment note"
        )
        assert any(
            "calendar" in r.get("content", "").lower()
            and "appointment" in r.get("content", "").lower()
            for r in results[:5]
        ), "Calendar query returned no semantically relevant synthetic content"

    def test_search_returns_scores(self, client):
        """Search should return both semantic and recency scores."""
        response = client.post("/api/search", json={
            "query": "test query",
            "top_k": 5
        })
        assert response.status_code == 200
        results = response.json()["results"]
        assert results, "Synthetic score corpus returned no results"

        for r in results:
            assert "score" in r, "Missing combined score"
            assert "semantic_score" in r, "Missing semantic score"
            assert "recency_score" in r, "Missing recency score"
            assert isinstance(r["score"], (int, float))
            assert isinstance(r["semantic_score"], (int, float))
            assert isinstance(r["recency_score"], (int, float))


class TestStreamingEndpoint:
    """Test the streaming ask endpoint."""

    @pytest.fixture
    def client(self, candidate_base_url):
        """HTTP client for API requests."""
        return httpx.Client(base_url=candidate_base_url, timeout=60.0)

    def test_streaming_returns_response(self, client):
        """Streaming endpoint should return non-empty generated content."""
        response = client.post(
            "/api/ask/stream",
            json={"question": "What meetings have I had recently?"},
            headers={"Accept": "text/event-stream"}
        )
        assert response.status_code == 200

        # Decode the actual content events rather than substring-matching the
        # raw SSE text: every content frame's JSON always carries the literal
        # key name "content" (e.g. {"type": "content", "content": ""}), so a
        # raw-text check for "content" passes even on an empty answer.
        events = _sse_events(response)
        content = "".join(
            event.get("content", "") for event in events if event.get("type") == "content"
        )
        assert content.strip(), "No non-empty content event in the SSE stream"

    def test_streaming_mentions_recent_content(self, client):
        """The provider fixture must search the corpus before answering."""
        response = client.post(
            "/api/ask/stream",
            json={
                "question": (
                    "What are my recent action items? "
                    f"{SYNTHETIC_RECENT_ACTIONS_TRIGGER}"
                ),
                "include_sources": True,
            },
            headers={"Accept": "text/event-stream"}
        )
        assert response.status_code == 200

        events = _sse_events(response)
        content = "".join(
            event.get("content", "") for event in events if event.get("type") == "content"
        ).lower()
        source_events = [event for event in events if event.get("type") == "sources"]
        source_names = [
            source.get("file_name", "")
            for event in source_events
            for source in event.get("sources", [])
        ]

        assert any("search_vault" in name for name in source_names), (
            "Synthetic recent-action provider did not report a search_vault source"
        )
        assert SYNTHETIC_RECENT_ACTIONS_DATE in content, (
            "Synthetic recent-action response omitted the seeded recent date"
        )
        assert SYNTHETIC_OLD_ACTIONS_DATE not in content, (
            "Synthetic recent-action response surfaced the old archive date"
        )


class TestSourceAttribution:
    """Test that sources are properly attributed."""

    @pytest.fixture
    def client(self, candidate_base_url):
        """HTTP client for API requests."""
        return httpx.Client(base_url=candidate_base_url, timeout=30.0)

    def test_search_returns_file_info(self, client):
        """Search results should include file path and name."""
        response = client.post("/api/search", json={
            "query": "synthetic test query",
            "top_k": 5
        })
        assert response.status_code == 200
        results = response.json()["results"]
        assert results, "Synthetic attribution corpus returned no results"

        r = results[0]
        assert "file_path" in r, "Missing file_path"
        assert "file_name" in r, "Missing file_name"
        assert r["file_name"].endswith(".md"), "File should be markdown"


class TestDateExtraction:
    """Test that dates are extracted from filenames correctly."""

    @pytest.fixture
    def client(self, candidate_base_url):
        """HTTP client for API requests."""
        return httpx.Client(base_url=candidate_base_url, timeout=30.0)

    def test_dated_files_have_correct_date(self, client):
        """Files with dates in filename should have correct modified_date."""
        response = client.post("/api/search", json={
            "query": "daily notes journal",
            "top_k": 20
        })
        assert response.status_code == 200
        results = response.json()["results"]
        dated = [
            r for r in results if r.get("file_name") == SYNTHETIC_DAILY_JOURNAL_RECORD.file_name
        ]
        assert dated, "Synthetic daily journal note was not returned"
        assert dated[0]["modified_date"] == SYNTHETIC_DAILY_JOURNAL_RECORD.modified_date

        for r in results:
            fname = r.get("file_name", "")
            date = r.get("modified_date", "")

            # Check YYYY-MM-DD pattern files
            if fname and len(fname) >= 10:
                # e.g., "2026-03-18.md"
                if fname[:4].isdigit() and fname[4] == "-" and fname[7] == "-":
                    expected_date = fname[:10]
                    assert date == expected_date, \
                        f"Date mismatch for {fname}: expected {expected_date}, got {date}"

    def test_yyyymmdd_pattern_extracted(self, client):
        """The synthetic YYYYMMDD filename should have its exact date extracted."""
        response = client.post("/api/search", json={
            "query": "meeting sync call",
            "top_k": 30
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

            # e.g., "Meeting 20260420.md"
            match = re.search(r"(\d{4})(\d{2})(\d{2})", fname)
            if match:
                expected = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
                year = int(match.group(1))
                if 2000 <= year <= 2100:
                    checked_count += 1
                    # Date should either match exactly or start with expected date
                    if date == expected or date.startswith(expected):
                        extracted_count += 1

        assert checked_count == 1, (
            f"Expected exactly one synthetic YYYYMMDD result, got {checked_count}"
        )
        assert extracted_count == 1, (
            "Synthetic YYYYMMDD filename did not produce its exact modified_date"
        )
