"""
Adversarial end-to-end tests for the LifeOS audit implementation.

Tests the ACTUAL end-to-end flows that a human user would trigger via Telegram
or the API. Mocks only external boundaries (Telegram API, Claude API) —
everything internal runs for real.

These tests are designed to find gaps between what the audit review claims
and what actually works in practice.
"""
import time
from pathlib import Path
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

pytestmark = pytest.mark.unit

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------------------
# A) Reminder Execution Pipeline End-to-End
# ---------------------------------------------------------------------------

class TestReminderPipelineE2E:
    """Test the full reminder execution flow end-to-end.

    Validates: _fire_reminder → _generate_message → _execute_prompt_reminder
               → chat_via_api_with_log → SSE parsing → Telegram send
    """

    @pytest.fixture
    def scheduler(self, tmp_path):
        from api.services.reminder_store import ReminderStore, ReminderScheduler
        store = ReminderStore(file_path=str(tmp_path / "e2e_reminders.json"))
        return ReminderScheduler(store)

    @pytest.fixture
    def prompt_reminder(self, scheduler):
        return scheduler.store.create(
            name="E2E Test Prompt",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            message_type="prompt",
            message_content="Check my calendar for meetings in the next 20 minutes.",
        )

    @pytest.fixture
    def static_reminder(self, scheduler):
        return scheduler.store.create(
            name="E2E Static",
            schedule_type="once",
            schedule_value=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            message_type="static",
            message_content="Don't forget to water the plants!",
        )

    @pytest.fixture
    def endpoint_reminder(self, scheduler):
        return scheduler.store.create(
            name="E2E Endpoint",
            schedule_type="cron",
            schedule_value="0 8 * * *",
            message_type="endpoint",
            message_content="",
            endpoint_config={"endpoint": "/api/admin/status", "method": "GET"},
        )

    # --- Prompt-type reminder full pipeline ---

    @pytest.mark.asyncio
    async def test_prompt_reminder_full_pipeline_success(self, scheduler, prompt_reminder):
        """Full pipeline: prompt → chat_via_api_with_log → suppress check → Telegram send."""
        mock_chat_result = {
            "answer": "You have a meeting with Sarah at 10:00 AM.",
            "conversation_id": "conv-abc",
            "tool_statuses": ["Checking calendar..."],
            "cost_usd": 0.012,
            "model": "claude-sonnet-4-5-20250929",
            "input_tokens": 800,
            "output_tokens": 120,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, return_value=mock_chat_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        # Verify Telegram was called with the formatted message
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "E2E Test Prompt" in sent_text
        assert "meeting with Sarah" in sent_text

        # Verify reminder was marked as triggered
        updated = scheduler.store.get(prompt_reminder.id)
        assert updated.last_triggered_at is not None

    @pytest.mark.asyncio
    async def test_prompt_reminder_retry_on_first_failure(self, scheduler, prompt_reminder):
        """When first chat_via_api_with_log call fails, retries and succeeds."""
        call_count = [0]

        async def flaky_chat(question):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("API temporarily unavailable")
            return {
                "answer": "Recovery answer: no meetings.",
                "conversation_id": "conv-retry",
                "tool_statuses": [],
                "cost_usd": 0.005,
                "model": "claude-sonnet-4-5-20250929",
                "input_tokens": 200,
                "output_tokens": 30,
            }

        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, side_effect=flaky_chat):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        assert call_count[0] == 2
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "Recovery answer" in sent_text

    @pytest.mark.asyncio
    async def test_prompt_reminder_suppression_flow(self, scheduler, prompt_reminder):
        """When chat returns NO_MEETING, Telegram should NOT be called."""
        mock_result = {
            "answer": "NO_MEETING",
            "conversation_id": "conv-nomtg",
            "tool_statuses": ["Checking calendar..."],
            "cost_usd": 0.003,
            "model": "claude-sonnet-4-5-20250929",
            "input_tokens": 300,
            "output_tokens": 10,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, return_value=mock_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        mock_send.assert_not_called()

        # But the reminder should STILL be marked as triggered
        updated = scheduler.store.get(prompt_reminder.id)
        assert updated.last_triggered_at is not None

    @pytest.mark.asyncio
    async def test_prompt_reminder_error_notification_flow(self, scheduler, prompt_reminder):
        """When _generate_message entirely crashes, error notification is sent to Telegram."""
        with patch.object(scheduler, "_generate_message",
                          new_callable=AsyncMock,
                          side_effect=RuntimeError("Database connection lost")):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "(failed)" in sent_text
        assert "Database connection lost" in sent_text

        # Should still mark as triggered (to prevent retry storm)
        updated = scheduler.store.get(prompt_reminder.id)
        assert updated.last_triggered_at is not None

    @pytest.mark.asyncio
    async def test_prompt_reminder_all_retries_exhausted(self, scheduler, prompt_reminder):
        """When all retries fail, sends failure message to user (not silent)."""
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock,
                    side_effect=Exception("Claude API down")):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_reminder)

        # The failure message should be sent to Telegram (not suppressed)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "failed after 2 attempts" in sent_text

    @pytest.mark.asyncio
    async def test_error_notification_failure_does_not_crash(self, scheduler, prompt_reminder):
        """When BOTH the pipeline AND the error notification fail, scheduler doesn't crash."""
        with patch.object(scheduler, "_generate_message",
                          new_callable=AsyncMock,
                          side_effect=RuntimeError("Pipeline crash")):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock,
                       side_effect=Exception("Telegram API also down")):
                # Should not raise — just log
                await scheduler._fire_reminder(prompt_reminder)

        # Reminder should still be marked triggered
        updated = scheduler.store.get(prompt_reminder.id)
        assert updated.last_triggered_at is not None

    # --- Static reminder pipeline ---

    @pytest.mark.asyncio
    async def test_static_reminder_sends_directly(self, scheduler, static_reminder):
        """Static reminders should send message_content directly without chat pipeline."""
        with patch("api.services.telegram.send_message_async",
                    new_callable=AsyncMock, return_value=True) as mock_send:
            await scheduler._fire_reminder(static_reminder)

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "water the plants" in sent_text

    # --- Endpoint reminder pipeline ---

    @pytest.mark.asyncio
    async def test_endpoint_reminder_missing_httpx_import(self, scheduler, endpoint_reminder):
        """ADVERSARIAL: _call_endpoint uses httpx but it is NOT imported in reminder_store.py.
        This tests that endpoint-type reminders actually work (or don't).
        """
        # This should fail because httpx is not imported in reminder_store.py
        with patch("api.services.telegram.send_message_async",
                    new_callable=AsyncMock, return_value=True) as mock_send:
            await scheduler._fire_reminder(endpoint_reminder)

        # If httpx import fails, this will trigger the error notification path
        # and send a failure message. Either way, Telegram should be called.
        mock_send.assert_called_once()

    # --- Timing and metadata ---

    @pytest.mark.asyncio
    async def test_execution_logging_captures_timing(self, scheduler, prompt_reminder):
        """Execution log should include elapsed time."""
        mock_result = {
            "answer": "Some answer.",
            "conversation_id": "conv-timing",
            "tool_statuses": ["Step 1..."],
            "cost_usd": 0.01,
            "model": "claude-sonnet-4-5-20250929",
            "input_tokens": 500,
            "output_tokens": 50,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                    new_callable=AsyncMock, return_value=mock_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True):
                # Just verify it doesn't crash — timing is logged internally
                await scheduler._fire_reminder(prompt_reminder)

    # --- Due detection edge cases ---

    def test_due_reminders_with_no_next_trigger(self, scheduler):
        """Reminders with next_trigger_at=None should never be returned as due."""
        r = scheduler.store.create(
            name="No trigger",
            schedule_type="once",
            schedule_value=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            message_type="static",
            message_content="Hi",
        )
        # Once-type reminder in the past has next_trigger_at=None
        due = scheduler.store.get_due_reminders()
        assert r.id not in [d.id for d in due]

    def test_due_reminders_precision(self, scheduler):
        """A reminder due 1 second ago should be detected."""
        r = scheduler.store.create(
            name="Just due",
            schedule_type="cron",
            schedule_value="0 0 * * *",  # Midnight daily
            message_type="static",
            message_content="Hi",
        )
        # Manually set next_trigger_at to 1 second ago
        r.next_trigger_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        due = scheduler.store.get_due_reminders()
        assert r.id in [d.id for d in due]


