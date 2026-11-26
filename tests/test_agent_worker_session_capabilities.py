"""A child agent session never has more tools or privileges than its creator.

Covers the capability set per route, the intersection through spawn chains
and agent-created tasks, the Claude Code and Codex argv each parent -> child
combination produces, the restricted MCP config file, the local route's
limited tool set, the spawn and dispatch refusals, and the task stamps that
name an agent creator. All data is synthetic; no CLI is launched.
"""
from __future__ import annotations

import functools
import json
import os
import stat
import tempfile
import types
from pathlib import Path

import pytest

from api.services.agent_worker import session_capabilities as caps_mod
from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
from api.services.agent_worker.codex_executor import CodexExecutor
from api.services.agent_worker.inter_agent import Caps, InterAgentContext, dispatch
from api.services.agent_worker.session_capabilities import (
    AGENT_CREATOR_FIELD,
    PROJECT_CHILD_CREATOR_FIELD,
    UNATTESTED_CREATOR,
    UNRESTRICTED,
    CapabilityLookupError,
    SessionCapabilities,
    capability_context,
    host_shell_refusal,
    resolve_lineage,
    route_capabilities,
    write_strict_mcp_config,
)
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from config.settings import settings


pytestmark = pytest.mark.unit

FAKE_SECRET = "synthetic-bearer-0000"
LIFEOS = "lifeos"


# ---------------------------------------------------------------------------
# Fixtures: a synthetic home with Claude and Codex MCP configs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _synthetic_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "lifeos": {"type": "stdio", "command": "/opt/example/python", "args": ["/opt/example/mcp_server.py"]},
        "keymaster": {"type": "http", "url": "http://keys.example.test/mcp",
                      "headers": {"Authorization": f"Bearer {FAKE_SECRET}"}},
        "playwright": {"type": "stdio", "command": "/opt/example/browser-mcp", "args": []},
    }}))
    codex_home = home / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.lifeos]\ncommand = "/opt/example/python"\nargs = ["/opt/example/mcp_server.py"]\n'
        '[mcp_servers.playwright]\ncommand = "/opt/example/browser-mcp"\nargs = []\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    scratch_root = tmp_path / "tmp"
    scratch_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_root))
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "sessions.db")


@pytest.fixture
def transcripts(tmp_path) -> TranscriptStore:
    return TranscriptStore(transcripts_dir=tmp_path / "transcripts")


def _root(store, routing, *, task_id=None, origin="operator"):
    return store.create(
        task_id=task_id or f"root_{routing}", status=STATUS_RUNNING, routing=routing,
        origin=origin, budget={"wall_seconds": 3600, "max_dollars": 5.0},
    )


def _child(store, parent, routing, *, task_id=None):
    return store.create(
        task_id=task_id or f"child_{routing}_of_{parent.task_id}", status=STATUS_RUNNING,
        routing=routing, parent_session_id=parent.session_id,
        root_session_id=parent.root_session_id or parent.session_id,
        spawn_depth=(parent.spawn_depth or 0) + 1,
    )


# ---------------------------------------------------------------------------
# The capability set and its intersection
# ---------------------------------------------------------------------------

def test_route_capabilities_match_what_each_route_is_granted():
    assert route_capabilities("claude_code") == UNRESTRICTED
    assert route_capabilities("codex") == SessionCapabilities(True, False, frozenset({LIFEOS, "playwright"}))
    for route in ("local", "remote", "hermes"):
        assert route_capabilities(route) == SessionCapabilities(True, False, frozenset({LIFEOS}))
    assert route_capabilities("claude") == SessionCapabilities(False, False, frozenset({LIFEOS}))
    assert route_capabilities("ask") == SessionCapabilities(False, False, frozenset())


def test_intersection_keeps_only_what_both_have():
    a = SessionCapabilities(True, True, frozenset({LIFEOS, "keymaster"}))
    b = SessionCapabilities(True, False, frozenset({LIFEOS, "playwright"}))
    assert a.intersect(b) == SessionCapabilities(True, False, frozenset({LIFEOS}))
    assert UNRESTRICTED.intersect(b) == b
    assert b.intersect(UNRESTRICTED) == b
    assert SessionCapabilities(False, True, None).intersect(UNRESTRICTED).host_shell is False


def test_may_spawn_follows_the_lifeos_server():
    assert UNRESTRICTED.may_spawn
    assert SessionCapabilities(True, False, frozenset({LIFEOS})).may_spawn
    assert not SessionCapabilities(True, False, frozenset({"playwright"})).may_spawn


# ---------------------------------------------------------------------------
# Lineage resolution
# ---------------------------------------------------------------------------

def test_top_level_session_has_no_ceiling(store):
    root = _root(store, "claude_code")
    lineage = resolve_lineage(store, root)
    assert lineage.ceiling is None
    assert lineage.depth == 0
    assert lineage.effective("claude_code") == UNRESTRICTED


def test_ceiling_intersects_every_creator_on_the_chain(store):
    root = _root(store, "claude_code")
    local_child = _child(store, root, "local")
    grandchild = _child(store, local_child, "claude_code")
    lineage = resolve_lineage(store, grandchild)
    assert lineage.ceiling == route_capabilities("local")
    assert lineage.depth == 2


def test_managed_ancestor_removes_the_host_shell_for_the_whole_chain(store):
    root = _root(store, "claude")
    local_child = _child(store, root, "local")
    grandchild = _child(store, local_child, "claude_code")
    assert resolve_lineage(store, grandchild).ceiling.host_shell is False


def test_task_backed_session_inherits_from_the_agent_that_created_its_task(store):
    creator = _root(store, "hermes")
    claimed = store.create(task_id="abc12345", status=STATUS_RUNNING, routing="claude_code")
    fields = {"abc12345": {AGENT_CREATOR_FIELD: creator.session_id}}
    lineage = resolve_lineage(store, claimed, task_fields=fields.get)
    assert lineage.ceiling == route_capabilities("hermes")
    assert lineage.depth == 1


def test_project_child_stamp_names_the_creator_too(store):
    creator = _root(store, "local")
    claimed = store.create(task_id="def12345", status=STATUS_RUNNING, routing="claude_code")
    lineage = resolve_lineage(
        store, claimed, own_fields={PROJECT_CHILD_CREATOR_FIELD: creator.session_id},
    )
    assert lineage.ceiling == route_capabilities("local")


