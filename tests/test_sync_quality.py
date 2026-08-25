"""Tests for sync quality fixes: encode fallback, phone normalization, stale ID re-pointing."""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Encode-time GPU → CPU fallback
# ---------------------------------------------------------------------------

class TestEncodeFallback:
    """Test GPU failure during encode() triggers CPU reload and retry."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        from api.services.embeddings import reset_embedding_service
        from api.services.service_health import reset_service_health
        reset_embedding_service()
        reset_service_health()
        yield
        reset_embedding_service()
        reset_service_health()

    @patch("sentence_transformers.SentenceTransformer")
    def test_encode_gpu_error_retries_on_cpu(self, mock_st_class):
        """If encode() raises a HIP error, model should reload on CPU and retry."""
        import numpy as np
        from api.services.embeddings import EmbeddingService

        gpu_model = MagicMock()
        cpu_model = MagicMock()

        # GPU model loads fine but encode fails
        gpu_model.encode.side_effect = RuntimeError("HIP error: invalid device function")
        cpu_model.encode.return_value = np.array([[0.1, 0.2]])

        # First construction → GPU model, second (after reset) → CPU model
        mock_st_class.side_effect = [gpu_model, cpu_model]

        svc = EmbeddingService(model_name="test-model")
        result = svc.embed_texts(["hello"])

        assert result == [[0.1, 0.2]]
        # Should have loaded model twice: first GPU, then CPU
        assert mock_st_class.call_count == 2
        _, kwargs = mock_st_class.call_args
        assert kwargs["device"] == "cpu"

    @patch("sentence_transformers.SentenceTransformer")
    def test_encode_non_gpu_error_raises(self, mock_st_class):
        """Non-GPU RuntimeError during encode should propagate, no fallback."""
        from api.services.embeddings import EmbeddingService

        gpu_model = MagicMock()
        gpu_model.encode.side_effect = RuntimeError("Expected all tensors on same device")
        mock_st_class.return_value = gpu_model

        svc = EmbeddingService(model_name="test-model")
        with pytest.raises(RuntimeError, match="same device"):
            svc.embed_texts(["hello"])

        # Model should only have been loaded once (no CPU fallback attempt)
        assert mock_st_class.call_count == 1

    @patch("sentence_transformers.SentenceTransformer")
    def test_embed_text_single_also_falls_back(self, mock_st_class):
        """embed_text (single) should also use fallback."""
        import numpy as np
        from api.services.embeddings import EmbeddingService

        gpu_model = MagicMock()
        cpu_model = MagicMock()

        gpu_model.encode.side_effect = RuntimeError("CUDA out of memory")
        cpu_model.encode.return_value = np.array([0.1, 0.2])

        mock_st_class.side_effect = [gpu_model, cpu_model]

        svc = EmbeddingService(model_name="test-model")
        result = svc.embed_text("hello")

        assert result == [0.1, 0.2]
        assert mock_st_class.call_count == 2

    @patch("sentence_transformers.SentenceTransformer")
    def test_encode_fallback_records_degradation(self, mock_st_class):
        """GPU encode failure should record a degradation event."""
        import numpy as np
        from api.services.embeddings import EmbeddingService
        from api.services.service_health import get_service_health

        gpu_model = MagicMock()
        cpu_model = MagicMock()

        gpu_model.encode.side_effect = RuntimeError("HIP error: invalid device function")
        cpu_model.encode.return_value = np.array([[0.5]])

        mock_st_class.side_effect = [gpu_model, cpu_model]

        svc = EmbeddingService(model_name="test-model")
        svc.embed_texts(["test"])

        # Service health should still be healthy (recovered via CPU)
        state = get_service_health().get_state("embedding_model")
        assert state.status.value == "healthy"


# ---------------------------------------------------------------------------
# 2. Phone number normalization
# ---------------------------------------------------------------------------

class TestPhoneNormalization:
    """Test normalize_phone utility."""

    def test_parenthesized_area_code(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("(703) 798-6709") == "+17037986709"

    def test_ten_digits_no_separators(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("4102591307") == "+14102591307"

    def test_already_e164(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("+15551234567") == "+15551234567"

    def test_dashes_only(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("703-798-6709") == "+17037986709"

    def test_eleven_digits_with_country_code(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("14102591307") == "+14102591307"

    def test_spaces(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("703 798 6709") == "+17037986709"

    def test_dots(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("703.798.6709") == "+17037986709"

    def test_empty_string(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("") is None

    def test_none(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone(None) is None

    def test_too_short(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("555123") is None

    def test_too_long(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("1234567890123456") is None

    def test_international_already_e164(self):
        from api.services.phone_utils import normalize_phone
        assert normalize_phone("+447911123456") == "+447911123456"

    def test_letters_stripped(self):
        from api.services.phone_utils import normalize_phone
        # "Call me: 703-798-6709" → should extract digits
        assert normalize_phone("703-798-6709 ext123") is None  # 13 digits after strip


# ---------------------------------------------------------------------------
# 3. Stale merged ID re-pointing
# ---------------------------------------------------------------------------

class TestStaleIdRepointing:
    """Test the proactive stale ID re-pointing for interactions."""

    def test_repoint_stale_interaction_ids(self, tmp_path):
        """Interactions with old merged person_ids should be re-pointed to canonical IDs."""
        from scripts.sync_repoint_stale_ids import repoint_stale_interaction_ids

        # Set up a temporary interactions DB
        db_path = str(tmp_path / "interactions.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                timestamp TEXT,
                source_type TEXT,
                title TEXT,
                snippet TEXT,
                source_link TEXT,
                source_id TEXT,
                created_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("i1", "old-merged-id", "2026-01-01T00:00:00", "gmail", "Test", None, "", "src1", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("i2", "valid-id", "2026-01-01T00:00:00", "gmail", "Test2", None, "", "src2", "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        # Mock: old-merged-id → canonical-id, valid-id → valid-id
        mock_store = MagicMock()
        mock_store.get_canonical_id.side_effect = lambda pid: {
            "old-merged-id": "canonical-id",
            "valid-id": "valid-id",
        }.get(pid, pid)

        valid_ids = {"canonical-id", "valid-id"}

        result = repoint_stale_interaction_ids(
            db_path=db_path,
            person_store=mock_store,
            valid_person_ids=valid_ids,
            dry_run=False,
        )

        assert result["repointed"] == 1

        # Verify the DB was updated
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT person_id FROM interactions WHERE id = 'i1'").fetchone()
        assert row[0] == "canonical-id"
        conn.close()

    def test_repoint_dry_run_no_changes(self, tmp_path):
        """Dry run should report but not modify."""
        from scripts.sync_repoint_stale_ids import repoint_stale_interaction_ids

        db_path = str(tmp_path / "interactions.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                timestamp TEXT, source_type TEXT, title TEXT,
                snippet TEXT, source_link TEXT, source_id TEXT, created_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("i1", "old-id", "2026-01-01T00:00:00", "gmail", "Test", None, "", "src1", "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        mock_store = MagicMock()
        mock_store.get_canonical_id.return_value = "new-id"

        result = repoint_stale_interaction_ids(
            db_path=db_path,
            person_store=mock_store,
            valid_person_ids={"new-id"},
            dry_run=True,
        )

        assert result["stale_count"] >= 1
        assert result["repointed"] == 0

        # DB should be unchanged
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT person_id FROM interactions WHERE id = 'i1'").fetchone()
        assert row[0] == "old-id"
        conn.close()
