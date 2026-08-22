"""Tests for the lifeos_agent_* tool family.

Each tool is exercised against an in-memory SessionStore + TranscriptStore.
The worker's resume/dispatch loop is tested separately in test_agent_worker_loop.
"""
from __future__ import annotations

import signal as _signal_module
from pathlib import Path

import pytest

from api.services.agent_worker import inter_agent
from api.services.agent_worker.inter_agent import (
    Caps,
    InterAgentContext,
    DEFAULT_MAX_DESCENDANTS_PER_ROOT,
    DEFAULT_MAX_SPAWN_DEPTH,
    dispatch,
    teardown_session,
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
@pytest.mark.parametrize("tier", ["haiku", "sonnet", "opus"])
def test_spawn_claude_code_tier_persisted(ctx, store, tier):
    """A claude_code child's `tier` arg lands on the session as
    claude_code_model so the executor runs the CLI with that --model (#349)."""
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "look up today's matches", "model": "claude_code", "tier": tier,
    })
    assert result["ok"]
    child = store.get_by_session_id(result["child_session_id"])
    assert child.claude_code_model == tier


@pytest.mark.unit
def test_spawn_claude_code_default_tier_is_none(ctx, store):
    """No tier → claude_code_model NULL → the executor falls back to opus."""
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "hard reasoning task", "model": "claude_code",
    })
    assert result["ok"]
    child = store.get_by_session_id(result["child_session_id"])
    assert child.claude_code_model is None


@pytest.mark.unit
def test_spawn_rejects_invalid_tier(ctx):
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "x", "model": "claude_code", "tier": "ultra",
    })
    assert not result["ok"]
    assert result["error"] == "invalid_arg"


@pytest.mark.unit
def test_spawn_tier_ignored_for_non_claude_code(ctx, store):
    """tier is a claude_code-only knob — a valid tier on a local child is
    silently dropped rather than erroring, so callers can pass it uniformly."""
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "x", "model": "local", "tier": "haiku",
    })
    assert result["ok"]
    child = store.get_by_session_id(result["child_session_id"])
    assert child.claude_code_model is None


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


def _seed_completed_cli_child(
    store, parent, *, task_id="cli_child", routing="claude_code",
    status=STATUS_COMPLETED, cli_session_id="cli-abc-123",
):
    """A CLI child that finished a turn — the reopen-on-send target shape."""
    child = store.create(
        task_id=task_id, status=status, routing=routing,
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    if cli_session_id:
        store.set_claude_code_session_id(task_id, cli_session_id)
    return child


@pytest.mark.unit
@pytest.mark.parametrize("routing", ["claude_code", "codex"])
def test_send_reopens_own_completed_cli_child(ctx, store, transcript, parent, routing):
    """A parent answering its completed CLI child's [needs clarification]
    question reopens it: message enqueued, status flipped back to CLAIMED so
    the spawned-session dispatcher resumes it via the persisted CLI session id."""
    child = _seed_completed_cli_child(store, parent, routing=routing)
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": child.session_id, "message": "use API v2",
    })
    assert result["ok"]
    assert result["reopened"] is True
    refreshed = store.get_by_session_id(child.session_id)
    assert refreshed.status == STATUS_CLAIMED
    pending = store.drain_pending_messages(child.session_id)
    assert len(pending) == 1
    assert pending[0]["content"] == "use API v2"
    assert pending[0]["sender_id"] == parent.session_id
    kinds = [e["kind"] for e in transcript.read(child.session_id)]
    assert "inter_agent_send_reopen" in kinds


@pytest.mark.unit
def test_send_reopen_message_enqueued_before_status_flip(ctx, store, parent, monkeypatch):
    """The pending message must exist by the time the child turns CLAIMED —
    otherwise the dispatch tick could resume the child with an empty prompt."""
    child = _seed_completed_cli_child(store, parent)
    calls: list[str] = []
    original_enqueue = store.enqueue_message
    original_update = store.update_status
    monkeypatch.setattr(store, "enqueue_message",
                        lambda *a, **k: (calls.append("enqueue"), original_enqueue(*a, **k))[1])
    monkeypatch.setattr(store, "update_status",
                        lambda *a, **k: (calls.append("status"), original_update(*a, **k))[1])
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": child.session_id, "message": "answer",
    })
    assert result["ok"]
    assert calls.index("enqueue") < calls.index("status")


