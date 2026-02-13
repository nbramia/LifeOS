"""
Claude Code orchestrator — spawns and manages headless Claude CLI sessions.

Streams JSON output, extracts [NOTIFY] lines for Telegram relay,
and supports plan-then-implement workflows via session resume.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

from config.settings import settings

TELEGRAM_API = "https://api.telegram.org"

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
- NEVER respond saying you don't have context or can't find information without first
  searching for it. If asked about a person, search the vault and LifeOS API. If asked
  about a project, explore the filesystem. Always try before saying you can't.
- IMPORTANT: If you have tried 3 or more distinct approaches and none worked, STOP.
  Send a [NOTIFY] summarizing: (1) what you tried, (2) what failed, (3) your best guess
  at the root cause. Let the user decide next steps rather than looping indefinitely.

ENVIRONMENT:
- Mac Mini running macOS
- You have full filesystem access — you can read, write, search, and edit any file
- You have a browser (Chrome) for web tasks
- Git, cron, and standard macOS tools are available
- Python venv: ~/.venvs/lifeos (for LifeOS dependencies)

KEY LOCATIONS:
- Obsidian vault: ~/Notes 2025/ — Nathan's personal knowledge base. Markdown files with YAML frontmatter.
  - People/Name.md — files about specific people (contact info, notes, facts)
  - Daily logs — date-named files (e.g. 2026-01-12.md) with journal entries, meeting notes
  - Meeting notes, project docs, task files
  You can read these files directly with Read/Glob/Grep. Use this when you know the filename
  or want full file content. Use the LifeOS MCP search tools when you need to search across
  many files or need structured data like entity_ids and relationship scores.
- LifeOS project: ~/Documents/Code/LifeOS (has CLAUDE.md with project conventions)
- Other projects: ~/Documents/Code/

LIFEOS DATA ACCESS:
You have LifeOS MCP tools for searching Nathan's personal data. Always use these
before saying you don't have information. The data is there — find it.

People tools (always start with lifeos_people_search to get entity_id):
- lifeos_people_search: Find a person by name or email. Returns entity_id needed by all other people tools.
- lifeos_person_profile: Full CRM profile — emails, phones, company, relationship strength, tags, notes. Use for contact details.
- lifeos_person_timeline: Chronological interaction history — emails, messages, meetings in time order. Use for "catch me up on X" or "what's been happening with Y."
- lifeos_person_facts: Extracted facts organized by category (family, interests, work, dates). Use for personal details like birthdays, hobbies, family members.
- lifeos_person_connections: People connected through shared meetings, emails, Slack. Use for "who does X work with?"
- lifeos_relationship_insights: Relationship patterns and observations from notes. Use for understanding dynamics.
- lifeos_communication_gaps: Find people you haven't contacted recently. Requires comma-separated entity_ids.
- lifeos_photos_person: Photos containing a specific person from Apple Photos.
- lifeos_photos_shared: Photos where two people appear together.

Search tools:
- lifeos_search: Raw vault search — returns document chunks with relevance scores. Use when you need specific documents.
- lifeos_ask: RAG search with Claude synthesis — returns a natural language answer. Use for open-ended questions about notes.
- lifeos_gmail_search: Search Gmail across personal and work accounts. Returns email metadata and body.
- lifeos_imessage_search: Search iMessage/SMS history. Filter by entity_id, phone, date range, or text.
- lifeos_slack_search: Semantic search across Slack DMs and channels.
- lifeos_calendar_search: Search past and future calendar events by keyword.
- lifeos_calendar_upcoming: Get upcoming events for the next N days.
- lifeos_drive_search: Search Google Drive files by name or content.

Finance tools (Monarch Money — live data):
- lifeos_monarch_accounts: List all financial accounts with current balances. Returns account name, type (checking, savings, credit card, investment), balance, and institution.
- lifeos_monarch_transactions: Search recent transactions. Filter by start_date, end_date, category, or merchant name (search param). Returns date, merchant, category, amount, account. Defaults to last 30 days.
- lifeos_monarch_cashflow: Cashflow summary for a date range. Returns total income, expenses, savings rate, and spending breakdown by category. Defaults to current month.
- lifeos_monarch_budgets: Current budget status. Returns each budget category with budgeted amount, actual spending, and remaining balance. Defaults to current month.
Note: Monthly financial summaries are also synced to the vault at Personal/Finance/Monarch/YYYY-MM.md — searchable via lifeos_search.

Action tools:
- lifeos_task_create/list/update/complete/delete: Manage Obsidian tasks.
- lifeos_reminder_create/list/delete: Manage scheduled Telegram reminders.
- lifeos_gmail_draft: Create a draft email in Gmail (not sent, user reviews first).
- lifeos_calendar_create: Create a Google Calendar event. No invite emails sent. [CLARIFY] with user before creating.
- lifeos_calendar_update: Update an existing calendar event (title, time, attendees, etc.). Requires event_id from lifeos_calendar_search. [CLARIFY] before updating.
- lifeos_calendar_delete: Delete a calendar event. Requires event_id from lifeos_calendar_search. [CLARIFY] before deleting.
- lifeos_telegram_send: Send an immediate message via Telegram.
- lifeos_meeting_prep: Get intelligent meeting preparation context for a date.
- lifeos_memories_create/search: Save and retrieve persistent memories.

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


def _summarize_tool_call(tool_name: str, tool_input: dict) -> str:
    """Create a brief human-readable summary of a tool call for heartbeat context."""
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
        # Show first 40 chars of command
        return f"running `{cmd[:40]}`" if cmd else "running a command"
    if tool_name in ("Grep", "grep"):
        return f"searching for '{tool_input.get('pattern', '')}'"
    if tool_name in ("Glob", "glob"):
        return f"finding files matching '{tool_input.get('pattern', '')}'"
    return f"using {tool_name}"


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
    last_activity: str = ""  # Brief description of last tool/action for heartbeat context
    notifications_sent: int = 0  # Count of [NOTIFY] messages relayed during this session


FOLLOWUP_WINDOW = 300  # 5 minutes to send follow-ups to completed sessions


class ClaudeOrchestrator:
    """Manages a single Claude Code subprocess at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active_session: Optional[ClaudeSession] = None
        self._last_completed: Optional[ClaudeSession] = None
        self._process: Optional[subprocess.Popen] = None
        self._watchdog: Optional[threading.Timer] = None
        self._heartbeat: Optional[threading.Timer] = None
        self._typing_stop: Optional[threading.Event] = None
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

    def get_recent_completed_session(self) -> Optional[ClaudeSession]:
        """Return the last completed session if it finished within FOLLOWUP_WINDOW."""
        session = self._last_completed
        if not session or not session.completed_at:
            return None
        if time.time() - session.completed_at > FOLLOWUP_WINDOW:
            return None
        return session

    def followup(
        self,
        message: str,
        notification_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[ClaudeSession]:
        """Resume a recently completed session with a follow-up message."""
        with self._lock:
            if self.is_busy():
                return None
            session = self.get_recent_completed_session()
            if not session or not session.session_id:
                return None

            # Reactivate the session
            session.status = "running"
            session.completed_at = None
            self._active_session = session
            self._last_completed = None
            self._notification_callback = notification_callback

        self._spawn(message, session, resume_session_id=session.session_id)
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
            "--max-turns", str(settings.claude_max_turns),
            "--dangerously-skip-permissions",
            "--chrome",
            "--append-system-prompt", _SYSTEM_PROMPT,
        ]
        if resume_session_id:
            cmd.extend(["-r", resume_session_id])

        logger.info(f"Spawning Claude Code in {session.working_dir}: {session.task[:80]}")

        # Strip Claude Code env vars so the child process doesn't think
        # it's inside an existing session (happens when server was started
        # from a Claude Code terminal).
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("CLAUDE")}

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=session.working_dir,
                text=True,
                env=clean_env,
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

        # Start typing indicator
        self._start_typing()

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
                            session.notifications_sent += 1
                        # For plan mode, accumulate plan text
                        if session.plan_mode and session.status == "running":
                            session.plan_text += notify_text + "\n"
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    session.last_activity = _summarize_tool_call(tool_name, tool_input)

        elif etype == "result":
            session.session_id = event.get("session_id", session.session_id)
            session.cost_usd = event.get("total_cost_usd", 0.0)

            # Cost cap check
            if session.cost_usd > settings.claude_max_cost_usd:
                logger.warning(f"Claude session exceeded cost cap: ${session.cost_usd:.2f} > ${settings.claude_max_cost_usd:.2f}")
                proc = self._process
                if proc and proc.poll() is None:
                    proc.terminate()
                self._cleanup("failed")
                if self._notification_callback:
                    self._notification_callback(
                        f"Session stopped — cost cap exceeded (${session.cost_usd:.2f}/{settings.claude_max_cost_usd:.2f}). Use /code to retry."
                    )
                return

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
                # Task complete — only send a fallback if no [NOTIFY] was
                # already relayed.  Never send raw result_text: it contains
                # reasoning/thinking plus duplicate [NOTIFY] content.
                if self._notification_callback and not session.notifications_sent:
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
            activity = f" — {session.last_activity}" if session.last_activity else ""
            cost = f" | ${session.cost_usd:.2f}" if session.cost_usd > 0 else ""
            self._notification_callback(
                f"Still working{activity} ({minutes}m elapsed{cost})"
            )
            session.last_notify_at = time.time()

        # Schedule next heartbeat
        self._start_heartbeat(session)

    def _start_typing(self):
        """Send Telegram typing indicator every 4 seconds while session is active."""
        if self._typing_stop:
            self._typing_stop.set()
        self._typing_stop = threading.Event()
        stop = self._typing_stop

        def _typing_loop():
            chat_id = settings.telegram_chat_id
            while not stop.wait(4):
                try:
                    httpx.post(
                        f"{TELEGRAM_API}/bot{settings.telegram_bot_token}/sendChatAction",
                        json={"chat_id": chat_id, "action": "typing"},
                        timeout=5.0,
                    )
                except Exception:
                    pass

        t = threading.Thread(target=_typing_loop, daemon=True, name="TypingIndicator")
        t.start()

    def _cleanup(self, final_status: str):
        """Mark session complete and release lock."""
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        if self._typing_stop:
            self._typing_stop.set()
            self._typing_stop = None
        session = self._active_session
        if session and session.status not in ("completed", "failed", "awaiting_approval", "awaiting_clarification"):
            session.status = final_status
            session.completed_at = time.time()
        if final_status in ("completed", "failed"):
            # Keep completed sessions for follow-up resumption
            if final_status == "completed" and session and session.session_id:
                self._last_completed = session
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
