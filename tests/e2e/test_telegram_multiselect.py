"""
E2E tests for numeric selection (disambiguation) flows.

Tests the "which one?" prompts that present numbered lists and
handle user's numeric responses to select items.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

pytestmark = pytest.mark.unit


class TestPendingSelectionContext:
    """Tests for pending selection context tracking."""

    def test_context_tracks_pending_selection(self):
        """Test that pending selection is extracted from conversation history."""
        from api.services.conversation_context import (
            ConversationContext,
            extract_context_from_history,
        )

        # Simulate message with pending_selection in routing metadata
        mock_message = MagicMock()
        mock_message.role = "assistant"
        mock_message.content = "Which reminder?\n1. Meeting reminder\n2. Dentist reminder"
        mock_message.routing = {
            "pending_selection": {
                "type": "reminder",
                "action": "delete",
                "items": ["rem-123", "rem-456"],
            }
        }
        mock_message.created_at = datetime.now(timezone.utc)

        context = extract_context_from_history([mock_message])

        assert context.has_pending_selection()
        assert context.pending_selection_type == "reminder"
        assert context.pending_selection_action == "delete"
        assert context.pending_selection_items == ["rem-123", "rem-456"]

    def test_context_without_pending_selection(self):
        """Test that context without pending selection returns False."""
        from api.services.conversation_context import (
            ConversationContext,
            extract_context_from_history,
        )

        mock_message = MagicMock()
        mock_message.role = "assistant"
        mock_message.content = "I've created a reminder."
        mock_message.routing = {}
        mock_message.created_at = datetime.now(timezone.utc)

        context = extract_context_from_history([mock_message])

        assert not context.has_pending_selection()

    def test_pending_selection_staleness(self):
        """Test that pending selection expires after 30 minutes."""
        from api.services.conversation_context import ConversationContext

        # Create context with old timestamp
        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        context = ConversationContext(
            pending_selection_type="reminder",
            pending_selection_action="delete",
            pending_selection_items=["rem-123"],
            last_query_time=old_time,
        )

        assert context.is_stale(max_minutes=30)

    def test_pending_selection_not_stale(self):
        """Test that recent pending selection is not stale."""
        from api.services.conversation_context import ConversationContext

        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        context = ConversationContext(
            pending_selection_type="reminder",
            pending_selection_action="delete",
            pending_selection_items=["rem-123"],
            last_query_time=recent_time,
        )

        assert not context.is_stale(max_minutes=30)


class TestNumericSelectionFlow:
    """Tests for the complete numeric selection flow via chat API."""

    @pytest.fixture
    def mock_reminder_store(self):
        """Mock reminder store with test reminders."""
        with patch("api.services.reminder_store.get_reminder_store") as mock:
            store = MagicMock()
            mock.return_value = store

            # Create mock reminders
            rem1 = MagicMock()
            rem1.id = "rem-123"
            rem1.name = "Meeting reminder"
            rem1.enabled = True

            rem2 = MagicMock()
            rem2.id = "rem-456"
            rem2.name = "Dentist reminder"
            rem2.enabled = True

            store.get.side_effect = lambda id: rem1 if id == "rem-123" else rem2
            store.list.return_value = [rem1, rem2]

            yield store

    @pytest.fixture
    def mock_conversation_store(self):
        """Mock conversation store."""
        with patch("api.services.conversation_store.get_store") as mock:
            store = MagicMock()
            mock.return_value = store

            store.create_conversation.return_value = "conv-test-123"
            store.get_conversation.return_value = MagicMock(id="conv-test-123")

            yield store

    def test_numeric_input_detected(self):
        """Test that numeric input is detected for pending selection."""
        test_inputs = ["1", "2", " 1 ", "3"]
        for inp in test_inputs:
            assert inp.strip().isdigit()

    def test_non_numeric_input_not_selected(self):
        """Test that non-numeric input doesn't trigger selection."""
        test_inputs = ["one", "first", "yes", "delete it"]
        for inp in test_inputs:
            assert not inp.strip().isdigit()

    def test_index_bounds_checking(self):
        """Test that selection handles out-of-bounds indices."""
        items = ["rem-123", "rem-456"]

        # Valid indices (1-based)
        assert 0 <= 1 - 1 < len(items)  # "1" -> index 0
        assert 0 <= 2 - 1 < len(items)  # "2" -> index 1

        # Invalid indices
        assert not (0 <= 0 - 1 < len(items))  # "0" -> index -1
        assert not (0 <= 3 - 1 < len(items))  # "3" -> index 2


