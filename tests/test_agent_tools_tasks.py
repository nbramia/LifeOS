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


class TestUnknownAction:
    def test_unknown_action(self, tm):
        out = _tool_manage_tasks({"action": "nope"})
        assert out.startswith("Error:")
