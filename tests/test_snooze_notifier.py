"""Agent-board snooze expiry notifications."""
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from api.services.snooze_notifier import SnoozeNotifier
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def manager(tmp_path):
    return TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
    )


def test_expired_snooze_sends_telegram_notification_once(manager):
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    task = manager.create(
        "Review synthetic launch notes",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )
    messages = []
    notifier = SnoozeNotifier(manager, lambda text: messages.append(text) or True)

    assert notifier.check_once(now) == 1
    assert messages == ["⏰ LifeOS snooze ended\n\nReview synthetic launch notes"]
    assert "snoozed_until" not in manager.get(task.id).fields

    assert notifier.check_once(now) == 0
    assert len(messages) == 1


def test_failed_send_is_retried_without_clearing_snooze(manager):
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    wake = (now - timedelta(minutes=1)).isoformat()
    task = manager.create("Check synthetic report", fields={"snoozed_until": wake})
    notifier = SnoozeNotifier(manager, lambda _text: False)

    assert notifier.check_once(now) == 0
    assert manager.get(task.id).fields["snoozed_until"] == wake


def test_future_snooze_does_not_notify(manager):
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    manager.create(
        "Prepare synthetic agenda",
        fields={"snoozed_until": (now + timedelta(minutes=1)).isoformat()},
    )
    messages = []

    assert SnoozeNotifier(manager, lambda text: messages.append(text) or True).check_once(now) == 0
    assert messages == []


def test_concurrent_resnooze_is_not_cleared(manager):
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    task = manager.create(
        "Follow up on synthetic request",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )
    later = (now + timedelta(hours=2)).isoformat()

    def send(_text):
        manager.update(task.id, fields={"snoozed_until": later})
        return True

    assert SnoozeNotifier(manager, send).check_once(now) == 0
    assert manager.get(task.id).fields["snoozed_until"] == later


def test_concurrent_unsnooze_is_not_reintroduced(manager):
    """A full unsnooze (the field removed, not just changed) racing the
    delivery of its own wake-up must not have the field written back by
    the notifier's own clear."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    task = manager.create(
        "Draft synthetic proposal",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )

    def send(_text):
        manager.update(task.id, fields={"snoozed_until": None})
        return True

    assert SnoozeNotifier(manager, send).check_once(now) == 0
    assert "snoozed_until" not in manager.get(task.id).fields


def test_natural_lane_no_longer_snoozable_does_not_notify(manager):
    """A card whose status/tags moved it out of the snooze-eligible lanes
    before its wake-up time passed (e.g. resumed via a tag swap while
    snoozed) must not fire — a snooze can never surface a card that already
    left the lanes it was hiding it from. The now-stale wake-up value is
    cleared immediately rather than left behind."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    task = manager.create(
        "Ship synthetic release",
        status="in_progress",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )
    messages = []

    assert SnoozeNotifier(manager, lambda text: messages.append(text) or True).check_once(now) == 0
    assert messages == []
    assert "snoozed_until" not in manager.get(task.id).fields


def test_ineligible_at_expiry_does_not_replay_after_becoming_eligible_later(manager):
    """A card that was ineligible when its wake-up time passed must not fire
    even after an unrelated, later edit returns it to an eligible lane —
    the wake-up already passed while the card couldn't be notified for it,
    so nothing should replay it once the card resurfaces some other way."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    task = manager.create(
        "Resume synthetic deployment",
        status="in_progress",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )
    messages = []
    notifier = SnoozeNotifier(manager, lambda text: messages.append(text) or True)

    assert notifier.check_once(now) == 0
    assert messages == []
    assert "snoozed_until" not in manager.get(task.id).fields

    # An unrelated later edit (e.g. an external vault edit picked up by the
    # task watcher) returns the card to an eligible lane. Its old wake-up
    # value is already gone, so there's nothing left to replay.
    manager.update(task.id, status="todo")
    assert notifier.check_once(now) == 0
    assert messages == []


def test_snooze_expired_while_process_was_down_still_fires_on_next_poll(manager):
    """`check_once` carries no "since when" state of its own — it re-scans
    every task on each call — so a snooze whose wake-up time passed hours
    earlier, while nothing was polling, still fires exactly once the first
    time a poll runs again."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    task = manager.create(
        "Confirm synthetic vendor renewal",
        fields={"snoozed_until": (now - timedelta(hours=6)).isoformat()},
    )
    messages = []

    assert SnoozeNotifier(manager, lambda text: messages.append(text) or True).check_once(now) == 1
    assert messages == ["⏰ LifeOS snooze ended\n\nConfirm synthetic vendor renewal"]
    assert "snoozed_until" not in manager.get(task.id).fields


