"""Drives a headless Codex CLI subprocess from inside the agent worker.

Surface mirrors :class:`ClaudeCodeExecutor` so the worker can route
``routing='codex'`` sessions uniformly. Differences from /claude:

- Codex's ``--json`` stream emits a small, regular event set
  (``thread.started``, ``turn.started``, ``item.completed``,
  ``turn.completed``) instead of Claude's stream-json.
- The final agent message is captured via ``--output-last-message``.
- No ``[NOTIFY]`` convention — Codex isn't trained on it, so every final
  message relays verbatim and there's no plan-approval blocking path.
  ``[CLARIFY]`` is different: a fresh session's briefing (the shared
  git-discipline text) tells Codex to end its final message with
  ``[CLARIFY] <question>`` when it needs to ask something before it's
  done, and the completion path here (reusing Claude Code's own
  ``_CLARIFY_RE``) treats that the same way Claude Code's live
  ``[CLARIFY]`` does: a paused, resumable question, not a finished turn.
- Cost is derived from the last ``turn.completed.usage`` block via the
  ingest module's pricing table.
- Resume uses ``codex exec resume <session_id> [PROMPT]``.

All session state — the CLI's thread id, transcript events, status
transitions — is persisted via ``SessionStore`` and ``TranscriptStore``
so sessions survive worker restarts and surface in ``/agents``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from typing import Callable, Optional

from api.services.agent_worker.assignment import ENGINE_CODEX, map_effort_for_engine
from api.services.agent_worker.binary_resolver import resolve_for_spawn
from api.services.agent_worker.capabilities_preamble import CAPABILITIES_PREAMBLE
from api.services.agent_worker.claude_code_executor import (
    _ALTERNATE_AUTH_ENV_PREFIXES,
    _CLARIFY_RE,
)
from api.services.agent_worker.delegation import PROJECT_TASK_GUIDANCE, delegation_preamble
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.remote_spawn import (
    HostResolutionError,
    build_remote_argv,
    env_names_matching_prefixes,
    is_local_host,
    last_nonempty_line,
    read_line_with_deadline,
    read_remote_pgid_line,
    resolve_host_target,
)
from api.services.agent_worker.remote_spawn import api_host_name as _api_host_name
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.usage_ledger import UsageLedger
from api.services.secret_redaction import scrub_and_bound
from api.services.codex.session_ingest import _cost_from_usage
from config.settings import settings


logger = logging.getLogger(__name__)


HEARTBEAT_INTERVAL = 300  # 5 minutes between progress pings


def _resolve_codex_binary() -> str:
    """Resolve the codex CLI binary path via the shared binary resolver
    (:mod:`api.services.agent_worker.binary_resolver`), so this spawn-time
    resolution agrees with the worker's readiness check.
    """
    return resolve_for_spawn(settings.codex_binary)


def _delegation_header(
    session_id: str, attempt_id: str | None = None, turn_id: str | None = None,
) -> str:
    """Per-session preamble line telling Codex its LifeOS session id and how to
    hand off work it can't do (e.g. browser automation) to another engine."""
    identity = (
        f"\nlifeos_session_id={session_id}; lifeos_attempt_id={attempt_id}; "
        f"lifeos_turn_id={turn_id}."
        if attempt_id and turn_id else ""
    )
    return "=== YOUR SESSION ===\n" + delegation_preamble(
        session_id,
        trigger=(
            "If a task needs a capability you lack — e.g. browser/GUI "
            "automation you can't perform headlessly —"
        ),
        model='"claude_code" for the browser-enabled Claude Code CLI',
    ) + "\n\n" + PROJECT_TASK_GUIDANCE + identity


def _git_discipline_header(working_dir: str) -> str:
    """Preamble block with the git-discipline instructions when
    `working_dir` is a worker-provisioned worktree (see
    `git_worktree.describe_worktree`), else an empty string — a vault- or
    home-directory session has no worktree and gets nothing prepended."""
    from api.services.agent_worker.git_worktree import git_discipline_text
    text = git_discipline_text(working_dir)
    return f"=== GIT DISCIPLINE ===\n{text}\n\n" if text else ""


# Reason codes returned in ``ExecutorOutcome.reason``.
REASON_TIMEOUT = "timeout"
REASON_BINARY_NOT_FOUND = "binary_not_found"
# parity with ClaudeCodeExecutor — an operator kill flips the row to
# FAILED and signals this subprocess; we exit silently under this reason so the
# worker skips the spurious "session failed" notice.
REASON_KILLED = "killed"
# A final message ending in `[CLARIFY] <question>` — parity with
# ClaudeCodeExecutor's live [CLARIFY] pause, detected post-hoc here since
# Codex has no mid-turn pause of its own.
REASON_AWAITING_CLARIFICATION = "awaiting_clarification"

# CODEX_* env vars kept when stripping the subprocess env (see `_clean_env`
# and `_remote_unset_env_names`) — CODEX_HOME carries `~/.codex/auth.json`.
_CODEX_ENV_KEEP = {"CODEX_HOME"}

