# Claude Code Orchestration PRD

Execute code tasks remotely via Telegram by spawning headless Claude Code sessions on the Mac Mini.

**Primary Use Cases:**
- Edit files in the Obsidian vault from your phone ("add X to the backlog")
- Create/modify scripts without SSH ("write a cron job for daily backups")
- Make small LifeOS changes on the go ("add a new health check endpoint")
- Any task requiring local file access or command execution

**Related Documentation:**
- [Usage Guide](../guides/CLAUDE-CODE-ORCHESTRATION.md) - Setup, commands, and examples
- [Configuration](../getting-started/CONFIGURATION.md#claude-code-orchestration) - Environment variables
- [Code Structure](../architecture/CODE-STRUCTURE.md) - Source file locations
- [Launchd Setup](../guides/LAUNCHD-SETUP.md) - Server environment context

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Scope and Limitations](#scope-and-limitations)
4. [Commands](#commands)
5. [Plan Mode](#plan-mode)
6. [Directory Resolution](#directory-resolution)
7. [Notification Protocol](#notification-protocol)
8. [Session Lifecycle](#session-lifecycle)
9. [Source Files](#source-files)

---

## Overview

This feature adds a parallel path alongside the existing Telegram chat pipeline. When a task requires **local file access or command execution** — writing scripts, editing files, creating cron jobs — LifeOS spawns a Claude Code CLI session, streams its output, and relays important messages back via Telegram.

The existing chat pipeline handles knowledge queries (calendar, emails, vault search). This handles action tasks that need to touch the filesystem.

**Key Design Decisions:**

| Decision | Rationale |
|----------|-----------|
| Python subprocess, not Node SDK | No polyglot boundary; same language as LifeOS |
| `--output-format stream-json` | Structured event parsing without screen-scraping |
| `[NOTIFY]` string convention | Deterministic prefix check, not LLM-based filtering |
| Always Opus model | Max plan subscription — no API credits consumed |
| One session at a time | Simple locking; avoids filesystem contention |
| Prompt-based plan mode | Avoids interactive plan approval complexity in headless mode |
| Sync `send_message()` in callback | Stream reader thread; sync HTTP is simpler than event loop bridging |

---

## Architecture

```
Telegram message
  → /code <task>
  → TelegramBotListener._handle_command()
  → resolve_working_directory(task)        # keyword → project dir
  → ClaudeOrchestrator.run_task()          # spawns subprocess
      → claude -p <task> --output-format stream-json --verbose
              --model opus --dangerously-skip-permissions
              --append-system-prompt <context>
      → _stream_reader thread parses JSON events
      → [NOTIFY] lines → send_message() → Telegram
      → result event → final summary → Telegram
```

The subprocess runs on the **Mac Mini** (where the LifeOS server runs). The Claude CLI has full filesystem access to the vault, code directories, and system tools.

---

## Scope and Limitations

### What This Is For

- Simple-to-moderate tasks triggered remotely via Telegram
- Tasks requiring local file access (edit vault notes, create scripts)
- Tasks requiring command execution (cron jobs, git operations)
- Quick fixes and additions to LifeOS or other projects

### What This Is NOT For

- Complex multi-session development work (use Claude Code directly via terminal)
- Tasks requiring interactive input or visual review
- Long-running processes (1-hour safety timeout by default; heartbeats provide progress)

### Assumptions

- **Mac Mini is the execution environment.** Claude Code runs where the LifeOS server runs.
- **Claude Max subscription.** The `--model opus` flag uses the Max plan — no API credits consumed, no budget cap needed.
- **Single user.** Only the configured `TELEGRAM_CHAT_ID` can trigger commands.
- **`--dangerously-skip-permissions`** is used because headless mode cannot prompt for tool approvals. The single-user, single-machine context makes this acceptable.
- **File sync delay.** Files edited on the MacBook may take seconds to appear on the Mac Mini (iCloud sync). Claude Code sessions run on the Mac Mini's filesystem directly, so this isn't an issue for tasks triggered from Telegram.

---

## Commands

| Command | Description |
|---------|-------------|
| `/code <task>` | Run a task with Claude Code |
| `/code_status` | Check active session (task, directory, duration, cost) |
| `/code_cancel` | Cancel the active session |

### Examples

```
/code create a file called backup.sh that backs up the LifeOS data directory
/code add "integrate weather alerts" to the backlog
/code write a cron job that runs backup.sh daily at 2am
/code edit the LifeOS README to update the architecture section
```

---

## Plan Mode

For complex tasks, the orchestrator uses a two-phase approach:

**Phase 1 — Planning:**
Claude creates a plan and presents it via `[NOTIFY]`. The session exits and waits for approval.

**Phase 2 — Implementation (on approval):**
The session resumes with `--resume <session_id>` and implements the approved plan.

### Trigger Heuristic

Plan mode activates when the task contains keywords: `refactor`, `implement`, `redesign`, `migrate`, `integrate`, `build a`, `set up a`.

Most tasks default to direct execution (no plan mode).

### Approval Flow

When a plan is presented, reply with a short keyword:
- **Approve:** `approve`, `yes`, `go`, `proceed`, `ok`
- **Reject:** `reject`, `no`, `cancel`, `stop`

Longer messages (questions, clarifications) pass through to the normal chat pipeline, so you can still use LifeOS chat while a plan is pending.

---

## Directory Resolution

The orchestrator automatically resolves a working directory based on task keywords.

| Priority | Keywords | Directory |
|----------|----------|-----------|
| 1 | "notes", "vault", "obsidian", "journal", "backlog", "meeting notes", "daily note" | `~/Notes 2025` |
| 2 | "lifeos", "server", "sync", "chromadb", "telegram bot", "api endpoint" | `~/Documents/Code/LifeOS` |
| 3 | Any project name in `~/Documents/Code/` | That project's directory |
| 4 | "script", "code", "function", "cron" | `~/Documents/Code` |
| 5 | (default) | `~` |

Keywords use word boundary matching to avoid false positives (e.g., "notification" won't match the "note" keyword).

---

## Notification Protocol

Claude Code sessions produce extensive output (tool calls, file reads, edits). Only messages prefixed with `[NOTIFY]` are relayed to Telegram.

The system prompt instructs Claude to use `[NOTIFY]` for:
- Plan summaries and key decision points
- Completion summaries (always — so the user knows it finished)
- Errors that block progress and need user input

Claude can also use `[CLARIFY]` to ask the user a question. When detected, the session pauses (`awaiting_clarification`) and the question is relayed to Telegram. The user's reply resumes the session via `--resume`. This enables back-and-forth on vague tasks without the user needing to re-run the command.

Everything else stays in the subprocess output (logged but not relayed).

**Heartbeat**: A 5-minute repeating timer sends "Still working... (Xm elapsed)" to Telegram if the session is active. This runs independently of Claude's `[NOTIFY]` output, ensuring the user always knows the session is alive.

---

## Session Lifecycle

```
run_task() → status: "running"
  ├─ [NOTIFY] messages → Telegram
  ├─ [CLARIFY] question → status: "awaiting_clarification"
  │    └─ user reply → status: "running" → resume subprocess
  ├─ result event (non-plan) → status: "completed" → Telegram
  └─ result event (plan mode) → status: "awaiting_approval"
       ├─ "approve" → status: "implementing" → resume subprocess
       │    └─ result event → status: "completed" → Telegram
       └─ "reject" → status: "completed"

Timeout (1 hr safety net) → status: "failed" → Telegram
Cancel (/code_cancel) → SIGTERM → status: "failed" → Telegram
Server shutdown → cancel active session
```

Only one session runs at a time. Attempting `/code` while busy returns an error with the active task description.

---

## Source Files

| File | Purpose |
|------|---------|
| `api/services/claude_orchestrator.py` | Core service: subprocess lifecycle, stream parsing, [NOTIFY] extraction, plan mode |
| `api/services/directory_resolver.py` | Maps task keywords to working directories |
| `api/services/telegram.py` | `/code` commands, approval routing, notification wiring |
| `config/settings.py` | `claude_binary`, `claude_timeout_seconds` settings |
| `api/main.py` | Shutdown cleanup for active sessions |
| `tests/test_claude_orchestrator.py` | Unit tests for orchestrator and Telegram integration |
| `tests/test_directory_resolver.py` | Unit tests for directory resolution |
