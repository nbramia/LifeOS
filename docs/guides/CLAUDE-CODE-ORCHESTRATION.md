# Claude Code Orchestration Guide

Run Claude Code tasks remotely from Telegram. Send `/code <task>` and get results back as messages.

## Prerequisites

1. **Telegram configured** — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set in `.env`. See [Configuration](../getting-started/CONFIGURATION.md#telegram).

2. **Claude Code installed on the Mac Mini** — the CLI binary must exist at the configured path.

3. **Claude Code authenticated on the Mac Mini** — this is the most common setup issue. See [Authentication Setup](#authentication-setup) below.

---

## Authentication Setup

Claude Code must be authenticated on the Mac Mini where the LifeOS server runs. Interactive login (`/login`) stores tokens that may not persist for headless/subprocess usage.

**Recommended: Set up a long-lived token:**

```bash
# SSH to the Mac Mini
ssh nathanramia@100.95.233.70

# Run the setup-token command (requires Claude Max/Pro subscription)
/Users/nathanramia/.local/bin/claude setup-token
```

This creates a persistent authentication token that works in headless mode (no browser/TTY needed).

**Verify it works:**

```bash
ssh nathanramia@100.95.233.70 \
  "/Users/nathanramia/.local/bin/claude -p 'say hello' \
   --output-format stream-json --verbose 2>&1 | head -3"
```

You should see a `system` init event followed by an `assistant` event with Claude's response. If you see `"Invalid API key"`, the token isn't configured — run `setup-token` again.

**Why this is needed:** The LifeOS server runs as a launchd agent with a minimal environment. The launchd PATH does not include `~/.local/bin`, and interactive OAuth tokens may not be accessible from the server process context. The `setup-token` command stores credentials that are accessible regardless of how the process is launched.

---

## Configuration

Two optional environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LIFEOS_CLAUDE_BINARY` | `/Users/nathanramia/.local/bin/claude` | Path to the Claude CLI binary |
| `LIFEOS_CLAUDE_TIMEOUT` | `600` (10 minutes) | Max runtime per session in seconds |

These rarely need changing. The binary path matches the standard Claude Code installation location.

After changing these values, restart the server:
```bash
ssh nathanramia@100.95.233.70 "cd ~/Documents/Code/LifeOS && ./scripts/server.sh restart"
```

---

## Usage

### Basic Tasks

Send `/code` followed by your task description:

```
/code create a file called test.txt with "hello world" on the Desktop
/code write a backup script for the LifeOS data directory
/code add "integrate weather alerts" to the backlog
/code create a cron job that runs backup.sh daily at 2am
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
| "fix the lifeos server", "update sync" | `~/Documents/Code/LifeOS` |
| "update the MyProject readme" | `~/Documents/Code/MyProject` |
| "write a script", "create a cron job" | `~/Documents/Code` |
| anything else | `~` (home) |

### Plan Mode

For complex tasks, Claude will present a plan before implementing. This triggers automatically for tasks containing words like "refactor", "implement", "build a", "set up a", "migrate", or "integrate".

**Flow:**
1. You send: `/code implement a new health check endpoint`
2. Claude presents a plan via Telegram
3. Claude asks: "Reply 'approve' to proceed or 'reject' to cancel."
4. You reply: `approve` (or `yes`, `go`, `ok`, `proceed`)
5. Claude implements the plan and reports completion

To reject: reply `reject` (or `no`, `cancel`, `stop`).

While a plan is pending, you can still send normal messages to LifeOS chat — only short approval/rejection keywords are intercepted.

### Monitoring and Control

```
/code_status    — Shows: task, directory, status, duration, cost
/code_cancel    — Terminates the active session
```

Only one session runs at a time. If you send `/code` while a session is active, you'll get an error with the current task description and a hint to use `/code_cancel`.

---

## How It Works

1. **Subprocess spawning**: `/code` spawns `claude -p <task>` as a subprocess on the Mac Mini with `--output-format stream-json` for structured output parsing.

2. **Stream parsing**: A background thread reads the subprocess stdout line-by-line, parsing JSON events (init, assistant, result).

3. **[NOTIFY] extraction**: Assistant events are scanned for lines matching `[NOTIFY] <message>`. These are relayed to Telegram via the sync `send_message()` function.

4. **Completion**: The result event triggers a final notification to Telegram with the task outcome.

5. **Timeout**: A watchdog timer kills the subprocess after 10 minutes (configurable). You'll get a timeout notification in Telegram.

6. **Server shutdown**: Active sessions are gracefully terminated during server restart.

### System Prompt

Claude Code receives a system prompt instructing it to use `[NOTIFY]` for user-facing messages. Only `[NOTIFY]` lines are relayed — all other output (tool calls, file reads, intermediate steps) stays in the subprocess.

This keeps Telegram messages clean and concise. Claude is instructed to always include a completion summary as a `[NOTIFY]` so you know the task finished.

---

## Troubleshooting

### "Claude binary not found"

The binary path doesn't exist. Check:
```bash
ssh nathanramia@100.95.233.70 "ls -la /Users/nathanramia/.local/bin/claude"
```

If missing, install Claude Code on the Mac Mini:
```bash
ssh nathanramia@100.95.233.70 "curl -fsSL https://claude.ai/install.sh | sh"
```

### "Invalid API key" or no response

Claude Code isn't authenticated. Run `setup-token`:
```bash
ssh nathanramia@100.95.233.70 "/Users/nathanramia/.local/bin/claude setup-token"
```

Then verify:
```bash
ssh nathanramia@100.95.233.70 \
  "/Users/nathanramia/.local/bin/claude -p 'say hello' \
   --output-format stream-json --verbose 2>&1 | head -3"
```

### Session seems stuck

Check status and cancel if needed:
```
/code_status
/code_cancel
```

Sessions timeout automatically after 10 minutes.

### Wrong directory resolved

If Claude is working in the wrong directory, make your task description more explicit:
- Instead of "edit the readme" → "edit the LifeOS readme"
- Instead of "update notes" → "update my vault notes"

---

## Limitations

- **10-minute timeout** — adjustable via `LIFEOS_CLAUDE_TIMEOUT` but tasks should be concise
- **One session at a time** — serial execution only; cancel before starting a new one
- **No interactive input** — Claude runs with `--dangerously-skip-permissions` (no approval prompts)
- **No streaming to Telegram** — you get `[NOTIFY]` checkpoints, not real-time output
- **File sync lag** — if you edit a file on the MacBook and immediately ask Claude to read it via `/code`, there may be a brief iCloud sync delay

---

## Related Documentation

- [PRD](../prd/CLAUDE-CODE-ORCHESTRATION.md) - Architecture decisions and technical details
- [Configuration](../getting-started/CONFIGURATION.md) - Environment variables
- [Reminders Guide](REMINDERS.md) - Another Telegram-based feature
- [Launchd Setup](LAUNCHD-SETUP.md) - Server environment context
- [Task Management](TASK-MANAGEMENT.md) - Obsidian task integration