def test_unattested_or_missing_creator_is_treated_as_managed(store):
    for value in (UNATTESTED_CREATOR, "sess_doesnotexist"):
        claimed = store.create(task_id=f"t_{value[:8]}", status=STATUS_RUNNING, routing="claude_code")
        lineage = resolve_lineage(store, claimed, own_fields={AGENT_CREATOR_FIELD: value})
        assert lineage.ceiling == route_capabilities("claude")
        assert lineage.depth == 1


def test_operator_task_has_no_ceiling(store):
    claimed = store.create(task_id="feed1234", status=STATUS_RUNNING, routing="claude_code")
    assert resolve_lineage(store, claimed, task_fields=lambda _tid: {}).ceiling is None


def test_operator_and_hermes_roots_ignore_task_stamps(store):
    lookups: list[str] = []

    def lookup(task_id):
        lookups.append(task_id)
        return {AGENT_CREATOR_FIELD: UNATTESTED_CREATOR}

    for origin in ("operator", "hermes"):
        root = _root(store, "claude_code", task_id=f"r_{origin}", origin=origin)
        assert resolve_lineage(store, root, task_fields=lookup).ceiling is None
    assert lookups == []


def test_lookup_failure_propagates(store):
    claimed = store.create(task_id="0badf00d", status=STATUS_RUNNING, routing="claude_code")

    def failing(_task_id):
        raise CapabilityLookupError("down")

    with pytest.raises(CapabilityLookupError):
        resolve_lineage(store, claimed, task_fields=failing)


def test_creator_cycle_resolves_to_no_capabilities(store):
    a = store.create(task_id="aaaa0001", status=STATUS_RUNNING, routing="claude_code")
    b = store.create(task_id="bbbb0001", status=STATUS_RUNNING, routing="claude_code")
    fields = {
        "aaaa0001": {AGENT_CREATOR_FIELD: b.session_id},
        "bbbb0001": {AGENT_CREATOR_FIELD: a.session_id},
    }
    lineage = resolve_lineage(store, a, task_fields=fields.get)
    assert lineage.ceiling == SessionCapabilities(False, False, frozenset())
    assert lineage.depth > settings.agent_max_spawn_depth


@pytest.mark.parametrize("routing,refused", [
    ("claude_code", True), ("codex", True), ("hermes", True),
    ("local", False), ("remote", False), ("claude", False),
])
def test_host_shell_refusal_without_a_host_shell(routing, refused):
    ceiling = route_capabilities("claude")
    assert (host_shell_refusal(routing, ceiling) is not None) is refused
    assert host_shell_refusal(routing, None) is None
    assert host_shell_refusal(routing, route_capabilities("local")) is None


# ---------------------------------------------------------------------------
# Restricted MCP config
# ---------------------------------------------------------------------------

def test_strict_mcp_config_lists_only_allowed_servers_with_mode_600(tmp_path):
    dest = tmp_path / "private"
    path, listed = write_strict_mcp_config(frozenset({LIFEOS, "not-configured"}), dest)
    assert listed == [LIFEOS]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert set(json.loads(path.read_text())["mcpServers"]) == {LIFEOS}
    # Rewriting an existing file keeps it private.
    os.chmod(path, 0o644)
    write_strict_mcp_config(frozenset({"keymaster"}), dest)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert set(json.loads(path.read_text())["mcpServers"]) == {"keymaster"}


# ---------------------------------------------------------------------------
# Claude Code argv per parent -> child combination
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self):
        lines = [
            {"type": "system", "subtype": "init", "session_id": "cli-1"},
            {"type": "result", "session_id": "cli-1", "total_cost_usd": 0.0, "result": "ok"},
        ]
        self._lines = [json.dumps(item) + "\n" for item in lines]
        self.stdout = self
        self.stderr = types.SimpleNamespace(read=lambda: "")
        self.returncode = 0
        self.pid = 4242

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def __iter__(self):
        return iter(self.readline, "")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _claude_executor(store, transcripts, captured):
    def spawn(cmd, **kwargs):
        captured.append(cmd)
        return _Proc()

    return ClaudeCodeExecutor(
        session_store=store, transcript_store=transcripts, spawn_fn=spawn,
        binary_resolver=lambda: "/usr/bin/claude-example", timeout_seconds=30,
        heartbeat_interval=3600,
    )


def _run_claude(store, transcripts, session):
    captured: list = []
    executor = _claude_executor(store, transcripts, captured)
    outcome = executor.execute(session, {"description": "do the synthetic thing"})
    return outcome, (captured[0] if captured else None)


def test_top_level_claude_code_spawn_event_records_no_capabilities(store, transcripts):
    session = store.create(task_id="op-1", routing="claude_code", origin="operator")
    outcome, cmd = _run_claude(store, transcripts, session)
    assert outcome.status == STATUS_COMPLETED
    spawn_event = [e for e in transcripts.read(session.session_id) if e["kind"] == "claude_code_spawn"][0]
    assert "capabilities" not in spawn_event["payload"]


@pytest.mark.parametrize("parent_origin", ["operator", None])
def test_claude_code_delegation_through_spawn_keeps_full_tools(store, transcripts, parent_origin):
    """Claude Code parent -> lifeos_agent_spawn -> worker gate -> executor."""
    parent = _root(store, "claude_code", task_id=f"cc_parent_{parent_origin}", origin=parent_origin)
    result = dispatch(_ctx(store, transcripts, parent, task_fields=lambda _tid: {}), "lifeos_agent_spawn", {
        "prompt": "open the dashboard and screenshot it", "model": "claude_code", "tier": "sonnet",
    })
    assert result["ok"], result
    child = store.get_by_session_id(result["child_session_id"])
    assert child.parent_session_id == parent.session_id and child.routing == "claude_code"
    stub, _ = _worker_stub(store, transcripts, {})
    capabilities, refusal = stub._resolve_capabilities(child)
    assert refusal is None and capabilities == UNRESTRICTED
    with capability_context(capabilities):
        outcome, cmd = _run_claude(store, transcripts, child)
    assert outcome.status == STATUS_COMPLETED
    assert "--chrome" in cmd
    assert "--no-chrome" not in cmd and "--strict-mcp-config" not in cmd


