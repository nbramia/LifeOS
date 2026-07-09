"""Tests for the investments snapshot route helpers.

Focus: the freshness check (#448) that warns when the Schwab-pipeline snapshot
(delivered via Syncthing) goes stale, with stale-but-present semantics — a
missing file is never an error, and a normal weekend cadence never warns.
Also covers the search_finances 'investments' digest (#452): every holding is
listed, so a beyond-top-15 position is never silently dropped.
"""
import json
import os
import time

import pytest

from api.routes import investments as inv
from api.services.agent_tools import _tool_search_finances

pytestmark = pytest.mark.unit


def _snapshot_with_many_positions():
    """A summary-style snapshot with 20 positions, value-descending, where a
    low-value holding ("SPCX") sits at rank 20 — beyond the old top-15 cap."""
    positions = [
        {"symbol": f"FIL{i:02d}", "value": 100000 - i * 1000,
         "weight_pct": 5, "unrealized": 1000}
        for i in range(19)
    ]
    positions.append({"symbol": "SPCX", "value": 3050, "weight_pct": 0.37})
    return {
        "as_of": "2026-07-09",
        "totals": {
            "all_investments": 1234567, "schwab": 1000000, "external_retirement": 234567,
            "tax_buckets": {"pretax": 500000, "roth": 300000, "taxable": 434567},
        },
        "accounts": [
            {"key": "brokerage", "name": "Synthetic Brokerage", "value": 1000000, "external": False},
        ],
        "positions": positions,
        "taxable_unrealized": {"long_term": 80000, "short_term": 5000, "harvestable_losses": -2000},
        "savings_net_by_year": {"2025": 1000},
    }


async def test_search_finances_investments_lists_all_positions(tmp_path, monkeypatch):
    """The investments digest must include a beyond-top-15 holding (regression
    for #452, where SPCX at rank 44 was silently dropped by a [:15] cap)."""
    home = tmp_path / "home"
    inv_dir = home / "Code" / "Sync" / "investments"
    inv_dir.mkdir(parents=True)
    (inv_dir / "summary.json").write_text(json.dumps(_snapshot_with_many_positions()))
    monkeypatch.setenv("HOME", str(home))  # ~ in the digest path resolves here

    out = await _tool_search_finances({"action": "investments"})

    assert "SPCX" in out                 # beyond-top-15 holding is present
    assert "Positions (20):" in out      # header reflects the full count
    assert "Top positions:" not in out   # old truncating header is gone


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