@pytest.mark.unit
def test_send_rejects_reopen_of_failed_child(ctx, store, parent):
    """Only COMPLETED children reopen — a FAILED child stays terminal."""
    child = _seed_completed_cli_child(store, parent, status=STATUS_FAILED)
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": child.session_id, "message": "try again",
    })
    assert not result["ok"]
    assert result["error"] == "terminal"
    assert store.get_by_session_id(child.session_id).status == STATUS_FAILED


@pytest.mark.unit
@pytest.mark.parametrize("routing", ["local", "claude"])
def test_send_rejects_reopen_of_non_cli_child(ctx, store, parent, routing):
    """local/managed children have no CLI resume path — reopen is CLI-only."""
    child = _seed_completed_cli_child(store, parent, routing=routing)
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": child.session_id, "message": "hello",
    })
    assert not result["ok"]
    assert result["error"] == "terminal"
    assert store.get_by_session_id(child.session_id).status == STATUS_COMPLETED


@pytest.mark.unit
def test_send_rejects_reopen_without_cli_session_id(ctx, store, parent):
    """A completed child whose init never persisted a CLI session id can't be
    resumed (`-r` needs the UUID) — reopen must refuse, not strand it CLAIMED."""
    child = _seed_completed_cli_child(store, parent, cli_session_id=None)
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": child.session_id, "message": "hello",
    })
    assert not result["ok"]
    assert result["error"] == "terminal"
    assert store.get_by_session_id(child.session_id).status == STATUS_COMPLETED


