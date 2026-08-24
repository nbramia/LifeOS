from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from api.services import agent_tools


pytestmark = pytest.mark.unit


def test_life_review_combines_recorded_sources(monkeypatch):
    task = SimpleNamespace(
        status="todo", description="Send the deck", due_date="2026-08-24", priority="high"
    )
    commitment = {
        "direction": "owed_by_me",
        "person_name": "John",
        "content": "Send the deck",
        "due_at": "",
    }
    memory = SimpleNamespace(
        category="projects",
        content="Build the cafe AI product",
        updated_at=datetime.now(timezone.utc) - timedelta(days=21),
        created_at=None,
    )
    monkeypatch.setattr(
        "api.services.task_manager.get_task_manager",
        lambda: SimpleNamespace(list_tasks=lambda: [task]),
    )
    monkeypatch.setattr(
        "api.services.commitment_store.list_commitments",
        lambda **_kwargs: [commitment],
    )
    monkeypatch.setattr(
        "api.services.inbox_store.list_items",
        lambda **_kwargs: [{"content": "Unresolved capture"}],
    )
    monkeypatch.setattr(
        "api.services.scheduler_store.get_scheduler_store",
        lambda: SimpleNamespace(list_all=lambda: []),
    )
    monkeypatch.setattr(
        "api.services.memory_store.get_memory_store",
        lambda: SimpleNamespace(list_memories=lambda **_kwargs: [memory]),
    )
    monkeypatch.setattr(
        "api.services.life_model_store.list_records",
        lambda: {
            "identity": [],
            "values": [],
            "current_state": [
                {"content": "Building the cafe AI product", "evidence_type": "explicit"},
                {"content": "Repairing the car is a top priority", "evidence_type": "explicit"},
            ],
            "ideal_state": [],
            "philosophy": [],
        },
    )

    result = agent_tools._tool_life_review({"mode": "today"})

    assert "Send the deck" in result
    assert "John" in result
    assert "Follow-ups" in result
    assert "You owe John" in result
    assert "Unresolved capture" in result
    assert "Build the cafe AI product" in result
    assert "days since update" in result
    assert "Building the cafe AI product" in result
    assert "Repairing the car is a top priority" in result
