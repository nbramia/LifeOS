"""Wake expired agent-board snoozes and notify the operator via Telegram."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from api.services import agent_board
from api.services.task_manager import TaskManager, get_task_manager
from config.settings import settings

logger = logging.getLogger(__name__)

# Telegram's legacy Markdown parse mode (what `send_message` uses) treats an
# unescaped backslash, underscore, asterisk, or opening bracket as the start
# of an entity. A card description is operator-authored free text, not
# markup, so each is escaped before being interpolated into the message —
# backslash first, so escaping the other four characters doesn't get
# reprocessed as more escape sequences.
_MARKDOWN_SPECIAL_CHARS = re.compile(r"([\\_*`\[])")


def _escape_markdown(text: str) -> str:
    return _MARKDOWN_SPECIAL_CHARS.sub(r"\\\1", text)


# Covers `send_message`'s own 30-second-per-part HTTP timeout plus the CAS
# write that follows a successful send, so `stop()` never cuts an in-flight
# delivery off mid-send just because shutdown asked the poll loop to end.
_SHUTDOWN_JOIN_TIMEOUT = 35.0


class _SnoozeChanged(Exception):
    """The card was re-snoozed or unsnoozed while its notification sent."""


class SnoozeNotifier:
    """Poll task fields for expired snoozes and deliver each wake-up.

    Delivery is at-least-once, not exactly-once: Telegram gives no
    transactional coupling between "the message was accepted" and "the
    wake-up field was cleared," so the two can't be made atomic. A send
    that's accepted is recorded in memory immediately; a later poll that
    finds the same (card, wake-up value) pair already recorded retries only
    the field-clearing write, never the send, for as long as this process
    keeps running. A duplicate is possible only if the process itself dies
    between a successful send and its clear — the in-memory record does not
    survive a restart, and the field is the only durable state.

    The clearing write carries an exact-value precondition, so a concurrent
    re-snooze or unsnooze is never clobbered by an older wake-up's clear.
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
        # (task_id, raw snoozed_until value) pairs whose wake-up message has
        # already been sent this process lifetime but whose field-clearing
        # write hasn't yet succeeded — see the at-least-once note above.
        self._delivered: set[tuple[str, str]] = set()

    def _manager(self) -> TaskManager:
        return self._task_manager or get_task_manager()

    def _sender(self) -> Callable[[str], bool]:
        if self._send is None:
            from api.services.telegram import send_message
            self._send = send_message
        return self._send

    def _clear_field(self, manager: TaskManager, task_id: str, raw_until: str) -> str:
        """Attempt to remove the now-stale `snoozed_until` value.

        Returns "cleared" on success, "changed" if an operator re-snoozed or
        unsnoozed the card in the meantime (its current value doesn't
        match `raw_until`, so there's nothing of this wake-up left to
        clear), or "failed" for any other write error — the caller keeps
        retrying a "failed" clear without re-sending.
        """
        def still_same_snooze(latest) -> None:
            if latest.fields.get(agent_board.SNOOZED_UNTIL_FIELD) != raw_until:
                raise _SnoozeChanged()

        try:
            manager.update(
                task_id,
                fields={agent_board.SNOOZED_UNTIL_FIELD: None},
                _precondition=still_same_snooze,
            )
        except _SnoozeChanged:
            logger.info("Card %s changed while its snooze notification sent", task_id)
            return "changed"
        except Exception:
            logger.exception(
                "Failed to clear snoozed_until for card %s; retrying the clear "
                "(not the send) on the next poll",
                task_id,
            )
            return "failed"
        return "cleared"

    def check_once(self, now: Optional[datetime] = None) -> int:
        """Notify all currently expired, still-eligible snoozes.

        Returns the number of cards whose delivery was completed (a message
        sent — this call or an earlier one — with its wake-up value
        successfully cleared) during this call. A card that's ineligible, a
        failed send, or a clear that keeps failing or loses a race to a
        concurrent re-snooze/unsnooze, none count.
        """
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

            delivery_key = (task.id, raw_until)

            if agent_board.natural_lane(task.status, task.tags) not in agent_board.SNOOZABLE_LANES:
                # Never eligible for a wake-up message. Clear the expired
                # value now rather than leaving it — otherwise a later,
                # unrelated edit that returns the card to an eligible lane
                # would replay a wake-up that already passed while the card
                # was ineligible for it.
                self._clear_field(manager, task.id, raw_until)
                self._delivered.discard(delivery_key)
                continue

            already_delivered = delivery_key in self._delivered
            if not already_delivered:
                text = f"⏰ LifeOS snooze ended\n\n{_escape_markdown(task.description)}"
                if not self._sender()(text):
                    continue
                self._delivered.add(delivery_key)

            result = self._clear_field(manager, task.id, raw_until)
            if result == "cleared":
                self._delivered.discard(delivery_key)
                sent += 1
            elif result == "changed":
                self._delivered.discard(delivery_key)
            # result == "failed": keep the delivery record so the next poll
            # retries only the clear, never the send.
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

    def stop(self, timeout: float = _SHUTDOWN_JOIN_TIMEOUT) -> None:
        self._stop_event.set()
        if self._thread is None:
            return
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            # A send (or the clear write behind it) is still in flight.
            # Leaving the reference in place keeps is_alive()/`/health`
            # truthful instead of claiming a still-running daemon thread
            # has stopped; the thread finishes and exits on its own once
            # the in-flight operation completes, since `_stop_event` is
            # already set.
            logger.warning(
                "Snooze notifier thread still running after stop() timed out "
                "(likely an in-flight Telegram send); leaving it to finish "
                "rather than reporting it stopped"
            )
            return
        self._thread = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
