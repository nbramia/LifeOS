"""Tests for the lifeos_agent_* tool family.

Each tool is exercised against an in-memory SessionStore + TranscriptStore.
The worker's resume/dispatch loop is tested separately in test_agent_worker_loop.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.inter_agent import (
    Caps,
    InterAgentContext,
    DEFAULT_MAX_DESCENDANTS_PER_ROOT,
    DEFAULT_MAX_SPAWN_DEPTH,
    dispatch,
)
from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_YIELDED,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "sessions.db")


@pytest.fixture
def transcript(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(transcripts_dir=tmp_path / "transcripts")


@pytest.fixture
def parent(store: SessionStore):
    session = store.create(
        task_id="parent_task",
        status=STATUS_RUNNING,
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 10.0},
        expected_output="text",
    )
    return session


@pytest.fixture
def ctx(store: SessionStore, transcript: TranscriptStore, parent) -> InterAgentContext:
    return InterAgentContext(
        session_store=store,
        transcript_store=transcript,
        caller_session_id=parent.session_id,
        caps=Caps(),
    )


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_spawn_creates_child_inheriting_lineage(ctx, store, parent):
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "find the answer", "model": "local",
    })
    assert result["ok"]
    child_id = result["child_session_id"]
    child = store.get_by_session_id(child_id)
    assert child is not None
    assert child.parent_session_id == parent.session_id
    assert child.root_session_id == parent.session_id  # parent is the root
    assert child.spawn_depth == 1
    assert child.routing == "local"
    assert child.status == STATUS_CLAIMED
    # The first message is the prompt as a user turn.
    messages = store.get_messages(child_id)
    assert messages and messages[0]["role"] == "user"
    assert "find the answer" in messages[0]["content"]


@pytest.mark.unit
def test_spawn_rejects_empty_prompt(ctx):
    result = dispatch(ctx, "lifeos_agent_spawn", {"prompt": "  ", "model": "local"})
    assert not result["ok"]
    assert result["error"] == "invalid_arg"


@pytest.mark.unit
def test_spawn_rejects_invalid_model(ctx):
    result = dispatch(ctx, "lifeos_agent_spawn", {"prompt": "x", "model": "gpt5"})
    assert not result["ok"]
    assert result["error"] == "invalid_arg"


@pytest.mark.unit
def test_spawn_rejects_at_depth_cap(store, transcript, parent):
    # Build a chain parent → grandchild already at depth 3 — next spawn from
    # grandchild would exceed depth cap.
    deep = store.create(
        task_id="deep_task", status=STATUS_RUNNING, routing="local",
        budget={"max_dollars": 5.0, "wall_seconds": 100, "max_tokens": 100},
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=DEFAULT_MAX_SPAWN_DEPTH,
    )
    ctx = InterAgentContext(store, transcript, deep.session_id, Caps())
    result = dispatch(ctx, "lifeos_agent_spawn", {"prompt": "x", "model": "local"})
    assert not result["ok"]
    assert result["error"] == "cap_spawn_depth"


@pytest.mark.unit
def test_spawn_rejects_at_descendant_cap(store, transcript, parent):
    # Pre-populate `MAX` descendants.
    for i in range(DEFAULT_MAX_DESCENDANTS_PER_ROOT):
        store.create(
            task_id=f"d_{i}", status=STATUS_RUNNING, routing="local",
            budget={"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100},
            parent_session_id=parent.session_id,
            root_session_id=parent.session_id,
            spawn_depth=1,
        )
    ctx = InterAgentContext(store, transcript, parent.session_id, Caps())
    result = dispatch(ctx, "lifeos_agent_spawn", {"prompt": "x", "model": "local"})
    assert not result["ok"]
    assert result["error"] == "cap_descendants"


@pytest.mark.unit
def test_spawn_local_concurrency_cap(store, transcript, parent):
    # One local already running; cap is 1 by default.
    store.create(
        task_id="busy", status=STATUS_RUNNING, routing="local",
        budget={"max_dollars": 1.0, "wall_seconds": 60, "max_tokens": 100},
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    ctx = InterAgentContext(store, transcript, parent.session_id, Caps(max_concurrent_local=1))
    result = dispatch(ctx, "lifeos_agent_spawn", {"prompt": "x", "model": "local"})
    assert not result["ok"]
    assert result["error"] == "cap_concurrency_local"


@pytest.mark.unit
def test_spawn_budget_cannot_exceed_parent_remaining(ctx, store, parent):
    # Parent has $10 budget; ask for $20.
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "x", "model": "local", "max_dollars": 20.0,
    })
    assert not result["ok"]
    assert result["error"] == "budget_exceeded"


# ---------------------------------------------------------------------------
# send / check
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_send_enqueues_message(ctx, store, parent):
    child = store.create(
        task_id="c1", status=STATUS_RUNNING, routing="local",
        budget={"max_dollars": 5.0, "wall_seconds": 60, "max_tokens": 100},
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": child.session_id, "message": "hello child",
    })
    assert result["ok"]
    pending = store.drain_pending_messages(child.session_id)
    assert len(pending) == 1
    assert pending[0]["content"] == "hello child"
    assert pending[0]["sender_id"] == parent.session_id


@pytest.mark.unit
def test_send_rejects_terminal_session(ctx, store, parent):
    dead = store.create(
        task_id="dead", status=STATUS_FAILED, routing="local",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
    )
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": dead.session_id, "message": "are you there",
    })
    assert not result["ok"]
    assert result["error"] == "terminal"


@pytest.mark.unit
def test_check_returns_session_metadata(ctx, store, parent):
    child = store.create(
        task_id="c2", status=STATUS_RUNNING, routing="local",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
    )
    store.record_spend(child.task_id, tokens_in=100, tokens_out=50, dollars=0.5)
    result = dispatch(ctx, "lifeos_agent_check", {"session_id": child.session_id})
    assert result["ok"]
    assert result["status"] == STATUS_RUNNING
    assert result["tokens_used"] == 150
    assert result["dollars_used"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# yield_until
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_yield_until_marks_caller_yielded(ctx, store, parent):
    child = store.create(
        task_id="c", status=STATUS_RUNNING, routing="local",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
    )
    result = dispatch(ctx, "lifeos_agent_yield_until", {
        "children": [child.session_id], "reason": "waiting for child",
    })
    assert result["ok"]
    refreshed = store.get(parent.task_id)
    assert refreshed.status == STATUS_YIELDED
    assert refreshed.yield_waiting_for == [child.session_id]


@pytest.mark.unit
def test_yield_until_rejects_foreign_children(store, transcript, parent):
    # Create an unrelated root + child.
    foreign_parent = store.create(
        task_id="foreign", status=STATUS_RUNNING, routing="local",
    )
    foreign_child = store.create(
        task_id="fc", status=STATUS_RUNNING, routing="local",
        parent_session_id=foreign_parent.session_id,
        root_session_id=foreign_parent.session_id,
        spawn_depth=1,
    )
    ctx = InterAgentContext(store, transcript, parent.session_id, Caps())
    result = dispatch(ctx, "lifeos_agent_yield_until", {
        "children": [foreign_child.session_id],
    })
    assert not result["ok"]
    assert result["error"] == "forbidden"


@pytest.mark.unit
def test_yield_until_rejects_empty_list(ctx):
    result = dispatch(ctx, "lifeos_agent_yield_until", {"children": []})
    assert not result["ok"]
    assert result["error"] == "invalid_arg"


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_kill_terminates_descendant(ctx, store, parent):
    child = store.create(
        task_id="vc", status=STATUS_RUNNING, routing="local",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
    )
    result = dispatch(ctx, "lifeos_agent_kill", {
        "session_id": child.session_id, "reason": "no longer needed",
    })
    assert result["ok"]
    assert store.get(child.task_id).status == STATUS_FAILED


@pytest.mark.unit
def test_kill_rejects_non_descendant(store, transcript, parent):
    foreign = store.create(task_id="x", status=STATUS_RUNNING, routing="local")
    ctx = InterAgentContext(store, transcript, parent.session_id, Caps())
    result = dispatch(ctx, "lifeos_agent_kill", {
        "session_id": foreign.session_id,
    })
    assert not result["ok"]
    assert result["error"] == "forbidden"


@pytest.mark.unit
def test_kill_cannot_target_self(ctx, parent):
    result = dispatch(ctx, "lifeos_agent_kill", {"session_id": parent.session_id})
    assert not result["ok"]


# ---------------------------------------------------------------------------
# transcript_read / sessions_list
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_transcript_read_returns_events(ctx, transcript, parent):
    transcript.append(parent.session_id, "event1", {"a": 1})
    transcript.append(parent.session_id, "event2", {"b": 2})
    result = dispatch(ctx, "lifeos_agent_transcript_read", {
        "session_id": parent.session_id,
    })
    assert result["ok"]
    assert result["count"] == 2
    kinds = [e["kind"] for e in result["events"]]
    assert kinds == ["event1", "event2"]


@pytest.mark.unit
def test_transcript_read_since_turn(ctx, transcript, parent):
    transcript.append(parent.session_id, "a", {})
    transcript.append(parent.session_id, "b", {})
    transcript.append(parent.session_id, "c", {})
    result = dispatch(ctx, "lifeos_agent_transcript_read", {
        "session_id": parent.session_id, "since_turn": 1,
    })
    assert [e["kind"] for e in result["events"]] == ["b", "c"]


@pytest.mark.unit
def test_sessions_list_filters_by_status(ctx, store, parent):
    store.create(task_id="t_done", status=STATUS_COMPLETED, routing="local",
                 parent_session_id=parent.session_id, root_session_id=parent.session_id)
    store.create(task_id="t_run", status=STATUS_RUNNING, routing="local",
                 parent_session_id=parent.session_id, root_session_id=parent.session_id)
    result = dispatch(ctx, "lifeos_agent_sessions_list", {"status": STATUS_RUNNING})
    assert result["ok"]
    statuses = {s["status"] for s in result["sessions"]}
    assert statuses == {STATUS_RUNNING}


@pytest.mark.unit
def test_sessions_list_filters_by_parent(ctx, store, parent):
    store.create(task_id="ch1", status=STATUS_RUNNING, routing="local",
                 parent_session_id=parent.session_id, root_session_id=parent.session_id)
    store.create(task_id="unrelated", status=STATUS_RUNNING, routing="local")
    result = dispatch(ctx, "lifeos_agent_sessions_list", {
        "parent_session_id": parent.session_id,
    })
    assert result["ok"]
    task_ids = {s["task_id"] for s in result["sessions"]}
    assert task_ids == {"ch1"}


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_dispatch_unknown_tool_errors(ctx):
    result = dispatch(ctx, "lifeos_agent_nonexistent", {})
    assert not result["ok"]
    assert result["error"] == "unknown_tool"


@pytest.mark.unit
def test_dispatch_handles_handler_exception(store, transcript, parent, monkeypatch):
    """An exception inside a handler is caught and returned as ok=False."""
    ctx = InterAgentContext(store, transcript, parent.session_id, Caps())
    from api.services.agent_worker import inter_agent
    def boom(c, a):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(inter_agent.DISPATCH_TABLE, "lifeos_agent_check", boom)
    result = dispatch(ctx, "lifeos_agent_check", {"session_id": "x"})
    assert not result["ok"]
    assert result["error"] == "crashed"
    assert "kaboom" in result["message"]
