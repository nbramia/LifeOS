"""Wake expired agent-board snoozes and notify the operator via Telegram."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from api.services import agent_board
from api.services.task_manager import TaskManager, get_task_manager
from config.settings import settings

logger = logging.getLogger(__name__)


class _SnoozeChanged(Exception):
    """The card was re-snoozed or unsnoozed while its notification sent."""


class SnoozeNotifier:
    """Poll task fields for expired snoozes and deliver each wake-up once.

    The expired field is removed only after Telegram accepts the message.  The
    write has an exact-value precondition, so a concurrent re-snooze is never
    cleared by an older wake-up.
    """

    def __init__(
        self,
        task_manager: Optional[TaskManager] = None,
        send: Optional[Callable[[str], bool]] = None,
        poll_seconds: float = 30.0,
    ) -> None:
        self._task_manager = task_manager
        self._send = send
        self._poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _manager(self) -> TaskManager:
        return self._task_manager or get_task_manager()

    def _sender(self) -> Callable[[str], bool]:
        if self._send is None:
            from api.services.telegram import send_message
            self._send = send_message
        return self._send

    def check_once(self, now: Optional[datetime] = None) -> int:
        """Notify all currently expired snoozes; return successful sends."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        manager = self._manager()
        sent = 0

        for task in manager.list_tasks():
            raw_until = task.fields.get(agent_board.SNOOZED_UNTIL_FIELD)
            until = agent_board.parse_snoozed_until(raw_until)
            if until is None or until > current:
                continue
            if agent_board.natural_lane(task.status, task.tags) not in agent_board.SNOOZABLE_LANES:
                continue
            if not self._sender()(f"⏰ LifeOS snooze ended\n\n{task.description}"):
                continue

            def still_same_snooze(latest) -> None:
                if latest.fields.get(agent_board.SNOOZED_UNTIL_FIELD) != raw_until:
                    raise _SnoozeChanged()

            try:
                manager.update(
                    task.id,
                    fields={agent_board.SNOOZED_UNTIL_FIELD: None},
                    _precondition=still_same_snooze,
                )
            except _SnoozeChanged:
                logger.info("Card %s changed while its snooze notification sent", task.id)
                continue
            except Exception:
                # The message already sent, so `snoozed_until` staying set
                # means the next poll retries the clear (and re-sends) rather
                # than losing the card's dedup record silently. Caught here
                # rather than left to propagate so one card's write failure
                # doesn't stop the rest of this poll's already-expired cards
                # from being checked.
                logger.exception(
                    "Failed to clear snoozed_until for card %s after sending its wake-up notification",
                    task.id,
                )
                continue
            sent += 1
        return sent

    def start(self) -> None:
        if not settings.telegram_enabled:
            logger.info("Telegram not configured, snooze notifier not started")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="SnoozeNotifier")
        self._thread.start()
        logger.info("Agent-board snooze notifier started")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("Agent-board snooze notification check failed")
            self._stop_event.wait(self._poll_seconds)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