@pytest.mark.unit
def test_send_rejects_reopen_of_non_direct_child(ctx, store, parent):
    """Only the direct parent may reopen — a same-lineage grandparent or
    sibling sending to a completed session still gets the terminal error."""
    middle = store.create(
        task_id="mid", status=STATUS_RUNNING, routing="claude",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    grandchild = store.create(
        task_id="gc", status=STATUS_COMPLETED, routing="claude_code",
        parent_session_id=middle.session_id,
        root_session_id=parent.session_id,
        spawn_depth=2,
    )
    store.set_claude_code_session_id("gc", "cli-gc-1")
    # ctx's caller is `parent` — the grandparent of `grandchild`.
    result = dispatch(ctx, "lifeos_agent_send", {
        "session_id": grandchild.session_id, "message": "hi",
    })
    assert not result["ok"]
    assert result["error"] == "terminal"
    assert store.get_by_session_id(grandchild.session_id).status == STATUS_COMPLETED


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


# ---------------------------------------------------------------------------
# #379 — teardown_session reaps the local CLI subprocess
# ---------------------------------------------------------------------------


class _SignalRecorder:
    """Records os.kill / os.killpg calls and scripts liveness.

    `alive_for` controls how many `os.kill(pid, 0)` liveness probes return
    "alive" before the process is considered gone (ProcessLookupError). A
    large value keeps it alive through the whole grace window so the SIGKILL
    fallback fires; a small value simulates a clean exit under SIGTERM.

    `group_gone_after` controls the #379 SIGKILL sweep: once the leader pid has
    been reported gone (its liveness probe raised), `killpg` raises
    ProcessLookupError to model a fully-exited group (no lingering children). If
    left None, killpg always succeeds — modelling a group child that outlived the
    leader, so the sweep lands.
    """

    def __init__(self, alive_for: int = 1000, initial_lookup_error: bool = False,
                 group_gone_when_leader_gone: bool = False):
        self.killpg_calls: list[tuple[int, int]] = []
        self._liveness_probes = 0
        self._alive_for = alive_for
        self._initial_lookup_error = initial_lookup_error
        self._group_gone_when_leader_gone = group_gone_when_leader_gone
        self._leader_gone = False

    def kill(self, pid, sig):
        # Liveness probe (sig == 0). The first probe is the up-front liveness
        # check; subsequent ones poll during the SIGTERM grace window.
        if sig == 0:
            if self._initial_lookup_error:
                self._leader_gone = True
                raise ProcessLookupError
            self._liveness_probes += 1
            if self._liveness_probes > self._alive_for:
                self._leader_gone = True
                raise ProcessLookupError
            return None
        raise AssertionError(f"unexpected os.kill signal {sig}")

    def killpg(self, pgid, sig):
        # Model a fully-gone group for the SIGKILL sweep: once the leader is gone
        # and the test asked for it, killpg raises (no children left to reap).
        if (self._group_gone_when_leader_gone and self._leader_gone
                and sig == _signal_module.SIGKILL):
            raise ProcessLookupError
        self.killpg_calls.append((pgid, sig))


def _seed_claude_code_with_pid(store, transcript, parent, *, pid=4242, pgid=4242):
    target = store.create(
        task_id="cc_target", status=STATUS_RUNNING, routing="claude_code",
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
    )
    transcript.append(target.session_id, "claude_code_pid", {"pid": pid, "pgid": pgid})
    return store.get_by_session_id(target.session_id)


@pytest.mark.unit
def test_teardown_kills_local_subprocess_sigterm_then_sigkill(
    store, transcript, parent, monkeypatch
):
    """A claude_code session with a recorded pid is SIGTERM'd then — when still
    alive through the grace window — SIGKILL'd, both on the pgid."""
    import signal as _signal

    target = _seed_claude_code_with_pid(store, transcript, parent, pid=4242, pgid=4242)
    rec = _SignalRecorder(alive_for=1000)  # never dies → SIGKILL fallback
    monkeypatch.setattr(inter_agent.os, "kill", rec.kill)
    monkeypatch.setattr(inter_agent.os, "killpg", rec.killpg)
    monkeypatch.setattr(inter_agent.time, "sleep", lambda _s: None)
    # Collapse the grace window so the loop exits to the SIGKILL branch fast.
    monkeypatch.setattr(inter_agent, "_LOCAL_KILL_GRACE_S", 0.0)

    teardown_session(
        store, transcript, target,
        transcript_kind="operator_killed",
        transcript_payload={"reason": "stop"},
        managed_driver=None,
    )

    signals = [sig for _pgid, sig in rec.killpg_calls]
    assert _signal.SIGTERM in signals
    assert _signal.SIGKILL in signals
    assert all(pgid == 4242 for pgid, _sig in rec.killpg_calls)
    # The kill is audited.
    kinds = [e["kind"] for e in transcript.read(target.session_id)]
    assert "local_subprocess_killed" in kinds
    assert store.get_by_session_id(target.session_id).status == STATUS_FAILED


@pytest.mark.unit
def test_teardown_local_subprocess_clean_exit_no_sigkill(
    store, transcript, parent, monkeypatch
):
    """If the leader AND its whole group exit during the SIGTERM grace window,
    the SIGKILL sweep is a no-op (group already gone) — only SIGTERM lands."""
    import signal as _signal

    target = _seed_claude_code_with_pid(store, transcript, parent, pid=7, pgid=7)
    # alive_for=1: the up-front liveness check passes, then the first in-grace
    # probe reports the leader gone → clean exit under SIGTERM. With
    # group_gone_when_leader_gone the #379 sweep's killpg(SIGKILL) raises
    # ProcessLookupError (no lingering children), so no SIGKILL is recorded.
    rec = _SignalRecorder(alive_for=1, group_gone_when_leader_gone=True)
    monkeypatch.setattr(inter_agent.os, "kill", rec.kill)
    monkeypatch.setattr(inter_agent.os, "killpg", rec.killpg)
    monkeypatch.setattr(inter_agent.time, "sleep", lambda _s: None)

    teardown_session(
        store, transcript, target,
        transcript_kind="operator_killed",
        transcript_payload={"reason": "stop"},
        managed_driver=None,
    )

    signals = [sig for _pgid, sig in rec.killpg_calls]
    assert signals == [_signal.SIGTERM]
    assert _signal.SIGKILL not in signals
    # The kill is still audited as a SIGTERM exit.
    kinds = [e["kind"] for e in transcript.read(target.session_id)]
    assert "local_subprocess_killed" in kinds


@pytest.mark.unit
def test_teardown_sigkill_sweep_reaps_lingering_group_child(
    store, transcript, parent, monkeypatch
):
    """#379: if the leader exits under SIGTERM but a group child lingers, the
    SIGKILL sweep still fires on the pgid so the survivor is reaped. (The old
    grace loop `break`'d on the leader exiting and skipped the SIGKILL.)"""
    import signal as _signal

    target = _seed_claude_code_with_pid(store, transcript, parent, pid=55, pgid=55)
    # Leader gone after the first in-grace probe, but group_gone_when_leader_gone
    # left False → killpg(SIGKILL) lands (a child outlived the leader).
    rec = _SignalRecorder(alive_for=1)
    monkeypatch.setattr(inter_agent.os, "kill", rec.kill)
    monkeypatch.setattr(inter_agent.os, "killpg", rec.killpg)
    monkeypatch.setattr(inter_agent.time, "sleep", lambda _s: None)

    teardown_session(
        store, transcript, target,
        transcript_kind="operator_killed",
        transcript_payload={"reason": "stop"},
        managed_driver=None,
    )

    signals = [sig for _pgid, sig in rec.killpg_calls]
    # The sweep escalated to SIGKILL even though the leader had exited.
    assert _signal.SIGTERM in signals
    assert _signal.SIGKILL in signals
    assert all(pgid == 55 for pgid, _sig in rec.killpg_calls)
    kinds = [e["kind"] for e in transcript.read(target.session_id)]
    assert "local_subprocess_killed" in kinds


@pytest.mark.unit
def test_teardown_stale_pid_does_not_raise_or_signal(
    store, transcript, parent, monkeypatch
):
    """A stale pid (the process already exited — `os.kill(pid, 0)` raises
    ProcessLookupError) is handled gracefully: no killpg, no raise, and teardown
    still flips status + writes the kill transcript event."""
    target = _seed_claude_code_with_pid(store, transcript, parent, pid=999, pgid=999)
    rec = _SignalRecorder(initial_lookup_error=True)
    monkeypatch.setattr(inter_agent.os, "kill", rec.kill)
    monkeypatch.setattr(inter_agent.os, "killpg", rec.killpg)

    # Must not raise.
    teardown_session(
        store, transcript, target,
        transcript_kind="operator_killed",
        transcript_payload={"reason": "stop"},
        managed_driver=None,
    )

    assert rec.killpg_calls == []  # nothing signalled
    # Teardown's other effects still happened.
    assert store.get_by_session_id(target.session_id).status == STATUS_FAILED
    kinds = [e["kind"] for e in transcript.read(target.session_id)]
    assert "operator_killed" in kinds
    assert "local_subprocess_killed" not in kinds  # no kill recorded for a dead proc


@pytest.mark.unit
def test_teardown_managed_session_skips_local_kill_but_does_managed(
    store, transcript, parent, monkeypatch
):
    """A pure managed/cloud session (routing='claude', no pid event) must NOT
    attempt a local kill, but MUST still flip status, append the transcript
    event, and call the managed driver's kill_session."""
    target = store.create(
        task_id="m_target", status=STATUS_RUNNING, routing="claude",
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
    )
    store.set_managed_session_id("m_target", "remote_xyz")
    target = store.get_by_session_id(target.session_id)

    rec = _SignalRecorder()
    monkeypatch.setattr(inter_agent.os, "kill", rec.kill)
    monkeypatch.setattr(inter_agent.os, "killpg", rec.killpg)

    managed_calls = []
    class _FakeDriver:
        def kill_session(self, sid, reason=""):
            managed_calls.append((sid, reason))

    result = teardown_session(
        store, transcript, target,
        transcript_kind="operator_killed",
        transcript_payload={"reason": "stop"},
        managed_driver=_FakeDriver(),
    )

    assert result["managed_failure"] is None
    # No local signal attempt at all (routing is not a CLI route).
    assert rec.killpg_calls == []
    # Managed kill still happened.
    assert managed_calls == [("remote_xyz", "stop")]
    # Status flip + transcript event still happened.
    assert store.get_by_session_id(target.session_id).status == STATUS_FAILED
    kinds = [e["kind"] for e in transcript.read(target.session_id)]
    assert "operator_killed" in kinds
    assert "local_subprocess_killed" not in kinds


# ---------------------------------------------------------------------------
# spawn — a subscription-billed lineage cannot spawn an API-billed child (#578)
# ---------------------------------------------------------------------------

def _cli_root(store: SessionStore, routing: str = "claude_code"):
    """A root session on a CLI route — the shape the doctor bot runs as."""
    return store.create(
        task_id=f"{routing}_root_task",
        status=STATUS_RUNNING,
        routing=routing,
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 10.0},
    )


