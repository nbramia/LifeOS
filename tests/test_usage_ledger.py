"""Synthetic regression coverage for the canonical usage ledger."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest

from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.usage_ledger import (
    ESTIMATED,
    MEASURED,
    UNKNOWN,
    UsageLedger,
    UsageObservation,
)
from api.services.usage_store import UsageStore


pytestmark = pytest.mark.unit


def _ledger(tmp_path):
    db = tmp_path / "sessions.db"
    sessions = SessionStore(db)
    sessions.create("task", session_id="sess")
    return sessions, UsageLedger(db)


def test_usage_key_is_idempotent_and_updates_pre_turn_session_once(tmp_path):
    sessions, ledger = _ledger(tmp_path)
    persisted = sessions.get("task")
    observation = UsageObservation(
        "sess", persisted.attempt_id, "legacy:sess:turn", "hermes",
        input_tokens=10, output_tokens=4,
        input_kind=MEASURED, output_kind=MEASURED,
        cost_usd=.25, cost_kind=MEASURED, billing_class="metered",
        requested_engine="hermes", requested_model="paid-request",
        served_engine=None, served_model=None,
        evidence_source="hermes_usage_event", event_id="upstream-1",
    )
    first = ledger.record(observation)
    duplicate = ledger.record(observation)
    assert not first.duplicate
    assert duplicate.duplicate
    refreshed = sessions.get("task")
    assert refreshed.total_input_tokens == 10
    assert refreshed.total_dollars == .25
    # The explicit legacy sentinel represents the persisted pre-turn NULL;
    # matching attempt identity allows this compatibility projection without
    # replacing either persisted lifecycle id.
    assert refreshed.attempt_id == persisted.attempt_id
    assert refreshed.turn_id is None


def test_worker_and_persister_observations_same_turn_do_not_double_count(tmp_path):
    sessions, ledger = _ledger(tmp_path)
    persisted = sessions.get("task")
    legacy_turn_id = "legacy:sess:turn"
    first = UsageObservation(
        "sess", persisted.attempt_id, legacy_turn_id, "worker", input_tokens=5, output_tokens=2,
        input_kind=MEASURED, output_kind=MEASURED, cost_usd=.1,
        cost_kind=MEASURED, billing_class="metered", event_id="worker",
    )
    second = UsageObservation(
        "sess", persisted.attempt_id, legacy_turn_id, "persister", input_tokens=5, output_tokens=2,
        input_kind=MEASURED, output_kind=MEASURED, cost_usd=.1,
        cost_kind=MEASURED, billing_class="metered", event_id="persister",
    )
    ledger.record(first)
    result = ledger.record(second)
    assert result.input_delta == 0
    usage_key = f"sess:{persisted.attempt_id}:{legacy_turn_id}"
    assert ledger.get(usage_key)["input_tokens"] == 5
    assert sessions.get("task").total_dollars == .1


def test_stale_exact_identity_does_not_fallback_after_a_real_turn_exists(tmp_path):
    sessions, ledger = _ledger(tmp_path)
    current = sessions.begin_executor_turn("task", "execute")
    ledger.record(UsageObservation(
        "sess", "stale-attempt", "stale-turn", "worker",
        input_tokens=5, output_tokens=2, input_kind=MEASURED,
        output_kind=MEASURED, cost_usd=.1, cost_kind=MEASURED,
        billing_class="metered", event_id="stale",
    ))
    refreshed = sessions.get("task")
    assert refreshed.attempt_id == current.attempt_id
    assert refreshed.turn_id == current.turn_id
    assert refreshed.total_input_tokens == 0
    assert refreshed.total_dollars == 0


def test_stale_identity_does_not_fallback_after_reopen_before_new_turn(tmp_path):
    sessions, ledger = _ledger(tmp_path)
    old = sessions.begin_executor_turn("task", "execute")
    assert sessions.update_status(
        "task", "failed", attempt_id=old.attempt_id, turn_id=old.turn_id,
    )
    reopened = sessions.begin_new_execution("task")
    assert reopened.turn_id is None

    ledger.record(UsageObservation(
        "sess", old.attempt_id, old.turn_id, "worker",
        input_tokens=5, output_tokens=2, input_kind=MEASURED,
        output_kind=MEASURED, cost_usd=.1, cost_kind=MEASURED,
        billing_class="metered", event_id="late-reopened",
    ))

    refreshed = sessions.get("task")
    assert refreshed.attempt_id == reopened.attempt_id
    assert refreshed.turn_id is None
    assert refreshed.total_input_tokens == 0
    assert refreshed.total_dollars == 0


def test_cumulative_snapshots_apply_deltas_once(tmp_path):
    _, ledger = _ledger(tmp_path)
    common = dict(
        session_id="sess", attempt_id="a", turn_id="t", source="cli",
        input_kind=MEASURED, output_kind=MEASURED, cost_kind=MEASURED,
        billing_class="subscription", cumulative=True,
    )
    first = ledger.record(UsageObservation(**common, input_tokens=10, output_tokens=2,
                                           cost_usd=.01, event_id="snap-1"))
    second = ledger.record(UsageObservation(**common, input_tokens=15, output_tokens=3,
                                            cost_usd=.02, event_id="snap-2"))
    assert (first.input_delta, second.input_delta) == (10, 5)
    assert ledger.get("sess:a:t")["input_tokens"] == 15
    assert ledger.get("sess:a:t")["billed_cost_usd"] == .02


def test_equal_timestamp_snapshot_counters_keep_high_water_marks(tmp_path):
    _, ledger = _ledger(tmp_path)
    common = dict(
        session_id="sess", attempt_id="a", turn_id="equal", source="managed",
        input_kind=MEASURED, output_kind=MEASURED, cost_kind=MEASURED,
        billing_class="metered", cumulative=True,
    )
    ledger.record(UsageObservation(**common, input_tokens=10, output_tokens=2,
                                   cost_usd=.10, observed_at=200, event_id="first"))
    ledger.record(UsageObservation(**common, input_tokens=5, output_tokens=1,
                                   cost_usd=.05, observed_at=200, event_id="equal-lower"))
    result = ledger.record(UsageObservation(**common, input_tokens=15, output_tokens=3,
                                            cost_usd=.15, observed_at=300, event_id="latest"))
    row = ledger.get("sess:a:equal")
    assert (result.input_delta, result.output_delta) == (5, 1)
    assert result.cost_delta_usd == pytest.approx(.05)
    assert (row["input_tokens"], row["output_tokens"], row["billed_cost_usd"]) == (15, 3, .15)


def test_measured_correction_replaces_unknown_reservation_without_double_count(tmp_path):
    _, ledger = _ledger(tmp_path)
    ledger.record(UsageObservation(
        "sess", "a", "t", "cli", input_tokens=7, output_tokens=3,
        input_kind=MEASURED, output_kind=MEASURED, cost_usd=0,
        cost_kind=UNKNOWN, billing_class="subscription", reservation_usd=1.0,
        reservation_kind=ESTIMATED, event_id="unknown",
    ))
    result = ledger.record(UsageObservation(
        "sess", "a", "t", "cli", input_tokens=7, output_tokens=3,
        input_kind=MEASURED, output_kind=MEASURED, cost_usd=.14,
        cost_kind=MEASURED, billing_class="subscription", correction=True,
        event_id="authoritative",
    ))
    row = ledger.get("sess:a:t")
    assert result.corrected
    assert row["billed_cost_usd"] == .14
    assert row["reservation_usd"] == 0
    assert row["input_tokens"] == 7


def test_cumulative_measured_correction_releases_unknown_and_projects_once(tmp_path):
    sessions, ledger = _ledger(tmp_path)
    current = sessions.begin_executor_turn("task", "execute")
    attempt_id = current.attempt_id
    turn_id = current.turn_id
    usage_key = f"sess:{attempt_id}:{turn_id}"
    assert ledger.reserve(
        usage_key, 1.0, daily_cap_dollars=2,
        reservation_id="admission:cumulative-correction",
    )
    ledger.record(UsageObservation(
        "sess", attempt_id, turn_id, "managed",
        input_tokens=12, output_tokens=3, input_kind=MEASURED,
        output_kind=MEASURED, cost_kind=UNKNOWN, billing_class="unknown",
        cumulative=True, reservation_usd=1.0, reservation_kind=ESTIMATED,
        reservation_id="admission:cumulative-correction", observed_at=100,
        event_id="unknown-snapshot",
    ))

    corrected = ledger.record(UsageObservation(
        "sess", attempt_id, turn_id, "managed",
        input_tokens=12, output_tokens=3, input_kind=MEASURED,
        output_kind=MEASURED, cost_usd=.42, cost_kind=MEASURED,
        billing_class="metered", cumulative=True, correction=True,
        reservation_id="admission:cumulative-correction", observed_at=200,
        event_id="measured-correction",
    ))
    duplicate = ledger.record(UsageObservation(
        "sess", attempt_id, turn_id, "managed",
        input_tokens=12, output_tokens=3, input_kind=MEASURED,
        output_kind=MEASURED, cost_usd=.42, cost_kind=MEASURED,
        billing_class="metered", cumulative=True, correction=True,
        reservation_id="admission:cumulative-correction", observed_at=200,
        event_id="measured-correction",
    ))

    row = ledger.get(usage_key)
    assert corrected.corrected
    assert not duplicate.corrected and duplicate.duplicate
    assert row["cost_kind"] == MEASURED
    assert row["unknown_cost"] == 0
    assert row["billed_cost_usd"] == pytest.approx(.42)
    assert row["estimated_cost_usd"] == 0
    assert row["reservation_usd"] == 0
    assert sessions.get("task").total_dollars == pytest.approx(.42)

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM usage_reservations WHERE reservation_id=?",
            ("admission:cumulative-correction",),
        ).fetchone()[0] == "released"
        assert conn.execute(
            "SELECT total_dollars FROM daily_spend WHERE date=date('now','localtime')"
        ).fetchone()[0] == pytest.approx(.42)

    usage = UsageStore(str(tmp_path / "usage.db"))
    assert ledger.replay_projection(usage) == 1
    assert ledger.replay_projection(usage) == 1
    assert usage.get_usage_stats() == {
        "total_cost": pytest.approx(.42),
        "total_input_tokens": 12,
        "total_output_tokens": 3,
        "request_count": 1,
    }


def test_late_measured_correction_books_original_usage_day(tmp_path):
    _, ledger = _ledger(tmp_path)
    ledger.record(UsageObservation(
        "sess", "a", "late", "hermes", input_tokens=1, output_tokens=1,
        input_kind=MEASURED, output_kind=MEASURED, cost_kind=UNKNOWN,
        billing_class="unknown", reservation_usd=1, period_start="2026-01-02T03:04:05Z",
        event_id="late-unknown",
    ))
    ledger.record(UsageObservation(
        "sess", "a", "late", "hermes", input_tokens=1, output_tokens=1,
        input_kind=MEASURED, output_kind=MEASURED, cost_usd=.2, cost_kind=MEASURED,
        billing_class="metered", correction=True, period_start="2026-01-02T03:04:05Z",
        event_id="late-measured",
    ))
    import sqlite3
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT total_dollars FROM daily_spend WHERE date='2026-01-02'").fetchone()[0] == .2


def test_unknown_served_identity_is_not_invented_and_projection_is_replayable(tmp_path):
    _, ledger = _ledger(tmp_path)
    ledger.record(UsageObservation(
        "sess", "a", "t", "hermes", input_tokens=1, output_tokens=1,
        input_kind=MEASURED, output_kind=MEASURED, cost_usd=.2,
        cost_kind=MEASURED, billing_class="metered",
        requested_engine="hermes", requested_model="paid-request",
        requested_provider=None, served_engine=None, served_model=None,
        served_provider=None, evidence_source="bridge_requested_label",
        event_id="e1",
    ))
    usage = UsageStore(str(tmp_path / "usage.db"))
    assert ledger.replay_projection(usage) == 1
    assert ledger.replay_projection(usage) == 1
    with sqlite3.connect(usage.db_path) as conn:  # projection idempotency is part of the contract
        row = conn.execute(
            "SELECT served_model, requested_model, billing_class, COUNT(*) "
            "FROM usage WHERE usage_key=? GROUP BY usage_key", ("sess:a:t",)
        ).fetchone()
    assert row[0] is None
    assert row[1] == "paid-request"
    assert row[2] == "metered"
    assert row[3] == 1


def test_overlapping_projection_replays_materialize_one_legacy_row(tmp_path):
    _, ledger = _ledger(tmp_path)
    ledger.record(UsageObservation(
        "sess", "a", "replay-race", "hermes", input_tokens=1, output_tokens=1,
        input_kind=MEASURED, output_kind=MEASURED, cost_usd=.2,
        cost_kind=MEASURED, billing_class="metered", event_id="replay-race",
    ))
    usage = UsageStore(str(tmp_path / "usage.db"))
    barrier = threading.Barrier(2)

    def replay():
        barrier.wait()
        return ledger.replay_projection(usage)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: replay(), range(2)))

    with sqlite3.connect(usage.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1


def test_reservations_are_bounded_and_nonpositive_cap_pauses_every_route(tmp_path):
    _, ledger = _ledger(tmp_path)
    assert not ledger.reserve("sess:a:t", 1, daily_cap_dollars=0, today=date(2026, 1, 1))
    assert ledger.reserve("sess:a:t", .6, daily_cap_dollars=1, today=date(2026, 1, 1))
    assert not ledger.reserve("sess:a:t2", .5, daily_cap_dollars=1, today=date(2026, 1, 1))
    assert ledger.release("sess:a:t:2026-01-01")
    assert ledger.reserve("sess:a:t2", .5, daily_cap_dollars=1, today=date(2026, 1, 1))


def test_cli_estimate_keeps_requested_identity_and_subscription_out_of_billed_totals(tmp_path):
    _, ledger = _ledger(tmp_path)
    session = SimpleNamespace(
        session_id="sess", routing="codex", model="synthetic-codex-model",
        execution_spec={"attempt_id": "attempt-2", "turn_id": "turn-3"},
    )
    ledger.record_cli_usage(
        session, source="codex_executor", input_tokens=12, output_tokens=4,
        cached_input_tokens=2, estimated_cost_usd=.42, event_id="cli-event",
    )
    row = ledger.get("sess:attempt-2:turn-3")
    assert row["requested_model"] == "synthetic-codex-model"
    assert row["served_model"] is None
    assert row["billing_class"] == "subscription"
    assert row["estimated_cost_usd"] == .42
    assert row["billed_cost_usd"] == 0


def test_admission_retry_is_idempotent_and_concurrent_claims_are_bounded(tmp_path):
    _, ledger = _ledger(tmp_path)
    results = []
    barrier = threading.Barrier(2)

    def admit(worker_id):
        barrier.wait()
        results.append(ledger.reserve(
            "task-a", .75, daily_cap_dollars=1,
            reservation_id=f"admission:task-a:{worker_id}", today=date(2026, 1, 3),
        ))

    threads = [threading.Thread(target=admit, args=(worker_id,)) for worker_id in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True, True]
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM usage_reservations WHERE usage_key='task-a'"
        ).fetchone()[0] == 1
    assert not ledger.reserve(
        "task-b", .3, daily_cap_dollars=1,
        reservation_id="admission:task-b", today=date(2026, 1, 3),
    )


def test_admission_conflicting_retry_for_held_usage_key_is_rejected(tmp_path):
    _, ledger = _ledger(tmp_path)
    assert ledger.reserve(
        "task-a", .75, daily_cap_dollars=2,
        reservation_id="admission:task-a:first", today=date(2026, 1, 3),
    )
    assert not ledger.reserve(
        "task-a", .8, daily_cap_dollars=2,
        reservation_id="admission:task-a:retry", today=date(2026, 1, 3),
    )


def test_prior_day_hold_is_released_for_same_usage_key_retry(tmp_path):
    _, ledger = _ledger(tmp_path)
    assert ledger.reserve(
        "task-a", .75, daily_cap_dollars=1,
        reservation_id="admission:task-a:first", today=date(2026, 1, 3),
    )
    assert ledger.reserve(
        "task-a", .75, daily_cap_dollars=1,
        reservation_id="admission:task-a:retry", today=date(2026, 1, 4),
    )
    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            "SELECT reservation_id, date, status FROM usage_reservations "
            "WHERE usage_key='task-a' ORDER BY reservation_id"
        ).fetchall()
    assert rows == [
        ("admission:task-a:first", "2026-01-03", "released"),
        ("admission:task-a:retry", "2026-01-04", "held"),
    ]
    assert ledger.daily_readout(today=date(2026, 1, 3))["reserved_usd"] == 0


def test_correction_releases_the_admission_row(tmp_path):
    _, ledger = _ledger(tmp_path)
    ledger.reserve("task-a", 1, daily_cap_dollars=2, reservation_id="admission:task-a")
    ledger.record(UsageObservation(
        "sess", "a", "t", "cli", input_tokens=1, output_tokens=1,
        cost_kind=UNKNOWN, billing_class="subscription",
        reservation_id="admission:task-a", event_id="unknown",
    ))
    ledger.record(UsageObservation(
        "sess", "a", "t", "cli", input_tokens=1, output_tokens=1,
        cost_usd=.2, cost_kind=MEASURED, billing_class="subscription",
        correction=True, reservation_id="admission:task-a", event_id="corrected",
    ))
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM usage_reservations WHERE reservation_id='admission:task-a'"
        ).fetchone()[0] == "released"


def test_out_of_order_snapshot_does_not_regress_high_water_mark(tmp_path):
    _, ledger = _ledger(tmp_path)
    common = dict(
        session_id="sess", attempt_id="a", turn_id="ordered", source="managed",
        input_kind=MEASURED, output_kind=MEASURED, cost_kind=MEASURED,
        billing_class="metered", cumulative=True,
    )
    ledger.record(UsageObservation(**common, input_tokens=10, output_tokens=2,
                                   cost_usd=.1, observed_at=200, event_id="new"))
    ledger.record(UsageObservation(**common, input_tokens=5, output_tokens=1,
                                   cost_usd=.05, observed_at=100, event_id="old"))
    ledger.record(UsageObservation(**common, input_tokens=15, output_tokens=3,
                                   cost_usd=.15, observed_at=300, event_id="latest"))
    row = ledger.get("sess:a:ordered")
    assert (row["input_tokens"], row["output_tokens"], row["billed_cost_usd"]) == (15, 3, .15)


def test_older_measured_correction_resets_unknown_snapshot_high_water(tmp_path):
    sessions, ledger = _ledger(tmp_path)
    current = sessions.begin_executor_turn("task", "execute")
    common = dict(
        session_id="sess", attempt_id=current.attempt_id, turn_id=current.turn_id,
        source="managed",
        input_kind=MEASURED, output_kind=MEASURED, cost_kind=MEASURED,
        billing_class="metered", cumulative=True,
    )
    ledger.record(UsageObservation(
        **{**common, "input_tokens": 100, "output_tokens": 10,
           "cost_kind": UNKNOWN, "observed_at": 200, "event_id": "unknown-200"},
    ))
    correction = ledger.record(UsageObservation(
        **{**common, "input_tokens": 100, "output_tokens": 10,
           "cost_usd": .10, "observed_at": 100, "correction": True,
           "event_id": "measured-100"},
    ))
    corrected_row = ledger.get(f"sess:{current.attempt_id}:{current.turn_id}")
    assert corrected_row["last_snapshot_at"] == 100
    assert corrected_row["last_snapshot_cost"] == pytest.approx(.10)
    latest = ledger.record(UsageObservation(
        **{**common, "input_tokens": 130, "output_tokens": 13,
           "cost_usd": .13, "observed_at": 300, "event_id": "measured-300"},
    ))

    row = ledger.get(f"sess:{current.attempt_id}:{current.turn_id}")
    assert correction.corrected
    assert (latest.input_delta, latest.output_delta) == (30, 3)
    assert latest.cost_delta_usd == pytest.approx(.03)
    assert row["billed_cost_usd"] == pytest.approx(.13)
    assert row["last_snapshot_at"] == 300
    assert row["last_snapshot_cost"] == pytest.approx(.13)
    refreshed = sessions.get("task")
    assert (refreshed.total_input_tokens, refreshed.total_output_tokens) == (130, 13)
    assert refreshed.total_dollars == pytest.approx(.13)


def test_daily_readout_on_fresh_day_is_zero(tmp_path):
    _, ledger = _ledger(tmp_path)
    assert ledger.daily_readout(today=date(2099, 12, 31)) == {
        "date": "2099-12-31", "billed_usd": 0.0, "reserved_usd": 0.0,
        "cap_usd": None, "paused": False,
    }
