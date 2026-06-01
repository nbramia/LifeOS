"""
Embedding service using sentence-transformers.

Uses model configured via settings.embedding_model for local embedding generation.
Model files are cached at settings.embedding_cache_dir to save internal disk space.

NOTE: sentence_transformers is imported lazily to avoid slow startup.
This allows tests to import this module without loading the ML library.
"""
import logging
import os
from typing import TYPE_CHECKING, Any

from config.settings import settings

logger = logging.getLogger(__name__)

# GPU error keywords — shared between load-time and encode-time fallback
_GPU_ERROR_KEYWORDS = ("hip", "cuda", "out of memory", "invalid device", "gpu hang")

# Network error keywords — model load tries to check HF for updates even when
# the snapshot is already cached. We retry offline if any of these fire.
_NETWORK_ERROR_KEYWORDS = (
    "huggingface.co",
    "max retries exceeded",
    "name or service not known",
    "temporary failure in name resolution",
    "connection refused",
    "connection timed out",
    "connection reset",
    "name resolution",
    "getaddrinfo",
    "dns",
)


def _looks_like_network_error(exc: BaseException) -> bool:
    """Return True if the exception looks like a transient network failure."""
    msg = str(exc).lower()
    if any(kw in msg for kw in _NETWORK_ERROR_KEYWORDS):
        return True
    # huggingface_hub raises specific subclasses for offline / network issues;
    # fall back to module name matching to avoid importing it at module load.
    cls_module = type(exc).__module__ or ""
    if cls_module.startswith("huggingface_hub") or cls_module.startswith("requests"):
        return True
    return False

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
        self._force_cpu: bool = False

    @property
    def model(self) -> "SentenceTransformer":
        """Lazy-load the model, falling back to CPU if GPU fails."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            try:
                import torch
                load_kwargs = dict(
                    model_name_or_path=self.model_name,
                    cache_folder=self.cache_dir,
                    model_kwargs={"dtype": torch.float16},
                    local_files_only=True,
                )
                if self._force_cpu:
                    load_kwargs["device"] = "cpu"

                self._model = self._load_with_offline_retry(SentenceTransformer, load_kwargs)
                from api.services.service_health import mark_service_healthy
                mark_service_healthy("embedding_model")
            except RuntimeError as e:
                # GPU errors (HIP OOM, GPU hang, invalid device) — fall back to CPU
                error_msg = str(e).lower()
                if any(kw in error_msg for kw in _GPU_ERROR_KEYWORDS):
                    logger.warning(f"GPU embedding load failed ({e}), falling back to CPU")
                    try:
                        cpu_kwargs = dict(
                            model_name_or_path=self.model_name,
                            cache_folder=self.cache_dir,
                            device="cpu",
                            local_files_only=True,
                        )
                        self._model = self._load_with_offline_retry(SentenceTransformer, cpu_kwargs)
                        self._force_cpu = True
                        from api.services.service_health import mark_service_healthy
                        mark_service_healthy("embedding_model")
                    except Exception as cpu_err:
                        from api.services.service_health import mark_service_failed, Severity
                        mark_service_failed("embedding_model", f"CPU fallback also failed: {cpu_err}", Severity.CRITICAL)
                        raise
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

    def _load_with_offline_retry(self, st_cls, load_kwargs: dict):
        """Load the SentenceTransformer, retrying with HF_HUB_OFFLINE=1 on network errors.

        Sentence-transformers contacts HuggingFace at startup to check for
        updates even when the snapshot is already cached locally. Past
        outages — including the 2026-05-23 DNS retry storm that took down the
        whole vault reindex — bubble up as ConnectionError / DNS failures and
        kill the load. When the model is already cached on disk, the right
        behaviour is to skip the network probe entirely.

        Thread safety: mutates ``os.environ`` for the duration of the retry.
        ``EmbeddingService`` is a process-wide singleton loaded lazily on
        first access (typically the background sync worker), so concurrent
        loads don't happen in practice. The try/finally restores the env
        before any other code can observe it. If a future caller starts
        triggering loads from multiple threads at once, wrap this in a lock.
        """
        try:
            return st_cls(**load_kwargs)
        except Exception as exc:
            if not _looks_like_network_error(exc):
                raise
            logger.warning(
                f"Embedding model load failed with network error ({exc.__class__.__name__}: {exc}). "
                "Retrying with HF_HUB_OFFLINE=1 to skip the upstream version check."
            )
            prev_offline = os.environ.get("HF_HUB_OFFLINE")
            prev_tf_offline = os.environ.get("TRANSFORMERS_OFFLINE")
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            try:
                model = st_cls(**load_kwargs)
            finally:
                # Restore env so other components keep their previous behaviour.
                if prev_offline is None:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = prev_offline
                if prev_tf_offline is None:
                    os.environ.pop("TRANSFORMERS_OFFLINE", None)
                else:
                    os.environ["TRANSFORMERS_OFFLINE"] = prev_tf_offline
            try:
                from api.services.service_health import record_degradation
                record_degradation(
                    "embedding_model", "load", "offline_cache",
                    f"HF network unreachable: {exc}",
                )
            except Exception:
                pass
            return model

    def _encode_with_fallback(self, data, **kwargs):
        """Encode with GPU→CPU fallback on RuntimeError.

        If the model was loaded on GPU and encode() raises a GPU error,
        the model is reloaded on CPU and the encode is retried.
        """
        try:
            return self.model.encode(data, **kwargs)
        except RuntimeError as e:
            error_msg = str(e).lower()
            if self._force_cpu or not any(kw in error_msg for kw in _GPU_ERROR_KEYWORDS):
                raise  # Already on CPU or not a GPU error

            logger.warning(f"GPU encode failed ({e}), reloading model on CPU")
            from api.services.service_health import record_degradation
            record_degradation(
                "embedding_gpu", "encode", "cpu_fallback",
                f"GPU encode error: {e}",
            )
            self._model = None
            self._force_cpu = True
            return self.model.encode(data, **kwargs)

    def embed_text(self, text: str) -> list[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            List of floats representing the embedding vector
        """
        embedding = self._encode_with_fallback(text, convert_to_numpy=True)
        return embedding.tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        embeddings = self._encode_with_fallback(texts, convert_to_numpy=True)
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
