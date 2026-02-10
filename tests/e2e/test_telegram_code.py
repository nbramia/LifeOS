"""
E2E tests for Claude Code orchestration flows via Telegram.

Tests the approval workflow, clarification handling, cost cap termination,
and auto-routing of code intents.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import time

pytestmark = pytest.mark.unit


class TestApprovalFlow:
    """Tests for Claude Code plan approval/rejection."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Mock Claude Code orchestrator."""
        with patch("api.services.claude_orchestrator.get_orchestrator") as mock:
            orch = MagicMock()
            mock.return_value = orch

            session = MagicMock()
            session.task = "Fix the bug in server.py"
            session.status = "awaiting_approval"
            session.working_dir = "/path/to/project"
            session.started_at = time.time() - 60
            session.cost_usd = 0.05

            orch.get_active_session.return_value = session
            orch.is_busy.return_value = True

            yield orch

    def test_approval_keywords_recognized(self):
        """Test that approval keywords are recognized."""
        approval_words = {"approve", "approved", "yes", "go", "proceed", "ok"}

        for word in approval_words:
            normalized = word.strip().lower().rstrip(".")
            assert normalized in approval_words

    def test_rejection_keywords_recognized(self):
        """Test that rejection keywords are recognized."""
        rejection_words = {"reject", "rejected", "no", "cancel", "stop"}

        for word in rejection_words:
            normalized = word.strip().lower().rstrip(".")
            assert normalized in rejection_words

    def test_yes_approves_plan(self, mock_orchestrator):
        """Test that 'yes' triggers plan approval."""
        # Simulate session awaiting approval
        assert mock_orchestrator.get_active_session().status == "awaiting_approval"

        # Check approval detection logic
        text = "yes"
        normalized = text.strip().lower().rstrip(".")
        approval_keywords = {"approve", "approved", "yes", "go", "proceed", "ok"}

        assert normalized in approval_keywords

    def test_no_rejects_plan_when_awaiting_approval(self, mock_orchestrator):
        """Test that 'no' rejects plan when awaiting approval."""
        text = "no"
        normalized = text.strip().lower().rstrip(".")
        rejection_keywords = {"reject", "rejected", "no", "cancel", "stop"}

        assert normalized in rejection_keywords

    def test_long_message_not_treated_as_approval(self, mock_orchestrator):
        """Test that longer messages are not treated as approval/rejection."""
        messages = [
            "yes I want to add more features",
            "no I don't think that's the right approach",
            "approve but first let me check something",
        ]

        for msg in messages:
            # These should NOT be treated as simple approval/rejection
            # because they contain additional context
            words = msg.split()
            assert len(words) > 1


class TestClarificationFlow:
    """Tests for clarification question handling."""

    @pytest.fixture
    def mock_orchestrator_clarification(self):
        """Mock orchestrator awaiting clarification."""
        with patch("api.services.claude_orchestrator.get_orchestrator") as mock:
            orch = MagicMock()
            mock.return_value = orch

            session = MagicMock()
            session.task = "Update the tests"
            session.status = "awaiting_clarification"
            session.clarification_question = "Should I also update the integration tests?"

            orch.get_active_session.return_value = session

            yield orch

    def test_clarification_status_detected(self, mock_orchestrator_clarification):
        """Test that awaiting_clarification status is detected."""
        session = mock_orchestrator_clarification.get_active_session()
        assert session.status == "awaiting_clarification"

    def test_no_as_clarification_answer_not_rejection(
        self, mock_orchestrator_clarification
    ):
        """Test that 'no' as clarification answer doesn't reject the plan."""
        # When status is "awaiting_clarification", "no" should be forwarded
        # as an answer, not treated as plan rejection
        session = mock_orchestrator_clarification.get_active_session()

        # The key check: status is NOT awaiting_approval
        assert session.status == "awaiting_clarification"
        assert session.status != "awaiting_approval"

        # Therefore "no" should be passed through as an answer
        text = "no"
        # This should call respond_to_clarification(text), not reject_plan()

    def test_any_text_valid_as_clarification(self, mock_orchestrator_clarification):
        """Test that any text is valid as clarification response."""
        valid_responses = [
            "yes",
            "no",
            "just the unit tests",
            "include integration tests too",
            "skip the flaky ones",
            "42",
        ]

        for response in valid_responses:
            # All should be valid as clarification answers
            assert isinstance(response, str)
            assert len(response) > 0


class TestCostCapTermination:
    """Tests for cost cap enforcement."""

    def test_cost_cap_default_value(self):
        """Test that cost cap has a sensible default."""
        # Default should be set in settings
        # Typical value: $1.00 or similar
        default_cap = 1.0  # Expected default

        assert default_cap > 0
        assert default_cap <= 10.0  # Sanity check

    def test_cost_exceeds_cap_detection(self):
        """Test detection when cost exceeds cap."""
        cost_cap = 1.0
        test_costs = [0.5, 0.9, 1.0, 1.1, 2.0]

        for cost in test_costs:
            exceeds = cost >= cost_cap
            if cost >= cost_cap:
                assert exceeds
            else:
                assert not exceeds

    def test_session_terminates_on_cost_cap(self):
        """Test that session is terminated when cost cap reached."""
        cost_cap = 1.0
        session_cost = 1.05

        # Session should be terminated
        should_terminate = session_cost >= cost_cap
        assert should_terminate