def _mcp_config_of(cmd) -> dict:
    assert "--strict-mcp-config" in cmd
    path = Path(cmd[cmd.index("--mcp-config") + 1])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.parent.name.startswith("sess-")
    return json.loads(path.read_text())["mcpServers"]


@pytest.mark.parametrize("parent_route", ["local", "remote", "hermes"])
def test_claude_code_child_of_a_lifeos_only_parent(store, transcripts, parent_route):
    parent = _root(store, parent_route)
    child = _child(store, parent, "claude_code")
    outcome, cmd = _run_claude(store, transcripts, child)
    assert outcome.status == STATUS_COMPLETED
    assert "--no-chrome" in cmd and "--chrome" not in cmd
    assert set(_mcp_config_of(cmd)) == {LIFEOS}
    assert "no browser" in cmd[cmd.index("--append-system-prompt") + 1]
    event = [e for e in transcripts.read(child.session_id) if e["kind"] == "claude_code_spawn"][0]
    assert event["payload"]["capabilities"] == {"chrome": False, "mcp_servers": [LIFEOS]}
    assert FAKE_SECRET not in json.dumps(transcripts.read(child.session_id))


def test_claude_code_child_of_codex_gets_codex_servers_without_chrome(store, transcripts):
    parent = _root(store, "codex")
    child = _child(store, parent, "claude_code")
    _, cmd = _run_claude(store, transcripts, child)
    assert "--no-chrome" in cmd
    assert set(_mcp_config_of(cmd)) == {LIFEOS, "playwright"}


@pytest.mark.parametrize("routing", ["claude_code", "codex"])
def test_cli_child_of_managed_parent_is_refused_without_spawning(store, transcripts, routing):
    parent = _root(store, "claude")
    child = _child(store, parent, routing)
    captured: list = []
    if routing == "claude_code":
        executor = _claude_executor(store, transcripts, captured)
    else:
        executor = _codex_executor(store, transcripts, captured)
    outcome = executor.execute(child, {"description": "x", "working_dir": str(Path.cwd())})
    assert outcome.status == STATUS_FAILED
    assert captured == []


def test_restricted_claude_code_child_on_a_remote_host_is_refused(store, transcripts, monkeypatch):
    from api.services.agent_worker import claude_code_executor as cce

    monkeypatch.setattr(cce, "resolve_host_target", lambda host, api_host: object())
    parent = _root(store, "local")
    child = _child(store, parent, "claude_code")
    outcome, cmd = _run_claude(store, transcripts, child)
    assert outcome.status == STATUS_FAILED
    assert cmd is None


def test_worker_resolved_set_wins_over_the_spawn_chain(store, transcripts):
    session = store.create(task_id="op-2", routing="claude_code", origin="operator")
    with capability_context(route_capabilities("local")):
        _, cmd = _run_claude(store, transcripts, session)
    assert "--no-chrome" in cmd and "--strict-mcp-config" in cmd


# ---------------------------------------------------------------------------
# Codex argv
# ---------------------------------------------------------------------------

def _codex_executor(store, transcripts, captured):
    def spawn(cmd, **kwargs):
        captured.append(cmd)
        proc = _Proc()
        proc._lines = [
            json.dumps({"type": "thread.started", "thread_id": "th-1"}) + "\n",
            json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
        ]
        return proc

    return CodexExecutor(
        session_store=store, transcript_store=transcripts,
        notification_callback=lambda _m: None, spawn_fn=spawn,
        binary_resolver=lambda: "/usr/bin/codex-example", heartbeat_interval=9999,
    )


def _codex_disabled(cmd) -> set[str]:
    return {
        cmd[i + 1].split(".")[1] for i, token in enumerate(cmd)
        if token == "-c" and cmd[i + 1].endswith(".enabled=false")
    }


def test_top_level_codex_disables_nothing(store, transcripts):
    session = store.create(task_id="cx-1", routing="codex", origin="operator")
    captured: list = []
    _codex_executor(store, transcripts, captured).execute(
        session, {"description": "x", "working_dir": str(Path.cwd())},
    )
    assert _codex_disabled(captured[0]) == set()


@pytest.mark.parametrize("parent_route,disabled", [
    ("local", {"playwright"}), ("hermes", {"playwright"}),
    ("claude_code", set()), ("codex", set()),
])
def test_codex_child_disables_servers_its_parent_lacks(store, transcripts, parent_route, disabled):
    parent = _root(store, parent_route)
    child = _child(store, parent, "codex")
    captured: list = []
    _codex_executor(store, transcripts, captured).execute(
        child, {"description": "x", "working_dir": str(Path.cwd())},
    )
    assert _codex_disabled(captured[0]) == disabled


# ---------------------------------------------------------------------------
# Local route runs limited instead of being refused
# ---------------------------------------------------------------------------

def test_local_tools_drop_host_tools_without_a_host_shell():
    from api.services.agent_worker.tools import ToolRegistry

    registry = ToolRegistry(lifeos_mcp_server=types.SimpleNamespace(tools=[]))
    host_tools = {"Read", "Write", "Edit", "Bash", "WebFetch", "WebSearch"}
    assert host_tools <= {t["name"] for t in registry.definitions()}
    limited = route_capabilities("local").intersect(route_capabilities("claude"))
    with capability_context(limited):
        names = {t["name"] for t in registry.definitions()}
        assert not (host_tools & names)
        assert "sleep" in names
        result = registry.dispatch("Bash", {"command": "echo hi"})
        assert result.is_error and "no host shell" in result.output
    with capability_context(route_capabilities("local")):
        assert host_tools <= {t["name"] for t in registry.definitions()}


# ---------------------------------------------------------------------------
# lifeos_agent_spawn
# ---------------------------------------------------------------------------

def _ctx(store, transcripts, caller, task_fields=None):
    return InterAgentContext(
        session_store=store, transcript_store=transcripts,
        caller_session_id=caller.session_id, caps=Caps(), task_fields=task_fields,
    )


@pytest.mark.parametrize("model,allowed", [
    ("claude_code", False), ("codex", False), ("hermes", False),
    ("local", True), ("claude", True),
])
def test_managed_caller_spawns_only_routes_within_its_capabilities(store, transcripts, model, allowed):
    caller = _root(store, "claude", origin=None)
    result = dispatch(_ctx(store, transcripts, caller), "lifeos_agent_spawn", {"prompt": "p", "model": model})
    assert result["ok"] is allowed
    if not allowed:
        assert result["error"] == "capability_exceeds_parent"


