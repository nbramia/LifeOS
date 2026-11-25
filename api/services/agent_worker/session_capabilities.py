"""What one agent session may use, and the ceiling it hands to its children.

A session's capability set is derived from what the worker grants its route:

| Route | Host shell | Chrome | MCP servers |
|-------|------------|--------|-------------|
| `claude_code` | yes | yes (`--chrome`) | the CLI's own configuration |
| `codex` | yes | no | the servers in Codex's `config.toml` |
| `local` / `remote` | yes (the `Bash` tool) | no | `lifeos` (in-process) |
| `hermes` | yes (the gateway's terminal runs as the operator) | no | `lifeos` |
| `claude` (Managed Agents) | no (tools run in Anthropic's container) | no | `lifeos` |

A session started by another session (a `lifeos_agent_spawn` child, or a task
an agent created or assigned to an engine) gets the intersection of its own
route's set and its creator's effective set, so a child never has more than
the session that started it. Sessions nobody started (the operator's own, the
board's Open, schedules and tasks the operator created) have no ceiling and
run exactly as their route grants. So does the one exemption: a child the
doctor's Hermes repair supervisor spawns for its repair
(`is_doctor_repair_child`).

How each route applies a ceiling:

- `claude_code`: `--no-chrome` when the ceiling has no Chrome, and
  `--strict-mcp-config` with a generated config listing only the servers
  the ceiling allows.
- `codex`: the configured servers the ceiling withholds are disabled.
- `local` / `remote`: without a host shell in the ceiling, the session runs
  with LifeOS tools only (no Read/Write/Edit/Bash/WebFetch/WebSearch).
- `claude_code`, `codex` and `hermes` without a host shell in the ceiling are
  refused (`host_shell_refusal`): those engines are a host shell, so no
  limited form of them exists.
"""
from __future__ import annotations

import json
import logging
import os
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional


logger = logging.getLogger(__name__)


LIFEOS_MCP_SERVER = "lifeos"

# Routes that are themselves a shell on one of the operator's machines and
# cannot run without one. `local`/`remote` also have a shell, through tools
# the worker can withhold, so they run limited instead of being refused.
HOST_ONLY_ROUTES = frozenset({"claude_code", "codex", "hermes"})

# Task fields naming the session that created (or engine-assigned) a task.
# The project-child stamp is written by `TaskManager.create`; the general
# stamp covers every other agent-attributed create or engine assignment.
# The value "unattested" marks a write from the shared HTTP MCP transport,
# whose exact session is unknown.
PROJECT_CHILD_CREATOR_FIELD = "project_child_creator_session"
AGENT_CREATOR_FIELD = "agent_creator_session"
UNATTESTED_CREATOR = "unattested"

# Bound on the creator chain walk; a longer chain is treated as unknown.
_MAX_CHAIN_HOPS = 32

MCP_CONFIG_FILENAME = "mcp-config.json"


@dataclass(frozen=True)
class SessionCapabilities:
    """The tools and privileges one session may use.

    `mcp_servers` is None when the session loads whatever its CLI is
    configured with; otherwise it is the exact set of server names.
    """

    host_shell: bool
    chrome: bool
    mcp_servers: Optional[frozenset[str]]

    @property
    def may_spawn(self) -> bool:
        """Spawning goes through the LifeOS MCP server's inter-agent tools."""
        return self.mcp_servers is None or LIFEOS_MCP_SERVER in self.mcp_servers

    def intersect(self, other: "SessionCapabilities") -> "SessionCapabilities":
        if self.mcp_servers is None:
            servers = other.mcp_servers
        elif other.mcp_servers is None:
            servers = self.mcp_servers
        else:
            servers = self.mcp_servers & other.mcp_servers
        return SessionCapabilities(
            host_shell=self.host_shell and other.host_shell,
            chrome=self.chrome and other.chrome,
            mcp_servers=servers,
        )


UNRESTRICTED = SessionCapabilities(host_shell=True, chrome=True, mcp_servers=None)
# A creator the worker cannot identify (the shared HTTP transport, or a
# session row that no longer exists) gets the set of the one route that
# reaches LifeOS that way: Managed Agents.
_UNIDENTIFIED_CREATOR = SessionCapabilities(
    host_shell=False, chrome=False, mcp_servers=frozenset({LIFEOS_MCP_SERVER}),
)
_NOTHING = SessionCapabilities(host_shell=False, chrome=False, mcp_servers=frozenset())


class CapabilityLookupError(RuntimeError):
    """A task needed to resolve a session's creator could not be read."""


