"""
Integration tests for Phase 9 retrieval improvements.
Tests all four improvements working together.

P9.1: Contextual Chunking
P9.2: Cross-Encoder Re-ranking
P9.3: Overlapping Chunks
P9.4: Document Summaries
"""
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


@pytest.mark.integration
class TestPhase9Integration:
    """Integration tests for all Phase 9 improvements."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_contextual_chunking_adds_context(self):
        """Verify contextual chunks have document context prepended."""
        from api.services.chunker import (
            chunk_document,
            add_context_to_chunks,
        )

        content = """# Meeting with Kevin

## Agenda
- Budget review
- Q1 planning

## Action Items
- Kevin: Send proposal
"""
        chunks = chunk_document(content, is_granola=True)

        # Add context
        metadata = {"note_type": "Granola", "people": ["Kevin"]}
        contextualized = add_context_to_chunks(
            chunks,
            Path("/vault/Granola/Meeting with Kevin.md"),
            metadata
        )

        # All chunks should have context
        for chunk in contextualized:
            assert chunk.get("has_context") is True
            assert "This chunk is from" in chunk["content"]
            assert "Meeting with Kevin.md" in chunk["content"]

    def test_hybrid_search_with_reranker_integration(self, temp_db):
        """Verify hybrid search integrates with reranker."""
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index

        # Create BM25 index with test data
        bm25 = BM25Index(db_path=temp_db)
        bm25.add_document("chunk1", "Q4 budget planning with Kevin", "Budget.md")
        bm25.add_document("chunk2", "Meeting notes from standup", "Standup.md")
        bm25.add_document("chunk3", "Kevin's phone: 555-1234", "Kevin.md")

        # Mock vector store
        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = [
            {"id": "chunk1", "content": "Q4 budget planning with Kevin", "metadata": {}},
            {"id": "chunk3", "content": "Kevin's phone: 555-1234", "metadata": {}},
        ]

        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)

        # Search with reranker disabled (to avoid loading model in tests)
        results = hybrid.search("Kevin's phone", top_k=3, use_reranker=False)

        # Should return results
        assert len(results) > 0

        # Search should accept reranker params
        results = hybrid.search(
            "budget",
            top_k=3,
            use_reranker=True,
            rerank_candidates=10
        )
        assert isinstance(results, list)

    def test_overlapping_chunks_deduplication(self):
        """Verify overlapping chunks are deduplicated in results."""
        from api.services.hybrid_search import deduplicate_overlapping_chunks

        # Simulate results with adjacent chunks from same file
        results = [
            {"id": "/path/budget.md::0", "file_path": "/path/budget.md", "hybrid_score": 0.9, "metadata": {"chunk_index": 0}},
            {"id": "/path/budget.md::1", "file_path": "/path/budget.md", "hybrid_score": 0.85, "metadata": {"chunk_index": 1}},
            {"id": "/path/meeting.md::0", "file_path": "/path/meeting.md", "hybrid_score": 0.8, "metadata": {"chunk_index": 0}},
            {"id": "/path/budget.md::2", "file_path": "/path/budget.md", "hybrid_score": 0.7, "metadata": {"chunk_index": 2}},
        ]

        deduplicated = deduplicate_overlapping_chunks(results)

        # Deduplication removes chunks adjacent to already-kept chunks
        # - Chunk 0 from budget.md is kept (highest score)
        # - Chunk 1 from budget.md is skipped (adjacent to 0)
        # - Chunk 0 from meeting.md is kept
        # - Chunk 2 from budget.md is kept (not adjacent to any kept chunk - only adjacent to skipped chunk 1)
        assert len(deduplicated) == 3

        file_paths = [r["file_path"] for r in deduplicated]
        assert "/path/meeting.md" in file_paths
        assert "/path/budget.md" in file_paths

        # Chunk 1 should be removed (adjacent to chunk 0)
        chunk_ids = [r["id"] for r in deduplicated]
        assert "/path/budget.md::1" not in chunk_ids

    def test_document_summary_generation(self):
        """Verify document summaries are generated correctly."""
        from api.services.summarizer import generate_summary, _fallback_summary

        # Test fallback summary (no Ollama needed)
        content = """# Budget Review Meeting

Meeting with Kevin and Sarah to discuss Q4 projections.

