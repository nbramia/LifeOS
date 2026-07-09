"""Tests for the investments snapshot route helpers.

Focus: the freshness check (#448) that warns when the Schwab-pipeline snapshot
(delivered via Syncthing) goes stale, with stale-but-present semantics — a
missing file is never an error, and a normal weekend cadence never warns.
"""
import os
import time

import pytest

from api.routes import investments as inv

pytestmark = pytest.mark.unit


def _write_summary(tmp_path, age_days: float):
    """Write a summary.json and backdate its mtime by age_days."""
    p = tmp_path / "summary.json"
    p.write_text('{"totals": {}}')
    old = time.time() - age_days * 86400
    os.utime(p, (old, old))
    return p


def test_freshness_fresh_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))
    _write_summary(tmp_path, age_days=1)
    assert inv.check_investments_freshness() is None


def test_freshness_weekend_cadence_does_not_warn(tmp_path, monkeypatch):
    """A file ~3 days old (Friday refresh read on Monday) must not warn."""
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))
    _write_summary(tmp_path, age_days=3)
    assert inv.check_investments_freshness() is None


def test_freshness_stale_returns_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))
    _write_summary(tmp_path, age_days=6)
    msg = inv.check_investments_freshness()
    assert msg is not None
    assert "days old" in msg
    assert not msg.lower().startswith("error")


def test_freshness_missing_file_is_not_an_error(tmp_path, monkeypatch):
    """A never-synced snapshot returns None (skip), not a warning or an error."""
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))  # empty dir, no file
    assert inv.check_investments_freshness() is None


def test_freshness_just_under_threshold_does_not_warn(tmp_path, monkeypatch):
    """3.9 days (just under the 4-day threshold) must not warn — pins the boundary."""
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))
    _write_summary(tmp_path, age_days=inv.STALENESS_WARNING_DAYS - 0.1)
    assert inv.check_investments_freshness() is None


def test_freshness_just_over_threshold_warns(tmp_path, monkeypatch):
    """4.5 days (just over the threshold) warns — pins the boundary."""
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))
    _write_summary(tmp_path, age_days=inv.STALENESS_WARNING_DAYS + 0.5)
    assert inv.check_investments_freshness() is not None
