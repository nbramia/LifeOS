"""
Claude Code orchestrator — spawns and manages headless Claude CLI sessions.

Streams JSON output, extracts [NOTIFY] lines for Telegram relay,
and supports plan-then-implement workflows via session resume.
"""
import json
import logging
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are being orchestrated by LifeOS on behalf of the user (Nathan).
The user sent this task via Telegram and cannot see your full output.
Only messages prefixed with [NOTIFY] will be relayed to the user.

CREATIVE TASK INTERPRETATION:
The user is messaging from their phone — requests will be brief and informal.
Think about what they actually want, not just the literal words.
- "add X to the backlog" → find the backlog file, understand its format, add a properly formatted entry
- "fix the sync" → investigate what's broken, understand the root cause, fix it
- "write a cron job for X" → create the script AND install the cron entry
- "update the readme" → read the current readme, understand the project, make meaningful updates
Explore the working directory structure and conventions before making changes.
When in doubt, do more rather than less — the user can't easily follow up from their phone.

SCOPE — keep changes proportional to the ask:
- Be creative in HOW you solve the task, but don't expand WHAT the task is.
- A small ask ("add X to the backlog") should be a small change. Don't refactor the backlog format.
- A bug fix should fix the bug, not redesign the surrounding system.
- If you discover the task is bigger than expected (would touch 4+ files or require
  significant refactoring), STOP. Send a [NOTIFY] explaining what you found and what
  you'd recommend, then make ONLY the minimal safe change. The user can follow up
  with a larger task if they want the full change.
- Never delete files, drop data, or reorganize project structure unless explicitly asked.
- If you notice adjacent issues, mention them in your completion [NOTIFY] — don't fix them.

CLARIFICATION — ask instead of guessing:
- If the task is vague or ambiguous, DO NOT guess. Ask the user.
- Use [CLARIFY] to ask a question. Your session will pause and the user's answer
  will be relayed back to you. After sending [CLARIFY], STOP and do not continue working.
- Keep questions short and specific. Offer options when possible.
- Example: [CLARIFY] The backlog has two sections (Work and Personal). Which one should I add this to?
- Example: [CLARIFY] I found 3 sync-related errors in the logs. Which one are you seeing? (1) OAuth token expired (2) ChromaDB timeout (3) Slack rate limit

PERSISTENCE:
- If your first approach doesn't work, try alternatives before giving up.
- Debug errors yourself — read logs, check file contents, inspect state.
- The user cannot easily intervene, so exhaust your options before asking for help.
- Only use [NOTIFY] to ask the user for input if you are truly stuck after multiple attempts.

ENVIRONMENT:
- This is a Mac Mini running macOS
- Obsidian vault: ~/Notes 2025 (markdown notes with YAML frontmatter)
- LifeOS project: ~/Documents/Code/LifeOS (has CLAUDE.md with project conventions)
- Other projects: ~/Documents/Code/
- Python venv: ~/.venvs/lifeos (for LifeOS dependencies)
- Git, cron, and standard macOS tools are available

NOTIFICATIONS — use [NOTIFY] for:
- Completion summaries (ALWAYS include one when done)
- Progress updates on significant milestones
- Errors that block progress after you've tried to resolve them
- Plans before large changes (see SCOPE above)

Do NOT use [NOTIFY] for routine tool calls or intermediate steps.
Keep [NOTIFY] messages concise (1-3 sentences).

Example: [NOTIFY] Created backup script at ~/scripts/backup.sh and added daily cron job at 2am."""

_PLAN_PREFIX = """\
First, create a detailed implementation plan for this task.
Present the complete plan in a single [NOTIFY] message.
After presenting the plan, STOP and do not implement anything.
The user will review and approve the plan before you proceed.

