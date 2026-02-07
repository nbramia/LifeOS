"""
Tests for Cross-Encoder Re-ranking (P9.2).

Tests the RerankerService for re-ranking search results.
"""
import pytest

# Most tests in this file are fast unit tests
pytestmark = pytest.mark.unit


class TestRerankerService:
    """Test the RerankerService class."""

    def test_rerank_empty_results(self):
        """Should return empty list for empty input."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()
        result = reranker.rerank(query="test", results=[], top_k=10)
        assert result == []

    def test_rerank_fewer_than_top_k(self):
        """Should return all results when fewer than top_k."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()
        results = [
            {"content": "First result", "hybrid_score": 0.9},
            {"content": "Second result", "hybrid_score": 0.8},
        ]

        reranked = reranker.rerank(query="test", results=results, top_k=10)

        # Should return all results with cross_encoder_score added
        assert len(reranked) == 2
        for r in reranked:
            assert "cross_encoder_score" in r

    def test_rerank_uses_hybrid_score_fallback(self):
        """Should use hybrid_score as fallback when not enough results."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()
        results = [
            {"content": "Result 1", "hybrid_score": 0.9},
            {"content": "Result 2", "hybrid_score": 0.7},
        ]

        reranked = reranker.rerank(query="test", results=results, top_k=10)

        # cross_encoder_score should equal hybrid_score
        assert reranked[0]["cross_encoder_score"] == 0.9
        assert reranked[1]["cross_encoder_score"] == 0.7

    def test_is_model_loaded_initially_false(self):
        """Model should not be loaded initially."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()
        assert reranker.is_model_loaded() is False

    def test_custom_model_name(self):
        """Should accept custom model name."""
        from api.services.reranker import RerankerService

        reranker = RerankerService(model_name="cross-encoder/ms-marco-MiniLM-L12-v2")
        assert reranker.model_name == "cross-encoder/ms-marco-MiniLM-L12-v2"


class TestRerankerSingleton:
    """Test the get_reranker singleton."""

    def test_get_reranker_returns_instance(self):
        """Should return a RerankerService instance."""
        from api.services.reranker import get_reranker, RerankerService

        reranker = get_reranker()
        assert isinstance(reranker, RerankerService)

    def test_get_reranker_is_singleton(self):
        """Should return same instance on multiple calls."""
        from api.services.reranker import get_reranker

        reranker1 = get_reranker()
        reranker2 = get_reranker()
        assert reranker1 is reranker2


class TestRerankerIntegration:
    """Integration tests for reranker with actual model loading."""

    @pytest.mark.slow
    def test_rerank_with_model(self):
        """Should actually rerank results using the model."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()

        # Create test results with varying relevance
        results = [
            {"content": "The quick brown fox jumps over the lazy dog", "hybrid_score": 0.9},
            {"content": "What is the phone number for Alex?", "hybrid_score": 0.8},
            {"content": "Alex's phone number is 555-1234", "hybrid_score": 0.7},
            {"content": "Random unrelated content about weather", "hybrid_score": 0.6},
            {"content": "Contact information and phone details", "hybrid_score": 0.5},
        ]

        reranked = reranker.rerank(
            query="What is Alex's phone number?",
            results=results,
            top_k=3
        )

        # Should have 3 results
        assert len(reranked) == 3

        # All should have cross_encoder_score
        for r in reranked:
            assert "cross_encoder_score" in r
            assert isinstance(r["cross_encoder_score"], float)

        # The result with "Alex's phone number is 555-1234" should rank highly
        top_contents = [r["content"] for r in reranked]
        assert "Alex's phone number is 555-1234" in top_contents

        # Model should now be loaded
        assert reranker.is_model_loaded() is True

    @pytest.mark.slow
    def test_rerank_preserves_other_fields(self):
        """Should preserve all fields from original results."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()

        results = [
            {"id": "doc1", "content": "Test content 1", "file_path": "/path/1", "hybrid_score": 0.9},
            {"id": "doc2", "content": "Test content 2", "file_path": "/path/2", "hybrid_score": 0.8},
            {"id": "doc3", "content": "Test content 3", "file_path": "/path/3", "hybrid_score": 0.7},
        ]

        reranked = reranker.rerank(query="test", results=results, top_k=2)

        # All original fields should be preserved
        for r in reranked:
            assert "id" in r
            assert "file_path" in r
            assert "hybrid_score" in r
            assert "cross_encoder_score" in r


class TestHybridSearchWithReranker:
    """Test hybrid search integration with reranker."""

    def test_search_with_reranker_disabled(self):
        """Should skip reranking when use_reranker=False."""
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index
        from unittest.mock import MagicMock
        import tempfile
        import os

        # Create temp DB for BM25
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            temp_db = f.name

        try:
            bm25 = BM25Index(db_path=temp_db)
            bm25.add_document("doc1", "Budget planning content", "Budget.md")
            bm25.add_document("doc2", "Meeting notes content", "Meeting.md")

            mock_vector_store = MagicMock()
            mock_vector_store.search.return_value = [
                {"id": "doc1", "content": "Budget planning content", "metadata": {}},
            ]

            hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)
            results = hybrid.search("budget", top_k=5, use_reranker=False)

            # Should return results without cross_encoder_score
            assert len(results) > 0
            # cross_encoder_score should not be present when reranker disabled
            assert "cross_encoder_score" not in results[0]

        finally:
            os.unlink(temp_db)

    def test_search_accepts_reranker_parameters(self):
        """Should accept use_reranker and rerank_candidates parameters."""
        from api.services.hybrid_search import HybridSearch
        from unittest.mock import MagicMock, patch

        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = []

        # Create mock BM25 that returns empty results
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = []

        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=mock_bm25)

        # Should not raise - parameters are accepted
        results = hybrid.search(
            "test query",
            top_k=10,
            use_reranker=True,
            rerank_candidates=50
        )
        assert isinstance(results, list)


