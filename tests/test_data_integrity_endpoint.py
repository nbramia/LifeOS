"""
Tests for the /health/data-integrity endpoint.

Mocks the Phase 7 verify_consistency() function to test endpoint
behavior without needing real databases.
"""
import time

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


def _healthy_result():
    """A verify_consistency() result with zero issues."""
    return {
        "person_stats_mismatches": {"count": 0, "fixed": 0, "details": "all consistent"},
        "orphaned_interactions": {"count": 0, "fixed": 0},
        "hidden_interactions": {"count": 0, "fixed": 0},
        "stale_merged_ids": {"count": 0, "fixed": 0},
        "stale_merged_relationships": {"count": 0, "fixed": 0},
        "relationship_hygiene": {"count": 0, "fixed": 0, "self_loops": 0, "hidden": 0},
        "orphaned_crm_records": {"count": 0, "fixed": 0},
        "total_issues": 0,
        "total_fixed": 0,
        "auto_fix_skipped": False,
    }


def _degraded_result():
    """A verify_consistency() result with some issues."""
    return {
        "person_stats_mismatches": {"count": 3, "fixed": 0, "details": "3 mismatched"},
        "orphaned_interactions": {"count": 2, "fixed": 0},
        "hidden_interactions": {"count": 0, "fixed": 0},
        "stale_merged_ids": {"count": 1, "fixed": 0},
        "stale_merged_relationships": {"count": 0, "fixed": 0},
        "relationship_hygiene": {"count": 0, "fixed": 0, "self_loops": 0, "hidden": 0},
        "orphaned_crm_records": {"count": 0, "fixed": 0},
        "total_issues": 6,
        "total_fixed": 0,
        "auto_fix_skipped": False,
    }


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the endpoint cache before each test."""
    from api.main import _data_integrity_cache
    _data_integrity_cache["result"] = None
    _data_integrity_cache["timestamp"] = 0.0
    yield
    _data_integrity_cache["result"] = None
    _data_integrity_cache["timestamp"] = 0.0


class TestDataIntegrityEndpoint:
    """Tests for /health/data-integrity endpoint."""

    @patch("api.main._run_consistency_check")
    def test_healthy_returns_status_healthy(self, mock_verify):
        """Zero issues → status 'healthy'."""
        mock_verify.return_value = _healthy_result()

        from api.main import data_integrity_check
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(data_integrity_check())

        assert result["status"] == "healthy"
        assert result["total_issues"] == 0
        assert all(v == 0 for v in result["checks"].values())
        mock_verify.assert_called_once()

    @patch("api.main._run_consistency_check")
    def test_degraded_returns_status_degraded(self, mock_verify):
        """Non-zero issues → status 'degraded'."""
        mock_verify.return_value = _degraded_result()

        from api.main import data_integrity_check
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(data_integrity_check())

        assert result["status"] == "degraded"
        assert result["total_issues"] == 6
        assert result["checks"]["person_stats_mismatches"] == 3
        assert result["checks"]["orphaned_interactions"] == 2
        assert result["checks"]["stale_merged_ids"] == 1

    @patch("api.main._run_consistency_check")
    def test_cache_returns_cached_result(self, mock_verify):
        """Second call within TTL returns cached result."""
        mock_verify.return_value = _healthy_result()

        from api.main import data_integrity_check
        import asyncio
        loop = asyncio.get_event_loop()

        result1 = loop.run_until_complete(data_integrity_check())
        assert result1["cached"] is False

        result2 = loop.run_until_complete(data_integrity_check())
        assert result2["cached"] is True

        # verify_consistency only called once
        mock_verify.assert_called_once()

    @patch("api.main._run_consistency_check")
    def test_cache_expires_after_ttl(self, mock_verify):
        """After TTL, cache is refreshed."""
        mock_verify.return_value = _healthy_result()

        from api.main import data_integrity_check, _data_integrity_cache
        import asyncio
        loop = asyncio.get_event_loop()

        loop.run_until_complete(data_integrity_check())
        assert mock_verify.call_count == 1

        # Expire the cache
        _data_integrity_cache["timestamp"] = time.time() - 3601

        loop.run_until_complete(data_integrity_check())
        assert mock_verify.call_count == 2

    @patch("api.main._run_consistency_check")
    def test_response_includes_elapsed_ms(self, mock_verify):
        """Response includes elapsed_ms timing."""
        mock_verify.return_value = _healthy_result()

        from api.main import data_integrity_check
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(data_integrity_check())

        assert "elapsed_ms" in result
        assert isinstance(result["elapsed_ms"], int)

    @patch("api.main._run_consistency_check")
    def test_all_check_fields_present(self, mock_verify):
        """Response includes all expected check fields."""
        mock_verify.return_value = _healthy_result()

        from api.main import data_integrity_check
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(data_integrity_check())

        expected_checks = {
            "person_stats_mismatches",
            "orphaned_interactions",
            "hidden_interactions",
            "stale_merged_ids",
            "stale_merged_relationships",
            "relationship_hygiene",
            "orphaned_crm_records",
        }
        assert set(result["checks"].keys()) == expected_checks

    @patch("api.main._run_consistency_check")
    def test_called_once_per_request(self, mock_verify):
        """Endpoint calls the consistency check exactly once (non-cached)."""
        mock_verify.return_value = _healthy_result()

        from api.main import data_integrity_check
        import asyncio
        asyncio.get_event_loop().run_until_complete(data_integrity_check())

        mock_verify.assert_called_once()
