"""
Tests for the manage_tasks agent tool dispatcher.

Covers the new 'update' and 'tags' actions and verifies dispatching for
the existing actions still works.
"""
import pytest

from api.services.agent_tools import _tool_manage_tasks
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def tm(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    index = tmp_path / "task_index.json"
    manager = TaskManager(vault_path=vault, index_path=index)
    monkeypatch.setattr(
        "api.services.agent_tools.get_task_manager",
        lambda: manager,
        raising=False,
    )
    # Patch the lazy import target too
    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)
    return manager


class TestManageTasksUpdate:
    def test_update_tags_replaces_list(self, tm):
        task = tm.create("Plan launch", tags=["work"])
        out = _tool_manage_tasks({"action": "update", "task_id": task.id, "tags": ["work", "AI"]})
        assert "Task updated" in out
        assert tm.get(task.id).tags == ["work", "AI"]

    def test_update_requires_task_id(self, tm):
        out = _tool_manage_tasks({"action": "update", "tags": ["x"]})
        assert out.startswith("Error:") and "task_id" in out

    def test_update_unknown_task(self, tm):
        out = _tool_manage_tasks({"action": "update", "task_id": "deadbeef", "tags": ["x"]})
        assert out.startswith("Error:") and "not found" in out

    def test_update_requires_at_least_one_field(self, tm):
        task = tm.create("Plan launch")
        out = _tool_manage_tasks({"action": "update", "task_id": task.id})
        assert out.startswith("Error:")

    def test_update_changes_status_and_priority(self, tm):
        task = tm.create("Ship feature")
        _tool_manage_tasks({
            "action": "update",
            "task_id": task.id,
            "status": "in_progress",
            "priority": "high",
        })
        refreshed = tm.get(task.id)
        assert refreshed.status == "in_progress"
        assert refreshed.priority == "high"


class TestManageTasksTags:
    def test_tags_empty(self, tm):
        assert _tool_manage_tasks({"action": "tags"}) == "No tags defined yet."

    def test_tags_lists_with_counts(self, tm):
        tm.create("a", tags=["work", "urgent"])
        tm.create("b", tags=["work"])
        out = _tool_manage_tasks({"action": "tags"})
        # work appears twice, urgent once; sorted by count desc
        lines = out.splitlines()
        assert lines[0] == "#work (2)"
        assert "#urgent (1)" in lines


class TestManageTasksCreate:
    def test_create_lands_in_inbox_even_if_context_provided(self, tm):
        # The chat assistant might still try to pass a context — we ignore it.
        _tool_manage_tasks({
            "action": "create",
            "description": "Build a financial model",
            "context": "Work",
        })
        tasks = tm.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].context == "Inbox"

    def test_create_forwards_status_notes_fields(self, tm):
        """#853: status/notes/fields are threaded through the chat tool too."""
        _tool_manage_tasks({
            "action": "create",
            "description": "Waiting on legal",
            "status": "blocked",
            "notes": "chased on Monday",
            "fields": {"host": "laptop"},
        })
        tasks = tm.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == "blocked"
        assert tasks[0].notes == "chased on Monday"
        assert tasks[0].fields == {"host": "laptop"}


class TestManageTasksUpdateNotesAndFields:
    def test_update_notes(self, tm):
        task = tm.create("Plan launch")
        _tool_manage_tasks({"action": "update", "task_id": task.id, "notes": "draft outline"})
        assert tm.get(task.id).notes == "draft outline"

    def test_update_fields_merges_and_removes(self, tm):
        task = tm.create("Plan launch", fields={"host": "laptop"})
        _tool_manage_tasks({
            "action": "update", "task_id": task.id,
            "fields": {"host": None, "effort": "high"},
        })
        assert tm.get(task.id).fields == {"effort": "high"}


class TestUnknownAction:
    def test_unknown_action(self, tm):
        out = _tool_manage_tasks({"action": "nope"})
        assert out.startswith("Error:")


class TestManageTasksFilterDocs:
    """The `manage_tasks` schema is what the orchestrator reads before calling the
    tool, and a bad filter hint fails silently — the call succeeds and returns the
    wrong slice. Two regressions cost real accuracy in model benchmarking:

    - `context` offered 'Work'/'Personal' as filter examples. Contexts are
      vault-defined and most tasks are in 'Inbox', so a model that trusted the
      example filtered to zero rows.
    - `status` did not say what omitting it does. Unfiltered `list` returns every
      status uncapped; in an established vault that is mostly done/cancelled, and
      summarising it yields a partial open-task list stated as complete.
    """

    @pytest.fixture
    def props(self):
        from api.services.agent_tools import TOOL_DEFINITIONS
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "manage_tasks")
        return tool["input_schema"]["properties"]

    def test_context_does_not_advertise_values_no_vault_need_have(self, props):
        desc = props["context"]["description"]
        assert "'Work'" not in desc and "'Personal'" not in desc
        assert "Inbox" in desc

    def test_context_warns_unknown_filter_matches_nothing(self, props):
        desc = props["context"]["description"].lower()
        assert "omit" in desc
        assert "returns nothing" in desc or "not in use" in desc

    def test_status_enumerates_values_and_warns_about_omitting(self, props):
        desc = props["status"]["description"]
        for value in ("todo", "done", "in_progress", "cancelled",
                      "deferred", "blocked", "urgent"):
            assert value in desc
        lowered = desc.lower()
        assert "every status" in lowered
        assert "cancelled" in lowered and "todo" in lowered

    def test_tags_rule_forbids_uninvited_routing_tags(self, props):
        """#804: an assistant that invents a routing tag (#local, #claude, ...)
        on a task the operator didn't ask to be routed launders its own engine
        preference into the highest-precedence slot — routing tags outrank
        every other routing safeguard. The tags field must say so."""
        desc = props["tags"]["description"]
        assert "operator named" in desc
        assert "operator-authority and outrank every routing safeguard" in desc
        assert "codex" in desc and "cloud" in desc
