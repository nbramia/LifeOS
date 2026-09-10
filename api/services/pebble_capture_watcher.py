"""Serialized Pebble archive watcher with debounced, coalesced recovery."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from api.services.pebble_capture import PebbleCaptureConsumer, parse_framed_blocks, process_sync

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2.0
_MAX_PENDING_PATHS = 256


def _eligible(path: Path, root: Path) -> bool:
    """Return whether ``path`` is one regular archive file directly in ``root``."""
    try:
        root = root.resolve()
        return (
            path.suffix == ".md"
            and ".sync-conflict-" not in path.name
            and not path.name.startswith(".syncthing.")
            and not path.is_symlink()
            and path.resolve().parent == root
            and path.is_file()
        )
    except (OSError, UnicodeError):
        return False


class _PebbleHandler(FileSystemEventHandler):
    """Watchdog adapter; all actual work belongs to the one consumer thread."""

    def __init__(self, changed: Callable[[Path], None]):
        self.changed = changed

    def _queue(self, raw: str) -> None:
        self.changed(Path(raw))

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory:
            self._queue(event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self._queue(event.src_path)

    def on_moved(self, event: FileSystemEvent):
        if not event.is_directory:
            self._queue(event.dest_path)


class PebbleCaptureWatcher:
    """Consume producer blocks on one bounded, stoppable processing thread.

    File events only update a bounded map of path deadlines. Overflow and
    periodic/startup recovery collapse into one boolean scan request, so an
    event storm cannot create a timer or inference thread per file. ``stop``
    rejects new work, cancels queued work, and waits for the currently active
    consumer call before returning.
    """

    def __init__(
        self,
        capture_dir: Path,
        consumer: PebbleCaptureConsumer,
        *,
        scan_seconds: float = 60,
        debounce_seconds: float = _DEBOUNCE_SECONDS,
        max_pending_paths: int = _MAX_PENDING_PATHS,
    ):
        if scan_seconds <= 0 or debounce_seconds < 0 or max_pending_paths < 1:
            raise ValueError("watcher timing and queue bounds must be positive")
        self.capture_dir = Path(capture_dir).resolve()
        self.consumer = consumer
        self.scan_seconds = scan_seconds
        self.debounce_seconds = debounce_seconds
        self.max_pending_paths = max_pending_paths
        self._observer: Optional[Observer] = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._condition = threading.Condition()
        self._pending: dict[Path, float] = {}
        self._recovery_requested = False
        self._accepting = False
        self._stopping = False

    def enqueue(self, path: Path) -> None:
        """Debounce one eligible event or request a bounded recovery scan."""
        path = Path(path)
        if not _eligible(path, self.capture_dir):
            return
        with self._condition:
            if not self._accepting:
                return
            deadline = time.monotonic() + self.debounce_seconds
            if path in self._pending or len(self._pending) < self.max_pending_paths:
                self._pending[path] = deadline
            else:
                self._recovery_requested = True
            self._condition.notify()

    def request_scan(self) -> None:
        """Coalesce any number of recovery requests into one pending scan."""
        with self._condition:
            if not self._accepting:
                return
            self._recovery_requested = True
            self._condition.notify()

    def process_file(self, path: Path) -> None:
        """Process a file synchronously on the caller's thread."""
        try:
            if not _eligible(path, self.capture_dir):
                return
            # Preserve bytes so universal-newline handling cannot make a CRLF
            # frame valid and a torn UTF-8 tail cannot hide later frames.
            payloads = parse_framed_blocks(path.read_bytes())
            for payload in payloads:
                with self._condition:
                    if self._stopping:
                        return
                try:
                    process_sync(self.consumer, payload)
                except Exception as exc:
                    # No transcript, model output, or content-bearing path.
                    logger.warning("Pebble capture remains pending (%s)", type(exc).__name__)
        except (OSError, UnicodeError) as exc:
            logger.warning("Pebble capture file could not be read (%s)", type(exc).__name__)

    def _recover_once(self) -> None:
        try:
            with os.scandir(self.capture_dir) as entries:
                for entry in entries:
                    with self._condition:
                        if self._stopping:
                            return
                    path = Path(entry.path)
                    if _eligible(path, self.capture_dir):
                        self.process_file(path)
        except OSError as exc:
            logger.warning("Pebble capture recovery scan failed (%s)", type(exc).__name__)

    def _next_work(self, next_scan: float) -> tuple[str, Optional[Path], float]:
        """Wait for one path, one scan token, or shutdown."""
        with self._condition:
            while True:
                if self._stopping:
                    self._pending.clear()
                    self._recovery_requested = False
                    return "stop", None, next_scan

                now = time.monotonic()
                if self._recovery_requested or now >= next_scan:
                    self._recovery_requested = False
                    return "scan", None, now + self.scan_seconds

                ready = [path for path, deadline in self._pending.items() if deadline <= now]
                if ready:
                    path = min(ready, key=lambda item: (self._pending[item], str(item)))
                    self._pending.pop(path, None)
                    return "path", path, next_scan

                wake_at = next_scan
                if self._pending:
                    wake_at = min(wake_at, min(self._pending.values()))
                self._condition.wait(timeout=max(0, wake_at - now))

    def _run(self) -> None:
        next_scan = time.monotonic() + self.scan_seconds
        while True:
            kind, path, next_scan = self._next_work(next_scan)
            if kind == "stop":
                return
            if kind == "scan":
                self._recover_once()
            elif path is not None:
                self.process_file(path)

    def start(self) -> None:
        """Start event delivery and queue startup recovery without scanning inline."""
        with self._condition:
            if self._accepting:
                return
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            self._pending.clear()
            self._stopping = False
            self._accepting = True
            self._recovery_requested = True

        observer = Observer()
        observer.schedule(_PebbleHandler(self.enqueue), str(self.capture_dir), recursive=False)
        try:
            observer.start()
            worker = threading.Thread(target=self._run, name="PebbleCaptureConsumer", daemon=True)
            worker.start()
        except BaseException:
            observer.stop()
            observer.join()
            with self._condition:
                self._accepting = False
                self._stopping = True
                self._condition.notify_all()
            raise
        self._observer = observer
        self._consumer_thread = worker
        logger.info("Pebble capture watcher started")

    def stop(self) -> None:
        """Cancel queued work and wait until the active consumer has drained."""
        with self._condition:
            self._accepting = False
            self._stopping = True
            self._pending.clear()
            self._recovery_requested = False
            self._condition.notify_all()

        observer, worker = self._observer, self._consumer_thread
        if observer is not None:
            observer.stop()
            observer.join()
        if worker is not None and worker is not threading.current_thread():
            worker.join()

        self._observer = None
        self._consumer_thread = None

    def is_alive(self) -> bool:
        observer, worker = self._observer, self._consumer_thread
        with self._condition:
            running = self._accepting and not self._stopping
        return bool(running and observer and observer.is_alive() and worker and worker.is_alive())
