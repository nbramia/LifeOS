import asyncio
import json

from api.services import inbox_store
from api.services.agent_tools import (
    _tool_confirm_inbox_proposal,
    _tool_list_inbox_proposals,
    _tool_process_inbox_item,
)


def test_auto_inbox_classifier_is_conservative():
    from api.services.agent_tools import _auto_inbox_category

    assert _auto_inbox_category("yes") == "dismissed"
    assert _auto_inbox_category("Remind me to call John next week") == "reminder"
    assert _auto_inbox_category("I want to build an AI product for cafes") == "project"
    assert _auto_inbox_category("John is moving to Berlin") == "relationship"
    assert _auto_inbox_category("Something I am not sure how to classify") is None


def test_review_inbox_auto_files_clear_items(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(tmp_path / "inbox.json"))
    from api.services import agent_tools

    memory = inbox_store.add_item("I want to build an AI product for cafes")
    noise = inbox_store.add_item("yes")
    unknown = inbox_store.add_item("Something I am not sure how to classify")

    async def fake_process(inp):
        category = inp["category"]
        inbox_store.update_item(
            inp["item_id"],
            status="dismissed" if category == "dismissed" else "processed",
            category=category,
        )
        return f"classified as {category}"

    monkeypatch.setattr(agent_tools, "_tool_process_inbox_item", fake_process)
    result = asyncio.run(agent_tools._tool_review_inbox({"since_days": 7}))

    assert "Automatically filed 2 clear item(s)" in result
    assert inbox_store.list_items(status="processed")[0]["id"] == memory["id"]
    assert inbox_store.list_items(status="dismissed")[0]["id"] == noise["id"]
    assert inbox_store.list_items(status="unreviewed")[0]["id"] == unknown["id"]


def test_chat_capture_is_closed_after_successful_interpretation(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(tmp_path / "inbox.json"))
    from api.routes.chat import (
        _close_chat_inbox_item,
        _capture_candidate,
        _extract_commitment_candidate,
        _requested_life_review_mode,
        _source_capture_candidate,
        _remove_capture_permission_prompt,
    )

    assert _capture_candidate("[Voice message transcription]\nI want to build a cafe AI product")
    assert _extract_commitment_candidate("I promised John to send him the deck") == {
        "direction": "owed_by_me",
        "person_name": "John",
        "content": "send him the deck",
    }
    assert _requested_life_review_mode("What should I do today?") == "today"
    assert _requested_life_review_mode("Which projects am I neglecting?") == "neglected"
    assert _source_capture_candidate(
        "Interesting video https://youtube.com/watch?v=abc",
        {"type": "telegram", "urls": ["https://youtube.com/watch?v=abc"]},
    ) == "Interesting video https://youtube.com/watch?v=abc"
    cleaned = _remove_capture_permission_prompt(
        "I captured the idea.\nWant me to save this as a product note?"
    )
    assert cleaned == "I captured the idea."
    item = inbox_store.add_item("I want to build a cafe AI product")
    _close_chat_inbox_item(
        item["id"],
        item["content"],
        [{"tool": "save_memory", "input": {"content": item["content"]}, "is_error": False}],
    )

    saved = inbox_store.list_items(status="processed")
    assert saved[0]["category"] == "memory"


def test_chat_capture_closes_transient_conversation_as_dismissed(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(tmp_path / "inbox.json"))
    from api.routes.chat import _close_chat_inbox_item

    item = inbox_store.add_item("hello there")
    _close_chat_inbox_item(item["id"], item["content"], [])

    dismissed = inbox_store.list_items(status="dismissed")
    assert dismissed[0]["category"] == "dismissed"


def test_uncaptioned_media_remains_open_for_future_understanding(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(tmp_path / "inbox.json"))
    from api.routes.chat import _close_chat_inbox_item

    item = inbox_store.add_item("[Telegram photo without caption]")
    _close_chat_inbox_item(item["id"], item["content"], [])

    assert inbox_store.list_items(status="unreviewed")[0]["id"] == item["id"]


def test_commitments_are_persistent_and_queryable(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_COMMITMENTS_PATH", str(tmp_path / "commitments.json"))
    from api.services.agent_tools import _tool_manage_commitments

    created = _tool_manage_commitments({
        "action": "create",
        "content": "Send John the deck",
        "direction": "owed_by_me",
        "person_name": "John",
        "source": {"type": "telegram", "chat_id": "1", "message_id": 7},
    })
    assert "Commitment recorded" in created
    listed = _tool_manage_commitments({"action": "list", "person_name": "John"})
    assert "Send John the deck" in listed

    commitment_id = listed.split("- ", 1)[1].split(":", 1)[0]
    completed = _tool_manage_commitments({"action": "complete", "commitment_id": commitment_id})
    assert "Commitment completed" in completed


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


def test_add_item_deduplicates_same_source_identity(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(path))
    source = {"type": "telegram", "chat_id": "1", "message_id": 42}

    first = inbox_store.add_item("same message", source=source)
    retry = inbox_store.add_item("same message", source=source)
    different_message = inbox_store.add_item("same message", source={**source, "message_id": 43})

    assert retry["id"] == first["id"]
    assert different_message["id"] != first["id"]
    assert len(inbox_store.list_items(status=None)) == 2


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
