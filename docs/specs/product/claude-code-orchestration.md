# Claude Code Orchestration

**Status:** Complete
**Owner:** Orchestrator
**Last Updated:** 2026-06-25

LifeOS spawns a **Claude Code** subprocess from Telegram when the operator sends `/claude <task>` (or when a natural-language message is classified as requiring terminal / filesystem / browser access). The subprocess runs the task on the server, streams progress back as Telegram messages, and terminates. The operator can monitor (`/claude_status`) and cancel (`/claude_cancel`) the active session.

This spec covers the consumer view — what the operator sees and controls. For implementation see [technical/claude-code-orchestration.md](../technical/claude-code-orchestration.md). For operator setup (binary install, `setup-token`, troubleshooting) see [guides/claude-code-orchestration.md](../../guides/claude-code-orchestration.md).

> **Codex sibling.** `/codex <task>` is the same surface for the OpenAI Codex CLI. Commands (`/codex`, `/codex_status`, `/codex_cancel`), Telegram reply-to-resume flow, and `/agents` integration mirror `/claude` exactly — only the executor (`codex_executor.py`) and the underlying CLI (`codex exec --json`) differ. Codex runs against your ChatGPT plan; everything in this document applies with `/claude` → `/codex` and `claude` → `codex` substituted, with two caveats: Codex isn't trained on the `[NOTIFY]`/`[CLARIFY]` protocol (final-message-only relay), and there's no plan-mode equivalent.

---

## Table of Contents