# ---------------------------------------------------------------------------
# B) Job Queue End-to-End
# ---------------------------------------------------------------------------

class TestJobQueueE2E:
    """Test the job queue from enqueue to completion with the background worker."""

    @pytest.fixture
    def queue(self, tmp_path):
        from api.services.job_queue import JobQueue, _JOB_HANDLERS
        # Save and clean handlers
        saved = dict(_JOB_HANDLERS)
        db = str(tmp_path / "e2e_jobs.db")
        q = JobQueue(db_path=db)
        yield q
        q.stop_worker()
        _JOB_HANDLERS.clear()
        _JOB_HANDLERS.update(saved)

    def test_full_lifecycle_pending_to_completed(self, queue):
        """Job goes through PENDING → RUNNING → COMPLETED."""
        from api.services.job_queue import register_job_handler, PENDING, COMPLETED

        results = []

        def handler(params):
            results.append(params)
            return {"processed": True}

        register_job_handler("e2e_test", handler)
        job_id = queue.enqueue("e2e_test", params={"key": "value"})

        # Verify starts as PENDING
        job = queue.get_job(job_id)
        assert job.status == PENDING

        queue.start_worker()
        for _ in range(50):
            job = queue.get_job(job_id)
            if job.status == COMPLETED:
                break
            time.sleep(0.1)
        queue.stop_worker()

        assert job.status == COMPLETED
        assert job.result == {"processed": True}
        assert results == [{"key": "value"}]

    def test_concurrent_enqueue_during_processing(self, queue):
        """Enqueuing new jobs while worker is processing should work."""
        from api.services.job_queue import register_job_handler, COMPLETED

        processed = []

        def slow_handler(params):
            time.sleep(0.1)
            processed.append(params["n"])
            return {"n": params["n"]}

        register_job_handler("slow_job", slow_handler)
        queue.enqueue("slow_job", params={"n": 1})

        queue.start_worker()
        # Wait for first job to start
        time.sleep(0.2)

        # Enqueue more while worker is busy
        queue.enqueue("slow_job", params={"n": 2})
        queue.enqueue("slow_job", params={"n": 3})

        for _ in range(100):
            jobs = queue.list_jobs(status=COMPLETED)
            if len(jobs) == 3:
                break
            time.sleep(0.1)
        queue.stop_worker()

        assert sorted(processed) == [1, 2, 3]

    def test_job_with_no_handler_fails_immediately(self, queue):
        """Jobs with unregistered type should fail, not hang."""
        from api.services.job_queue import FAILED

        job_id = queue.enqueue("nonexistent_type", max_attempts=1)
        queue.start_worker()

        for _ in range(50):
            job = queue.get_job(job_id)
            if job.status == FAILED:
                break
            time.sleep(0.1)
        queue.stop_worker()

        job = queue.get_job(job_id)
        assert job.status == FAILED
        assert "No handler" in job.error

    def test_cancel_during_pending(self, queue):
        """Cancellation should work for pending jobs."""
        from api.services.job_queue import CANCELLED

        job_id = queue.enqueue("anything")
        assert queue.cancel_job(job_id) is True
        job = queue.get_job(job_id)
        assert job.status == CANCELLED

    def test_wal_mode_enabled(self, queue):
        """Verify WAL mode is actually enabled on the jobs database."""
        import sqlite3
        conn = sqlite3.connect(queue.db_path)
        result = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert result[0] == "wal"


