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
from typing import Callable, NamedTuple, Optional

from api.services.agent_worker.delegation import delegation_preamble
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


_NOTIFY_RE = re.compile(r"\[NOTIFY\]\s*(.*?)(?=\[(?:NOTIFY|CLARIFY|GOAL)\]|\Z)", re.DOTALL)
_CLARIFY_RE = re.compile(r"\[CLARIFY\]\s*(.*?)(?=\[(?:NOTIFY|CLARIFY|GOAL)\]|\Z)", re.DOTALL)
# [GOAL] proposes a success condition for the operator to approve; once
# approved, the worker injects `/goal <condition>` at resume (#398). It mirrors
# the [NOTIFY]/[CLARIFY] split so an interleaved GOAL doesn't bleed into an
# adjacent tag's body.
_GOAL_RE = re.compile(r"\[GOAL\]\s*(.*?)(?=\[(?:NOTIFY|CLARIFY|GOAL)\]|\Z)", re.DOTALL)
# Fenced code blocks: a fence marker (``` or ~~~) at the START of a line
# through its matching closing fence on its own line. Tags inside these are
# illustrative — the agent quoting the protocol or showing example output — so
# they must NOT be treated as real notifications nor stripped from the
# narrative (#402). Anchoring to line starts (proper Markdown semantics) means
# an *inline* triple-backtick span inside a tag body is not mistaken for a
# fence and so doesn't truncate the body. Tag bodies are short operator-facing
# prose by contract, so a multi-line fenced block embedded *inside* a tag body
# is out of scope (it would split at the fence — acceptable for short tags).
# Only *balanced* fences match; an unclosed fence falls back to plain scanning,
# so a real tag after a stray fence marker still surfaces (the safe direction).
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
# Orphaned/malformed control-tag markers left after well-formed extraction
# (e.g. an unclosed "[NOTIFY" with no closing bracket). Scrubbed from the
# operator-facing narrative so raw control tokens never leak (#402). The
# closing bracket is optional to catch unclosed tags; the (?![A-Za-z]) boundary
# stops it from eating the prefix of unrelated words like "[NOTIFYING ...]".
_ORPHAN_TAG_RE = re.compile(r"\[(?:NOTIFY|CLARIFY|GOAL)(?![A-Za-z])\]?")


class _TagScan(NamedTuple):
    """Result of a fence-aware scan of assistant text for control tags.

    ``clarify`` / ``notify`` / ``goal`` are the non-empty tag bodies in document
    order, drawn only from outside fenced code blocks. ``narrative`` is the text
    with all control tags + orphaned markers removed from non-fenced regions and
    fenced code blocks preserved verbatim — i.e. the agent's prose as the
    operator should see it.
    """
    clarify: list[str]
    notify: list[str]
    goal: list[str]
    narrative: str


def _scan_segment(
    seg: str, clarify: list[str], notify: list[str], goal: list[str], parts: list[str]
) -> None:
    """Extract tag bodies from one non-fenced segment and append its cleaned
    narrative to ``parts``."""
    for match in _CLARIFY_RE.finditer(seg):
        body = match.group(1).strip()
        if body:
            clarify.append(body)
    for match in _NOTIFY_RE.finditer(seg):
        body = match.group(1).strip()
        if body:
            notify.append(body)
    for match in _GOAL_RE.finditer(seg):
        body = match.group(1).strip()
        if body:
            goal.append(body)
    cleaned = _NOTIFY_RE.sub("", seg)
    cleaned = _CLARIFY_RE.sub("", cleaned)
    cleaned = _GOAL_RE.sub("", cleaned)
    cleaned = _ORPHAN_TAG_RE.sub("", cleaned)
    parts.append(cleaned)