1. [How it differs from the agent worker](#how-it-differs-from-the-agent-worker)
2. [Trigger paths](#trigger-paths)
3. [Lifecycle](#lifecycle)
4. [Plan mode](#plan-mode)
5. [Clarification questions](#clarification-questions)
6. [Telegram notifications](#telegram-notifications)
7. [Operator commands](#operator-commands)
8. [Budget model](#budget-model)
9. [Directory resolution](#directory-resolution)
10. [Capability boundaries](#capability-boundaries)
11. [Resume from the `/agents` page](#resume-from-the-agents-page)

---

## How it differs from the agent worker

LifeOS has two related-but-distinct ways to run autonomous work on the operator's behalf. Don't conflate them:

| | Claude Code orchestrator (this spec) | Agent worker ([agent-worker.md](agent-worker.md)) |
|---|---|---|
| **Trigger** | Telegram `/claude <task>` (or auto-detect) | `#agent`-tagged Obsidian task |
| **Runtime** | Local `claude` CLI subprocess on the server | Stand-alone Python worker hitting llama-server or Anthropic Managed Agents |
| **State** | In-process; no persistent SessionStore | SQLite SessionStore + JSONL transcript per session |
| **Concurrency** | One session at a time | Many concurrent sessions, bounded by config caps |
| **Pickup** | Operator-driven (one-shot) | Worker-driven (poll loop) |
| **Resume** | Operator can re-launch from `/agents` (see [agent-viz.md](agent-viz.md)) | Operator can swap the task tag and re-queue |

Both surface on the `/agents` page side-by-side. They share the visualization layer ([agent-viz.md](agent-viz.md)) but not the execution layer.

---

## Trigger paths

The operator gets a Claude Code session through one of two paths:

1. **Explicit `/claude` command in Telegram**. The Telegram bot dispatches the body of the message to the orchestrator. Examples:
   ```
   /claude create a file called test.txt with "hello world" on the Desktop
   /claude write a backup script for the LifeOS data directory
   /claude add "integrate weather alerts" to the backlog
   /claude create a cron job that runs backup.sh daily at 2am
   ```

2. **Auto-detected via the chat intent classifier.** If the operator sends a natural-language message that the classifier tags as "claude intent" (terminal, filesystem, or browser work), the chat pipeline yields a `claude_intent` event and Telegram handles it identically to `/claude`. The plan mode, clarification, and notification flow are the same.

---

## Lifecycle

Each session moves through:

```
ACCEPTED  (Telegram ack message back to operator, with resolved cwd)
   ↓
RUNNING   (Claude Code subprocess emits stdout JSONL events; orchestrator parses)
   ↓
[optional] PLAN_PENDING   (plan-mode tasks: Claude proposes a plan, waits for approve/reject)
   ↓
[optional] CLARIFY_PENDING   (Claude asked a question; operator must answer)
   ↓
[optional] GOAL_PENDING   (Claude proposed a [GOAL]; operator approves to lock it — the worker then injects /goal on resume — or replies with changes to refine)
   ↓
COMPLETED  (subprocess exited; final summary sent to Telegram)
   or
FAILED / TIMEOUT / CANCELLED   (operator-visible terminal state with reason)
```

Only one session runs at a time. Sending `/claude` while a session is active returns an error message naming the current task plus a hint to use `/claude_cancel`.

---

## Plan mode

Tasks containing words like `refactor`, `implement`, `rewrite`, `overhaul`, `build a`, `set up a`, `add a new`, `create a new`, `remove all`, `delete all`, `migrate`, `replace`, `restructure`, or `integrate` trigger plan mode. Claude presents the plan via Telegram and waits for approval.

**Flow:**

1. Operator: `/claude implement a new health check endpoint`
2. Claude presents a plan via Telegram.
3. Claude asks: "Reply 'approve' to proceed or 'reject' to cancel."
4. Operator replies: `approve` (or `yes`, `go`, `ok`, `proceed`)
5. Claude implements and reports completion via `[NOTIFY]`.

To reject: reply `reject` (or `no`, `cancel`, `stop`).

While a plan is pending, normal Telegram messages still reach the chat pipeline — only short approval/rejection keywords are intercepted.

---

## Clarification questions

If a task is vague or ambiguous, Claude asks a clarifying question instead of guessing. The question is relayed via Telegram and the session pauses until the operator answers.

**Flow:**

1. Operator: `/claude add this to the backlog`
2. Claude: "The backlog has two sections (Work and Personal). Which one?"
3. Operator: `Work`
4. Claude resumes with the answer and completes the task.

While a clarification is pending, all non-command Telegram messages route as the answer. The operator uses `/claude_cancel` if they want to chat normally instead.

**Note:** `no` is treated as an answer to yes/no questions, not as a cancellation. Use `/claude_cancel` to actually cancel.

---

## Goal approval

For longer or fuzzier objectives, Claude can propose a **success condition** with `[GOAL] <condition>` before it starts working. The proposed goal is relayed to the operator, the session pauses, and the operator either approves it or replies with changes.

**Flow:**

1. Claude: `[GOAL] All unit tests pass and the linter is clean.`
2. LifeOS: "Reply 'yes' to lock this goal and start, or send changes to refine it."
3. Operator: `yes` → the worker locks the goal (it arms Claude Code's native goal mode by injecting `/goal <condition>` on resume) and Claude begins.
   Or: `make it also require the docs to build` → treated as a refinement; the raw reply goes back to Claude, which re-proposes an updated `[GOAL]`.

Approval is recognized from short affirmatives (`yes`, `approve`, `go ahead`, `sounds good`, `lgtm`, …). A reply that also asks for changes (`yes, but make it stricter`) is treated as a refinement, not a lock.

**Note:** `[GOAL]` is a first-class protocol tag with its own pending state (`REASON_AWAITING_GOAL_APPROVAL`). The doctor persona is the first to emit it; that persona change is tracked separately (issue #397).

---

## Telegram notifications

Claude sends three kinds of messages via Telegram:

| Marker | When | Example |
|--------|------|---------|
| `[NOTIFY] ...` | Progress checkpoint or completion summary | `[NOTIFY] Created backup script at ~/scripts/backup.sh and added daily cron job at 2am.` |
| `[CLARIFY] ...` | Claude needs an answer | `[CLARIFY] Which backlog section — Work or Personal?` |
| `[GOAL] ...` | Claude proposes a success condition to lock before starting | `[GOAL] All unit tests pass and the linter is clean.` |
| heartbeat | Every 5 minutes while running | `Still working... (5m elapsed)` |

Only `[NOTIFY]`, `[CLARIFY]`, and `[GOAL]` lines are relayed. All other output (tool calls, file reads, intermediate steps) stays in the subprocess. The heartbeat is sent by the orchestrator (not Claude) so the operator always knows the session is alive even when Claude is busy without notifying.

---

## Operator commands

| Command | Behavior |
|---------|----------|
| `/claude <task>` | Spawn a new Claude Code session with the given task. Returns an ack with resolved cwd. |
| `/claude_status` | Shows task description, working directory, current status, elapsed duration, cost-so-far. |
| `/claude_cancel` | Terminate the active session immediately. Sends a cancellation notification. |

`/claude_status` and `/claude_cancel` operate on the single active session (if any). If no session is active, they return a message saying so.

---

## Budget model

The orchestrator enforces three caps from `.env` (see [configuration.md § Claude Code Orchestration](../../guides/configuration.md#claude-code-orchestration-code-telegram-command)):

| Cap | Default | What happens at the limit |
|-----|---------|---------------------------|
| `LIFEOS_CLAUDE_TIMEOUT` | 3600 seconds | Watchdog kills the subprocess; operator gets a timeout notification. |
| `LIFEOS_CLAUDE_MAX_TURNS` | 50 turns | Session terminated; operator gets a "max turns reached" notification. |
| `LIFEOS_CLAUDE_MAX_COST` | $2.00 USD | Session terminated; operator gets a "cost cap reached" notification. |

The wall-time cap is the backstop — the 5-minute heartbeat plus `[NOTIFY]` checkpoints mean the operator usually has earlier signals to react to.

---

## Directory resolution

The orchestrator picks the working directory from keywords in the task description:

| Task description matches… | Working directory |
|---------------------------|-------------------|
| "edit the backlog", "update my journal" | Operator's Obsidian vault (`LIFEOS_VAULT_PATH`) |
| "fix the lifeos server", "update sync" | `LIFEOS_CODE_DIR/LifeOS` |
| "update the <project> readme" | `LIFEOS_CODE_DIR/<project>` |
| "write a script", "create a cron job" | `LIFEOS_CODE_DIR` |
| anything else | `~` (home) |

If the resolution is wrong, the operator makes the task more explicit (e.g., "edit the LifeOS readme" instead of "edit the readme").

---

## Capability boundaries

### What the orchestrator can do

- Spawn a single `claude` subprocess at a time on the server.
- Pass it the operator's task as a one-shot prompt.
- Run with `--dangerously-skip-permissions` (no approval prompts) — Claude operates with the same filesystem and shell access as the operator.
- Use any MCP server configured for Claude Code on the server, including the LifeOS MCP catalog (see [mcp-tools.md](mcp-tools.md)).
- Send Telegram messages on the operator's behalf.

### What the orchestrator can't do

- Run two sessions in parallel — operator must `/claude_cancel` first.
- Stream every line of output to Telegram — operator gets `[NOTIFY]` checkpoints, not real-time stdout.
- Persist conversation state across runs — each session is one-shot. (Resume via `/agents` is a separate UI; see below.)
- Exceed the configured budgets — wall, turns, and cost caps are enforced externally.

---

## Resume from the `/agents` page

Every Claude Code session writes a JSONL transcript to `~/.claude/projects/<encoded-cwd>/<session>.jsonl`. The `/agents` page reads those transcripts and shows finished sessions as nodes (inferred status: `inactive` after the most-recent-touch threshold elapses — see [agent-viz.md](agent-viz.md)).

The operator can click "Resume" on an inactive Claude Code session and the LifeOS server runs an operator-configured terminal launcher (`LIFEOS_CC_RESUME_CMD`) that re-opens a Claude session resumed from that transcript id. Gated by `LIFEOS_CC_RESUME_ENABLED`. Configuration in [configuration.md § Claude Code Resume](../../guides/configuration.md#claude-code-resume-agents-operator-controlled-re-launch).

This is operator-driven (you click it) and runs through the operator's terminal — the orchestrator itself doesn't resume sessions automatically.

---

## Related Documents

- [Claude Code Orchestration — Technical](../technical/claude-code-orchestration.md) — Implementation: subprocess management, stream parsing, system prompt, plan-mode detection, transcript handling
- [Claude Code Orchestration — Guide](../../guides/claude-code-orchestration.md) — Operator setup (binary install, `setup-token`, troubleshooting, examples)
- [Agent Worker (product)](agent-worker.md) — The parallel system for `#agent`-tagged tasks
- [Agent Viz (product)](agent-viz.md) — `/agents` page that shows both orchestrator and worker sessions
- [MCP Tools](mcp-tools.md) — LifeOS tools available to Claude Code sessions
- [Configuration](../../guides/configuration.md) — `LIFEOS_CLAUDE_*` env vars
