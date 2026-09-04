"""Tests working-directory support on the local and remote
(#cloud) agent-worker routes, which both run through `LocalExecutor`.

Two layers are covered:
1. `local_executor._resolve_task_working_dir` — the task-level guard that
   runs before any LLM call: unset field, missing/non-directory path, and
   the worker's-own-checkout refusal.
2. `tools._resolve_within_base` (+ the Read/Write/Edit/Bash handlers) —
   the per-tool-call containment guard that resolves file paths and the
   Bash cwd against the named base, rejecting `..`/symlink escapes.

And end-to-end wiring through `LocalExecutor.execute()`, including the
"no directory named" case being byte-identical to before.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from api.services.agent_worker import local_executor as local_executor_module
from api.services.agent_worker.local_executor import (
    LocalExecutor,
    _resolve_task_working_dir,
)
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.tools import (
    STANDARD_HANDLERS,
    ToolRegistry,
    _resolve_within_base,
)
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes (mirrors test_agent_worker_local_executor.py's conventions)
# ---------------------------------------------------------------------------

@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class _FakeResponse:
    text: str = ""
    usage: _FakeUsage = None
    tool_calls: list = None
    model: str = "local"
    finish_reason: str = ""


class _ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        if not self._responses:
            raise AssertionError("LLM was called more times than scripted")
        return self._responses.pop(0)


class _FakeMCPServer:
    tools: list[dict] = []
    def _call_api(self, name, args): return {}
    def _format_response(self, name, data): return ""


@pytest.fixture
def fake_session(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(
        task_id="t1",
        routing="local",
        budget={"wall_seconds": 3600, "max_tokens": 5_000, "max_dollars": 5.0},
        expected_output="text",
    )
    session = store.get("t1")
    return store, session


def _make_executor(session_store, transcript_dir, llm):
    return LocalExecutor(
        session_store=session_store,
        transcript_store=TranscriptStore(transcripts_dir=transcript_dir),
        tool_registry=ToolRegistry(lifeos_mcp_server=_FakeMCPServer()),
        llm_client=llm,
        model_name="local",
    )


# ---------------------------------------------------------------------------
# _resolve_task_working_dir — the task-level guard
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_working_dir_field_is_unset_not_an_error():
    resolved, error = _resolve_task_working_dir({"description": "do a thing"})
    assert resolved is None
    assert error is None


@pytest.mark.unit
def test_blank_working_dir_field_is_unset_not_an_error():
    resolved, error = _resolve_task_working_dir(
        {"description": "x", "fields": {"working_dir": "   "}}
    )
    assert resolved is None
    assert error is None


@pytest.mark.unit
def test_existing_directory_resolves(tmp_path: Path):
    resolved, error = _resolve_task_working_dir(
        {"fields": {"working_dir": str(tmp_path)}}
    )
    assert error is None
    assert resolved == str(tmp_path.resolve())


@pytest.mark.unit
def test_nonexistent_directory_is_refused_naming_the_path(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    resolved, error = _resolve_task_working_dir(
        {"fields": {"working_dir": str(missing)}}
    )
    assert resolved is None
    assert str(missing) in error
    assert "does not exist" in error


@pytest.mark.unit
def test_file_path_is_refused_as_not_a_directory(tmp_path: Path):
    f = tmp_path / "not-a-dir.txt"
    f.write_text("hi")
    resolved, error = _resolve_task_working_dir({"fields": {"working_dir": str(f)}})
    assert resolved is None
    assert str(f) in error
    assert "not a directory" in error


@pytest.mark.unit
def test_worker_own_checkout_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(local_executor_module, "_worker_repo_root", lambda: tmp_path)
    resolved, error = _resolve_task_working_dir({"fields": {"working_dir": str(tmp_path)}})
    assert resolved is None
    assert "worker's own checkout" in error


@pytest.mark.unit
def test_subdirectory_of_worker_own_checkout_is_also_refused(tmp_path: Path, monkeypatch):
    """The live service's tree can't be the target even one level down —
    naming a subdirectory of the checkout is still naming the checkout."""
    monkeypatch.setattr(local_executor_module, "_worker_repo_root", lambda: tmp_path)
    sub = tmp_path / "api" / "services"
    sub.mkdir(parents=True)
    resolved, error = _resolve_task_working_dir({"fields": {"working_dir": str(sub)}})
    assert resolved is None
    assert "worker's own checkout" in error


@pytest.mark.unit
def test_immediate_parent_of_worker_checkout_is_refused(tmp_path: Path, monkeypatch):
    """A named directory one level up from the checkout isn't itself the
    checkout, but it *contains* it — `tools._resolve_within_base` would
    approve any path under it, including the checkout's own files, so
    this must be refused just as directly as naming the checkout itself."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(local_executor_module, "_worker_repo_root", lambda: checkout)
    resolved, error = _resolve_task_working_dir({"fields": {"working_dir": str(tmp_path)}})
    assert resolved is None
    assert "would contain the worker's own checkout" in error


