"""
Unit tests for EmbeddingService GPU-to-CPU fallback logic.

These tests mock SentenceTransformer to test fallback behavior without
loading the actual ML model, so they run fast (no @pytest.mark.slow).
"""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset embedding service and service health singletons between tests."""
    from api.services.embeddings import reset_embedding_service
    from api.services.service_health import reset_service_health

    reset_embedding_service()
    reset_service_health()
    yield
    reset_embedding_service()
    reset_service_health()


class TestGpuToCpuFallback:
    """Test GPU failure detection and CPU fallback in EmbeddingService.model."""

    @patch("sentence_transformers.SentenceTransformer")
    def test_gpu_oom_falls_back_to_cpu(self, mock_st_class):
        """GPU OOM error should trigger CPU fallback and mark healthy."""
        from api.services.embeddings import EmbeddingService
        from api.services.service_health import get_service_health

        cpu_model = MagicMock()

        # First call (GPU) raises OOM, second call (CPU) succeeds
        mock_st_class.side_effect = [
            RuntimeError("HIP out of memory"),
            cpu_model,
        ]

        svc = EmbeddingService(model_name="test-model")
        result = svc.model

        assert result is cpu_model
        # Second call should have device="cpu"
        assert mock_st_class.call_count == 2
        _, kwargs = mock_st_class.call_args
        assert kwargs["device"] == "cpu"

        # Service should be marked healthy (not failed)
        state = get_service_health().get_state("embedding_model")
        assert state.status.value == "healthy"

    @patch("sentence_transformers.SentenceTransformer")
    def test_cuda_error_falls_back_to_cpu(self, mock_st_class):
        """CUDA error should also trigger CPU fallback."""
        from api.services.embeddings import EmbeddingService

        cpu_model = MagicMock()
        mock_st_class.side_effect = [
            RuntimeError("CUDA error: device-side assert triggered"),
            cpu_model,
        ]

        svc = EmbeddingService(model_name="test-model")
        result = svc.model

        assert result is cpu_model

    @patch("sentence_transformers.SentenceTransformer")
    def test_gpu_hang_falls_back_to_cpu(self, mock_st_class):
        """GPU hang error should trigger CPU fallback."""
        from api.services.embeddings import EmbeddingService

        cpu_model = MagicMock()
        mock_st_class.side_effect = [
            RuntimeError("GPU hang detected, resetting"),
            cpu_model,
        ]

        svc = EmbeddingService(model_name="test-model")
        result = svc.model

        assert result is cpu_model

    @patch("sentence_transformers.SentenceTransformer")
    def test_cpu_fallback_failure_marks_service_failed(self, mock_st_class):
        """If CPU fallback also fails, service should be marked failed."""
        from api.services.embeddings import EmbeddingService
        from api.services.service_health import get_service_health

        # Both GPU and CPU fail
        mock_st_class.side_effect = [
            RuntimeError("HIP out of memory"),
            OSError("Model files corrupted"),
        ]

        svc = EmbeddingService(model_name="test-model")
        with pytest.raises(OSError, match="Model files corrupted"):
            _ = svc.model

        # Service should be marked failed
        state = get_service_health().get_state("embedding_model")
        assert state.status.value == "unavailable"
        assert "CPU fallback also failed" in state.last_error

    @patch("sentence_transformers.SentenceTransformer")
    def test_non_gpu_runtime_error_does_not_fallback(self, mock_st_class):
        """RuntimeError without GPU keywords should NOT trigger fallback."""
        from api.services.embeddings import EmbeddingService
        from api.services.service_health import get_service_health

        mock_st_class.side_effect = RuntimeError("Expected all tensors to be on the same device")

        svc = EmbeddingService(model_name="test-model")
        with pytest.raises(RuntimeError, match="Expected all tensors"):
            _ = svc.model

        # Should only have been called once (no fallback attempt)
        assert mock_st_class.call_count == 1

        # Service should be marked failed
        state = get_service_health().get_state("embedding_model")
        assert state.status.value == "unavailable"

    @patch("sentence_transformers.SentenceTransformer")
    def test_non_runtime_error_does_not_fallback(self, mock_st_class):
        """Non-RuntimeError exceptions should NOT trigger GPU fallback."""
        from api.services.embeddings import EmbeddingService
        from api.services.service_health import get_service_health

        mock_st_class.side_effect = ValueError("Invalid model configuration")

        svc = EmbeddingService(model_name="test-model")
        with pytest.raises(ValueError, match="Invalid model configuration"):
            _ = svc.model

        # Should only have been called once (no fallback attempt)
        assert mock_st_class.call_count == 1

        # Service should be marked failed
        state = get_service_health().get_state("embedding_model")
        assert state.status.value == "unavailable"

    @patch("sentence_transformers.SentenceTransformer")
    def test_invalid_device_keyword_triggers_fallback(self, mock_st_class):
        """'invalid device' should trigger fallback (narrowed from bare 'device')."""
        from api.services.embeddings import EmbeddingService

        cpu_model = MagicMock()
        mock_st_class.side_effect = [
            RuntimeError("HIP error: invalid device function"),
            cpu_model,
        ]

        svc = EmbeddingService(model_name="test-model")
        result = svc.model

        assert result is cpu_model

    @patch("sentence_transformers.SentenceTransformer")
    def test_same_device_error_does_not_trigger_fallback(self, mock_st_class):
        """'same device' tensor error should NOT trigger fallback (regression test for #56)."""
        from api.services.embeddings import EmbeddingService

        mock_st_class.side_effect = RuntimeError(
            "Expected all tensors to be on the same device, but found at least two devices"
        )

        svc = EmbeddingService(model_name="test-model")
        with pytest.raises(RuntimeError, match="same device"):
            _ = svc.model

        # No fallback — only one call
        assert mock_st_class.call_count == 1

    @patch("sentence_transformers.SentenceTransformer")
    def test_concurrent_first_access_loads_model_once(self, mock_st_class):
        """Parallel threads hitting .model on first use must construct the model
        exactly once. Search tools run via execute_tool_parallel → asyncio.to_thread,
        so an unsynchronized lazy load races the (meta-device) init and surfaces as
        'Cannot copy out of meta tensor'. A load lock serializes the construction.
        """
        import threading
        import time
        from api.services.embeddings import EmbeddingService

        def slow_construct(*args, **kwargs):
            # Widen the race window so unsynchronized loads would double-construct.
            time.sleep(0.05)
            return MagicMock()

        mock_st_class.side_effect = slow_construct

        svc = EmbeddingService(model_name="test-model")
        results = []
        start = threading.Barrier(8)

        def access():
            start.wait()  # release all threads into .model at once
            results.append(svc.model)

        threads = [threading.Thread(target=access) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Constructed once despite 8 concurrent first-accessors.
        assert mock_st_class.call_count == 1
        # Every caller sees the same instance.
        assert all(r is svc._model for r in results)

    @patch("sentence_transformers.SentenceTransformer")
    def test_successful_gpu_load_marks_healthy(self, mock_st_class):
        """Successful GPU model load should mark service healthy."""
        from api.services.embeddings import EmbeddingService
        from api.services.service_health import get_service_health

        gpu_model = MagicMock()
        mock_st_class.return_value = gpu_model

        svc = EmbeddingService(model_name="test-model")
        result = svc.model

        assert result is gpu_model
        assert mock_st_class.call_count == 1

        state = get_service_health().get_state("embedding_model")
        assert state.status.value == "healthy"


class TestBatchSizeCap:
    """The encode batch is bounded so a large document can't spike VRAM in one
    allocation and exhaust the iGPU's SDMA queues, freezing the host (#483)."""

    @staticmethod
    def _fake_encode(data, **kwargs):
        # Mirror sentence-transformers: a list in → a vector per item; a single
        # string in → one vector. Each vector exposes .tolist().
        if isinstance(data, list):
            return [MagicMock(tolist=lambda: [0.1, 0.2]) for _ in data]
        return MagicMock(tolist=lambda: [0.1, 0.2])

    @patch("sentence_transformers.SentenceTransformer")
    def test_embed_texts_passes_configured_batch_size(self, mock_st_class):
        from api.services.embeddings import EmbeddingService
        from config.settings import settings

        model = MagicMock()
        model.encode.side_effect = self._fake_encode
        mock_st_class.return_value = model

        svc = EmbeddingService(model_name="test-model")
        svc.embed_texts(["a", "b", "c"])

        _, kwargs = model.encode.call_args
        assert kwargs["batch_size"] == settings.embedding_batch_size

    @patch("sentence_transformers.SentenceTransformer")
    def test_embed_text_passes_configured_batch_size(self, mock_st_class):
        from api.services.embeddings import EmbeddingService
        from config.settings import settings

        model = MagicMock()
        model.encode.side_effect = self._fake_encode
        mock_st_class.return_value = model

        svc = EmbeddingService(model_name="test-model")
        svc.embed_text("hello")

        _, kwargs = model.encode.call_args
        assert kwargs["batch_size"] == settings.embedding_batch_size

    @patch("sentence_transformers.SentenceTransformer")
    def test_caller_can_override_batch_size(self, mock_st_class):
        """setdefault means an explicit batch_size still wins."""
        from api.services.embeddings import EmbeddingService

        model = MagicMock()
        model.encode.side_effect = self._fake_encode
        mock_st_class.return_value = model

        svc = EmbeddingService(model_name="test-model")
        svc._encode_with_fallback(["a"], convert_to_numpy=True, batch_size=1)

        _, kwargs = model.encode.call_args
        assert kwargs["batch_size"] == 1

    def test_default_batch_size_is_bounded(self):
        """Guard against a future edit restoring a dangerously large default."""
        from config.settings import settings
        assert 1 <= settings.embedding_batch_size <= 32