def _ctx_for(store: SessionStore, transcript: TranscriptStore, session):
    return InterAgentContext(
        session_store=store,
        transcript_store=transcript,
        caller_session_id=session.session_id,
        caps=Caps(),
    )


@pytest.mark.unit
@pytest.mark.parametrize("routing", ["claude_code", "codex"])
def test_spawn_managed_child_rejected_from_cli_root(store, transcript, routing):
    """model='claude' routes through Managed Agents — the Anthropic API — so a
    CLI lineage (billed to the operator's subscription) may not spawn one."""
    root = _cli_root(store, routing)

    result = dispatch(_ctx_for(store, transcript, root), "lifeos_agent_spawn", {
        "prompt": "do some background work", "model": "claude",
    })

    assert not result["ok"]
    assert result["error"] == "api_billing_blocked"
    assert store.list_sessions(parent_session_id=root.session_id) == []


@pytest.mark.unit
def test_spawn_managed_child_rejected_from_hermes_root(store, transcript):
    """Hermes (#640) is not a CLI routing — it never runs as a local
    subprocess this worker manages — but it's billed exactly like one for
    this purpose: an external, non-API-billed harness must not be able to
    open the model="claude" side door either. See
    `NON_API_BILLED_ROOT_ROUTINGS` in inter_agent.py."""
    root = _cli_root(store, "hermes")

    result = dispatch(_ctx_for(store, transcript, root), "lifeos_agent_spawn", {
        "prompt": "do some background work", "model": "claude",
    })

    assert not result["ok"]
    assert result["error"] == "api_billing_blocked"
    assert store.list_sessions(parent_session_id=root.session_id) == []


