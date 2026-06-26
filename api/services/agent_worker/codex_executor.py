"""Drives a headless Codex CLI subprocess from inside the agent worker.

Surface mirrors :class:`ClaudeCodeExecutor` so the worker can route
``routing='codex'`` sessions uniformly. Differences from /claude:

- Codex's ``--json`` stream emits a small, regular event set
  (``thread.started``, ``turn.started``, ``item.completed``,
  ``turn.completed``) instead of Claude's stream-json.
- The final agent message is captured via ``--output-last-message``.
- No ``[NOTIFY]/[CLARIFY]`` convention — Codex isn't trained on them.
  We relay the final message verbatim and skip the plan/clarification
  blocking paths.
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
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from api.services.agent_worker.capabilities_preamble import CAPABILITIES_PREAMBLE
from api.services.agent_worker.delegation import delegation_preamble
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.codex.session_ingest import _cost_from_usage
from config.settings import settings


logger = logging.getLogger(__name__)


HEARTBEAT_INTERVAL = 300  # 5 minutes between progress pings


# Common install locations for the codex CLI when systemd-style minimal
# PATHs don't pick up the user-local install (npm-global / nvm).
_CODEX_SEARCH_PATHS = [
    os.path.expanduser("~/.local/bin/codex"),
    "/usr/local/bin/codex",
    os.path.expanduser("~/.npm/bin/codex"),
    "/opt/homebrew/bin/codex",
]


def _resolve_codex_binary() -> str:
    """Resolve the codex CLI binary path, with nvm-friendly fallbacks.

    Mirrors :func:`claude_code_executor._resolve_claude_binary`. Also probes
    the active nvm version dir since codex is usually `npm i -g`-installed
    into the current node version's bin.
    """
    configured = getattr(settings, "codex_binary", "codex")
    if os.path.isabs(configured):
        return configured
    if shutil.which(configured):
        return shutil.which(configured)
    for path in _CODEX_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            logger.info("codex binary not on PATH, found at %s", path)
            return path
    # nvm: ~/.nvm/versions/node/v*/bin/codex
    nvm_root = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_root):
        for ver in sorted(os.listdir(nvm_root), reverse=True):
            candidate = os.path.join(nvm_root, ver, "bin", "codex")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                logger.info("codex binary found under nvm at %s", candidate)
                return candidate
    return configured  # caller surfaces FileNotFoundError on spawn


def _delegation_header(session_id: str) -> str:
    """Per-session preamble line telling Codex its LifeOS session id and how to
    hand off work it can't do (e.g. browser automation) to another engine."""
    return "=== YOUR SESSION ===\n" + delegation_preamble(
        session_id,
        trigger=(
            "If a task needs a capability you lack — e.g. browser/GUI "
            "automation you can't perform headlessly —"
        ),
        model='"claude_code" for the browser-enabled Claude Code CLI',
    )


# Reason codes returned in ``ExecutorOutcome.reason``.
REASON_TIMEOUT = "timeout"
REASON_BINARY_NOT_FOUND = "binary_not_found"
# #379: parity with ClaudeCodeExecutor — an operator kill flips the row to
# FAILED and signals this subprocess; we exit silently under this reason so the
# worker skips the spurious "session failed" notice.
REASON_KILLED = "killed"


