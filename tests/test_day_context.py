"""Day-context job (api/services/day_context.py). Every source and the writer
are fakes with obviously synthetic values; nothing touches a network or DB."""
import logging
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from api.services import day_context
from config.settings import settings

pytestmark = pytest.mark.unit

NY = day_context.NY
SUBJECT = "synthetic|subject-0001"
SECRET_REASON = "synthetic-reason-text"
SECRET_RESONANT = "synthetic-resonant-text"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "day_context_database_url", "postgresql://writer@db.invalid/x")
    monkeypatch.setattr(settings, "dailies_database_url", "postgresql://reader@db.invalid/x")


def moment(confidence=0.9, path=("Fearful", "Anxious")):
    return {"path": list(path), "valence": "negative", "reason": SECRET_REASON,
            "confidence": confidence, "at": "15:04"}


class Writer:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, dsn, subject, rows, cutoff):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((dsn, subject, rows, cutoff))


def run(now=None, subjects=(SUBJECT,), sleep=None, signals=None, writer=None):
    writer = writer or Writer()
    result = day_context.run_once(
        now=now or datetime(2026, 11, 10, 12, 0, tzinfo=NY),
        fetch_subjects_fn=lambda dsn: list(subjects),
        sleep_fn=sleep or (lambda d: None),
        signals_fn=signals or (lambda d: None),
        write_fn=writer,
    )
    return result, writer


def test_logical_day_rolls_back_before_three_am():
    assert day_context.logical_day(datetime(2026, 11, 10, 2, 59, tzinfo=NY)) == date(2026, 11, 9)
    assert day_context.logical_day(datetime(2026, 11, 10, 3, 0, tzinfo=NY)) == date(2026, 11, 10)
    assert day_context.target_days(datetime(2026, 11, 10, 12, tzinfo=NY)) == [date(2026, 11, 10), date(2026, 11, 9)]


def test_writes_both_days_with_sleep_and_signals_and_prunes():
    sleeps = {date(2026, 11, 10): 6.64}
    signals = {date(2026, 11, 10): {
        "emotional_moment": moment(), "resonant": SECRET_RESONANT, "words": ["Steady", "Busy"]}}
    result, writer = run(sleep=sleeps.get, signals=signals.get)
    assert result == {"status": "ok", "written": 1}
    dsn, subject, rows, cutoff = writer.calls[0]
    assert dsn == settings.day_context_database_url and subject == SUBJECT
    assert cutoff == date(2026, 10, 27)
    assert rows == [{
        "entry_date": date(2026, 11, 10), "sleep_hours": 6.64, "sleep_source": "apple_watch",
        "feelings": [{"path": ["Fearful", "Anxious"], "reason": SECRET_REASON}],
        "resonant": SECRET_RESONANT, "words": ["Steady", "Busy"],
    }]


def test_low_confidence_moment_yields_no_feelings():
    signals = {date(2026, 11, 10): {"emotional_moment": moment(0.79), "words": ["Calm"]}}
    _, writer = run(signals=signals.get)
    assert writer.calls[0][2][0]["feelings"] == []
    assert writer.calls[0][2][0]["sleep_hours"] is None
    assert writer.calls[0][2][0]["sleep_source"] is None


def test_confidence_boundary_and_bad_paths():
    assert day_context._feelings({"emotional_moment": moment(0.8)})
    assert not day_context._feelings({"emotional_moment": moment(path=())})
    assert not day_context._feelings({"emotional_moment": moment(path=("a", "b", "c", "d"))})
    assert not day_context._feelings({"emotional_moment": moment(path=("a", 3))})
    assert not day_context._feelings({"emotional_moment": None})


def test_limits_are_enforced():
    row = day_context.build_row(date(2026, 11, 10), None, {
        "resonant": "x" * 500, "words": ["a", "b", "c", "d", "w" * 41, 7]})
    assert len(row["resonant"]) == 280
    assert row["words"] == ["a", "b", "c"]


def test_missing_sources_write_nothing_but_still_prune():
    result, writer = run()
    assert result == {"status": "ok", "written": 0}
    assert writer.calls[0][2] == []


