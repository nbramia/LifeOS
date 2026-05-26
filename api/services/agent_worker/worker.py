"""Main poll loop for the LifeOS agent worker.

Issue B scope (this file): claim `#agent` tasks atomically via the API,
record a session row + transcript event, run a no-op dispatcher, mark the
session complete, and notify via Telegram. Later issues replace the no-op
dispatcher with real LLM execution (C/D), inter-agent coordination (E), and
the clarification round-trip (F).

The worker is a stand-alone process (`python -m api.services.agent_worker.worker`)
managed by the `lifeos-agent-worker.service` systemd unit. It does not import
the FastAPI app — all task ops go through `/api/tasks`. This keeps the worker
trivially restartable and lets the API enforce its own locking.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

import httpx

from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from config.settings import settings


logger = logging.getLogger(__name__)


AGENT_TAG = "agent"
RUNNING_TAG = "agent-running"


class Worker:
    """Single-process poll loop. One instance per process."""

    def __init__(
        self,
        api_base: str | None = None,
        session_store: SessionStore | None = None,
        transcript_store: TranscriptStore | None = None,
        spend_tracker: SpendTracker | None = None,
        poll_seconds: float | None = None,
        telegram_send=None,  # injectable for tests
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_base = (api_base or os.environ.get("LIFEOS_API_URL", "http://localhost:8000")).rstrip("/")
        self.session_store = session_store or SessionStore()
        self.transcript_store = transcript_store or TranscriptStore()
        self.spend_tracker = spend_tracker or SpendTracker(
            daily_cap_dollars=settings.agent_daily_cap_dollars,
        )
        self.poll_seconds = poll_seconds if poll_seconds is not None else settings.agent_worker_poll_seconds
        self._stop = False
        # Telegram is optional — operator may not have configured it. The
        # worker should still run end-to-end without crashing on a missing bot.
        if telegram_send is None:
            def _noop_telegram(text, chat_id=None):
                return False
            try:
                from api.services.telegram import send_message
                telegram_send = send_message
            except Exception:  # pragma: no cover — defensive
                telegram_send = _noop_telegram
        self._telegram_send = telegram_send
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=10.0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Mark the loop for graceful shutdown. Safe to call from a signal."""
        self._stop = True

    def run(self) -> None:
        """Main loop. Runs until `stop()` is called or the process is killed."""
        logger.info(
            "agent worker starting (api=%s, poll=%ss, daily_cap=$%s)",
            self.api_base, self.poll_seconds, self.spend_tracker.daily_cap_dollars,
        )
        while not self._stop:
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover — loop guard
                logger.exception("worker tick failed: %s", exc)
            # Sleep in small slices so SIGTERM is honored promptly.
            slept = 0.0
            while slept < self.poll_seconds and not self._stop:
                step = min(1.0, self.poll_seconds - slept)
                time.sleep(step)
                slept += step
        logger.info("agent worker stopped")
        if self._owns_http_client:
            self._http.close()

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------

    def tick(self) -> int:
        """Process one poll cycle. Returns the number of tasks handled (for tests)."""
        if not self.spend_tracker.can_start_task(0.0):
            # Cap is zero or already exceeded — pause new claims this cycle.
            logger.info("daily spend cap reached; skipping poll cycle")
            return 0

        candidates = self._list_agent_tasks()
        handled = 0
        for task in candidates:
            task_id = task.get("id")
            if not task_id:
                continue
            # Skip tasks we've already claimed in a previous run.
            if self.session_store.get(task_id) is not None:
                continue
            if not self._claim(task_id):
                continue
            self._dispatch_noop(task)
            handled += 1
        return handled

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _list_agent_tasks(self) -> list[dict[str, Any]]:
        """Fetch open `#agent` tasks from the API."""
        try:
            resp = self._http.get(
                f"{self.api_base}/api/tasks",
                params={"status": "todo", "tag": AGENT_TAG},
            )
            resp.raise_for_status()
            return resp.json().get("tasks", [])
        except Exception as exc:
            logger.warning("failed to list agent tasks: %s", exc)
            return []

    def _swap_tag(self, task_id: str, from_tag: str, to_tag: str) -> bool:
        try:
            resp = self._http.post(
                f"{self.api_base}/api/tasks/{task_id}/swap-tag",
                params={"from": from_tag, "to": to_tag},
            )
            resp.raise_for_status()
            return bool(resp.json().get("swapped"))
        except Exception as exc:
            logger.warning("swap_tag failed for %s: %s", task_id, exc)
            return False

    def _complete_task(self, task_id: str) -> bool:
        try:
            resp = self._http.put(f"{self.api_base}/api/tasks/{task_id}/complete")
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("complete failed for %s: %s", task_id, exc)
            return False

    # ------------------------------------------------------------------
    # Claim + dispatch
    # ------------------------------------------------------------------

    def _claim(self, task_id: str) -> bool:
        """Atomically swap `#agent` → `#agent-running` and record the session.

        Returns True iff this worker won the race.
        """
        if not self._swap_tag(task_id, AGENT_TAG, RUNNING_TAG):
            return False
        try:
            session = self.session_store.create(
                task_id=task_id,
                status=STATUS_CLAIMED,
            )
            self.transcript_store.append(
                session.session_id,
                "claim",
                {"task_id": task_id, "worker": "agent-worker"},
            )
            return True
        except Exception as exc:
            # Already-claimed by a sibling worker, or DB hiccup. Try to un-do
            # the tag swap so the task remains pickable; if that also fails the
            # operator will see `#agent-running` and can manually re-tag.
            logger.error("session create failed for %s: %s", task_id, exc)
            self._swap_tag(task_id, RUNNING_TAG, AGENT_TAG)
            return False

    def _dispatch_noop(self, task: dict[str, Any]) -> None:
        """Placeholder dispatcher: marks the task complete without spending money.

        Replaced in later issues by the real router + executor.
        """
        task_id = task["id"]
        title = task.get("description", task_id)
        session = self.session_store.get(task_id)
        if session is None:
            logger.error("no session for claimed task %s — skipping", task_id)
            return
        sid = session.session_id

        self.session_store.update_status(task_id, STATUS_RUNNING)
        self.transcript_store.append(sid, "noop_dispatch", {"title": title})

        ok = self._complete_task(task_id)
        if ok:
            self.session_store.update_status(task_id, STATUS_COMPLETED)
            self.transcript_store.append(sid, "noop_complete", {"title": title})
            self._notify(f"✅ Agent worker (scaffolding): no-op completed task '{title}'")
        else:
            self.session_store.update_status(task_id, STATUS_FAILED)
            self.transcript_store.append(sid, "complete_failed", {"title": title})
            self._notify(f"⚠️ Agent worker (scaffolding): failed to mark task '{title}' complete")

    def _notify(self, text: str) -> None:
        try:
            self._telegram_send(text)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("telegram notify failed: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LIFEOS_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    worker = Worker()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s; stopping after current tick", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    worker.run()


if __name__ == "__main__":
    main()