@pytest.mark.unit
def test_spawn_cli_child_allowed_from_hermes_root(store, transcript):
    """The route left open to a Hermes lineage, same as a CLI one: a
    subscription-billed CLI child, not the Anthropic API."""
    root = _cli_root(store, "hermes")

    result = dispatch(_ctx_for(store, transcript, root), "lifeos_agent_spawn", {
        "prompt": "do some background work", "model": "claude_code",
    })

    assert result["ok"], result
    assert store.get_by_session_id(result["child_session_id"]).routing == "claude_code"


@pytest.mark.unit
def test_hermes_routing_is_not_a_valid_spawn_model():
    """`NON_API_BILLED_ROOT_ROUTINGS` is deliberately a separate set from
    `CLI_ROUTINGS` / `SPAWN_MODELS` (#640) — adding Hermes to the spend
    guard must not also make "hermes" a valid `model=` value for spawn()
    (there's no executor that would ever dispatch such a child)."""
    assert "hermes" not in inter_agent.SPAWN_MODELS
    assert "hermes" not in inter_agent.CLI_ROUTINGS
    assert "hermes" in inter_agent.NON_API_BILLED_ROOT_ROUTINGS


@pytest.mark.unit
def test_spawn_managed_child_rejected_through_local_intermediary(store, transcript):
    """The check reads the ROOT's routing, so an intermediate child on another
    engine can't be used to launder the API-billed spawn."""
    root = _cli_root(store)
    middle = store.create(
        task_id="middle_task",
        status=STATUS_RUNNING,
        routing="local",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 10.0},
        parent_session_id=root.session_id,
        root_session_id=root.session_id,
        spawn_depth=1,
    )

    result = dispatch(_ctx_for(store, transcript, middle), "lifeos_agent_spawn", {
        "prompt": "do some background work", "model": "claude",
    })

    assert not result["ok"]
    assert result["error"] == "api_billing_blocked"


@pytest.mark.unit
def test_spawn_managed_child_still_allowed_from_managed_root(ctx, store, parent):
    """Unchanged for lineages that are API-billed by design: the block is about
    a *subscription* session opening an API side door, not about Managed Agents."""
    result = dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "do some background work", "model": "claude",
    })

    assert result["ok"]
    assert store.get_by_session_id(result["child_session_id"]).routing == "claude"


@pytest.mark.unit
def test_spawn_cli_child_with_tier_allowed_from_cli_root(store, transcript):
    """The route left open to a CLI lineage: another CLI child, on whichever
    tier suits the work — subscription-billed, so no API charge either way."""
    root = _cli_root(store)

    result = dispatch(_ctx_for(store, transcript, root), "lifeos_agent_spawn", {
        "prompt": "implement the fix", "model": "claude_code", "tier": "sonnet",
    })

    assert result["ok"]
    child = store.get_by_session_id(result["child_session_id"])
    assert child.routing == "claude_code"
    assert child.claude_code_model == "sonnet"
