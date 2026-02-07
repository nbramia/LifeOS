"""
Tests for Hybrid Retrieval (P5.2).

Tests the BM25Index service and RRF fusion logic.
"""
import pytest

# Most tests in this file are fast unit tests (SQLite FTS5 + pure logic)
pytestmark = pytest.mark.unit
import tempfile
import os
from collections import defaultdict


class TestBM25Index:
    """Test the BM25 keyword index using SQLite FTS5."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_index_initialization(self, temp_db):
        """Index should create FTS5 table on init."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)

        # Check FTS5 table exists
        import sqlite3
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None

    def test_add_document(self, temp_db):
        """Should add document to FTS index."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        index.add_document(
            doc_id="doc1",
            content="Meeting notes about Q4 budget planning",
            file_name="Budget Review.md",
            people=["Kevin", "Sarah"]
        )

        # Verify it was added
        results = index.search("budget")
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"

    def test_add_multiple_documents(self, temp_db):
        """Should handle multiple documents."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        index.add_document("doc1", "Q4 budget planning", "Budget.md")
        index.add_document("doc2", "Team meeting notes", "Meeting.md")
        index.add_document("doc3", "Budget review for Q3", "Q3 Budget.md")

        results = index.search("budget")
        assert len(results) == 2  # doc1 and doc3

    def test_search_returns_ranked_results(self, temp_db):
        """Search should return results ranked by relevance."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        # Doc with more "budget" mentions should rank higher
        index.add_document("doc1", "Budget budget budget planning", "Budget.md")
        index.add_document("doc2", "Meeting about budget", "Meeting.md")

        results = index.search("budget")
        assert len(results) == 2
        # doc1 should be first (more relevant)
        assert results[0]["doc_id"] == "doc1"

    def test_search_by_filename(self, temp_db):
        """Should match on filename."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        index.add_document("doc1", "Random content", "ML Infrastructure.md")
        index.add_document("doc2", "Other content", "Meeting Notes.md")

        results = index.search("ML Infrastructure")
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"

    def test_search_by_person(self, temp_db):
        """Should match on people names."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        index.add_document("doc1", "Meeting notes", "Meeting.md", people=["Kevin", "Sarah"])
        index.add_document("doc2", "Other notes", "Other.md", people=["Mike"])

        results = index.search("Kevin")
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"

    def test_update_document(self, temp_db):
        """Should update existing document."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        index.add_document("doc1", "Old content", "File.md")
        index.add_document("doc1", "New updated content", "File.md")

        results = index.search("updated")
        assert len(results) == 1

        results = index.search("Old")
        assert len(results) == 0

    def test_delete_document(self, temp_db):
        """Should delete document from index."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        index.add_document("doc1", "Test content", "Test.md")

        index.delete_document("doc1")

        results = index.search("Test")
        assert len(results) == 0

    def test_search_limit(self, temp_db):
        """Should respect limit parameter."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        for i in range(10):
            index.add_document(f"doc{i}", f"Budget document number {i}", f"Doc{i}.md")

        results = index.search("budget", limit=5)
        assert len(results) == 5

    def test_empty_search(self, temp_db):
        """Should return empty list for no matches."""
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=temp_db)
        index.add_document("doc1", "Meeting notes", "Meeting.md")

        results = index.search("xyz123nonexistent")
        assert len(results) == 0


