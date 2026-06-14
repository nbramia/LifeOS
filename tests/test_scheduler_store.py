"""
Tests for the Scheduler Store and Scheduler (renamed from the reminder store).

Covers CRUD, cron computation, auto-disable, due detection, suppression,
prompt execution, the markdown round-trip, markdown-as-source-of-truth, and
the auto-generated dashboard.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

from api.services.scheduler_store import (
    SchedulerStore,
    SchedulerScheduler,
    ScheduleEntry,
    compute_next_trigger,
    _format_entry_line,
    _parse_entry_line,
    _format_cron_human,
    _format_dt_short,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return SchedulerStore(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "scheduler_index.json",
    )


class TestSchedulerStoreCRUD:
    def test_create_schedule(self, store):
        entry = store.create(
            name="Test Schedule",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            message_type="static",
            message_content="Hello!",
        )
        assert entry.id
        assert entry.name == "Test Schedule"
        assert entry.enabled is True
        assert entry.created_at
        assert entry.action == "notify"  # mapped from static

    def test_create_with_explicit_action(self, store):
        entry = store.create(
            name="Agent job",
            schedule_type="cron",
            schedule_value="0 9 * * 6",
            action="agent",
            executor="cloud",
            message_content="Draft my weekly review",
        )
        assert entry.action == "agent"
        assert entry.executor == "cloud"

    def test_get(self, store):
        created = store.create(
            name="Test", schedule_type="once",
            schedule_value=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            message_type="static", message_content="Hi",
        )
        found = store.get(created.id)
        assert found is not None and found.name == "Test"

    def test_get_nonexistent(self, store):
        assert store.get("nope") is None

    def test_list_all(self, store):
        store.create(name="A", schedule_type="cron", schedule_value="0 9 * * *",
                     message_type="static", message_content="a")
        store.create(name="B", schedule_type="cron", schedule_value="0 10 * * *",
                     message_type="static", message_content="b")
        assert len(store.list_all()) == 2

    def test_update(self, store):
        created = store.create(name="Original", schedule_type="cron",
                               schedule_value="0 9 * * *", message_type="static",
                               message_content="Hello")
        updated = store.update(created.id, name="Updated", message_content="Bye")
        assert updated.name == "Updated"
        assert updated.message_content == "Bye"

    def test_update_nonexistent(self, store):
        assert store.update("nope", name="X") is None

    def test_delete(self, store):
        created = store.create(name="Delete Me", schedule_type="once",
                               schedule_value=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                               message_type="static", message_content="Hi")
        assert store.delete(created.id) is True
        assert store.get(created.id) is None

    def test_delete_nonexistent(self, store):
        assert store.delete("nope") is False


class TestMarkdownSourceOfTruth:
    """Markdown (Inbox.md) is the source of truth; the index is a cache."""

    def test_create_writes_inbox_line(self, store):
        entry = store.create(name="Water plants", schedule_type="cron",
                             schedule_value="0 18 * * *", message_type="static",
                             message_content="hydrate")
        content = store.inbox_path.read_text(encoding="utf-8")
        assert f"<!-- id:{entry.id} -->" in content
        assert "Water plants" in content
        assert "[cron:: 0 18 * * *]" in content

    def test_external_edit_reflected_on_reindex(self, store):
        entry = store.create(name="Daily", schedule_type="cron",
                             schedule_value="0 9 * * *", message_type="static",
                             message_content="x")
        # Externally edit the cron value in the markdown line.
        content = store.inbox_path.read_text(encoding="utf-8")
        patched = content.replace("[cron:: 0 9 * * *]", "[cron:: 0 10 * * *]")
        store.inbox_path.write_text(patched, encoding="utf-8")

        store.reindex_file(str(store.inbox_path))
        assert store.get(entry.id).schedule_value == "0 10 * * *"

    def test_external_checkbox_toggle_disables(self, store):
        entry = store.create(name="Toggle", schedule_type="cron",
                             schedule_value="0 9 * * *", message_type="static",
                             message_content="x")
        content = store.inbox_path.read_text(encoding="utf-8")
        patched = content.replace("- [ ] Toggle", "- [x] Toggle")
        store.inbox_path.write_text(patched, encoding="utf-8")

        store.reindex_file(str(store.inbox_path))
        assert store.get(entry.id).enabled is False

    def test_reindex_preserves_message_content(self, store):
        """An edit to a definition field leaves the instruction body intact."""
        entry = store.create(name="Briefing", schedule_type="cron",
                             schedule_value="0 9 * * *", message_type="prompt",
                             message_content="What's on my calendar?")
        # Editing only the cron field (body untouched) must not wipe the content.
        content = store.inbox_path.read_text(encoding="utf-8")
        patched = content.replace("[cron:: 0 9 * * *]", "[cron:: 30 9 * * *]")
        store.inbox_path.write_text(patched, encoding="utf-8")

        store.reindex_file(str(store.inbox_path))
        refreshed = store.get(entry.id)
        assert refreshed.schedule_value == "30 9 * * *"
        assert refreshed.message_content == "What's on my calendar?"

    def test_create_writes_message_content_to_markdown(self, store):
        """The instruction text is human-readable in Inbox.md, not just the cache."""
        store.create(name="Briefing", schedule_type="cron", schedule_value="0 9 * * *",
                     message_type="prompt", message_content="What's on my calendar?")
        content = store.inbox_path.read_text(encoding="utf-8")
        assert "> What's on my calendar?" in content

    def test_external_edit_message_content_round_trips(self, store):
        """Hand-editing the instruction body in markdown updates the cache on reindex."""
        entry = store.create(name="Briefing", schedule_type="cron",
                             schedule_value="0 9 * * *", message_type="prompt",
                             message_content="old instruction")
        content = store.inbox_path.read_text(encoding="utf-8")
        patched = content.replace("> old instruction", "> new instruction")
        assert patched != content  # sanity: the body line was present to edit
        store.inbox_path.write_text(patched, encoding="utf-8")

        store.reindex_file(str(store.inbox_path))
        assert store.get(entry.id).message_content == "new instruction"

    def test_multiline_message_content_round_trips(self, store, tmp_path):
        """Multi-line instructions survive a full rebuild from markdown."""
        multiline = "Line one.\n\nLine three after a blank."
        store.create(name="Multi", schedule_type="cron", schedule_value="0 9 * * *",
                     message_type="prompt", message_content=multiline)
        # A fresh store rebuilds purely from the vault markdown.
        store2 = SchedulerStore(
            vault_path=tmp_path / "vault",
            index_path=tmp_path / "scheduler_index_multi.json",
        )
        names = {e.name: e for e in store2.list_all()}
        assert names["Multi"].message_content == multiline

    def test_delete_removes_body_lines(self, store):
        """Deleting a schedule removes its body block, not just the checkbox line."""
        entry = store.create(name="Doomed", schedule_type="cron", schedule_value="0 9 * * *",
                             message_type="prompt", message_content="erase me")
        store.delete(entry.id)
        content = store.inbox_path.read_text(encoding="utf-8")
        assert "erase me" not in content
        assert entry.id not in content

    def test_rebuild_index_from_markdown(self, store, tmp_path):
        store.create(name="Persist", schedule_type="cron", schedule_value="0 9 * * *",
                     message_type="static", message_content="hi")
        # A fresh store over the same vault reads the markdown back.
        store2 = SchedulerStore(
            vault_path=tmp_path / "vault",
            index_path=tmp_path / "scheduler_index2.json",
        )
        names = {e.name for e in store2.list_all()}
        assert "Persist" in names


class TestRoundTrip:
    """parse → format → parse is lossless for the markdown definition fields."""

    def test_cron_round_trip(self):
        line = ("- [ ] Weekly review [cron:: 0 9 * * 6] [tz:: America/New_York] "
                "[action:: agent] [mtype:: prompt] #cloud "
                "[created:: 2026-05-28T12:00:00+00:00] <!-- id:abc123 -->")
        entry = _parse_entry_line(line)
        assert entry is not None
        assert entry.name == "Weekly review"
        assert entry.schedule_type == "cron"
        assert entry.schedule_value == "0 9 * * 6"
        assert entry.timezone == "America/New_York"
        assert entry.action == "agent"
        assert entry.message_type == "prompt"
        assert entry.executor == "cloud"
        assert entry.enabled is True
        assert entry.id == "abc123"
        # Round-trip: re-formatting the parsed entry reproduces the line.
        assert _format_entry_line(entry) == line

    def test_once_round_trip(self):
        entry = ScheduleEntry(
            id="s2", name="One off", schedule_type="once",
            schedule_value="2026-06-03T15:05:00", action="notify",
            message_type="static", enabled=True,
            created_at="2026-05-28T12:00:00+00:00",
        )
        line = _format_entry_line(entry)
        reparsed = _parse_entry_line(line)
        assert reparsed.schedule_type == "once"
        assert reparsed.schedule_value == "2026-06-03T15:05:00"
        assert _format_entry_line(reparsed) == line

    def test_disabled_checkbox_round_trip(self):
        entry = ScheduleEntry(
            id="s3", name="Paused", schedule_type="cron", schedule_value="0 9 * * *",
            enabled=False, created_at="2026-05-28T12:00:00+00:00",
        )
        line = _format_entry_line(entry)
        assert line.startswith("- [x] Paused")
        assert _parse_entry_line(line).enabled is False

    def test_non_schedule_line_returns_none(self):
        assert _parse_entry_line("# A heading") is None
        assert _parse_entry_line("- [ ] just a task with no trigger") is None
        assert _parse_entry_line("plain text") is None


class TestCronComputation:
    def test_cron_next_trigger(self):
        entry = ScheduleEntry(id="t", name="T", schedule_type="cron",
                              schedule_value="0 9 * * *")
        nxt = compute_next_trigger(entry)
        assert nxt is not None
        assert datetime.fromisoformat(nxt) > datetime.now(timezone.utc)

    def test_once_future_trigger(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        entry = ScheduleEntry(id="t", name="T", schedule_type="once", schedule_value=future)
        assert compute_next_trigger(entry) is not None

    def test_once_past_trigger(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        entry = ScheduleEntry(id="t", name="T", schedule_type="once", schedule_value=past)
        assert compute_next_trigger(entry) is None

    def test_invalid_cron(self):
        entry = ScheduleEntry(id="t", name="T", schedule_type="cron",
                              schedule_value="invalid cron")
        assert compute_next_trigger(entry) is None


class TestDueChecking:
    def test_due_detected(self, store):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        entry = store.create(name="Due", schedule_type="once", schedule_value=past,
                             message_type="static", message_content="Hi")
        entry.next_trigger_at = past
        entry.enabled = True
        due = store.get_due_reminders()
        assert len(due) == 1 and due[0].id == entry.id

    def test_disabled_not_due(self, store):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        store.create(name="Disabled", schedule_type="once", schedule_value=past,
                     message_type="static", message_content="Hi", enabled=False)
        assert store.get_due_reminders() == []

    def test_cooldown_skips_recent(self, store):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        entry = store.create(name="Cooldown", schedule_type="cron",
                             schedule_value="* * * * *", message_type="static",
                             message_content="Hi")
        entry.next_trigger_at = past
        entry.last_triggered_at = datetime.now(timezone.utc).isoformat()  # just fired
        assert store.get_due_reminders() == []


class TestAutoDisable:
    def test_once_disables_after_trigger(self, store):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        entry = store.create(name="One-time", schedule_type="once", schedule_value=future,
                             message_type="static", message_content="Hi")
        assert entry.enabled is True
        store.mark_triggered(entry.id)
        updated = store.get(entry.id)
        assert updated.enabled is False
        assert updated.next_trigger_at is None
        assert updated.last_triggered_at is not None

    def test_cron_stays_enabled_after_trigger(self, store):
        entry = store.create(name="Recurring", schedule_type="cron",
                             schedule_value="0 9 * * *", message_type="static",
                             message_content="Hi")
        store.mark_triggered(entry.id)
        updated = store.get(entry.id)
        assert updated.enabled is True
        assert updated.next_trigger_at is not None
        assert updated.last_triggered_at is not None


class TestDashboard:
    def test_dashboard_has_three_sections(self, store):
        store.create(name="Recurring one", schedule_type="cron", schedule_value="0 9 * * *",
                     message_type="static", message_content="x")
        store.create(name="Upcoming one", schedule_type="once",
                     schedule_value=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                     message_type="static", message_content="y")
        dashboard = (store.scheduler_dir / "Dashboard.md").read_text(encoding="utf-8")
        assert "## Recurring" in dashboard
        assert "## Upcoming" in dashboard
        assert "## Recently Fired" in dashboard
        assert "Recurring one" in dashboard
        assert "Upcoming one" in dashboard

    def test_fired_entry_appears_in_recently_fired(self, store):
        entry = store.create(name="Fires", schedule_type="cron", schedule_value="0 9 * * *",
                             message_type="static", message_content="x")
        store.mark_triggered(entry.id)
        dashboard = (store.scheduler_dir / "Dashboard.md").read_text(encoding="utf-8")
        # The fired schedule shows up under Recently Fired with its name.
        rf = dashboard.split("## Recently Fired", 1)[1]
        assert "Fires" in rf

    def test_dashboard_not_parsed_as_schedule_source(self, store):
        store.create(name="X", schedule_type="cron", schedule_value="0 9 * * *",
                     message_type="static", message_content="x")
        before = len(store.list_all())
        store.reindex_file(str(store.scheduler_dir / "Dashboard.md"))
        assert len(store.list_all()) == before


class TestSuppression:
    def test_sentinels_suppressed(self):
        for s in ("NO_MEETING", "NO_MEETINGS", "NOTHING_TO_REPORT", "NO_ACTION"):
            assert SchedulerScheduler._should_suppress(s) is True
            assert SchedulerScheduler._should_suppress(s.lower()) is True
            assert SchedulerScheduler._should_suppress(f"  {s}  ") is True

    def test_normal_not_suppressed(self):
        assert SchedulerScheduler._should_suppress("Here is your briefing") is False
        assert SchedulerScheduler._should_suppress("") is False
        assert SchedulerScheduler._should_suppress("NO_MEETING but here's news") is False

    def test_sentinel_with_punctuation(self):
        assert SchedulerScheduler._should_suppress("NO_MEETING.") is True
        assert SchedulerScheduler._should_suppress("NO_MEETING—nothing scheduled") is True


class TestPromptExecution:
    @pytest.fixture
    def scheduler(self, store):
        return SchedulerScheduler(store)

    @pytest.fixture
    def prompt_entry(self, scheduler):
        return scheduler.store.create(
            name="Test Prompt", schedule_type="cron", schedule_value="0 9 * * *",
            message_type="prompt", message_content="What meetings do I have today?",
        )

    @pytest.mark.asyncio
    async def test_successful_prompt(self, scheduler, prompt_entry):
        mock_result = {
            "answer": "You have 3 meetings today.", "tool_statuses": ["Searching calendar..."],
            "cost_usd": 0.015, "model": "claude", "input_tokens": 500, "output_tokens": 100,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                   new_callable=AsyncMock, return_value=mock_result) as mock_chat:
            answer, exec_log = await scheduler._execute_prompt_reminder(prompt_entry)
        assert answer == "You have 3 meetings today."
        assert exec_log["tool_calls"] == 1
        assert exec_log["attempt"] == 1
        mock_chat.assert_called_once_with("What meetings do I have today?")

    @pytest.mark.asyncio
    async def test_retry_on_empty(self, scheduler, prompt_entry):
        results = [
            {"answer": "", "tool_statuses": [], "cost_usd": 0, "model": "", "input_tokens": 0, "output_tokens": 0},
            {"answer": "Retry worked!", "tool_statuses": ["Searching..."], "cost_usd": 0.01,
             "model": "claude", "input_tokens": 100, "output_tokens": 50},
        ]
        with patch("api.services.telegram.chat_via_api_with_log",
                   new_callable=AsyncMock, side_effect=results):
            answer, exec_log = await scheduler._execute_prompt_reminder(prompt_entry)
        assert answer == "Retry worked!"
        assert exec_log["attempt"] == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self, scheduler, prompt_entry):
        with patch("api.services.telegram.chat_via_api_with_log",
                   new_callable=AsyncMock, side_effect=Exception("Persistent error")):
            answer, exec_log = await scheduler._execute_prompt_reminder(prompt_entry)
        assert "failed after 2 attempts" in answer
        assert exec_log["error"] == "Persistent error"

    @pytest.mark.asyncio
    async def test_fire_sends_telegram(self, scheduler, prompt_entry):
        mock_result = {
            "answer": "Your morning briefing...", "tool_statuses": [], "cost_usd": 0.01,
            "model": "claude", "input_tokens": 100, "output_tokens": 50,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                   new_callable=AsyncMock, return_value=mock_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_reminder(prompt_entry)
        mock_send.assert_called_once()
        sent = mock_send.call_args[0][0]
        assert "Test Prompt" in sent and "Your morning briefing..." in sent

    @pytest.mark.asyncio
    async def test_fire_suppresses_no_meeting(self, scheduler, prompt_entry):
        mock_result = {
            "answer": "NO_MEETING", "tool_statuses": [], "cost_usd": 0.005,
            "model": "claude", "input_tokens": 50, "output_tokens": 5,
        }
        with patch("api.services.telegram.chat_via_api_with_log",
                   new_callable=AsyncMock, return_value=mock_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock) as mock_send:
                await scheduler._fire_reminder(prompt_entry)
        mock_send.assert_not_called()


class TestFormatHelpers:
    def test_format_cron_human(self):
        assert "ET" in _format_cron_human("0 9 * * *", "America/New_York")

    def test_format_dt_short_handles_garbage(self):
        assert _format_dt_short("not-a-date") == "not-a-date"


class TestActionDispatch:
    """#245 — _fire_entry dispatches on action, records run history, hands off agents."""

    @pytest.fixture
    def scheduler(self, store):
        return SchedulerScheduler(store)

    @pytest.mark.asyncio
    async def test_notify_sends_static_message(self, scheduler):
        entry = scheduler.store.create(
            name="Water plants", schedule_type="cron", schedule_value="0 18 * * *",
            action="notify", message_type="static", message_content="hydrate the ferns",
        )
        with patch("api.services.telegram.send_message_async",
                   new_callable=AsyncMock, return_value=True) as mock_send:
            await scheduler._fire_entry(entry)
        mock_send.assert_called_once()
        assert "hydrate the ferns" in mock_send.call_args[0][0]
        assert scheduler.store.get(entry.id).last_status == "sent"

    @pytest.mark.asyncio
    async def test_notify_suppressed_when_empty(self, scheduler):
        entry = scheduler.store.create(
            name="Empty", schedule_type="cron", schedule_value="0 9 * * *",
            action="notify", message_type="static", message_content="",
        )
        with patch("api.services.telegram.send_message_async",
                   new_callable=AsyncMock) as mock_send:
            await scheduler._fire_entry(entry)
        mock_send.assert_not_called()
        assert scheduler.store.get(entry.id).last_status == "suppressed"

    @pytest.mark.asyncio
    async def test_prompt_suppressed_on_sentinel(self, scheduler):
        entry = scheduler.store.create(
            name="Meeting prep", schedule_type="cron", schedule_value="0 9 * * *",
            action="prompt", message_type="prompt", message_content="check calendar",
        )
        mock_result = {"answer": "NO_MEETING", "tool_statuses": [], "cost_usd": 0,
                       "model": "claude", "input_tokens": 1, "output_tokens": 1}
        with patch("api.services.telegram.chat_via_api_with_log",
                   new_callable=AsyncMock, return_value=mock_result):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock) as mock_send:
                await scheduler._fire_entry(entry)
        mock_send.assert_not_called()
        assert scheduler.store.get(entry.id).last_status == "suppressed"

    @pytest.mark.asyncio
    async def test_agent_action_creates_tagged_task(self, scheduler):
        entry = scheduler.store.create(
            name="Weekly review", schedule_type="cron", schedule_value="0 9 * * 6",
            action="agent", executor="cloud", message_content="Draft my weekly review",
        )
        fake_task = MagicMock(id="task42")
        fake_tm = MagicMock()
        fake_tm.create.return_value = fake_task
        with patch("api.services.task_manager.get_task_manager", return_value=fake_tm):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock) as mock_send:
                await scheduler._fire_entry(entry)
        # The agent worker discovers #agent tasks; the executor tag rides along.
        fake_tm.create.assert_called_once()
        kwargs = fake_tm.create.call_args.kwargs
        assert kwargs["description"] == "Draft my weekly review"
        assert kwargs["tags"] == ["agent", "cloud"]
        # No Telegram for an agent hand-off; the worker reports through its channel.
        mock_send.assert_not_called()
        refreshed = scheduler.store.get(entry.id)
        assert refreshed.last_status == "handed-off"
        assert "task42" in refreshed.last_result

    @pytest.mark.asyncio
    async def test_agent_action_local_executor_tag(self, scheduler):
        entry = scheduler.store.create(
            name="Local job", schedule_type="cron", schedule_value="0 9 * * *",
            action="agent", executor="local", message_content="summarize inbox",
        )
        fake_tm = MagicMock()
        fake_tm.create.return_value = MagicMock(id="t1")
        with patch("api.services.task_manager.get_task_manager", return_value=fake_tm):
            await scheduler._fire_entry(entry)
        assert fake_tm.create.call_args.kwargs["tags"] == ["agent", "local"]

    @pytest.mark.asyncio
    async def test_run_history_surfaced_in_dashboard(self, scheduler):
        entry = scheduler.store.create(
            name="Briefing", schedule_type="cron", schedule_value="0 9 * * *",
            action="notify", message_type="static", message_content="Good morning",
        )
        with patch("api.services.telegram.send_message_async",
                   new_callable=AsyncMock, return_value=True):
            await scheduler._fire_entry(entry)
        dashboard = (scheduler.store.scheduler_dir / "Dashboard.md").read_text(encoding="utf-8")
        rf = dashboard.split("## Recently Fired", 1)[1]
        assert "Briefing" in rf
        assert "sent" in rf  # outcome column

    @pytest.mark.asyncio
    async def test_failed_fire_records_failure(self, scheduler):
        entry = scheduler.store.create(
            name="Breaks", schedule_type="cron", schedule_value="0 9 * * *",
            action="prompt", message_type="prompt", message_content="x",
        )
        with patch.object(scheduler, "_generate_message",
                          new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_entry(entry)
        assert scheduler.store.get(entry.id).last_status == "failed"
        assert "(failed)" in mock_send.call_args[0][0]

    def test_fire_reminder_alias_exists(self, scheduler):
        # Back-compat: the HTTP trigger route still calls _fire_reminder.
        assert scheduler._fire_reminder == scheduler._fire_entry


class TestMissedFire:
    """Run-once catch-up: a missed trigger fires once, then advances."""

    def test_missed_cron_is_due_once_then_advances(self, store):
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        entry = store.create(name="Missed", schedule_type="cron", schedule_value="0 9 * * *",
                             action="notify", message_type="static", message_content="x")
        entry.next_trigger_at = past  # simulate a window missed while down
        # First tick after startup: due exactly once.
        due = store.get_due_reminders()
        assert [e.id for e in due] == [entry.id]
        # Firing advances the trigger into the future and stamps last_triggered.
        store.mark_triggered(entry.id)
        refreshed = store.get(entry.id)
        assert refreshed.next_trigger_at is not None
        assert datetime.fromisoformat(refreshed.next_trigger_at) > datetime.now(timezone.utc)
        # No duplicate catch-up: not due again (advanced + within cooldown).
        assert store.get_due_reminders() == []
