"""Tests for the embedding layer's hard cap on input length (embedding_max_seq_length)."""
import logging
import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config.settings import settings
from tests.tiny_tokenizer import build_tokenizer, text_with_tokens

pytestmark = pytest.mark.unit

CEILING = 400


@pytest.fixture(scope="module")
def tokenizer():
    # model_max_length equals the ceiling so a length check that truncates cannot see past it
    return build_tokenizer(model_max_length=CEILING)


class FakeModel:
    """A stand-in SentenceTransformer that encodes the way the real one does.

    ``encode`` tokenizes with truncation and padding set on the model's own
    (shared) tokenizer, as sentence-transformers does for every call.
    """

    def __init__(self, tokenizer, native: int = 32768):
        self.tokenizer = tokenizer
        self.max_seq_length = native
        self.calls = []

    def encode(self, data, **kwargs):
        self.calls.append((data, kwargs))
        texts = [data] if isinstance(data, str) else list(data)
        self.tokenizer(texts, truncation=True, max_length=self.max_seq_length, padding=True, verbose=False)
        return np.zeros((len(texts), 2)) if not isinstance(data, str) else np.zeros(2)


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


def warnings_from(caplog):
    """Warnings from the embedding service itself, not from libraries it calls."""
    return [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "api.services.embeddings"
    ]


class TestCeilingApplied:
    @patch("sentence_transformers.SentenceTransformer")
    def test_long_context_model_is_capped(self, st_class, tokenizer):
        model = FakeModel(tokenizer, native=32768)
        st_class.return_value = model

        service().model

        assert model.max_seq_length == CEILING

    @patch("sentence_transformers.SentenceTransformer")
    def test_model_with_a_lower_limit_keeps_it(self, st_class, tokenizer):
        model = FakeModel(tokenizer, native=300)
        st_class.return_value = model

        service().model

        assert model.max_seq_length == 300

    @patch("sentence_transformers.SentenceTransformer")
    def test_cpu_fallback_reload_is_capped_too(self, st_class, tokenizer):
        gpu, cpu = FakeModel(tokenizer), FakeModel(tokenizer)
        gpu.encode = MagicMock(side_effect=RuntimeError("HIP error: invalid device function"))
        st_class.side_effect = [gpu, cpu]

        service().embed_texts(["hello"])

        assert cpu.max_seq_length == CEILING

    @patch("sentence_transformers.SentenceTransformer")
    def test_cpu_model_loaded_after_a_gpu_load_failure_is_capped(self, st_class, tokenizer):
        cpu = FakeModel(tokenizer)
        st_class.side_effect = [RuntimeError("HIP error: out of memory"), cpu]

        service().model

        assert cpu.max_seq_length == CEILING

    @patch("sentence_transformers.SentenceTransformer")
    def test_queries_and_batches_reach_the_model_unchanged(self, st_class, tokenizer):
        model = FakeModel(tokenizer)
        st_class.return_value = model
        svc = service()

        svc.embed_text("what did we decide")
        svc.embed_texts(["a", "b"])

        assert model.calls[0][0] == "what did we decide"
        assert model.calls[1][0] == ["a", "b"]


