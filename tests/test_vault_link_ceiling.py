"""
Unit tests for the vault-interaction unverifiable-link ceiling arithmetic
and failure-message formatting used by test_p91_data_integrity.py.
"""
import pytest

from tests.test_p91_data_integrity import TestR2EntityResolution as _R2

pytestmark = pytest.mark.unit


class TestUnverifiableCeilingArithmetic:
    """Tests for the proportional ceiling computation."""

    def test_ceiling_rounds_up(self):
        """Ceiling is a fraction of checked rows, rounded up to the next integer."""
        assert _R2._unverifiable_ceiling(100) == 2  # 1.5 -> 2
        assert _R2._unverifiable_ceiling(101) == 2  # 1.515 -> 2
        assert _R2._unverifiable_ceiling(67) == 2  # 1.005 -> 2

    def test_ceiling_has_a_floor_of_one(self):
        """Ceiling never drops below 1, even for a small number of checked rows."""
        assert _R2._unverifiable_ceiling(1) == 1
        assert _R2._unverifiable_ceiling(10) == 1


class TestUnverifiableFailureMessage:
    """Tests for the failure message shown when the ceiling is exceeded."""

    def test_message_includes_counts_and_ceiling(self):
        """Message names the checked count, unverifiable count, and ceiling."""
        message = _R2._format_unverifiable_failure(100, ["a", "b", "c"], 2)
        assert "3 (ceiling 2) out of 100 checked" in message

    def test_message_lists_up_to_ten_ids_and_nothing_else(self):
        """Message lists at most ten offending ids and includes no other detail."""
        ids = [f"interaction-{i}" for i in range(15)]
        message = _R2._format_unverifiable_failure(100, ids, 2)
        for iid in ids[:10]:
            assert iid in message
        for iid in ids[10:]:
            assert iid not in message