def test_spawn_reads_the_callers_task_creator(store, transcripts):
    creator = _root(store, "claude", task_id="mgr_task")
    caller = store.create(
        task_id="cafe0001", status=STATUS_RUNNING, routing="local",
        budget={"wall_seconds": 3600, "max_dollars": 5.0},
    )
    fields = {"cafe0001": {AGENT_CREATOR_FIELD: creator.session_id}}
    result = dispatch(
        _ctx(store, transcripts, caller, fields.get), "lifeos_agent_spawn",
        {"prompt": "p", "model": "claude_code"},
    )
    assert result["error"] == "capability_exceeds_parent"


def test_spawn_depth_counts_task_created_hops(store, transcripts):
    previous = _root(store, "claude_code", task_id="t0")
    fields: dict = {}
    for i in range(1, settings.agent_max_spawn_depth + 1):
        task_id = f"deadbe{i:02d}"
        fields[task_id] = {AGENT_CREATOR_FIELD: previous.session_id}
        previous = store.create(
            task_id=task_id, status=STATUS_RUNNING, routing="claude_code",
            budget={"wall_seconds": 3600, "max_dollars": 5.0},
        )
    result = dispatch(
        _ctx(store, transcripts, previous, fields.get), "lifeos_agent_spawn",
        {"prompt": "p", "model": "claude_code"},
    )
    assert result["error"] == "cap_spawn_depth"


def test_spawn_refuses_when_the_creator_cannot_be_read(store, transcripts):
    caller = store.create(task_id="0bad0001", status=STATUS_RUNNING, routing="local",
                          budget={"wall_seconds": 3600, "max_dollars": 5.0})

    def failing(_task_id):
        raise CapabilityLookupError("down")

    result = dispatch(_ctx(store, transcripts, caller, failing), "lifeos_agent_spawn",
                      {"prompt": "p", "model": "local"})
    assert result["error"] == "capability_unverified"


# ---------------------------------------------------------------------------
# Worker dispatch gate
# ---------------------------------------------------------------------------

def _worker_stub(store, transcripts, fields: dict):
    from api.services.agent_worker.worker import Worker

    stub = types.SimpleNamespace(session_store=store, transcript_store=transcripts)
    stub._capability_task_fields = fields.get
    stub._resolve_capabilities = functools.partial(Worker._resolve_capabilities, stub)
    stub._capability_refused = functools.partial(Worker._capability_refused, stub)
    return stub, Worker


def test_worker_refuses_a_cli_task_created_by_a_managed_session(store, transcripts):
    creator = _root(store, "claude", task_id="mgr2")
    claimed = store.create(task_id="beef0001", status=STATUS_RUNNING, routing="claude_code")
    stub, worker_cls = _worker_stub(store, transcripts, {"beef0001": {AGENT_CREATOR_FIELD: creator.session_id}})
    started: list = []
    stub._lifecycle_adapter = lambda _s: types.SimpleNamespace(start=lambda *a: started.append(a))
    outcome = worker_cls._execute_start(stub, claimed, {"id": "beef0001", "description": "x"})
    assert outcome.status == STATUS_FAILED
    assert started == []
    assert any(e["kind"] == "capability_refused" for e in transcripts.read(claimed.session_id))


def test_worker_resolves_capabilities_for_each_kind_of_session(store, transcripts):
    hermes = _root(store, "hermes", task_id="h1")
    claimed = store.create(task_id="beef0002", status=STATUS_RUNNING, routing="claude_code")
    operator_task = store.create(task_id="beef0003", status=STATUS_RUNNING, routing="claude_code")
    stub, _ = _worker_stub(store, transcripts, {
        "beef0002": {AGENT_CREATOR_FIELD: hermes.session_id},
        "beef0003": {},
    })
    capabilities, refusal = stub._resolve_capabilities(claimed)
    assert refusal is None and capabilities == route_capabilities("hermes")
    capabilities, refusal = stub._resolve_capabilities(operator_task)
    assert refusal is None and capabilities == UNRESTRICTED
    # The request's own fields are used without a lookup.
    capabilities, _ = stub._resolve_capabilities(
        operator_task, {"id": "beef0003", "fields": {AGENT_CREATOR_FIELD: hermes.session_id}},
    )
    assert capabilities == route_capabilities("hermes")


def test_worker_refuses_a_task_chain_over_the_depth_cap(store, transcripts):
    previous = _root(store, "claude_code", task_id="d0")
    fields: dict = {}
    for i in range(1, settings.agent_max_spawn_depth + 2):
        task_id = f"dddd00{i:02d}"
        fields[task_id] = {AGENT_CREATOR_FIELD: previous.session_id}
        previous = store.create(task_id=task_id, status=STATUS_RUNNING, routing="claude_code")
    stub, _ = _worker_stub(store, transcripts, fields)
    _, refusal = stub._resolve_capabilities(previous)
    assert refusal and "spawn depth cap" in refusal


def test_worker_task_lookup_reports_a_failed_read():
    from api.services.agent_worker.worker import _TASK_FETCH_FAILED, Worker

    stub = types.SimpleNamespace(_fetch_task=lambda _tid, distinguish_failure=False: _TASK_FETCH_FAILED)
    with pytest.raises(CapabilityLookupError):
        Worker._capability_task_fields(stub, "t1")
    stub._fetch_task = lambda _tid, distinguish_failure=False: None
    assert Worker._capability_task_fields(stub, "t1") is None
    stub._fetch_task = lambda _tid, distinguish_failure=False: {"id": "t1", "fields": {"a": "b"}}
    assert Worker._capability_task_fields(stub, "t1") == {"a": "b"}


# ---------------------------------------------------------------------------
# Task stamps
# ---------------------------------------------------------------------------

def test_field_names_match_the_task_store():
    from api.services.task_projects import AGENT_CREATOR_SESSION_FIELD, CHILD_CREATOR_SESSION_FIELD

    assert caps_mod.AGENT_CREATOR_FIELD == AGENT_CREATOR_SESSION_FIELD
    assert caps_mod.PROJECT_CHILD_CREATOR_FIELD == CHILD_CREATOR_SESSION_FIELD


