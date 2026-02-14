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


class TestSuppression:
    """Tests for prompt response suppression (Phase 4b)."""

    def test_no_meeting_suppressed(self):
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NO_MEETING") is True
        assert ReminderScheduler._should_suppress("  NO_MEETING  ") is True
        assert ReminderScheduler._should_suppress("no_meeting") is True

    def test_no_meetings_suppressed(self):
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NO_MEETINGS") is True

    def test_nothing_to_report_suppressed(self):
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NOTHING_TO_REPORT") is True

    def test_no_action_suppressed(self):
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NO_ACTION") is True

    def test_normal_content_not_suppressed(self):
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("Here is your meeting prep...") is False
        assert ReminderScheduler._should_suppress("**Morning Briefing**\n\nToday...") is False

    def test_empty_not_suppressed(self):
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("") is False


class TestPromptExecution:
    """Tests for prompt-type reminder execution with retry (Phase 4a)."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        from api.services.reminder_store import ReminderStore, ReminderScheduler
        store = ReminderStore(file_path=str(tmp_path / "exec.json"))
        return ReminderScheduler(store)

    @pytest.fixture
    def prompt_reminder(self, scheduler):
        return scheduler.store.create(
            name="Test Prompt",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            message_type="prompt",
            message_content="What meetings do I have today?",
        )

    @pytest.mark.asyncio
    async def test_successful_prompt_execution(self, scheduler, prompt_reminder):
        """Prompt reminder should return answer and execution log."""
        mock_result = {
            "answer": "You have 3 meetings today.",
            "conversation_id": "conv-1",
            "tool_statuses": ["Searching calendar..."],
            "cost_usd": 0.015,
            "model": "claude-sonnet-4-5-20250929",
            "input_tokens": 500,
            "output_tokens": 100,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, return_value=mock_result) as mock_chat:
            answer, exec_log = await scheduler._execute_prompt_reminder(prompt_reminder)

        assert answer == "You have 3 meetings today."
        assert exec_log["tool_calls"] == 1
        assert exec_log["cost_usd"] == 0.015
        assert exec_log["attempt"] == 1
        mock_chat.assert_called_once_with("What meetings do I have today?")

    @pytest.mark.asyncio
    async def test_retry_on_empty_response(self, scheduler, prompt_reminder):
        """Should retry when chat pipeline returns empty answer."""
        mock_results = [
            {"answer": "", "tool_statuses": [], "cost_usd": 0, "model": "", "input_tokens": 0, "output_tokens": 0},
            {"answer": "Retry worked!", "tool_statuses": ["Searching..."], "cost_usd": 0.01,
             "model": "claude", "input_tokens": 100, "output_tokens": 50},
        ]
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, side_effect=mock_results):
            answer, exec_log = await scheduler._execute_prompt_reminder(prompt_reminder)

        assert answer == "Retry worked!"
        assert exec_log["attempt"] == 2

    @pytest.mark.asyncio
    async def test_retry_on_exception(self, scheduler, prompt_reminder):
        """Should retry when chat pipeline raises an exception."""
        mock_results = [
            Exception("Connection error"),
            {"answer": "Recovery!", "tool_statuses": [], "cost_usd": 0.01,
             "model": "claude", "input_tokens": 100, "output_tokens": 50},
        ]
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, side_effect=mock_results):
            answer, exec_log = await scheduler._execute_prompt_reminder(prompt_reminder)

        assert answer == "Recovery!"
        assert exec_log["attempt"] == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self, scheduler, prompt_reminder):
        """Should return failure message when all retries fail."""
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, side_effect=Exception("Persistent error")):
            answer, exec_log = await scheduler._execute_prompt_reminder(prompt_reminder)

        assert "failed after 2 attempts" in answer
        assert exec_log["error"] == "Persistent error"
        assert exec_log["attempt"] == 2

    @pytest.mark.asyncio
    async def test_fire_reminder_sends_telegram(self, scheduler, prompt_reminder):
        """_fire_reminder should send the result via Telegram."""
        mock_result = {
            "answer": "Your morning briefing...",
            "tool_statuses": [], "cost_usd": 0.01,
            "model": "claude", "input_tokens": 100, "output_tokens": 50,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, return_value=mock_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "Test Prompt" in sent_text
        assert "Your morning briefing..." in sent_text

    @pytest.mark.asyncio
    async def test_fire_reminder_suppresses_no_meeting(self, scheduler, prompt_reminder):
        """_fire_reminder should NOT send Telegram when response is NO_MEETING."""
        mock_result = {
            "answer": "NO_MEETING",
            "tool_statuses": [], "cost_usd": 0.005,
            "model": "claude", "input_tokens": 50, "output_tokens": 5,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, return_value=mock_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_reminder_error_notification(self, scheduler, prompt_reminder):
        """_fire_reminder should send error notification when _generate_message raises."""
        with patch.object(scheduler, "_generate_message",
                          new_callable=AsyncMock, side_effect=Exception("Unexpected crash")):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        # Should have sent error notification
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "(failed)" in sent_text

    @pytest.mark.asyncio
    async def test_fire_reminder_retries_exhausted_still_sends(self, scheduler, prompt_reminder):
        """When all retries fail, the failure message should still be sent via Telegram."""
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, side_effect=Exception("Pipeline down")):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "failed after 2 attempts" in sent_text
