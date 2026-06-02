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
    # The prompt is queued as a pending message — the worker's spawned-
    # dispatcher drains it and uses it as the task description so the
    # executor's _seed_conversation produces a proper system + user turn.
    pending = store.drain_pending_messages(child_id)
    assert len(pending) == 1
    assert pending[0]["content"] == "find the answer"
    assert pending[0]["sender_id"] == parent.session_id


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
@pytest.mark.parametrize("model", ["claude_code", "codex"])
def test_spawn_cli_child_for_capability_fallback(ctx, store, parent, model):
    """An agent can delegate to a CLI engine (claude_code/codex) — the child is
    created with that routing so the worker's spawned-dispatcher runs it."""
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "open the dashboard and screenshot it", "model": model,
    })
    assert result["ok"]
    child = store.get_by_session_id(result["child_session_id"])
    assert child.routing == model
    assert child.parent_session_id == parent.session_id
    assert child.status == STATUS_CLAIMED
    # Plain-string prompt — the codex/claude_code payload parsers fall back to
    # treating it as the prompt, so no JSON wrapping is required.
    pending = store.drain_pending_messages(child.session_id)
    assert pending[0]["content"] == "open the dashboard and screenshot it"


@pytest.mark.unit
def test_spawn_cli_child_skips_dollar_ceiling(store, transcript):
    """CLI routes are subscription-billed, so a CLI child spawns even when the
    parent has no remaining per-token dollar budget."""
    broke_parent = store.create(
        task_id="broke", status=STATUS_RUNNING, routing="codex",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 0.0},
        expected_output="text",
    )
    ctx = InterAgentContext(
        session_store=store, transcript_store=transcript,
        caller_session_id=broke_parent.session_id, caps=Caps(),
    )
    result = dispatch(ctx, "lifeos_agent_spawn", {"prompt": "x", "model": "claude_code"})
    assert result["ok"]  # would be budget_exceeded for a managed child


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


@pytest.mark.unit
def test_yield_until_kills_remote_session_for_cloud_caller(store, transcript):
    """Live bug: a cloud parent calling yield_until kept burning Anthropic
    tokens AFTER the yield because the tool only updated local state.
    Now the handler must call driver.kill_session() so the remote session
    stops generating immediately."""
    cloud_parent = store.create(
        task_id="cloud_p", status=STATUS_RUNNING, routing="claude",
    )
    store.set_managed_session_id("cloud_p", "sesn_remote_xyz")
    cloud_parent = store.get("cloud_p")

    child = store.create(
        task_id="c", status=STATUS_RUNNING, routing="claude",
        parent_session_id=cloud_parent.session_id,
        root_session_id=cloud_parent.session_id,
        spawn_depth=1,
    )

    killed: list[tuple[str, str]] = []
    class _FakeDriver:
        def kill_session(self, session_id, reason=""):
            killed.append((session_id, reason))

    ctx = InterAgentContext(
        store, transcript, cloud_parent.session_id, Caps(),
        managed_driver=_FakeDriver(),
    )
    result = dispatch(ctx, "lifeos_agent_yield_until", {
        "children": [child.session_id], "reason": "waiting on child",
    })
    assert result["ok"]
    # The remote session got killed; local state still records the yield.
    assert killed == [("sesn_remote_xyz", "yield_until")]
    assert store.get("cloud_p").status == STATUS_YIELDED


@pytest.mark.unit
def test_yield_until_local_caller_does_not_kill_remote(store, transcript):
    """Local sessions have no remote handle — yield_until should not
    attempt to kill anything (and should not crash when managed_driver
    is None, which is the local-executor's default context shape)."""
    local_parent = store.create(
        task_id="local_p", status=STATUS_RUNNING, routing="local",
    )
    child = store.create(
        task_id="c", status=STATUS_RUNNING, routing="local",
        parent_session_id=local_parent.session_id,
        root_session_id=local_parent.session_id,
        spawn_depth=1,
    )
    ctx = InterAgentContext(
        store, transcript, local_parent.session_id, Caps(),
        managed_driver=None,
    )
    result = dispatch(ctx, "lifeos_agent_yield_until", {
        "children": [child.session_id],
    })
    assert result["ok"]
    assert store.get("local_p").status == STATUS_YIELDED


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
def test_kill_calls_managed_driver_for_remote_session(store, transcript, parent):
    """Killing a managed descendant should attempt the remote kill via the driver."""
    target = store.create(
        task_id="m_target", status=STATUS_RUNNING, routing="claude",
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
    )
    store.set_managed_session_id("m_target", "remote_xyz")
    target = store.get_by_session_id(target.session_id)

    calls = []
    class _FakeDriver:
        def kill_session(self, sid, reason=""):
            calls.append((sid, reason))

    ctx_with_driver = InterAgentContext(
        store, transcript, parent.session_id, Caps(), managed_driver=_FakeDriver(),
    )
    result = dispatch(ctx_with_driver, "lifeos_agent_kill", {
        "session_id": target.session_id, "reason": "no longer needed",
    })
    assert result["ok"]
    assert calls and calls[0] == ("remote_xyz", "no longer needed")


@pytest.mark.unit
def test_kill_remote_failure_doesnt_block_local_kill(store, transcript, parent):
    target = store.create(
        task_id="m_t", status=STATUS_RUNNING, routing="claude",
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
    )
    store.set_managed_session_id("m_t", "remote_abc")

    class _BoomDriver:
        def kill_session(self, sid, reason=""):
            raise RuntimeError("network down")

    ctx_with_driver = InterAgentContext(
        store, transcript, parent.session_id, Caps(), managed_driver=_BoomDriver(),
    )
    result = dispatch(ctx_with_driver, "lifeos_agent_kill", {
        "session_id": target.session_id,
    })
    # Local DB still marked failed even though remote kill raised.
    assert result["ok"]
    assert store.get_by_session_id(target.session_id).status == STATUS_FAILED


@pytest.mark.unit
def test_send_rejects_foreign_lineage(store, transcript, parent):
    """An agent can't inject messages into sessions outside its lineage."""
    foreign = store.create(task_id="x", status=STATUS_RUNNING, routing="local")
    ctx = InterAgentContext(store, transcript, parent.session_id, Caps())
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": foreign.session_id, "message": "psst",
    })
    assert not result["ok"]
    assert result["error"] == "forbidden"


@pytest.mark.unit
def test_spawn_excludes_caller_from_concurrency_cap(store, transcript):
    """The parent shouldn't count against its own routing cap — the expected
    pattern is `spawn` followed immediately by `yield_until`."""
    local_parent = store.create(
        task_id="p_local", status=STATUS_RUNNING, routing="local",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 10.0},
        expected_output="text",
    )
    ctx = InterAgentContext(
        store, transcript, local_parent.session_id, Caps(max_concurrent_local=1),
    )
    # No other local sessions besides the caller; the spawn should succeed
    # because the caller is excluded from the count.
    result = dispatch(ctx, "lifeos_agent_spawn", {"prompt": "x", "model": "local"})
    assert result["ok"], f"spawn should have succeeded; got {result}"


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
