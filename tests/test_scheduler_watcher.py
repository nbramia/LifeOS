"""
Tests for the SchedulerWatcher — verifies that external edits to scheduler
files propagate to SchedulerStore via reindex_file().
"""
import threading
import time

import pytest

from api.services.scheduler_store import SchedulerStore
from api.services.scheduler_watcher import SchedulerWatcher, _SchedulerFileHandler

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fast_debounce(monkeypatch):
    import api.services.scheduler_watcher as mod
    monkeypatch.setattr(mod, "_DEBOUNCE_SECONDS", 0.1)


@pytest.fixture
def store(tmp_path):
    return SchedulerStore(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "scheduler_index.json",
    )


class _FakeEvent:
    def __init__(self, src_path, is_directory=False, dest_path=None):
        self.src_path = src_path
        self.is_directory = is_directory
        if dest_path is not None:
            self.dest_path = dest_path


class TestHandlerScheduling:
    def test_debounces_rapid_changes_to_single_call(self):
        calls: list[str] = []
        event = threading.Event()

        def on_change(path):
            calls.append(path)
            event.set()

        handler = _SchedulerFileHandler(on_change)
        for _ in range(3):
            handler.on_modified(_FakeEvent("/x/Inbox.md"))

        assert event.wait(timeout=2.0), "handler never fired"
        time.sleep(0.15)
        assert calls == ["/x/Inbox.md"]

    def test_ignores_non_markdown(self):
        calls = []
        handler = _SchedulerFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Inbox.txt"))
        handler.on_modified(_FakeEvent("/x/notes.json"))
        time.sleep(0.2)
        assert calls == []

    def test_ignores_directory_events(self):
        calls = []
        handler = _SchedulerFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Scheduler", is_directory=True))
        time.sleep(0.2)
        assert calls == []

    def test_skips_dashboard_and_control(self):
        """Dashboard.md and Scheduler.md are generated — never schedule them."""
        calls = []
        handler = _SchedulerFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Scheduler/Dashboard.md"))
        handler.on_modified(_FakeEvent("/x/Scheduler/Scheduler.md"))
        time.sleep(0.2)
        assert calls == []

    def test_move_event_fires_for_both_paths(self):
        calls = []
        done = threading.Event()

        def on_change(path):
            calls.append(path)
            if len(calls) >= 2:
                done.set()

        handler = _SchedulerFileHandler(on_change)
        handler.on_moved(_FakeEvent("/x/Old.md", dest_path="/x/New.md"))
        assert done.wait(timeout=2.0)
        assert set(calls) == {"/x/Old.md", "/x/New.md"}


class TestEndToEnd:
    def test_external_edit_propagates_to_store(self, store):
        entry = store.create(name="Watch me", schedule_type="cron",
                             schedule_value="0 9 * * *", message_type="static",
                             message_content="x")
        assert store.get(entry.id).schedule_value == "0 9 * * *"

        watcher = SchedulerWatcher(scheduler_dir=store.scheduler_dir,
                                   on_change=store.reindex_file)
        watcher.start()
        try:
            content = store.inbox_path.read_text(encoding="utf-8")
            patched = content.replace("[cron:: 0 9 * * *]", "[cron:: 0 10 * * *]")
            store.inbox_path.write_text(patched, encoding="utf-8")

            for _ in range(50):
                if store.get(entry.id).schedule_value == "0 10 * * *":
                    break
                time.sleep(0.05)

            assert store.get(entry.id).schedule_value == "0 10 * * *"
        finally:
            watcher.stop()