def _scan_protocol_tags(text: str) -> _TagScan:
    """Fence-aware extraction of ``[NOTIFY]``/``[CLARIFY]``/``[GOAL]`` tags from
    assistant text. Tags inside fenced code blocks are left untouched (neither
    extracted nor stripped); outside fences, well-formed bodies are extracted and
    the tags plus any orphaned/malformed markers are stripped from the narrative."""
    clarify: list[str] = []
    notify: list[str] = []
    goal: list[str] = []
    parts: list[str] = []
    pos = 0
    for fence in _FENCE_RE.finditer(text):
        _scan_segment(text[pos:fence.start()], clarify, notify, goal, parts)
        parts.append(text[fence.start():fence.end()])  # fenced block, verbatim
        pos = fence.end()
    _scan_segment(text[pos:], clarify, notify, goal, parts)
    return _TagScan(clarify, notify, goal, "".join(parts))


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
{clarification}

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
You already have a browser (--chrome), filesystem, and shell. {delegation}
"""


# Operator sessions really do pause on [CLARIFY] — the worker goes BLOCKED and
# relays the Telegram reply. A spawned child does NOT (#356): its question is
# folded into its output as "[needs clarification] …" and the turn completes;
# the parent may answer via lifeos_agent_send, which resumes the child's CLI
# session. Each variant states only what actually happens to that session.
_CLARIFY_OPERATOR = """\
- Use [CLARIFY] to ask a question. Your session will pause and the user's
  answer will be relayed back to you. After sending [CLARIFY], STOP and do
  not continue working."""

_CLARIFY_CHILD = """\
- Use [CLARIFY] to ask a question. Your turn will end and the question is
  delivered to the parent agent that spawned you; if it answers, you will be
  resumed with the answer as your next turn. After sending [CLARIFY], STOP
  and do not continue working."""


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
REASON_AWAITING_GOAL_APPROVAL = "awaiting_goal_approval"
REASON_TIMEOUT = "timeout"
REASON_BINARY_NOT_FOUND = "binary_not_found"
# #379: the operator kill endpoint flips the session row to FAILED and signals
# our subprocess. When `proc.wait()` returns after such a kill, we exit silently
# under this reason so the worker doesn't fire a spurious "session failed" notice.
REASON_KILLED = "killed"


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
    # True when this session was spawned by another agent (has a parent). Child
    # sessions report back to their parent, not the operator: their [NOTIFY]
    # bodies and heartbeats are NOT streamed to Telegram (#349). Instead the
    # bodies accumulate in `notify_bodies` and get folded into final_text so the
    # parent — which only reads final_text — still receives the substance.
    is_child: bool = False
    notify_bodies: list[str] = field(default_factory=list)
    awaiting_approval: bool = False   # plan-mode result event reached
    awaiting_clarification: bool = False
    pending_goal: str = ""            # Last [GOAL] body, awaiting approval (#398)
    awaiting_goal: bool = False
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
        operator_send: Optional[Callable[[object, str], None]] = None,
        conversation_mirror: Optional[Callable[[str, str], None]] = None,
        spawn_fn: Optional[SpawnFn] = None,
        binary_resolver: Optional[Callable[[], str]] = None,
        timeout_seconds: Optional[int] = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
    ) -> None:
        self.session_store = session_store
        self.transcript_store = transcript_store
        self._notify = notification_callback or (lambda _msg: None)
        # Preferred operator-facing sender: called with (session, body) so the
        # worker can capture Telegram message ids and register them as reply
        # anchors (threaded replies route back into the session). Falls back to
        # the plain `notification_callback` when unset (tests, legacy wiring).
        self._operator_send = operator_send
        # #311: optional (session_id, body) sink that mirrors each streamed
        # [NOTIFY]/[CLARIFY]/[GOAL] body into the web/voice conversation thread
        # that spawned the session. Additive — `notification_callback` (the
        # Telegram relay) is unchanged. None → no mirroring (the default; tests
        # and non-web sessions are unaffected).
        self._conversation_mirror = conversation_mirror
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

    def _build_command(
        self,
        prompt: str,
        resume_session_id: Optional[str],
        session_id: str = "",
        model: str = "opus",
        is_child: bool = False,
    ) -> list[str]:
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
            "--model", model,
            "--max-turns", str(settings.claude_max_turns),
            "--dangerously-skip-permissions",
            "--chrome",
            "--append-system-prompt", _SYSTEM_PROMPT.format(
                vault_path=settings.vault_path,
                user_name=settings.user_name,
                code_dir=settings.code_dir,
                platform_desc=platform_desc,
                clarification=_CLARIFY_CHILD if is_child else _CLARIFY_OPERATOR,
                delegation=delegation_preamble(
                    session_id,
                    trigger="To run background work in parallel,",
                    model='"local" or "claude"',
                ),
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

    @staticmethod
    def _effective_final_text(state: _RunState) -> str:
        """Text handed back to the worker as the session's result.

        For child sessions (#349) the [NOTIFY] bodies never streamed to the
        operator, so fold them into final_text — the parent only reads
        final_text and would otherwise lose everything the child reported.
        Operator sessions return final_text unchanged (their notifies already
        streamed live, and are deliberately stripped to avoid repetition).
        """
        if not state.is_child or not state.notify_bodies:
            return state.final_text
        parts = list(state.notify_bodies)
        prose = (state.final_text or "").strip()
        if prose and prose not in parts:
            parts.append(prose)
        return "\n\n".join(parts).strip()

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
        cmd = self._build_command(
            prompt, resume_session_id, session_id=sid,
            model=session.claude_code_model or "opus",
            is_child=bool(session.parent_session_id),
        )

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
                # #379: own session/process-group leader so the operator kill can
                # `os.killpg(pgid, ...)` the CLI + every child it spawns WITHOUT
                # touching this worker process (which shares the worker's group).
                start_new_session=True,
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

        # #379: record the subprocess PID + process-group id so the operator
        # kill endpoint (a separate process) can signal it. `start_new_session`
        # makes pid==pgid, but resolve the pgid explicitly so the teardown can
        # `killpg` the whole group. Separate from the `claude_code_spawn` marker
        # above, which is the #400 crash-guard written *before* the Popen.
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:  # pragma: no cover — defensive; fall back to the pid
            pgid = proc.pid
        self.transcript_store.append(sid, "claude_code_pid", {"pid": proc.pid, "pgid": pgid})

        state = _RunState(plan_mode=plan_mode, is_child=bool(session.parent_session_id))
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
            args=(session, state, stop_heartbeat),
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

        # #379: the kill endpoint flips status to FAILED and signals our
        # subprocess. If the row is already FAILED *and the subprocess did not
        # exit cleanly*, treat it as an operator kill — exit silently
        # (REASON_KILLED) so the worker doesn't fire a spurious "session failed"
        # notice (the killpg'd subprocess returns a negative returncode, which
        # would otherwise fall through to the FAILED path below).
        #
        # The row can be flipped to FAILED mid-run by two legitimate writers: the
        # operator kill (signals the subprocess → non-zero/negative returncode)
        # and `LocalExecutor._cascade_kill_lineage` on a lineage-budget breach
        # (routing-agnostic — it flips non-terminal CLI descendants too). The
        # returncode gate keeps these from clobbering a clean completion: a
        # subprocess that exited 0 *finished its work* and falls through to the
        # COMPLETED path so its final_text is persisted (a parent reads it via
        # _child_final_text), even if a cascade raced the FAILED flip in.
        current = self.session_store.get(session.task_id)
        if current is not None and current.status == STATUS_FAILED and proc.returncode != 0:
            self.transcript_store.append(sid, "claude_code_killed", {"returncode": proc.returncode})
            return ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED)

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

        if state.awaiting_goal:
            self.session_store.update_status(session.task_id, STATUS_BLOCKED)
            self.transcript_store.append(sid, "claude_code_awaiting_goal_approval", {
                "condition": state.pending_goal, "condition_chars": len(state.pending_goal),
            })
            return ExecutorOutcome(
                status=STATUS_BLOCKED,
                reason=REASON_AWAITING_GOAL_APPROVAL,
                final_text=state.pending_goal,
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
            final_text = self._effective_final_text(state)
            self.session_store.update_status(session.task_id, STATUS_COMPLETED)
            self.transcript_store.append(sid, "claude_code_completed", {
                "cost_usd": state.cost_usd,
                "notifications_sent": state.notifications_sent,
                "final_chars": len(final_text),
                # Persist the text itself so a parent that spawned this session
                # can read it via _child_final_text — for children the bodies
                # never streamed to Telegram, so this is their only path out (#349).
                "final_text": final_text,
            })
            return ExecutorOutcome(
                status=STATUS_COMPLETED,
                final_text=final_text,
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
                if not text.strip():
                    continue
                # Fence-aware scan: extract [NOTIFY]/[CLARIFY] bodies from
                # outside code fences, and keep `final_text` as the agent's
                # narrative with tags (and any orphaned markers) stripped. Tags
                # inside ``` fences are illustrative and left untouched (#402).
                # Streaming the tags out here would otherwise make the worker's
                # terminal summary repeat each tagged body verbatim.
                scan = _scan_protocol_tags(text)
                if scan.narrative.strip():
                    state.final_text = scan.narrative.strip()
                for body in scan.clarify:
                    state.notifications_sent += 1
                    state.last_notify_at = time.time()
                    if state.is_child:
                        # #356: a spawned child must not pause on an operator reply.
                        # The operator owns no thread to a child, and a BLOCKED child
                        # would strand its yielded parent (which only resumes once
                        # every child is terminal). So fold the question into the
                        # child's output — prefixed so the parent recognizes it as a
                        # request, like [NOTIFY] folding (#349) — and let the turn
                        # complete. The PARENT, which owns the operator conversation,
                        # reads it on resume and decides (answer via a re-spawn,
                        # relay to the operator, or proceed). Crucially DON'T set
                        # pending_clarification: no BLOCKED outcome, no operator notify.
                        state.notify_bodies.append(f"[needs clarification] {body}")
                        self.transcript_store.append(sid, "claude_code_child_clarify_folded", {
                            "body": body, "body_chars": len(body),
                        })
                        continue
                    state.pending_clarification = body
                    # NOT streamed to Telegram here: the worker sends ONE
                    # anchored message — question + reply instructions — when
                    # the session blocks, so the operator's threaded reply
                    # lands on the message that shows the question itself
                    # (same treatment [GOAL] gets). The web thread still gets
                    # the body live via the mirror.
                    if self._conversation_mirror:  # #311: stream into the web thread
                        try:
                            self._conversation_mirror(session.session_id, body)
                        except Exception as exc:  # pragma: no cover — defensive
                            logger.warning("conversation mirror raised: %s", exc)
                    self.transcript_store.append(sid, "claude_code_clarify", {
                        "body": body, "body_chars": len(body),
                    })
                for body in scan.notify:
                    state.notifications_sent += 1
                    state.last_notify_at = time.time()
                    state.notify_bodies.append(body)
                    # Child sessions don't stream to the operator — their bodies
                    # are folded into final_text for the parent instead (#349).
                    if not state.is_child:
                        try:
                            if self._operator_send is not None:
                                self._operator_send(session, body)
                            else:
                                self._notify(body)
                        except Exception as exc:  # pragma: no cover — defensive
                            logger.warning("notification callback raised: %s", exc)
                        if self._conversation_mirror:  # #311: stream into the web thread
                            try:
                                self._conversation_mirror(session.session_id, body)
                            except Exception as exc:  # pragma: no cover — defensive
                                logger.warning("conversation mirror raised: %s", exc)
                    self.transcript_store.append(sid, "claude_code_notify", {
                        "body": body, "body_chars": len(body),
                    })
                    if state.plan_mode and not state.awaiting_approval:
                        state.plan_text += body + "\n"
                for body in scan.goal:
                    # The agent proposed a success condition (#398). NOT
                    # streamed to Telegram here: the worker sends ONE anchored
                    # message — goal body + reply instructions — when the
                    # session blocks, so the operator's threaded reply lands on
                    # the message that shows the goal itself (previously the
                    # goal streamed as its own message and a separate
                    # instruction message was the reply anchor, which made
                    # "reply yes — but to which message?" ambiguous). The web
                    # thread still gets the body live via the mirror. The
                    # notify timestamps still advance so the heartbeat doesn't
                    # race the blocked-prompt send with a "Still working".
                    state.pending_goal = body
                    state.notifications_sent += 1
                    state.last_notify_at = time.time()
                    if not state.is_child and self._conversation_mirror:
                        try:  # #311: stream into the web thread
                            self._conversation_mirror(session.session_id, body)
                        except Exception as exc:  # pragma: no cover — defensive
                            logger.warning("conversation mirror raised: %s", exc)
                    self.transcript_store.append(sid, "claude_code_goal", {
                        "body": body, "body_chars": len(body),
                    })
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

        if state.pending_goal:
            # Agent proposed a goal — block for operator approval (#398). On
            # approval the worker injects `/goal <condition>` at resume.
            # Clarification is checked first above: a same-turn [CLARIFY]
            # supersedes [GOAL], so the goal must be re-proposed after the
            # clarification resolves.
            state.awaiting_goal = True
            state.terminal = True
            return

        if state.plan_mode:
            state.awaiting_approval = True
            state.terminal = True
            return

        result_text = (event.get("result") or "").strip()
        if result_text:
            # Strip any [NOTIFY]/[CLARIFY] tags (fence-aware) that already
            # streamed via assistant events so we don't double-surface them.
            narrative = _scan_protocol_tags(result_text).narrative.strip()
            if narrative:
                state.final_text = narrative
        state.terminal = True

    # ------------------------------------------------------------------
    # Watchdog + heartbeat
    # ------------------------------------------------------------------

    @staticmethod
    def _on_timeout(proc, timed_out: threading.Event) -> None:
        # MUST NOT write a terminal status to the session row. The #379 kill-guard
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

    def _heartbeat_loop(self, session, state: _RunState, stop_event: threading.Event) -> None:
        # Recursive Timers (as used in the legacy orchestrator) are hard to
        # cancel cleanly from the calling thread because each fires the next.
        # An `Event.wait()` loop drops to zero overhead when stopped.
        while not stop_event.wait(self._heartbeat_interval):
            if state.terminal:
                return
            now = time.time()
            if now - state.last_notify_at < self._heartbeat_interval:
                continue
            # Child sessions stay silent to the operator — the parent reports
            # progress on their behalf (#349).
            if state.is_child:
                state.last_notify_at = now
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