# The trusted-identity env vars `_clean_env` sets on the codex subprocess so
# `mcp_server.py`'s stdio server can attest the caller for `lifeos_agent_*`
# tools. Codex does not forward arbitrary parent env vars to a stdio MCP
# server child — only names listed in that server's own `env_vars` config
# key — so a *local* spawn's `_build_command` also merges these names into
# a `-c mcp_servers.lifeos.env_vars=[...]` override (see
# `_merged_identity_env_vars`) whenever the API host's own Codex config
# already declares an `[mcp_servers.lifeos]` server; it's skipped otherwise
# (overriding `env_vars` on a server that doesn't exist breaks Codex's
# config loader) and for a remote-target spawn (that host's own Codex
# config is unknown to the API host).
_IDENTITY_ENV_VARS = (
    "LIFEOS_AGENT_SESSION_ID",
    "LIFEOS_AGENT_ATTEMPT_ID",
    "LIFEOS_AGENT_TURN_ID",
)


@dataclass
class _RunState:
    """Per-invocation mutable state. Module-level so the stream-reader
    thread can mutate it without capture surprises.
    """
    session_id: Optional[str] = None  # CLI's thread id, captured at thread.started
    # Codex's --json stream does not prove the served model. This is the
    # requested assignment only; served identity stays unknown in the ledger.
    model: Optional[str] = None
    final_text: str = ""
    cost_usd: float = 0.0
    tool_call_count: int = 0
    last_activity: str = ""
    last_usage: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    last_notify_at: float = field(default_factory=time.time)
    terminal: bool = False
    last_error: str = ""


NotificationCallback = Callable[[str], None]
SpawnFn = Callable[..., subprocess.Popen]


