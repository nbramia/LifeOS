"""Tests for the generalised backup_retention helper (issue #227).

The integration tests over ``interaction_store._prune_backups`` live in
``tests/test_interaction_store.py::TestBackupRetention`` and exercise the
same code path via the legacy alias. These tests target the helper
directly so any other store can adopt it with confidence.
"""
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from api.services.backup_retention import (
    DEFAULT_POLICY,
    RetentionPolicy,
    backup_filename,
    create_snapshot,
    parse_backup_timestamp,
    prune,
    prune_verified,
    verify_snapshot,
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
        with pytest.raises(FrozenInstanceError):
            DEFAULT_POLICY.daily_keep = 99  # type: ignore[misc]


class TestCreateSnapshot:
    """Snapshotting a live database (#562 — crm.db had no backup at all)."""

    @staticmethod
    def _db(path: Path, rows: int = 3) -> Path:
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO people (name) VALUES (?)", [(f"person{i}",) for i in range(rows)]
        )
        conn.commit()
        conn.close()
        return path

    def test_snapshot_copies_the_data(self, tmp_path):
        src = self._db(tmp_path / "crm.db")
        out = tmp_path / "backups"

        snap = create_snapshot(src, out, "crm.db")

        assert snap is not None and snap.exists()
        conn = sqlite3.connect(str(snap))
        assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3
        conn.close()

    def test_snapshot_is_consistent_with_an_open_writer(self, tmp_path):
        """
        A file copy of a live WAL database can capture a torn state — the main
        file without committed pages still in the -wal. The online backup API
        must not.
        """
        src = self._db(tmp_path / "crm.db")
        writer = sqlite3.connect(str(src))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO people (name) VALUES ('committed_via_wal')")
        writer.commit()
        try:
            snap = create_snapshot(src, tmp_path / "backups", "crm.db")
        finally:
            writer.close()

        conn = sqlite3.connect(str(snap))
        names = {r[0] for r in conn.execute("SELECT name FROM people")}
        conn.close()
        assert "committed_via_wal" in names

    def test_missing_source_returns_none(self, tmp_path):
        assert create_snapshot(tmp_path / "absent.db", tmp_path / "b", "crm.db") is None

    def test_creates_backup_dir(self, tmp_path):
        src = self._db(tmp_path / "crm.db")
        out = tmp_path / "nested" / "backups"

        assert create_snapshot(src, out, "crm.db") is not None
        assert out.is_dir()

    def test_uses_canonical_filename(self, tmp_path):
        src = self._db(tmp_path / "crm.db")
        ts = datetime(2026, 8, 13, 3, 30, 0)

        snap = create_snapshot(src, tmp_path / "b", "crm.db", now=ts)

        assert snap.name == backup_filename("crm.db", ts)
        assert parse_backup_timestamp(snap.name, "crm.db") == ts

    def test_prunes_only_its_own_basename(self, tmp_path):
        """
        Retention stays per-database: snapshotting crm.db must not evict
        interactions.db backups sharing the directory.
        """
        src = self._db(tmp_path / "crm.db")
        out = tmp_path / "backups"
        out.mkdir()
        base = datetime(2026, 1, 1)
        # Clustered a day apart so they share a retention bucket and are
        # genuinely eligible for pruning; spread out, each would be kept.
        for i in range(10):
            _touch_backup(out, "crm.db", base - timedelta(days=400 + i))
        survivor = _touch_backup(out, "interactions.db", base - timedelta(days=900))

        create_snapshot(src, out, "crm.db", now=base)

        assert survivor.exists()
        assert len(list(out.glob("crm.db.*.backup"))) < 11

    def test_leaves_manual_backups_alone(self, tmp_path):
        """Operators drop hand-named files here; retention must not eat them."""
        src = self._db(tmp_path / "crm.db")
        out = tmp_path / "backups"
        out.mkdir()
        manual = out / "crm.db.pre-junk-entity-purge.backup"
        manual.touch()

        create_snapshot(src, out, "crm.db")

        assert manual.exists()


