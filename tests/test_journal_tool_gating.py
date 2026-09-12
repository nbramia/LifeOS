"""Journal is a filing surface, not an orchestrator: on a journal-persona
turn, the native agentic loop advertises a narrowed tool catalog
(`tools_for_persona`) and enforces the same boundary again at execution time
(`execute_tool_parallel`'s journal gate), so a model that names an excluded
tool anyway is refused rather than served.

Also covers the tag/field-attestation filter on `manage_tasks` create (only
what the operator actually typed in their own message survives) and the
`bot`/`timezone` fields `manage_schedules` create accepts and persists.
"""
from __future__ import annotations

import pytest

from api.services.agent_tools import (
    JOURNAL_EXCLUDED_TOOLS,
    TOOL_DEFINITIONS,
    execute_tool_parallel,
    tools_for_persona,
)
from api.services.memory_store import MemoryStore
from api.services.scheduler_store import SchedulerStore
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


class TestToolsForPersona:
    def test_non_journal_persona_gets_the_full_catalog(self):
        assert tools_for_persona("primary") is TOOL_DEFINITIONS
        assert tools_for_persona("") is TOOL_DEFINITIONS
        assert tools_for_persona("fitness") is TOOL_DEFINITIONS

    def test_journal_persona_excludes_the_orchestration_tools(self):
        names = {t["name"] for t in tools_for_persona("journal")}
        assert names & JOURNAL_EXCLUDED_TOOLS == set()
        # Read/search and filing tools remain.
        assert "search_vault" in names
        assert "manage_tasks" in names
        assert "manage_schedules" in names

    def test_journal_persona_excludes_manage_reminders(self):
        # manage_reminders is a deprecated alias of manage_schedules that
        # writes into the same scheduler store without ever setting `bot` —
        # journal only gets manage_schedules for this job.
        names = {t["name"] for t in tools_for_persona("journal")}
        assert "manage_reminders" not in names
        assert "manage_reminders" in {t["name"] for t in tools_for_persona("primary")}

    def test_journal_list_keeps_exactly_one_cache_breakpoint(self):
        journal_tools = tools_for_persona("journal")
        with_marker = [t for t in journal_tools if "cache_control" in t]
        assert len(with_marker) == 1
        assert with_marker[0] is journal_tools[-1]

    def test_building_the_journal_list_does_not_mutate_the_shared_catalog(self):
        before = [dict(t) for t in TOOL_DEFINITIONS]
        tools_for_persona("journal")
        assert TOOL_DEFINITIONS == before


@pytest.fixture
def tm(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    index = tmp_path / "task_index.json"
    manager = TaskManager(vault_path=vault, index_path=index)
    monkeypatch.setattr("api.services.agent_tools.get_task_manager", lambda: manager, raising=False)
    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)
    return manager


@pytest.fixture
def sched(tmp_path, monkeypatch):
    store = SchedulerStore(vault_path=tmp_path / "vault", index_path=tmp_path / "sched_index.json")
    monkeypatch.setattr("api.services.agent_tools.get_scheduler_store", lambda: store, raising=False)
    import api.services.scheduler_store as sched_mod
    monkeypatch.setattr(sched_mod, "get_scheduler_store", lambda: store)
    return store


@pytest.fixture
def mem(tmp_path, monkeypatch):
    store = MemoryStore(file_path=str(tmp_path / "memories.json"))
    monkeypatch.setattr("api.services.memory_store.get_memory_store", lambda: store)
    return store


