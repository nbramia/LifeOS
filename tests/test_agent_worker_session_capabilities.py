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


def _today_top_level_argv(session) -> list[str]:
    """The argv a top-level Claude Code session runs with, spelled out."""
    from api.services.agent_worker.claude_code_executor import _CLARIFY_OPERATOR, _SYSTEM_PROMPT
    from api.services.agent_worker.delegation import PROJECT_TASK_GUIDANCE, delegation_preamble

    platform_desc = "Linux server running Ubuntu" if os.uname().sysname == "Linux" else "Mac running macOS"
    prompt = _SYSTEM_PROMPT.format(
        vault_path=settings.vault_path, user_name=settings.user_name,
        code_dir=settings.code_dir, platform_desc=platform_desc, git_discipline="",
        tool_summary="You already have a browser (--chrome), filesystem, and shell.",
        clarification=_CLARIFY_OPERATOR,
        delegation=delegation_preamble(
            session.session_id, trigger="To run background work in parallel,",
            model='"claude_code" with tier="haiku"/"sonnet"/"opus", '
                  'or "local" for the on-box model',
        ),
        project_guidance=PROJECT_TASK_GUIDANCE,
        turn_identity=(
            f"lifeos_session_id={session.session_id}; lifeos_attempt_id={session.attempt_id}; "
            f"lifeos_turn_id={session.turn_id}."
        ),
    )
    return [
        "/usr/bin/claude-example", "-p", "do the synthetic thing",
        "--output-format", "stream-json", "--verbose",
        "--max-turns", str(settings.claude_max_turns),
        "--dangerously-skip-permissions", "--chrome",
        "--append-system-prompt", prompt,
    ]


def _run_claude(store, transcripts, session):
    captured: list = []
    executor = _claude_executor(store, transcripts, captured)
    outcome = executor.execute(session, {"description": "do the synthetic thing"})
    return outcome, (captured[0] if captured else None)


def test_top_level_claude_code_argv_is_exactly_todays(store, transcripts):
    session = store.create(task_id="op-1", routing="claude_code", origin="operator")
    outcome, cmd = _run_claude(store, transcripts, session)
    assert outcome.status == STATUS_COMPLETED
    assert cmd == _today_top_level_argv(store.get("op-1"))
    assert "You already have a browser (--chrome), filesystem, and shell. Your LifeOS" in cmd[cmd.index("--append-system-prompt") + 1]
    spawn_event = [e for e in transcripts.read(session.session_id) if e["kind"] == "claude_code_spawn"][0]
    assert "capabilities" not in spawn_event["payload"]


def test_claude_code_child_of_claude_code_parent_is_unrestricted(store, transcripts):
    parent = _root(store, "claude_code")
    child = _child(store, parent, "claude_code")
    _, cmd = _run_claude(store, transcripts, child)
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


def test_agent_assignment_stamps_once(task_manager):
    task = task_manager.create(description="Operator task")
    updated = task_manager.update(task.id, tags=["claude"], _agent_creator_session="sess_first")
    assert updated.fields[AGENT_CREATOR_FIELD] == "sess_first"
    again = task_manager.update(task.id, tags=["codex"], _agent_creator_session="sess_second")
    assert again.fields[AGENT_CREATOR_FIELD] == "sess_first"


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