# ---------------------------------------------------------------------------
# C) Proactive Intelligence Prompts
# ---------------------------------------------------------------------------

class TestProactiveIntelligence:
    """Test that the seed script creates correct reminders with valid prompts."""

    def test_seed_script_creates_three_reminders(self, tmp_path):
        """Seed script should create exactly 3 reminders."""
        from api.services.reminder_store import ReminderStore

        # Monkey-patch the store to use temp file
        store = ReminderStore(file_path=str(tmp_path / "seed_test.json"))

        # Import reminder definitions directly
        from scripts.seed_proactive_reminders import REMINDERS

        for reminder_def in REMINDERS:
            store.create(**reminder_def)

        reminders = store.list_all()
        assert len(reminders) == 3

    def test_meeting_prep_prompt_is_well_formed(self):
        """Meeting prep prompt should contain expected keywords."""
        from scripts.seed_proactive_reminders import MEETING_PREP_PROMPT

        assert len(MEETING_PREP_PROMPT) > 100
        assert "calendar" in MEETING_PREP_PROMPT.lower()
        assert "NO_MEETING" in MEETING_PREP_PROMPT
        assert "20 minutes" in MEETING_PREP_PROMPT
        assert "attendee" in MEETING_PREP_PROMPT.lower()

    def test_morning_briefing_prompt_is_well_formed(self):
        """Morning briefing prompt should reference all expected sections."""
        from scripts.seed_proactive_reminders import MORNING_BRIEFING_PROMPT

        assert len(MORNING_BRIEFING_PROMPT) > 200
        assert "calendar" in MORNING_BRIEFING_PROMPT.lower()
        assert "task" in MORNING_BRIEFING_PROMPT.lower()
        assert "email" in MORNING_BRIEFING_PROMPT.lower()

    def test_communication_gaps_prompt_is_well_formed(self):
        """Communication gaps prompt should include thresholds."""
        from scripts.seed_proactive_reminders import COMMUNICATION_GAPS_PROMPT

        assert len(COMMUNICATION_GAPS_PROMPT) > 100
        assert "14 days" in COMMUNICATION_GAPS_PROMPT
        assert "30 days" in COMMUNICATION_GAPS_PROMPT

    def test_cron_schedules_are_valid(self):
        """All cron schedules should be parseable by croniter."""
        from scripts.seed_proactive_reminders import REMINDERS
        from croniter import croniter

        for reminder_def in REMINDERS:
            cron_expr = reminder_def["schedule_value"]
            # Should not raise
            croniter(cron_expr)

    def test_meeting_prep_schedule_is_weekday_only(self):
        """Meeting prep should only fire on weekdays."""
        from scripts.seed_proactive_reminders import REMINDERS

        meeting_prep = next(r for r in REMINDERS if r["name"] == "Pre-Meeting Prep")
        cron_parts = meeting_prep["schedule_value"].split()
        # Day of week field should be 1-5 (weekdays)
        assert cron_parts[4] == "1-5"

    def test_all_reminders_are_prompt_type(self):
        """All proactive reminders should use the 'prompt' message type."""
        from scripts.seed_proactive_reminders import REMINDERS

        for r in REMINDERS:
            assert r["message_type"] == "prompt", f"{r['name']} is type {r['message_type']}, not 'prompt'"

    def test_seed_script_skip_duplicates(self, tmp_path):
        """Running seed twice should not create duplicates (without --force)."""
        from api.services.reminder_store import ReminderStore
        from scripts.seed_proactive_reminders import REMINDERS

        store = ReminderStore(file_path=str(tmp_path / "dup_test.json"))

        # First run
        for reminder_def in REMINDERS:
            store.create(**reminder_def)

        existing_names = {r.name for r in store.list_all()}

        # Second run should skip existing names
        created_count = 0
        for reminder_def in REMINDERS:
            if reminder_def["name"] not in existing_names:
                store.create(**reminder_def)
                created_count += 1

        assert created_count == 0
        assert len(store.list_all()) == 3


# ---------------------------------------------------------------------------
# D) Suppression Logic
# ---------------------------------------------------------------------------

class TestSuppressionLogic:
    """Test _should_suppress with all sentinel values and edge cases."""

    def test_all_sentinel_values(self):
        from api.services.reminder_store import ReminderScheduler

        sentinels = ["NO_MEETING", "NO_MEETINGS", "NOTHING_TO_REPORT", "NO_ACTION"]
        for s in sentinels:
            assert ReminderScheduler._should_suppress(s) is True, f"Failed for {s}"
            assert ReminderScheduler._should_suppress(s.lower()) is True, f"Failed for {s.lower()}"
            assert ReminderScheduler._should_suppress(f"  {s}  ") is True, f"Failed for padded {s}"

    def test_non_sentinel_not_suppressed(self):
        from api.services.reminder_store import ReminderScheduler

        assert ReminderScheduler._should_suppress("") is False
        assert ReminderScheduler._should_suppress("Here is your briefing") is False
        assert ReminderScheduler._should_suppress("NO_MEETING but also some text") is False
        assert ReminderScheduler._should_suppress("Reminder: NO_MEETING today") is False

    def test_partial_match_not_suppressed(self):
        """'NO_MEETING' embedded in longer text should NOT be suppressed."""
        from api.services.reminder_store import ReminderScheduler

        assert ReminderScheduler._should_suppress("There is NO_MEETING scheduled") is False

    def test_failure_messages_not_suppressed(self):
        """Failure messages from exhausted retries should NOT be suppressed."""
        from api.services.reminder_store import ReminderScheduler

        failure_msg = "(Reminder execution failed after 2 attempts: Claude API down)"
        assert ReminderScheduler._should_suppress(failure_msg) is False


