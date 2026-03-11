"""
Embedding service using sentence-transformers.

Uses model configured via settings.embedding_model for local embedding generation.
Model files are cached at settings.embedding_cache_dir to save internal disk space.

NOTE: sentence_transformers is imported lazily to avoid slow startup.
This allows tests to import this module without loading the ML library.
"""
from typing import TYPE_CHECKING, Any

from config.settings import settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


# Model dimension lookup (for known models)
MODEL_DIMENSIONS = {
    "all-MiniLM-L6-v2": 384,
    "all-mpnet-base-v2": 768,
    "Alibaba-NLP/gte-Qwen2-1.5B-instruct": 1536,
    "BAAI/bge-large-en-v1.5": 1024,
    "mixedbread-ai/mxbai-embed-large-v1": 1024,
}


class EmbeddingService:
    """Service for generating text embeddings."""

    def __init__(self, model_name: str = None, cache_dir: str = None):
        """
        Initialize embedding service.

        Args:
            model_name: Name of the sentence-transformers model to use.
            cache_dir: Directory to cache model files (defaults to settings).
        """
        self.model_name = model_name or settings.embedding_model
        self.cache_dir = cache_dir or getattr(settings, 'embedding_cache_dir', None) or None
        self._model: Any = None

    @property
    def model(self) -> "SentenceTransformer":
        """Lazy-load the model, falling back to CPU if GPU fails."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            import logging

            logger = logging.getLogger(__name__)

            try:
                # Load model with cache directory (defaults to GPU if available)
                self._model = SentenceTransformer(
                    self.model_name,
                    cache_folder=self.cache_dir,
                )
                # Mark embedding model as healthy
                from api.services.service_health import mark_service_healthy
                mark_service_healthy("embedding_model")
            except RuntimeError as e:
                # GPU errors (HIP OOM, GPU hang, invalid device) — fall back to CPU
                error_msg = str(e).lower()
                gpu_errors = ("hip", "cuda", "out of memory", "device", "gpu hang")
                if any(keyword in error_msg for keyword in gpu_errors):
                    logger.warning(f"GPU embedding failed ({e}), falling back to CPU")
                    self._model = SentenceTransformer(
                        self.model_name,
                        cache_folder=self.cache_dir,
                        device="cpu",
                    )
                    from api.services.service_health import mark_service_healthy
                    mark_service_healthy("embedding_model")
                else:
                    from api.services.service_health import mark_service_failed, Severity
                    mark_service_failed("embedding_model", str(e), Severity.CRITICAL)
                    raise
            except Exception as e:
                # Non-GPU errors — no fallback
                from api.services.service_health import mark_service_failed, Severity
                mark_service_failed("embedding_model", str(e), Severity.CRITICAL)
                raise
        return self._model

    def embed_text(self, text: str) -> list[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            List of floats representing the embedding vector
        """
        embedding = self.model.encode(text, convert_to_numpy=True)
        return embedding.tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        embeddings = self.model.encode(texts, convert_to_numpy=True)
        return [emb.tolist() for emb in embeddings]

    @property
    def embedding_dimension(self) -> int:
        """Return the dimension of embeddings produced by this model."""
        if self.model_name in MODEL_DIMENSIONS:
            return MODEL_DIMENSIONS[self.model_name]
        # Fallback: query the model (requires loading it)
        return self.model.get_sentence_embedding_dimension()


# Singleton instance
_embedding_service: EmbeddingService | None = None


def get_embedding_service(model_name: str = None) -> EmbeddingService:
    """
    Get or create the embedding service singleton.

    Args:
        model_name: Model to use (only used on first call, defaults to settings)

    Returns:
        EmbeddingService instance
    """
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService(model_name)
    return _embedding_service


def reset_embedding_service() -> None:
    """
    Reset the embedding service singleton.

    For testing only - allows tests to start with fresh state.
    WARNING: This causes model to reload on next use (~2s).
    """
    global _embedding_service
    _embedding_service = None
