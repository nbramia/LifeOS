"""Investments snapshot — Nathan's Schwab pipeline, served from Syncthing.

The macbook's nightly refresh (~/Code/Personal/investments, private repo
nbramia/investments) aggregates 5 Schwab accounts + Guideline 401(k) + TSP
and writes summary.json / portfolio.json into ~/Code/Sync/investments;
Syncthing lands them here within seconds. These endpoints serve the files
from disk — stale-but-present when the mac is asleep (check synced_at).

- GET /api/investments/summary            compact household picture
- GET /api/investments/portfolio          full detail (no price series)
- GET /api/investments/portfolio?section= one top-level section only
"""
import json
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/investments", tags=["investments"])

SYNC_DIR = os.path.expanduser("~/Code/Sync/investments")


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
