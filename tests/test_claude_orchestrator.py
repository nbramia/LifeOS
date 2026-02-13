"""
Tests for the Claude Code orchestrator.

Tests event parsing, [NOTIFY] extraction, session lifecycle, and plan mode.
Does NOT spawn real Claude processes — all subprocess calls are mocked.
"""
import json
import pytest
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.unit


class TestNotifyExtraction:
    """Test the [NOTIFY] regex pattern."""

    def test_basic_notify(self):
        from api.services.claude_orchestrator import _NOTIFY_RE
        match = _NOTIFY_RE.search("[NOTIFY] Task completed successfully.")
        assert match
        assert match.group(1) == "Task completed successfully."

    def test_notify_with_extra_spaces(self):
        from api.services.claude_orchestrator import _NOTIFY_RE
        match = _NOTIFY_RE.search("[NOTIFY]   Extra spaces here.")
        assert match
        assert match.group(1).strip() == "Extra spaces here."

    def test_no_notify(self):
        from api.services.claude_orchestrator import _NOTIFY_RE
        match = _NOTIFY_RE.search("Just a regular line of text.")
        assert match is None

    def test_notify_in_multiline(self):
        from api.services.claude_orchestrator import _NOTIFY_RE
        text = "Some preamble\n[NOTIFY] Found the issue.\nMore text"
        matches = list(_NOTIFY_RE.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(1) == "Found the issue."

    def test_multiple_notifies(self):
        from api.services.claude_orchestrator import _NOTIFY_RE
        text = "[NOTIFY] Step 1 done.\n[NOTIFY] Step 2 done."
        matches = list(_NOTIFY_RE.finditer(text))
        assert len(matches) == 2


class TestClaudeSession:
    """Test session dataclass defaults."""

    def test_defaults(self):
        from api.services.claude_orchestrator import ClaudeSession
        session = ClaudeSession()
        assert session.status == "running"
        assert session.session_id is None
        assert session.cost_usd == 0.0
        assert session.plan_mode is False
        assert len(session.id) == 12


class TestHandleEvent:
    """Test event handling logic in the orchestrator."""

    def _make_orchestrator(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        return orch

    def test_init_event_captures_session_id(self):
        from api.services.claude_orchestrator import ClaudeSession
        orch = self._make_orchestrator()
        session = ClaudeSession(task="test")

        event = {"type": "system", "subtype": "init", "session_id": "sess-abc123"}
        orch._handle_event(event, session)

        assert session.session_id == "sess-abc123"

    def test_assistant_event_extracts_notify(self):
        from api.services.claude_orchestrator import ClaudeSession
        orch = self._make_orchestrator()
        session = ClaudeSession(task="test")
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Working on it...\n[NOTIFY] Created the file at ~/test.txt"},
                ]
            }
        }
        orch._handle_event(event, session)

        assert len(notifications) == 1
        assert "Created the file" in notifications[0]

    def test_assistant_event_no_notify(self):
        from api.services.claude_orchestrator import ClaudeSession
        orch = self._make_orchestrator()
        session = ClaudeSession(task="test")
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Just doing some work, no notify here."},
                ]
            }
        }
        orch._handle_event(event, session)

        assert len(notifications) == 0

    def test_result_event_completes_session(self):
        from api.services.claude_orchestrator import ClaudeSession
        orch = self._make_orchestrator()
        session = ClaudeSession(task="test")
        orch._active_session = session
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {
            "type": "result",
            "session_id": "sess-final",
            "total_cost_usd": 0.0042,
            "result": "All done!",
        }
        orch._handle_event(event, session)

        assert session.session_id == "sess-final"
        assert session.cost_usd == 0.0042
        # No [NOTIFY] was sent during session, so fallback message is used
        assert len(notifications) == 1
        assert "completed" in notifications[0].lower()

    def test_result_event_empty_result_still_notifies(self):
        from api.services.claude_orchestrator import ClaudeSession
        orch = self._make_orchestrator()
        session = ClaudeSession(task="test")
        orch._active_session = session
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {"type": "result", "result": "", "total_cost_usd": 0.0}
        orch._handle_event(event, session)

        assert len(notifications) == 1
        assert "completed" in notifications[0].lower()

    def test_result_event_plan_mode_awaits_approval(self):
        from api.services.claude_orchestrator import ClaudeSession
        orch = self._make_orchestrator()
        session = ClaudeSession(task="test", plan_mode=True)
        orch._active_session = session
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {
            "type": "result",
            "session_id": "sess-plan",
            "total_cost_usd": 0.001,
            "result": "Here is the plan...",
        }
        orch._handle_event(event, session)

        assert session.status == "awaiting_approval"
        assert len(notifications) == 1
        assert "approve" in notifications[0].lower()

    def test_plan_mode_accumulates_notify_text(self):
        from api.services.claude_orchestrator import ClaudeSession
        orch = self._make_orchestrator()
        session = ClaudeSession(task="test", plan_mode=True)
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "[NOTIFY] Step 1: Do X\n[NOTIFY] Step 2: Do Y"},
                ]
            }
        }
        orch._handle_event(event, session)

        assert "Step 1" in session.plan_text
        assert "Step 2" in session.plan_text


