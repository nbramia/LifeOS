"""Drives a headless Claude Code CLI subprocess from inside the agent worker.

Mirrors the executor surface used by `LocalExecutor` / `ManagedExecutor` so
the worker can route `routing="claude_code"` sessions uniformly. All session
state — the CLI's session UUID, transcript events, status transitions — is
persisted via `SessionStore` and `TranscriptStore`, so sessions survive
worker restarts and surface in the `/agents` UI.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from config.settings import settings


logger = logging.getLogger(__name__)


HEARTBEAT_INTERVAL = 300  # 5 minutes between progress pings


_NOTIFY_RE = re.compile(r"\[NOTIFY\]\s*(.*?)(?=\[(?:NOTIFY|CLARIFY)\]|\Z)", re.DOTALL)
_CLARIFY_RE = re.compile(r"\[CLARIFY\]\s*(.*?)(?=\[(?:NOTIFY|CLARIFY)\]|\Z)", re.DOTALL)


# Common install locations for the Claude CLI when launchd-style minimal PATHs
# don't pick up the user-local install. Mirrors the orchestrator's resolver.
_CLAUDE_SEARCH_PATHS = [
    os.path.expanduser("~/.local/bin/claude"),
    "/usr/local/bin/claude",
    os.path.expanduser("~/.npm/bin/claude"),
    "/opt/homebrew/bin/claude",
]


def _resolve_claude_binary() -> str:
    """Resolve the Claude CLI binary path; identical contract to the legacy
    orchestrator so swapping execution paths is a no-op for operators."""
    configured = settings.claude_binary
    if os.path.isabs(configured):
        return configured
    if shutil.which(configured):
        return configured
    for path in _CLAUDE_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            logger.info("Claude binary not on PATH, found at %s", path)
            return path
    return configured  # caller surfaces the FileNotFoundError on spawn


# The system prompt is the operator-facing contract for /claude's behavior —
# scope, clarification protocol, persistence, environment shape.
_SYSTEM_PROMPT = """\
You are being orchestrated by LifeOS on behalf of the user ({user_name}).
The user sent this task via Telegram and cannot see your full output.
Only messages prefixed with [NOTIFY] will be relayed to the user.

CREATIVE TASK INTERPRETATION:
The user is messaging from their phone — requests will be brief and informal.
Think about what they actually want, not just the literal words.
Explore the working directory structure and conventions before making changes.
When in doubt, do more rather than less — the user can't easily follow up from their phone.

SCOPE — keep changes proportional to the ask.
A small ask should be a small change. A bug fix should fix the bug, not redesign
the surrounding system. If you discover the task is bigger than expected
(would touch 4+ files or require significant refactoring), STOP. Send a
[NOTIFY] explaining what you found and what you'd recommend, then make ONLY
the minimal safe change.

CLARIFICATION:
- Use [CLARIFY] to ask a question. Your session will pause and the user's
  answer will be relayed back to you. After sending [CLARIFY], STOP and do
  not continue working.

PERSISTENCE:
- If your first approach doesn't work, try alternatives before giving up.
- Debug errors yourself — read logs, check file contents, inspect state.
- If you have tried 3 or more distinct approaches and none worked, STOP and
  send a [NOTIFY] summarizing what you tried and your best guess at the root cause.

ENVIRONMENT:
- {platform_desc}
- You have full filesystem access
- Git and standard system tools are available

KEY LOCATIONS:
- Obsidian vault: {vault_path}/
- LifeOS project: {code_dir}/LifeOS
- Other projects: {code_dir}/

NOTIFICATIONS — use [NOTIFY] for:
- Completion summaries (ALWAYS include one when done)
- Progress updates on significant milestones
- Errors that block progress after you've tried to resolve them
- Plans before large changes

