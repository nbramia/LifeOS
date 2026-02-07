"""
Tests for embedding generation using sentence-transformers.

NOTE: Imports are deferred to avoid loading sentence-transformers during
pytest collection, which would slow down unit test runs.
"""
import pytest

# These tests require loading the sentence-transformers model (slow)
pytestmark = pytest.mark.slow


class TestEmbeddingService:
    """Test embedding generation."""

    @pytest.fixture
    def embedding_service(self):
        """Create embedding service instance."""
        from api.services.embeddings import get_embedding_service
        return get_embedding_service()

    def test_generates_embeddings(self, embedding_service):
        """Should generate embeddings for text."""
        text = "This is a test sentence about meeting notes."
        embedding = embedding_service.embed_text(text)

        assert embedding is not None
        assert len(embedding) > 0
        # MiniLM produces 384-dimensional embeddings
        assert len(embedding) == 384

    def test_generates_batch_embeddings(self, embedding_service):
        """Should generate embeddings for multiple texts."""
        texts = [
            "First document about budget planning.",
            "Second document about team meetings.",
            "Third document about project timelines.",
        ]
        embeddings = embedding_service.embed_texts(texts)

        assert len(embeddings) == 3
        for emb in embeddings:
            assert len(emb) == 384

    def test_similar_texts_have_similar_embeddings(self, embedding_service):
        """Semantically similar texts should have higher cosine similarity."""
        import numpy as np

        text1 = "We discussed the quarterly budget."
        text2 = "The meeting covered financial planning for Q1."
        text3 = "My cat likes to sleep on the couch."

        emb1 = np.array(embedding_service.embed_text(text1))
        emb2 = np.array(embedding_service.embed_text(text2))
        emb3 = np.array(embedding_service.embed_text(text3))

        # Cosine similarity
        def cosine_sim(a, b):
            return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

        sim_12 = cosine_sim(emb1, emb2)  # Related (budget/financial)
        sim_13 = cosine_sim(emb1, emb3)  # Unrelated

        # Similar texts should have higher similarity
        assert sim_12 > sim_13

    def test_handles_empty_text(self, embedding_service):
        """Should handle empty text gracefully."""
        embedding = embedding_service.embed_text("")
        assert embedding is not None
        assert len(embedding) == 384

    def test_handles_long_text(self, embedding_service):
        """Should handle long text (model truncates if needed)."""
        long_text = "This is a test sentence. " * 500
        embedding = embedding_service.embed_text(long_text)

        assert embedding is not None
        assert len(embedding) == 384

    def test_consistent_embeddings(self, embedding_service):
        """Same text should produce same embedding."""
        import numpy as np

        text = "Consistent embedding test."
        emb1 = embedding_service.embed_text(text)
        emb2 = embedding_service.embed_text(text)

        np.testing.assert_array_almost_equal(emb1, emb2)
