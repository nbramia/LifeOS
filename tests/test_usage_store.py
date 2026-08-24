"""Tests for the usage tracking store (api/services/usage_store.py) and the
admin usage summary that reads it (#595).

The store itself is backend-agnostic: `record_usage()` takes whatever model
name and cost it's given and never recomputes or filters by model. This is
what lets a Hermes-proxied (external-backend) turn land in the same table
as a native turn and count toward the same totals with no store or admin
route change -- these tests are the proof.

The one exception is a negative `cost_usd`: money spent can't be less than
zero, so `record_usage()` clamps a negative value to 0.0 rather than storing
it verbatim (#657) -- see `test_record_usage_clamps_negative_cost_and_logs`.
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


def test_record_usage_clamps_negative_cost_and_logs(store, caplog):
    """A negative `cost_usd` (#657 -- e.g. a cache-token accounting bug that
    subtracts more than it should) must never reach the table: a floor that
    can be dragged below zero silently shrinks every SUM(cost_usd) it feeds
    (GET /api/admin/usage, session-cost totals). The guard clamps to 0.0 but
    must log loudly rather than silently absorbing it, so a recurrence is
    visible."""
    with caplog.at_level("ERROR"):
        store.record_usage(
            model="claude-sonnet-4-5-20250929", input_tokens=1087, output_tokens=245,
            cost_usd=-0.008319, conversation_id="conv-negative",
        )

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd FROM usage WHERE conversation_id = 'conv-negative'"
        ).fetchone()
    # Token counts are untouched -- only the invalid cost is clamped.
    assert row == (1087, 245, 0.0)
    assert any("negative cost_usd" in r.message for r in caplog.records)


def test_summary_totals_include_a_non_anthropic_model(store):
    """A row recorded under a model name the cost calculator
    (`agent_worker/pricing.py`'s `cost_for`, #656) doesn't recognize
    contributes to `get_summary()`'s totals exactly like a native one -- the
    store has no per-model branching that could exclude it."""
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


# ---------------------------------------------------------------------------
# `get_conversation_usage()` (#610) and the `unpriced` column (#613) —
# session-to-date cost, and a marker distinguishing a row whose upstream
# sent no `cost_usd` (recorded as 0.0, never invented) from a row that
# reported a real cost of zero. Both otherwise land in this table
# indistinguishably, which is exactly the gap #613 closes: prior to it,
# `get_conversation_usage()`'s sum was always documented as an unconditional
# floor, since there was no way to tell "these turns were free" from "some
# were unpriced." With the flag, a sum containing only rows written after
# this column existed can be exact; a sum spanning any earlier row remains
# a floor regardless of what this flag reports (see the docstring).
# ---------------------------------------------------------------------------

def test_record_usage_defaults_to_priced(store):
    """`unpriced` defaults False -- an ordinary call (no external-backend
    ambiguity) is a normal, priced turn."""
    store.record_usage(model="claude-haiku-4-5", input_tokens=10, output_tokens=5, cost_usd=0.001)
    with sqlite3.connect(store.db_path) as conn:
        (unpriced,) = conn.execute("SELECT unpriced FROM usage").fetchone()
    assert unpriced == 0


def test_record_usage_stores_unpriced_flag(store):
    store.record_usage(
        model="some-unrecognized-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-unpriced", unpriced=True,
    )
    with sqlite3.connect(store.db_path) as conn:
        (unpriced,) = conn.execute("SELECT unpriced FROM usage").fetchone()
    assert unpriced == 1


def test_existing_db_without_the_unpriced_column_is_migrated(tmp_path):
    """A `usage.db` written before #613 has no `unpriced` column at all.
    `UsageStore.__init__` must add it via ALTER TABLE without erroring, and
    existing rows must default to 0 (priced) -- that history genuinely
    can't be recovered as unpriced, so it's treated as priced rather than
    guessed at."""
    db_path = tmp_path / "pre_613_usage.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                conversation_id TEXT
            )
        """)
        conn.execute(
            "INSERT INTO usage (timestamp, model, input_tokens, output_tokens, cost_usd, conversation_id) "
            "VALUES ('2026-01-01T00:00:00', 'old-model', 10, 5, 0.0, 'pre-existing-conv')"
        )
        conn.commit()

    UsageStore(db_path=str(db_path))  # must not raise on the pre-existing table
    with sqlite3.connect(db_path) as conn:
        (unpriced,) = conn.execute(
            "SELECT unpriced FROM usage WHERE conversation_id = 'pre-existing-conv'"
        ).fetchone()
    assert unpriced == 0  # unrecoverable history defaults to priced, not guessed unpriced

    # A fresh instance against the same (now-migrated) file must not
    # re-raise on the ALTER TABLE either.
    UsageStore(db_path=str(db_path))


def test_get_conversation_usage_sums_verbatim_scoped_to_one_conversation(store):
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-a",
    )
    store.record_usage(
        model="deepseek-v3-fireworks", input_tokens=200, output_tokens=80,
        cost_usd=0.0009, conversation_id="conv-a",
    )
    # A different conversation's usage must never leak into the sum.
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=999, output_tokens=999,
        cost_usd=9.99, conversation_id="conv-b",
    )

    usage = store.get_conversation_usage("conv-a")
    assert usage["cost_usd"] == pytest.approx(0.002 + 0.0009)
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 130
    assert usage["turn_count"] == 2
    assert usage["is_lower_bound"] is False


def test_get_conversation_usage_unknown_or_missing_id_is_zero(store):
    """No usage recorded yet for this id -- and no id at all -- both report
    all-zero rather than raising. A brand-new conversation is a normal
    state, not an error."""
    expected = {
        "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
        "turn_count": 0, "is_lower_bound": False,
    }
    assert store.get_conversation_usage("never-recorded") == expected
    assert store.get_conversation_usage(None) == expected
    assert store.get_conversation_usage("") == expected


def test_get_conversation_usage_distinguishes_unpriced_from_genuinely_free(store):
    """A turn with no real cost_usd (`unpriced=True`) must mark the sum as
    a lower bound, while a turn that genuinely cost zero (`unpriced=False`)
    must not -- even though both persist the identical `cost_usd=0.0`."""
    store.record_usage(
        model="local-gemma", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-free", unpriced=False,
    )
    store.record_usage(
        model="some-unrecognized-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-unpriced", unpriced=True,
    )

    free = store.get_conversation_usage("conv-free")
    unpriced = store.get_conversation_usage("conv-unpriced")
    assert free["cost_usd"] == unpriced["cost_usd"] == 0.0
    assert free["is_lower_bound"] is False
    assert unpriced["is_lower_bound"] is True


def test_get_conversation_usage_one_unpriced_turn_marks_the_whole_sum(store):
    """A conversation with a real cost AND one unpriced turn is still
    reported as a lower bound -- one unknown turn is enough to make the
    total untrustworthy as an exact figure, even if most of it is real.
    Also checks the turn_count and token sums still cover both turns,
    including the unpriced one -- it isn't silently dropped from the
    total, just flagged."""
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-mixed", unpriced=False,
    )
    store.record_usage(
        model="some-unrecognized-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-mixed", unpriced=True,
    )

    usage = store.get_conversation_usage("conv-mixed")
    assert usage["cost_usd"] == pytest.approx(0.002)
    assert usage["input_tokens"] == 110
    assert usage["output_tokens"] == 60
    assert usage["turn_count"] == 2  # both turns count, including the unpriced one
    assert usage["is_lower_bound"] is True