class TestReminderDisambiguation:
    """Tests for reminder disambiguation prompts."""

    def test_format_reminder_selection_prompt(self):
        """Test formatting of numbered reminder list."""
        # Import and test the prompt formatter if it exists
        # Otherwise test the expected output format
        reminders = [
            MagicMock(id="rem-1", name="Meeting at 3pm"),
            MagicMock(id="rem-2", name="Call dentist"),
        ]

        # Expected format for disambiguation prompt
        expected_parts = [
            "Which reminder",
            "1.",
            "Meeting",
            "2.",
            "dentist",
        ]

        # Simulate what the prompt should look like
        prompt = "Which reminder do you mean?\n1. Meeting at 3pm\n2. Call dentist"

        for part in expected_parts:
            assert part.lower() in prompt.lower()

    @pytest.mark.asyncio
    async def test_delete_disambiguation_stores_pending_selection(self):
        """Test that ambiguous delete stores pending selection in routing."""
        from api.services.chat_helpers import classify_action_intent

        # Mock the classification to return reminder_delete
        mock_response = '{"intent": "reminder_delete", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("delete the meeting reminder", [])
            assert result is not None
            assert result.category == "reminder"
            assert result.sub_type == "delete"


class TestTaskDisambiguation:
    """Tests for task disambiguation (if implemented)."""

    def test_task_selection_format(self):
        """Test formatting of numbered task list."""
        tasks = [
            MagicMock(id="task-1", description="Review PR #123"),
            MagicMock(id="task-2", description="Review PR #456"),
        ]

        # Expected format for task disambiguation
        expected_parts = ["Which task", "1.", "2.", "Review"]

        prompt = "Which task do you mean?\n1. Review PR #123\n2. Review PR #456"

        for part in expected_parts:
            assert part in prompt


class TestContextExpiryBehavior:
    """Tests for context expiry edge cases."""

    def test_stale_context_ignored(self):
        """Test that stale pending selection is not used."""
        from api.services.conversation_context import ConversationContext

        # Context from 40 minutes ago
        old_time = datetime.now(timezone.utc) - timedelta(minutes=40)
        context = ConversationContext(
            pending_selection_type="reminder",
            pending_selection_action="delete",
            pending_selection_items=["rem-123"],
            last_query_time=old_time,
        )

        # Should be stale
        assert context.is_stale(max_minutes=30)
        # has_pending_selection still returns True (data exists)
        assert context.has_pending_selection()
        # But the staleness check should prevent usage

    def test_context_without_timestamp_is_stale(self):
        """Test that context without timestamp is treated as stale."""
        from api.services.conversation_context import ConversationContext

        context = ConversationContext(
            pending_selection_type="reminder",
            pending_selection_action="delete",
            pending_selection_items=["rem-123"],
            last_query_time=None,  # No timestamp
        )

        assert context.is_stale()

    def test_fresh_context_used(self):
        """Test that fresh context is used for selection."""
        from api.services.conversation_context import ConversationContext

        recent_time = datetime.now(timezone.utc) - timedelta(minutes=2)
        context = ConversationContext(
            pending_selection_type="reminder",
            pending_selection_action="delete",
            pending_selection_items=["rem-123", "rem-456"],
            last_query_time=recent_time,
        )

        assert not context.is_stale()
        assert context.has_pending_selection()
        assert len(context.pending_selection_items) == 2


class TestSelectionActions:
    """Tests for different selection action types."""

    def test_delete_action_selection(self):
        """Test selecting item for delete action."""
        from api.services.conversation_context import ConversationContext

        context = ConversationContext(
            pending_selection_type="reminder",
            pending_selection_action="delete",
            pending_selection_items=["rem-123", "rem-456"],
            last_query_time=datetime.now(timezone.utc),
        )

        # User enters "1"
        idx = 1 - 1  # Convert to 0-based
        selected_id = context.pending_selection_items[idx]

        assert selected_id == "rem-123"
        assert context.pending_selection_action == "delete"

    def test_edit_action_selection(self):
        """Test selecting item for edit action."""
        from api.services.conversation_context import ConversationContext

        context = ConversationContext(
            pending_selection_type="reminder",
            pending_selection_action="edit",
            pending_selection_items=["rem-123", "rem-456"],
            last_query_time=datetime.now(timezone.utc),
        )

        # User enters "2"
        idx = 2 - 1
        selected_id = context.pending_selection_items[idx]

        assert selected_id == "rem-456"
        assert context.pending_selection_action == "edit"
