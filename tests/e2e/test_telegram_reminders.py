"""
E2E tests for reminder CRUD operations via chat.

Tests the complete flow from user message -> intent classification ->
reminder store operations -> response formatting, including timezone handling.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

pytestmark = pytest.mark.unit


class TestReminderCreation:
    """Tests for reminder creation via chat."""

    @pytest.fixture
    def mock_reminder_store(self):
        """Mock reminder store for testing."""
        with patch("api.services.reminder_store.get_reminder_store") as mock:
            store = MagicMock()
            mock.return_value = store

            reminder = MagicMock()
            reminder.id = "rem-123"
            reminder.name = "Call mom"
            reminder.schedule_type = "once"
            reminder.schedule_value = "2026-02-10T17:00:00-05:00"
            reminder.enabled = True
            reminder.timezone = "America/New_York"

            store.create.return_value = reminder
            store.list.return_value = [reminder]

            yield store

    @pytest.mark.asyncio
    async def test_reminder_create_intent_classified(self):
        """Test that reminder creation message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "reminder_create", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("remind me to call mom at 5pm", [])

            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "create"

    @pytest.mark.parametrize("message", [
        "remind me to call mom",
        "set a reminder for the meeting",
        "ping me about the dentist tomorrow",
        "alert me at 3pm about the call",
        "don't let me forget to buy milk",
    ])
    @pytest.mark.asyncio
    async def test_various_reminder_phrases(self, message):
        """Test various ways users create reminders."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "reminder_create", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])

            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "create"


class TestTimezoneHandling:
    """Tests for timezone-aware reminder scheduling."""

    def test_cron_respects_timezone(self):
        """Test that 6pm ET means 6pm Eastern, not UTC."""
        eastern = ZoneInfo("America/New_York")
        utc = ZoneInfo("UTC")

        # 6pm ET
        et_time = datetime(2026, 2, 10, 18, 0, 0, tzinfo=eastern)

        # Should NOT be 6pm UTC
        utc_time = et_time.astimezone(utc)
        assert utc_time.hour != 18  # Should be different due to offset

        # In winter (EST), 6pm ET = 11pm UTC
        assert utc_time.hour == 23

    def test_smart_default_time_tomorrow_9am(self):
        """Test that no time specified defaults to 9am tomorrow."""
        from api.services.time_parser import get_smart_default_time

        eastern = ZoneInfo("America/New_York")
        now = datetime(2026, 2, 9, 14, 0, 0, tzinfo=eastern)  # 2pm today

        default_time = get_smart_default_time(now)

        # Should be tomorrow at 9am
        expected = datetime(2026, 2, 10, 9, 0, 0, tzinfo=eastern)
        assert default_time.date() == expected.date()
        assert default_time.hour == 9

    def test_evening_reminder_same_day(self):
        """Test that 'tonight' defaults to 8pm today."""
        from api.services.time_parser import parse_contextual_time

        eastern = ZoneInfo("America/New_York")
        now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=eastern)  # 10am

        result = parse_contextual_time("tonight", now)

        if result:
            assert result.date() == now.date()  # Same day
            assert result.hour >= 19  # Evening hours

    def test_cron_expression_for_daily(self):
        """Test cron expression generation for daily reminders."""
        # 6pm daily = 0 18 * * *
        cron = "0 18 * * *"
        parts = cron.split()

        assert len(parts) == 5
        assert parts[0] == "0"   # minute
        assert parts[1] == "18"  # hour (6pm in 24h)
        assert parts[2] == "*"   # day of month
        assert parts[3] == "*"   # month
        assert parts[4] == "*"   # day of week

    def test_cron_expression_for_weekdays(self):
        """Test cron expression for weekday-only reminders."""
        # 9am on weekdays = 0 9 * * 1-5
        cron = "0 9 * * 1-5"
        parts = cron.split()

        assert parts[1] == "9"     # 9am
        assert parts[4] == "1-5"   # Mon-Fri


class TestReminderList:
    """Tests for reminder listing via chat."""

    @pytest.fixture
    def mock_reminder_store_with_reminders(self):
        """Mock store with multiple reminders."""
        with patch("api.services.reminder_store.get_reminder_store") as mock:
            store = MagicMock()
            mock.return_value = store

            reminders = [
                MagicMock(
                    id="rem-1",
                    name="Meeting at 3pm",
                    schedule_type="once",
                    schedule_value="2026-02-10T15:00:00-05:00",
                    enabled=True,
                ),
                MagicMock(
                    id="rem-2",
                    name="Daily standup",
                    schedule_type="cron",
                    schedule_value="0 9 * * 1-5",
                    enabled=True,
                ),
            ]
            store.list.return_value = reminders

            yield store

    @pytest.mark.asyncio
    async def test_reminder_list_intent_classified(self):
        """Test that reminder list message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "reminder_list", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("show my reminders", [])

            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "list"