class TestExecuteToolParallelJournalGate:
    async def test_non_journal_turn_is_never_gated(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks", {"action": "create", "description": "x", "tags": ["invented"]},
        )
        assert out.startswith("Task created")
        assert tm.list_tasks()[0].tags == ["invented"]

    async def test_excluded_tool_is_refused_on_a_journal_turn(self, mem):
        # `mem` isolates the store so a gate regression can never reach the
        # operator's real ~/.lifeos/memories.json or fire a live LLM call —
        # the gate itself is what this test asserts, not a safe fallback.
        out = await execute_tool_parallel(
            "save_memory", {"content": "x"}, persona_id="journal", user_message="x",
        )
        assert out.startswith("Error:")
        assert "journal persona" in out
        assert mem.list_memories() == []

    async def test_manage_reminders_is_refused_on_a_journal_turn(self, sched, monkeypatch):
        # _reminder_create imports get_reminder_store from the shim at call
        # time, not from scheduler_store's patched attribute — patch the
        # shim directly so a gate regression can't be masked by writing to
        # an already-isolated-but-different store.
        monkeypatch.setattr(
            "api.services.reminder_store.get_reminder_store", lambda: sched,
        )
        out = await execute_tool_parallel(
            "manage_reminders",
            {
                "action": "create", "name": "call mom", "schedule_type": "once",
                "schedule_value": "2030-01-02T15:00:00", "message_content": "call mom",
            },
            persona_id="journal", user_message="remind me to call mom tomorrow at 3",
        )
        assert out.startswith("Error:")
        assert "journal persona" in out
        assert sched.list_all() == []

    async def test_excluded_tool_is_not_refused_off_a_journal_turn(self, monkeypatch):
        # save_memory's own handler is irrelevant here -- just confirm the
        # journal gate doesn't fire without persona_id="journal".
        called = {}
        async def fake_save_memory(inp):
            called["ran"] = True
            return "Memory saved."
        monkeypatch.setitem(
            __import__("api.services.agent_tools", fromlist=["_TOOL_HANDLERS"])._TOOL_HANDLERS,
            "save_memory", fake_save_memory,
        )
        out = await execute_tool_parallel("save_memory", {"content": "x"})
        assert out == "Memory saved."
        assert called.get("ran")

    async def test_manage_tasks_complete_is_refused_on_a_journal_turn(self, tm):
        task = tm.create("Existing task")
        out = await execute_tool_parallel(
            "manage_tasks", {"action": "complete", "task_id": task.id},
            persona_id="journal", user_message="mark it done",
        )
        assert out.startswith("Error:")
        assert tm.get(task.id).status != "done"

    async def test_manage_tasks_update_is_refused_on_a_journal_turn(self, tm):
        task = tm.create("Existing task")
        out = await execute_tool_parallel(
            "manage_tasks", {"action": "update", "task_id": task.id, "description": "changed"},
            persona_id="journal", user_message="change it",
        )
        assert out.startswith("Error:")
        assert tm.get(task.id).description == "Existing task"

    async def test_manage_tasks_create_keeps_only_attested_tags(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {"action": "create", "description": "call mom", "tags": ["claude", "invented"]},
            persona_id="journal",
            user_message="create a calendar event to call mom tomorrow at 3 #claude",
        )
        assert out.startswith("Task created")
        assert tm.list_tasks()[0].tags == ["claude"]

    async def test_manage_tasks_create_strips_all_tags_when_none_attested(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {"action": "create", "description": "call mom", "tags": ["agent"]},
            persona_id="journal",
            user_message="create a calendar event to call mom tomorrow at 3",
        )
        assert out.startswith("Task created")
        assert tm.list_tasks()[0].tags == []

    async def test_manage_tasks_create_strips_unattested_fields(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {"action": "create", "description": "call mom", "fields": {"model": "opus"}},
            persona_id="journal",
            user_message="create a calendar event to call mom tomorrow at 3",
        )
        assert out.startswith("Task created")
        created = tm.get(tm.list_tasks()[0].id)
        assert created.fields == {}

    async def test_manage_tasks_create_strips_a_tag_that_is_only_a_substring_of_a_message_tag(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {"action": "create", "description": "notes", "tags": ["agent", "claude"]},
            persona_id="journal",
            user_message="thinking about the #agentic loop and #claude-code docs",
        )
        assert out.startswith("Task created")
        assert tm.list_tasks()[0].tags == []

    async def test_manage_tasks_create_strips_fields_that_are_bare_words_in_the_message(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {
                "action": "create", "description": "call the vendor",
                "fields": {"executor": "local", "model": "opus"},
            },
            persona_id="journal",
            user_message="call the local vendor about the opus recording",
        )
        assert out.startswith("Task created")
        created = tm.get(tm.list_tasks()[0].id)
        assert created.fields == {}

    async def test_manage_tasks_create_keeps_a_genuinely_written_tag(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {"action": "create", "description": "notes", "tags": ["claude"]},
            persona_id="journal",
            user_message="thinking about the #claude docs",
        )
        assert out.startswith("Task created")
        assert tm.list_tasks()[0].tags == ["claude"]

    async def test_manage_tasks_create_keeps_a_field_value_attested_in_the_message(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {"action": "create", "description": "call mom", "fields": {"key": "callmom"}},
            persona_id="journal",
            user_message="create a calendar event to call mom tomorrow at 3 key:callmom",
        )
        assert out.startswith("Task created")
        created = tm.get(tm.list_tasks()[0].id)
        assert created.fields == {"key": "callmom"}

    async def test_manage_tasks_create_keeps_a_field_value_attested_by_a_quoted_span(self, tm):
        out = await execute_tool_parallel(
            "manage_tasks",
            {"action": "create", "description": "call the vendor", "fields": {"executor": "local"}},
            persona_id="journal",
            user_message='use the "local" executor for this',
        )
        assert out.startswith("Task created")
        created = tm.get(tm.list_tasks()[0].id)
        assert created.fields == {"executor": "local"}

    async def test_manage_schedules_notify_create_is_allowed_on_a_journal_turn(self, sched):
        out = await execute_tool_parallel(
            "manage_schedules",
            {
                "action": "create", "name": "call mom", "schedule_type": "once",
                "schedule_value": "2030-01-02T15:00:00", "schedule_action": "notify",
                "message_content": "call mom",
            },
            persona_id="journal", user_message="remind me to call mom tomorrow at 3",
        )
        assert out.startswith("Schedule created")
        assert sched.list_all()[0].action == "notify"

    @pytest.mark.parametrize("bad_action", ["prompt", "endpoint", "agent"])
    async def test_manage_schedules_non_notify_create_is_refused_on_a_journal_turn(self, sched, bad_action):
        out = await execute_tool_parallel(
            "manage_schedules",
            {
                "action": "create", "name": "x", "schedule_type": "once",
                "schedule_value": "2030-01-02T15:00:00", "schedule_action": bad_action,
                "message_content": "x",
            },
            persona_id="journal", user_message="x",
        )
        assert out.startswith("Error:")
        assert sched.list_all() == []

    async def test_manage_schedules_create_persists_bot_and_timezone(self, sched):
        out = await execute_tool_parallel(
            "manage_schedules",
            {
                "action": "create", "name": "call mom", "schedule_type": "once",
                "schedule_value": "2030-01-02T15:00:00", "schedule_action": "notify",
                "message_content": "call mom", "bot": "journal", "timezone": "America/New_York",
            },
            persona_id="journal", user_message="remind me to call mom tomorrow at 3",
        )
        assert out.startswith("Schedule created")
        entry = sched.list_all()[0]
        assert entry.bot == "journal"
        assert entry.timezone == "America/New_York"

    async def test_manage_schedules_create_defaults_bot_to_journal_when_configured(self, sched, monkeypatch):
        # Simulate an install with TELEGRAM_JOURNAL_BOT_TOKEN configured, the
        # way test_vault_write_route.py's _enable_journal_persona does: the
        # registry loader merges dotenv values with os.environ, and
        # monkeypatch.setenv always wins that merge.
        monkeypatch.setenv("TELEGRAM_JOURNAL_BOT_TOKEN", "test-token")
        out = await execute_tool_parallel(
            "manage_schedules",
            {
                "action": "create", "name": "call mom", "schedule_type": "once",
                "schedule_value": "2030-01-02T15:00:00", "schedule_action": "notify",
                "message_content": "call mom",
            },
            persona_id="journal", user_message="remind me to call mom tomorrow at 3",
        )
        assert out.startswith("Schedule created")
        assert sched.list_all()[0].bot == "journal"

    async def test_manage_schedules_create_leaves_bot_empty_when_journal_not_configured(self, sched, tmp_path, monkeypatch):
        # Force the registry to resolve no bots at all, regardless of what
        # this machine's real config/telegram_bots.json or .env carries.
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", tmp_path / "no-registry.json")
        out = await execute_tool_parallel(
            "manage_schedules",
            {
                "action": "create", "name": "call mom", "schedule_type": "once",
                "schedule_value": "2030-01-02T15:00:00", "schedule_action": "notify",
                "message_content": "call mom",
            },
            persona_id="journal", user_message="remind me to call mom tomorrow at 3",
        )
        assert out.startswith("Schedule created")
        assert sched.list_all()[0].bot == ""

    async def test_manage_schedules_delete_is_refused_on_a_journal_turn(self, sched):
        entry = sched.create(
            action="notify", name="existing", schedule_type="once",
            schedule_value="2030-01-02T15:00:00", message_content="x",
        )
        out = await execute_tool_parallel(
            "manage_schedules", {"action": "delete", "schedule_id": entry.id},
            persona_id="journal", user_message="actually, cancel that 3pm reminder",
        )
        assert out.startswith("Error:")
        assert sched.get(entry.id) is not None

    async def test_manage_schedules_update_is_refused_on_a_journal_turn(self, sched):
        entry = sched.create(
            action="agent", name="existing", schedule_type="once",
            schedule_value="2030-01-02T15:00:00", message_content="do work",
            executor="claude_code",
        )
        out = await execute_tool_parallel(
            "manage_schedules",
            {"action": "update", "schedule_id": entry.id, "message_content": "changed", "executor": "codex"},
            persona_id="journal", user_message="change that schedule",
        )
        assert out.startswith("Error:")
        unchanged = sched.get(entry.id)
        assert unchanged.message_content == "do work"
        assert unchanged.executor == "claude_code"