class TestTruncationIsReported:
    @patch("sentence_transformers.SentenceTransformer")
    def test_text_over_the_ceiling_is_logged_with_counts_only(self, st_class, tokenizer, caplog):
        st_class.return_value = FakeModel(tokenizer)
        longest = text_with_tokens(tokenizer, CEILING * 3, seed=1)
        texts = ["short", text_with_tokens(tokenizer, CEILING + 5, seed=2), longest]

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            service().embed_texts(texts)

        (message,) = warnings_from(caplog)
        assert "2 of 2 long texts" in message
        assert f"longest {CEILING * 3} tokens" in message
        assert f"{CEILING}-token ceiling" in message
        assert longest not in message and "short" not in message

    @patch("sentence_transformers.SentenceTransformer")
    def test_the_ceiling_itself_is_allowed_and_one_more_token_is_not(self, st_class, tokenizer, caplog):
        st_class.return_value = FakeModel(tokenizer)
        svc = service()

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            svc.embed_texts([text_with_tokens(tokenizer, CEILING)])
            assert not warnings_from(caplog)
            svc.embed_texts([text_with_tokens(tokenizer, CEILING + 1)])

        assert len(warnings_from(caplog)) == 1

    @patch("sentence_transformers.SentenceTransformer")
    def test_text_of_few_characters_but_many_tokens_is_still_measured(self, st_class, tokenizer, caplog):
        st_class.return_value = FakeModel(tokenizer)
        emoji = "\U0001F600" * (CEILING // 4 + 10)  # four byte tokens per character
        assert len(tokenizer(emoji, verbose=False)["input_ids"]) > CEILING > len(emoji) + 8

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            service().embed_texts([emoji])

        assert len(warnings_from(caplog)) == 1

    @patch("sentence_transformers.SentenceTransformer")
    def test_a_single_long_query_is_reported_too(self, st_class, tokenizer, caplog):
        st_class.return_value = FakeModel(tokenizer)

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            service().embed_text(text_with_tokens(tokenizer, CEILING * 2))

        assert len(warnings_from(caplog)) == 1

    @patch("sentence_transformers.SentenceTransformer")
    def test_a_models_own_lower_limit_is_not_reported_as_the_ceiling(self, st_class, tokenizer, caplog):
        st_class.return_value = FakeModel(tokenizer, native=300)

        with caplog.at_level(logging.WARNING, logger="api.services.embeddings"):
            service().embed_texts([text_with_tokens(tokenizer, CEILING * 2)])

        assert not warnings_from(caplog)

    @patch("sentence_transformers.SentenceTransformer")
    def test_a_failing_length_check_never_blocks_embedding(self, st_class, tokenizer):
        st_class.return_value = FakeModel(tokenizer)
        svc = service()
        svc.model
        svc._length_tokenizer = MagicMock(side_effect=ValueError("broken"))

        assert svc.embed_texts([text_with_tokens(tokenizer, CEILING * 2)]) == [[0.0, 0.0]]

    @patch("sentence_transformers.SentenceTransformer")
    def test_a_model_whose_tokenizer_cannot_be_copied_still_embeds(self, st_class, tokenizer, caplog):
        model = FakeModel(MagicMock(__deepcopy__=MagicMock(side_effect=TypeError("no copy"))))
        model.encode = MagicMock(return_value=np.zeros((1, 2)))
        st_class.return_value = model

        with caplog.at_level(logging.DEBUG, logger="api.services.embeddings"):
            assert service().embed_texts([text_with_tokens(tokenizer, CEILING * 2)]) == [[0.0, 0.0]]

        assert not [r for r in caplog.records if "Could not check" in r.getMessage()]

    def test_the_private_tokenizer_starts_with_no_truncation_or_padding(self, tokenizer):
        from api.services.embeddings import EmbeddingService

        model = FakeModel(tokenizer)
        model.encode(["some text"])  # leaves truncation and padding set on the shared tokenizer
        assert tokenizer.backend_tokenizer.truncation is not None

        private = EmbeddingService._private_tokenizer(model)

        assert private is not tokenizer
        assert private.backend_tokenizer.truncation is None
        assert private.backend_tokenizer.padding is None


class TestCostAndConcurrency:
    @patch("sentence_transformers.SentenceTransformer")
    def test_short_texts_are_never_measured(self, st_class, tokenizer):
        st_class.return_value = FakeModel(tokenizer)
        svc = service()
        svc.model
        measure = MagicMock()
        svc._length_tokenizer = measure

        svc.embed_text("what did we decide last week")
        svc.embed_texts(["a short note", "another short note"])

        measure.assert_not_called()

    @patch("sentence_transformers.SentenceTransformer")
    def test_the_length_check_leaves_the_shared_tokenizer_alone(self, st_class, tokenizer):
        """Parallel encodes with no cross-process lock must not collide on tokenizer state."""
        model = FakeModel(tokenizer)
        st_class.return_value = model
        svc = service()
        svc.model
        long_text = text_with_tokens(tokenizer, CEILING * 2)
        errors = []

        def hammer():
            try:
                for _ in range(250):
                    svc.embed_text(long_text)
                    svc.embed_texts([long_text, "short"])
            except Exception as exc:  # noqa: BLE001 - any failure is the finding
                errors.append(repr(exc))

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


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
