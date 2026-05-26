# Agent Worker — Technical

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-05-26

Engineering view of the agent worker — the stand-alone process that consumes `#agent`-tagged tasks and runs them on either a local LLM or Anthropic Managed Agents. For consumer-facing behavior, see [product/agent-worker.md](../product/agent-worker.md). For operator setup, see [guides/agent-worker-setup.md](../../guides/agent-worker-setup.md).

---

## Table of Contents

1. [Architecture overview](#architecture-overview)
2. [Component layout](#component-layout)
3. [Lifecycle of a task](#lifecycle-of-a-task)
4. [Session state machine](#session-state-machine)
5. [Preflight](#preflight)
6. [Local executor (Gemma path)](#local-executor-gemma-path)
7. [Managed executor (Claude path)](#managed-executor-claude-path)
8. [System prompts](#system-prompts)
9. [Inter-agent coordination](#inter-agent-coordination)
10. [Budget enforcement](#budget-enforcement)
11. [Restart resumability](#restart-resumability)
12. [Telegram clarification flow](#telegram-clarification-flow)
13. [Transcripts](#transcripts)
14. [Configuration surface](#configuration-surface)
15. [Related Documents](#related-documents)

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Operator's task list                             │
│                    (Obsidian markdown, #agent tag)                       │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ HTTP poll (60s)
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     lifeos-agent-worker.service                          │
│   - Single-threaded poll loop                                            │
│   - SQLite state (sessions, transcripts, daily spend)                    │
│   - Telegram client (notifications + clarifications)                     │
└────┬───────────────────┬────────────────────────────────┬───────────────┘
     │                   │                                │
     │ Haiku preflight   │ Local executor                 │ Managed executor
     ▼                   ▼                                ▼
┌─────────┐    ┌──────────────────────┐    ┌──────────────────────────────┐
│Anthropic│    │  llama-server (local)│    │ Anthropic Managed Agents API │
│ Haiku   │    │  Gemma 4 26B         │    │  api.anthropic.com/v1/       │
│ classify│    │  + LifeOS MCP        │    │  sessions/events             │
└─────────┘    │  + Bash/Read/Write   │    │ + cloud container            │
               │  + inter-agent tools │    │ + Vault MCPs (LifeOS, Gmail, │
               └──────────────────────┘    │   Calendar, Drive, Slack…)   │
                                           └──────────────────────────────┘
```

The worker is a stand-alone Python process (`python -m api.services.agent_worker.worker`) managed by a systemd unit. It does **not** import the FastAPI app — all task operations go through `/api/tasks` HTTP. This keeps the worker trivially restartable and lets the API layer own task-list locking.

---

## Component layout

All code lives in `api/services/agent_worker/`:

| File | Responsibility |
|---|---|
| `worker.py` | Main poll loop, claim/dispatch, startup resume, signal handling, Telegram delivery, completion summaries |
| `preflight.py` | Haiku-based classifier (budget parsing, routing, ambiguity, sanity) |
| `local_executor.py` | Agent loop against a local LLM (llama-server / Gemma 4 by default) |
| `managed_executor.py` | Lifecycle wrapper around a Managed Agents session — `start()` → `poll()` → `_finalize_remote()` |
| `managed_driver.py` | HTTP wrapper for `api.anthropic.com/v1/sessions` + events endpoint + session-state fan-out |
| `session_store.py` | SQLite schema + accessors (sessions, daily_spend, sleeps, pending_messages, pending_questions, managed_cursor) |
| `spend_tracker.py` | Daily $-cap ledger; pause semantics when cap ≤ 0 |
| `transcript_store.py` | Append-only JSONL per `session_id` at `data/agent_transcripts/` |
| `tools.py` | `STANDARD_TOOLS` (Read/Write/Edit/Bash/Glob/Grep/WebFetch/WebSearch/sleep) + `ToolRegistry` combining standard + inter-agent + MCP tools |
| `inter_agent.py` | `lifeos_agent_*` family — spawn, send, check, yield_until, kill, transcript_read, sessions_list, user_ask |
| `pricing.py` | Per-model $/token table; `MANAGED_SESSION_HOUR_OVERHEAD = $0.08` |
| `router.py` | Thin local-vs-claude dispatch helper |

External boundaries:

- **HTTP**: `/api/tasks` for task CRUD, `/api/tasks/{id}/swap-tag` for atomic tag transitions, the Telegram Bot API for notifications.
- **LLM**: `https://api.anthropic.com/v1` for Haiku preflight + Managed Agents API; `http://localhost:8080` (llama-server) for local execution.
- **MCP**: `https://<host>/mcp` for the LifeOS MCP HTTP transport (used by Managed Agents and other remote MCP clients), and stdio MCP for local Claude Code.

---

## Lifecycle of a task

```
poll → spend tracker check (`can_start_task(default_budget)`)
     → wake sleeping sessions whose timer expired
     → poll managed sessions for state advancement
     → resume yielded-for-children sessions if children done
     → dispatch spawned sessions (drain pending_messages)
     → process clarification answers (Telegram replies)
     → timeout stale clarifications (default 72h)
     → list /api/tasks across AGENT_PICKUP_STATUSES (`todo` + `urgent`), tag=agent, dedupe by id
     → for each candidate:
         atomic swap #agent → #agent-running   (race-free via swap-tag API)
         create session row + transcript "claim" event
         run preflight (Haiku) → PreflightResult
             routing in {local, claude, ask}
             expected_output in {text, file, external_action, structured}
             ambiguity: question | null
             sane: bool
         dispatch on routing:
             local  → LocalExecutor.execute(session, task)
             claude → ManagedExecutor.start(session, task)
             ask    → Telegram clarification, park at #agent-blocked
         on terminal outcome:
             COMPLETED → mark task done in vault + swap to #agent-completed
             FAILED / BUDGET_EXCEEDED → swap to matching tag
             BLOCKED → Telegram clarification + leave for human
             YIELDED → leave for sleeps loop to wake at wake_at
         notify operator (Telegram) on terminal states
```

---

## Session state machine

`SessionStore.sessions` row statuses, with valid transitions:

```
            ┌─────────┐
            │ CLAIMED │ (worker won the tag-swap race)
            └────┬────┘
                 │ preflight + dispatch
                 ▼
            ┌─────────┐
            │ RUNNING │
            └────┬────┘
                 ├────► COMPLETED  (terminal — task done)
                 ├────► FAILED      (terminal — executor error, sanity reject, etc.)
                 ├────► BUDGET_EXCEEDED  (terminal — wall/tokens/dollars cap)
                 ├────► BLOCKED     (waiting on Telegram, or Managed Agents not configured)
                 └────► YIELDED     (sleep tool / yield_until — wake on timer or child completion)
```

Each session also tracks `routing`, `budget`, `expected_output`, `total_input_tokens`, `total_output_tokens`, `total_dollars`, `managed_agent_session_id`, `started_at`, `parent_session_id`, `root_session_id`, `spawn_depth` (for lineage budgets), and `yield_waiting_for` (when in `YIELDED` from `yield_until`).

---

## Preflight

The preflight is a single Haiku call (`claude-haiku-4-5` by default) that classifies a task before executor dispatch. Cheap (~$0.001) and fast (~1s). Returns:

```python
@dataclass
class PreflightResult:
    budget: PreflightBudget         # parsed from title or default
    routing: str                    # local | claude | ask
    routing_reason: str
    expected_output: str            # text | file | external_action | structured
    ambiguity: PreflightAmbiguity | None
    sane: bool
    sane_reason: str
    raw: dict                       # the parsed JSON for debugging
```

Routing precedence (per the prompt instructions):

1. `#local` tag → routing=local
2. `#cloud` tag → routing=claude
3. Title contains explicit model cue ("use claude", "with opus", "using gemma") → claude / local
4. Title contains capability-implying phrase ("search my gmail", "google drive", "send a slack message", etc.) → claude (those tools require cloud connectors)
5. Otherwise → ask (worker pauses the task and asks via Telegram)

Hardening: response is parsed defensively (handles `` ```json `` fences, partial schemas, missing keys, exceptions). On any parse failure the result defaults to `sane=false` so the worker parks the task rather than running with garbage.

---

## Local executor (Gemma path)

`LocalExecutor.execute(session, task) -> ExecutorOutcome`. Wraps an agent loop against an OpenAI-compatible local LLM server (llama-server with `unsloth/gemma-4-26B-A4B-it-GGUF` by default).

Per-turn flow:

1. Check budgets at top-of-loop — kill with `STATUS_BUDGET_EXCEEDED` if `total_tokens >= max_tokens` OR `total_dollars >= max_dollars` OR `wall_seconds_elapsed >= wall_seconds` OR lineage budget breached. Cascade-kill descendants on lineage breach.
2. Build the message list (system prompt + prior user/assistant/tool_result turns).
3. Call the local LLM with the tool catalog (`STANDARD_HANDLERS` + `lifeos_agent_*` inter-agent tools + LifeOS MCP tools, all in OpenAI format).
4. Parse response — handles both OpenAI `{"function": {"name", "arguments"}}` and Anthropic `{"name", "input"}` shapes via `_normalize_tool_calls`.
5. If response has tool_calls: dispatch each to `ToolRegistry`, append `tool_result` turns, loop.
6. If no tool_calls and content is non-empty: finalize with `STATUS_COMPLETED` + `final_text`.
7. If tool emitted a yield (sleep: `yield_seconds > 0`; `yield_until`: `yield_seconds == -1`): set `STATUS_YIELDED` and return — the worker's sleep / yield-resumption loops take over.

Cost: 0 dollars (`local` model maps to $0 in `pricing.py`). Wall-time enforcement still applies.

---

## Managed executor (Claude path)

`ManagedExecutor.start(session, task)` creates a remote Managed Agents session and returns `STATUS_RUNNING`. The worker's `_poll_managed_sessions` then calls `poll(session)` each tick until terminal.

Session creation body (only fields actually used; the agent preset holds persona / tools / MCPs / system prompt):

```json
{
  "agent": "agent_…",
  "environment_id": "env_…",
  "vault_ids": ["vlt_…"],
  "metadata": {"lifeos_session_id": "sess_…", "task_id": "…"},
  "title": "<first 100 chars of task description>"
}
```

Plus a follow-up `POST /v1/sessions/{id}/events` with the initial user message (`Task: …` + soft budget constraints).

State polling fans out to two endpoints (live API doesn't embed events in the session-state response):

- `GET /v1/sessions/{id}` → status + cumulative `usage`. Treats `status: "idle"` as the canonical successful terminal.
- `GET /v1/sessions/{id}/events?after=<cursor>` → event stream (`agent.message`, `agent.tool_use`, `session.status_idle`, `session.error`, etc.). Paginates while `has_more=true`.

Synthesized terminal status precedence:

1. Non-init `session.error` events → status=`failed` (cascading failure, can't lose it).
2. Raw status in `TERMINAL_REMOTE_STATUSES` → use raw.
3. `session.status_idle` event → status=`completed`.
4. Otherwise → still running.

Cost accounting: delta-tracks token spend each poll using `pricing.cost_for(model, …)` and adds `(wall_seconds / 3600) × $0.08` session-hour overhead. Mid-flight budget breach kills the remote session via `DELETE /v1/sessions/{id}` and finalizes locally.

### MCP-init failure handling

The Managed Agents API emits `session.error` events at session-start for any MCP that fails to initialize (URL doesn't match a Vault credential, OAuth invalid, host unreachable, etc.). These are **informational** — the agent works around the missing MCP. The driver filters `mcp_authentication_failed_error` / `mcp_connection_failed_error` types out of the failed-status synthesis. Affected MCP names are persisted in `managed_cursor.init_failed_mcps_json` (across polls, since they fire on the first batch but the session may not idle until later) and surfaced as a footer in the completion Telegram summary.

### Empty-final-text carry-forward

`agent.message` events with the agent's final text can arrive in an earlier poll batch than the `session.status_idle` event. The driver's per-batch `_extract_final_text` would return `None` on the idle-only batch. The executor mitigates this by caching `final_text` to `managed_cursor.final_text` on every poll where the driver returns a non-None value, and reading it back at finalize.

---

## System prompts

All four model-facing prompts are structured per [Anthropic's Claude 4.6/4.7 prompt-engineering best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) — XML section tags, positive framing, explicit final-summary requirement after tool use.

| Prompt | Location | Audience |
|---|---|---|
| Cloud agent preset | Anthropic console (versioned; current v3) | Claude Sonnet 4.6 on Managed Agents |
| Local Gemma `_system_prompt` | `local_executor.py` (`_SYSTEM_PROMPT_STATIC` + per-task `<this_task>` trailer) | Gemma 4 26B |
| Cloud per-task user message | `managed_executor.py` (`_user_message_for`) | Initial user turn for each managed session |
| Haiku preflight prompt | `preflight.py` (`_PREFLIGHT_INSTRUCTIONS`) | Claude Haiku classifier |

Cache strategy: the local executor's static portion is a module-level constant so prompt caches don't invalidate between sessions; only the small trailing `<this_task>` block (expected_output + soft budget) varies.

The cloud preset YAML is mirrored verbatim in [`guides/agent-worker-setup.md`](../../guides/agent-worker-setup.md) for fresh-clone operators.

---

## Inter-agent coordination

Local agents can spawn child sessions and coordinate via the `lifeos_agent_*` tool family:

| Tool | Purpose |
|---|---|
| `lifeos_agent_spawn` | Create a new agent session (local or claude). Returns `session_id`. |
| `lifeos_agent_send` | Post a message to a child session's queue. |
| `lifeos_agent_check` | Poll a child's current state. |
| `lifeos_agent_yield_until` | Pause the caller until specific children reach terminal state — preferred over polling (no idle billing). |
| `lifeos_agent_kill` | Terminate a child early. |
| `lifeos_agent_transcript_read` | Read another session's transcript. |
| `lifeos_agent_sessions_list` | List active + recent sessions. |
| `lifeos_agent_user_ask` | Pause and ask the operator a clarifying question via Telegram (reply-threaded). |

Security: lineage checks ensure a session can only message / kill / yield-on its own descendants (rooted at `root_session_id`).

Lineage budgets: every session tracks `root_session_id` + `spawn_depth`. Budget breaches at any descendant cascade-kill the entire lineage. Limits configurable via `LIFEOS_AGENT_MAX_SPAWN_DEPTH`, `LIFEOS_AGENT_MAX_DESCENDANTS_PER_ROOT`, `LIFEOS_AGENT_MAX_CONCURRENT_LOCAL`, `LIFEOS_AGENT_MAX_CONCURRENT_MANAGED`.

---

## Budget enforcement

Four overlapping layers, executed in this order:

1. **Daily $-cap** — `SpendTracker.can_start_task(estimated)` short-circuits to False when `daily_cap_dollars <= 0` (operator pause). Otherwise blocks new claims when accumulated day spend ≥ cap.
2. **Per-task token / wall / dollar caps** — checked at the top of every executor turn (local) or every poll (managed).
3. **Lineage caps** — for child sessions, the entire lineage's combined spend is checked against any ancestor's `max_dollars`. Breach cascade-kills the lineage.
4. **Remote (cloud only)** — when the worker detects a mid-flight breach for a managed session, it calls `DELETE /v1/sessions/{id}` to stop Anthropic-side billing immediately.

---

## Restart resumability

The worker is signal-safe and crash-resumable. `resume_pending()` runs on startup and scans non-terminal sessions:

- `YIELDED` with a `sleeps` row → leave alone (sleeps loop wakes it on schedule).
- `BLOCKED` → leave alone (waiting on Telegram reply or operator unblock).
- Anything else (`CLAIMED` / `RUNNING` mid-execution) → roll tag back from `#agent-running` to `#agent`, mark session `FAILED` in the DB, notify operator.

A managed session's `managed_agent_session_id` is durable across worker restarts — on resume the worker reattaches via `GET /v1/sessions/{id}` and continues polling from `managed_cursor.last_event_id`.

---

## Telegram clarification flow

When the worker needs operator input mid-task — preflight routing=ask, ambiguity question, or `lifeos_agent_user_ask` mid-loop — it:

1. Sends a Telegram message via `send_message_capture_id()` (returns the Telegram `message_id`).
2. Persists `(session_id, telegram_message_id, question)` in `pending_questions`.
3. Swaps the tag to `#agent-blocked` and parks the session.
4. For managed sessions: also calls `driver.kill_session` to stop session-hour billing while waiting.

When the operator replies (using Telegram's native reply feature), the bot's `_maybe_deposit_agent_answer()` hook intercepts the `reply_to_message_id` and calls `SessionStore.deposit_answer()`. The worker's `_process_clarification_answers()` runs each tick, picks up answered questions, parses the answer (for routing questions: extracts `local` / `claude` from free-text), updates the session, and re-dispatches.

For local sessions, the parent session resumes via the existing pending_messages drain.
For managed sessions, a new remote session is created with the resolved routing.

Clarifications older than `LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS` (default 72h) are abandoned with a Telegram heads-up; the transcript stays preserved.

---

## Transcripts

Every session has an append-only JSONL transcript at `data/agent_transcripts/<session_id>.jsonl`. Each line is one event:

```json
{"ts": 1779800000.0, "kind": "claim", "data": {"task_id": "abc"}}
{"ts": 1779800001.5, "kind": "preflight_result", "data": {...}}
{"ts": 1779800003.2, "kind": "llm_turn", "data": {"role": "assistant", "content": "...", "tool_calls": [...]}}
{"ts": 1779800004.1, "kind": "tool_dispatch", "data": {"name": "Bash", "input": {...}, "result": "..."}}
{"ts": 1779800010.4, "kind": "managed_event_agent.message", "data": {...}}
{"ts": 1779800012.8, "kind": "managed_completed", "data": {"final_chars": 240, "init_failed_mcps": []}}
```

Transcripts are append-only and survive worker restarts. They're the audit trail of choice — Telegram summaries point at them when a task lands at `#agent-failed` or produces an unexpectedly empty completion.

---

## Configuration surface

Full reference in [`agent-worker-setup.md`](../../guides/agent-worker-setup.md). Categories:

| Group | Vars |
|---|---|
| Worker lifecycle | `LIFEOS_AGENT_WORKER_AUTOSTART`, `LIFEOS_AGENT_WORKER_POLL_SECONDS` |
| Budgets | `LIFEOS_AGENT_DAILY_CAP_DOLLARS`, `LIFEOS_AGENT_DEFAULT_BUDGET_DOLLARS`, `LIFEOS_AGENT_DEFAULT_WALL_SECONDS`, `LIFEOS_AGENT_DEFAULT_MAX_TOKENS` |
| Preflight | `LIFEOS_AGENT_PREFLIGHT_MODEL` |
| Managed Agents (cloud) | `LIFEOS_AGENT_PRESET_ID`, `LIFEOS_AGENT_ENVIRONMENT_ID`, `LIFEOS_AGENT_VAULT_ID`, `LIFEOS_AGENT_MANAGED_MODEL`, `ANTHROPIC_API_KEY` |
| MCP HTTP transport | `LIFEOS_MCP_HTTP_URL`, `LIFEOS_MCP_BEARER_TOKEN`, `LIFEOS_MCP_HTTP_HOST`, `LIFEOS_MCP_HTTP_PORT` |
| Inter-agent caps | `LIFEOS_AGENT_MAX_SPAWN_DEPTH`, `LIFEOS_AGENT_MAX_DESCENDANTS_PER_ROOT`, `LIFEOS_AGENT_MAX_CONCURRENT_LOCAL`, `LIFEOS_AGENT_MAX_CONCURRENT_MANAGED` |
| Telegram clarifications | `LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS` |

---

## Related Documents

- [Agent Worker — Product](../product/agent-worker.md) — What `#agent` does, consumer view
- [Agent Worker — Setup](../../guides/agent-worker-setup.md) — Operator setup walkthrough
- [Task Management](../product/task-management.md) — How `#agent` tasks sit alongside regular tasks
- [MCP Tools](../product/mcp-tools.md) — Standard MCP catalog including `lifeos_agent_*` family
- [API Reference](../product/api-reference.md) — Task endpoints the worker uses (`/api/tasks/{id}/swap-tag`, `/api/tasks/{id}/complete`)
- [Architecture](architecture.md) — Where the worker fits in the broader code structure
- [Observability](observability.md) — Tracing, perf, and logging patterns the worker uses
