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
    positions.append({"symbol": "SPCX", "desc": "SpaceX Class A (SPV)",
                      "value": 3050, "weight_pct": 0.37})
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
    # The security NAME rides along with the ticker: a bare ticker the model
    # doesn't recognize loses to stale world knowledge ("SpaceX is private"),
    # so "do I own SpaceX?" must be answerable by literal text match.
    assert "SPCX — SpaceX Class A (SPV)" in out
    # Positions without a desc (the FIL fillers) keep the bare-ticker line.
    assert "FIL01:" in out


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


# ---------------------------------------------------------------------------
# Big-mover alert (#463)
# ---------------------------------------------------------------------------

async def test_movers_reports_positions_past_threshold(monkeypatch):
    import re
    monkeypatch.setattr(inv, "_held_tickers", lambda: ["AMD", "VTI", "NVDA"])
    monkeypatch.setattr(inv, "_day_changes", lambda syms: {"AMD": -7.2, "VTI": 1.1, "NVDA": 6.5})
    out = await inv.investments_movers(threshold=5)
    assert out["count"] == 2
    msg = out["scheduler_message"]
    assert "AMD" in msg and "NVDA" in msg
    assert "VTI" not in msg            # under threshold
    assert "-7.2%" in msg
    # Privacy: every mover line is exactly "- TICKER: ▲/▼ ±N.N%" — no dollar
    # amount, share count, or weight can structurally appear in the digest.
    for line in msg.splitlines()[1:]:
        assert re.match(r"^- [A-Z.]+: [▲▼] [+-]\d+\.\d%$", line), line


async def test_movers_strict_threshold_excludes_exactly_5(monkeypatch):
    """'More than 5%' is strict: a position at exactly the threshold is not a mover."""
    monkeypatch.setattr(inv, "_held_tickers", lambda: ["AMD"])
    monkeypatch.setattr(inv, "_day_changes", lambda syms: {"AMD": 5.0})
    out = await inv.investments_movers(threshold=5)
    assert out == {"scheduler_message": "", "count": 0}


async def test_movers_silent_when_nothing_moves(monkeypatch):
    monkeypatch.setattr(inv, "_held_tickers", lambda: ["AMD", "VTI"])
    monkeypatch.setattr(inv, "_day_changes", lambda syms: {"AMD": 1.0, "VTI": -2.4})
    out = await inv.investments_movers(threshold=5)
    assert out == {"scheduler_message": "", "count": 0}   # empty => scheduler stays silent


async def test_movers_non_fatal_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(inv, "_held_tickers", lambda: ["AMD"])

    def boom(syms):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(inv, "_day_changes", boom)
    out = await inv.investments_movers(threshold=5)
    assert out == {"scheduler_message": "", "count": 0}   # degrades to no-alert, not a 500


def test_held_tickers_excludes_external_and_blanks(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))
    (tmp_path / "summary.json").write_text(
        '{"positions": [{"symbol": "VTI", "external": false}, '
        '{"symbol": "GFND", "external": true}, {"symbol": "", "external": false}]}'
    )
    assert inv._held_tickers() == ["VTI"]


def test_held_tickers_empty_when_not_synced_or_malformed(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "SYNC_DIR", str(tmp_path))
    assert inv._held_tickers() == []                      # empty dir, no file
    (tmp_path / "summary.json").write_text("[1, 2, 3]")   # non-dict top-level
    assert inv._held_tickers() == []


def test_scheduler_endpoint_prefers_scheduler_message_field():
    """The endpoint action sends a ready {'scheduler_message': ...} verbatim
    (empty => suppressed), and does NOT hijack a generic {message} response
    from other routes (#463)."""
    from api.services.scheduler_store import _format_endpoint_result
    assert _format_endpoint_result({"scheduler_message": "AMD -7%", "count": 1}) == "AMD -7%"
    assert _format_endpoint_result({"scheduler_message": "", "count": 0}) == ""
    dumped = _format_endpoint_result({"status": "ok", "message": "done"})
    assert '"status"' in dumped and '"message"' in dumped   # generic {message} left as JSON
    assert '"foo"' in _format_endpoint_result({"foo": 1})


# ---------------------------------------------------------------------------
# On-demand movers via search_finances (#468)
# ---------------------------------------------------------------------------

async def test_search_finances_movers_action(monkeypatch):
    """search_finances(action='movers') returns the on-demand day-movers digest,
    reusing investments_movers — tickers + % only, no dollar amounts."""
    from unittest.mock import AsyncMock
    from api.services.agent_tools import _tool_search_finances
    monkeypatch.setattr(inv, "_held_tickers", lambda: ["AMD"])
    monkeypatch.setattr(inv, "investments_movers", AsyncMock(return_value={
        "scheduler_message": "Positions moving more than 5% today:\n- AMD: ▲ +5.8%", "count": 1}))
    out = await _tool_search_finances({"action": "movers"})
    assert "AMD" in out and "5.8%" in out
    assert "$" not in out


async def test_search_finances_movers_none_today(monkeypatch):
    """When nothing moved past the (custom) threshold, the tool answers plainly
    rather than returning an empty string."""
    from unittest.mock import AsyncMock
    from api.services.agent_tools import _tool_search_finances
    monkeypatch.setattr(inv, "_held_tickers", lambda: ["AMD"])
    monkeypatch.setattr(inv, "investments_movers", AsyncMock(return_value={
        "scheduler_message": "", "count": 0}))
    out = await _tool_search_finances({"action": "movers", "threshold": 3})
    assert "No held position moved more than 3% today" in out


async def test_search_finances_movers_threshold_zero_uses_default(monkeypatch):
    """An explicit threshold of 0 (or non-positive/non-numeric) falls back to the
    5% default rather than listing every position that moved at all."""
    from api.services.agent_tools import _tool_search_finances
    monkeypatch.setattr(inv, "_held_tickers", lambda: ["AMD"])
    captured = {}

    async def fake(threshold):
        captured["threshold"] = threshold
        return {"scheduler_message": "", "count": 0}

    monkeypatch.setattr(inv, "investments_movers", fake)
    out = await _tool_search_finances({"action": "movers", "threshold": 0})
    assert captured["threshold"] == inv.MOVER_THRESHOLD_PCT
    assert "more than 5% today" in out


async def test_search_finances_movers_snapshot_not_synced(monkeypatch):
    """On-demand: a missing snapshot says 'couldn't check' — distinct from a
    genuinely quiet day, so the user isn't misled that the market was flat."""
    from api.services.agent_tools import _tool_search_finances
    monkeypatch.setattr(inv, "_held_tickers", lambda: [])
    out = await _tool_search_finances({"action": "movers"})
    assert "couldn't check" in out.lower() or "isn't available" in out.lower()