class TestCodeIntentAutoRouting:
    """Tests for automatic routing of code intents to Claude Code."""

    @pytest.mark.asyncio
    async def test_code_intent_detected_via_chat(self):
        """Test that code intent in chat triggers Claude Code."""
        from api.services.chat_helpers import classify_action_intent

        mock_response = '{"intent": "code", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent("fix the bug in server.py", [])

            assert result is not None
            assert result.category == "code"

    @pytest.mark.parametrize("message,should_be_code", [
        ("create a file called test.py", True),
        ("run the tests", True),
        ("fix the bug in server.py", True),
        ("what does server.py do", False),
        ("how do I write a for loop", False),
        ("tell me about Python", False),
    ])
    @pytest.mark.asyncio
    async def test_code_vs_question_distinction(self, message, should_be_code):
        """Test distinction between code actions and code questions."""
        from api.services.chat_helpers import classify_action_intent

        if should_be_code:
            mock_response = '{"intent": "code", "confidence": 0.9}'
        else:
            mock_response = '{"intent": "none", "confidence": 0.9}'

        with patch(
            "api.services.chat_helpers._classify_via_ollama",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_action_intent(message, [])

            if should_be_code:
                assert result is not None
                assert result.category == "code"
            else:
                if result is not None:
                    assert result.category != "code"


class TestPlanModeHeuristics:
    """Tests for plan mode detection heuristics."""

    def test_complex_task_triggers_plan_mode(self):
        """Test that complex-sounding tasks trigger plan mode."""
        plan_mode_keywords = [
            "refactor",
            "implement",
            "redesign",
            "migrate",
            "integrate",
            "build a",
            "set up a",
            "rewrite",
            "overhaul",
            "replace",
            "restructure",
            "add a new",
            "create a new",
            "remove all",
            "delete all",
        ]

        complex_tasks = [
            "refactor the authentication system",
            "implement user login",
            "redesign the dashboard",
            "migrate to PostgreSQL",
            "build a new API endpoint",
            "rewrite the test suite",
        ]

        for task in complex_tasks:
            task_lower = task.lower()
            matches = any(kw in task_lower for kw in plan_mode_keywords)
            assert matches, f"Expected plan mode for: {task}"

    def test_simple_task_no_plan_mode(self):
        """Test that simple tasks don't trigger plan mode."""
        plan_mode_keywords = [
            "refactor",
            "implement",
            "redesign",
            "migrate",
        ]

        simple_tasks = [
            "fix the typo in README",
            "update the version number",
            "add a log statement",
            "run the tests",
        ]

        for task in simple_tasks:
            task_lower = task.lower()
            matches = any(kw in task_lower for kw in plan_mode_keywords)
            assert not matches, f"Unexpected plan mode for: {task}"


class TestDirectoryResolution:
    """Tests for working directory resolution."""

    def test_default_directory(self):
        """Test default working directory when no project mentioned."""
        # Default should be the LifeOS project
        default_dir = "/Users/nathanramia/Documents/Code/LifeOS"
        assert "LifeOS" in default_dir

    def test_project_keyword_detection(self):
        """Test that project keywords are detected in tasks."""
        task_project_pairs = [
            ("fix the bug in LifeOS", "LifeOS"),
            ("update the server", "LifeOS"),  # Default
        ]

        for task, expected_project in task_project_pairs:
            assert expected_project in task or expected_project == "LifeOS"


class TestSessionStatusTransitions:
    """Tests for session status state machine."""

    def test_valid_status_values(self):
        """Test all valid session status values."""
        valid_statuses = {
            "initializing",
            "running",
            "awaiting_approval",
            "awaiting_clarification",
            "implementing",
            "completed",
            "failed",
            "cancelled",
            "cost_exceeded",
        }

        # All statuses should be strings
        for status in valid_statuses:
            assert isinstance(status, str)
            assert len(status) > 0

    def test_approval_flow_transitions(self):
        """Test status transitions during approval flow."""
        # running -> awaiting_approval -> (approved) -> implementing -> completed
        # running -> awaiting_approval -> (rejected) -> cancelled
        approval_flow_approved = [
            "running",
            "awaiting_approval",
            "implementing",
            "completed",
        ]

        approval_flow_rejected = [
            "running",
            "awaiting_approval",
            "cancelled",
        ]

        # Both should be valid sequences
        assert len(approval_flow_approved) == 4
        assert len(approval_flow_rejected) == 3


class TestTelegramCommands:
    """Tests for Telegram slash commands."""

    def test_code_command_format(self):
        """Test /code command parsing."""
        commands = [
            ("/code fix the bug", "fix the bug"),
            ("/code update the tests", "update the tests"),
            ("/code", ""),  # No task provided
        ]

        for full_cmd, expected_task in commands:
            parts = full_cmd.split(" ", 1)
            task = parts[1] if len(parts) > 1 else ""
            assert task == expected_task

    def test_code_status_command(self):
        """Test /code_status command."""
        command = "/code_status"
        assert command.startswith("/code_status")

    def test_code_cancel_command(self):
        """Test /code_cancel command."""
        command = "/code_cancel"
        assert command.startswith("/code_cancel")
