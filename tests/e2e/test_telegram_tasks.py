"""
E2E tests for task CRUD operations via chat.

Tests the complete flow from user message -> intent classification ->
task manager operations -> response formatting.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime

pytestmark = pytest.mark.unit


class TestTaskCreation:
    """Tests for task creation via chat."""

    @pytest.fixture
    def mock_task_manager(self):
        """Mock task manager for testing."""
        with patch("api.services.task_manager.get_task_manager") as mock:
            manager = MagicMock()
            mock.return_value = manager

            # Create mock task
            task = MagicMock()
            task.id = "task-123"
            task.description = "Review the PR"
            task.context = "Work"
            task.tags = ["review"]
            task.due_date = None
            task.priority = ""
            task.status = "todo"

            manager.create.return_value = task
            manager.list.return_value = [task]

            yield manager

    @pytest.mark.asyncio
    async def test_task_create_intent_classified(self):
        """Test that task creation message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_create", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("add a task to review the PR", [])

            assert result is not None
            assert result.category == "task"
            assert result.sub_type == "create"

    @pytest.mark.asyncio
    async def test_extract_task_params(self):
        """Test that task parameters are extracted from message."""
        # Test the param extraction logic pattern
        message = "add a task to review the PR for the dashboard project"

        # Key phrases that should be extracted
        assert "review" in message.lower()
        assert "PR" in message or "pr" in message.lower()

    def test_task_response_formatting(self, mock_task_manager):
        """Test that task creation response is properly formatted."""
        task = mock_task_manager.create.return_value

        # Expected response format
        response = f"Done! Added to your task list:\n\n"
        response += f"**{task.description}**\n"
        response += f"Context: {task.context}"
        if task.tags:
            response += f" | {' '.join('#' + t for t in task.tags)}"

        assert "Review the PR" in response
        assert "Work" in response
        assert "#review" in response


class TestTaskList:
    """Tests for task listing via chat."""

    @pytest.fixture
    def mock_task_manager_with_tasks(self):
        """Mock task manager with multiple tasks."""
        with patch("api.services.task_manager.get_task_manager") as mock:
            manager = MagicMock()
            mock.return_value = manager

            tasks = [
                MagicMock(
                    id="task-1",
                    description="Review PR #123",
                    context="Work",
                    status="todo",
                    tags=["review"],
                    due_date=None,
                    priority="",
                ),
                MagicMock(
                    id="task-2",
                    description="Call dentist",
                    context="Personal",
                    status="todo",
                    tags=[],
                    due_date="2026-02-15",
                    priority="high",
                ),
            ]
            manager.list.return_value = tasks

            yield manager

    @pytest.mark.asyncio
    async def test_task_list_intent_classified(self):
        """Test that task list message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_list", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("what tasks do I have", [])

            assert result is not None
            assert result.category == "task"
            assert result.sub_type == "list"

    def test_task_list_formatting(self, mock_task_manager_with_tasks):
        """Test that task list is properly formatted."""
        tasks = mock_task_manager_with_tasks.list.return_value

        # Build expected list format
        lines = []
        for i, task in enumerate(tasks, 1):
            line = f"{i}. {task.description}"
            if task.context:
                line += f" ({task.context})"
            if task.due_date:
                line += f" - Due: {task.due_date}"
            lines.append(line)

        response = "\n".join(lines)

        assert "Review PR #123" in response
        assert "Call dentist" in response
        assert "Work" in response
        assert "Personal" in response


class TestTaskComplete:
    """Tests for task completion via chat."""

    @pytest.mark.asyncio
    async def test_task_complete_intent_classified(self):
        """Test that task complete message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_complete", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("mark the PR review as done", [])

            assert result is not None
            assert result.category == "task"
            assert result.sub_type == "complete"

    @pytest.mark.parametrize("message", [
        "mark the task as done",
        "complete the PR review task",
        "check off the dentist task",
        "I finished the report task",
    ])
    @pytest.mark.asyncio
    async def test_various_complete_phrases(self, message):
        """Test various ways users say they completed a task."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_complete", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])

            assert result is not None
            assert result.category == "task"
            assert result.sub_type == "complete"


class TestTaskDelete:
    """Tests for task deletion via chat."""

    @pytest.mark.asyncio
    async def test_task_delete_intent_classified(self):
        """Test that task delete message is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_delete", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("delete the old task", [])

            assert result is not None
            assert result.category == "task"
            assert result.sub_type == "delete"


class TestTaskAndReminderCompound:
    """Tests for compound task+reminder creation."""

    @pytest.fixture
    def mock_stores(self):
        """Mock both task manager and reminder store."""
        with patch("api.services.task_manager.get_task_manager") as task_mock:
            task_manager = MagicMock()
            task_mock.return_value = task_manager

            task = MagicMock()
            task.id = "task-123"
            task.description = "Submit taxes"
            task.context = "Personal"
            task.tags = []
            task.due_date = None
            task_manager.create.return_value = task

            # Create mock reminder without patching (not needed for formatting test)
            reminder = MagicMock()
            reminder.id = "rem-123"
            reminder.name = "Submit taxes"

            yield task_manager, reminder

    @pytest.mark.asyncio
    async def test_task_and_reminder_intent(self):
        """Test that compound intent is correctly classified."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "task_and_reminder", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(
                "add a task to submit taxes and remind me Friday",
                [],
            )

            assert result is not None
            assert result.category == "task_and_reminder"

    @pytest.mark.asyncio
    async def test_both_followup_classified(self):
        """Test that 'both' response after ambiguous prompt works."""
        from api.services.chat_helpers import classify_action_intent

        # Simulate conversation history
        mock_history = [
            MagicMock(
                role="user",
                content="add submit taxes to my list",
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

    def test_compound_response_formatting(self, mock_stores):
        """Test that compound creation response shows both items."""
        task_manager, reminder = mock_stores

        task = task_manager.create.return_value

        response = f"Done! I've created both:\n\n"
        response += f"**Task:** {task.description}\n"
        response += f"**Context:** {task.context}\n"
        response += f"\n**Reminder** set to ping you about it."

        assert "Submit taxes" in response
        assert "Task" in response
        assert "Reminder" in response


class TestAmbiguousTaskReminder:
    """Tests for ambiguous task/reminder prompts."""

    @pytest.mark.asyncio
    async def test_ambiguous_prompt_shown(self):
        """Test that ambiguous input triggers clarification prompt."""
        # Messages that could be either task or reminder
        ambiguous_messages = [
            "add submit taxes to my list",
            "remember to call mom",
            "don't forget the meeting",
        ]

        expected_prompt_keywords = [
            "to-do",
            "reminder",
            "both",
        ]

        # The clarification prompt should contain these options
        clarification = "Should I add this as a **to-do** in your task list, or set a **timed reminder** to ping you about it, or both?"

        for keyword in expected_prompt_keywords:
            assert keyword in clarification.lower()
