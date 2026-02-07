"""
Tests for the Reminder Store and Scheduler.

Tests CRUD operations, cron computation, auto-disable, and scheduling.
"""
import json
import pytest
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

pytestmark = pytest.mark.unit


class TestReminderStore:
    """Tests for ReminderStore CRUD operations."""

    @pytest.fixture
    def store(self, tmp_path):
        from api.services.reminder_store import ReminderStore
        return ReminderStore(file_path=str(tmp_path / "test_reminders.json"))

    def test_create_reminder(self, store):
        reminder = store.create(
            name="Test Reminder",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            message_type="static",
            message_content="Hello!",
        )
        assert reminder.id
        assert reminder.name == "Test Reminder"
        assert reminder.enabled is True
        assert reminder.created_at

    def test_get_reminder(self, store):
        created = store.create(
            name="Test",
            schedule_type="once",
            schedule_value=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            message_type="static",
            message_content="Hi",
        )
        found = store.get(created.id)
        assert found is not None
        assert found.name == "Test"

    def test_get_nonexistent_reminder(self, store):
        assert store.get("nonexistent") is None

    def test_list_all_reminders(self, store):
        store.create(name="A", schedule_type="cron", schedule_value="0 9 * * *",
                     message_type="static", message_content="a")
        store.create(name="B", schedule_type="cron", schedule_value="0 10 * * *",
                     message_type="static", message_content="b")
        reminders = store.list_all()
        assert len(reminders) == 2

    def test_update_reminder(self, store):
        created = store.create(
            name="Original",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            message_type="static",
            message_content="Hello",
        )
        updated = store.update(created.id, name="Updated", message_content="Bye")
        assert updated is not None
        assert updated.name == "Updated"
        assert updated.message_content == "Bye"

    def test_update_nonexistent_reminder(self, store):
        assert store.update("nonexistent", name="X") is None

    def test_delete_reminder(self, store):
        created = store.create(
            name="Delete Me",
            schedule_type="once",
            schedule_value=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            message_type="static",
            message_content="Hi",
        )
        assert store.delete(created.id) is True
        assert store.get(created.id) is None

    def test_delete_nonexistent_reminder(self, store):
        assert store.delete("nonexistent") is False

    def test_persistence(self, tmp_path):
        from api.services.reminder_store import ReminderStore
        file_path = str(tmp_path / "persist.json")

        # Create and save
        store1 = ReminderStore(file_path=file_path)
        store1.create(name="Persistent", schedule_type="cron",
                      schedule_value="0 9 * * *", message_type="static",
                      message_content="Hi")

        # Load from same file
        store2 = ReminderStore(file_path=file_path)
        reminders = store2.list_all()
        assert len(reminders) == 1
        assert reminders[0].name == "Persistent"


class TestCronComputation:
    """Tests for next trigger time computation."""

    def test_cron_next_trigger(self):
        from api.services.reminder_store import compute_next_trigger, Reminder
        reminder = Reminder(
            id="test",
            name="Test",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            message_type="static",
            message_content="Hi",
        )
        next_time = compute_next_trigger(reminder)
        assert next_time is not None
        parsed = datetime.fromisoformat(next_time)
        assert parsed > datetime.now(timezone.utc)

    def test_once_future_trigger(self):
        from api.services.reminder_store import compute_next_trigger, Reminder
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        reminder = Reminder(
            id="test",
            name="Test",
            schedule_type="once",
            schedule_value=future,
            message_type="static",
            message_content="Hi",
        )
        next_time = compute_next_trigger(reminder)
        assert next_time is not None

    def test_once_past_trigger(self):
        from api.services.reminder_store import compute_next_trigger, Reminder
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        reminder = Reminder(
            id="test",
            name="Test",
            schedule_type="once",
            schedule_value=past,
            message_type="static",
            message_content="Hi",
        )
        next_time = compute_next_trigger(reminder)
        assert next_time is None

    def test_invalid_cron_expression(self):
        from api.services.reminder_store import compute_next_trigger, Reminder
        reminder = Reminder(
            id="test",
            name="Test",
            schedule_type="cron",
            schedule_value="invalid cron",
            message_type="static",
            message_content="Hi",
        )
        next_time = compute_next_trigger(reminder)
        assert next_time is None


class TestReminderDueChecking:
    """Tests for due reminder detection."""

    def test_due_reminder_detected(self, tmp_path):
        from api.services.reminder_store import ReminderStore
        store = ReminderStore(file_path=str(tmp_path / "due.json"))

        # Create a reminder that was due 1 minute ago
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        reminder = store.create(
            name="Due",
            schedule_type="once",
            schedule_value=past,
            message_type="static",
            message_content="Hi",
        )
        # Manually set next_trigger_at to past (since create would set it to None for past once)
        reminder.next_trigger_at = past
        reminder.enabled = True

        due = store.get_due_reminders()
        assert len(due) == 1
        assert due[0].id == reminder.id

    def test_disabled_reminder_not_due(self, tmp_path):
        from api.services.reminder_store import ReminderStore
        store = ReminderStore(file_path=str(tmp_path / "disabled.json"))

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        reminder = store.create(
            name="Disabled",
            schedule_type="once",
            schedule_value=past,
            message_type="static",
            message_content="Hi",
            enabled=False,
        )

        due = store.get_due_reminders()
        assert len(due) == 0


class TestAutoDisable:
    """Tests for one-time reminder auto-disable."""

    def test_once_reminder_disables_after_trigger(self, tmp_path):
        from api.services.reminder_store import ReminderStore
        store = ReminderStore(file_path=str(tmp_path / "autodisable.json"))

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        reminder = store.create(
            name="One-time",
            schedule_type="once",
            schedule_value=future,
            message_type="static",
            message_content="Hi",
        )
        assert reminder.enabled is True

        store.mark_triggered(reminder.id)

        updated = store.get(reminder.id)
        assert updated.enabled is False
        assert updated.next_trigger_at is None
        assert updated.last_triggered_at is not None

    def test_cron_reminder_stays_enabled_after_trigger(self, tmp_path):
        from api.services.reminder_store import ReminderStore
        store = ReminderStore(file_path=str(tmp_path / "cron.json"))

        reminder = store.create(
            name="Recurring",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            message_type="static",
            message_content="Hi",
        )

        store.mark_triggered(reminder.id)

        updated = store.get(reminder.id)
        assert updated.enabled is True
        assert updated.next_trigger_at is not None
        assert updated.last_triggered_at is not None