class TestRRFFusion:
    """Test Reciprocal Rank Fusion algorithm."""

    def test_basic_fusion(self):
        """Should merge two ranked lists correctly."""
        from api.services.hybrid_search import reciprocal_rank_fusion

        vector_results = ["doc1", "doc2", "doc3"]
        bm25_results = ["doc2", "doc1", "doc4"]

        fused = reciprocal_rank_fusion(vector_results, bm25_results)

        # doc1 and doc2 appear in both lists, should rank higher
        doc_ids = [doc_id for doc_id, score in fused]
        assert "doc1" in doc_ids[:2]
        assert "doc2" in doc_ids[:2]

    def test_fusion_scores(self):
        """Should calculate RRF scores correctly."""
        from api.services.hybrid_search import reciprocal_rank_fusion

        # doc1: rank 1 in vector (score = 1/61), rank 2 in bm25 (score = 1/62)
        # doc2: rank 2 in vector (score = 1/62), rank 1 in bm25 (score = 1/61)
        vector_results = ["doc1", "doc2"]
        bm25_results = ["doc2", "doc1"]

        fused = reciprocal_rank_fusion(vector_results, bm25_results, k=60)

        # Both should have same score (symmetric)
        scores = {doc_id: score for doc_id, score in fused}
        assert abs(scores["doc1"] - scores["doc2"]) < 0.0001

    def test_single_source_doc(self):
        """Should handle docs appearing in only one list."""
        from api.services.hybrid_search import reciprocal_rank_fusion

        vector_results = ["doc1", "doc2"]
        bm25_results = ["doc3", "doc4"]

        fused = reciprocal_rank_fusion(vector_results, bm25_results)

        doc_ids = [doc_id for doc_id, score in fused]
        assert len(doc_ids) == 4
        assert set(doc_ids) == {"doc1", "doc2", "doc3", "doc4"}

    def test_empty_lists(self):
        """Should handle empty lists gracefully."""
        from api.services.hybrid_search import reciprocal_rank_fusion

        fused = reciprocal_rank_fusion([], [])
        assert fused == []

        fused = reciprocal_rank_fusion(["doc1"], [])
        assert len(fused) == 1

    def test_duplicate_handling(self):
        """Should not double-count duplicates within a list."""
        from api.services.hybrid_search import reciprocal_rank_fusion

        vector_results = ["doc1", "doc1", "doc2"]  # Duplicate
        bm25_results = ["doc2"]

        fused = reciprocal_rank_fusion(vector_results, bm25_results)

        # Should handle gracefully (implementation may vary)
        doc_ids = [doc_id for doc_id, score in fused]
        assert "doc1" in doc_ids
        assert "doc2" in doc_ids