@pytest.mark.unit
def test_far_ancestor_of_worker_checkout_is_refused(tmp_path: Path, monkeypatch):
    """Not just the immediate parent — any ancestor that would contain
    the checkout is refused, however many levels up it sits."""
    far_ancestor = tmp_path
    checkout = far_ancestor / "a" / "b" / "c" / "checkout"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(local_executor_module, "_worker_repo_root", lambda: checkout)
    resolved, error = _resolve_task_working_dir({"fields": {"working_dir": str(far_ancestor)}})
    assert resolved is None
    assert "would contain the worker's own checkout" in error


@pytest.mark.unit
def test_sibling_directory_of_worker_checkout_is_allowed(tmp_path: Path, monkeypatch):
    """Only the checkout itself (or something inside it) is refused — a
    directory that merely shares a parent is a legitimate target."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    sibling = tmp_path / "scratch-worktree"
    sibling.mkdir()
    monkeypatch.setattr(local_executor_module, "_worker_repo_root", lambda: checkout)
    resolved, error = _resolve_task_working_dir({"fields": {"working_dir": str(sibling)}})
    assert error is None
    assert resolved == str(sibling.resolve())


@pytest.mark.unit
def test_symlink_to_worker_checkout_is_refused(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(local_executor_module, "_worker_repo_root", lambda: checkout)
    link = tmp_path / "link-to-checkout"
    link.symlink_to(checkout, target_is_directory=True)
    resolved, error = _resolve_task_working_dir({"fields": {"working_dir": str(link)}})
    assert resolved is None
    assert "worker's own checkout" in error


# ---------------------------------------------------------------------------
# tools._resolve_within_base — per-tool-call escape guard
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_resolve_within_base_none_is_a_no_op():
    p, error = _resolve_within_base("relative/path.txt", None)
    assert error is None
    assert p == Path("relative/path.txt")


@pytest.mark.unit
def test_resolve_within_base_joins_relative_path(tmp_path: Path):
    p, error = _resolve_within_base("sub/file.txt", str(tmp_path))
    assert error is None
    assert p == (tmp_path / "sub" / "file.txt").resolve()


@pytest.mark.unit
def test_resolve_within_base_allows_absolute_path_inside_base(tmp_path: Path):
    target = tmp_path / "file.txt"
    p, error = _resolve_within_base(str(target), str(tmp_path))
    assert error is None
    assert p == target.resolve()


@pytest.mark.unit
def test_resolve_within_base_rejects_absolute_path_outside_base(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside" / "file.txt"
    p, error = _resolve_within_base(str(outside), str(base))
    assert p is None
    assert "escapes working directory" in error


@pytest.mark.unit
def test_resolve_within_base_rejects_dotdot_traversal(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    p, error = _resolve_within_base("../outside.txt", str(base))
    assert p is None
    assert "escapes working directory" in error


@pytest.mark.unit
def test_resolve_within_base_rejects_symlink_escape(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    outside_target = tmp_path / "secret.txt"
    outside_target.write_text("shh")
    link = base / "escape-link"
    link.symlink_to(outside_target)
    p, error = _resolve_within_base(str(link), str(base))
    assert p is None
    assert "escapes working directory" in error


# ---------------------------------------------------------------------------
# Read/Write/Edit/Bash handlers honor base_dir
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_read_resolves_relative_path_against_base_dir(tmp_path: Path):
    (tmp_path / "hi.txt").write_text("hello")
    r = STANDARD_HANDLERS["Read"]({"file_path": "hi.txt"}, base_dir=str(tmp_path))
    assert not r.is_error
    assert r.output == "hello"


@pytest.mark.unit
def test_read_rejects_escape_via_base_dir(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    (tmp_path / "outside.txt").write_text("nope")
    r = STANDARD_HANDLERS["Read"]({"file_path": "../outside.txt"}, base_dir=str(base))
    assert r.is_error
    assert "escapes working directory" in r.output


@pytest.mark.unit
def test_write_confines_relative_path_to_base_dir(tmp_path: Path):
    r = STANDARD_HANDLERS["Write"](
        {"file_path": "out.txt", "content": "bye"}, base_dir=str(tmp_path)
    )
    assert not r.is_error
    assert (tmp_path / "out.txt").read_text() == "bye"


@pytest.mark.unit
def test_write_rejects_escape_via_base_dir(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    r = STANDARD_HANDLERS["Write"](
        {"file_path": "../escape.txt", "content": "x"}, base_dir=str(base)
    )
    assert r.is_error
    assert "escapes working directory" in r.output
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.unit
def test_edit_rejects_escape_via_base_dir(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("alpha")
    r = STANDARD_HANDLERS["Edit"](
        {"file_path": "../outside.txt", "old_string": "alpha", "new_string": "BETA"},
        base_dir=str(base),
    )
    assert r.is_error
    assert "escapes working directory" in r.output
    assert outside.read_text() == "alpha"


@pytest.mark.unit
def test_bash_runs_with_base_dir_as_cwd(tmp_path: Path):
    r = STANDARD_HANDLERS["Bash"]({"command": "pwd"}, base_dir=str(tmp_path))
    assert not r.is_error
    assert str(tmp_path.resolve()) in r.output or str(tmp_path) in r.output


@pytest.mark.unit
def test_bash_without_base_dir_is_unchanged():
    r = STANDARD_HANDLERS["Bash"]({"command": "echo hi"}, base_dir=None)
    assert not r.is_error
    assert "hi" in r.output


@pytest.mark.unit
def test_registry_dispatch_forwards_base_dir(tmp_path: Path):
    (tmp_path / "f.txt").write_text("content")
    reg = ToolRegistry(lifeos_mcp_server=_FakeMCPServer())
    r = reg.dispatch("Read", {"file_path": "f.txt"}, base_dir=str(tmp_path))
    assert not r.is_error
    assert r.output == "content"


# ---------------------------------------------------------------------------
# End-to-end via LocalExecutor.execute()
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_execute_no_working_dir_named_is_unchanged(tmp_path: Path, fake_session):
    """AC2 — behavior with no working directory named is byte-identical:
    the tool registry is still called with exactly the two positional
    args it always was (no base_dir kwarg). If the executor started
    always forwarding `base_dir` this test double — which only accepts
    `(name, args)`, mirroring a caller that never supplies one — would
    raise TypeError."""
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(
            text="", usage=_FakeUsage(20, 10),
            tool_calls=[{"id": "c1", "name": "Bash", "input": {"command": "echo hi"}}],
        ),
        _FakeResponse(text="done", usage=_FakeUsage(10, 5)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)

    calls = []
    original_dispatch = executor.tools.dispatch
    def _tracking_dispatch(name, args):  # only 2 positional params on purpose
        calls.append((name, args))
        return original_dispatch(name, args)
    executor.tools.dispatch = _tracking_dispatch

    outcome = executor.execute(session, {"id": "t1", "description": "no dir here"})
    assert outcome.status == STATUS_COMPLETED
    assert calls == [("Bash", {"command": "echo hi"})]


@pytest.mark.unit
def test_execute_uses_named_working_dir_for_tool_calls(tmp_path: Path, fake_session):
    """AC1 — a named, valid working directory is what file/shell tool
    calls resolve against."""
    store, session = fake_session
    workdir = tmp_path / "scratch"
    workdir.mkdir()
    llm = _ScriptedLLM([
        _FakeResponse(
            text="",
            usage=_FakeUsage(20, 10),
            tool_calls=[{"id": "c1", "name": "Write",
                         "input": {"file_path": "out.txt", "content": "hello"}}],
        ),
        _FakeResponse(text="wrote it", usage=_FakeUsage(10, 5)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(
        session,
        {"id": "t1", "description": "write a file", "fields": {"working_dir": str(workdir)}},
    )
    assert outcome.status == STATUS_COMPLETED
    assert (workdir / "out.txt").read_text() == "hello"


@pytest.mark.unit
def test_execute_refuses_nonexistent_working_dir_without_calling_llm(tmp_path: Path, fake_session):
    """AC3 — a nonexistent named directory fails the task and never calls
    the model."""
    store, session = fake_session
    missing = tmp_path / "does-not-exist"
    llm = _ScriptedLLM([_FakeResponse(text="should never run")])
    executor = _make_executor(store, tmp_path / "transcripts", llm)

    outcome = executor.execute(
        session,
        {"id": "t1", "description": "x", "fields": {"working_dir": str(missing)}},
    )
    assert outcome.status == STATUS_FAILED
    assert str(missing) in outcome.reason
    assert llm.calls == []


@pytest.mark.unit
def test_execute_refuses_worker_own_checkout_without_calling_llm(tmp_path: Path, fake_session, monkeypatch):
    """AC4 — a named directory that resolves to the worker's own checkout
    is refused, not executed."""
    monkeypatch.setattr(local_executor_module, "_worker_repo_root", lambda: tmp_path)
    store, session = fake_session
    llm = _ScriptedLLM([_FakeResponse(text="should never run")])
    executor = _make_executor(store, tmp_path / "transcripts", llm)

    outcome = executor.execute(
        session,
        {"id": "t1", "description": "x", "fields": {"working_dir": str(tmp_path)}},
    )
    assert outcome.status == STATUS_FAILED
    assert "worker's own checkout" in outcome.reason
    assert llm.calls == []


@pytest.mark.unit
def test_execute_failure_reason_reaches_session_store_as_failed(tmp_path: Path, fake_session):
    """AC6 — the failure reaches the same STATUS_FAILED channel every
    other executor failure uses (worker._handle_outcome / _mark_failed
    both key off session status + ExecutorOutcome.reason)."""
    store, session = fake_session
    missing = tmp_path / "nope"
    llm = _ScriptedLLM([])
    executor = _make_executor(store, tmp_path / "transcripts", llm)

    outcome = executor.execute(
        session,
        {"id": "t1", "description": "x", "fields": {"working_dir": str(missing)}},
    )
    assert outcome.status == STATUS_FAILED
    refreshed = store.get("t1")
    assert refreshed.status == STATUS_FAILED