## Key Points
- Revenue up 15%
- Expenses under control
"""
        fallback = _fallback_summary(content, "Budget Review.md")

        # Should contain file name and first content line
        assert "Budget Review.md" in fallback
        assert "Kevin" in fallback or "Sarah" in fallback or "Q4" in fallback

    def test_chunk_overlap_setting(self):
        """Verify chunk overlap is configured at 20%."""
        from config.settings import settings

        # 20% of 500 tokens = 100 tokens
        assert settings.chunk_overlap == 100
        assert settings.chunk_size == 500

    def test_reranker_settings(self):
        """Verify reranker settings are configured."""
        from config.settings import settings

        assert settings.reranker_model == "cross-encoder/ms-marco-MiniLM-L6-v2"
        # Reranker enabled with query-aware protection for factual queries
        assert settings.reranker_enabled is True
        assert settings.reranker_candidates == 50

    def test_query_classifier_integration(self):
        """Verify query classifier works with hybrid search."""
        from api.services.query_classifier import classify_query
        from api.services.hybrid_search import find_protected_indices

        # Factual query should be classified correctly
        assert classify_query("Jane's KTN") == "factual"

        # And should result in protection
        results = [{"content": "Jane's KTN: JD12ABC34"}]
        protected = find_protected_indices("Jane's KTN", results)
        assert len(protected) > 0

    def test_all_services_can_be_imported(self):
        """Verify all Phase 9 services can be imported."""
        # This tests that there are no import errors

        # P9.1 - Contextual Chunking
        from api.services.chunker import (
            generate_chunk_context,
            add_context_to_chunks,
            _infer_topic,
        )

        # P9.2 - Cross-Encoder Re-ranking
        from api.services.reranker import (
            RerankerService,
            get_reranker,
        )

        # P9.3 - Overlapping Chunks (deduplication)
        from api.services.hybrid_search import (
            deduplicate_overlapping_chunks,
        )

        # P9.4 - Document Summaries
        from api.services.summarizer import (
            generate_summary,
            is_ollama_available,
            create_summary_chunk,
            _fallback_summary,
        )

        # All imports succeeded
        assert True


@pytest.mark.integration
class TestPhase9Benchmark:
    """Benchmark tests for Phase 9 retrieval quality."""

    # Sample benchmark queries
    BENCHMARK_QUERIES = [
        # Person queries - expect person file in top results
        ("Jane's passport", "Jane", "People file lookup"),
        ("Alex's phone number", "Alex", "Contact info lookup"),

        # Topic queries
        ("Q4 budget", "budget", "Topic search"),

        # Discovery queries
        ("which file has travel", "travel", "Discovery search"),
    ]

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_benchmark_query_format(self, temp_db):
        """Verify benchmark can be run (format test)."""
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index

        # Create minimal test data
        bm25 = BM25Index(db_path=temp_db)
        bm25.add_document("jane1", "Jane's passport: ABC123", "Jane.md")
        bm25.add_document("alex1", "Alex's phone: 555-1234", "Alex.md")

        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = [
            {"id": "jane1", "content": "Jane's passport: ABC123", "file_name": "Jane.md", "metadata": {}},
        ]

        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)

        # Run sample query
        results = hybrid.search("Jane's passport", top_k=5, use_reranker=False)

        # Verify we get results
        assert len(results) > 0

        # Verify result format
        for result in results:
            assert "id" in result or "content" in result


@pytest.mark.integration
class TestPhase9EndToEnd:
    """End-to-end tests for the complete retrieval pipeline."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_full_pipeline_person_query(self, temp_db):
        """Test full pipeline for person query."""
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index
        from api.services.chunker import add_context_to_chunks, chunk_document

        # 1. Create content with context
        content = "# Kevin Smith\n\nPhone: 555-1234\nEmail: kevin@example.com"
        chunks = chunk_document(content, is_granola=False)
        chunks = add_context_to_chunks(
            chunks,
            Path("/vault/People/Kevin Smith.md"),
            {"note_type": "Personal", "people": ["Kevin Smith"]}
        )

        # 2. Index in BM25
        bm25 = BM25Index(db_path=temp_db)
        for i, chunk in enumerate(chunks):
            bm25.add_document(
                f"kevin_{i}",
                chunk["content"],
                "Kevin Smith.md",
                people=["Kevin Smith"]
            )

        # 3. Mock vector store with contextualized content
        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = [
            {
                "id": "kevin_0",
                "content": chunks[0]["content"],
                "file_name": "Kevin Smith.md",
                "metadata": {"file_path": "/vault/People/Kevin Smith.md"}
            },
        ]

        # 4. Search
        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)
        results = hybrid.search("Kevin's phone number", top_k=5, use_reranker=False)

        # 5. Verify results
        assert len(results) > 0
        assert "Kevin Smith.md" in results[0].get("file_name", "") or "Kevin" in results[0].get("content", "")

    def test_search_latency_acceptable(self, temp_db):
        """Verify search completes in acceptable time."""
        import time
        from api.services.hybrid_search import HybridSearch
        from api.services.bm25_index import BM25Index

        # Create BM25 index with test data
        bm25 = BM25Index(db_path=temp_db)
        for i in range(100):
            bm25.add_document(f"doc{i}", f"Test content {i} about various topics", f"Doc{i}.md")

        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = []

        hybrid = HybridSearch(vector_store=mock_vector_store, bm25_index=bm25)

        # Time the search (without reranker to get base latency)
        start = time.time()
        results = hybrid.search("test content", top_k=10, use_reranker=False)
        elapsed = time.time() - start

        # Should complete in under 500ms for 100 docs
        assert elapsed < 0.5, f"Search took {elapsed:.2f}s, expected <0.5s"
