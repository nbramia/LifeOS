"""Tests for the embedding layer's hard cap on input length (embedding_max_seq_length)."""
import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config.settings import settings

pytestmark = pytest.mark.unit

CEILING = 1000


def fake_model(native: int, lengths_seen=None):
    """A stand-in SentenceTransformer whose tokenizer reports ``lengths_seen`` per text."""
    model = MagicMock()
    model.max_seq_length = native
    model.encode.side_effect = lambda data, **kw: np.zeros((1 if isinstance(data, str) else len(data), 2))
    model.tokenizer = MagicMock(
        side_effect=lambda texts, **kw: {"input_ids": [[7] * n for n in (lengths_seen or [1] * len(texts))]}
    )
    return model


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from api.services.embeddings import reset_embedding_service
    from api.services.service_health import reset_service_health

    monkeypatch.setattr(settings, "embedding_max_seq_length", CEILING)
    monkeypatch.setattr(settings, "embedding_gpu_lock_enabled", False)
    reset_embedding_service()
    reset_service_health()
    yield
    reset_embedding_service()
    reset_service_health()


def service():
    from api.services.embeddings import EmbeddingService

    return EmbeddingService(model_name="test-model")


class TestCeilingApplied:
    @patch("sentence_transformers.SentenceTransformer")
    def test_long_context_model_is_capped(self, st_class):
        model = fake_model(native=32768)
        st_class.return_value = model

        service().model

        assert model.max_seq_length == CEILING

    @patch("sentence_transformers.SentenceTransformer")
    def test_model_with_a_lower_limit_keeps_it(self, st_class):
        model = fake_model(native=512)
        st_class.return_value = model

        service().model

        assert model.max_seq_length == 512

    @patch("sentence_transformers.SentenceTransformer")
    def test_cpu_fallback_reload_is_capped_too(self, st_class):
        gpu, cpu = fake_model(native=32768), fake_model(native=32768)
        gpu.encode.side_effect = RuntimeError("HIP error: invalid device function")
        st_class.side_effect = [gpu, cpu]

        service().embed_texts(["hello"])

        assert cpu.max_seq_length == CEILING

    @patch("sentence_transformers.SentenceTransformer")
    def test_cpu_model_loaded_after_a_gpu_load_failure_is_capped(self, st_class):
        cpu = fake_model(native=32768)
        st_class.side_effect = [RuntimeError("HIP error: out of memory"), cpu]

        service().model

        assert cpu.max_seq_length == CEILING

    @patch("sentence_transformers.SentenceTransformer")
    def test_queries_and_batches_reach_the_model_unchanged(self, st_class):
        model = fake_model(native=32768)
        st_class.return_value = model
        svc = service()

        svc.embed_text("what did we decide")
        svc.embed_texts(["a", "b"])

        assert model.encode.call_args_list[0].args == ("what did we decide",)
        assert model.encode.call_args_list[1].args == (["a", "b"],)


class TestTruncationIsReported:
    @patch("sentence_transformers.SentenceTransformer")
    def test_text_over_the_ceiling_is_logged_with_counts_only(self, st_class, caplog):
        st_class.return_value = fake_model(native=32768, lengths_seen=[10, CEILING + 1, 5000])
        secret = "synthetic-sentinel-text"

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            service().embed_texts([secret, secret, secret])

        message = " ".join(r.getMessage() for r in caplog.records)
        assert "2 of 3 texts" in message
        assert "longest 5000" in message
        assert str(CEILING) in message
        assert secret not in message

    @patch("sentence_transformers.SentenceTransformer")
    def test_text_at_the_ceiling_is_not_reported(self, st_class, caplog):
        st_class.return_value = fake_model(native=32768, lengths_seen=[CEILING, 3])

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            service().embed_texts(["a", "b"])

        assert not caplog.records

    @patch("sentence_transformers.SentenceTransformer")
    def test_a_models_own_lower_limit_is_not_reported_as_the_ceiling(self, st_class, caplog):
        st_class.return_value = fake_model(native=512, lengths_seen=[5000])

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            service().embed_texts(["a"])

        assert not caplog.records

    @patch("sentence_transformers.SentenceTransformer")
    def test_a_failing_length_check_never_blocks_embedding(self, st_class):
        model = fake_model(native=32768)
        model.tokenizer.side_effect = ValueError("no tokenizer")
        st_class.return_value = model

        assert service().embed_texts(["a"]) == [[0.0, 0.0]]


class TestSetting:
    def test_field_default_leaves_room_for_the_prefix(self):
        from api.services.chunker import CONTEXT_PREFIX_RESERVE_TOKENS
        from config.settings import Settings

        default = Settings.model_fields["embedding_max_seq_length"].default
        assert default == 2048
        assert default - CONTEXT_PREFIX_RESERVE_TOKENS > 0

    def test_a_ceiling_too_low_for_any_chunk_is_rejected(self, monkeypatch):
        from pydantic import ValidationError
        from config.settings import Settings

        monkeypatch.setenv("LIFEOS_EMBEDDING_MAX_SEQ_LENGTH", "383")
        with pytest.raises(ValidationError):
            Settings()

        monkeypatch.setenv("LIFEOS_EMBEDDING_MAX_SEQ_LENGTH", "384")
        assert Settings().embedding_max_seq_length == 384