def test_update_failure_after_send_does_not_abort_the_rest_of_the_poll(manager):
    """A card whose clearing write fails for a reason other than a
    concurrent re-snooze (a disk error, say) must not crash the whole poll
    — the next already-expired card in the same call still gets checked."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    broken = manager.create(
        "Investigate synthetic outage",
        fields={"snoozed_until": (now - timedelta(minutes=5)).isoformat()},
    )
    healthy = manager.create(
        "File synthetic expense report",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )

    real_update = manager.update

    def flaky_update(task_id, **kwargs):
        if task_id == broken.id:
            raise RuntimeError("synthetic disk write failure")
        return real_update(task_id, **kwargs)

    manager.update = flaky_update
    messages = []

    sent = SnoozeNotifier(manager, lambda text: messages.append(text) or True).check_once(now)

    assert sent == 1
    assert len(messages) == 2
    assert manager.get(broken.id).fields["snoozed_until"] is not None
    assert "snoozed_until" not in manager.get(healthy.id).fields


def test_clear_failure_after_send_retries_only_the_clear_not_the_send(manager):
    """Delivery is at-least-once with a narrow window: once a send is
    accepted, this process never sends it again for as long as it keeps
    running, even across many polls where only the clear keeps failing —
    only the clear is retried."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    task = manager.create(
        "Renew synthetic certificate",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )
    real_update = manager.update
    failing = {"active": True}

    def flaky_update(task_id, **kwargs):
        if failing["active"]:
            raise RuntimeError("synthetic disk write failure")
        return real_update(task_id, **kwargs)

    manager.update = flaky_update
    messages = []
    notifier = SnoozeNotifier(manager, lambda text: messages.append(text) or True)

    assert notifier.check_once(now) == 0
    assert len(messages) == 1
    assert manager.get(task.id).fields["snoozed_until"] is not None

    # The clear keeps failing across further polls; still no resend.
    assert notifier.check_once(now) == 0
    assert len(messages) == 1

    # The clear starts working again — the retry succeeds, still with no
    # second Telegram message for the same wake-up.
    failing["active"] = False
    assert notifier.check_once(now) == 1
    assert len(messages) == 1
    assert "snoozed_until" not in manager.get(task.id).fields


def test_a_new_process_after_a_crash_may_resend_once_more(manager):
    """The in-memory delivery record does not survive a process restart —
    the one window in which this notifier's at-least-once contract permits
    a duplicate: the process dying between a successful send and its
    clear."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    manager.create(
        "Audit synthetic backup",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )

    def always_fail_update(*_args, **_kwargs):
        raise RuntimeError("synthetic disk write failure")

    manager.update = always_fail_update
    messages = []

    SnoozeNotifier(manager, lambda text: messages.append(text) or True).check_once(now)
    assert len(messages) == 1

    # A fresh process — a new SnoozeNotifier sharing no in-memory state —
    # has no record of the earlier send, so it resends once more.
    SnoozeNotifier(manager, lambda text: messages.append(text) or True).check_once(now)
    assert len(messages) == 2


def test_description_with_markdown_special_characters_is_escaped(manager):
    """The card description is operator-authored free text, not Telegram
    Markdown — an unescaped `*`, `_`, backtick, or `[` in a title must not
    be reinterpreted as formatting by Telegram's legacy Markdown parser."""
    now = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    manager.create(
        "Fix *urgent* `bug` in _prod_ near [brackets",
        fields={"snoozed_until": (now - timedelta(minutes=1)).isoformat()},
    )
    messages = []

    SnoozeNotifier(manager, lambda text: messages.append(text) or True).check_once(now)

    assert messages == [
        "⏰ LifeOS snooze ended\n\nFix \\*urgent\\* \\`bug\\` in \\_prod\\_ near \\[brackets"
    ]


@patch("api.services.snooze_notifier.settings")
def test_start_does_not_spin_a_thread_when_telegram_not_configured(mock_settings, manager):
    mock_settings.telegram_enabled = False
    notifier = SnoozeNotifier(manager, lambda _text: True)

    notifier.start()

    assert notifier.is_alive() is False


@patch("api.services.snooze_notifier.settings")
def test_start_and_stop_join_the_polling_thread(mock_settings, manager):
    mock_settings.telegram_enabled = True
    notifier = SnoozeNotifier(manager, lambda _text: True, poll_seconds=0.01)

    notifier.start()
    assert notifier.is_alive() is True

    notifier.stop()
    assert notifier.is_alive() is False


@patch("api.services.snooze_notifier.settings")
def test_stop_does_not_falsely_report_stopped_during_an_in_flight_send(mock_settings, manager):
    """`stop()` must not clear its thread reference (making `is_alive()`
    report False) while the thread is still actually running an in-flight
    send — only once it has genuinely exited."""
    mock_settings.telegram_enabled = True
    # The polling thread's own check_once() call uses the real clock (no
    # `now` is injected), so the fixture task's wake-up must already have
    # elapsed relative to wall-clock time, not the fixed instant other
    # tests use.
    real_now = datetime.now(timezone.utc)
    manager.create(
        "Escalate synthetic incident",
        fields={"snoozed_until": (real_now - timedelta(minutes=1)).isoformat()},
    )
    send_started = threading.Event()
    release_send = threading.Event()

    def blocking_send(_text):
        send_started.set()
        release_send.wait(timeout=5)
        return True

    notifier = SnoozeNotifier(manager, blocking_send, poll_seconds=0.01)
    notifier.start()
    assert send_started.wait(timeout=2), "send was never reached"

    # Ask it to stop with a deliberately too-short join — the send is still
    # blocked, so the thread cannot have exited yet.
    notifier.stop(timeout=0.05)
    assert notifier.is_alive() is True

    # Let the send finish and clean up for real.
    release_send.set()
    notifier.stop(timeout=2)
    assert notifier.is_alive() is False
