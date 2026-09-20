"""Minimal Jev (TypeSafe System One) client for the offline orchestrator
experiments (issue #1158). Vendored from ~/.claude/lifeos-jev/jev.py rather
than imported from api/services/jev_client.py because that client (#1157
PR 1) had not landed on this branch when these scripts were written; swap
to the real client once it does. httpx only, no other project dependency.

Never prints TYPESAFE_API_KEY. Never logs request/response bodies (they
carry Nathan's message text) -- only aggregate counts/latencies via
USAGE / usage_summary().
"""
import os
import time
import asyncio
from pathlib import Path
import httpx

_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
# Hard stop for the whole eval run (issue budget: <= $1 total).
_BUDGET_USD = 1.00

USAGE = {"calls": 0, "input_tokens": 0, "latency_s": [], "errors": 0}


def _key() -> str:
    k = os.environ.get("TYPESAFE_API_KEY")
    if k:
        return k
    env_path = Path.home() / "Code/Sync/envs/LifeOS/.env"
    for line in env_path.read_text().splitlines():
        if line.startswith("TYPESAFE_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("TYPESAFE_API_KEY missing (env var or ~/Code/Sync/envs/LifeOS/.env)")


def current_cost_usd() -> float:
    return USAGE["input_tokens"] / 1e6 * 0.042


def _check_budget() -> None:
    if current_cost_usd() > _BUDGET_USD:
        raise RuntimeError(
            f"Jev eval budget exceeded: ${current_cost_usd():.4f} > ${_BUDGET_USD:.2f} "
            f"after {USAGE['calls']} calls. Stopping rather than overspending."
        )


async def ask(state, questions: dict, model: str = MODEL, client: httpx.AsyncClient | None = None) -> dict:
    """POST one systemone request. `state` and `questions` follow the API
    contract in the issue: questions are {name: {type, criteria, ...}},
    answers come back as {name: {choice|score|noul, probabilities, confidence?}}.
    """
    _check_budget()
    t = time.perf_counter()
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30)
    try:
        r = None
        for attempt in range(4):
            r = await client.post(
                _URL,
                headers={"Authorization": f"Bearer {_key()}"},
                json={"state": state, "model": model, "questions": questions},
            )
            if r.status_code != 429:
                break
            await asyncio.sleep(float(r.headers.get("retry-after", 2 ** attempt)))
        r.raise_for_status()
        body = r.json()
    except Exception:
        USAGE["errors"] += 1
        raise
    finally:
        if owns_client:
            await client.aclose()
    USAGE["calls"] += 1
    USAGE["input_tokens"] += body.get("usage", {}).get("input_tokens", 0)
    USAGE["latency_s"].append(time.perf_counter() - t)
    return body["answers"]


def usage_summary() -> str:
    lat = sorted(USAGE["latency_s"]) or [0]
    return (
        f"{USAGE['calls']} calls ({USAGE['errors']} errors), "
        f"{USAGE['input_tokens']} input tokens (${current_cost_usd():.5f}), "
        f"latency p50 {lat[len(lat) // 2] * 1000:.0f}ms max {lat[-1] * 1000:.0f}ms"
    )