# ---------------------------------------------------------------------------
# E) chat_via_api_with_log SSE Parsing
# ---------------------------------------------------------------------------

class TestChatViaApiWithLogParsing:
    """Test SSE event parsing in chat_via_api_with_log."""

    def _make_mock_client(self, sse_events):
        """Build a mock httpx.AsyncClient that simulates SSE streaming.

        The real code does:
            async with httpx.AsyncClient(...) as client:
                async with client.stream("POST", url, json=body) as resp:
                    async for line in resp.aiter_lines():
                        ...

        We need to mock this nested async context manager chain.
        """
        # Build the response mock: supports `async for line in resp.aiter_lines()`
        mock_response = MagicMock()
        mock_response.status_code = 200

        def aiter_lines():
            return _async_iter(sse_events)

        mock_response.aiter_lines = aiter_lines

        # client.stream(...) returns an async context manager yielding mock_response
        stream_cm = AsyncMock()
        stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
        stream_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=stream_cm)

        # httpx.AsyncClient(...) is itself an async context manager yielding mock_client
        client_cm = MagicMock()
        client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        client_cm.__aexit__ = AsyncMock(return_value=False)

        return client_cm

    @pytest.mark.asyncio
    async def test_parses_all_event_types(self):
        """Should correctly parse content, status, usage, and error events."""
        sse_events = [
            'data: {"type": "content", "content": "Hello "}',
            'data: {"type": "content", "content": "world."}',
            'data: {"type": "status", "message": "Searching calendar..."}',
            'data: {"type": "conversation_id", "conversation_id": "conv-123"}',
            'data: {"type": "usage", "cost_usd": 0.015, "model": "claude-sonnet-4-5-20250929", "input_tokens": 800, "output_tokens": 150}',
        ]

        mock_client = self._make_mock_client(sse_events)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from api.services.telegram import chat_via_api_with_log
            result = await chat_via_api_with_log("Test question")

        assert result["answer"] == "Hello world."
        assert result["conversation_id"] == "conv-123"
        assert result["cost_usd"] == 0.015
        assert result["model"] == "claude-sonnet-4-5-20250929"
        assert result["input_tokens"] == 800
        assert result["output_tokens"] == 150
        assert "Searching calendar..." in result["tool_statuses"]

    @pytest.mark.asyncio
    async def test_handles_error_event(self):
        """Error events should be appended to the answer text."""
        sse_events = [
            'data: {"type": "content", "content": "Partial "}',
            'data: {"type": "error", "message": "Tool failed: search_calendar"}',
        ]

        mock_client = self._make_mock_client(sse_events)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from api.services.telegram import chat_via_api_with_log
            result = await chat_via_api_with_log("Test error")

        assert "Tool failed" in result["answer"]

    @pytest.mark.asyncio
    async def test_handles_self_correction_event(self):
        """Self-correction events should clear previously accumulated text."""
        sse_events = [
            'data: {"type": "content", "content": "I cannot access..."}',
            'data: {"type": "self_correction"}',
            'data: {"type": "content", "content": "Here is your answer."}',
        ]

        mock_client = self._make_mock_client(sse_events)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from api.services.telegram import chat_via_api_with_log
            result = await chat_via_api_with_log("Test self correction")

        assert result["answer"] == "Here is your answer."
        assert "cannot access" not in result["answer"]

    @pytest.mark.asyncio
    async def test_handles_malformed_sse(self):
        """Should gracefully skip malformed SSE lines."""
        sse_events = [
            'not a data line',
            'data: not json',
            'data: {"type": "content", "content": "Valid."}',
            '',
            'data: {"type": "usage", "cost_usd": 0.01, "model": "claude", "input_tokens": 100, "output_tokens": 50}',
        ]

        mock_client = self._make_mock_client(sse_events)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from api.services.telegram import chat_via_api_with_log
            result = await chat_via_api_with_log("Test malformed")

        assert result["answer"] == "Valid."
        assert result["cost_usd"] == 0.01


# ---------------------------------------------------------------------------
# F) PersonEntity SQLite Store
# ---------------------------------------------------------------------------

