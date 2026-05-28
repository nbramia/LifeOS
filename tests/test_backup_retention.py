"""Tests for the generalised backup_retention helper (issue #227).

The integration tests over ``interaction_store._prune_backups`` live in
``tests/test_interaction_store.py::TestBackupRetention`` and exercise the
same code path via the legacy alias. These tests target the helper
directly so any other store can adopt it with confidence.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from api.services.backup_retention import (
    DEFAULT_POLICY,
    RetentionPolicy,
    backup_filename,
    parse_backup_timestamp,
    prune,
)


pytestmark = pytest.mark.unit


def _touch_backup(d: Path, basename: str, ts: datetime) -> Path:
    """Create an empty backup file matching the canonical naming scheme."""
    p = d / backup_filename(basename, ts)
    p.touch()
    return p


class TestFilenameRoundTrip:
    """``backup_filename`` and ``parse_backup_timestamp`` must round-trip."""

    def test_roundtrip(self):
        ts = datetime(2026, 5, 28, 9, 7, 42)
        name = backup_filename("interactions.db", ts)
        assert name == "interactions.db.20260528_090742.backup"
        assert parse_backup_timestamp(name, "interactions.db") == ts

    def test_parser_rejects_wrong_basename(self):
        # The pruner for crm.db must not consume interactions.db files.
        name = backup_filename("interactions.db", datetime(2026, 1, 1, 0, 0, 0))
        assert parse_backup_timestamp(name, "crm.db") is None

    def test_parser_rejects_non_canonical_filenames(self):
        # Operator's hand-rolled backups stay untouched.
        assert parse_backup_timestamp(
            "interactions.db.pre-upgrade.backup", "interactions.db"
        ) is None

    def test_parser_rejects_corrupt_timestamp(self):
        # Right shape, wrong digits.
        assert parse_backup_timestamp(
            "interactions.db.99999999_999999.backup", "interactions.db"
        ) is None

    def test_basename_with_dots_doesnt_overmatch(self):
        # ``re.escape`` on the basename means ``interactions.db`` only
        # matches that exact filename, not e.g. ``interactionsXdb``.
        ts = datetime(2026, 5, 28, 12, 0, 0)
        weird_name = f"interactionsXdb.{ts.strftime('%Y%m%d_%H%M%S')}.backup"
        assert parse_backup_timestamp(weird_name, "interactions.db") is None


class TestPruneSegregation:
    """``prune`` only touches files matching the requested basename."""

    def test_other_basename_files_ignored(self, tmp_path):
        now = datetime(2026, 5, 28, 12, 0, 0)
        # 10 daily snapshots of interactions.db over the past 10 days
        # (would normally prune days 6/8/9 → 7 kept).
        for i in range(10):
            _touch_backup(tmp_path, "interactions.db", now - timedelta(days=i))
        # And 10 daily snapshots of crm.db — separate retention domain.
        for i in range(10):
            _touch_backup(tmp_path, "crm.db", now - timedelta(days=i))

        removed = prune(tmp_path, "interactions.db", now=now)

        # The crm.db files must be fully intact.
        crm_files = sorted(p.name for p in tmp_path.glob("crm.db.*.backup"))
        assert len(crm_files) == 10
        # And the interactions.db retention must have run normally.
        assert len(removed) == 3
        for prune_age in (6, 8, 9):
            stamp = (now - timedelta(days=prune_age)).strftime("%Y%m%d_%H%M%S")
            assert any(stamp in p.name for p in removed)

    def test_independent_pruning_per_basename(self, tmp_path):
        """Prune crm.db separately; interactions.db survives untouched."""
        now = datetime(2026, 5, 28, 12, 0, 0)
        # Only crm.db has more than 5 backups → exercises bucket math.
        for i in range(8):
            _touch_backup(tmp_path, "crm.db", now - timedelta(days=i))
        interactions_file = _touch_backup(tmp_path, "interactions.db", now)

        prune(tmp_path, "crm.db", now=now)

        # The interactions.db file must still be there — it's a different
        # retention domain.
        assert interactions_file.exists()


class TestPolicyOverride:
    """Caller-supplied ``RetentionPolicy`` actually changes the math."""

    def test_higher_daily_keep_retains_more(self, tmp_path):
        now = datetime(2026, 5, 28, 12, 0, 0)
        for i in range(10):
            _touch_backup(tmp_path, "interactions.db", now - timedelta(days=i))

        # Bump daily slots from 5 to 10 — nothing should be pruned.
        custom = RetentionPolicy(daily_keep=10)
        removed = prune(tmp_path, "interactions.db", policy=custom, now=now)
        assert removed == []

    def test_zero_daily_keep_still_keeps_one_per_weekly_bucket(self, tmp_path):
        """``daily_keep=0`` skips tier-1 entirely; everything goes through
        the bucket math, so we keep exactly one per week-of-age."""
        now = datetime(2026, 5, 28, 12, 0, 0)
        for i in range(10):
            _touch_backup(tmp_path, "interactions.db", now - timedelta(days=i))

        custom = RetentionPolicy(daily_keep=0)
        prune(tmp_path, "interactions.db", policy=custom, now=now)
        kept = sorted(p.name for p in tmp_path.glob("interactions.db.*.backup"))
        # Days 0..9 split into week-0 (days 0..6) and week-1 (days 7..9).
        # Newest per bucket kept → 2 survivors.
        assert len(kept) == 2


class TestNoOps:
    """Empty / nothing-to-do cases."""

    def test_empty_dir_returns_empty(self, tmp_path):
        assert prune(tmp_path, "interactions.db") == []

    def test_default_policy_is_immutable(self):
        # Defensive: DEFAULT_POLICY is a frozen dataclass, so accidental
        # in-place mutation by a caller can't bleed into other callers.
        with pytest.raises((AttributeError, Exception)):
            DEFAULT_POLICY.daily_keep = 99  # type: ignore[misc]