DELEGATION:
- Your LifeOS agent session id is {session_id}.
- You already have a browser (--chrome), filesystem, and shell. If you want to
  run background work in parallel, delegate it with the `lifeos_agent_spawn`
  MCP tool (pass caller_session_id={session_id}, model="local" or "claude").
  Monitor the child with `lifeos_agent_check` and read its result with
  `lifeos_agent_transcript_read`.
"""


_PLAN_PREFIX = """\
First, create a detailed implementation plan for this task.
Present the complete plan in a single [NOTIFY] message.
After presenting the plan, STOP and do not implement anything.
The user will review and approve the plan before you proceed.

"""


# Reason codes returned in `ExecutorOutcome.reason` for the worker (and tests)
# to discriminate without parsing prose.
REASON_AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
REASON_AWAITING_CLARIFICATION = "awaiting_clarification"
REASON_TIMEOUT = "timeout"
REASON_BINARY_NOT_FOUND = "binary_not_found"


@dataclass
class _RunState:
    """Per-invocation mutable state. Module-level instead of nested so the
    stream-reader thread can mutate it without capture surprises.
    """
    session_id: Optional[str] = None  # CLI's session UUID, captured at init
    final_text: str = ""              # Last assistant text or result text
    pending_clarification: str = ""   # Last [CLARIFY] body
    plan_text: str = ""               # Accumulated [NOTIFY] when plan_mode
    plan_mode: bool = False
    awaiting_approval: bool = False   # plan-mode result event reached
    awaiting_clarification: bool = False
    cost_usd: float = 0.0
    last_activity: str = ""
    notifications_sent: int = 0
    started_at: float = field(default_factory=time.time)
    last_notify_at: float = field(default_factory=time.time)
    # Marker that the result event has been processed — used by the stream
    # reader's fall-through to avoid double-finalizing if the subprocess exits
    # right after we already saw the terminal event.
    terminal: bool = False


def _summarize_tool_call(tool_name: str, tool_input: dict) -> str:
    """Compact human-readable hint for the heartbeat message; mirrors the
    legacy orchestrator so operator-facing strings don't change."""
    if tool_name in ("Read", "read"):
        path = tool_input.get("file_path", "")
        return f"reading {os.path.basename(path)}" if path else "reading a file"
    if tool_name in ("Edit", "edit"):
        path = tool_input.get("file_path", "")
        return f"editing {os.path.basename(path)}" if path else "editing a file"
    if tool_name in ("Write", "write"):
        path = tool_input.get("file_path", "")
        return f"writing {os.path.basename(path)}" if path else "writing a file"
    if tool_name in ("Bash", "bash"):
        cmd = tool_input.get("command", "")
        return f"running `{cmd[:40]}`" if cmd else "running a command"
    if tool_name in ("Grep", "grep"):
        return f"searching for '{tool_input.get('pattern', '')}'"
    if tool_name in ("Glob", "glob"):
        return f"finding files matching '{tool_input.get('pattern', '')}'"
    return f"using {tool_name}"


NotificationCallback = Callable[[str], None]
SpawnFn = Callable[..., subprocess.Popen]


