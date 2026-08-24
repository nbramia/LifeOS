import asyncio
import json

from api.services import inbox_store
from api.services.agent_tools import (
    _tool_confirm_inbox_proposal,
    _tool_list_inbox_proposals,
    _tool_process_inbox_item,
)


def test_update_item_retains_pending_proposal(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(path))

    item = inbox_store.add_item("Remind me to call John next week", source={"type": "telegram"})
    updated = inbox_store.update_item(
        item["id"],
        status="processed",
        category="reminder",
        proposal={
            "type": "reminder",
            "content": "Remind me to call John next week",
            "requires_confirmation": True,
        },
    )

    assert updated["status"] == "processed"
    assert updated["proposal"]["requires_confirmation"] is True
    persisted = json.loads(path.read_text())
    assert persisted["items"][0]["source"] == {"type": "telegram"}
    assert persisted["items"][0]["proposal"]["type"] == "reminder"


def test_list_inbox_proposals_returns_confirmation_work(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(path))
    item = inbox_store.add_item("Remind me to call John next week")
    inbox_store.update_item(
        item["id"],
        status="processed",
        category="reminder",
        proposal={"type": "reminder", "content": "Call John next week", "requires_confirmation": True},
    )

    result = _tool_list_inbox_proposals({})

    assert item["id"] in result
    assert "Call John next week" in result


def test_confirm_task_proposal_creates_native_task_once(tmp_path, monkeypatch):
    inbox_path = tmp_path / "inbox.json"
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(inbox_path))
    from api.services.task_manager import TaskManager
    import api.services.task_manager as task_manager_module
    manager = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "tasks.json")
    monkeypatch.setattr(task_manager_module, "get_task_manager", lambda: manager)

    item = inbox_store.add_item("Send John the deck")
    inbox_store.update_item(
        item["id"],
        status="processed",
        category="task",
        proposal={"type": "task", "content": "Send John the deck", "requires_confirmation": True},
    )

    first = _tool_confirm_inbox_proposal({"proposal_id": item["id"], "priority": "high"})
    second = _tool_confirm_inbox_proposal({"proposal_id": item["id"]})

    assert first.startswith("Task created:")
    assert "already confirmed" in second
    assert len(manager.list_tasks()) == 1


def test_relationship_capture_writes_unconfirmed_person_fact(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(tmp_path / "inbox.json"))
    from api.services.memory_store import MemoryStore
    import api.services.memory_store as memory_store_module
    import api.routes.memories as memories_route
    memory_store = MemoryStore(file_path=tmp_path / "memories.json")
    monkeypatch.setattr(memory_store_module, "get_memory_store", lambda: memory_store)

    async def identity(text):
        return text
    monkeypatch.setattr(memories_route, "synthesize_memory", identity)

    from api.services.person_facts import PersonFactStore
    import api.services.person_facts as person_facts_module
    facts = PersonFactStore(db_path=str(tmp_path / "crm.db"))
    monkeypatch.setattr(person_facts_module, "get_person_fact_store", lambda: facts)

    item = inbox_store.add_item(
        "John is moving to Berlin",
        source={"type": "telegram", "chat_id": "1", "message_id": 99},
    )
    result = asyncio.run(_tool_process_inbox_item({
        "item_id": item["id"],
        "category": "relationship",
        "person_id": "person-123",
    }))

    assert "relationship" in result
    saved = facts.get_for_person("person-123")
    assert len(saved) == 1
    assert saved[0].value == "John is moving to Berlin"
    assert saved[0].confirmed_by_user is False
    assert saved[0].source_quote == "John is moving to Berlin"
    assert saved[0].source_link == "telegram://1/99"
