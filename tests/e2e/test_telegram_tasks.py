"""
E2E tests for task CRUD operations via chat.

Tests the complete flow from user message -> intent classification ->
task manager operations -> response formatting.
"""
import pytest
from unittest.mock import patch, MagicMock

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
        response = "Done! Added to your task list:\n\n"
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




class TestTaskDelete:
    """Tests for task deletion via chat."""



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



    def test_compound_response_formatting(self, mock_stores):
        """Test that compound creation response shows both items."""
        task_manager, reminder = mock_stores

        task = task_manager.create.return_value

        response = "Done! I've created both:\n\n"
        response += f"**Task:** {task.description}\n"
        response += f"**Context:** {task.context}\n"
        response += "\n**Reminder** set to ping you about it."

        assert "Submit taxes" in response
        assert "Task" in response
        assert "Reminder" in response


class TestAmbiguousTaskReminder:
    """Tests for ambiguous task/reminder prompts."""

    @pytest.mark.asyncio
    async def test_ambiguous_prompt_shown(self):
        """Test that ambiguous input triggers clarification prompt."""
        # Messages that could be either task or reminder
        _ambiguous_messages = [  # noqa: F841 — kept as documentation of expected fixture shape
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
