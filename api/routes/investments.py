"""Investments snapshot — the operator's Schwab pipeline, served from Syncthing.

The publisher's weekday refresh (nathan-linux ~/Code/Personal/investments, private
repo nbramia/investments) aggregates the Schwab accounts + Guideline 401(k) +
TSP and writes summary.json / portfolio.json into ~/Code/Sync/investments;
Syncthing carries them to the LifeOS host. These endpoints serve the files
from disk — stale-but-present when a refresh is missed (check synced_at).

- GET /api/investments/summary            compact household picture
- GET /api/investments/portfolio          full detail (no price series)
- GET /api/investments/portfolio?section= one top-level section only
- GET /api/investments/movers?threshold=  scheduler digest: the investments
                                          repo's movers.py (big day movers)
- GET /api/investments/today              scheduler digest: the investments
                                          repo's day_digest.py (day so far vs IVV)

The two digests run scripts in the investments checkout
(settings.investments_dir), which computes them from live Schwab data; LifeOS
only runs and schedules them.
"""
import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from typing import Optional

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


# --- Scheduler digests from the investments repo ---------------------------
#
# The investments repo owns these computations (live Schwab positions and
# quotes); LifeOS only runs its scripts with that repo's venv and schedules
# the output.

INVESTMENTS_SCRIPT_TIMEOUT_S = 120

# Default day-change threshold: a held ticker up or down more than this many
# percent on the day is a "mover" worth a nudge.
MOVER_THRESHOLD_PCT = 5.0


def _run_investments_script(script: str, *args: str) -> str:
    """Run ``script`` from the investments checkout with its own venv and
    return its stdout. Raises on a missing checkout, timeout or non-zero exit."""
    repo = os.path.expanduser(settings.investments_dir)
    result = subprocess.run(
        [os.path.join(repo, "venv", "bin", "python"), script, *args],
        cwd=repo, capture_output=True, text=True, timeout=INVESTMENTS_SCRIPT_TIMEOUT_S,
    )
    if result.returncode:
        raise RuntimeError(f"{script} exited {result.returncode}: {result.stderr[-500:]}")
    return result.stdout.strip()


async def _movers(threshold: float) -> dict:
    """Run movers.py; raises when it couldn't check (e.g. Schwab unreachable)."""
    msg = await asyncio.to_thread(_run_investments_script, "movers.py", "--threshold", f"{threshold:g}")
    count = sum(1 for line in msg.splitlines() if line.startswith("- "))
    return {"scheduler_message": msg, "count": count}


@router.get("/movers")
async def investments_movers(threshold: float = MOVER_THRESHOLD_PCT):
    """Held positions whose absolute day change is more than ``threshold``
    percent, from the investments repo's movers.py.

    Returns ``{"scheduler_message": <digest or "">, "count": N}``. The digest is
    tickers and percentages only (no dollar amounts); it is empty on a quiet or
    non-trading day — or on any failure — so a scheduled ``endpoint`` action
    stays silent.
    """
    try:
        return await _movers(threshold)
    except Exception as e:
        logger.warning(f"investments movers check failed: {e}")
        return {"scheduler_message": "", "count": 0}


@router.get("/today")
async def investments_today():
    """The invested portfolio's move so far today vs IVV with its top three
    gainers and losers (the investments repo's day_digest.py), for a weekday
    15:00 ``endpoint`` schedule. Empty — and the scheduler silent — on a
    non-trading day or any failure."""
    try:
        return {"scheduler_message": await asyncio.to_thread(_run_investments_script, "day_digest.py")}
    except Exception as e:
        logger.warning(f"investments day digest failed: {e}")
        return {"scheduler_message": ""}
