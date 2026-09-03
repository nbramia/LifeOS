"""
Tests for the manage_human_queue native chat tool (#852) — the surface the
/chat orchestrator uses when asked "what's waiting on me".
"""
import pytest

from api.services.agent_tools import _tool_manage_human_queue, TOOL_DEFINITIONS
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def tm(tmp_path, monkeypatch):
    manager = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json")
    from api.services import human_queue
    monkeypatch.setattr(human_queue, "get_task_manager", lambda: manager)
    return manager


class TestToolRegistration:
    def test_registered_with_three_actions(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "manage_human_queue")
        assert set(tool["input_schema"]["properties"]["action"]["enum"]) == {"add", "list", "resolve"}


class TestAdd:
    def test_add_files_card(self, tm):
        out = _tool_manage_human_queue({"action": "add", "title": "Re-auth example service"})
        assert "Human-queue card filed" in out
        cards = tm.list_tasks(tag="human", status="blocked")
        assert len(cards) == 1
        assert cards[0].description == "Re-auth example service"

    def test_add_requires_title(self, tm):
        out = _tool_manage_human_queue({"action": "add"})
        assert out.startswith("Error:") and "title" in out

    def test_add_invalid_done_when_returns_error(self, tm):
        out = _tool_manage_human_queue({
            "action": "add", "title": "X", "done_when": {"type": "shell"},
        })
        assert out.startswith("Error:")
        assert tm.list_tasks(tag="human") == []


class TestList:
    def test_list_empty(self, tm):
        assert _tool_manage_human_queue({"action": "list"}) == "No open Human-queue cards."

    def test_list_shows_open_cards(self, tm):
        _tool_manage_human_queue({"action": "add", "title": "Card one"})
        out = _tool_manage_human_queue({"action": "list"})
        assert "Card one" in out


class TestResolve:
    def test_resolve_requires_id_or_key(self, tm):
        out = _tool_manage_human_queue({"action": "resolve"})
        assert out.startswith("Error:") and "id_or_key" in out

    def test_resolve_marks_card_done(self, tm):
        _tool_manage_human_queue({"action": "add", "title": "X", "key": "k"})
        out = _tool_manage_human_queue({"action": "resolve", "id_or_key": "k", "note": "fixed"})
        assert "Human-queue card resolved" in out
        assert tm.list_tasks(tag="human", status="blocked") == []

    def test_resolve_unknown_returns_error(self, tm):
        out = _tool_manage_human_queue({"action": "resolve", "id_or_key": "nope"})
        assert out.startswith("Error:")