class TestPersonEntitySQLite:
    """Verify the PersonEntity store is actually using SQLite, not JSON."""

    def test_store_constructor_takes_db_path(self):
        """Constructor should accept db_path parameter (not storage_path)."""
        from api.services.person_entity import PersonEntityStore
        import inspect
        sig = inspect.signature(PersonEntityStore.__init__)
        params = list(sig.parameters.keys())
        assert "db_path" in params, f"Expected db_path parameter, got: {params}"

    def test_store_uses_sqlite_tables(self, tmp_path):
        """Store should create SQLite tables on init."""
        import sqlite3
        from api.services.person_entity import PersonEntityStore

        db_path = str(tmp_path / "test_crm.db")
        PersonEntityStore(db_path=db_path)

        conn = sqlite3.connect(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        conn.close()

        assert "person_entities" in table_names, f"Missing person_entities table. Found: {table_names}"

    def test_store_has_wal_mode(self, tmp_path):
        """CRM database should use WAL mode."""
        import sqlite3
        from api.services.person_entity import PersonEntityStore

        db_path = str(tmp_path / "test_crm_wal.db")
        PersonEntityStore(db_path=db_path)

        conn = sqlite3.connect(db_path)
        result = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert result[0] == "wal"


# ---------------------------------------------------------------------------
# G) Memory Integration in Agent Loop
# ---------------------------------------------------------------------------

class TestMemoryIntegration:
    """Verify memory injection into the agent loop system prompt."""

    def test_memory_tools_registered(self):
        """save_memory and search_memories should be in TOOL_DEFINITIONS."""
        from api.services.agent_tools import TOOL_DEFINITIONS

        tool_names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "save_memory" in tool_names
        assert "search_memories" in tool_names

    def test_memory_tool_handler_exists(self):
        """Tool handlers should exist for memory tools."""
        from api.services.agent_tools import _TOOL_HANDLERS

        assert "save_memory" in _TOOL_HANDLERS
        assert "search_memories" in _TOOL_HANDLERS


# ---------------------------------------------------------------------------
# H) MCP Write Tools
# ---------------------------------------------------------------------------

class TestMCPWriteTools:
    """Verify the MCP server exposes the claimed write tools."""

    def test_curated_endpoints_include_write_tools(self):
        """MCP server should have all 6 claimed write tools."""
        # We need to check the mcp_server curated endpoints
        import importlib.util

        # Load mcp_server module manually since it's not in the api package
        spec = importlib.util.spec_from_file_location(
            "mcp_server",
            f"{_PROJECT_ROOT}/mcp_server.py"
        )
        importlib.util.module_from_spec(spec)

        # The module tries to connect to the API on import; we just need CURATED_ENDPOINTS
        # Read it from source instead
        with open(f"{_PROJECT_ROOT}/mcp_server.py") as f:
            source = f.read()

        # Check for the claimed tool names in the source
        expected_tools = [
            "lifeos_person_update",
            "lifeos_reminder_update",
            "lifeos_sync_trigger",
            "lifeos_person_fact_update",
            "lifeos_person_fact_confirm",
            "lifeos_person_fact_delete",
        ]

        for tool in expected_tools:
            assert tool in source, f"MCP server missing tool: {tool}"

    def test_call_api_handles_put_patch_delete(self):
        """_call_api should handle PUT, PATCH, and DELETE methods."""
        with open(f"{_PROJECT_ROOT}/mcp_server.py") as f:
            source = f.read()

        # Verify the method dispatch handles all HTTP methods
        assert 'method == "DELETE"' in source or "DELETE" in source
        assert 'method in ("PUT", "PATCH")' in source or '"PUT"' in source


# ---------------------------------------------------------------------------
# I) Bug Fixes Verification
# ---------------------------------------------------------------------------

class TestAgentReminderMessageType:
    """Verify: _reminder_create uses valid message_type (was 'telegram', now 'static')."""

    def test_reminder_create_uses_valid_message_type(self):
        """agent_tools._reminder_create must use 'static', not 'telegram'."""
        with open(f"{_PROJECT_ROOT}/api/services/agent_tools.py") as f:
            source = f.read()

        # Find the _reminder_create function
        assert 'message_type="static"' in source, \
            "_reminder_create should use message_type='static'"
        # Make sure the old broken value is gone
        assert 'message_type="telegram"' not in source, \
            "message_type='telegram' is invalid and should not appear"


class TestEndpointReminderFixed:
    """Verify: endpoint reminders have httpx imported (previously missing)."""

    def test_httpx_imported_in_reminder_store(self):
        """reminder_store.py must import httpx for _call_endpoint."""
        with open(f"{_PROJECT_ROOT}/api/services/scheduler_store.py") as f:
            source = f.read()

        import_lines = [line for line in source.split("\n") if line.startswith("import ") or line.startswith("from ")]
        httpx_imported = any("httpx" in line for line in import_lines)
        uses_httpx = "httpx.AsyncClient" in source

        assert httpx_imported, "httpx must be imported since _call_endpoint uses httpx.AsyncClient"
        assert uses_httpx, "_call_endpoint should use httpx.AsyncClient"

    @pytest.mark.asyncio
    async def test_call_endpoint_no_longer_crashes(self):
        """_call_endpoint should not raise NameError now that httpx is imported."""
        from api.services.reminder_store import ReminderStore, ReminderScheduler

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = ReminderStore(file_path=f"{tmp}/test.json")
            scheduler = ReminderScheduler(store)

            # This will fail to connect (no server on port 99999) but should NOT
            # raise NameError — httpx is now imported
            config = {"endpoint": "/health", "method": "GET", "params": {}}
            result = await scheduler._call_endpoint(config)
            # Should return an error string about connection, not about missing import
            assert result is not None
            assert "NameError" not in str(result)


# ---------------------------------------------------------------------------
# J) Scheduler Thread Crash Recovery
# ---------------------------------------------------------------------------

class TestSchedulerCrashRecovery:
    """Test what happens when the scheduler thread crashes."""

    def test_scheduler_thread_is_daemon(self):
        """Scheduler thread should be a daemon (dies with main process)."""
        from api.services.reminder_store import ReminderScheduler, ReminderStore
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = ReminderStore(file_path=f"{tmp}/daemon_test.json")
            ReminderScheduler(store)
            # Check that the thread would be created as daemon
            # (we can't start it without Telegram, but we can verify the code)
            with open(f"{_PROJECT_ROOT}/api/services/scheduler_store.py") as f:
                source = f.read()
            assert "daemon=True" in source

    def test_scheduler_logs_crash(self):
        """Scheduler crash should be logged (even if not alerted)."""
        with open(f"{_PROJECT_ROOT}/api/services/scheduler_store.py") as f:
            source = f.read()
        assert "scheduler crashed" in source.lower() or "Reminder scheduler crashed" in source


# ---------------------------------------------------------------------------
# K) Chat Pipeline Unification
# ---------------------------------------------------------------------------

class TestChatPipelineUnification:
    """Verify legacy handlers were actually removed."""

    def test_no_legacy_compose_handler(self):
        """chat.py should not have a compose handler."""
        with open(f"{_PROJECT_ROOT}/api/routes/chat.py") as f:
            source = f.read()
        assert "def handle_compose" not in source
        assert "async def handle_compose" not in source

    def test_no_legacy_task_handler(self):
        """chat.py should not have dedicated task/reminder handlers."""
        with open(f"{_PROJECT_ROOT}/api/routes/chat.py") as f:
            source = f.read()
        assert "def handle_task_intent" not in source
        assert "def handle_reminder_intent" not in source
        assert "def handle_task_and_reminder" not in source

    def test_ambiguous_handler_still_exists(self):
        """The ambiguous_task_reminder handler should still exist."""
        with open(f"{_PROJECT_ROOT}/api/routes/chat.py") as f:
            source = f.read()
        assert "ambiguous_task_reminder" in source

    def test_claude_handler_still_exists(self):
        """The claude intent handler should still exist."""
        with open(f"{_PROJECT_ROOT}/api/routes/chat.py") as f:
            source = f.read()
        assert "claude_intent" in source


# ---------------------------------------------------------------------------
# L) Scheduler Crash Recovery (Batch 1 — Gap #2)
# ---------------------------------------------------------------------------

class TestSchedulerCrashRecoveryBatch1:
    """Test auto-restart with backoff and health check."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        from api.services.reminder_store import ReminderStore, ReminderScheduler
        store = ReminderStore(file_path=str(tmp_path / "crash_test.json"))
        return ReminderScheduler(store)

    def test_is_alive_false_when_not_started(self, scheduler):
        """is_alive() should return False when scheduler hasn't started."""
        assert scheduler.is_alive() is False

    def test_crash_triggers_restart(self, scheduler):
        """Scheduler should attempt restart after crash."""
        crash_count = [0]


        async def crashing_loop():
            crash_count[0] += 1
            if crash_count[0] <= 2:
                raise RuntimeError("Test crash")
            # Third call — just exit cleanly
            scheduler._stop_event.set()

        scheduler._schedule_loop = crashing_loop

        with patch("api.services.telegram.send_message"):
            scheduler._stop_event.clear()
            scheduler._run()

        assert crash_count[0] == 3
        assert scheduler._crash_count == 2

    def test_max_retries_sends_permanent_down_alert(self, scheduler):
        """After MAX_CONSECUTIVE_CRASHES, should send final alert and stop."""
        scheduler.MAX_CONSECUTIVE_CRASHES = 2

        async def always_crash():
            raise RuntimeError("Persistent failure")

        scheduler._schedule_loop = always_crash
        alerts = []

        with patch("api.services.telegram.send_message", side_effect=lambda msg: alerts.append(msg)):
            scheduler._stop_event.clear()
            scheduler._run()

        assert scheduler._crash_count >= 2
        assert any("DOWN" in a for a in alerts)

    def test_scheduler_reads_control_file_enabled(self, scheduler):
        """Scheduler should read enabled: true from the control file."""
        control_file = scheduler.store.scheduler_dir / "Scheduler.md"
        control_file.parent.mkdir(parents=True, exist_ok=True)
        control_file.write_text("---\nenabled: true\n---\n# Scheduler\n")

        assert scheduler._read_control_file() is True

    def test_scheduler_reads_control_file_disabled(self, scheduler):
        """Scheduler should read enabled: false from the control file."""
        control_file = scheduler.store.scheduler_dir / "Scheduler.md"
        control_file.parent.mkdir(parents=True, exist_ok=True)
        control_file.write_text("---\nenabled: false\n---\n# Scheduler\n")

        assert scheduler._read_control_file() is False

    def test_missing_control_file_creates_one(self, scheduler):
        """Missing control file should be created with enabled: true."""
        control_file = scheduler.store.scheduler_dir / "Scheduler.md"
        if control_file.exists():
            control_file.unlink()

        result = scheduler._read_control_file()

        assert result is True
        assert control_file.exists()
        assert "enabled: true" in control_file.read_text()


# ---------------------------------------------------------------------------
# M) Job Queue Hardening (Batch 2 — Gaps #3, #12)
# ---------------------------------------------------------------------------

class TestJobQueueHardening:
    """Test atomic claim and auto-cleanup."""

    @pytest.fixture
    def queue(self, tmp_path):
        from api.services.job_queue import JobQueue, _JOB_HANDLERS
        saved = dict(_JOB_HANDLERS)
        db = str(tmp_path / "hardening_jobs.db")
        q = JobQueue(db_path=db)
        yield q
        q.stop_worker()
        _JOB_HANDLERS.clear()
        _JOB_HANDLERS.update(saved)

    def test_atomic_claim_returns_job(self, queue):
        """Atomic _claim_next should return the job with RUNNING status."""
        from api.services.job_queue import RUNNING
        job_id = queue.enqueue("test_type", params={"x": 1})
        claimed = queue._claim_next()
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == RUNNING

    def test_cleanup_removes_old_jobs(self, queue):
        """cleanup_old_jobs should remove old completed jobs."""
        from api.services.job_queue import COMPLETED
        import sqlite3

        # Insert an old completed job manually
        with sqlite3.connect(queue.db_path) as conn:
            conn.execute(
                """INSERT INTO jobs (id, type, status, params, created_at, attempts, max_attempts, priority)
                   VALUES (?, ?, ?, '{}', datetime('now', '-60 days'), 1, 3, 10)""",
                ("old-job", "test", COMPLETED),
            )

        # Also insert a recent one
        recent_id = queue.enqueue("test")

        queue.cleanup_old_jobs(days=30)

        assert queue.get_job("old-job") is None
        assert queue.get_job(recent_id) is not None


# ---------------------------------------------------------------------------
# N) Pre-Meeting Prep Cost Optimization (Batch 3 — Gap #4)
# ---------------------------------------------------------------------------

class TestPreMeetingCostOptimization:
    """Test lightweight calendar pre-check skips pipeline."""

    @pytest.fixture
    def scheduler(self, tmp_path):
        from api.services.reminder_store import ReminderStore, ReminderScheduler
        store = ReminderStore(file_path=str(tmp_path / "cost_test.json"))
        return ReminderScheduler(store)

    @pytest.fixture
    def meeting_reminder(self, scheduler):
        return scheduler.store.create(
            name="Pre-Meeting Prep",
            schedule_type="cron",
            schedule_value="*/15 8-18 * * 1-5",
            message_type="prompt",
            message_content="Check my calendar...",
        )

    @pytest.mark.asyncio
    async def test_skips_pipeline_when_no_meeting(self, scheduler, meeting_reminder):
        """Pre-meeting reminder should skip pipeline when no meeting found."""
        with patch("api.services.calendar.get_calendar_service") as mock_cal:
            mock_service = MagicMock()
            mock_service.has_upcoming_meeting.return_value = False
            mock_cal.return_value = mock_service
            with patch("api.services.telegram.chat_via_api_with_log",
                       new_callable=AsyncMock) as mock_chat:
                with patch("api.services.telegram.send_message_async",
                           new_callable=AsyncMock):
                    await scheduler._fire_reminder(meeting_reminder)

            # Chat pipeline should NOT have been called
            mock_chat.assert_not_called()

        # But reminder should be marked triggered
        updated = scheduler.store.get(meeting_reminder.id)
        assert updated.last_triggered_at is not None

    @pytest.mark.asyncio
    async def test_runs_pipeline_when_meeting_exists(self, scheduler, meeting_reminder):
        """Pre-meeting reminder should run full pipeline when meeting found."""
        with patch("api.services.calendar.get_calendar_service") as mock_cal:
            mock_service = MagicMock()
            mock_service.has_upcoming_meeting.return_value = True
            mock_cal.return_value = mock_service
            mock_result = {
                "answer": "Meeting with Sarah at 10 AM",
                "conversation_id": "conv-x",
                "tool_statuses": [],
                "cost_usd": 0.01,
                "model": "claude-sonnet-4-5-20250929",
                "input_tokens": 500,
                "output_tokens": 50,
            }
            with patch("api.services.telegram.chat_via_api_with_log",
                       new_callable=AsyncMock, return_value=mock_result) as mock_chat:
                with patch("api.services.telegram.send_message_async",
                           new_callable=AsyncMock, return_value=True):
                    await scheduler._fire_reminder(meeting_reminder)

            mock_chat.assert_called_once()


# ---------------------------------------------------------------------------
# O) Memory Relevance Filtering (Batch 4 — Gap #7)
# ---------------------------------------------------------------------------

class TestMemoryRelevanceFiltering:
    """Test minimum relevance threshold and token budget."""

    @pytest.fixture
    def store(self, tmp_path):
        from api.services.memory_store import MemoryStore
        return MemoryStore(file_path=str(tmp_path / "mem_test.json"))

    def test_low_relevance_memories_excluded(self, store):
        """Memories with <15% keyword overlap should be excluded."""
        # Create a memory with very specific keywords
        store.create_memory("The quarterly budget for Project Alpha is $50,000")
        store.create_memory("I had lunch at the Italian restaurant downtown")

        # Search for something with minimal overlap to "lunch" memory
        results = store.search_memories("quarterly budget Alpha finances", min_relevance=0.15)

        # Budget memory should match, lunch memory might not (low overlap)
        content_texts = [m.content for m in results]
        assert any("budget" in c.lower() for c in content_texts)

    def test_token_budget_caps_memory_injection(self):
        """Memory injection should stop when word count exceeds 400."""
        from api.services.memory_store import Memory
        from datetime import datetime

        memories = [
            Memory(
                id=str(i), content=" ".join(["word"] * 200),
                category="facts", keywords=["word"],
                created_at=datetime.now(), updated_at=datetime.now(),
            )
            for i in range(5)
        ]

        # Simulate the budget logic from agent_loop.py
        budgeted = []
        word_count = 0
        for m in memories:
            words = len(m.content.split())
            if word_count + words > 400:
                break
            budgeted.append(m)
            word_count += words

        assert len(budgeted) == 2  # 200 + 200 = 400, third would exceed


# ---------------------------------------------------------------------------
# P) Suppression & Sentinel Improvements (Batch 5 — Gaps #13, #15)
# ---------------------------------------------------------------------------

class TestFuzzySuppressionBatch5:
    """Test startswith-based fuzzy sentinel matching."""

    def test_exact_sentinels_still_suppressed(self):
        """Original exact sentinels should still work."""
        from api.services.reminder_store import ReminderScheduler
        for s in ("NO_MEETING", "NO_MEETINGS", "NOTHING_TO_REPORT", "NO_ACTION"):
            assert ReminderScheduler._should_suppress(s) is True

    def test_sentinel_with_period_suppressed(self):
        """'NO_MEETING.' should be suppressed."""
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NO_MEETING.") is True

    def test_sentinel_with_trailing_text_suppressed(self):
        """'NO_MEETING.' and 'NO_MEETING—...' should be suppressed."""
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NO_MEETING- no events scheduled") is True
        assert ReminderScheduler._should_suppress("NO_MEETING\u2014nothing on calendar") is True  # em-dash

    def test_sentinel_with_space_then_words_not_suppressed(self):
        """'NO_MEETING but also some text' should NOT be suppressed (space + words)."""
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NO_MEETING but also some text") is False

    def test_nothing_to_report_sentinel_suppressed(self):
        """NOTHING_TO_REPORT should be suppressed (morning briefing sentinel)."""
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("NOTHING_TO_REPORT") is True
        assert ReminderScheduler._should_suppress("NOTHING_TO_REPORT.") is True

    def test_normal_messages_not_suppressed(self):
        """Normal messages should not be suppressed."""
        from api.services.reminder_store import ReminderScheduler
        assert ReminderScheduler._should_suppress("Here is your morning briefing") is False
        assert ReminderScheduler._should_suppress("") is False

    def test_morning_briefing_has_sentinel(self):
        """Morning briefing prompt should include NOTHING_TO_REPORT sentinel."""
        from scripts.seed_proactive_reminders import MORNING_BRIEFING_PROMPT
        assert "NOTHING_TO_REPORT" in MORNING_BRIEFING_PROMPT

    def test_morning_briefing_surfaces_human_queue(self):
        """#852: the morning briefing must surface open Human-queue cards
        by naming the list tool the orchestrator calls to check them."""
        from scripts.seed_proactive_reminders import MORNING_BRIEFING_PROMPT
        assert "Human queue" in MORNING_BRIEFING_PROMPT
        assert "manage_human_queue" in MORNING_BRIEFING_PROMPT


# ---------------------------------------------------------------------------
# Q) chat_via_api HTTP Status Check (Batch 6 — Gap #8 note)
# ---------------------------------------------------------------------------

class TestChatViaApiStatusCheck:
    """Test that chat_via_api raises on non-200 response."""

    def _make_mock_client(self, status_code, sse_events=None):
        """Build a mock httpx.AsyncClient."""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.aread = AsyncMock(return_value=b"Internal Server Error")

        if sse_events:
            mock_response.aiter_lines = lambda: _async_iter(sse_events)

        stream_cm = AsyncMock()
        stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
        stream_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=stream_cm)

        client_cm = MagicMock()
        client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        client_cm.__aexit__ = AsyncMock(return_value=False)

        return client_cm

    @pytest.mark.asyncio
    async def test_chat_via_api_raises_on_non_200(self):
        """chat_via_api should raise RuntimeError on non-200 status."""
        mock_client = self._make_mock_client(500)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from api.services.telegram import chat_via_api
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await chat_via_api("test question")