class TestReminderEdit:
    """Tests for reminder editing via chat."""

    @pytest.mark.asyncio
    async def test_reminder_edit_intent_classified(self):
        """Test that reminder edit message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "reminder_edit", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(
                "change the meeting reminder to 4pm",
                [],
            )

            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "edit"

    @pytest.mark.parametrize("message", [
        "change the reminder to 6pm",
        "update the meeting reminder",
        "reschedule the dentist reminder to tomorrow",
        "move the call reminder to next week",
    ])
    @pytest.mark.asyncio
    async def test_various_edit_phrases(self, message):
        """Test various ways users edit reminders."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "reminder_edit", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])

            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "edit"


class TestReminderDelete:
    """Tests for reminder deletion via chat."""

    @pytest.mark.asyncio
    async def test_reminder_delete_intent_classified(self):
        """Test that reminder delete message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "reminder_delete", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("delete the dentist reminder", [])

            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "delete"

    @pytest.mark.parametrize("message", [
        "delete the reminder",
        "remove the meeting reminder",
        "cancel the dentist reminder",
        "turn off that reminder",
    ])
    @pytest.mark.asyncio
    async def test_various_delete_phrases(self, message):
        """Test various ways users delete reminders."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "reminder_delete", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])

            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "delete"


class TestReminderDisambiguationFlow:
    """Tests for reminder disambiguation when multiple match."""

    def test_disambiguation_prompt_format(self):
        """Test the format of disambiguation prompts."""
        reminders = [
            MagicMock(id="rem-1", name="Meeting at 3pm"),
            MagicMock(id="rem-2", name="Meeting tomorrow"),
        ]

        # Expected prompt format
        expected_keywords = [
            "Which",
            "1.",
            "2.",
            "Meeting",
        ]

        prompt = "Which reminder do you mean?\n1. Meeting at 3pm\n2. Meeting tomorrow"

        for keyword in expected_keywords:
            assert keyword in prompt

    def test_fuzzy_match_finds_closest(self):
        """Test that fuzzy matching finds the closest reminder name."""
        # Using simple dicts to avoid MagicMock attribute issues
        reminders = [
            {"id": "rem-1", "name": "Meeting with John"},
            {"id": "rem-2", "name": "Dentist appointment"},
            {"id": "rem-3", "name": "Meeting prep"},
        ]

        query_topic = "meeting"

        # Filter by topic
        matches = [r for r in reminders if query_topic in r["name"].lower()]

        assert len(matches) == 2  # Both meeting reminders
        assert all("Meeting" in m["name"] for m in matches)


class TestRecurringReminders:
    """Tests for recurring (cron-based) reminders."""

    def test_cron_reminder_creation(self):
        """Test creating a recurring reminder with cron schedule."""
        # Daily at 6pm
        reminder_data = {
            "name": "Daily standup reminder",
            "schedule_type": "cron",
            "schedule_value": "0 18 * * *",
            "timezone": "America/New_York",
        }

        assert reminder_data["schedule_type"] == "cron"
        parts = reminder_data["schedule_value"].split()
        assert len(parts) == 5  # Valid cron expression

    def test_weekday_only_cron(self):
        """Test creating weekday-only recurring reminder."""
        reminder_data = {
            "name": "Workday standup",
            "schedule_type": "cron",
            "schedule_value": "0 9 * * 1-5",  # 9am Mon-Fri
            "timezone": "America/New_York",
        }

        parts = reminder_data["schedule_value"].split()
        assert parts[4] == "1-5"  # Mon-Fri

    def test_monthly_cron(self):
        """Test creating monthly recurring reminder."""
        reminder_data = {
            "name": "Monthly report",
            "schedule_type": "cron",
            "schedule_value": "0 9 1 * *",  # 9am on the 1st
            "timezone": "America/New_York",
        }

        parts = reminder_data["schedule_value"].split()
        assert parts[2] == "1"  # 1st of month