@pytest.fixture
def task_manager(tmp_path):
    from api.services.task_manager import TaskManager

    vault = tmp_path / "vault"
    (vault / "LifeOS" / "Tasks").mkdir(parents=True)
    return TaskManager(vault_path=str(vault), index_path=str(tmp_path / "index.json"))


def test_agent_create_is_stamped_with_its_creator(task_manager):
    task = task_manager.create(description="Synthetic follow up", tags=["claude"],
                               _agent_creator_session="sess_aaaa")
    assert task.fields[AGENT_CREATOR_FIELD] == "sess_aaaa"
    plain = task_manager.create(description="Operator task", tags=["claude"])
    assert AGENT_CREATOR_FIELD not in plain.fields


def test_creator_stamp_cannot_be_set_through_fields(task_manager):
    from api.services.task_projects import ProjectConflictError

    with pytest.raises(ProjectConflictError):
        task_manager.create(description="Forged", fields={AGENT_CREATOR_FIELD: "sess_x"})
    task = task_manager.create(description="Existing")
    with pytest.raises(ProjectConflictError):
        task_manager.update(task.id, fields={AGENT_CREATOR_FIELD: "sess_x"})


def test_agent_assignment_adds_every_assigning_agent(task_manager):
    task = task_manager.create(description="Operator task")
    updated = task_manager.update(task.id, tags=["claude"], _agent_creator_session="sess_first")
    assert updated.fields[AGENT_CREATOR_FIELD] == "sess_first"
    again = task_manager.update(task.id, tags=["codex"], _agent_creator_session="sess_second")
    assert again.fields[AGENT_CREATOR_FIELD] == "sess_first,sess_second"
    repeat = task_manager.update(task.id, tags=["claude"], _agent_creator_session="sess_first")
    assert repeat.fields[AGENT_CREATOR_FIELD] == "sess_first,sess_second"


def test_reassignment_by_a_restricted_agent_restricts_the_task(store):
    full_creator = _root(store, "claude_code", task_id="cc_creator")
    managed_assigner = _root(store, "claude", task_id="mgr_assigner")
    claimed = store.create(task_id="ab12cd34", status=STATUS_RUNNING, routing="claude_code")
    fields = {AGENT_CREATOR_FIELD: f"{full_creator.session_id},{managed_assigner.session_id}"}
    lineage = resolve_lineage(store, claimed, own_fields=fields)
    assert lineage.ceiling == route_capabilities("claude")
    assert host_shell_refusal("claude_code", lineage.ceiling) is not None
    # Each creator's own chain counts toward depth.
    deep = _child(store, _child(store, full_creator, "claude_code"), "claude_code")
    fields = {AGENT_CREATOR_FIELD: f"{managed_assigner.session_id},{deep.session_id}"}
    assert resolve_lineage(store, claimed, own_fields=fields).depth == 3


def test_project_child_stamp_and_assigner_both_count(store):
    hermes = _root(store, "hermes", task_id="h_creator")
    managed = _root(store, "claude", task_id="m_assigner")
    claimed = store.create(task_id="ab12cd35", status=STATUS_RUNNING, routing="local")
    lineage = resolve_lineage(store, claimed, own_fields={
        PROJECT_CHILD_CREATOR_FIELD: hermes.session_id, AGENT_CREATOR_FIELD: managed.session_id,
    })
    assert lineage.ceiling == route_capabilities("claude")


def test_assigns_engine_detects_tags_and_description_hashtags():
    from api.routes.tasks import _assigns_engine

    current = types.SimpleNamespace(tags=["work"], description="Synthetic title")
    assert _assigns_engine(current, ["work", "claude"], None)
    assert _assigns_engine(current, None, "Synthetic title #codex")
    assert not _assigns_engine(current, ["work", "errands"], "Plain title")
    already = types.SimpleNamespace(tags=["claude"], description="Fix it")
    assert not _assigns_engine(already, ["claude"], None)
    titled = types.SimpleNamespace(tags=[], description="Fix it #claude")
    assert not _assigns_engine(titled, None, "Fix it better #claude")


@pytest.fixture
def task_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routes import tasks as tasks_route
    from api.services.task_manager import TaskManager

    sessions = SessionStore(tmp_path / "api-sessions.db")
    manager = TaskManager(vault_path=tmp_path / "api-vault", index_path=tmp_path / "api-index" / "tasks.json")
    monkeypatch.setattr(tasks_route, "get_task_manager", lambda: manager)
    monkeypatch.setattr(tasks_route, "_session_store", sessions)
    monkeypatch.setattr(tasks_route, "_transcript_store", TranscriptStore(tmp_path / "api-transcripts"))
    return TestClient(app), manager


def test_api_stamps_agent_creates_and_assignments(task_api):
    client, manager = task_api
    agent = {"X-LifeOS-Agent-Session": "sess-agent-synthetic"}
    created = client.post("/api/tasks", json={"description": "Synthetic follow up #claude"}, headers=agent)
    assert created.status_code == 200, created.text
    assert manager.get(created.json()["id"]).fields[AGENT_CREATOR_FIELD] == "sess-agent-synthetic"

    operator = client.post("/api/tasks", json={"description": "Operator errand"})
    task_id = operator.json()["id"]
    assert AGENT_CREATOR_FIELD not in manager.get(task_id).fields
    notes_only = client.put(f"/api/tasks/{task_id}", json={"notes": "progress"}, headers=agent)
    assert notes_only.status_code == 200, notes_only.text
    assert AGENT_CREATOR_FIELD not in manager.get(task_id).fields
    assigned = client.put(f"/api/tasks/{task_id}", json={"tags": ["claude"]}, headers=agent)
    assert assigned.status_code == 200, assigned.text
    assert manager.get(task_id).fields[AGENT_CREATOR_FIELD] == "sess-agent-synthetic"


# ---------------------------------------------------------------------------
# The doctor's repair children keep the full tool set
# ---------------------------------------------------------------------------

def _hermes_root(store, conversation_id, bot):
    from api.services.agent_worker.hermes_session import resolve_hermes_caller_session_id

    return store.get_by_session_id(resolve_hermes_caller_session_id(store, conversation_id, bot=bot))


def _repair_child(store, root, routing="claude_code", *, workflow_id="inherit"):
    return store.create(
        task_id=f"repair_child_{routing}_{root.task_id}", status=STATUS_RUNNING, routing=routing,
        parent_session_id=root.session_id, root_session_id=root.session_id, spawn_depth=1,
        bot=root.bot, workflow_id=root.workflow_id if workflow_id == "inherit" else workflow_id,
    )


