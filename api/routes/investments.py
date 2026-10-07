"""Investments snapshot — the operator's Schwab pipeline, served from Syncthing.

The publisher's weekday refresh (nathan-linux ~/Code/investments, private
repo nbramia/investments) aggregates the Schwab accounts + Guideline 401(k) +
TSP and writes summary.json / portfolio.json into ~/Code/Sync/investments;
Syncthing carries them to the LifeOS host. These endpoints serve the files
from disk — stale-but-present when a refresh is missed (check synced_at).

- GET /api/investments/summary            compact household picture
- GET /api/investments/portfolio          full detail (no price series)
- GET /api/investments/portfolio?section= one top-level section only
- GET /api/investments/movers             scheduler digest: big day movers
- GET /api/investments/today              scheduler digest: day so far vs IVV
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from config.settings import settings

router = APIRouter(prefix="/api/investments", tags=["investments"])

logger = logging.getLogger(__name__)

SYNC_DIR = os.path.expanduser(settings.investments_sync_dir)

# Freshness alerting: the publisher refreshes on weekdays (~18:30)
# and Syncthing delivers here. A weekend plus the weekday cadence can leave the
# file ~3 days old legitimately, so warn only past this threshold — enough to
# catch a genuinely stuck pipeline / Syncthing without false-alarming on Mondays.
STALENESS_WARNING_DAYS = 4


def _load(name: str):
    path = os.path.join(SYNC_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail=f"{name} not synced yet — run the publisher refresh")
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


# --- Big-mover alert -------------------------------------------------------

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


# --- Day-so-far digest -------------------------------------------------------

BENCHMARK = "IVV"
NY = ZoneInfo("America/New_York")


def _held_shares() -> dict[str, float]:
    """Shares per quotable ticker held as of the latest snapshot, summed across
    the Schwab accounts (external accounts excluded, as in ``_held_tickers``).
    Mutual funds (five letters ending in X) are left out: their NAV posts after
    the close, so mid-session yfinance reports yesterday's move for them."""
    path = os.path.join(SYNC_DIR, "summary.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for p in data.get("positions", []):
        if not isinstance(p, dict) or p.get("external"):
            continue
        sym = (p.get("symbol") or "").strip().upper()
        shares = p.get("shares")
        if not sym or not isinstance(shares, (int, float)) or shares <= 0:
            continue
        if len(sym) == 5 and sym.endswith("X"):
            continue
        out[sym] = out.get(sym, 0.0) + float(shares)
    return out


def _quotes(symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Map each symbol to (last price, previous close) via yfinance; a symbol
    whose quote can't be resolved is omitted."""
    import yfinance as yf

    out: dict[str, tuple[float, float]] = {}
    for sym in symbols:
        try:
            fi = yf.Ticker(sym).fast_info
            last = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            if last and prev and prev > 0:
                out[sym] = (float(last), float(prev))
        except Exception:
            continue
    return out


def _traded_today() -> bool:
    """True when the benchmark has printed a trade in today's New York session
    (False on weekends and market holidays)."""
    import yfinance as yf

    bars = yf.Ticker(BENCHMARK).history(period="1d", interval="1m")
    if bars.empty:
        return False
    return bars.index[-1].tz_convert(NY).date() == datetime.now(NY).date()


def _pct(x: float) -> str:
    return f"{x:+.2f}%".replace("-", "−")


def _usd(x: float) -> str:
    return ("−" if x < 0 else "+") + f"${abs(x):,.0f}"


def day_digest(shares: dict[str, float], quotes: dict[str, tuple[float, float]],
               now: datetime) -> str:
    """The day-so-far message: the invested portfolio's move in % and $ against
    the benchmark's %, then the three biggest gainers and losers by %."""
    if BENCHMARK not in quotes:
        return ""
    held = [s for s in shares if s in quotes]
    if not held:
        return ""
    start = sum(shares[s] * quotes[s][1] for s in held)
    gain = sum(shares[s] * (quotes[s][0] - quotes[s][1]) for s in held)
    moves = {s: (quotes[s][0] / quotes[s][1] - 1) * 100 for s in held}
    ivv = (quotes[BENCHMARK][0] / quotes[BENCHMARK][1] - 1) * 100
    when = now.astimezone(NY).strftime("%-I:%M%p").lower()
    lines = [f"Portfolio through {when}: {_pct(gain / start * 100)} ({_usd(gain)}) "
             f"vs IVV {_pct(ivv)}"]
    up = sorted((s for s in held if moves[s] > 0), key=lambda s: -moves[s])[:3]
    down = sorted((s for s in held if moves[s] < 0), key=lambda s: moves[s])[:3]
    if up:
        lines.append("Top gainers: " + ", ".join(f"{s} {_pct(moves[s])}" for s in up))
    if down:
        lines.append("Top losers: " + ", ".join(f"{s} {_pct(moves[s])}" for s in down))
    missing = sorted(s for s in shares if s not in quotes)
    if missing:
        lines.append("No quote: " + ", ".join(missing))
    return "\n".join(lines)


def _today_message() -> str:
    if not _traded_today():
        return ""
    shares = _held_shares()
    quotes = _quotes(sorted(set(shares) | {BENCHMARK}))
    return day_digest(shares, quotes, datetime.now(NY))


@router.get("/today")
async def investments_today():
    """The invested portfolio's gain or loss so far today vs IVV, with the top
    three gainers and losers, for a scheduled ``endpoint`` action.

    Holdings are the latest snapshot's share counts (Schwab accounts, cash and
    mutual funds excluded) priced against the previous close via yfinance, so a
    trade made today counts from the next refresh. The message is empty — and
    the scheduler silent — on a non-trading day or any failure.
    """
    try:
        return {"scheduler_message": await asyncio.to_thread(_today_message)}
    except Exception as e:
        logger.warning(f"investments day digest failed: {e}")
        return {"scheduler_message": ""}
