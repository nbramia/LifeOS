"""
Tests for the TaskWatcher — verifies that external edits to task files
propagate to TaskManager via reindex_file().
"""
import threading
import time

import pytest

from api.services.task_manager import TaskManager
from api.services.task_watcher import TaskWatcher, _TaskFileHandler

pytestmark = pytest.mark.unit


# Speed up tests — the production debounce is 2s; we want sub-second.
@pytest.fixture(autouse=True)
def _fast_debounce(monkeypatch):
    import api.services.task_watcher as mod
    monkeypatch.setattr(mod, "_DEBOUNCE_SECONDS", 0.1)


@pytest.fixture
def tm(tmp_path):
    return TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
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

        handler = _TaskFileHandler(on_change)
        # Fire three rapid 'modified' events for the same file
        for _ in range(3):
            handler.on_modified(_FakeEvent("/x/Inbox.md"))

        assert event.wait(timeout=2.0), "handler never fired"
        # Tiny grace period to ensure no second timer slipped in
        time.sleep(0.15)
        assert calls == ["/x/Inbox.md"]

    def test_ignores_non_markdown_files(self):
        calls = []
        handler = _TaskFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Inbox.txt"))
        handler.on_modified(_FakeEvent("/x/notes.json"))
        time.sleep(0.2)
        assert calls == []

    def test_ignores_directory_events(self):
        calls = []
        handler = _TaskFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Tasks", is_directory=True))
        time.sleep(0.2)
        assert calls == []

    def test_skips_dashboard_md(self):
        """Reindexing Dashboard.md would be wasteful — never schedule it."""
        calls = []
        handler = _TaskFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Tasks/Dashboard.md"))
        time.sleep(0.2)
        assert calls == []

    def test_skips_sync_conflict_file(self):
        """#853: a Syncthing conflict copy is never a reindex source."""
        calls = []
        handler = _TaskFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Tasks/Inbox.sync-conflict-20260101-120000-ABCDEFG.md"))
        time.sleep(0.2)
        assert calls == []

    def test_skips_syncthing_temp_file(self):
        """#853: a Syncthing in-progress temp file is never a reindex source."""
        calls = []
        handler = _TaskFileHandler(lambda p: calls.append(p))
        handler.on_modified(_FakeEvent("/x/Tasks/.syncthing.Inbox.md.tmp"))
        time.sleep(0.2)
        assert calls == []

    def test_move_event_fires_for_both_paths(self):
        calls = []
        done = threading.Event()

        def on_change(path):
            calls.append(path)
            if len(calls) >= 2:
                done.set()

        handler = _TaskFileHandler(on_change)
        handler.on_moved(_FakeEvent("/x/Old.md", dest_path="/x/New.md"))
        assert done.wait(timeout=2.0)
        assert set(calls) == {"/x/Old.md", "/x/New.md"}


class TestEndToEnd:
    def test_external_file_edit_propagates_to_task_manager(self, tm, tmp_path):
        # Seed: one task, no tags
        task = tm.create("touch me")
        assert tm.get(task.id).tags == []

        watcher = TaskWatcher(tasks_dir=tm.tasks_dir, on_change=tm.reindex_file)
        watcher.start()
        try:
            # Externally rewrite the file to add a tag to the existing task line
            inbox = tm.tasks_dir / "Inbox.md"
            content = inbox.read_text(encoding="utf-8")
            assert task.id in content
            patched = content.replace(
                f"<!-- id:{task.id} -->",
                f"#external <!-- id:{task.id} -->",
            )
            inbox.write_text(patched, encoding="utf-8")

            # Wait for debounce + reindex
            for _ in range(50):
                refreshed = tm.get(task.id)
                if refreshed and "external" in refreshed.tags:
                    break
                time.sleep(0.05)

            assert "external" in tm.get(task.id).tags
        finally:
            watcher.stop()

    def test_dashboard_regenerates_after_external_edit(self, tm):
        task = tm.create("untagged")
        dashboard = tm.tasks_dir / "Dashboard.md"
        # Before: no #flagged section
        assert "### #flagged" not in dashboard.read_text(encoding="utf-8")

        watcher = TaskWatcher(tasks_dir=tm.tasks_dir, on_change=tm.reindex_file)
        watcher.start()
        try:
            inbox = tm.tasks_dir / "Inbox.md"
            patched = inbox.read_text(encoding="utf-8").replace(
                f"<!-- id:{task.id} -->",
                f"#flagged <!-- id:{task.id} -->",
            )
            inbox.write_text(patched, encoding="utf-8")

            for _ in range(50):
                if "### #flagged" in dashboard.read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)

            assert "### #flagged" in dashboard.read_text(encoding="utf-8")
        finally:
            watcher.stop()
