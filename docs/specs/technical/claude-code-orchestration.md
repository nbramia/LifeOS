# Claude Code Orchestration — Technical

**Status:** Complete
**Owner:** Orchestrator
**Last Updated:** 2026-05-28

Implementation view of `api/services/claude_orchestrator.py` — how the Telegram `/code` command spawns and supervises a `claude` CLI subprocess. For the consumer view (`/code` command, plan mode, clarifications, budgets) see [product/claude-code-orchestration.md](../product/claude-code-orchestration.md). For operator setup see [guides/claude-code-orchestration.md](../../guides/claude-code-orchestration.md).

---

## Table of Contents

1. [Module layout](#module-layout)
2. [Subprocess spawning](#subprocess-spawning)
3. [Stream parsing](#stream-parsing)
4. [`[NOTIFY]` and `[CLARIFY]` extraction](#notify-and-clarify-extraction)
5. [Session state](#session-state)
6. [Plan mode detection](#plan-mode-detection)
7. [Clarification flow](#clarification-flow)
8. [Heartbeat timer](#heartbeat-timer)
9. [Cancellation](#cancellation)
10. [Budget enforcement](#budget-enforcement)
11. [System prompt](#system-prompt)
12. [Directory resolution](#directory-resolution)
13. [Transcript handling and the `/agents` read path](#transcript-handling-and-the-agents-read-path)
14. [System boundaries](#system-boundaries)

---

## Module layout

All code lives in [`api/services/claude_orchestrator.py`](../../../api/services/claude_orchestrator.py) (~700 lines). Key classes:

| Symbol | Responsibility |
|--------|----------------|
| `ClaudeOrchestrator` | Singleton owning the active subprocess, session state, and lifecycle methods (`run_task`, `approve_plan`, `reject_plan`, `respond_to_clarification`, `cancel`, `followup`). |
| `ClaudeSession` | Dataclass with task description, cwd, status, started_at, cost/tokens, `notifications_sent`. |
| `_NOTIFY_RE`, `_CLARIFY_RE` | Module-level regexes that extract `[NOTIFY] ...` and `[CLARIFY] ...` blocks from Claude's streamed text. |
| `_resolve_claude_binary` | Locates the `claude` CLI binary (`LIFEOS_CLAUDE_BINARY` override or `~/.local/bin/claude` default). |
| `_summarize_tool_call` | One-line human-readable summary of a `tool_use` JSONL event (for transcript display, not Telegram). |

The orchestrator runs **in-process** with the FastAPI server — it does not have a separate daemon. Concurrency is single-session (one `subprocess.Popen` at a time).

---

## Subprocess spawning

`ClaudeOrchestrator.run_task(task, cwd)` does:

1. Reject if `_process` is not None (single-session invariant).
2. Resolve the working directory (see [Directory resolution](#directory-resolution)).
3. Build the command:
   ```
   claude -p "<task with system prompt prepended>" \
     --output-format stream-json \
     --verbose \
     --dangerously-skip-permissions
   ```
   Built with `shlex.split` (no `shell=True`) to avoid shell-injection paths even though `task` comes from a trusted operator.
4. `subprocess.Popen(..., stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, cwd=resolved_cwd)`.
5. Spin up a daemon thread that calls `_read_stream(self._process.stdout, session)` to parse the JSONL line-by-line.
6. Start the heartbeat timer (5-minute interval).
7. Send the Telegram ack message including the resolved cwd.

The `--dangerously-skip-permissions` flag means Claude doesn't pause for tool approvals. This is intentional — the operator already authorized the session by sending the `/code` command, and pausing for per-tool approvals would defeat the point of an autonomous helper.

---

## Stream parsing

Claude Code emits one JSON object per line on stdout (`--output-format stream-json`). The reader thread classifies events:

| Event `type` | Handling |
|--------------|----------|
| `system` (init) | Capture session id; ignore. |
| `assistant` | Extract `content[*].text`; pipe through `[NOTIFY]`/`[CLARIFY]` extractors. |
| `tool_use` | Summarize via `_summarize_tool_call` for transcript display. Never relayed to Telegram. |
| `tool_result` | Track for cost/turn counting. Not relayed. |
| `result` (terminal) | Record final cost/tokens; transition session to `COMPLETED`; send final Telegram summary. |

Errors during parse log to `logs/lifeos-api-error.log` and do not crash the reader thread.

---

## `[NOTIFY]` and `[CLARIFY]` extraction

The orchestrator scans assistant-message text for two markers using compiled regexes:

```python
_NOTIFY_RE  = re.compile(r"\[NOTIFY\]\s*(.*?)(?=\[(?:NOTIFY|CLARIFY)\]|\Z)", re.DOTALL)
_CLARIFY_RE = re.compile(r"\[CLARIFY\]\s*(.*?)(?=\[(?:NOTIFY|CLARIFY)\]|\Z)", re.DOTALL)
```

The non-greedy `.*?` with a lookahead for the next marker (or end of string) means multiple `[NOTIFY]` messages within one assistant turn each get their own Telegram delivery. The session row tracks `notifications_sent` so the orchestrator can include the count in the completion summary.

When a `[CLARIFY]` block is detected:
- The orchestrator pauses the session (state → `CLARIFY_PENDING`).
- The question is sent to Telegram.
- The next non-command Telegram message is routed to `respond_to_clarification(answer)` instead of the chat pipeline.
- The orchestrator then sends the answer to the still-running `claude` subprocess via stdin.

---

## Session state

`ClaudeSession` is a single dataclass (no SQLite — orchestrator state is process-local):

```python
@dataclass
class ClaudeSession:
    task: str
    cwd: Path
    status: str   # ACCEPTED | RUNNING | PLAN_PENDING | CLARIFY_PENDING | COMPLETED | FAILED | TIMEOUT | CANCELLED
    started_at: datetime
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    notifications_sent: int = 0
    session_id: str | None = None   # Claude Code's own id from the `system` init event
```

On terminal status, the orchestrator clears `self._process` so the next `run_task` can proceed.

---

## Plan mode detection

Plan mode is keyword-triggered before the subprocess starts. The orchestrator scans the task text for one of the trigger words (`refactor`, `implement`, `rewrite`, `overhaul`, `build a`, `set up a`, `add a new`, `create a new`, `remove all`, `delete all`, `migrate`, `replace`, `restructure`, `integrate`).

If matched, the system prompt prepended to the task instructs Claude to present a plan first via `[NOTIFY]` and explicitly ask "Reply 'approve' to proceed or 'reject' to cancel." The state goes to `PLAN_PENDING` after the plan is delivered; `approve_plan()` / `reject_plan()` resume or cancel.

Plan-mode detection is intentionally permissive — false positives (plan asked for a small change) are mildly annoying; false negatives (no plan for a big change) can damage state. The list of trigger words is conservative on the safe side.

---

## Clarification flow

If Claude emits `[CLARIFY] <question>` mid-session, the orchestrator:

1. Sends `<question>` to Telegram.
2. Sets state to `CLARIFY_PENDING`.
3. Stops relaying chat-pipeline messages — the next Telegram message is captured as the answer.
4. On `respond_to_clarification(answer)`, the answer is fed to the subprocess via stdin (the `claude` CLI supports answering interactively via stdin while a session is running).
5. State returns to `RUNNING`.

If the operator wants to cancel during a clarification, they send `/code_cancel`. The orchestrator distinguishes commands from clarification answers by the leading slash.

---

## Replyable completions (#237)

A reply to a `/code` completion message resumes that session, uniform with `#agent` thread replies. The mechanism reuses the shared `pending_questions` follow-up table rather than a `/code`-specific path:

1. `run_task` / `followup` accept an `on_complete(session)` callback, fired once from `_cleanup` when a session completes successfully (status `completed`, `session_id` known) — after the final notification is sent.
2. The Telegram listener's `_code_callbacks(chat_id)` builds the notify callback (which captures each sent message's chunk ids via `send_message_capture_ids`) plus an `on_complete` that registers a `pending_questions` row with `kind='code_followup'`, keyed to the completion message's chunk ids and storing the Claude `session_id`.
3. On an incoming reply, `_maybe_handle_code_reply` looks up the open row (any-chunk match, `get_open_question_by_message_id`); if `kind='code_followup'` it closes the row and resumes via the orchestrator's existing `followup()` → `resume_session_id` path. The agent worker skips `code_followup` rows (they point at a Claude Code session, not an agent-worker session).

This is **option A** (light build): reply *matching* is unified, but `/code` still runs as a separate session model and resume is bound to the orchestrator's `_last_completed` + `FOLLOWUP_WINDOW`. The deeper merge (routing `/code` through the unified worker, persisting the session id so resume survives restarts) is tracked as option B in #248.

---

## Heartbeat timer

A `threading.Timer` runs every 5 minutes while the session is active. Each tick:

- If `status == RUNNING`, send a Telegram heartbeat: `Still working... (Nm elapsed)`.
- Reschedule.

The heartbeat is the orchestrator's signal, not Claude's. It guarantees the operator hears from the session even if Claude is busy doing work without sending `[NOTIFY]` checkpoints.

The timer is cancelled on any terminal status transition.

---

## Cancellation

`ClaudeOrchestrator.cancel()`:

1. If `_process` is None, return (no-op).
2. Send `SIGTERM` to the process group; wait up to 5 seconds.
3. If still alive, send `SIGKILL`.
4. Mark session `CANCELLED`.
5. Send Telegram cancellation notification.
6. Clear `_process` and stop the heartbeat timer.

This is the path `/code_cancel` triggers. It's also called by the watchdog timer when wall-time / turns / cost caps are exceeded — see [Budget enforcement](#budget-enforcement).

---

## Budget enforcement

Three caps from `.env`:

| Env var | Enforced by |
|---------|-------------|
| `LIFEOS_CLAUDE_TIMEOUT` (seconds, default 3600) | Wall-time watchdog `threading.Timer` started at spawn. On fire, calls `cancel()` and sets status to `TIMEOUT`. |
| `LIFEOS_CLAUDE_MAX_TURNS` (default 50) | Counted from `tool_use` events in the stream. When the count exceeds the cap, `cancel()` is called and status is `FAILED` with reason `max_turns`. |
| `LIFEOS_CLAUDE_MAX_COST` (USD, default 2.0) | Updated from each `result`/`assistant` event's accumulated cost; same cancel-on-breach pattern as above. |

All three caps are enforced **outside** the Claude subprocess so the model can't override them. The operator gets a distinct Telegram notification per cap-breach reason.

---

## System prompt

A fixed system prompt is prepended to every task (see `claude_orchestrator.py` lines ~50–180 for the canonical text). It instructs Claude to:

- **Interpret tasks creatively** — Telegram tasks are short and informal; explore the directory, understand conventions, do the full job.
- **Be persistent and resourceful** — try alternatives, debug, ask only after exhausting options.
- **Know the environment** — vault location, project directories, git, cron, Python venv at `~/.venvs/lifeos`.
- **Use `[NOTIFY]` for progress and completion summaries** — concise, 1–3 sentences. Do not use `[NOTIFY]` for routine tool calls.
- **Use `[CLARIFY]` for genuine ambiguity** — present a single targeted question.
- For plan-mode tasks: present the complete plan in a single `[NOTIFY]`, then ask for approval.

The system prompt also tells Claude not to fix unrelated issues it notices — mention them in the completion summary instead. This keeps blast radius bounded.

---

## Directory resolution

`api/services/directory_resolver.py` (called by `claude_orchestrator.py`) picks the cwd by keyword match against the task text:

| Keyword pattern | Resolved cwd |
|-----------------|--------------|
| `backlog`, `journal`, `vault`, `note(s)` (without project prefix) | `LIFEOS_VAULT_PATH` |
| `lifeos`, `lifeos server`, `sync` (LifeOS-internal terms) | `LIFEOS_CODE_DIR/LifeOS` |
| `<project> readme/script/...` where `<project>` matches a known dir | `LIFEOS_CODE_DIR/<project>` |
| `cron job`, `script`, `tool` (general dev terms) | `LIFEOS_CODE_DIR` |
| anything else | `Path.home()` |

The resolver is a simple match-by-precedence function. If it picks wrong, the operator gets a more-explicit task description and re-runs.

---

## Transcript handling and the `/agents` read path

The orchestrator itself does not write a transcript file. **Claude Code writes its own** to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` — that's where the per-session id from the `system` init event matters.

The `/agents` page's Claude Code ingest path reads those JSONL files independently of this orchestrator (read-only, see [agent-viz technical spec](agent-viz.md) and [ADR-011](../../adr/011-external-agent-ingest.md)). The orchestrator and the viz are loosely coupled — neither imports the other; the JSONL file format is the contract.

The "Resume from `/agents`" UX (see [product/claude-code-orchestration.md § Resume](../product/claude-code-orchestration.md#resume-from-the-agents-page)) runs a separate operator-configured terminal command (`LIFEOS_CC_RESUME_CMD`); it does not go through this orchestrator.

---

## System boundaries

- **Local network only.** All `/code*` Telegram routes are unauthenticated beyond Telegram's own bot-token-based auth.
- **MCP HTTP transport must not expose the orchestrator.** The bearer-gated `/mcp` HTTP endpoint serves the MCP tool catalog only; the `/code` Telegram command path is not exposed externally and the orchestrator is **not** an MCP tool. (Distinct from agent-worker Managed Agents, which is bearer-gated but external by design — see [ADR-008](../../adr/008-managed-agents-cloud-routing.md).)
- **Operator trusts Claude with operator-level access.** `--dangerously-skip-permissions` + no sandboxing means a Claude Code session can do anything the operator's user can. This is the same trust model as the agent worker's local executor (see [agent-worker technical spec § Local executor](agent-worker.md#local-executor-gemma-path)).
- **Single-session is a soft hardening.** Parallelism could be added but would require state migration (session map keyed by id) and isn't needed for personal-assistant usage.

---

## Related Documents

- [Claude Code Orchestration — Product](../product/claude-code-orchestration.md) — Operator-facing behavior
- [Claude Code Orchestration — Guide](../../guides/claude-code-orchestration.md) — Operator setup (binary install, `setup-token`, troubleshooting)
- [Agent Worker — Technical](agent-worker.md) — Sibling autonomous-work system; different trust model and concurrency story
- [Agent Viz — Technical](agent-viz.md) — How Claude Code transcripts surface in `/agents` (read-only)
- [ADR-008: Managed Agents Cloud Routing](../../adr/008-managed-agents-cloud-routing.md) — Worker-side cloud path (contrast with this orchestrator's local-only model)
- [ADR-011: External Agent Ingest](../../adr/011-external-agent-ingest.md) — Read-only adapter pattern the `/agents` page uses for these transcripts
- [`api/services/claude_orchestrator.py`](../../../api/services/claude_orchestrator.py) — Implementation
- [`api/services/directory_resolver.py`](../../../api/services/directory_resolver.py) — Cwd resolution
