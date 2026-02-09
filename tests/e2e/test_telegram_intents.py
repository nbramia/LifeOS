"""
E2E tests for intent classification.

Tests that user messages are correctly classified into action intents.
Uses mocked LLM responses to test the full classification pipeline.
"""
import pytest
from unittest.mock import patch, AsyncMock

pytestmark = pytest.mark.unit


class TestIntentClassification:
    """Tests for classify_action_intent function."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message,expected_category,expected_subtype", [
        # Reminder intents
        ("remind me to call mom at 5pm", "reminder", "create"),
        ("set a reminder for tomorrow morning", "reminder", "create"),
        ("ping me about the meeting at 3", "reminder", "create"),
        ("show my reminders", "reminder", "list"),
        ("list all reminders", "reminder", "list"),
        ("delete the dentist reminder", "reminder", "delete"),
        ("remove that reminder", "reminder", "delete"),
        ("change the reminder to 6pm", "reminder", "edit"),
        ("update the meeting reminder", "reminder", "edit"),
        # Task intents
        ("add task review PR", "task", "create"),
        ("add a to-do to call dentist", "task", "create"),
        ("create task for quarterly report", "task", "create"),
        ("what tasks do I have", "task", "list"),
        ("show my tasks", "task", "list"),
        ("list my to-dos", "task", "list"),
        ("mark the report task as done", "task", "complete"),
        ("complete the review task", "task", "complete"),
        ("delete the old task", "task", "delete"),
        ("remove the dentist task", "task", "delete"),
        # Compose intents
        ("draft an email to John", "compose", None),
        ("write an email about the meeting", "compose", None),
        ("compose a message to the team", "compose", None),
        # Code intents
        ("create a file called test.py", "code", None),
        ("fix the bug in server.py", "code", None),
        ("update the CSS for the dashboard", "code", None),
        ("run the tests", "code", None),
        ("delete the old logs", "code", None),
    ])
    async def test_intent_classification_with_mocked_llm(
        self, message, expected_category, expected_subtype
    ):
        """Test that messages are classified to correct intents via LLM."""
        from api.services.chat_helpers import classify_action_intent

        # Mock Ollama to return the expected classification
        mock_response = f'{{"intent": "{expected_category}_{expected_subtype or "create"}", "confidence": 0.9}}'
        if expected_category == "code":
            mock_response = '{"intent": "code", "confidence": 0.9}'
        elif expected_category == "compose":
            mock_response = '{"intent": "compose", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])

            assert result is not None, f"Expected intent for: {message}"
            assert result.category == expected_category, (
                f"Expected {expected_category}, got {result.category} for: {message}"
            )
            if expected_subtype:
                assert result.sub_type == expected_subtype, (
                    f"Expected {expected_subtype}, got {result.sub_type} for: {message}"
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "what's the weather",
        "who is the president",
        "tell me about John Smith",
        "what's on my calendar tomorrow",
        "how do I write a for loop",
        "what does this function do",
    ])
    async def test_non_action_messages_return_none(self, message):
        """Test that non-action messages return None (no action intent)."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "none", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])
            assert result is None, f"Expected None for non-action: {message}"


class TestPatternFallback:
    """Tests for pattern-based fallback when LLMs are unavailable."""

    @pytest.mark.asyncio
    async def test_fallback_used_when_llms_down(self):
        """Pattern matching should be used when both Ollama and Haiku fail."""
        from api.services.chat_helpers import classify_action_intent

        # Simulate both LLMs failing
        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch(
                "api.services.chat_helpers._classify_via_haiku",
                new_callable=AsyncMock,
                return_value=None,
            ):
                # Should fall back to pattern matching
                result = await classify_action_intent("remind me to call mom", [])
                assert result is not None
                assert result.category == "reminder"

    @pytest.mark.parametrize("message,expected_category", [
        ("remind me to call mom", "reminder"),
        ("add a task to review PR", "task"),
        ("draft an email to John", "compose"),
        ("show my reminders", "reminder"),
        ("list my tasks", "task"),
        ("delete the reminder", "reminder"),
    ])
    def test_pattern_matching_directly(self, message, expected_category):
        """Test the pattern matching fallback directly."""
        from api.services.chat_helpers import _classify_action_intent_patterns

        result = _classify_action_intent_patterns(message)
        assert result is not None, f"Expected pattern match for: {message}"
        assert result.category == expected_category


class TestCompoundIntents:
    """Tests for task_and_reminder compound intent."""

    @pytest.mark.asyncio
    async def test_task_and_reminder_classification(self):
        """Test that 'both' response is classified as task_and_reminder."""
        from api.services.chat_helpers import classify_action_intent
        from unittest.mock import MagicMock

        # Simulate conversation where user was asked "task or reminder?"
        # and responded "both"
        mock_history = [
            MagicMock(
                role="user",
                content="add a todo to submit taxes and remind me Friday",
            ),
            MagicMock(
                role="assistant",
                content="Should I add this as a to-do or set a timed reminder?",
            ),
        ]

        mock_response = '{"intent": "task_and_reminder", "confidence": 0.95}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("both", mock_history)
            assert result is not None
            assert result.category == "task_and_reminder"

    @pytest.mark.asyncio
    async def test_explicit_task_and_reminder_request(self):
        """Test explicit 'task and reminder' in single message."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_and_reminder", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(
                "add a task to submit taxes and remind me about it Friday",
                [],
            )
            assert result is not None
            assert result.category == "task_and_reminder"


class TestCodeIntentClassification:
    """Tests for code intent detection and auto-routing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "create a Python script that does X",
        "fix the bug in my server",
        "update the code to handle edge cases",
        "run npm install",
        "check disk usage",
        "browse to google.com and search for X",
        "delete all .pyc files",
    ])
    async def test_code_intent_detected(self, message):
        """Test that code-related actions are detected."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "code", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])
            assert result is not None
            assert result.category == "code"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "how do I write a for loop in Python",
        "what does this function do",
        "explain the code in server.py",
        "what's the difference between let and const",
    ])
    async def test_code_questions_not_classified_as_code(self, message):
        """Test that questions ABOUT code are not classified as code actions."""
        from api.services.chat_helpers import classify_action_intent

        # Questions about code should return "none" not "code"
        mock_response = '{"intent": "none", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])
            # Should be None (no action) or not "code"
            if result is not None:
                assert result.category != "code", (
                    f"Code question incorrectly classified as code action: {message}"
                )


class TestLowConfidenceHandling:
    """Tests for handling low-confidence classifications."""

    @pytest.mark.asyncio
    async def test_low_confidence_returns_none(self):
        """Test that low confidence scores return None from parsing."""
        from api.services.chat_helpers import _parse_intent_response

        # Return a valid intent but with very low confidence
        mock_response = '{"intent": "task_create", "confidence": 0.2}'

        result = _parse_intent_response(mock_response)
        # Confidence threshold is 0.3, so this should return None
        assert result is None

    @pytest.mark.asyncio
    async def test_medium_confidence_accepted(self):
        """Test that medium confidence scores are accepted."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_create", "confidence": 0.5}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("add a task to review", [])
            assert result is not None
            assert result.category == "task"