class TestOrchestratorLifecycle:
    """Test session lifecycle: busy check, reject, cancel."""

    def test_is_busy_when_no_session(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator
        orch = ClaudeOrchestrator()
        assert orch.is_busy() is False

    def test_is_busy_when_running(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="running")
        assert orch.is_busy() is True

    def test_is_busy_when_awaiting(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="awaiting_approval")
        assert orch.is_busy() is True

    def test_not_busy_when_completed(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="completed")
        assert orch.is_busy() is False

    def test_reject_plan(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        session = ClaudeSession(status="awaiting_approval", plan_mode=True)
        orch._active_session = session

        result = orch.reject_plan()
        assert result is session
        assert session.status == "completed"
        assert orch._active_session is None

    def test_reject_when_not_awaiting(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="running")

        result = orch.reject_plan()
        assert result is None

    def test_cancel_kills_process(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="running")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        orch._process = mock_proc

        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        orch.cancel()

        mock_proc.terminate.assert_called_once()
        assert orch._active_session is None
        assert len(notifications) == 1
        assert "cancelled" in notifications[0].lower()

    @patch("api.services.claude_orchestrator.subprocess.Popen")
    def test_run_task_rejects_when_busy(self, mock_popen):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="running")

        with pytest.raises(RuntimeError, match="already active"):
            orch.run_task("new task", "/tmp")

    @patch("api.services.claude_orchestrator.subprocess.Popen")
    def test_run_task_spawns_process(self, mock_popen):
        from api.services.claude_orchestrator import ClaudeOrchestrator

        mock_proc = MagicMock()
        mock_proc.stdout = iter([])  # Empty output
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = ""
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        orch = ClaudeOrchestrator()
        session = orch.run_task("test task", "/tmp")

        assert session.task == "test task"
        assert session.working_dir == "/tmp"
        mock_popen.assert_called_once()

        # Verify CLI args
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--model" in cmd
        assert "opus" in cmd


class TestTelegramIntegration:
    """Test the Telegram command wiring for /code commands."""

    def test_should_use_plan_mode(self):
        from api.services.telegram import TelegramBotListener
        listener = TelegramBotListener()
        assert listener._should_use_plan_mode("refactor the auth module") is True
        assert listener._should_use_plan_mode("implement a new search feature") is True
        assert listener._should_use_plan_mode("build a backup system") is True
        assert listener._should_use_plan_mode("rewrite the auth module") is True
        assert listener._should_use_plan_mode("overhaul the sync pipeline") is True
        assert listener._should_use_plan_mode("add a new health check endpoint") is True
        assert listener._should_use_plan_mode("remove all unused imports") is True
        assert listener._should_use_plan_mode("create a file called test.txt") is False
        assert listener._should_use_plan_mode("edit the backlog") is False
        assert listener._should_use_plan_mode("write a cron job") is False
        assert listener._should_use_plan_mode("add weather alerts to the backlog") is False

    def test_check_agent_approval_no_session(self):
        from api.services.telegram import TelegramBotListener
        listener = TelegramBotListener()

        with patch("api.services.claude_orchestrator.get_orchestrator") as mock_get:
            mock_orch = MagicMock()
            mock_orch.get_active_session.return_value = None
            mock_get.return_value = mock_orch

            assert listener._check_agent_approval("approve") is False

    def test_check_agent_approval_with_pending_plan(self):
        from api.services.telegram import TelegramBotListener
        from api.services.claude_orchestrator import ClaudeSession
        listener = TelegramBotListener()

        with patch("api.services.claude_orchestrator.get_orchestrator") as mock_get:
            mock_orch = MagicMock()
            mock_orch.get_active_session.return_value = ClaudeSession(status="awaiting_approval")
            mock_get.return_value = mock_orch

            assert listener._check_agent_approval("approve") is True
            assert listener._check_agent_approval("yes") is True
            assert listener._check_agent_approval("reject") is True
            assert listener._check_agent_approval("no") is True
            # Long messages should NOT be intercepted
            assert listener._check_agent_approval("I think we should approve but also change X") is False

    def test_check_agent_clarification(self):
        from api.services.telegram import TelegramBotListener
        from api.services.claude_orchestrator import ClaudeSession
        listener = TelegramBotListener()

        with patch("api.services.claude_orchestrator.get_orchestrator") as mock_get:
            mock_orch = MagicMock()
            # No session → False
            mock_orch.get_active_session.return_value = None
            mock_get.return_value = mock_orch
            assert listener._check_agent_clarification() is False

            # Awaiting clarification → True
            mock_orch.get_active_session.return_value = ClaudeSession(
                status="awaiting_clarification",
                pending_clarification="Which section?",
            )
            assert listener._check_agent_clarification() is True

            # Running → False
            mock_orch.get_active_session.return_value = ClaudeSession(status="running")
            assert listener._check_agent_clarification() is False


class TestClarificationFlow:
    """Test the [CLARIFY] extraction and session pause/resume."""

    def test_clarify_regex(self):
        from api.services.claude_orchestrator import _CLARIFY_RE
        match = _CLARIFY_RE.search("[CLARIFY] Which backlog section?")
        assert match
        assert match.group(1) == "Which backlog section?"

    def test_clarify_regex_no_match(self):
        from api.services.claude_orchestrator import _CLARIFY_RE
        assert _CLARIFY_RE.search("[NOTIFY] Done.") is None
        assert _CLARIFY_RE.search("regular text") is None

    def test_clarify_event_sets_pending(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test")
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "[CLARIFY] Work or Personal backlog?"},
                ]
            }
        }
        orch._handle_event(event, session)

        assert session.pending_clarification == "Work or Personal backlog?"
        assert len(notifications) == 1
        assert "Work or Personal" in notifications[0]

    def test_result_after_clarify_awaits_response(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test", pending_clarification="Which one?")
        session.session_id = "sess-123"
        orch._active_session = session
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {"type": "result", "session_id": "sess-123", "total_cost_usd": 0.001, "result": ""}
        orch._handle_event(event, session)

        assert session.status == "awaiting_clarification"
        # Should NOT send a completion notification
        assert len(notifications) == 0

    def test_respond_to_clarification_resumes(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        session = ClaudeSession(
            task="test",
            status="awaiting_clarification",
            pending_clarification="Which one?",
            session_id="sess-123",
        )
        orch._active_session = session

        with patch.object(orch, "_spawn") as mock_spawn:
            result = orch.respond_to_clarification("The work backlog")
            assert result is session
            assert session.status == "running"
            assert session.pending_clarification == ""
            mock_spawn.assert_called_once_with(
                "The work backlog",
                session,
                resume_session_id="sess-123",
            )

    def test_respond_when_not_awaiting(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="running")
        assert orch.respond_to_clarification("answer") is None

    def test_is_busy_when_awaiting_clarification(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession
        orch = ClaudeOrchestrator()
        orch._active_session = ClaudeSession(status="awaiting_clarification")
        assert orch.is_busy() is True


class TestMaxTurnsAndCostCap:
    """Test --max-turns CLI flag and cost cap enforcement."""

    @patch("api.services.claude_orchestrator.subprocess.Popen")
    def test_max_turns_in_cli_args(self, mock_popen):
        from api.services.claude_orchestrator import ClaudeOrchestrator

        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = ""
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        orch = ClaudeOrchestrator()
        orch.run_task("test task", "/tmp")

        cmd = mock_popen.call_args[0][0]
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1].isdigit()

    def test_cost_cap_cancels_session(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test")
        orch._active_session = session
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        # Mock a process that's still running
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        orch._process = mock_proc

        # Send a result event with cost exceeding cap
        event = {
            "type": "result",
            "session_id": "sess-expensive",
            "total_cost_usd": 5.00,
            "result": "Still going...",
        }
        orch._handle_event(event, session)

        # Session should be cleaned up
        assert orch._active_session is None
        assert len(notifications) == 1
        assert "cost cap" in notifications[0].lower()
        mock_proc.terminate.assert_called_once()

    def test_cost_under_cap_continues(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test")
        orch._active_session = session
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        event = {
            "type": "result",
            "session_id": "sess-cheap",
            "total_cost_usd": 0.50,
            "result": "All done!",
        }
        orch._handle_event(event, session)

        # No [NOTIFY] was sent during session, so fallback message is used
        assert len(notifications) == 1
        assert "completed" in notifications[0].lower()


class TestToolCallTracking:
    """Test tool_use event tracking for heartbeat context."""

    def test_tool_use_updates_last_activity(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test")
        orch._notification_callback = lambda msg: None

        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/home/user/api/main.py"},
                    }
                ]
            }
        }
        orch._handle_event(event, session)
        assert session.last_activity == "reading main.py"

    def test_bash_tool_truncates_command(self):
        from api.services.claude_orchestrator import _summarize_tool_call

        result = _summarize_tool_call("Bash", {"command": "pytest tests/test_search.py -v --tb=short"})
        assert "pytest" in result
        assert len(result) < 80

    def test_unknown_tool_shows_name(self):
        from api.services.claude_orchestrator import _summarize_tool_call

        result = _summarize_tool_call("WebSearch", {"query": "test"})
        assert "WebSearch" in result

    def test_heartbeat_includes_activity(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test", last_activity="editing main.py")
        session.started_at = session.started_at - 600  # 10 min ago
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        orch._on_heartbeat(session)

        assert len(notifications) == 1
        assert "editing main.py" in notifications[0]
        assert "10m" in notifications[0]

    def test_heartbeat_without_activity(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test")
        session.started_at = session.started_at - 300
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        orch._on_heartbeat(session)

        assert len(notifications) == 1
        assert "Still working" in notifications[0]
        # Should not have a dangling " —" when no activity
        assert " — " not in notifications[0]

    def test_heartbeat_includes_cost(self):
        from api.services.claude_orchestrator import ClaudeOrchestrator, ClaudeSession

        orch = ClaudeOrchestrator()
        session = ClaudeSession(task="test", cost_usd=0.75)
        session.started_at = session.started_at - 300
        notifications = []
        orch._notification_callback = lambda msg: notifications.append(msg)

        orch._on_heartbeat(session)

        assert "$0.75" in notifications[0]


class TestClarificationNoCollision:
    """Test that 'no' is passed through as an answer, not treated as cancellation."""

    @pytest.mark.asyncio
    async def test_no_passes_through_as_answer(self):
        from api.services.telegram import TelegramBotListener
        from api.services.claude_orchestrator import ClaudeSession

        listener = TelegramBotListener()

        with patch("api.services.claude_orchestrator.get_orchestrator") as mock_get, \
             patch("api.services.telegram.send_message_async") as mock_send:
            mock_orch = MagicMock()
            session = ClaudeSession(
                status="awaiting_clarification",
                pending_clarification="Should I also update the tests?",
                session_id="sess-123",
            )
            mock_orch.respond_to_clarification.return_value = session
            mock_orch.get_active_session.return_value = session
            mock_get.return_value = mock_orch

            await listener._handle_agent_clarification("no", "chat123")

            # Should pass "no" through, not cancel
            mock_orch.respond_to_clarification.assert_called_once_with("no")
            mock_orch.cancel.assert_not_called()
            mock_send.assert_called_once()
            assert "Resuming" in mock_send.call_args[0][0]


class TestDirectoryResolverNewKeywords:
    """Test expanded LifeOS keywords in directory resolver."""

    def test_readme_resolves_to_lifeos(self):
        from api.services.directory_resolver import resolve_working_directory, _LIFEOS_DIR
        assert resolve_working_directory("update the readme") == _LIFEOS_DIR

    def test_fix_resolves_to_lifeos(self):
        from api.services.directory_resolver import resolve_working_directory, _LIFEOS_DIR
        assert resolve_working_directory("fix the failing test") == _LIFEOS_DIR

    def test_deploy_resolves_to_lifeos(self):
        from api.services.directory_resolver import resolve_working_directory, _LIFEOS_DIR
        assert resolve_working_directory("fix the deploy script") == _LIFEOS_DIR

    def test_api_resolves_to_lifeos(self):
        from api.services.directory_resolver import resolve_working_directory, _LIFEOS_DIR
        assert resolve_working_directory("the api returns 500") == _LIFEOS_DIR

    def test_search_resolves_to_lifeos(self):
        from api.services.directory_resolver import resolve_working_directory, _LIFEOS_DIR
        assert resolve_working_directory("search is broken") == _LIFEOS_DIR

    def test_config_resolves_to_lifeos(self):
        from api.services.directory_resolver import resolve_working_directory, _LIFEOS_DIR
        assert resolve_working_directory("update the config") == _LIFEOS_DIR

    def test_backup_resolves_to_lifeos(self):
        from api.services.directory_resolver import resolve_working_directory, _LIFEOS_DIR
        assert resolve_working_directory("set up database backup") == _LIFEOS_DIR

    def test_unrelated_still_defaults_to_home(self):
        from api.services.directory_resolver import resolve_working_directory, _HOME
        assert resolve_working_directory("clean up my desktop") == _HOME

    def test_existing_vault_keywords_still_work(self):
        from api.services.directory_resolver import resolve_working_directory, _VAULT_DIR
        assert resolve_working_directory("add to the backlog") == _VAULT_DIR