def test_doctor_repair_child_runs_with_todays_full_argv(store, transcripts, pinned_settings, tmp_path):
    from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor as Cce

    root = _hermes_root(store, "conv-doctor-1", "doctor")
    assert root.workflow_id and caps_mod.is_doctor_repair_root(store, root)
    # Same session id as the pinned child, so the prompt it embeds matches.
    child = store.create(
        task_id="doctor-child", session_id="sess_pin_child", status=STATUS_RUNNING,
        routing="claude_code", parent_session_id=root.session_id,
        root_session_id=root.session_id, spawn_depth=1, workflow_id=root.workflow_id,
    )
    assert resolve_lineage(store, child).ceiling is None
    cmd = normalized_argv(Cce, "/usr/bin/claude-example", child, store, transcripts, tmp_path)
    _assert_pinned(cmd, PINNED_ARGV["claude_code_child"], prompt_after="--append-system-prompt")
    # The same child under a non-doctor Hermes parent differs.
    plain_root = _hermes_root(store, "conv-plain-1", None)
    plain = store.create(task_id="plain-child", session_id="sess_pin_child2", status=STATUS_RUNNING,
                         routing="claude_code", parent_session_id=plain_root.session_id)
    assert "--no-chrome" in normalized_argv(Cce, "/usr/bin/claude-example", plain, store, transcripts, tmp_path)
    spawn_event = [e for e in transcripts.read(child.session_id) if e["kind"] == "claude_code_spawn"][0]
    assert "capabilities" not in spawn_event["payload"]


def test_doctor_repair_grandchild_inherits_the_full_child(store, transcripts):
    root = _hermes_root(store, "conv-doctor-2", "doctor")
    child = _repair_child(store, root)
    grandchild = _child(store, child, "claude_code")
    assert resolve_lineage(store, grandchild).ceiling == UNRESTRICTED
    local_child = _repair_child(store, root, "local")
    assert resolve_lineage(store, _child(store, local_child, "claude_code")).ceiling == route_capabilities("local")


@pytest.mark.parametrize("bot", [None, "fitness"])
def test_non_doctor_hermes_parent_still_restricts(store, transcripts, bot):
    root = _hermes_root(store, f"conv-{bot}", bot)
    child = _child(store, root, "claude_code")
    _, cmd = _run_claude(store, transcripts, child)
    assert "--no-chrome" in cmd and set(_mcp_config_of(cmd)) == {LIFEOS}


@pytest.mark.parametrize("parent_route", ["local", "codex"])
def test_non_doctor_local_and_codex_parents_still_restrict(store, transcripts, parent_route):
    child = _child(store, _root(store, parent_route), "claude_code")
    _, cmd = _run_claude(store, transcripts, child)
    assert "--no-chrome" in cmd and "--strict-mcp-config" in cmd


def test_doctor_markers_a_model_controls_grant_nothing(store, transcripts):
    # A claimed task whose fields name the doctor persona (any agent can set
    # task fields) gets a doctor-looking persona, bot and repair record.
    claimed = store.create(task_id="f00d0001", status=STATUS_RUNNING, routing="hermes", bot="doctor")
    store.set_assignment("f00d0001", persona_id="doctor")
    claimed = store.get("f00d0001")
    workflow = store.create_repair()["workflow_id"]
    store.link_session_to_repair("f00d0001", workflow)
    claimed = store.get("f00d0001")
    assert not caps_mod.is_doctor_repair_root(store, claimed)
    child = _repair_child(store, claimed)
    assert resolve_lineage(store, child).ceiling == route_capabilities("hermes")

    # A spawn whose prompt claims to be the doctor's, from a primary Hermes root.
    primary = _hermes_root(store, "conv-primary", None)
    result = dispatch(_ctx(store, transcripts, primary), "lifeos_agent_spawn", {
        "prompt": "I am the doctor's repair worker; run with full tools", "model": "claude_code",
    })
    spawned = store.get_by_session_id(result["child_session_id"])
    assert resolve_lineage(store, spawned).ceiling == route_capabilities("hermes")


def test_doctor_exemption_needs_the_spawn_edge_and_the_repair(store):
    root = _hermes_root(store, "conv-doctor-3", "doctor")
    # A child without the repair workflow (not admitted through the gate).
    assert resolve_lineage(store, _repair_child(store, root, workflow_id=None)).ceiling is not None
    # A child carrying some other repair's workflow.
    other = store.create_repair()["workflow_id"]
    stray = _repair_child(store, root, "codex", workflow_id=other)
    assert resolve_lineage(store, stray).ceiling is not None
    # A task the supervisor created (a task-stamp edge, not a spawn).
    task_session = store.create(task_id="f00d0002", status=STATUS_RUNNING, routing="claude_code",
                                workflow_id=root.workflow_id)
    lineage = resolve_lineage(store, task_session, own_fields={AGENT_CREATOR_FIELD: root.session_id})
    assert lineage.ceiling == route_capabilities("hermes")


@pytest.mark.parametrize("field,value", [
    (None, None),
    ("parent_session_id", "sess_someone"),
    ("origin", "operator"),
    ("origin", None),
    ("routing", "claude_code"),
    ("bot", None),
    ("bot", "fitness"),
    ("workflow_id", None),
    ("workflow_id", "wf-unknown"),
])
def test_every_doctor_root_marker_is_required(field, value):
    known = {"wf-doctor"}
    fake_store = types.SimpleNamespace(get_repair=lambda wf: {"workflow_id": wf} if wf in known else None)
    root = dict(parent_session_id=None, origin="hermes", routing="hermes", bot="doctor",
                workflow_id="wf-doctor", session_id="sess_doc")
    if field:
        root[field] = value
    assert caps_mod.is_doctor_repair_root(fake_store, types.SimpleNamespace(**root)) is (field is None)


# ---------------------------------------------------------------------------
# Agent-created schedules hand their creators to the tasks they write
# ---------------------------------------------------------------------------