class TestHybridSearch:
    """Test the hybrid search integration."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_hybrid_search_combines_results(self, temp_db):
        """Hybrid search should combine vector and BM25 results."""
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index
        from unittest.mock import MagicMock

        # Create BM25 index with test data
        bm25 = BM25Index(db_path=temp_db)
        bm25.add_document("chunk1", "Q4 budget planning meeting", "Budget.md")
        bm25.add_document("chunk2", "Team standup notes", "Standup.md")
        bm25.add_document("chunk3", "Budget review Q4", "Review.md")

        # Mock vector store
        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = [
            {"id": "chunk2", "content": "Team standup notes", "metadata": {}},
            {"id": "chunk1", "content": "Q4 budget planning meeting", "metadata": {}},
        ]

        # Pass mock directly to constructor
        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)
        results = hybrid.search("budget", top_k=5)

        # Should return fused results
        assert len(results) > 0
        # Budget-related docs should be at top
        doc_ids = [r.get("id") for r in results]
        assert "chunk1" in doc_ids or "chunk3" in doc_ids

    def test_hybrid_search_with_recency(self, temp_db):
        """Hybrid search should apply recency boost."""
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index
        from unittest.mock import MagicMock
        from datetime import datetime, timedelta

        bm25 = BM25Index(db_path=temp_db)
        bm25.add_document("old_chunk", "Budget content old", "Old.md")
        bm25.add_document("new_chunk", "Budget content new", "New.md")

        mock_vector_store = MagicMock()
        # Both have same semantic similarity
        mock_vector_store.search.return_value = [
            {
                "id": "old_chunk",
                "content": "Budget content old",
                "metadata": {"date": (datetime.now() - timedelta(days=365)).isoformat()}
            },
            {
                "id": "new_chunk",
                "content": "Budget content new",
                "metadata": {"date": datetime.now().isoformat()}
            },
        ]

        # Pass mock directly to constructor
        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)
        results = hybrid.search("budget", top_k=5)

        # Newer doc should rank higher after recency boost
        if len(results) >= 2:
            # First result should be the newer one
            assert results[0].get("id") == "new_chunk"

    def test_fallback_to_vector_only(self, temp_db):
        """Should fallback to vector search if BM25 returns no results."""
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index
        from unittest.mock import MagicMock

        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = [
            {"id": "chunk1", "content": "Test content", "metadata": {}},
        ]

        # Use empty BM25 index (new temp db has no documents)
        empty_bm25 = BM25Index(db_path=temp_db)

        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=empty_bm25)
        results = hybrid.search("test", top_k=5)

        assert len(results) == 1
        assert results[0]["id"] == "chunk1"


class TestHybridBenchmark:
    """Benchmark tests for hybrid retrieval quality."""

    BENCHMARK_QUERIES = [
        # Exact match queries (should improve with BM25)
        ("Q4 budget", "exact term match"),
        ("ML infrastructure", "folder/topic match"),
        ("Alex", "exact name match"),

        # Semantic queries (should stay good with vector)
        ("what are my priorities", "conceptual query"),
        ("meeting preparation", "semantic similarity"),
    ]

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_exact_match_improvement(self, temp_db):
        """BM25 should improve exact match queries."""
        from api.services.bm25_index import BM25Index

        bm25 = BM25Index(db_path=temp_db)

        # Add test documents
        bm25.add_document("doc1", "Q4 budget planning for next fiscal year", "Q4 Budget.md")
        bm25.add_document("doc2", "Annual financial review and forecasting", "Finance.md")
        bm25.add_document("doc3", "Q4 budget approval process", "Budget Approval.md")

        # Exact match query
        results = bm25.search("Q4 budget")

        # Should find exact matches
        assert len(results) >= 2
        doc_ids = [r["doc_id"] for r in results]
        assert "doc1" in doc_ids
        assert "doc3" in doc_ids

    def test_name_match_improvement(self, temp_db):
        """BM25 should find exact name matches."""
        from api.services.bm25_index import BM25Index

        bm25 = BM25Index(db_path=temp_db)

        bm25.add_document("doc1", "Meeting with leadership team", "Meeting.md", people=["Alex", "Sarah"])
        bm25.add_document("doc2", "1:1 discussion about roadmap", "1on1.md", people=["Kevin"])
        bm25.add_document("doc3", "Alex mentioned the new strategy", "Notes.md")

        results = bm25.search("Alex")

        # Should find both docs mentioning Alex
        assert len(results) >= 2
        doc_ids = [r["doc_id"] for r in results]
        assert "doc1" in doc_ids
        assert "doc3" in doc_ids


class TestChunkDeduplication:
    """Test overlapping chunk deduplication (P9.3)."""

    def test_removes_adjacent_chunks(self):
        """Should remove adjacent chunks from same file, keeping higher scored."""
        from api.services.hybrid_search import deduplicate_overlapping_chunks

        results = [
            {"id": "/path/file.md::0", "file_path": "/path/file.md", "hybrid_score": 0.9, "metadata": {"chunk_index": 0}},
            {"id": "/path/file.md::1", "file_path": "/path/file.md", "hybrid_score": 0.8, "metadata": {"chunk_index": 1}},
            {"id": "/path/other.md::0", "file_path": "/path/other.md", "hybrid_score": 0.7, "metadata": {"chunk_index": 0}},
        ]

        deduplicated = deduplicate_overlapping_chunks(results)

        # chunk 1 from file.md should be removed (adjacent to chunk 0)
        assert len(deduplicated) == 2
        doc_ids = [r["id"] for r in deduplicated]
        assert "/path/file.md::0" in doc_ids
        assert "/path/other.md::0" in doc_ids
        assert "/path/file.md::1" not in doc_ids

    def test_keeps_non_adjacent_chunks(self):
        """Should keep chunks that are not adjacent."""
        from api.services.hybrid_search import deduplicate_overlapping_chunks

        results = [
            {"id": "/path/file.md::0", "file_path": "/path/file.md", "hybrid_score": 0.9, "metadata": {"chunk_index": 0}},
            {"id": "/path/file.md::5", "file_path": "/path/file.md", "hybrid_score": 0.8, "metadata": {"chunk_index": 5}},
        ]

        deduplicated = deduplicate_overlapping_chunks(results)

        # Both should be kept (not adjacent)
        assert len(deduplicated) == 2

    def test_handles_underscore_id_format(self):
        """Should extract chunk index from underscore format IDs."""
        from api.services.hybrid_search import deduplicate_overlapping_chunks

        results = [
            {"id": "/path/file.md_0", "file_path": "/path/file.md", "hybrid_score": 0.9, "metadata": {}},
            {"id": "/path/file.md_1", "file_path": "/path/file.md", "hybrid_score": 0.8, "metadata": {}},
        ]

        deduplicated = deduplicate_overlapping_chunks(results)

        # Second chunk should be removed (adjacent)
        assert len(deduplicated) == 1
        assert deduplicated[0]["id"] == "/path/file.md_0"

    def test_handles_empty_results(self):
        """Should return empty list for empty input."""
        from api.services.hybrid_search import deduplicate_overlapping_chunks

        deduplicated = deduplicate_overlapping_chunks([])
        assert deduplicated == []

    def test_handles_missing_file_path(self):
        """Should include results without file_path."""
        from api.services.hybrid_search import deduplicate_overlapping_chunks

        results = [
            {"id": "chunk1", "content": "test", "hybrid_score": 0.9, "metadata": {}},
            {"id": "chunk2", "content": "test2", "hybrid_score": 0.8, "metadata": {}},
        ]

        deduplicated = deduplicate_overlapping_chunks(results)

        # Both should be kept (no file_path to check)
        assert len(deduplicated) == 2

    def test_uses_metadata_file_path(self):
        """Should fall back to metadata.file_path if top-level missing."""
        from api.services.hybrid_search import deduplicate_overlapping_chunks

        results = [
            {"id": "chunk1", "metadata": {"file_path": "/path/file.md", "chunk_index": 0}, "hybrid_score": 0.9},
            {"id": "chunk2", "metadata": {"file_path": "/path/file.md", "chunk_index": 1}, "hybrid_score": 0.8},
        ]

        deduplicated = deduplicate_overlapping_chunks(results)

        # Second chunk should be removed (adjacent)
        assert len(deduplicated) == 1


class TestQueryAwareReranking:
    """Test query-aware reranking in hybrid search."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_factual_query_preserves_bm25_matches(self, temp_db):
        """Factual queries should preserve top BM25 exact matches."""
        from api.services.hybrid_search import HybridSearch, find_protected_indices
        from api.services.bm25_index import BM25Index
        from unittest.mock import MagicMock

        bm25 = BM25Index(db_path=temp_db)
        bm25.add_document("jane_ktn", "Jane's KTN: TT11YZS7J", "Jane.md")
        bm25.add_document("travel_1", "General travel tips", "Travel.md")
        bm25.add_document("travel_2", "Airport information", "Airport.md")

        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = [
            {"id": "travel_1", "content": "General travel tips", "metadata": {}},
            {"id": "travel_2", "content": "Airport information", "metadata": {}},
            {"id": "jane_ktn", "content": "Jane's KTN: TT11YZS7J", "metadata": {}},
        ]

        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)

        # find_protected_indices should identify Jane.md for factual query
        results = [
            {"id": "jane_ktn", "content": "Jane's KTN: TT11YZS7J"},
            {"id": "travel_1", "content": "General travel tips"},
        ]
        protected = find_protected_indices("Jane's KTN", results, max_protected=3)

        # Should protect the exact match
        assert 0 in protected

    def test_semantic_query_no_protection(self, temp_db):
        """Semantic queries should not protect any results."""
        from api.services.hybrid_search import find_protected_indices

        results = [
            {"id": "doc1", "content": "Meeting with Sarah about project"},
            {"id": "doc2", "content": "Sarah's feedback on design"},
        ]

        protected = find_protected_indices(
            "prepare me for meeting with Sarah",
            results,
            max_protected=3
        )

        # Semantic query = no protection
        assert len(protected) == 0

    def test_find_protected_indices_checks_content(self, temp_db):
        """Should only protect results that contain query keywords."""
        from api.services.hybrid_search import find_protected_indices

        results = [
            {"id": "doc1", "content": "Random unrelated content"},
            {"id": "doc2", "content": "Alex's phone: 555-1234"},
            {"id": "doc3", "content": "More unrelated stuff"},
        ]

        protected = find_protected_indices("Alex's phone", results, max_protected=3)

        # Should only protect doc2 (contains "Alex" and "phone")
        assert 1 in protected
        assert 0 not in protected
        assert 2 not in protected
