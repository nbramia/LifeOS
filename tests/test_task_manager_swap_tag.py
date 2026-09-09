"""Atomic tag-swap tests for TaskManager.

Used by the external agent worker to perform atomic tag transitions even
when multiple workers race. Storage is markdown-based with a single in-process
lock, so the racing scenario is *two threads sharing one TaskManager* — which
matches production where the worker hits the API server's singleton.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from api.services.task_manager import TaskManager


@pytest.fixture
def manager(tmp_path: Path) -> TaskManager:
    vault = tmp_path / "vault"
    index = tmp_path / "task_index.json"
    return TaskManager(vault_path=vault, index_path=index)


@pytest.mark.unit
def test_swap_tag_swaps_when_present(manager: TaskManager):
    task = manager.create("claim me", tags=["agent", "research"])
    assert manager.swap_tag(task.id, "agent", "agent-running") is True

    refreshed = manager.get(task.id)
    assert "agent-running" in refreshed.tags
    assert "agent" not in refreshed.tags
    assert "research" in refreshed.tags  # other tags untouched


@pytest.mark.unit
def test_swap_tag_accepts_hash_prefix(manager: TaskManager):
    task = manager.create("with hash", tags=["agent"])
    assert manager.swap_tag(task.id, "#agent", "#agent-running") is True
    refreshed = manager.get(task.id)
    assert "agent-running" in refreshed.tags


@pytest.mark.unit
def test_swap_tag_returns_false_when_tag_absent(manager: TaskManager):
    task = manager.create("no agent tag", tags=["research"])
    assert manager.swap_tag(task.id, "agent", "agent-running") is False
    # Task tags unchanged
    assert manager.get(task.id).tags == ["research"]


@pytest.mark.unit
def test_swap_tag_returns_false_when_task_missing(manager: TaskManager):
    assert manager.swap_tag("does-not-exist", "agent", "agent-running") is False


@pytest.mark.unit
def test_swap_tag_is_atomic_under_race(manager: TaskManager):
    """Exactly one of N concurrent swap_tag calls on the same task wins."""
    task = manager.create("racey", tags=["agent"])

    n = 8
    results: list[bool] = []
    barrier = threading.Barrier(n)

    def attempt():
        barrier.wait()
        results.append(manager.swap_tag(task.id, "agent", "agent-running"))

    threads = [threading.Thread(target=attempt) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"expected exactly one winner, got {results}"
    # Final state: tag is swapped to "agent-running" exactly once.
    final_tags = manager.get(task.id).tags
    assert final_tags.count("agent-running") == 1
    assert "agent" not in final_tags


def _claim(manager: TaskManager, task_id: str) -> tuple[bool, bool]:
    return manager.claim_for_agent(
        task_id,
        pickup_tags={"agent", "hermes", "cloud"},
        exclusion_tags={"agent-running", "agent-blocked", "agent-completed"},
        eligible_statuses={"todo", "urgent"},
    )


@pytest.mark.unit
def test_claim_for_agent_adds_running_for_engine_only_task(manager: TaskManager):
    task = manager.create("engine only", tags=["hermes"])
    assert _claim(manager, task.id) == (True, False)
    refreshed = manager.get(task.id)
    assert "agent-running" in refreshed.tags
    assert "hermes" in refreshed.tags
    assert refreshed.status == "in_progress"


@pytest.mark.unit
def test_claim_for_agent_consumes_legacy_queue_tag(manager: TaskManager):
    task = manager.create("legacy", tags=["agent"])
    assert _claim(manager, task.id) == (True, True)
    assert manager.get(task.id).tags == ["agent-running"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "tags"),
    [
        ("done", ["hermes"]),
        ("todo", ["research"]),
        ("todo", ["hermes", "agent-running"]),
        ("todo", ["cloud", "agent-blocked"]),
    ],
)
def test_claim_for_agent_rejects_ineligible_current_state(manager: TaskManager, status, tags):
    task = manager.create("ineligible", status=status, tags=tags)
    assert _claim(manager, task.id) == (False, False)
    refreshed = manager.get(task.id)
    assert refreshed.status == status
    assert refreshed.tags == tags


@pytest.mark.unit
def test_claim_for_agent_returns_false_when_already_claimed(manager: TaskManager):
    task = manager.create("already", tags=["hermes", "agent-running"])
    assert _claim(manager, task.id) == (False, False)
    assert manager.get(task.id).tags == ["hermes", "agent-running"]


@pytest.mark.unit
def test_claim_for_agent_is_atomic_under_race(manager: TaskManager):
    task = manager.create("racey engine", tags=["cloud"])
    n = 8
    results: list[tuple[bool, bool]] = []
    barrier = threading.Barrier(n)

    def attempt():
        barrier.wait()
        results.append(_claim(manager, task.id))

    threads = [threading.Thread(target=attempt) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count((True, False)) == 1, f"expected exactly one winner, got {results}"
    assert manager.get(task.id).tags.count("agent-running") == 1


@pytest.mark.unit
def test_remove_tag_if_present_removes_when_present(manager: TaskManager):
    task = manager.create("claimed", tags=["hermes", "agent-running"])
    assert manager.remove_tag_if_present(task.id, "agent-running") is True
    refreshed = manager.get(task.id)
    assert "agent-running" not in refreshed.tags
    assert "hermes" in refreshed.tags
    assert "agent" not in refreshed.tags


@pytest.mark.unit
def test_remove_tag_if_present_returns_false_when_absent(manager: TaskManager):
    task = manager.create("clean", tags=["hermes"])
    assert manager.remove_tag_if_present(task.id, "agent-running") is False
