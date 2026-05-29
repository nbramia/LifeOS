"""
File watcher for the LifeOS Scheduler directory.

Watches ``LifeOS/Scheduler/*.md`` and calls ``SchedulerStore.reindex_file()``
when files change, so external edits (e.g. via Obsidian) propagate to the
scheduler index and the auto-generated dashboard without a manual reindex.

Scoped narrowly to the Scheduler/ directory. The auto-generated Dashboard.md
and the Scheduler.md control file are never reindexed as schedule sources.
"""
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2.0
_GENERATED_FILES = ("Dashboard.md", "Scheduler.md")


class _SchedulerFileHandler(FileSystemEventHandler):
    """Coalesces rapid file events per path and forwards a single reindex call."""

    def __init__(self, on_change: Callable[[str], None]):
        self._on_change = on_change
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str):
        if path.endswith(_GENERATED_FILES):
            return  # generated/control files are never schedule sources
        with self._lock:
            existing = self._pending.pop(path, None)
            if existing:
                existing.cancel()
            timer = threading.Timer(_DEBOUNCE_SECONDS, self._fire, args=(path,))
            timer.daemon = True
            self._pending[path] = timer
            timer.start()

    def _fire(self, path: str):
        with self._lock:
            self._pending.pop(path, None)
        try:
            self._on_change(path)
        except Exception as e:
            logger.warning(f"Scheduler file reindex failed for {path}: {e}")

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._schedule(event.src_path)

    def on_deleted(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent):
        if event.is_directory:
            return
        src = getattr(event, "src_path", "")
        dst = getattr(event, "dest_path", "")
        if src.endswith(".md"):
            self._schedule(src)
        if dst.endswith(".md"):
            self._schedule(dst)


class SchedulerWatcher:
    """Watches the Scheduler directory and forwards changes to SchedulerStore."""

    def __init__(
        self,
        scheduler_dir: Path,
        on_change: Optional[Callable[[str], None]] = None,
    ):
        self.scheduler_dir = Path(scheduler_dir)
        self._observer: Optional[Observer] = None
        self._on_change = on_change or self._default_on_change

    @staticmethod
    def _default_on_change(path: str) -> None:
        from api.services.scheduler_store import get_scheduler_store
        get_scheduler_store().reindex_file(path)

    def start(self) -> None:
        if self._observer is not None:
            return
        if not self.scheduler_dir.exists():
            logger.warning(f"Scheduler watcher: directory missing {self.scheduler_dir}")
            return
        handler = _SchedulerFileHandler(self._on_change)
        observer = Observer()
        observer.schedule(handler, str(self.scheduler_dir), recursive=False)
        observer.start()
        self._observer = observer
        logger.info(f"Scheduler watcher started for {self.scheduler_dir}")

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