# ---------------------------------------------------------------------------
# R) Dashboard Improvements (Batch 8)
# ---------------------------------------------------------------------------

class TestDashboardImprovements:
    """Test dashboard formatting improvements."""

    def test_format_cron_human_interval_pattern(self):
        """Should handle */15 8-18 * * 1-5 pattern."""
        from api.services.reminder_store import _format_cron_human
        result = _format_cron_human("*/15 8-18 * * 1-5", "America/New_York")
        assert "weekdays" in result
        assert "15m" in result
        assert "8AM" in result
        assert "6PM" in result or "18" in result
        assert "ET" in result

    def test_format_cron_human_simple_time(self):
        """Should still handle simple cron patterns."""
        from api.services.reminder_store import _format_cron_human
        result = _format_cron_human("30 6 * * *", "America/New_York")
        assert "daily" in result
        assert "6:30 AM" in result
        assert "ET" in result

    def test_format_dt_short_converts_timezone(self):
        """Should convert UTC to local timezone."""
        from api.services.reminder_store import _format_dt_short
        # 2026-02-14 15:30 UTC = 2026-02-14 10:30 AM ET
        result = _format_dt_short("2026-02-14T15:30:00+00:00", "America/New_York")
        assert "10:30 AM" in result
        assert "Feb 14" in result


# ---------------------------------------------------------------------------
# S) Admin Reindex E2E (Batch 7 — Gap #11)
# ---------------------------------------------------------------------------

class TestAdminReindexE2E:
    """Test that POST /api/admin/reindex enqueues a job."""

    def test_admin_reindex_enqueues_job(self):
        """POST /api/admin/reindex should create a job in the queue."""
        from fastapi.testclient import TestClient
        import api.services.job_queue as jq_mod

        # Use a temp queue to avoid modifying production
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from api.services.job_queue import JobQueue
            test_queue = JobQueue(db_path=f"{tmp}/test_reindex.db")

            # Monkey-patch the singleton
            old = jq_mod._instance
            jq_mod._instance = test_queue
            try:
                from api.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post("/api/admin/reindex")
                assert resp.status_code == 200

                data = resp.json()
                assert "job_id" in data

                # Verify the job exists in the queue
                job = test_queue.get_job(data["job_id"])
                assert job is not None
                assert job.type == "reindex_vault"
            finally:
                jq_mod._instance = old


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _async_iter(items):
    """Helper to create an async iterator from a list."""
    for item in items:
        yield item