# Returns the `fields` dict of a task, None when the task does not exist,
# and raises `CapabilityLookupError` when it cannot be read.
TaskFieldsLookup = Callable[[str], Optional[dict]]


def http_task_fields_lookup(client, api_base: str) -> TaskFieldsLookup:
    """A `TaskFieldsLookup` that reads tasks through the LifeOS API."""

    def lookup(task_id: str) -> Optional[dict]:
        try:
            response = client.get(f"{api_base}/api/tasks/{task_id}")
        except Exception as exc:
            raise CapabilityLookupError(f"task {task_id} could not be read") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise CapabilityLookupError(f"task {task_id} could not be read")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("id") != task_id:
            raise CapabilityLookupError(f"task {task_id} could not be read")
        return payload.get("fields") or {}

    return lookup


def _codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return Path(codex_home) / "config.toml"


def codex_mcp_servers() -> dict:
    """The `[mcp_servers]` tables in Codex's config; empty when unreadable."""
    try:
        with open(_codex_config_path(), "rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    servers = config.get("mcp_servers") or {}
    return servers if isinstance(servers, dict) else {}


def route_capabilities(routing: Optional[str]) -> SessionCapabilities:
    """The set the worker grants `routing` before any ceiling applies."""
    if routing == "claude_code":
        return UNRESTRICTED
    if routing == "codex":
        return SessionCapabilities(
            host_shell=True, chrome=False,
            mcp_servers=frozenset(codex_mcp_servers()),
        )
    if routing in ("local", "remote", "hermes"):
        return SessionCapabilities(
            host_shell=True, chrome=False,
            mcp_servers=frozenset({LIFEOS_MCP_SERVER}),
        )
    if routing == "claude":
        return _UNIDENTIFIED_CREATOR
    return _NOTHING


def _task_backed(session) -> bool:
    """A session claimed from a real task (not a spawn, operator or Hermes
    root): its task may name the agent that created it."""
    return not session.parent_session_id and session.origin in (None, "", "agent")


def creator_session_id(fields: Optional[dict]) -> Optional[str]:
    """The creator a task's fields name, or None for an operator task."""
    fields = fields or {}
    for key in (PROJECT_CHILD_CREATOR_FIELD, AGENT_CREATOR_FIELD):
        value = str(fields.get(key) or "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class Lineage:
    """A session's inherited ceiling and its distance from a session nobody
    started. `ceiling` is None for such a session."""

    ceiling: Optional[SessionCapabilities]
    depth: int

    def effective(self, routing: Optional[str]) -> SessionCapabilities:
        own = route_capabilities(routing)
        return own if self.ceiling is None else own.intersect(self.ceiling)


def is_doctor_repair_root(session_store, session) -> bool:
    """Whether `session` is the doctor's Hermes repair supervisor.

    Every field read here is written by server code and none by a model:
    `origin="hermes"` and `routing="hermes"` only by
    `hermes_session.resolve_hermes_caller_session_id`, the root `bot` from
    the persona of the doctor's own Hermes conversation, and `workflow_id`
    by `SessionStore.create` when it opens the repair record for a doctor
    root. A task's `persona_id` field, a spawn argument, a tag or a prompt
    reaches none of them.
    """
    from api.services.agent_worker.session_store import DOCTOR_PERSONA_ID

    return bool(
        session.parent_session_id is None
        and session.origin == "hermes"
        and session.routing == "hermes"
        and session.bot == DOCTOR_PERSONA_ID
        and session_store.get_repair(session.workflow_id) is not None
    )


def is_doctor_repair_child(session_store, session, creator) -> bool:
    """Whether `session` is a child the doctor's repair supervisor spawned
    for its repair: a direct `lifeos_agent_spawn` child (not a task the
    supervisor created) carrying the supervisor's repair workflow, which
    `lifeos_agent_spawn` attaches only after the operator approved a goal."""
    return bool(
        session.parent_session_id == creator.session_id
        and session.workflow_id
        and session.workflow_id == creator.workflow_id
        and is_doctor_repair_root(session_store, creator)
    )


def resolve_lineage(
    session_store,
    session,
    *,
    task_fields: Optional[TaskFieldsLookup] = None,
    own_fields: Optional[dict] = None,
) -> Lineage:
    """Walk from `session` to the session nobody started, intersecting each
    creator's route set along the way.

    A session's creator is its spawning parent, or, for a task-backed
    session, the session its task's fields name. `own_fields` supplies
    `session`'s own task fields when the caller already holds them. Without
    a `task_fields` lookup, task stamps are read only from `own_fields`.
    """
    creators: list = []
    # What the chain ends in beyond its known creators: nothing (None), a
    # creator the worker cannot identify, or a chain too long to trust.
    tail: Optional[SessionCapabilities] = _NOTHING
    hops = _MAX_CHAIN_HOPS
    current = session
    current_fields = own_fields
    for _ in range(_MAX_CHAIN_HOPS):
        creator_id: Optional[str] = current.parent_session_id
        if not creator_id and _task_backed(current):
            fields = current_fields
            if fields is None and task_fields is not None and current.task_id:
                fields = task_fields(current.task_id)
            creator_id = creator_session_id(fields)
        current_fields = None
        if not creator_id:
            tail, hops = None, len(creators)
            break
        creator = None
        if creator_id != UNATTESTED_CREATOR:
            creator = session_store.get_by_session_id(creator_id)
        if creator is None:
            tail, hops = _UNIDENTIFIED_CREATOR, len(creators) + 1
            break
        if is_doctor_repair_child(session_store, current, creator):
            # The doctor's repair child runs as a session nobody started;
            # its own children inherit from it as usual.
            tail, hops = None, len(creators) + 1
            break
        creators.append(creator)
        current = creator
    ceiling = tail
    for creator in creators:
        own = route_capabilities(creator.routing)
        ceiling = own if ceiling is None else own.intersect(ceiling)
    return Lineage(ceiling=ceiling, depth=hops)


def host_shell_refusal(routing: Optional[str], ceiling: Optional[SessionCapabilities]) -> Optional[str]:
    """Why `routing` may not run under `ceiling`, or None when it may."""
    if ceiling is None or ceiling.host_shell or routing not in HOST_ONLY_ROUTES:
        return None
    return (
        f"route {routing!r} is a shell on the operator's machine, and the "
        "session that started this one has none; a child gets only what its "
        "creator has (use 'claude', or 'local' for LifeOS tools only)"
    )


# ---------------------------------------------------------------------------
# Dispatch-time hand-off to the executors
# ---------------------------------------------------------------------------

_active_capabilities: ContextVar[Optional[SessionCapabilities]] = ContextVar(
    "agent_session_capabilities", default=None,
)


@contextmanager
def capability_context(capabilities: SessionCapabilities) -> Iterator[None]:
    """Make `capabilities` the effective set for the executor turn run inside."""
    token = _active_capabilities.set(capabilities)
    try:
        yield
    finally:
        _active_capabilities.reset(token)


def active_capabilities() -> Optional[SessionCapabilities]:
    """The set the worker resolved for the turn running now, if any."""
    return _active_capabilities.get()


def effective_for_executor(session_store, session) -> SessionCapabilities:
    """The set the worker resolved for this turn, or, when an executor runs
    outside the worker's dispatch, the set its spawn chain gives it."""
    active = _active_capabilities.get()
    if active is not None:
        return active
    return resolve_lineage(session_store, session).effective(session.routing)


# ---------------------------------------------------------------------------
# Restricted MCP configuration for Claude Code children
# ---------------------------------------------------------------------------

def _claude_user_config_path() -> Path:
    # The executor strips CLAUDE* from the CLI's environment, so the child
    # reads the default user config, never a CLAUDE_CONFIG_DIR override.
    return Path.home() / ".claude.json"


def claude_user_mcp_servers() -> dict:
    """User-scope `mcpServers` from `~/.claude.json`; empty when unreadable."""
    try:
        config = json.loads(_claude_user_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    return servers if isinstance(servers, dict) else {}


def write_strict_mcp_config(allowed: frozenset[str], dest_dir: Path) -> tuple[Path, list[str]]:
    """Write an MCP config holding only the user-scope servers in `allowed`.

    The file goes in the session's private directory with mode 0600, since
    server entries can carry credentials. Returns the path and the server
    names it lists.
    """
    available = claude_user_mcp_servers()
    selected = {name: available[name] for name in sorted(allowed) if name in available}
    dest_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = dest_dir / MCP_CONFIG_FILENAME
    payload = json.dumps({"mcpServers": selected}).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
    finally:
        os.close(fd)
    return path, list(selected)


def codex_disabled_servers(capabilities: SessionCapabilities) -> list[str]:
    """Codex-configured servers `capabilities` withholds, sorted."""
    if capabilities.mcp_servers is None:
        return []
    return sorted(set(codex_mcp_servers()) - capabilities.mcp_servers)