"""

_NOTIFY_RE = re.compile(r"\[NOTIFY\]\s*(.+)")
_CLARIFY_RE = re.compile(r"\[CLARIFY\]\s*(.+)")

HEARTBEAT_INTERVAL = 300  # 5 minutes


@dataclass
class ClaudeSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: Optional[str] = None  # Claude's session ID from init event
    task: str = ""
    working_dir: str = ""
    status: str = "running"  # running | awaiting_approval | awaiting_clarification | implementing | completed | failed
    plan_text: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    cost_usd: float = 0.0
    plan_mode: bool = False
    last_notify_at: float = field(default_factory=time.time)
    pending_clarification: str = ""  # Last [CLARIFY] question text


class ClaudeOrchestrator:
    """Manages a single Claude Code subprocess at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active_session: Optional[ClaudeSession] = None
        self._process: Optional[subprocess.Popen] = None
        self._watchdog: Optional[threading.Timer] = None
        self._heartbeat: Optional[threading.Timer] = None
        self._notification_callback: Optional[Callable[[str], None]] = None

    def is_busy(self) -> bool:
        return self._active_session is not None and self._active_session.status in (
            "running", "awaiting_approval", "awaiting_clarification", "implementing",
        )

    def get_active_session(self) -> Optional[ClaudeSession]:
        return self._active_session

    def run_task(
        self,
        task: str,
        working_dir: str,
        plan_mode: bool = False,
        notification_callback: Optional[Callable[[str], None]] = None,
    ) -> ClaudeSession:
        """Spawn a Claude Code subprocess for the given task."""
        with self._lock:
            if self.is_busy():
                raise RuntimeError("A Claude Code session is already active")

            session = ClaudeSession(
                task=task,
                working_dir=working_dir,
                plan_mode=plan_mode,
            )
            self._active_session = session
            self._notification_callback = notification_callback

        prompt = task
        if plan_mode:
            prompt = _PLAN_PREFIX + task

        self._spawn(prompt, session)
        return session

    def approve_plan(self) -> Optional[ClaudeSession]:
        """Resume a plan-mode session to implement the approved plan."""
        with self._lock:
            session = self._active_session
            if not session or session.status != "awaiting_approval":
                return None
            if not session.session_id:
                logger.error("No session_id to resume")
                return None
            session.status = "implementing"

        self._spawn(
            "Approved. Proceed with the implementation.",
            session,
            resume_session_id=session.session_id,
        )
        return session

    def reject_plan(self) -> Optional[ClaudeSession]:
        """Reject and close a pending plan."""
        with self._lock:
            session = self._active_session
            if not session or session.status != "awaiting_approval":
                return None
            session.status = "completed"
            session.completed_at = time.time()
            self._active_session = None
            return session

    def respond_to_clarification(self, answer: str) -> Optional[ClaudeSession]:
        """Resume a session that asked a [CLARIFY] question with the user's answer."""
        with self._lock:
            session = self._active_session
            if not session or session.status != "awaiting_clarification":
                return None
            if not session.session_id:
                logger.error("No session_id to resume for clarification")
                return None
            session.status = "running"
            session.pending_clarification = ""

        self._spawn(
            answer,
            session,
            resume_session_id=session.session_id,
        )
        return session

    def cancel(self):
        """Cancel the active session."""
        proc = self._process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._cleanup("failed")
        if self._notification_callback:
            self._notification_callback("Claude Code session cancelled.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _spawn(self, prompt: str, session: ClaudeSession, resume_session_id: str = None):
        """Build the CLI command and spawn the subprocess."""
        cmd = [
            settings.claude_binary,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", "opus",
            "--dangerously-skip-permissions",
            "--chrome",
            "--append-system-prompt", _SYSTEM_PROMPT,
        ]
        if resume_session_id:
            cmd.extend(["-r", resume_session_id])

        logger.info(f"Spawning Claude Code in {session.working_dir}: {session.task[:80]}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=session.working_dir,
                text=True,
            )
        except FileNotFoundError:
            logger.error(f"Claude binary not found at {settings.claude_binary}")
            self._cleanup("failed")
            if self._notification_callback:
                self._notification_callback(f"Error: Claude binary not found at {settings.claude_binary}")
            return

        # Start stream reader thread
        reader = threading.Thread(
            target=self._stream_reader,
            args=(self._process, session),
            daemon=True,
            name=f"ClaudeStream-{session.id}",
        )
        reader.start()

        # Start watchdog timer
        if self._watchdog:
            self._watchdog.cancel()
        self._watchdog = threading.Timer(
            settings.claude_timeout_seconds,
            self._on_timeout,
            args=(session,),
        )
        self._watchdog.daemon = True
        self._watchdog.start()

        # Start heartbeat timer
        self._start_heartbeat(session)

    def _stream_reader(self, proc: subprocess.Popen, session: ClaudeSession):
        """Read and parse stream-json output from the Claude subprocess."""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                self._handle_event(event, session)

            proc.wait()

            if proc.returncode != 0:
                stderr_out = proc.stderr.read() if proc.stderr else ""
                if stderr_out:
                    logger.warning(f"Claude stderr: {stderr_out[:500]}")

            # If session is still running (no result event received), mark complete
            if session.status in ("running", "implementing"):
                if proc.returncode != 0 and self._notification_callback:
                    self._notification_callback(
                        f"Claude Code exited with code {proc.returncode}."
                    )
                    self._cleanup("failed")
                else:
                    if self._notification_callback:
                        self._notification_callback("Claude Code session completed.")
                    self._cleanup("completed")
            elif session.status in ("awaiting_approval", "awaiting_clarification"):
                pass  # Stay in waiting state — user will respond

        except Exception as e:
            logger.error(f"Stream reader error: {e}")
            self._cleanup("failed")
            if self._notification_callback:
                self._notification_callback(f"Claude Code session failed: {e}")

    def _handle_event(self, event: dict, session: ClaudeSession):
        """Process a single stream-json event."""
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            session.session_id = event.get("session_id")
            logger.info(f"Claude session started: {session.session_id}")

        elif etype == "assistant":
            # Extract [NOTIFY] and [CLARIFY] lines from message content
            content_blocks = event.get("message", {}).get("content", [])
            for block in content_blocks:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    for match in _CLARIFY_RE.finditer(text):
                        clarify_text = match.group(1).strip()
                        if clarify_text:
                            session.pending_clarification = clarify_text
                            if self._notification_callback:
                                self._notification_callback(clarify_text)
                                session.last_notify_at = time.time()
                    for match in _NOTIFY_RE.finditer(text):
                        notify_text = match.group(1).strip()
                        if notify_text and self._notification_callback:
                            self._notification_callback(notify_text)
                            session.last_notify_at = time.time()
                        # For plan mode, accumulate plan text
                        if session.plan_mode and session.status == "running":
                            session.plan_text += notify_text + "\n"

        elif etype == "result":
            session.session_id = event.get("session_id", session.session_id)
            session.cost_usd = event.get("total_cost_usd", 0.0)

            result_text = event.get("result", "")

            if session.pending_clarification:
                # Claude asked a question — wait for user's answer
                session.status = "awaiting_clarification"
            elif session.plan_mode and session.status == "running":
                # Plan phase complete — await approval
                session.status = "awaiting_approval"
                if self._notification_callback:
                    self._notification_callback(
                        "Reply 'approve' to proceed or 'reject' to cancel."
                    )
            else:
                # Task complete
                if self._notification_callback:
                    if result_text:
                        self._notification_callback(result_text[:3000])
                    else:
                        self._notification_callback("Claude Code session completed.")
                self._cleanup("completed")

    def _on_timeout(self, session: ClaudeSession):
        """Kill the subprocess if it exceeds the timeout."""
        logger.warning(f"Claude session timed out after {settings.claude_timeout_seconds}s")
        proc = self._process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._cleanup("failed")
        if self._notification_callback:
            self._notification_callback(
                f"Claude Code session timed out after {settings.claude_timeout_seconds // 60} minutes."
            )

    def _start_heartbeat(self, session: ClaudeSession):
        """Start a repeating heartbeat that pings Telegram if no [NOTIFY] recently."""
        if self._heartbeat:
            self._heartbeat.cancel()
        self._heartbeat = threading.Timer(
            HEARTBEAT_INTERVAL,
            self._on_heartbeat,
            args=(session,),
        )
        self._heartbeat.daemon = True
        self._heartbeat.start()

    def _on_heartbeat(self, session: ClaudeSession):
        """Send a progress update if no [NOTIFY] was sent in the last interval."""
        if session.status not in ("running", "implementing"):
            return

        elapsed = int(time.time() - session.started_at)
        minutes = elapsed // 60

        if self._notification_callback:
            self._notification_callback(
                f"Still working... ({minutes}m elapsed)"
            )
            session.last_notify_at = time.time()

        # Schedule next heartbeat
        self._start_heartbeat(session)

    def _cleanup(self, final_status: str):
        """Mark session complete and release lock."""
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        session = self._active_session
        if session and session.status not in ("completed", "failed", "awaiting_approval", "awaiting_clarification"):
            session.status = final_status
            session.completed_at = time.time()
        if final_status in ("completed", "failed"):
            self._active_session = None
        self._process = None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_orchestrator: Optional[ClaudeOrchestrator] = None


def get_orchestrator() -> ClaudeOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ClaudeOrchestrator()
    return _orchestrator