class ClaudeCodeExecutor:
    """Run one Claude Code CLI session synchronously and persist its state.

    Surface mirrors `LocalExecutor`: a single `execute(session, task)` call
    drives the subprocess to a terminal `ExecutorOutcome`. The worker's
    `_dispatch_spawned_sessions` calls this when `session.routing == "claude_code"`.

    Constructor injection points:
      - `notification_callback` — invoked with each [NOTIFY] body during the
        session. The worker passes its Telegram sender in production; tests
        capture into a list. Defaults to a no-op so a stray instantiation
        without wiring won't surface user-visible Telegram chatter.
      - `spawn_fn` / `binary_resolver` — test seams. Override `spawn_fn` to
        return a fake `Popen` with pre-canned stdout, or `binary_resolver`
        to control the CLI path without touching the filesystem.
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
        self._binary_resolver = binary_resolver or _resolve_claude_binary
        self._timeout = timeout_seconds if timeout_seconds is not None else settings.claude_timeout_seconds
        self._heartbeat_interval = heartbeat_interval

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, session, task: dict) -> ExecutorOutcome:
        """Drive a fresh /claude session.

        Reads the prompt from `task["description"]`. `task["working_dir"]`
        and `task["plan_mode"]` are optional; defaults follow the legacy
        orchestrator (cwd of the worker process, plan_mode off).
        """
        prompt = (task.get("description") or "").strip()
        if not prompt:
            self.transcript_store.append(session.session_id, "claude_code_no_prompt", {})
            return ExecutorOutcome(status=STATUS_FAILED, reason="empty prompt")

        plan_mode = bool(task.get("plan_mode"))
        if plan_mode:
            prompt = _PLAN_PREFIX + prompt

        working_dir = task.get("working_dir") or os.getcwd()
        return self._run(
            session=session,
            prompt=prompt,
            working_dir=working_dir,
            resume_session_id=None,
            plan_mode=plan_mode,
        )

    def resume(self, session, message: str, working_dir: Optional[str] = None) -> ExecutorOutcome:
        """Resume a previously-completed /claude session by passing
        `-r <claude_code_session_id>` to the CLI.

        Used by the worker's follow-up reply path — the reply text becomes
        the next user turn on the existing CLI session.
        """
        resume_id = session.claude_code_session_id
        if not resume_id:
            self.transcript_store.append(
                session.session_id, "claude_code_resume_no_session_id", {},
            )
            return ExecutorOutcome(status=STATUS_FAILED, reason="no claude_code_session_id on record")
        wd = working_dir or os.getcwd()
        return self._run(
            session=session,
            prompt=message,
            working_dir=wd,
            resume_session_id=resume_id,
            plan_mode=False,
        )

    # ------------------------------------------------------------------
    # Internal lifecycle
    # ------------------------------------------------------------------

    def _build_command(self, prompt: str, resume_session_id: Optional[str], session_id: str = "") -> list[str]:
        platform_desc = (
            "Linux server running Ubuntu"
            if platform.system() == "Linux"
            else "Mac running macOS"
        )
        cmd = [
            self._binary_resolver(),
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", "opus",
            "--max-turns", str(settings.claude_max_turns),
            "--dangerously-skip-permissions",
            "--chrome",
            "--append-system-prompt", _SYSTEM_PROMPT.format(
                vault_path=settings.vault_path,
                user_name=settings.user_name,
                code_dir=settings.code_dir,
                platform_desc=platform_desc,
                session_id=session_id,
            ),
        ]
        if resume_session_id:
            cmd.extend(["-r", resume_session_id])
        return cmd

    @staticmethod
    def _clean_env() -> dict:
        """Strip CLAUDE* env vars so the subprocess doesn't inherit the
        operator's interactive Claude Code context. Without this, launching
        from a Claude-managed terminal would have the child claim it's
        already inside a session and refuse to run."""
        return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

    def _run(
        self,
        *,
        session,
        prompt: str,
        working_dir: str,
        resume_session_id: Optional[str],
        plan_mode: bool,
    ) -> ExecutorOutcome:
        sid = session.session_id
        cmd = self._build_command(prompt, resume_session_id, session_id=sid)

        self.transcript_store.append(sid, "claude_code_spawn", {
            "resume": bool(resume_session_id),
            "plan_mode": plan_mode,
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
            )
        except FileNotFoundError as exc:
            self.transcript_store.append(sid, "claude_code_binary_not_found", {"error": str(exc)})
            return ExecutorOutcome(
                status=STATUS_FAILED,
                reason=REASON_BINARY_NOT_FOUND,
            )

        # Worker may have created the session in CLAIMED state. Move it to
        # RUNNING for the duration of the subprocess so an /agents-page
        # observer sees the correct status.
        self.session_store.update_status(session.task_id, STATUS_RUNNING)

        state = _RunState(plan_mode=plan_mode)
        timed_out = threading.Event()
        stop_heartbeat = threading.Event()

        # Watchdog: terminate the subprocess if it overruns the wall budget.
        watchdog = threading.Timer(self._timeout, self._on_timeout, args=(proc, timed_out))
        watchdog.daemon = True
        watchdog.start()

        # Heartbeat: periodic progress notification when no [NOTIFY] in the
        # window. Kept on a dedicated thread instead of recursive Timers so
        # cleanup is a single `stop_heartbeat.set()`.
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(state, stop_heartbeat),
            daemon=True,
            name=f"CodeHeartbeat-{sid[:8]}",
        )
        heartbeat_thread.start()

        try:
            # Drain stdout synchronously — we want the call to block until the
            # subprocess finishes (or is killed). This matches `LocalExecutor`'s
            # sync contract; `_dispatch_spawned_sessions` already accepts that
            # long-running children block the tick loop (see worker.py).
            self._consume_stream(proc, session, state)
            proc.wait()
        finally:
            watchdog.cancel()
            stop_heartbeat.set()

        if timed_out.is_set():
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            self.transcript_store.append(sid, "claude_code_timeout", {
                "timeout_seconds": self._timeout,
            })
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_TIMEOUT)

        if state.awaiting_clarification:
            self.session_store.update_status(session.task_id, STATUS_BLOCKED)
            self.transcript_store.append(sid, "claude_code_awaiting_clarification", {
                "question_chars": len(state.pending_clarification),
            })
            return ExecutorOutcome(
                status=STATUS_BLOCKED,
                reason=REASON_AWAITING_CLARIFICATION,
                final_text=state.pending_clarification,
            )

        if state.awaiting_approval:
            self.session_store.update_status(session.task_id, STATUS_BLOCKED)
            self.transcript_store.append(sid, "claude_code_awaiting_plan_approval", {
                "plan_chars": len(state.plan_text),
            })
            return ExecutorOutcome(
                status=STATUS_BLOCKED,
                reason=REASON_AWAITING_PLAN_APPROVAL,
                final_text=state.plan_text.strip(),
            )

        if proc.returncode == 0 or state.terminal:
            self.session_store.update_status(session.task_id, STATUS_COMPLETED)
            self.transcript_store.append(sid, "claude_code_completed", {
                "cost_usd": state.cost_usd,
                "notifications_sent": state.notifications_sent,
                "final_chars": len(state.final_text),
            })
            return ExecutorOutcome(
                status=STATUS_COMPLETED,
                final_text=state.final_text,
            )

        # Subprocess exited non-zero without a terminal event — surface the
        # stderr tail for the operator. Reading stderr after wait() is safe.
        stderr_tail = ""
        try:
            stderr_tail = (proc.stderr.read() if proc.stderr else "") or ""
        except Exception:
            pass
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        self.transcript_store.append(sid, "claude_code_failed", {
            "returncode": proc.returncode,
            "stderr_tail": stderr_tail[-500:],
        })
        return ExecutorOutcome(
            status=STATUS_FAILED,
            reason=f"claude exited with code {proc.returncode}",
        )

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
                # Drain the rest of stdout without re-processing so the
                # subprocess can flush and exit. Without this we'd keep
                # appending duplicate `claude_code_assistant_text` events that
                # arrived after the result.
                for _ in proc.stdout:
                    pass
                break

    def _handle_event(self, event: dict, session, state: _RunState) -> None:
        etype = event.get("type")
        sid = session.session_id

        if etype == "system" and event.get("subtype") == "init":
            cli_session_id = event.get("session_id")
            if cli_session_id:
                state.session_id = cli_session_id
                # Persist immediately so a worker crash mid-session still
                # leaves enough state to resume via `-r <claude_code_session_id>`.
                self.session_store.set_claude_code_session_id(session.task_id, cli_session_id)
                self.transcript_store.append(sid, "claude_code_init", {
                    "claude_code_session_id": cli_session_id,
                })
            return

        if etype == "assistant":
            self._handle_assistant_event(event, session, state)
            return

        if etype == "result":
            self._handle_result_event(event, session, state)
            return

    def _handle_assistant_event(self, event: dict, session, state: _RunState) -> None:
        sid = session.session_id
        content_blocks = event.get("message", {}).get("content", []) or []
        for block in content_blocks:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "") or ""
                if text.strip():
                    # Strip the [NOTIFY]/[CLARIFY] tag-and-body so `final_text`
                    # holds just the agent's narrative prose. Tags themselves
                    # already stream out via the notification callback below;
                    # leaving them in would make the worker's terminal summary
                    # repeat each tagged body verbatim.
                    stripped = _NOTIFY_RE.sub("", text)
                    stripped = _CLARIFY_RE.sub("", stripped).strip()
                    if stripped:
                        state.final_text = stripped
                for match in _CLARIFY_RE.finditer(text):
                    body = match.group(1).strip()
                    if body:
                        state.pending_clarification = body
                        state.notifications_sent += 1
                        state.last_notify_at = time.time()
                        try:
                            self._notify(body)
                        except Exception as exc:  # pragma: no cover — defensive
                            logger.warning("notification callback raised: %s", exc)
                        self.transcript_store.append(sid, "claude_code_clarify", {
                            "body": body, "body_chars": len(body),
                        })
                for match in _NOTIFY_RE.finditer(text):
                    body = match.group(1).strip()
                    if not body:
                        continue
                    state.notifications_sent += 1
                    state.last_notify_at = time.time()
                    try:
                        self._notify(body)
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning("notification callback raised: %s", exc)
                    self.transcript_store.append(sid, "claude_code_notify", {
                        "body": body, "body_chars": len(body),
                    })
                    if state.plan_mode and not state.awaiting_approval:
                        state.plan_text += body + "\n"
            elif btype == "tool_use":
                tool_name = block.get("name", "")
                tool_input = block.get("input", {}) or {}
                state.last_activity = _summarize_tool_call(tool_name, tool_input)
                self.transcript_store.append(sid, "claude_code_tool_use", {
                    "name": tool_name, "input": tool_input,
                })

    def _handle_result_event(self, event: dict, session, state: _RunState) -> None:
        # Some CLI versions only emit the session id on result; refresh.
        cli_session_id = event.get("session_id") or state.session_id
        if cli_session_id and cli_session_id != state.session_id:
            state.session_id = cli_session_id
            self.session_store.set_claude_code_session_id(session.task_id, cli_session_id)

        # Track cost for /agents reporting, but DON'T cap it — the Claude Code
        # route is subscription-billed, so there's no marginal per-task cost to
        # cap. Only the managed/API route enforces max_dollars. Wall-clock and
        # the CLI's own limits still bound runaway sessions.
        state.cost_usd = float(event.get("total_cost_usd") or 0.0)

        if state.pending_clarification:
            # Agent asked a question — surface as BLOCKED outcome so the
            # worker can register the question for follow-up reply routing.
            state.awaiting_clarification = True
            state.terminal = True
            return

        if state.plan_mode:
            state.awaiting_approval = True
            state.terminal = True
            return

        result_text = (event.get("result") or "").strip()
        if result_text:
            # Strip any [NOTIFY]/[CLARIFY] tags that already streamed via
            # assistant events so we don't double-surface them in final_text.
            stripped = _NOTIFY_RE.sub("", result_text)
            stripped = _CLARIFY_RE.sub("", stripped).strip()
            if stripped:
                state.final_text = stripped
        state.terminal = True

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
        # Recursive Timers (as used in the legacy orchestrator) are hard to
        # cancel cleanly from the calling thread because each fires the next.
        # An `Event.wait()` loop drops to zero overhead when stopped.
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
