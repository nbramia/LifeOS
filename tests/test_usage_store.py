"""Tests for the usage tracking store (api/services/usage_store.py) and the
admin usage summary that reads it (#595).

The store itself is backend-agnostic: `record_usage()` takes whatever model
name and cost it's given and never recomputes or filters by model. This is
what lets a Hermes-proxied (external-backend) turn land in the same table
as a native turn and count toward the same totals with no store or admin
route change -- these tests are the proof.
"""
import sqlite3

import httpx
import pytest
from fastapi import FastAPI

from api.routes import admin
from api.services.usage_store import UsageStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return UsageStore(db_path=str(tmp_path / "usage.db"))


def test_record_usage_stores_cost_verbatim(store):
    """`record_usage()` takes `cost_usd` as given -- no pricing table, no
    recompute from token counts anywhere in this store."""
    store.record_usage(
        model="deepseek-v3-fireworks", input_tokens=120, output_tokens=340,
        cost_usd=0.00087, conversation_id="conv-1",
    )
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT model, input_tokens, output_tokens, cost_usd, conversation_id FROM usage"
        ).fetchone()
    assert row == ("deepseek-v3-fireworks", 120, 340, 0.00087, "conv-1")


def test_summary_totals_include_a_non_anthropic_model(store):
    """A row recorded under a model name the cost calculator
    (`api/services/cost_tracker.py`) doesn't recognize contributes to
    `get_summary()`'s totals exactly like a native one -- the store has no
    per-model branching that could exclude it."""
    store.record_usage(model="claude-haiku-4-5", input_tokens=100, output_tokens=50, cost_usd=0.001)
    store.record_usage(
        model="deepseek-v3-fireworks", input_tokens=120, output_tokens=340, cost_usd=0.00087,
    )

    stats = store.get_usage_stats()
    assert stats["request_count"] == 2
    assert stats["total_input_tokens"] == 220
    assert stats["total_output_tokens"] == 390
    assert stats["total_cost"] == pytest.approx(0.001 + 0.00087)

    summary = store.get_summary()
    assert summary["all_time"]["request_count"] == 2
    assert summary["last_24h"]["request_count"] == 2


async def test_admin_usage_endpoint_includes_external_backend_totals(monkeypatch, store):
    """End-to-end through `GET /api/admin/usage`: a Hermes-proxied turn's
    usage row (recorded by `_HermesTurnPersister.finalize()`,
    api/routes/hermes_proxy.py) shows up in the same admin summary as a
    native one, with no route-level filtering to bypass."""
    store.record_usage(model="claude-haiku-4-5", input_tokens=100, output_tokens=50, cost_usd=0.001)
    store.record_usage(
        model="deepseek-v3-fireworks", input_tokens=120, output_tokens=340, cost_usd=0.00087,
    )

    import api.services.usage_store as usage_store_module
    monkeypatch.setattr(usage_store_module, "get_usage_store", lambda: store)

    app = FastAPI()
    app.include_router(admin.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.get("/api/admin/usage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["all_time"]["request_count"] == 2
    assert data["all_time"]["total_cost"] == pytest.approx(0.001 + 0.00087)
