"""
Tests for the fitness Google Sheet mirror (issue #321).

Verifies the off-by-default guardrail, full-tab build from the store, the
content-hash short-circuit, dedup-by-rebuild, and graceful failure (no raise).
"""
import pytest

import api.services.fitness_sheet_mirror as mirror
from api.services.fitness_store import FitnessStore

pytestmark = pytest.mark.unit


class FakeSheets:
    """Records write calls instead of hitting Google."""
    def __init__(self):
        self.cleared = []
        self.updated = []
        self.ensured = []
        self.fail = False

    def ensure_sheets(self, sheet_id, titles):
        if self.fail:
            raise RuntimeError("403 insufficient scope")
        self.ensured.append((sheet_id, list(titles)))

    def clear_values(self, sheet_id, rng):
        self.cleared.append((sheet_id, rng))

    def update_values(self, sheet_id, rng, values):
        self.updated.append((sheet_id, rng, values))


@pytest.fixture(autouse=True)
def reset_mirror_state():
    mirror._last_hash = None
    mirror._running = False
    mirror._dirty = False
    yield
    mirror._last_hash = None
    mirror._running = False
    mirror._dirty = False


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A store with data + a fake sheets service + a configured sheet id."""
    store = FitnessStore(db_path=str(tmp_path / "fitness.db"))
    store.add_session(sets=[{"exercise": "bench", "reps": 8, "weight": 135}], date="2026-06-07")
    store.add_session(sets=[{"exercise": "squats", "reps": 5, "weight": 185, "count": 3}], date="2026-06-08")
    fake = FakeSheets()
    monkeypatch.setattr(mirror.settings, "fitness_sheet_id", "SHEET123")
    monkeypatch.setattr("api.services.fitness_store.get_fitness_store", lambda: store)
    monkeypatch.setattr("api.services.sheets.get_sheets_service", lambda *a, **k: fake)
    return store, fake


def test_disabled_when_no_sheet_id(monkeypatch):
    monkeypatch.setattr(mirror.settings, "fitness_sheet_id", "")
    assert mirror.mirror_enabled() is False
    assert mirror.sync() is False


def test_build_tabs_shapes(wired):
    store, _ = wired
    tabs = mirror.build_tabs(store)
    assert set(tabs) == {"Sessions", "Sets"}
    assert tabs["Sessions"][0] == mirror._SESSIONS_HEADER
    assert tabs["Sets"][0] == mirror._SETS_HEADER
    # 2 sessions + header
    assert len(tabs["Sessions"]) == 3
    # 1 + 3 set rows + header
    assert len(tabs["Sets"]) == 5
    # None rendered as empty string, not "None"
    for row in tabs["Sets"][1:]:
        assert "None" not in [str(c) for c in row]


def test_sync_writes_both_tabs(wired):
    _, fake = wired
    assert mirror.sync() is True
    written_tabs = {rng.split("!")[0] for _, rng, _ in fake.updated}
    assert written_tabs == {"Sessions", "Sets"}
    # cleared before writing
    assert len(fake.cleared) == 2


def test_hash_shortcircuits_second_sync(wired):
    _, fake = wired
    assert mirror.sync() is True
    assert mirror.sync() is False  # unchanged → no write
    assert len(fake.updated) == 2  # only the first sync wrote


def test_change_triggers_rewrite(wired):
    store, fake = wired
    mirror.sync()
    store.add_session(sets=[{"exercise": "deadlift", "reps": 5, "weight": 315}], date="2026-06-09")
    assert mirror.sync() is True
    assert len(fake.updated) == 4  # two more tab writes


def test_force_rewrites_even_if_unchanged(wired):
    mirror.sync()
    assert mirror.sync(force=True) is True


def test_failure_does_not_raise(wired):
    _, fake = wired
    fake.fail = True
    assert mirror.sync() is False  # swallowed, returns False


def test_trigger_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(mirror.settings, "fitness_sheet_id", "")
    # Should not raise or start a thread.
    mirror.trigger_mirror()


# -- background debounce/serialize guard --

def test_drain_reruns_sync_when_dirtied_midflight(monkeypatch):
    """A write arriving during a sync must trigger one more sync (no lost update)."""
    calls = []

    def fake_sync(force=False):
        calls.append(1)
        if len(calls) == 1:
            with mirror._state_lock:
                mirror._dirty = True  # simulate a log() landing mid-sync
        return True

    monkeypatch.setattr(mirror, "sync", fake_sync)
    mirror._dirty = True
    mirror._running = True
    mirror._drain()
    assert len(calls) == 2          # re-ran for the mid-flight write
    assert mirror._dirty is False   # drained
    assert mirror._running is False  # cleared on exit


def test_drain_stops_when_not_dirty(monkeypatch):
    calls = []
    monkeypatch.setattr(mirror, "sync", lambda force=False: calls.append(1))
    mirror._dirty = True
    mirror._running = True
    mirror._drain()
    assert len(calls) == 1
    assert mirror._running is False


def test_trigger_while_running_does_not_double_spawn(monkeypatch):
    monkeypatch.setattr(mirror.settings, "fitness_sheet_id", "SHEET")
    spawned = []

    class FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            spawned.append(1)  # do NOT run _drain — leave _running True

    monkeypatch.setattr(mirror.threading, "Thread", FakeThread)
    mirror._running = False
    mirror._dirty = False

    mirror.trigger_mirror()          # not running → spawns one drain
    assert spawned == [1]
    assert mirror._running is True

    mirror.trigger_mirror()          # already running → just mark dirty, no new thread
    assert spawned == [1]
    assert mirror._dirty is True