class TestVerificationGatesRetention:
    """
    Tight retention is only safe if the copy being kept is known good (#562).

    With a long tail, a corrupt snapshot was survivable — older tiers remained.
    Keeping only a couple means a bad snapshot could be the last one standing,
    so verification has to gate the delete.
    """

    @staticmethod
    def _db(path: Path) -> Path:
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (1)")
        conn.commit()
        conn.close()
        return path

    def test_verify_accepts_a_real_database(self, tmp_path):
        assert verify_snapshot(self._db(tmp_path / "ok.db")) is True

    def test_verify_rejects_corrupt_empty_and_missing(self, tmp_path):
        corrupt = tmp_path / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database, not even close")
        empty = tmp_path / "empty.db"
        empty.touch()

        assert verify_snapshot(corrupt) is False
        assert verify_snapshot(empty) is False
        assert verify_snapshot(tmp_path / "absent.db") is False

    def test_verify_rejects_a_database_with_no_tables(self, tmp_path):
        """A snapshot of nothing is not a usable rollback point."""
        blank = tmp_path / "blank.db"
        sqlite3.connect(str(blank)).close()

        assert verify_snapshot(blank) is False

    def test_corrupt_newest_snapshot_blocks_pruning(self, tmp_path):
        """The whole point: a bad backup must not displace good ones."""
        out = tmp_path / "b"
        out.mkdir()
        base = datetime(2026, 1, 1)
        olds = [_touch_backup(out, "crm.db", base - timedelta(days=10 + i)) for i in range(5)]
        for p in olds:
            self._db(p)
        newest = out / backup_filename("crm.db", base)
        newest.write_bytes(b"truncated garbage")

        removed = prune_verified(
            out, "crm.db", policy=RetentionPolicy(daily_keep=2, tiered=False), now=base,
        )

        assert removed == []
        assert all(p.exists() for p in olds)

    def test_good_newest_snapshot_allows_pruning(self, tmp_path):
        out = tmp_path / "b"
        out.mkdir()
        base = datetime(2026, 1, 1)
        for i in range(5):
            self._db(_touch_backup(out, "crm.db", base - timedelta(days=10 + i)))
        self._db(out / backup_filename("crm.db", base))

        removed = prune_verified(
            out, "crm.db", policy=RetentionPolicy(daily_keep=2, tiered=False), now=base,
        )

        assert removed
        assert len(list(out.glob("crm.db.*.backup"))) == 2

    def test_failed_snapshot_is_deleted_and_prunes_nothing(self, tmp_path, monkeypatch):
        """A snapshot that cannot be verified leaves no trace and no damage."""
        out = tmp_path / "b"
        out.mkdir()
        existing = self._db(_touch_backup(out, "crm.db", datetime(2025, 1, 1)))
        monkeypatch.setattr(
            "api.services.backup_retention.verify_snapshot", lambda p: False
        )

        result = create_snapshot(self._db(tmp_path / "crm.db"), out, "crm.db")

        assert result is None
        assert existing.exists()
        assert len(list(out.glob("crm.db.*.backup"))) == 1


class TestUntieredRetention:
    """`tiered=False` keeps exactly N and drops the long tail."""

    def test_keeps_only_the_n_most_recent(self, tmp_path):
        base = datetime(2026, 1, 1)
        for i in range(12):
            _touch_backup(tmp_path, "crm.db", base - timedelta(days=i * 40))

        prune(tmp_path, "crm.db", policy=RetentionPolicy(daily_keep=2, tiered=False), now=base)

        assert len(list(tmp_path.glob("crm.db.*.backup"))) == 2

    def test_tiered_default_still_keeps_a_tail(self, tmp_path):
        """Regression guard: the default policy is unchanged."""
        base = datetime(2026, 1, 1)
        for i in range(12):
            _touch_backup(tmp_path, "crm.db", base - timedelta(days=i * 40))

        prune(tmp_path, "crm.db", policy=DEFAULT_POLICY, now=base)

        assert len(list(tmp_path.glob("crm.db.*.backup"))) > 2


class TestSnapshotIsSelfContained:
    """
    A snapshot must be one file, not three.

    ``Connection.backup`` gives the destination the source's journal_mode, so
    snapshotting a WAL database produced a .backup plus -wal and -shm. Retention
    only tracks ``*.backup``, so the sidecars were orphaned the moment their
    parent was pruned — and a snapshot whose committed pages live in a separate
    -wal is not safe to copy or restore on its own.
    """

    @staticmethod
    def _wal_db(path: Path) -> Path:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('committed')")
        conn.commit()
        conn.close()
        return path

    def test_no_sidecar_files_are_left_behind(self, tmp_path):
        out = tmp_path / "b"

        snap = create_snapshot(self._wal_db(tmp_path / "src.db"), out, "src.db")

        assert snap is not None
        assert not (out / f"{snap.name}-wal").exists()
        assert not (out / f"{snap.name}-shm").exists()
        assert [p.name for p in out.iterdir()] == [snap.name]

    def test_snapshot_is_not_in_wal_mode(self, tmp_path):
        snap = create_snapshot(self._wal_db(tmp_path / "src.db"), tmp_path / "b", "src.db")

        conn = sqlite3.connect(str(snap))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.lower() != "wal"

    def test_data_survives_the_journal_mode_change(self, tmp_path):
        snap = create_snapshot(self._wal_db(tmp_path / "src.db"), tmp_path / "b", "src.db")

        conn = sqlite3.connect(str(snap))
        rows = [r[0] for r in conn.execute("SELECT v FROM t")]
        conn.close()
        assert rows == ["committed"]

    def test_verifying_a_snapshot_creates_no_sidecars(self, tmp_path):
        """Read-only opens of a WAL database still spawn -shm; ours must not."""
        out = tmp_path / "b"
        snap = create_snapshot(self._wal_db(tmp_path / "src.db"), out, "src.db")

        assert verify_snapshot(snap) is True
        assert [p.name for p in out.iterdir()] == [snap.name]