def test_a_source_that_raises_is_treated_as_no_signal(caplog):
    def boom(d):
        raise RuntimeError(SECRET_REASON)
    with caplog.at_level(logging.DEBUG):
        result, writer = run(sleep=boom, signals=boom)
    assert result["status"] == "ok" and writer.calls[0][2] == []
    assert SECRET_REASON not in caplog.text


@pytest.mark.parametrize("subjects", [(), ("a", "b")])
def test_subject_must_be_exactly_one(subjects):
    result, writer = run(subjects=subjects)
    assert result["reason"] == "subject_not_unique" and writer.calls == []


def test_subject_lookup_failure_skips():
    def fail(dsn):
        raise RuntimeError("down")
    result = day_context.run_once(fetch_subjects_fn=fail, write_fn=Writer())
    assert result["reason"] == "subject_lookup_failed"


def test_unset_writer_url_disables_job(monkeypatch):
    monkeypatch.setattr(settings, "day_context_database_url", "")
    result, writer = run()
    assert result["status"] == "disabled" and writer.calls == []
    day_context.start()
    assert day_context._task is None


def test_missing_reader_url_skips(monkeypatch):
    monkeypatch.setattr(settings, "dailies_database_url", "")
    result, _ = run()
    assert result["reason"] == "dailies_db_not_configured"


def test_write_failure_logs_category_only(caplog):
    with caplog.at_level(logging.DEBUG):
        result, _ = run(sleep=lambda d: 7.5, writer=Writer(fail=True))
    assert result == {"status": "skipped", "reason": "write_failed", "written": 0}
    assert "7.5" not in caplog.text


class FakeStore:
    def __init__(self, metrics):
        self.metrics, self.args = metrics, None

    def list_metrics(self, metric_type, start=None, end=None, limit=100):
        self.args = (metric_type, start, end, limit)
        return self.metrics


def test_sleep_reads_wake_up_date_row():
    store = FakeStore([SimpleNamespace(value=6.6449)])
    assert day_context.sleep_hours_for(date(2026, 6, 14), store) == 6.64
    assert store.args == ("sleep_hours", "2026-06-14", "2026-06-14T23:59:59", 1)
    assert day_context.sleep_hours_for(date(2026, 6, 14), FakeStore([])) is None
    assert day_context.sleep_hours_for(date(2026, 6, 14), FakeStore([SimpleNamespace(value=30)])) is None


def test_pendant_fetch_sends_token_and_treats_errors_as_no_signal(monkeypatch):
    import urllib.error
    import urllib.request
    monkeypatch.setattr(settings, "pendant_feed_token", "synthetic-token")
    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"day": "2026-11-10", "words": ["Calm"]}'

    def ok(request, timeout):
        seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", ok)
    assert day_context.fetch_pendant_signals(date(2026, 11, 10))["words"] == ["Calm"]
    assert seen["url"].endswith("/pendant/feed/day-signals?day=2026-11-10")
    assert seen["auth"] == "Bearer synthetic-token"

    def not_found(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", not_found)
    assert day_context.fetch_pendant_signals(date(2026, 11, 10)) is None
    monkeypatch.setattr(urllib.request, "urlopen", lambda r, timeout: (_ for _ in ()).throw(ConnectionRefusedError()))
    assert day_context.fetch_pendant_signals(date(2026, 11, 10)) is None


def test_writer_upserts_and_prunes_in_one_connection(monkeypatch):
    import psycopg
    executed = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=()):
            executed.append((sql, params))

    monkeypatch.setattr(psycopg, "connect", lambda dsn, connect_timeout: Conn())
    row = day_context.build_row(date(2026, 11, 10), 7.0, {"words": ["Calm"]})
    day_context.write_rows("dsn", SUBJECT, [row], date(2026, 10, 27))
    assert executed[0][0] == day_context.UPSERT_SQL
    assert executed[0][1][:4] == (SUBJECT, date(2026, 11, 10), 7.0, "apple_watch")
    assert executed[1] == (day_context.PRUNE_SQL, (date(2026, 10, 27),))
