# Claude Code Orchestration Guide

> **Status:** Complete
> **Last Updated:** 2026-05-29
> **Audience:** Operators

Run Claude Code tasks remotely from Telegram. Send `/claude <task>` (alias: `/claude`) and get results back as messages.

`/claude` runs through the agent worker's `ClaudeCodeExecutor` (`api/services/agent_worker/claude_code_executor.py`). Sessions are persisted, so a threaded reply still resumes a `/claude` session after a server restart. The `LIFEOS_CLAUDE_*` env vars are the budget knobs.

> **For what `/claude` does and how the operator interaction works**, see [Claude Code Orchestration — product spec](../specs/product/claude-code-orchestration.md). For the worker internals see [`api/services/agent_worker/AGENTS.md`](../../api/services/agent_worker/AGENTS.md). This file is the operator how-to.

> **`/codex` is the sibling surface for the Codex CLI.** Same setup pattern: install the binary (`npm i -g @openai/codex`), authenticate (`codex login`), set `LIFEOS_CODEX_RESUME_ENABLED=true` to enable Resume + Go To from `/agents`. Telegram commands are `/codex`, `/codex_status`, `/codex_cancel`. The agent-worker route is `routing='codex'`; the `#codex` task tag flips a `#agent` task to that surface. See the [Codex section in agent-viz](../specs/product/agent-viz.md#operator-controls--resume-and-go-to) and the [`LIFEOS_CODEX_*` env vars](configuration.md#codex-viz-agents-ingest-of-codex-sessions).

## Prerequisites

1. **Telegram configured** — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set in `.env`. See [configuration.md § Telegram](configuration.md#telegram).

2. **Claude Code installed on the server** — the CLI binary must exist at the configured path.

3. **Claude Code authenticated on the server** — this is the most common setup issue. See [Authentication Setup](#authentication-setup) below.

---

## Authentication Setup

Claude Code must be authenticated on the server where the LifeOS server runs. Interactive login (`/login`) stores tokens that may not persist for headless/subprocess usage.

**Recommended: Set up a long-lived token:**

```bash
# SSH to the server
ssh <your-user>@<your-tailscale-ip>

# Run the setup-token command (requires Claude Max/Pro subscription)
~/.local/bin/claude setup-token
```

This creates a persistent authentication token that works in headless mode (no browser/TTY needed).

**Verify it works:**

```bash
ssh <your-user>@<your-tailscale-ip> \
  "~/.local/bin/claude -p 'say hello' \
   --output-format stream-json --verbose 2>&1 | head -3"
```

You should see a `system` init event followed by an `assistant` event with Claude's response. If you see `"Invalid API key"`, the token isn't configured — run `setup-token` again.

**Why this is needed:** The LifeOS server runs as a systemd service (Linux) or launchd agent (macOS) with a minimal environment. The service PATH does not include `~/.local/bin`, and interactive OAuth tokens may not be accessible from the server process context. The `setup-token` command stores credentials that are accessible regardless of how the process is launched.

---

## Configuration

The orchestrator reads four env vars: `LIFEOS_CLAUDE_BINARY`, `LIFEOS_CLAUDE_TIMEOUT`, `LIFEOS_CLAUDE_MAX_TURNS`, `LIFEOS_CLAUDE_MAX_COST`. Defaults rarely need changing. See [configuration.md § Claude Code Orchestration](configuration.md#claude-code-orchestration-code-telegram-command) for the full table.

After changing these values, restart the server:
```bash
ssh <your-user>@<your-tailscale-ip> "cd ~/Code/LifeOS && ./scripts/server.sh restart"
```

---

## Usage

### Automatic Detection

You don't always need `/claude`. If you send a natural language message that requires terminal, filesystem, or browser access, the chat pipeline's intent classifier detects it and automatically routes to Claude Code. For example:

```
"create a backup script for the data directory"
"fix the bug in the sync pipeline"
```

These are handled identically to `/claude <task>` — the Telegram handler spawns a Claude Code session with the same plan mode, clarification, and notification flow.

### Explicit `/claude` Command

Send `/claude` followed by your task description:

```
/claude create a file called test.txt with "hello world" on the Desktop
/claude write a backup script for the LifeOS data directory
/claude add "integrate weather alerts" to the backlog
/claude create a cron job that runs backup.sh daily at 2am
```

You'll receive:
1. An acknowledgment with the resolved working directory
2. Progress updates (if Claude sends `[NOTIFY]` messages)
3. A completion summary when the task finishes

### Directory Resolution

The orchestrator picks the working directory based on keywords in your task:

| Say this... | Claude works in... |
|-------------|-------------------|
| "edit the backlog", "update my journal" | `~/Notes 2025` (vault) |
| "fix the lifeos server", "update sync" | `~/Code/LifeOS` |
| "update the MyProject readme" | `~/Code/MyProject` |
| "write a script", "create a cron job" | `~/Code` |
| anything else | `~` (home) |

### Plan Mode

For complex tasks, Claude will present a plan before implementing. This triggers automatically for tasks containing words like "refactor", "implement", "rewrite", "overhaul", "build a", "set up a", "add a new", "create a new", "remove all", "delete all", "migrate", "replace", "restructure", or "integrate".

**Flow:**
1. You send: `/claude implement a new health check endpoint`
2. Claude presents a plan via Telegram
3. Claude asks: "Reply 'approve' to proceed or 'reject' to cancel."
4. You reply: `approve` (or `yes`, `go`, `ok`, `proceed`)
5. Claude implements the plan and reports completion

To reject: reply `reject` (or `no`, `cancel`, `stop`).

While a plan is pending, you can still send normal messages to LifeOS chat — only short approval/rejection keywords are intercepted.

### Clarification Questions

If a task is vague or ambiguous, Claude will ask you a clarifying question instead of guessing. The question is relayed via Telegram, and the session pauses until you respond.

**Flow:**
1. You send: `/claude add this to the backlog`
2. Claude asks: "The backlog has two sections (Work and Personal). Which one?"
3. You reply: `Work`
4. Claude resumes with your answer and completes the task

**Important:** "no" is treated as an answer to yes/no questions, not a cancellation. Use `/claude_cancel` to cancel instead.

While a clarification is pending, all non-command messages are routed as responses. Use `/claude_cancel` if you want to chat normally instead.

### Monitoring and Control

```
/claude_status    — Shows: task, directory, status, duration, cost
/claude_cancel    — Terminates the active session
```

Only one session runs at a time. If you send `/claude` while a session is active, you'll get an error with the current task description and a hint to use `/claude_cancel`.

---

## How It Works

Implementation moved to the [technical spec](../specs/technical/claude-code-orchestration.md) — that covers subprocess spawning, stream parsing, `[NOTIFY]`/`[CLARIFY]` extraction, the system prompt, the heartbeat timer, and budget enforcement. Operator-facing summary: it's a one-shot `claude` subprocess with `--output-format stream-json --dangerously-skip-permissions`, parsed in real time, with `[NOTIFY]` checkpoints relayed to Telegram and a 5-minute heartbeat so you always know it's alive.

---

## Troubleshooting

### "Claude binary not found"

The binary path doesn't exist. Check:
```bash
ssh <your-user>@<your-tailscale-ip> "ls -la ~/.local/bin/claude"
```

If missing, install Claude Code on the server:
```bash
ssh <your-user>@<your-tailscale-ip> "curl -fsSL https://claude.ai/install.sh | sh"
```

### "Invalid API key" or no response

Claude Code isn't authenticated. Run `setup-token`:
```bash
ssh <your-user>@<your-tailscale-ip> "~/.local/bin/claude setup-token"
```

Then verify:
```bash
ssh <your-user>@<your-tailscale-ip> \
  "~/.local/bin/claude -p 'say hello' \
   --output-format stream-json --verbose 2>&1 | head -3"
```

### Session seems stuck

Check status and cancel if needed:
```
/claude_status
/claude_cancel
```

Sessions timeout automatically after 10 minutes.

### Wrong directory resolved

If Claude is working in the wrong directory, make your task description more explicit:
- Instead of "edit the readme" → "edit the LifeOS readme"
- Instead of "update notes" → "update my vault notes"

---

## Limitations

- **1-hour safety timeout** — adjustable via `LIFEOS_CLAUDE_TIMEOUT`; heartbeats keep you informed, this is a backstop
- **One session at a time** — serial execution only; cancel before starting a new one
- **No interactive input** — Claude runs with `--dangerously-skip-permissions` (no approval prompts)
- **No streaming to Telegram** — you get `[NOTIFY]` checkpoints, not real-time output
- **File sync lag** — if you edit a file and immediately ask Claude to read it via `/claude`, there may be a brief sync delay

---

## Related Documents

- [Claude Code Orchestration — Product](../specs/product/claude-code-orchestration.md) -- What `/claude` does (consumer view); plan mode; clarifications; budgets
- [Claude Code Orchestration — Technical](../specs/technical/claude-code-orchestration.md) -- Implementation: subprocess, stream parsing, system prompt, cancellation
- [Configuration](configuration.md) -- `LIFEOS_CLAUDE_*` env vars
- [MCP Tools](../specs/product/mcp-tools.md) -- MCP tools available to Claude Code sessions
- [Scripts Reference](scripts.md) -- All LifeOS scripts with usage examples
