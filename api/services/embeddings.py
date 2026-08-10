"""
Embedding service using sentence-transformers.

Uses model configured via settings.embedding_model for local embedding generation.
Model files are cached at settings.embedding_cache_dir to save internal disk space.

NOTE: sentence_transformers is imported lazily to avoid slow startup.
This allows tests to import this module without loading the ML library.
"""
import errno
import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from config.settings import settings

logger = logging.getLogger(__name__)

# GPU error keywords — shared between load-time and encode-time fallback
_GPU_ERROR_KEYWORDS = ("hip", "cuda", "out of memory", "invalid device", "gpu hang")

# Outcomes of _acquire_gpu_lock(). See its docstring (#521).
_LOCK_ACQUIRED = "acquired"
_LOCK_DISABLED = "disabled"
_LOCK_ERROR = "error"
_LOCK_TIMEOUT = "timeout"

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
        # Serialize lazy loads. Searches run through execute_tool_parallel →
        # asyncio.to_thread, so concurrent first-use from multiple tool threads
        # would otherwise race the (meta-device) model init and leave params on
        # the meta device — surfacing as "Cannot copy out of meta tensor" when
        # SentenceTransformer.__init__ calls self.to(device). Loading once under
        # a lock removes the race.
        self._load_lock = threading.Lock()

    @property
    def model(self) -> "SentenceTransformer":
        """Lazy-load the model, falling back to CPU if GPU fails."""
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._load_model()
        return self._model

    def _load_model(self) -> None:
        """Construct the SentenceTransformer (caller holds ``self._load_lock``)."""
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

    def _load_with_offline_retry(self, st_cls, load_kwargs: dict):
        """Load the SentenceTransformer, retrying with HF_HUB_OFFLINE=1 on network errors.

        Sentence-transformers contacts HuggingFace at startup to check for
        updates even when the snapshot is already cached locally. Past
        outages — including the 2026-05-23 DNS retry storm that took down the
        whole vault reindex — bubble up as ConnectionError / DNS failures and
        kill the load. When the model is already cached on disk, the right
        behaviour is to skip the network probe entirely.

        Thread safety: mutates ``os.environ`` for the duration of the retry.
        This runs inside ``EmbeddingService._load_lock`` (the ``model`` property
        serializes lazy loads), so the env swap can't race a concurrent load,
        and the try/finally restores it before the lock is released.
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

    @contextmanager
    def _acquire_gpu_lock(self):
        """Cross-process lock serializing GPU embedding across all LifeOS
        processes (API server, agent worker, nightly sync, ad-hoc scripts) (#521).

        This host's iGPU has only 8 SDMA queues. If several processes each try
        to grab GPU compute queues at the same time (e.g. the API server and a
        manual reindex both embedding on GPU), the kernel logs "No more SDMA
        queue to allocate" and the amdgpu driver has, on this host, wedged into
        an unrecoverable freeze requiring a hard reboot — the incident #483's
        per-call batch-size cap only partially addressed (that cap bounds the
        size of one call; it does nothing about multiple concurrent callers).

        Implementation: ``fcntl.flock`` on a well-known file
        (``settings.embedding_gpu_lock_path``), not a named semaphore, a
        lockfile-with-PID, or a DB row. flock is the right tool here because:
          - It is released automatically by the kernel if the holding process
            dies or is killed (crash, OOM-kill, SIGKILL) — no staleness
            detection or cleanup logic is needed, unlike a PID file or a lock
            row in SQLite that a dead writer can leave held forever.
          - It needs no extra infrastructure: no server, no port, no schema —
            just a file, which fits a single-host coordination problem.
          - It works across independent processes (unlike ``threading.Lock``,
            which only serializes within one process — the exact gap #521
            reported: ``self._load_lock`` already does the in-process case).

        Bounded wait: gives up after
        ``settings.embedding_gpu_lock_timeout_seconds`` and yields
        ``_LOCK_TIMEOUT`` rather than waiting forever behind a stuck or
        long-running holder. The caller treats a timeout as "someone else is
        actively using the GPU" and falls back to CPU instead of piling on.

        Defensive: any error opening or locking the file (permissions, missing
        parent dir, disk full, an fcntl-less platform) logs a warning and
        yields ``_LOCK_ERROR``. This is a different outcome from timeout on
        purpose — an error means the *locking mechanism* is unavailable, not
        that another process holds the GPU, so the caller proceeds on GPU
        unserialized rather than needlessly forcing CPU. Serializing GPU access
        is a safety improvement; failing (or forcibly downgrading) an embed
        because a lock file couldn't be opened would be strictly worse.
        """
        if not settings.embedding_gpu_lock_enabled or not settings.embedding_gpu_lock_path:
            yield _LOCK_DISABLED
            return

        # Resolve a relative lock path against the PROJECT ROOT, not the
        # process cwd. Every LifeOS service pins cwd via systemd
        # WorkingDirectory, but an ad-hoc `python scripts/...` run from
        # elsewhere would otherwise open a *different* lock file and serialize
        # against nobody — the exact kind of silent no-op this lock exists to
        # prevent. An absolute setting is honoured as-is.
        lock_path = settings.embedding_gpu_lock_path
        if not os.path.isabs(lock_path):
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            lock_path = os.path.join(project_root, lock_path)
        fd = None
        try:
            lock_dir = os.path.dirname(lock_path)
            if lock_dir:
                os.makedirs(lock_dir, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            logger.warning(
                f"Could not open GPU embedding lock file {lock_path!r} ({e}); "
                "proceeding without cross-process serialization"
            )
            yield _LOCK_ERROR
            return

        acquired = False
        try:
            timeout = settings.embedding_gpu_lock_timeout_seconds
            deadline = time.monotonic() + timeout
            outcome = _LOCK_ERROR
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    outcome = _LOCK_ACQUIRED
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        logger.warning(
                            f"Unexpected error acquiring GPU embedding lock ({e}); "
                            "proceeding without cross-process serialization"
                        )
                        outcome = _LOCK_ERROR
                        break
                    if time.monotonic() >= deadline:
                        logger.warning(
                            f"Timed out after {timeout}s waiting for the GPU "
                            f"embedding lock ({lock_path}) — another process is "
                            "likely embedding on the GPU right now; falling back "
                            "to CPU for this process."
                        )
                        outcome = _LOCK_TIMEOUT
                        break
                    time.sleep(0.25)
            yield outcome
        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

    def _encode_with_fallback(self, data, **kwargs):
        """Encode with GPU→CPU fallback on RuntimeError, serialized against
        other processes' GPU embedding via a cross-process lock (#521).

        If the model was loaded on GPU and encode() raises a GPU error,
        the model is reloaded on CPU and the encode is retried.
        """
        # Bound the batch so a large document's chunks are encoded in small
        # groups instead of one giant allocation. An unbounded batch of a
        # multi-MB note's chunks spiked ~10GB of VRAM in a single call, which
        # exhausted the gfx1151 iGPU's SDMA queues and froze the host (#483).
        # This is semantically neutral: batching changes only peak memory, not
        # the resulting vectors. Callers may still override batch_size.
        kwargs.setdefault("batch_size", settings.embedding_batch_size)

        if self._force_cpu:
            # Already on CPU — nothing to serialize (the lock only protects
            # concurrent GPU access), so skip the flock overhead entirely.
            return self.model.encode(data, **kwargs)

        with self._acquire_gpu_lock() as outcome:
            if outcome == _LOCK_TIMEOUT:
                # Another process is very likely on the GPU right now. Rather
                # than encode on top of it — the exact concurrent-access
                # pattern that exhausts the iGPU's SDMA queues — give up on
                # GPU for the rest of this process's lifetime, same as a real
                # GPU error would (see the RuntimeError branch below).
                logger.warning(
                    "Falling back to CPU embeddings for this process because "
                    "the GPU embedding lock was not acquired in time"
                )
                self._model = None
                self._force_cpu = True
                return self.model.encode(data, **kwargs)

            # ACQUIRED, DISABLED, or ERROR all proceed on GPU as before — the
            # existing GPU->CPU RuntimeError fallback still applies.
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