@pytest.fixture
def schedule_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routes import scheduler as scheduler_route
    from api.services.scheduler_store import SchedulerScheduler, SchedulerStore
    from api.services.task_manager import TaskManager

    store = SchedulerStore(vault_path=tmp_path / "sched-vault", index_path=tmp_path / "sched-index.json")
    sessions = SessionStore(db_path=tmp_path / "sched-sessions.db")
    scheduler = SchedulerScheduler(store, session_store=sessions)
    manager = TaskManager(vault_path=tmp_path / "sched-task-vault", index_path=tmp_path / "sched-task-index.json")
    monkeypatch.setattr(scheduler_route, "get_scheduler_store", lambda: store)
    monkeypatch.setattr(scheduler_route, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr("api.services.task_manager.get_task_manager", lambda: manager)
    return TestClient(app), store, manager, tmp_path


def _agent_schedule(client, headers=None, **overrides):
    body = {"name": "Synthetic job", "schedule_type": "manual", "schedule_value": "",
            "action": "agent", "executor": "claude", "message_content": "Do the synthetic job"}
    body.update(overrides)
    response = client.post("/api/scheduler", json=body, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _fired_task_fields(client, manager, schedule_id, headers=None):
    response = client.post(f"/api/scheduler/{schedule_id}/trigger", headers=headers or {})
    assert response.status_code == 200, response.text
    tasks = [t for t in manager.list_tasks() if "Do the synthetic job" in t.description]
    assert len(tasks) == 1
    return tasks[0].fields


def test_agent_schedule_stamps_its_creator_on_every_task(schedule_api):
    from api.services.scheduler_store import SchedulerStore

    client, store, manager, tmp_path = schedule_api
    schedule_id = _agent_schedule(client, {"X-LifeOS-Agent-Session": "sess-sched-agent"})
    assert store.get(schedule_id).agent_creator_session == "sess-sched-agent"
    # The stamp round-trips through the entry's Markdown line.
    reloaded = SchedulerStore(vault_path=tmp_path / "sched-vault", index_path=tmp_path / "other-index.json")
    assert reloaded.get(schedule_id).agent_creator_session == "sess-sched-agent"
    fields = _fired_task_fields(client, manager, schedule_id)
    assert fields[AGENT_CREATOR_FIELD] == "sess-sched-agent"


def test_operator_schedule_writes_an_unstamped_task(schedule_api):
    client, store, manager, _ = schedule_api
    schedule_id = _agent_schedule(client)
    assert store.get(schedule_id).agent_creator_session == ""
    assert AGENT_CREATOR_FIELD not in _fired_task_fields(client, manager, schedule_id)


def test_agent_update_and_trigger_add_the_agent(schedule_api):
    client, store, manager, _ = schedule_api
    schedule_id = _agent_schedule(client)
    response = client.put(f"/api/scheduler/{schedule_id}", json={"message_content": "Do the synthetic job again"},
                          headers={"X-LifeOS-Agent-Session": "sess-editor"})
    assert response.status_code == 200, response.text
    assert store.get(schedule_id).agent_creator_session == "sess-editor"
    fields = _fired_task_fields(client, manager, schedule_id, {"X-LifeOS-Agent-Session": "sess-trigger"})
    assert fields[AGENT_CREATOR_FIELD] == "sess-editor,sess-trigger"
    # A trigger changes only the task it writes, not the schedule.
    assert store.get(schedule_id).agent_creator_session == "sess-editor"


def test_non_agent_schedule_is_not_stamped(schedule_api):
    client, store, _, _ = schedule_api
    schedule_id = _agent_schedule(client, {"X-LifeOS-Agent-Session": "sess-x"},
                                  action="notify", executor="", message_content="hello")
    assert store.get(schedule_id).agent_creator_session == ""


def test_body_cannot_set_the_schedule_stamp(schedule_api):
    client, store, _, _ = schedule_api
    schedule_id = _agent_schedule(client, agent_creator_session="sess-forged")
    assert store.get(schedule_id).agent_creator_session == ""
    client.put(f"/api/scheduler/{schedule_id}", json={"agent_creator_session": "sess-forged"})
    assert store.get(schedule_id).agent_creator_session == ""


@pytest.mark.parametrize("tool,args", [
    ("lifeos_schedule_create", {"name": "x", "schedule_type": "manual", "action": "agent"}),
    ("lifeos_schedule_update", {"schedule_id": "s1", "message_content": "x"}),
    ("lifeos_schedule_trigger", {"schedule_id": "s1"}),
])
def test_mcp_sends_the_agent_header_on_schedule_writes(tool, args):
    from unittest.mock import MagicMock

    import mcp_server

    server = mcp_server.LifeOSMCPServer(trusted_session_id="sess-stdio-agent")
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "s1"}
    client = MagicMock()
    client.post.return_value = client.put.return_value = client.request.return_value = response
    server.client = client
    server._call_api(tool, dict(args))
    calls = [c for c in (client.post.call_args, client.put.call_args, client.request.call_args) if c]
    assert calls and calls[0].kwargs["headers"][mcp_server.AGENT_SESSION_HEADER] == "sess-stdio-agent"


# ---------------------------------------------------------------------------
# Remote Codex children
# ---------------------------------------------------------------------------

@pytest.fixture
def remote_host(monkeypatch):
    from api.services.agent_worker import remote_spawn

    def resolve(host, api_host):
        return object() if host == "studio" else None

    monkeypatch.setattr(remote_spawn, "resolve_host_target", resolve)
    return resolve


def test_codex_child_on_a_remote_host_is_refused_even_with_nothing_to_disable(
    store, transcripts, remote_host, monkeypatch, _synthetic_home,
):
    from api.services.agent_worker import codex_executor as cx

    # This machine's Codex has only lifeos, so nothing would be disabled
    # locally; the remote host's own config is unknown.
    (_synthetic_home / ".codex" / "config.toml").write_text(
        '[mcp_servers.lifeos]\ncommand = "/opt/example/python"\nargs = []\n'
    )
    monkeypatch.setattr(cx, "resolve_host_target", remote_host)
    for parent_route in ("hermes", "codex"):
        parent = _root(store, parent_route, task_id=f"remote_parent_{parent_route}")
        child = store.create(
            task_id=f"remote_child_{parent_route}", status=STATUS_RUNNING, routing="codex",
            parent_session_id=parent.session_id, host="studio",
        )
        captured: list = []
        outcome = _codex_executor(store, transcripts, captured).execute(child, {"description": "x"})
        assert outcome.status == STATUS_FAILED and captured == []
        stub, _ = _worker_stub(store, transcripts, {})
        _, refusal = stub._resolve_capabilities(child)
        assert refusal and "API host" in refusal


def test_top_level_remote_codex_is_not_refused(store, transcripts, remote_host):
    stub, _ = _worker_stub(store, transcripts, {})
    session = store.create(task_id="cx-remote-top", status=STATUS_RUNNING, routing="codex",
                           origin="operator", host="studio")
    assert stub._resolve_capabilities(session)[1] is None
    local_child = store.create(task_id="cx-local-child", status=STATUS_RUNNING, routing="codex",
                               parent_session_id=_root(store, "local", task_id="lp").session_id)
    assert stub._resolve_capabilities(local_child)[1] is None
    remote_claude = store.create(task_id="cc-remote-top", status=STATUS_RUNNING, routing="claude_code",
                                 origin="operator", host="studio")
    assert stub._resolve_capabilities(remote_claude)[1] is None


# ---------------------------------------------------------------------------
# Top-level argv pinned to literal values captured from origin/main
# ---------------------------------------------------------------------------

PIN_SETTINGS = dict(vault_path="/vault/example", user_name="Example User",
                    code_dir="/code/example", claude_max_turns=40)


def normalized_argv(executor_cls, binary, session, store, transcripts, work_dir) -> list[str]:
    """One executor's argv for `session`, with per-run values (turn ids,
    working dir, temp output file) replaced by placeholders."""
    import re

    got = {}

    def spawn(cmd, **_kwargs):
        got["cmd"] = list(cmd)
        raise FileNotFoundError("capture only")

    executor_cls(session_store=store, transcript_store=transcripts, spawn_fn=spawn,
                 binary_resolver=lambda: binary).execute(
        session, {"description": "do the synthetic thing", "working_dir": str(work_dir)},
    )
    out = []
    for token in got["cmd"]:
        token = re.sub(r"lifeos_attempt_id=[^;\s]+", "lifeos_attempt_id=<A>", token)
        token = re.sub(r"lifeos_turn_id=[^.\s]+", "lifeos_turn_id=<T>", token)
        if token.startswith(tempfile.gettempdir()):
            token = "<OUT>"
        token = token.replace(str(work_dir), "<WORK>")
        out.append(token)
    return out


def normalized_top_level_argv(cce, cx, store, transcripts, work_dir) -> dict:
    """Top-level Claude Code and Codex argv, and a Claude Code child's, from
    executor modules `cce`/`cx`. Shared with the script that captured the
    pins below from origin/main."""
    claude = store.create(task_id="pin-cc", session_id="sess_pin_cc", routing="claude_code", origin="operator")
    codex = store.create(task_id="pin-cx", session_id="sess_pin_cx", routing="codex", origin="operator")
    parent = store.create(task_id="pin-parent", session_id="sess_pin_parent", routing="claude_code",
                          origin="operator", status=STATUS_RUNNING)
    child = store.create(task_id="pin-child", session_id="sess_pin_child", routing="claude_code",
                         parent_session_id=parent.session_id)
    return {
        "claude_code": normalized_argv(cce.ClaudeCodeExecutor, "/usr/bin/claude-example", claude,
                                       store, transcripts, work_dir),
        "codex": normalized_argv(cx.CodexExecutor, "/usr/bin/codex-example", codex,
                                 store, transcripts, work_dir),
        "claude_code_child": normalized_argv(cce.ClaudeCodeExecutor, "/usr/bin/claude-example", child,
                                             store, transcripts, work_dir),
    }


# Captured from origin/main's executors with PIN_SETTINGS, the synthetic
# Codex config of `_synthetic_home`, and `normalized_top_level_argv`.
PINNED_ARGV = {
    "claude_code": {
        "flags": [
            "/usr/bin/claude-example", "-p", "do the synthetic thing",
            "--output-format", "stream-json", "--verbose", "--max-turns", "40",
            "--dangerously-skip-permissions", "--chrome", "--append-system-prompt",
        ],
        "prompt_sha256": "5123941c82f09b83b5784a4716f338130d96c081bdd025eae5c50a82f43eb86d",
    },
    "claude_code_child": {
        "flags": [
            "/usr/bin/claude-example", "-p", "do the synthetic thing",
            "--output-format", "stream-json", "--verbose", "--max-turns", "40",
            "--dangerously-skip-permissions", "--chrome", "--append-system-prompt",
        ],
        "prompt_sha256": "435e54d337db65865fbb6877b4284534f874868d08d197aff0f974e1e6eda18d",
    },
    "codex": {
        "flags": [
            "/usr/bin/codex-example", "exec", "-c",
            'mcp_servers.lifeos.env_vars=["LIFEOS_AGENT_SESSION_ID", "LIFEOS_AGENT_ATTEMPT_ID", "LIFEOS_AGENT_TURN_ID"]',
            "--json", "--skip-git-repo-check", "--sandbox", "workspace-write",
            "--dangerously-bypass-approvals-and-sandbox", "-C", "<WORK>", "-o", "<OUT>",
        ],
        "prompt_sha256": "3e9dcbcf48479b0a956216e83874956592d8af8a004720925a82afc1d4dcfcd7",
    },
}


@pytest.fixture
def pinned_settings(monkeypatch):
    import platform

    for key, value in PIN_SETTINGS.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(platform, "system", lambda: "Linux")


def _assert_pinned(cmd, pin, *, prompt_after=None):
    import hashlib

    index = cmd.index(prompt_after) + 1 if prompt_after else len(cmd) - 1
    assert cmd[:index] + cmd[index + 1:] == pin["flags"]
    assert hashlib.sha256(cmd[index].encode()).hexdigest() == pin["prompt_sha256"]


def test_top_level_argv_matches_origin_main(store, transcripts, pinned_settings, tmp_path):
    from api.services.agent_worker import claude_code_executor, codex_executor

    argv = normalized_top_level_argv(claude_code_executor, codex_executor, store, transcripts, tmp_path)
    _assert_pinned(argv["claude_code"], PINNED_ARGV["claude_code"], prompt_after="--append-system-prompt")
    _assert_pinned(argv["claude_code_child"], PINNED_ARGV["claude_code_child"],
                   prompt_after="--append-system-prompt")
    _assert_pinned(argv["codex"], PINNED_ARGV["codex"])