class CodexExecutor:
    """Run one Codex CLI session synchronously and persist its state.

    Constructor injection points mirror :class:`ClaudeCodeExecutor`:
      - ``notification_callback`` — invoked with each agent_message during
        the session. Defaults to no-op.
      - ``spawn_fn`` / ``binary_resolver`` — test seams.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore,
        transcript_store: TranscriptStore,
        notification_callback: Optional[NotificationCallback] = None,
        operator_send: Optional[Callable[[object, str], None]] = None,
        spawn_fn: Optional[SpawnFn] = None,
        binary_resolver: Optional[Callable[[], str]] = None,
        timeout_seconds: Optional[int] = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
    ) -> None:
        self.session_store = session_store
        self.transcript_store = transcript_store
        self._notify = notification_callback or (lambda _msg: None)
        self._operator_send = operator_send
        self._spawn_fn = spawn_fn or subprocess.Popen
        self._binary_resolver = binary_resolver or _resolve_codex_binary
        # Reuse the existing /claude wall-clock knob — operators have one less
        # thing to configure. A future PR can split this if codex turns out
        # to need a different budget.
        self._timeout = timeout_seconds if timeout_seconds is not None else settings.claude_timeout_seconds
        self._heartbeat_interval = heartbeat_interval
        self._mcp_warned = False  # gate the missing-MCP warning to once per process

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _with_identity(session, outcome: ExecutorOutcome) -> ExecutorOutcome:
        from dataclasses import replace
        return replace(
            outcome,
            session_id=getattr(outcome, "session_id", None) or session.session_id,
            attempt_id=getattr(outcome, "attempt_id", None) or session.attempt_id,
            turn_id=getattr(outcome, "turn_id", None) or session.turn_id,
            executor="codex",
            continuation_id=(
                getattr(outcome, "continuation_id", None)
                or session.claude_code_session_id
            ),
        )

    def execute(self, session, task: dict) -> ExecutorOutcome:
        """Drive a fresh /codex session."""
        prompt = (task.get("description") or "").strip()
        if not prompt:
            self.transcript_store.append(session.session_id, "codex_no_prompt", {})
            return ExecutorOutcome(status=STATUS_FAILED, reason="empty prompt")

        session = self.session_store.begin_executor_turn(
            session.task_id, "execute", session=session,
        )

        working_dir = task.get("working_dir") or None
        # Warn (once per process) if Codex can't reach the lifeos MCP server —
        # without it the agent is context-blind to personal data. See
        # docs/guides/agent-worker-setup.md § Codex for the config block.
        self._warn_if_mcp_missing()
        # Prepend the LifeOS capabilities briefing so the fresh Codex turn has
        # the same situational awareness as the managed/local routes, plus a
        # per-session delegation header so the agent can hand off work it can't
        # do (e.g. browser automation → a claude_code child), and — only when
        # `working_dir` is a worker-provisioned worktree — the git-discipline
        # instructions. Only on the opening turn — resume() reloads the
        # thread, which already carries this from the first prompt.
        delegation = _delegation_header(
            session.session_id, session.attempt_id, session.turn_id,
        )
        prompt_working_dir = working_dir
        if prompt_working_dir is None and is_local_host(session.host, _api_host_name()):
            prompt_working_dir = os.getcwd()
        git_discipline = (
            _git_discipline_header(prompt_working_dir) if prompt_working_dir else ""
        )
        full_prompt = f"{delegation}\n{git_discipline}{CAPABILITIES_PREAMBLE}\n{prompt}"
        return self._with_identity(session, self._run(
            session=session,
            prompt=full_prompt,
            task_title=prompt,
            working_dir=working_dir,
            resume_session_id=None,
        ))

    def resume(self, session, message: str, working_dir: Optional[str] = None) -> ExecutorOutcome:
        """Resume an already-completed /codex session via
        ``codex exec resume <thread_id> [PROMPT]``.
        """
        # Reuses the claude_code_session_id column; routing='codex' disambiguates.
        resume_id = session.claude_code_session_id
        if not resume_id:
            self.transcript_store.append(
                session.session_id, "codex_resume_no_session_id", {},
            )
            return ExecutorOutcome(status=STATUS_FAILED, reason="no codex_session_id on record")
        session = self.session_store.begin_executor_turn(
            session.task_id, "resume", session=session,
        )
        return self._with_identity(session, self._run(
            session=session,
            prompt=message,
            task_title=session.task_id,
            working_dir=working_dir,
            resume_session_id=resume_id,
        ))

    # ------------------------------------------------------------------
    # Internal lifecycle
    # ------------------------------------------------------------------

    def _build_command(
        self,
        prompt: str,
        working_dir: Optional[str],
        resume_session_id: Optional[str],
        last_message_file: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        is_remote: bool = False,
    ) -> list[str]:
        binary = self._binary_resolver()
        # `workspace-write` lets codex edit files inside the working dir
        # but not touch anything outside it — matches the spirit of the
        # /claude `--dangerously-skip-permissions` choice (operator trusts
        # codex inside the project) without going full danger-full-access.
        common = [
            "--json",
            "--skip-git-repo-check",
            "--sandbox", "workspace-write",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if working_dir:
            common.extend(("-C", working_dir))
        common.extend(("-o", last_message_file))
        # Board-assigned model/effort. When neither flag is passed, codex
        # falls back to whatever `~/.codex/config.toml` says.
        # `--model` only when set (an unset board model keeps that
        # fallback); effort is mapped to Codex's own
        # minimal|low|medium|high|xhigh vocabulary via the config override.
        if model:
            common = ["--model", model, *common]
        codex_effort = map_effort_for_engine(ENGINE_CODEX, effort)
        if codex_effort:
            common = ["-c", f"model_reasoning_effort={codex_effort}", *common]
        # Codex only forwards parent env vars named in a stdio MCP server's
        # own `env_vars` config key (see `_IDENTITY_ENV_VARS`) — this
        # override makes the identity vars `_clean_env` sets reach the
        # lifeos MCP child without requiring an operator config edit. Only
        # add it when the operator's config actually declares an
        # `[mcp_servers.lifeos]` server: overriding `env_vars` on a server
        # that doesn't exist gives that key a bare `{env_vars: [...]}`
        # table with no `command`, which fails Codex's config loader
        # entirely ("invalid transport") — an install with no lifeos MCP
        # server configured must keep working (context-blind, same as
        # today) rather than hard-failing every `#codex` task. The value is
        # the ordered union of any `env_vars` the operator's server entry
        # already declares plus the identity names — a bare override would
        # otherwise silently replace (not extend) an existing list, e.g. one
        # forwarding another var into the same MCP child.
        #
        # Only checked for a *local* spawn: this reads the API host's own
        # config.toml, which describes nothing about a board-assigned remote
        # host's Codex install — Codex loads config from wherever it
        # actually runs, so a remote-target spawn gets no override at all;
        # adding it there risks the same config-loader crash on a remote
        # host with no lifeos server.
        if not is_remote:
            merged_env_vars = self._merged_identity_env_vars()
            if merged_env_vars is not None:
                common = [
                    "-c",
                    "mcp_servers.lifeos.env_vars=" + json.dumps(merged_env_vars),
                    *common,
                ]
        if resume_session_id:
            return [binary, "exec", "resume", resume_session_id, *common, prompt]
        return [binary, "exec", *common, prompt]

    @staticmethod
    def _codex_config_path() -> str:
        """Path to the Codex CLI's own config file — `$CODEX_HOME/config.toml`
        when set, else `~/.codex/config.toml`. Used by `_lifeos_mcp_config`
        and directly by `_warn_if_mcp_missing`'s log message."""
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        return os.path.join(codex_home, "config.toml")

    @classmethod
    def _lifeos_mcp_config(cls) -> Optional[dict]:
        """The `[mcp_servers.lifeos]` table from the Codex config, or None
        when it isn't declared or the config can't be read/parsed. Parsed
        with `tomllib` (not a substring check) so callers get the table's
        actual contents — e.g. an existing `env_vars` list — not just a
        yes/no. Any read or parse failure reads as "not configured", the
        conservative default: a false positive would let `_build_command`
        add the `env_vars` override for a server Codex doesn't actually
        have, which breaks config loading entirely. Cheap per-spawn: the
        file is small and read once per `_build_command` call. Shared by
        `_lifeos_mcp_server_configured`, `_merged_identity_env_vars`, and
        `_warn_if_mcp_missing`.
        """
        try:
            with open(cls._codex_config_path(), "rb") as f:
                cfg = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        server_cfg = (cfg.get("mcp_servers") or {}).get("lifeos")
        return server_cfg if isinstance(server_cfg, dict) else None

    @classmethod
    def _lifeos_mcp_server_configured(cls) -> bool:
        """True when the Codex config declares an `[mcp_servers.lifeos]`
        server — see `_lifeos_mcp_config`."""
        return cls._lifeos_mcp_config() is not None

    @classmethod
    def _merged_identity_env_vars(cls) -> Optional[list[str]]:
        """Ordered, de-duplicated union of any `env_vars` the operator's
        `[mcp_servers.lifeos]` entry already declares plus
        `_IDENTITY_ENV_VARS`, or None when that server isn't configured at
        all (see `_lifeos_mcp_config`). A bare `env_vars=[...]` `-c`
        override *replaces* the operator's list rather than extending it,
        so this preserves anything already forwarded (e.g. a var another
        tool on that server relies on) alongside the identity vars.
        """
        server_cfg = cls._lifeos_mcp_config()
        if server_cfg is None:
            return None
        existing = server_cfg.get("env_vars")
        merged = list(existing) if isinstance(existing, list) else []
        for name in _IDENTITY_ENV_VARS:
            if name not in merged:
                merged.append(name)
        return merged

    def _warn_if_mcp_missing(self) -> None:
        """Best-effort check that Codex has the lifeos MCP server configured.

        Codex reaches LifeOS data only through an ``[mcp_servers.lifeos]`` block
        in ``~/.codex/config.toml`` (or ``$CODEX_HOME/config.toml``). Unlike
        Claude Code — which inherits the ``lifeos`` server from ``~/.claude.json``
        — a fresh Codex install has none, leaving the agent context-blind. We
        check for the lifeos server specifically (not just any MCP block), since
        an unrelated server would leave LifeOS just as unreachable. We can't fix
        per-machine config from the repo, so we surface it loudly in logs once
        per process. Never raises: a config we can't read is not fatal.
        """
        if self._mcp_warned:
            return
        self._mcp_warned = True
        if not self._lifeos_mcp_server_configured():
            logger.warning(
                "Codex has no [mcp_servers.lifeos] in %s — the agent cannot reach "
                "lifeos_* tools and will be blind to personal data. See "
                "docs/guides/agent-worker-setup.md § Codex MCP setup.",
                self._codex_config_path(),
            )

    @staticmethod
    def _clean_env(
        session_id: str | None = None,
        attempt_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict:
        """Strip CODEX_* env vars so the subprocess doesn't inherit the
        operator's interactive Codex context — keep CODEX_HOME so auth
        (`~/.codex/auth.json`) is preserved.

        Anthropic credentials go too. Codex doesn't use them itself, but
        it has a shell and `claude` is on the PATH: an inherited
        ANTHROPIC_API_KEY would let a codex session start an API-billed Claude
        Code session, which — like codex itself — is exempt from the per-task
        dollar cap for being subscription-billed.
        """
        keep = _CODEX_ENV_KEEP
        env = {
            k: v for k, v in os.environ.items()
            if (not k.startswith("CODEX_") or k in keep)
            and not k.startswith(_ALTERNATE_AUTH_ENV_PREFIXES)
        }
        if session_id:
            session_var, attempt_var, turn_var = _IDENTITY_ENV_VARS
            env[session_var] = session_id
            if attempt_id:
                env[attempt_var] = attempt_id
            if turn_id:
                env[turn_var] = turn_id
            from api.services.agent_worker.session_resources import scratch_env
            env.update(scratch_env(session_id))
        return env

    @staticmethod
    def _remote_unset_env_names() -> list[str]:
        """Env var names to `env -u` on a remote-spawned subprocess —
        mirrors `_clean_env`'s own strip (CODEX_* except CODEX_HOME, plus
        the alternate-auth prefixes), applied to the remote command instead
        of the local one."""
        names = env_names_matching_prefixes(("CODEX_",), keep=_CODEX_ENV_KEEP)
        names += env_names_matching_prefixes(_ALTERNATE_AUTH_ENV_PREFIXES)
        return sorted(set(names))

    def _run(
        self,
        *,
        session,
        prompt: str,
        task_title: str,
        working_dir: Optional[str],
        resume_session_id: Optional[str],
    ) -> ExecutorOutcome:
        sid = session.session_id
        # Use a per-session tempfile so concurrent runs (future) don't
        # clobber each other.
        last_msg_fd, last_msg_path = tempfile.mkstemp(prefix="codex_last_", suffix=".txt")
        os.close(last_msg_fd)

        # Board-assigned host: resolve BEFORE any spawn call. An
        # unknown host name fails the task closed with no ssh invocation.
        # NOTE: the `-o last_msg_path` fallback (below) reads a LOCAL temp
        # file, which a remote CLI never writes to — remote sessions rely
        # entirely on the `--json` stream's `agent_message` events for
        # final_text (the normal, primary path); see
        # docs/specs/technical/agent-worker.md's host registry section.
        host = getattr(session, "host", None)
        is_remote = False
        try:
            target = resolve_host_target(host, _api_host_name())
        except HostResolutionError as exc:
            self.transcript_store.append(sid, "codex_unknown_host", {"host": host})
            self._cleanup_tempfile(last_msg_path)
            return ExecutorOutcome(status=STATUS_FAILED, reason=str(exc))
        if target is not None:
            is_remote = True

        command_working_dir = working_dir
        if command_working_dir is None and not is_remote:
            command_working_dir = os.getcwd()
        cmd = self._build_command(
            prompt, command_working_dir, resume_session_id, last_msg_path,
            model=getattr(session, "model", None),
            effort=getattr(session, "effort", None),
            is_remote=is_remote,
        )

        if target is not None:
            cmd = build_remote_argv(
                cmd,
                target=target,
                unset_env_names=self._remote_unset_env_names(),
                session_id=sid,
                env={key: value for key, value in self._clean_env(
                    sid, session.attempt_id, session.turn_id,
                ).items() if key in {"TMPDIR", "TMP", "TEMP", "LIFEOS_AGENT_ATTEMPT_ID", "LIFEOS_AGENT_TURN_ID"}},
            )

        self.transcript_store.append(sid, "codex_spawn", {
            "resume": bool(resume_session_id),
            "working_dir": command_working_dir,
            "host": host,
            "remote": is_remote,
        })

        try:
            proc = self._spawn_fn(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd() if is_remote else command_working_dir,
                text=True,
                env=self._clean_env(sid, session.attempt_id, session.turn_id),
                # own process-group leader so the operator kill can
                # `os.killpg(pgid, ...)` codex + its children without touching
                # the worker process. Mirrors ClaudeCodeExecutor. For a remote
                # spawn this is the local `ssh` client's own group — the
                # remote kill path reaches the real CLI over ssh.
                start_new_session=True,
            )
        except OSError as exc:
            # Mirrors ClaudeCodeExecutor's matching handler: any spawn-time
            # OS failure, not just a missing binary, must write the SAME
            # compensating `codex_binary_not_found` marker the missing-binary
            # case does, or `_cli_subprocess_launch_count` would miscount an
            # uncompensated failure as a real launch.
            self.transcript_store.append(sid, "codex_binary_not_found", {"error": str(exc)})
            self._cleanup_tempfile(last_msg_path)
            return ExecutorOutcome(
                status=STATUS_FAILED,
                reason=(
                    REASON_BINARY_NOT_FOUND if isinstance(exc, FileNotFoundError)
                    else f"codex spawn failed: {exc}"
                ),
            )

        self.session_store.update_status(
            session.task_id, STATUS_RUNNING,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )

        if is_remote:
            # Strip the remote wrapper's `PGID:<n>` first stdout line
            # — see ClaudeCodeExecutor._run for the identical mechanism.
            #
            # Bounded wait: see ClaudeCodeExecutor._run
            # for why this read needs a deadline of its own, ahead of the
            # wall-clock watchdog below.
            #
            # Record the pid event immediately after
            # Popen — ahead of this deadline-bounded read — see
            # ClaudeCodeExecutor._run for why: it's what lets the operator-
            # kill fallback reach a stalled local ssh client during the
            # read's own deadline window rather than finding no pid event.
            self.transcript_store.append(sid, "codex_pid", {
                "pid": proc.pid, "pgid": None, "remote": True, "host": host,
            })
            pgid = None
            if proc.stdout is not None:
                deadline = settings.agent_ssh_connect_timeout + 5
                first_line, timed_out_reading_pgid = read_line_with_deadline(proc.stdout, deadline)
                if timed_out_reading_pgid:
                    self._terminate_unresponsive(proc)
                    self.transcript_store.append(sid, "codex_remote_unresponsive", {
                        "host": host, "deadline_seconds": deadline,
                    })
                    self.session_store.update_status(
                        session.task_id, STATUS_FAILED,
                        attempt_id=session.attempt_id, turn_id=session.turn_id,
                    )
                    self._cleanup_tempfile(last_msg_path)
                    return ExecutorOutcome(
                        status=STATUS_FAILED,
                        reason=f"host {host} did not answer within {deadline}s",
                    )
                pgid = read_remote_pgid_line(first_line)
            if pgid is not None:
                self.session_store.set_remote_pgid(
                    session.task_id, pgid,
                    attempt_id=session.attempt_id, turn_id=session.turn_id,
                )
                self.transcript_store.append(sid, "codex_pid", {
                    "pid": proc.pid, "pgid": pgid, "remote": True, "host": host,
                })
        else:
            # record the subprocess pid + pgid so the operator kill endpoint
            # (a separate process) can signal it via the transcript. Mirrors the
            # claude_code path; teardown scans for `codex_pid` too.
            try:
                pgid = os.getpgid(proc.pid)
            except Exception:  # pragma: no cover — defensive; fall back to the pid
                pgid = proc.pid
            self.transcript_store.append(sid, "codex_pid", {"pid": proc.pid, "pgid": pgid})

        requested_model = getattr(session, "model", None)
        if not requested_model:
            requested_model = (getattr(session, "execution_spec", None) or {}).get("model_id")
        state = _RunState(model=requested_model)
        timed_out = threading.Event()
        stop_heartbeat = threading.Event()

        watchdog = threading.Timer(self._timeout, self._on_timeout, args=(proc, timed_out))
        watchdog.daemon = True
        watchdog.start()

        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(session, state, stop_heartbeat),
            daemon=True,
            name=f"CodexHeartbeat-{sid[:8]}",
        )
        heartbeat_thread.start()

        try:
            self._consume_stream(proc, session, state)
            proc.wait()
        finally:
            watchdog.cancel()
            stop_heartbeat.set()

        # Pick up the final agent message from the output file as the
        # authoritative `final_text`. The `--json` stream's `item.completed`
        # events sometimes deliver partial chunks for long responses; the
        # `-o` file is always the complete final message.
        if not state.final_text:
            try:
                with open(last_msg_path, "r", encoding="utf-8") as f:
                    state.final_text = f.read().strip()
            except OSError:
                pass
        self._cleanup_tempfile(last_msg_path)
        # Normalize once so the codex_completed payload, final_chars, and the
        # outcome all agree with the worker's stripped `body` — a whitespace-only
        # final_text must read as empty (no blank "output:" block in a parent's
        # resume turn, no anchor-less operator send).
        state.final_text = state.final_text.strip()
        self._record_usage(session, state)

        # operator-kill silent guard (parity with ClaudeCodeExecutor). If
        # the row is already FAILED *and the subprocess did not exit cleanly*, the
        # operator killed us mid-run — exit silently so the worker skips the
        # spurious "session failed" notice (the killpg'd subprocess returns a
        # negative returncode that would otherwise hit the FAILED path below).
        #
        # Two legitimate writers flip the row FAILED mid-run: the operator kill
        # (signals the subprocess → non-zero/negative returncode) and
        # `LocalExecutor._cascade_kill_lineage` on a lineage-budget breach
        # (routing-agnostic — it flips non-terminal CLI descendants too). The
        # returncode gate keeps a clean completion (returncode 0 → work finished)
        # from being mis-tagged REASON_KILLED if a cascade races the FAILED flip:
        # it falls through to the COMPLETED path below.
        current = self.session_store.get(session.task_id)
        if self.session_store.is_cancelled(
            session.task_id, session.attempt_id, session.turn_id,
        ):
            self.transcript_store.append(sid, "codex_killed", {
                "returncode": proc.returncode, "reason": "cancelled",
            })
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED)
        if current is not None and current.status == STATUS_FAILED and proc.returncode != 0:
            self.transcript_store.append(sid, "codex_killed", {"returncode": proc.returncode})
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED)

        if timed_out.is_set():
            self.session_store.update_status(
                session.task_id, STATUS_FAILED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
            )
            self.transcript_store.append(sid, "codex_timeout", {
                "timeout_seconds": self._timeout,
            })
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_TIMEOUT)

        # A non-zero exit is authoritative even when `turn.completed` was
        # parsed: the CLI process itself is reporting a bad end-of-run, and
        # a turn that completed just before that is exactly the "stopped
        # mid-thought but looks done" shape the terminal-evidence gate in
        # normalize_outcome exists to catch, not a case for this executor to
        # paper over. `state.terminal` still reaches `exit_meta` below either
        # way, so a genuinely-clean run with no `turn.completed` (an
        # interrupted stream that happens to exit 0) is still flagged there.
        if proc.returncode == 0:
            # A final message ending `[CLARIFY] <question>` is a paused
            # question, not a finished turn — checked before the completion
            # write below so it never reaches STATUS_COMPLETED. Reuses
            # ClaudeCodeExecutor's own `_CLARIFY_RE` so both engines honor
            # exactly the same marker convention. A spawned child has no
            # operator to pause for — parity with ClaudeCodeExecutor's
            # `_CLARIFY_CHILD` convention, its question folds into the
            # completed turn's text instead so the parent sees it via the
            # normal completion path.
            clarify_match = _CLARIFY_RE.search(state.final_text)
            if clarify_match and not session.parent_session_id:
                question = clarify_match.group(1).strip()
                self.session_store.update_status(
                    session.task_id, STATUS_BLOCKED,
                    attempt_id=session.attempt_id, turn_id=session.turn_id,
                )
                self.transcript_store.append(sid, "codex_awaiting_clarification", {
                    "question_chars": len(question),
                })
                return ExecutorOutcome(
                    status=STATUS_BLOCKED,
                    reason=REASON_AWAITING_CLARIFICATION,
                    final_text=question,
                )
            if clarify_match and session.parent_session_id:
                state.final_text = f"[needs clarification] {clarify_match.group(1).strip()}"
            exit_meta = self._exit_metadata(proc, timed_out, state)
            # `project=False`: a clean exit alone is not an earned completion —
            # the dispatch layer's own check runs on the outcome this call
            # returns. Recording the row without projecting keeps the
            # cancelled/stale-turn detection below intact while leaving the
            # card alone until that check decides this run actually completed.
            completed = self.session_store.update_status(
                session.task_id, STATUS_COMPLETED,
                attempt_id=session.attempt_id, turn_id=session.turn_id,
                project=False,
            )
            if not completed:
                if self.session_store.is_cancelled(
                    session.task_id, session.attempt_id, session.turn_id,
                ):
                    self.transcript_store.append(sid, "codex_killed", {
                        "returncode": proc.returncode, "reason": "cancelled",
                    })
                    return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED)
                return ExecutorOutcome(status=STATUS_RUNNING, reason="stale CLI turn")
            self.transcript_store.append(sid, "codex_completed", {
                "cost_usd": state.cost_usd,
                "model": state.model,
                "tool_call_count": state.tool_call_count,
                "final_chars": len(state.final_text),
                # Persist the text itself so a parent that spawned this session
                # can read it via _child_final_text — a child's completion never
                # streams to the operator, so this is its only path out
                # (parity with claude_code_completed).
                "final_text": state.final_text,
                # how the subprocess ended, parity with claude_code_completed.
                "exit_meta": exit_meta,
            })
            return ExecutorOutcome(
                status=STATUS_COMPLETED,
                final_text=state.final_text,
                # Codex has no [NOTIFY] convention — always 0. The earned-
                # completion gate in worker.py falls through to the
                # PR-mention / summary-shape checks for this route.
                notifications_sent=0,
                exit_meta=exit_meta,
            )

        stderr_tail = ""
        try:
            stderr_tail = (proc.stderr.read() if proc.stderr else "") or ""
        except Exception:
            pass
        self.session_store.update_status(
            session.task_id, STATUS_FAILED,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        self.transcript_store.append(sid, "codex_failed", {
            "returncode": proc.returncode,
            "stderr_tail": stderr_tail[-500:],
        })
        # Fold the ssh failure's stderr into the
        # reason on the remote path — see ClaudeCodeExecutor._run for why.
        title = task_title or session.task_id
        detail = last_nonempty_line(stderr_tail) or state.last_error
        if not detail:
            detail = "no error output was captured"
        reason = f"task '{title}': codex exited with code {proc.returncode}: {detail}"
        if is_remote:
            last_line = last_nonempty_line(stderr_tail)
            if last_line:
                reason = f"ssh to {host} failed (exit {proc.returncode}): {last_line}"
        return ExecutorOutcome(
            status=STATUS_FAILED,
            reason=scrub_and_bound(reason),
        )

    @staticmethod
    def _exit_metadata(proc, timed_out: threading.Event, state: "_RunState") -> dict:
        """Best-effort description of how the subprocess ended.
        Mirrors ClaudeCodeExecutor._exit_metadata; ``stream_terminal_event_seen``
        is True only when a `turn.completed` event was actually parsed, not
        merely inferred from a clean returncode."""
        rc = proc.returncode
        meta: dict = {
            "returncode": rc,
            "timed_out": timed_out.is_set(),
            "stream_terminal_event_seen": state.terminal,
        }
        if rc is not None and rc < 0:
            meta["signal"] = -rc
        return meta

    @staticmethod
    def _cleanup_tempfile(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Stream parsing
    # ------------------------------------------------------------------

    def _consume_stream(self, proc, session, state: _RunState) -> None:
        if proc.stdout is None:
            return
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_event(event, session, state)
            if state.terminal:
                # Drain remaining stdout without re-processing so the
                # subprocess can flush and exit.
                for _ in proc.stdout:
                    pass
                break

    def _handle_event(self, event: dict, session, state: _RunState) -> None:
        etype = event.get("type")
        sid = session.session_id

        if etype == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                state.session_id = thread_id
                # Persist immediately so a worker crash leaves enough state
                # to resume via `codex exec resume <id>`.
                self.session_store.set_claude_code_session_id(
                    session.task_id, thread_id,
                    attempt_id=session.attempt_id, turn_id=session.turn_id,
                )
                self.transcript_store.append(sid, "codex_init", {
                    "codex_session_id": thread_id,
                })
            return

        if etype == "turn.started":
            state.last_activity = "thinking"
            return

        if etype == "item.completed":
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                text = (item.get("text") or "").strip()
                if text:
                    # Record the message as the running final_text and surface
                    # a short preview in the heartbeat, but do NOT stream it to
                    # Telegram. Codex emits a narration message before most tool
                    # calls; forwarding each one floods the chat. The worker
                    # sends the final agent message exactly once on completion
                    # (and registers it as the reply-thread anchor), so the
                    # operator sees meaningful progress (heartbeats) + the
                    # result, mirroring Claude's [NOTIFY] selectivity.
                    state.final_text = text
                    state.last_activity = text[:40]
                    self.transcript_store.append(sid, "codex_assistant_text", {
                        "text": text, "chars": len(text),
                    })
            elif itype in (
                "command_executed", "command_execution", "local_shell_call",
                "function_call", "mcp_tool_call", "file_change", "web_search",
            ):
                state.tool_call_count += 1
                changes = item.get("changes") or [{}]
                cmd_preview = str(
                    item.get("command") or item.get("name")
                    or changes[0].get("path") or ""
                )
                state.last_activity = f"running {cmd_preview[:40]}" if cmd_preview else "running a tool"
                self.transcript_store.append(sid, "codex_tool_use", {
                    "type": itype,
                    "preview": cmd_preview[:240],
                })
            elif itype == "agent_reasoning":
                # Drop reasoning text from the transcript — it's verbose
                # and the cumulative token count gives us the size signal.
                pass
            return

        if etype in ("error", "turn.failed"):
            error = event.get("error") or event.get("message") or {}
            if isinstance(error, dict):
                error = error.get("message") or error.get("code") or ""
            state.last_error = str(error).strip()
            return

        if etype == "turn.completed":
            usage = event.get("usage") or {}
            if usage:
                state.last_usage = usage
                # Track cost for /agents reporting, but DON'T cap it — the Codex
                # route is subscription-billed, so there's no marginal per-task
                # cost to cap. Only the managed/API route enforces max_dollars.
                # Wall-clock and the CLI's own limits still bound runaway sessions.
                state.cost_usd = _cost_from_usage(usage, state.model)
            # `turn.completed` is the CLI's own signal that it finished the
            # turn (the module docstring's event list) — this is the terminal
            # marker `_exit_metadata` reports as `stream_terminal_event_seen`.
            state.terminal = True
            return

    def _record_usage(self, session, state: _RunState) -> None:
        """Record subscription usage without inventing a served model."""
        usage = state.last_usage or {}
        if not usage:
            return
        try:
            ledger = UsageLedger(self.session_store.db_path)
            ledger.record_cli_usage(
                session,
                source="codex_executor",
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0) or 0),
                estimated_cost_usd=state.cost_usd if state.model else None,
                source_event_id=state.session_id,
                event_id=f"codex:{session.session_id}:{state.session_id or 'unknown'}",
            )
        except Exception:  # noqa: BLE001 — accounting cannot alter executor outcome
            logger.warning("codex usage ledger write failed for %s", session.task_id, exc_info=True)

    # ------------------------------------------------------------------
    # Watchdog + heartbeat
    # ------------------------------------------------------------------

    @staticmethod
    def _terminate_unresponsive(proc) -> None:
        """Best-effort terminate an ssh client that never answered the
        `PGID:` read within its deadline. Mirrors
        `_on_timeout`'s terminate/wait/kill sequence minus the timed_out
        flag (there is no watchdog running yet at this point — this read
        happens BEFORE it starts). Mirrors ClaudeCodeExecutor's identical
        helper."""
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("remote pgid-wait terminate failed: %s", exc)

    @staticmethod
    def _on_timeout(proc, timed_out: threading.Event) -> None:
        # MUST NOT write a terminal status to the session row. The kill-guard
        # (in execute(), after proc.wait()) keys on the row being FAILED to detect
        # an operator kill; a timed-out session must still be RUNNING when the
        # guard checks so the timeout path — not REASON_KILLED — claims it. This
        # only sets the timed_out flag and terminates the OS process; execute()
        # owns the row transition.
        timed_out.set()
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("watchdog terminate failed: %s", exc)

    def _heartbeat_loop(
        self, session, state: _RunState, stop_event: threading.Event,
    ) -> None:
        while not stop_event.wait(self._heartbeat_interval):
            if state.terminal:
                return
            now = time.time()
            if now - state.last_notify_at < self._heartbeat_interval:
                continue
            elapsed = int(now - state.started_at)
            minutes = elapsed // 60
            activity = f" — {state.last_activity}" if state.last_activity else ""
            cost = f" | ${state.cost_usd:.2f}" if state.cost_usd > 0 else ""
            try:
                body = f"Still working{activity} ({minutes}m elapsed{cost})"
                if self._operator_send is not None:
                    self._operator_send(session, body)
                else:
                    self._notify(body)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("heartbeat callback raised: %s", exc)
            state.last_notify_at = now