class TestProtectedReranking:
    """Test protected reranking for factual queries."""

    @pytest.mark.slow
    def test_rerank_with_protected_indices(self):
        """Should preserve results at protected indices."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()

        results = [
            {"id": "exact_match", "content": "Jane's KTN: JD12ABC34", "hybrid_score": 0.9},
            {"id": "semantic_1", "content": "General travel information", "hybrid_score": 0.8},
            {"id": "semantic_2", "content": "Passport and visa requirements", "hybrid_score": 0.7},
            {"id": "semantic_3", "content": "Airport security guidelines", "hybrid_score": 0.6},
        ]

        # Protect index 0 (the exact match)
        reranked = reranker.rerank(
            query="Jane's KTN",
            results=results,
            top_k=3,
            protected_indices=[0]
        )

        # Protected result should be first
        assert reranked[0]["id"] == "exact_match"
        assert len(reranked) == 3

    @pytest.mark.slow
    def test_rerank_protected_multiple(self):
        """Should preserve multiple protected results in order."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()

        results = [
            {"id": "match_1", "content": "Alex phone: 555-1234", "hybrid_score": 0.9},
            {"id": "match_2", "content": "Alex email: alex@example.com", "hybrid_score": 0.85},
            {"id": "unrelated_1", "content": "Random content", "hybrid_score": 0.8},
            {"id": "unrelated_2", "content": "More random content", "hybrid_score": 0.7},
        ]

        reranked = reranker.rerank(
            query="Alex contact info",
            results=results,
            top_k=3,
            protected_indices=[0, 1]
        )

        # First two should be protected results in order
        assert reranked[0]["id"] == "match_1"
        assert reranked[1]["id"] == "match_2"
        assert len(reranked) == 3

    def test_rerank_no_protection_full_rerank(self):
        """Without protection, should fully rerank."""
        from api.services.reranker import RerankerService

        reranker = RerankerService()

        results = [
            {"id": "doc1", "content": "Budget overview", "hybrid_score": 0.9},
            {"id": "doc2", "content": "Financial planning details", "hybrid_score": 0.8},
        ]

        # No protected_indices = full rerank
        reranked = reranker.rerank(
            query="test",
            results=results,
            top_k=2
        )

        # Should work normally
        assert len(reranked) == 2

    def test_protected_indices_unit(self):
        """Unit test: Should preserve results at protected indices (mocked model)."""
        from api.services.reranker import RerankerService
        from unittest.mock import MagicMock, patch

        reranker = RerankerService()
        mock_model = MagicMock()
        mock_model.predict.return_value = [0.5, 0.4, 0.3]

        results = [
            {"id": "exact_match", "content": "Jane's KTN: JD12ABC34", "hybrid_score": 0.9},
            {"id": "semantic_1", "content": "General travel information", "hybrid_score": 0.8},
            {"id": "semantic_2", "content": "Passport and visa requirements", "hybrid_score": 0.7},
            {"id": "semantic_3", "content": "Airport security guidelines", "hybrid_score": 0.6},
        ]

        with patch.object(reranker, '_get_model', return_value=mock_model):
            reranked = reranker.rerank(
                query="Jane's KTN",
                results=results,
                top_k=3,
                protected_indices=[0]
            )

        # Protected result should be first and marked
        assert reranked[0]["id"] == "exact_match"
        assert reranked[0].get("protected") is True
        assert len(reranked) == 3

    def test_protected_multiple_unit(self):
        """Unit test: Should preserve multiple protected results in order (mocked model)."""
        from api.services.reranker import RerankerService
        from unittest.mock import MagicMock, patch

        reranker = RerankerService()
        mock_model = MagicMock()
        mock_model.predict.return_value = [0.5, 0.4]

        results = [
            {"id": "match_1", "content": "Alex phone: 555-1234", "hybrid_score": 0.9},
            {"id": "match_2", "content": "Alex email: alex@example.com", "hybrid_score": 0.85},
            {"id": "unrelated_1", "content": "Random content", "hybrid_score": 0.8},
            {"id": "unrelated_2", "content": "More random content", "hybrid_score": 0.7},
        ]

        with patch.object(reranker, '_get_model', return_value=mock_model):
            reranked = reranker.rerank(
                query="Alex contact info",
                results=results,
                top_k=3,
                protected_indices=[0, 1]
            )

        # First two should be protected results in order
        assert reranked[0]["id"] == "match_1"
        assert reranked[0].get("protected") is True
        assert reranked[1]["id"] == "match_2"
        assert reranked[1].get("protected") is True
        assert len(reranked) == 3
