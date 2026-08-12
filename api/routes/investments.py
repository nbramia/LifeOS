"""Investments snapshot — the operator's Schwab pipeline, served from Syncthing.

The macbook's nightly refresh (~/Code/Personal/investments, private repo
nbramia/investments) aggregates 5 Schwab accounts + Guideline 401(k) + TSP
and writes summary.json / portfolio.json into ~/Code/Sync/investments;
Syncthing lands them here within seconds. These endpoints serve the files
from disk — stale-but-present when the mac is asleep (check synced_at).

- GET /api/investments/summary            compact household picture
- GET /api/investments/portfolio          full detail (no price series)
- GET /api/investments/portfolio?section= one top-level section only
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/investments", tags=["investments"])

logger = logging.getLogger(__name__)

SYNC_DIR = os.path.expanduser("~/Code/Sync/investments")

# Freshness alerting (#448): the macbook pipeline refreshes on weekdays (~18:30)
# and Syncthing delivers here. A weekend plus the weekday cadence can leave the
# file ~3 days old legitimately, so warn only past this threshold — enough to
# catch a genuinely stuck pipeline / Syncthing without false-alarming on Mondays.
STALENESS_WARNING_DAYS = 4


def _load(name: str):
    path = os.path.join(SYNC_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail=f"{name} not synced yet — run the macbook refresh")
    with open(path) as f:
        data = json.load(f)
    synced = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    return data, synced


@router.get("/summary")
async def investments_summary():
    """Compact, LLM-friendly household financial summary."""
    data, synced = _load("summary.json")
    return {"synced_at": synced, **data}


@router.get("/portfolio")
async def investments_portfolio(section: Optional[str] = None):
    """Full portfolio detail (positions with lots/flows, savings, wealth
    history, regret, external accounts). Large — prefer ?section= for one
    top-level key (e.g. positions, savings, wealth, accounts, external)."""
    data, synced = _load("portfolio.json")
    if section:
        if section not in data:
            raise HTTPException(status_code=404,
                                detail=f"no section '{section}'; available: {sorted(data)}")
        return {"synced_at": synced, "section": section, "data": data[section]}
    return {"synced_at": synced, **data}


def check_investments_freshness() -> Optional[str]:
    """Return a staleness warning message if the snapshot is older than
    STALENESS_WARNING_DAYS, else None.

    Stale-but-present semantics: a missing / never-synced file is NOT an error
    (returns None) — the pipeline may simply not be set up on this host. Only a
    present-but-old snapshot warrants a warning (the weekday refresh or Syncthing
    likely stalled). Intended to be logged at WARNING by the nightly runner so it
    lands in the batched health report — not raised, not a CRITICAL page.
    """
    path = os.path.join(SYNC_DIR, "summary.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        # Missing / never-synced / vanished mid-check — not an error.
        return None
    age_days = (datetime.now().timestamp() - mtime) / 86400
    if age_days > STALENESS_WARNING_DAYS:
        synced = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        return (
            f"Investments snapshot is {age_days:.1f} days old (last synced {synced}); "
            f"the weekday refresh (~18:30) or Syncthing may have stalled."
        )
    return None


# --- Big-mover alert (#463) ----------------------------------------------

# Default day-change threshold: a held ticker up or down more than this many
# percent on the day is a "mover" worth a nudge.
MOVER_THRESHOLD_PCT = 5.0


def _held_tickers() -> list[str]:
    """Quotable tickers held as of the latest snapshot (non-external only).

    External accounts (Guideline 401(k), TSP) are excluded by policy — their
    fund-level balances have no actionable intraday day-move — regardless of
    whether the snapshot carries a symbol for them. Returns [] when the snapshot
    isn't synced or is malformed.
    """
    path = os.path.join(SYNC_DIR, "summary.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for p in data.get("positions", []):
        if not isinstance(p, dict):
            continue
        sym = (p.get("symbol") or "").strip().upper()
        if sym and not p.get("external") and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _day_changes(symbols: list[str]) -> dict[str, float]:
    """Map each symbol to its day-change percent (current price vs. prior close)
    via yfinance. Best-effort: a symbol whose quote can't be resolved is omitted,
    so a partial or failed fetch degrades to fewer movers rather than an error.
    """
    if not symbols:
        return {}
    import yfinance as yf

    out: dict[str, float] = {}
    for sym in symbols:
        try:
            fi = yf.Ticker(sym).fast_info
            last = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            if last and prev and prev > 0:
                out[sym] = (last - prev) / prev * 100.0
        except Exception:
            continue
    return out


@router.get("/movers")
async def investments_movers(threshold: float = MOVER_THRESHOLD_PCT):
    """Held positions whose absolute day change is more than ``threshold`` percent.

    Returns ``{"scheduler_message": <digest or "">, "count": N}``. The digest is
    tickers and percentages only (no dollar amounts); it is empty when nothing
    moved that much — or on any failure (missing snapshot / quote-fetch error) —
    so a scheduled ``endpoint`` action stays silent on a quiet day. The blocking
    yfinance fetch runs in a worker thread so it never stalls the event loop.
    """
    try:
        changes = await asyncio.to_thread(_day_changes, _held_tickers())
        movers = sorted(
            ((s, pct) for s, pct in changes.items() if abs(pct) > threshold),
            key=lambda sp: -abs(sp[1]),
        )
        if not movers:
            return {"scheduler_message": "", "count": 0}
        lines = [f"Positions moving more than {threshold:g}% today:"]
        for sym, pct in movers:
            lines.append(f"- {sym}: {'▲' if pct >= 0 else '▼'} {pct:+.1f}%")
        return {"scheduler_message": "\n".join(lines), "count": len(movers)}
    except Exception as e:
        logger.warning(f"investments movers check failed: {e}")
        return {"scheduler_message": "", "count": 0}