@dataclass
class _RunState:
    """Per-invocation mutable state. Module-level so the stream-reader
    thread can mutate it without capture surprises.
    """
    session_id: Optional[str] = None  # CLI's thread id, captured at thread.started
    # Codex's --json stream doesn't include the model id in any event, so we
    # default to gpt-5.5 (the current top OpenAI model since 2026-04 and the
    # codex CLI's recommended default). This only affects cost rollups —
    # token counts are accurate regardless. Override on the state directly
    # if the operator pins a different model via `-m` or config.toml.
    model: str = "gpt-5.5"
    final_text: str = ""
    cost_usd: float = 0.0
    tool_call_count: int = 0
    last_activity: str = ""
    last_usage: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    last_notify_at: float = field(default_factory=time.time)
    terminal: bool = False


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
        spawn_fn: Optional[SpawnFn] = None,
        binary_resolver: Optional[Callable[[], str]] = None,
        timeout_seconds: Optional[int] = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
    ) -> None:
        self.session_store = session_store
        self.transcript_store = transcript_store
        self._notify = notification_callback or (lambda _msg: None)
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

    def execute(self, session, task: dict) -> ExecutorOutcome:
        """Drive a fresh /codex session."""
        prompt = (task.get("description") or "").strip()
        if not prompt:
            self.transcript_store.append(session.session_id, "codex_no_prompt", {})
            return ExecutorOutcome(status=STATUS_FAILED, reason="empty prompt")

        working_dir = task.get("working_dir") or os.getcwd()
        # Warn (once per process) if Codex can't reach the lifeos MCP server —
        # without it the agent is context-blind to personal data. See
        # docs/guides/agent-worker-setup.md § Codex for the config block.
        self._warn_if_mcp_missing()
        # Prepend the LifeOS capabilities briefing so the fresh Codex turn has
        # the same situational awareness as the managed/local routes, plus a
        # per-session delegation header so the agent can hand off work it can't
        # do (e.g. browser automation → a claude_code child). Only on the
        # opening turn — resume() reloads the thread, which already carries
        # this from the first prompt.
        delegation = _delegation_header(session.session_id)
        full_prompt = f"{delegation}\n{CAPABILITIES_PREAMBLE}\n{prompt}"
        return self._run(
            session=session,
            prompt=full_prompt,
            working_dir=working_dir,
            resume_session_id=None,
        )

    def resume(self, session, message: str, working_dir: Optional[str] = None) -> ExecutorOutcome:
        """Resume a previously-completed /codex session via
        ``codex exec resume <thread_id> [PROMPT]``.
        """
        # Reuses the claude_code_session_id column; routing='codex' disambiguates.
        resume_id = session.claude_code_session_id
        if not resume_id:
            self.transcript_store.append(
                session.session_id, "codex_resume_no_session_id", {},
            )
            return ExecutorOutcome(status=STATUS_FAILED, reason="no codex_session_id on record")
        wd = working_dir or os.getcwd()
        return self._run(
            session=session,
            prompt=message,
            working_dir=wd,
            resume_session_id=resume_id,
        )

    # ------------------------------------------------------------------
    # Internal lifecycle
    # ------------------------------------------------------------------

    def _build_command(
        self,
        prompt: str,
        working_dir: str,
        resume_session_id: Optional[str],
        last_message_file: str,
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
            "-C", working_dir,
            "-o", last_message_file,
        ]
        if resume_session_id:
            return [binary, "exec", "resume", resume_session_id, *common, prompt]
        return [binary, "exec", *common, prompt]

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
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        config_path = os.path.join(codex_home, "config.toml")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                contents = f.read()
        except OSError:
            contents = ""
        if "[mcp_servers.lifeos]" not in contents:
            logger.warning(
                "Codex has no [mcp_servers.lifeos] in %s — the agent cannot reach "
                "lifeos_* tools and will be blind to personal data. See "
                "docs/guides/agent-worker-setup.md § Codex MCP setup.",
                config_path,
            )

    @staticmethod
    def _clean_env() -> dict:
        """Strip CODEX_* env vars so the subprocess doesn't inherit the
        operator's interactive Codex context — keep CODEX_HOME so auth
        (`~/.codex/auth.json`) is preserved.
        """
        keep = {"CODEX_HOME"}
        return {k: v for k, v in os.environ.items()
                if not k.startswith("CODEX_") or k in keep}

    def _run(
        self,
        *,
        session,
        prompt: str,
        working_dir: str,
        resume_session_id: Optional[str],
    ) -> ExecutorOutcome:
        sid = session.session_id
        # Use a per-session tempfile so concurrent runs (future) don't
        # clobber each other.
        last_msg_fd, last_msg_path = tempfile.mkstemp(prefix="codex_last_", suffix=".txt")
        os.close(last_msg_fd)

        cmd = self._build_command(prompt, working_dir, resume_session_id, last_msg_path)

        self.transcript_store.append(sid, "codex_spawn", {
            "resume": bool(resume_session_id),
            "working_dir": working_dir,
        })

        try:
            proc = self._spawn_fn(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=working_dir,
                text=True,
                env=self._clean_env(),
                # #379: own process-group leader so the operator kill can
                # `os.killpg(pgid, ...)` codex + its children without touching
                # the worker process. Mirrors ClaudeCodeExecutor.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            self.transcript_store.append(sid, "codex_binary_not_found", {"error": str(exc)})
            self._cleanup_tempfile(last_msg_path)
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_BINARY_NOT_FOUND)

        self.session_store.update_status(session.task_id, STATUS_RUNNING)

        # #379: record the subprocess pid + pgid so the operator kill endpoint
        # (a separate process) can signal it via the transcript. Mirrors the
        # claude_code path; teardown scans for `codex_pid` too.
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:  # pragma: no cover — defensive; fall back to the pid
            pgid = proc.pid
        self.transcript_store.append(sid, "codex_pid", {"pid": proc.pid, "pgid": pgid})

        state = _RunState()
        timed_out = threading.Event()
        stop_heartbeat = threading.Event()

        watchdog = threading.Timer(self._timeout, self._on_timeout, args=(proc, timed_out))
        watchdog.daemon = True
        watchdog.start()

        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(state, stop_heartbeat),
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

        # #379: operator-kill silent guard (parity with ClaudeCodeExecutor). If
        # the row is already FAILED, the operator killed us mid-run — exit
        # silently so the worker skips the spurious "session failed" notice (the
        # killpg'd subprocess returns a negative returncode that would otherwise
        # hit the FAILED path below). Only the operator kill can flip the row to
        # FAILED while we run — the executor itself set RUNNING just after spawn.
        current = self.session_store.get(session.task_id)
        if current is not None and current.status == STATUS_FAILED:
            self.transcript_store.append(sid, "codex_killed", {"returncode": proc.returncode})
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED)

        if timed_out.is_set():
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            self.transcript_store.append(sid, "codex_timeout", {
                "timeout_seconds": self._timeout,
            })
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_TIMEOUT)

        if proc.returncode == 0 or state.terminal:
            self.session_store.update_status(session.task_id, STATUS_COMPLETED)
            self.transcript_store.append(sid, "codex_completed", {
                "cost_usd": state.cost_usd,
                "model": state.model,
                "tool_call_count": state.tool_call_count,
                "final_chars": len(state.final_text),
            })
            return ExecutorOutcome(
                status=STATUS_COMPLETED,
                final_text=state.final_text,
            )

        stderr_tail = ""
        try:
            stderr_tail = (proc.stderr.read() if proc.stderr else "") or ""
        except Exception:
            pass
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        self.transcript_store.append(sid, "codex_failed", {
            "returncode": proc.returncode,
            "stderr_tail": stderr_tail[-500:],
        })
        return ExecutorOutcome(
            status=STATUS_FAILED,
            reason=f"codex exited with code {proc.returncode}",
        )

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
                self.session_store.set_claude_code_session_id(session.task_id, thread_id)
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
            elif itype in ("command_executed", "local_shell_call", "function_call"):
                state.tool_call_count += 1
                cmd_preview = str(item.get("command") or item.get("name") or "")
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

        if etype == "turn.completed":
            usage = event.get("usage") or {}
            if usage:
                state.last_usage = usage
                # Track cost for /agents reporting, but DON'T cap it — the Codex
                # route is subscription-billed, so there's no marginal per-task
                # cost to cap. Only the managed/API route enforces max_dollars.
                # Wall-clock and the CLI's own limits still bound runaway sessions.
                state.cost_usd = _cost_from_usage(usage, state.model)
            return

        if etype in ("session.completed", "exec.completed"):
            state.terminal = True
            return

    # ------------------------------------------------------------------
    # Watchdog + heartbeat
    # ------------------------------------------------------------------

    @staticmethod
    def _on_timeout(proc, timed_out: threading.Event) -> None:
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

    def _heartbeat_loop(self, state: _RunState, stop_event: threading.Event) -> None:
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
                self._notify(f"Still working{activity} ({minutes}m elapsed{cost})")
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("heartbeat callback raised: %s", exc)
            state.last_notify_at = now
