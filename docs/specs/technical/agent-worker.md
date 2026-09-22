# Agent Worker — Technical

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-22

Engineering view of the agent worker — the stand-alone process that consumes engine-assigned tasks and runs them on either a local LLM or Anthropic Managed Agents. For consumer-facing behavior, see [product/agent-worker.md](../product/agent-worker.md). For operator setup, see [guides/agent-worker-setup.md](../../guides/agent-worker-setup.md).

---

## Table of Contents

1. [Architecture overview](#architecture-overview)
2. [Component layout](#component-layout)
3. [Session store schema](#session-store-schema)
4. [Lifecycle of a task](#lifecycle-of-a-task)
5. [Session state machine](#session-state-machine)
6. [Preflight](#preflight)
7. [Local executor (Gemma path)](#local-executor-gemma-path)
8. [Managed executor (Claude path)](#managed-executor-claude-path)
9. [Card assignment](#card-assignment)
10. [System prompts](#system-prompts)
11. [Project task context and coordination](#project-task-context-and-coordination)
12. [Inter-agent coordination](#inter-agent-coordination)
13. [Budget enforcement](#budget-enforcement)
14. [Restart resumability](#restart-resumability)
15. [Lifecycle drift reconciliation](#lifecycle-drift-reconciliation)
16. [Telegram clarification flow](#telegram-clarification-flow)
17. [Transcripts](#transcripts)
18. [Agent Output notes](#agent-output-notes)
19. [Configuration surface](#configuration-surface)
20. [Related Documents](#related-documents)

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Operator's task list                             │
│                    (Obsidian markdown, engine assignee)                       │
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

### Runtime identity

At process import, the worker captures one immutable full git revision, a
unique startup id, its PID, source root, and whether the source tree was clean.
The capture is never recomputed from checkout state while the worker is
running. It publishes a private local identity record under
the local user cache under `~/.cache/lifeos/runtime-identities/`; each poll-loop heartbeat updates only that record's
heartbeat timestamp. Deployment evidence may therefore distinguish a worker
that is alive on an older revision from the API process, and missing, dirty, or
unavailable identity is not a successful deployment result.

---

## Component layout

All code lives in `api/services/agent_worker/`:

| File | Responsibility |
|---|---|
| `worker.py` | Main poll loop, claim/dispatch, startup resume, signal handling, Telegram delivery, completion summaries, Agent Output notes |
| `preflight.py` | Haiku-based classifier (budget parsing, routing, ambiguity, sanity) |
| `local_executor.py` | Agent loop against a local LLM (llama-server / Gemma 4 by default) |
| `managed_executor.py` | Lifecycle wrapper around a Managed Agents session — `start()` → `poll()` → `_finalize_remote()` |
| `managed_driver.py` | HTTP wrapper for `api.anthropic.com/v1/sessions` + events endpoint + session-state fan-out |
| `session_store.py` | SQLite schema + accessors (sessions, daily_spend, sleeps, pending_messages, pending_questions, managed_cursor) |
| `usage_ledger.py` | SessionStore-backed usage observations, provenance, idempotency, reservations, and replay into the legacy usage projection |
| `spend_tracker.py` | Daily $-cap ledger; pause semantics when cap ≤ 0 |
| `transcript_store.py` | Append-only JSONL per `session_id` at `data/agent_transcripts/` |
| `tools.py` | `STANDARD_TOOLS` (Read/Write/Edit/Bash/Glob/Grep/WebFetch/WebSearch/sleep) + `ToolRegistry` combining standard + inter-agent + MCP tools |
| `inter_agent.py` | `lifeos_agent_*` family — project_handoff, spawn, send, check, yield_until, kill, transcript_read, sessions_list, user_ask, execution_override; MCP caller proofs bind remote calls to one session and handoff calls to one executor turn |
| `pricing.py` | Per-model $/token table; `MANAGED_SESSION_HOUR_OVERHEAD = $0.08` |
| `router.py` | Thin local-vs-claude dispatch helper |

External boundaries:

- **HTTP**: `/api/tasks` for task CRUD, `/api/tasks/{id}/swap-tag` for atomic tag transitions, the Telegram Bot API for notifications.
- **LLM**: `https://api.anthropic.com/v1` for Haiku preflight + Managed Agents API; `http://localhost:8080` (llama-server) for local execution.
- **MCP**: `https://<host>/mcp` for the LifeOS MCP HTTP transport (used by Managed Agents and other remote MCP clients), and stdio MCP for local Claude Code.

---

## Session store schema

`session_store.py` creates eight tables (SQLite, `data/agent_sessions.db` by default). The schema is deliberately permissive — new columns and tables have been added issue-by-issue rather than designed up front — so this list is read directly off the table-creation code (`_SCHEMA` in `session_store.py`) rather than written from memory; re-check the source if it looks stale.

### `sessions`

One row per agent session (1:1 with a claimed task, or a root-spawned operator session — see `origin` below). `task_id` is the primary key, **not** `session_id` — the two are related but distinct ids.

| Column | Meaning |
|---|---|
| `task_id` (PK) | The engine-assigned task this session was claimed from (or a synthetic id for operator sessions). |
| `session_id` (UNIQUE) | Internal session identifier used for inter-agent addressing, transcripts, etc. |
| `status` | One of `claimed`, `running`, `yielded`, `completed`, `failed`, `budget_exceeded`, `blocked`. |
| `routing` | `local`, `claude`, `claude_code`, `codex`, `hermes`, or `ask` — which executor runs the session. |
| `budget_json` | JSON-encoded budget (dollars / wall-clock / tokens) set at preflight. |
| `started_at`, `last_activity_at` | Unix epoch seconds. |
| `total_input_tokens`, `total_output_tokens` | Accumulated token counts. |
| `total_cache_creation_tokens`, `total_cache_read_tokens` | Prompt-cache token buckets, tracked separately because they're billed at different rates than plain input tokens. |
| `total_dollars` | **The accumulated-spend column.** Not `spend`, not `cost` — `total_dollars`. |
| `total_active_seconds` | Accumulated active compute time. |
| `expected_output` | `text`, `file`, `external_action`, or `structured`, set at preflight. |
| `parent_session_id`, `root_session_id`, `spawn_depth` | Inter-agent lineage for spawned sessions. |
| `yield_waiting_for` | JSON array of `session_id`s this session is yielded waiting on. |
| `managed_agent_session_id` | Anthropic Managed Agents session id, for `routing="claude"` sessions. |
| `preset_class` | Tool-filtering preset applied to a Managed Agents session at start. |
| `origin` | NULL or `"agent"` = claimed from an engine-assigned vault task; `"operator"` = root-spawned on demand with no backing task. |
| `claude_code_session_id` | Claude Code (or Codex) CLI session/thread id, for `routing="claude_code"`/`"codex"` — the column is reused for both; `routing` disambiguates which CLI it belongs to. |
| `claude_code_model` | Legacy Claude tier for `routing="claude_code"` (`haiku`/`sonnet`/`opus`); NULL omits `--model` and leaves the CLI's configured/native default authoritative. |
| `bot` | Telegram bot that owns this session's notices; NULL = primary bot. |
| `unpriced` | Sticky flag: set once any turn was priced against a model `pricing.py` doesn't recognize, so a reader can tell "$0.00 total" apart from "some turns couldn't be priced". |
| `host` | Board-assigned host name from `settings.agent_hosts`; NULL/`""` = this API host. Read by the `claude_code`/`codex` executors to decide whether to spawn locally or wrap the argv in `ssh`. |
| `model`, `effort` | Board-assigned model id and effort level (`low`/`medium`/`high`/`max`), threaded into the executor's argv — see [Card assignment](#card-assignment) below. Distinct from `claude_code_model`, which predates this and is set by `lifeos_agent_spawn`'s `tier` argument, not the board. |
| `conversation_id` | Hermes conversation id (`routing="hermes"` only) — set by `HermesExecutor` once the turn's `conversation_id` SSE event arrives, and what a card's `session.open_url` points `/chat?conversation=` at. |
| `remote_pgid` | Process-group id a remote-spawned subprocess echoed back on its first stdout line — see [Host registry and ssh spawn](#host-registry-and-ssh-spawn-851). Used by the operator kill endpoint to reach the process over ssh; NULL for a local session. |
| `hermes_model` | The model Hermes itself reported for this session's MOST RECENT turn (`routing="hermes"` only) — set by `HermesExecutor.execute` (`SessionStore.set_hermes_model`) from that turn's own `_HermesTurnPersister.reported_model`, on both the completion and failure exit paths. NULL until one of this session's own turns reports a model in a well-formed `usage` event; a turn without one leaves it NULL and the badge plain `Hermes`. Distinct from `model` above (the board's operator-chosen picker value, which `HermesExecutor` never reads or writes) and from `api/services/model_readout.py`'s process-wide "last observed" Hermes reading, which serves `/api/health`'s model readout and the board's `GET /api/agents/models` picker catalog (see [Model catalog](#model-catalog)) and never this badge — no other writer touches this column, so it can't be overwritten by an unrelated session's or surface's turn. Feeds `/agents`'s per-session `Hermes · <model>` badge; see [agent-viz.md](agent-viz.md#lifeos-agent-ingest). |
| `execution_request_json` | Strict canonical client choices (`executor`, `model_id`, `effort`, `host`, `working_dir`, budget, and constraints). Trusted lineage/persona/reply identity and derived provider/runtime/billing fields are not writable. |
| `execution_spec_json` | Immutable resolver snapshot persisted before dispatch. Retries and resumes reuse it instead of recomputing changed defaults. |

Indexed on `status`, `parent_session_id`, `root_session_id`, and (partial index) `status = 'yielded'`.

### Execution resolution

`execution.py` resolves one target in this order: explicit canonical request, a matching temporary session/lineage override, task assignment, workflow/preflight defaults, installation default, then an executor-native model default. Model pins carry an executor scope and never cross an engine change. A loaded or valid-empty catalog can reject a pin; unknown, unavailable, and unconfigured catalog states remain distinct and cannot erase a same-engine pin. Readiness and host validation fail closed, constraints are intersected before dispatch, and provider/runtime/billing are derived only from observed executor facts.

`SessionStore.set_execution_snapshot()` is compare-and-set: concurrent resolvers all receive the persisted winner. `begin_new_execution()` can clear a snapshot only after a terminal session, while ordinary restart/retry/resume paths deserialize the existing spec. The `execution_overrides` table stores at most one override per session or lineage root; expiry changes only later resolutions. `lifeos_agent_execution_override` is the reachable MCP surface: session identity is derived from the caller, and only a root may alter its lineage override, so clients cannot forge parent/root scope.

### `pending_messages`

Inter-agent messages queued for delivery to a peer/child/parent session — written by `lifeos_agent_send` for sessions that aren't actively running, and injected on resume for yielded sessions.

| Column | Meaning |
|---|---|
| `id` (PK, autoincrement) | Row id. |
| `session_id` | Recipient session. |
| `sender_id` | Sending session's id. |
| `content` | Message body. |
| `created_at` | Unix epoch seconds. |
| `delivered` | 0/1 — whether the recipient has consumed it. |

### `pending_questions`

Open clarification questions sent to the operator via Telegram, and completion-message follow-ups (an operator reply to a finished task's Telegram message that continues the thread). `kind` distinguishes several flows: `"clarification"` blocks the session until answered; `"followup"` reopens an already-`completed` session and appends the reply as a new turn; `"budget"` parks a `STATUS_YIELDED` session after an in-process budget breach — `yes`/`yes $N`/`yes N min` extends the cap and resumes, `stop` finalizes as `budget_exceeded` (see [Breach → yield → ask](#breach--yield--ask)). `kind` is a plain value in this free-text column, not a separate schema element — adding `"budget"` required no migration beyond the pre-existing idempotent `ALTER TABLE ... ADD COLUMN kind` that already runs for a legacy DB predating this column at all.

| Column | Meaning |
|---|---|
| `id` (PK, autoincrement) | Row id. |
| `session_id`, `task_id` | The session and task this question belongs to. |
| `question` | Question text sent to the operator. |
| `sent_message_id` | Telegram message id the answer is matched against. |
| `sent_at` | Unix epoch seconds. |
| `answer`, `answered_at` | Populated once the operator replies. |
| `processed` | 0 = queued, 2 = atomically claimed by a worker pass, 1 = processed/retired. Reassignment retires both queued and in-flight follow-ups. |
| `timed_out` | 0/1 — set if the clarification aged out (default 72h) before an answer arrived. |
| `kind` | `"clarification"` or `"followup"` (default `"clarification"`). |
| `sent_message_ids` | JSON array of every Telegram chunk id for this notification (a long completion splits across multiple messages); NULL for legacy rows, which still match via `sent_message_id` alone. |
| `bot` | Telegram bot that sent this question; NULL = primary. Scopes reply-matching so a doctor-bot reply can't collide with a primary-bot question sharing the same numeric message id. |

### `daily_spend`

The daily $-cap ledger `spend_tracker.py` reads and increments.

| Column | Meaning |
|---|---|
| `date` (PK) | Calendar date, as text. |
| `total_dollars` | Total spend booked against that date. |

### Usage ledger tables

`usage_observations` is append-only audit input keyed by an observation event;
`usage_ledger` is the current aggregate keyed by
`session_id`/`attempt_id`/`turn_id`; `usage_reservations` holds active bounded
estimates separately from billed dollars; and `usage_projection_state` records
replay progress into the legacy `usage.db` projection. These tables are
created additively by `usage_ledger.py`, so lifecycle owners can add their
identity columns without changing the ledger's authority or dedupe rules.

### `messages`

Conversation log for local-path (`routing="local"`) sessions only — Managed Agents sessions store their conversation in the JSONL transcript instead, since their authoritative state lives on the Anthropic side.

| Column | Meaning |
|---|---|
| `session_id`, `turn_index` (composite PK) | Session and 0-based turn position. |
| `role` | Turn role (e.g. `user`, `assistant`, `tool`). |
| `content_json` | JSON-encoded turn content. |
| `tokens_in`, `tokens_out` | Per-turn token counts. |
| `created_at` | Unix epoch seconds. |

### `sleeps`

A session with a row here is yielded on a timer — the worker's main loop scans this table and resumes the session once `wake_at` has passed.

| Column | Meaning |
|---|---|
| `session_id` (PK) | The yielded session. |
| `wake_at` | Unix epoch seconds the session should resume at. |

### `managed_cursor`

Polling bookkeeping for Managed Agents sessions, one row per `task_id`.

| Column | Meaning |
|---|---|
| `task_id` (PK) | The task this cursor belongs to. |
| `last_event_id` | Last Managed Agents event id ingested, so polling resumes without re-processing. |
| `accrued_session_hour_dollars` | Cumulative session-hour overhead already booked into the session's `total_dollars`. |
| `final_text` | Most recent `agent.message` text seen across polls — cached because the final message and the terminal `session.status_idle` event can land on different polls, and a Telegram completion summary would otherwise have no text to show. |
| `tool_loop_signature`, `tool_loop_count`, `tool_calls_since_message` | Runaway-loop detection counters, persisted so cross-poll signals survive worker restarts mid-session. |

### `cli_sessions`

One row per Claude Code / Codex CLI session registered from any host via `POST /api/agents/cli-sessions/events`, keyed `cc:<uuid>` / `cx:<uuid>` and carrying event-driven status (`idle` / `running` / `ended`) instead of a file-age guess. See [Cross-machine CLI session registration](agent-viz.md#cross-machine-cli-session-registration) for the column table and status machine.

---

## Lifecycle of a task

```
poll → resolve Human-queue cards whose done_when passes (throttled by
        LIFEOS_HUMAN_QUEUE_POLL_SECONDS; runs before the spend guard — it
        never spends)
     → heal stranded lifecycle tags (#agent-running / #agent-blocked) whose
        backing session is at a terminal status — also ungated by the spend
        guard, since it reconciles existing state and starts no work
     → spend tracker check (`can_start_task(default_budget)`)
     → wake sleeping sessions whose timer expired
     → poll managed sessions for state advancement
     → resume yielded-for-children sessions if children done
     → dispatch spawned sessions (drain pending_messages)
     → process clarification answers (Telegram replies)
     → timeout stale clarifications (default 72h)
     → list /api/tasks across AGENT_PICKUP_STATUSES (`todo` + `urgent`) ×
       AGENT_PICKUP_TAGS (`#agent` + every engine assignee + Managed Agents
       consent tags), dedupe by id,
       keep candidates with `#agent` OR an engine assignee OR a consent tag,
       drop any task that already carries a lifecycle claim/terminal tag
     → for each candidate:
         atomically re-check status, pickup tag, and lifecycle exclusions;
         replace legacy `#agent` or append `#agent-running`, and set in-progress
         create session row + transcript "claim" event
         (session-create failure rolls the tag back: swap to `#agent` when
         that was the original handoff, otherwise remove `#agent-running`)
         run preflight (Haiku) → PreflightResult
             routing in {local, claude, claude_code, codex, ask}
             expected_output in {text, file, external_action, structured}
             ambiguity: question | null
             sane: bool
         dispatch on routing:
             local              → LocalExecutor.execute(session, task)              — inline, tick thread
             claude             → ManagedExecutor.start(session, task)              — inline, tick thread
             claude_code / codex → _submit_cli_dispatch(...)                        — off-tick, on _cli_pool
             ask                → Telegram clarification, park at #agent-blocked
         on terminal outcome:
             COMPLETED → mark task done in vault + swap to #agent-completed
                         + write Agent Output note (one-off, or prepend for recurring)
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
                 ├────► BUDGET_EXCEEDED  (terminal — operator replied `stop` to a budget question)
                 ├────► BLOCKED     (waiting on Telegram, or Managed Agents not configured)
                 └────► YIELDED     (sleep tool / yield_until / budget breach — wake on timer, child completion, or an operator reply)
```

`BUDGET_EXCEEDED` is reached only by an operator's `stop` reply on an in-process route (local, remote-forced, or Managed Agents) — a wall/token/dollar/lineage-dollar breach itself lands on `YIELDED`, not this status; see [Budget enforcement](#budget-enforcement). The Claude Code and Codex CLI routes carry no budget enforcement at all and never produce this status.

Each session also tracks `routing`, `budget`, `expected_output`, `total_input_tokens`, `total_output_tokens`, `total_dollars`, `managed_agent_session_id`, `started_at`, `parent_session_id`, `root_session_id`, `spawn_depth` (for lineage budgets), and `yield_waiting_for` (when in `YIELDED` from `yield_until`).

---

## Preflight

The preflight classifies a task before executor dispatch, cheap (~$0.001) and fast (~1s). Which LLM client runs the classifier call is controlled by `LIFEOS_AGENT_PREFLIGHT_ENGINE`, default `auto`:

- **`auto`** (default) — Anthropic (`claude-haiku-4-5` by default) when `ANTHROPIC_API_KEY` is set, without a reachability probe; else the local llama-server if reachable; else the remote provider described under "Local executor" below, if configured and enabled (`LIFEOS_AGENT_REMOTE_EXECUTOR` + `remote_llm_configured`); else the call raises, which `run_preflight()` degrades to `sane=True` (with `preflight_error` recorded)/`routing=ask` like any other preflight failure. This keeps an install with no Anthropic key from failing every engine-assigned task at the classification step, before the local-executor fallback below ever gets a chance to run.
- **`remote`** — build the remote OpenAI-compatible provider (e.g. Fireworks running DeepSeek) first, when `remote_llm_configured`. Built the same way the `auto` chain's own remote fallback is, and used **unprobed** by design (the same convention the `auto` chain already follows for that branch — the remote client is trusted, not health-checked) — so this never adds a reachability check that wasn't already implicit in the request itself. A failure of the completion call is not caught specially; it propagates to `run_preflight()`'s existing except-clause exactly like a failure on any other engine. If the provider *isn't* configured, the call raises — a forced engine never silently falls back to another one, and in particular never to the Anthropic API, which is the spend `remote` exists to avoid; `run_preflight()` degrades the raise to `routing=ask`, so the operator sees a confirmation question rather than a surprise API bill. Operator motivation: all five observed field instruction-deviations were Haiku's, while the remote provider has executed real tasks cleanly — classifier engine choice is a quality lever, not a safety dependency (routing/ambiguity/sanity opinions already can't cancel, bypass the default route, or block under one — see the demotion sections below).
- **`anthropic`** — force the Anthropic branch. Falls through to `auto` (with a logged warning) if no API key is configured.
- **`local`** — force the local llama-server client. Still probed via `is_available()`, same as the `auto` chain's own local branch — but since there's no further engine to fall back to for a forced value, an unreachable server raises (degrading via the same except-clause) rather than silently trying something else.
- Any other value is treated as `auto`, with a logged warning — never a crash over a typo'd env var, mirroring `LIFEOS_AGENT_DEFAULT_ROUTE`'s own invalid-value handling below.

This only chooses which client classifies a task — not which engine the task itself dispatches to. Spend attribution: preflight calls do not write to the usage store (`usage_store.record_usage`) on **any** engine today — usage recording is caller-side and lives only in the chat and Hermes-proxy routes, not in `llm_client.py` or the agent worker. A preflight call on the remote engine is exactly as unattributed as one on Anthropic or local, so `remote_llm_*_price_per_mtok` never sees a preflight-driven row. Returns:

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
    demoted_ambiguity: str | None   # ambiguity text demoted to advisory, if any
    demoted_routing: str | None     # LLM route demoted to the default, if any
    demoted_sanity: str | None      # non-fatal sane_reason demoted to advisory, if any
    destructive_score: float | None       # Jev harm score (0-4), if judged
    destructive_probability: float | None # Jev irreversible probability, if judged
    destructive_block: bool               # True when the block gate parked the task
    raw: dict                       # the parsed JSON for debugging
```

Routing precedence (per the prompt instructions):

1. `#local` tag → routing=local
2. `#cloud` tag → routing=remote (the configured remote OpenAI-compatible provider, never Anthropic); `#cloud-haiku` / `#cloud-sonnet` tag → routing=claude
3. Title contains explicit model cue ("use claude", "with opus", "using gemma", "use the anthropic api") → claude / local
4. Title contains capability-implying phrase ("search my gmail", "google drive", "send a slack message", etc.) → claude (those tools require cloud connectors)
5. Otherwise → `LIFEOS_AGENT_DEFAULT_ROUTE` if set, else ask (worker pauses the task and asks via Telegram)

`LIFEOS_AGENT_DEFAULT_ROUTE` (empty by default) applies when preflight would otherwise land on `ask` purely for lack of routing cues — not when a *fatal* sanity failure is the reason, and not when the classifier inferred a cloud route that the API-consent downgrade below sends to `ask` — **and also** when the model returned a `local`/`claude_code`/`codex` route on its own initiative but the title doesn't corroborate it. A *non-fatal* sanity failure is demoted ahead of this substitution check, so it does not exclude a task from it either. It exists for a single-executor install (e.g. local-only, no Claude Code/Codex/Managed Agents) where a multi-engine clarification question has nothing useful to offer. Tag overrides (`#local`, `#cloud`, etc.) always take precedence over it. An invalid value logs an error and falls back to `ask` rather than crashing the worker loop.

**Ambiguity demotion.** When `LIFEOS_AGENT_DEFAULT_ROUTE` is set to a valid route, a non-null `ambiguity` does not block the task, regardless of what routing was ultimately picked (default-route substitution, a corroborated LLM route, or a tag override). Configuring a default route is the operator saying "run untagged tasks without asking me"; a cheap classifier's hedging shouldn't override that standing instruction — especially since string-matching the hedge's prose proved to be whack-a-mole once the model started rephrasing around the pattern. The question is preserved on `demoted_ambiguity` and logged to the session transcript as advisory context rather than discarded, and the executing agent can still ask a specific question mid-run via `lifeos_agent_user_ask` if it genuinely needs to. The unconfirmed-cloud downgrade still blocks either way (never auto-spend on inferred cloud routing). With no default route configured, ambiguity blocks.

**Sanity demotion.** The same standing-instruction argument applies to sanity: a *non-fatal* `sane=false` — the classifier's own inferred "this isn't executable" opinion, as opposed to a `sane_fatal` verdict the code itself established (empty title, or the deterministic destructive-title regex) — is demoted to advisory under the identical gate (`LIFEOS_AGENT_DEFAULT_ROUTE` set and valid). A failed or unparseable preflight call carries no verdict at all, so it is never `sane_fatal` in the first place — it sets `sane=True`/`sane_fatal=False` and records `preflight_error` instead. This matters because the classifier can misjudge ordinary feature requests as "a product specification or feature request, not a task an agent can execute" — building features is half the point of this pipeline — and turning that misjudgment into a park (rather than a cancel) still costs the operator a confirmation round-trip for legitimate work every time it fires. `sane_reason` is preserved on `demoted_sanity` and logged to the session transcript the same way `demoted_ambiguity` is, and `sane` itself flips back to `True` — which also means a demoted sanity objection does not block the default-route substitution below it (a task that is both sanity-flagged and routing-`ask` both demotes *and* routes on the same pass). `sane_fatal` verdicts are completely unaffected by this setting in either direction — they fail closed regardless of whether a default route is configured. The preflight prompt itself carries an explicit line ("feature requests and product specifications ARE executable tasks... never mark them insane") as defense in depth, since the classifier can ignore negative constraints in its prompt — the demotion is what actually holds. A `destructive_block` verdict (see the Jev destructiveness judgment below) is excluded from this demotion the same way `sane_fatal` is — it's a code-thresholded Jev verdict, not the classifier's own opinion, so it stays parked regardless of the setting. With no default route configured, non-fatal sanity still parks.

**Route corroboration.** A default route only rescues a genuine `ask` outcome for a *cloud* route — the classifier naming `local`/`claude_code`/`codex` on its own is not challenged the same way, because those routes aren't `ask` and so skip the substitution above entirely. Whenever a default route is configured and valid, an LLM-chosen `local`/`claude_code`/`codex` route must be corroborated by the title — `routing_explicit=true` from the model *and* a matching cue (the rule-3 phrasing for `local`; "claude code" / "codex" for the CLI routes) — or it's demoted to the configured default and logged, mirroring the ambiguity demotion above (`demoted_routing` holds the route the model actually picked); this guards against a noncompliant classifier inventing an explicit-looking route (e.g. `routing="local"` with a plausible-sounding but non-cue reason) and having it silently beat a configured default. `routing_explicit=false` never corroborates, regardless of the title. Tag overrides are unaffected (a tag is the operator's own corroboration, checked first). `ROUTE_CLAUDE` is out of scope for this check — the unconfirmed-cloud downgrade below already corroborates cloud routes against the title, and on a miss sends them to `ask` (a confirmation question) rather than to the default, since unconfirmed API spend must stay a question even on a default-route install. With no default route configured, this is a no-op.

**Preset class (Jev).** `_apply_preset_class` sets `result.preset_class` from, in order: an explicit `#<class>` tag (always wins); else a Jev fan-out judgment (`jev_task_routing.judge_task`) when it clears two guards; else left unset, same as an install with no key. A wrong narrow class is worse than the unfiltered default — it removes tools from the session — so the Jev class is only honored when BOTH `preset_class.confidence >= 0.7` AND `software_work.noul < 0.5` (a task the judgment itself flags as likely software work always keeps the full toolset, regardless of class confidence). Either guard failing leaves `preset_class` unset, not a lower-confidence fallback value. The judgment is one call shared with working-directory and plan-mode resolution (see "Working directory" below and [agent-worker-setup.md](../../guides/agent-worker-setup.md#typed-judgments-jev)) — `judge_task` is memoized per task title so the three consumers don't triple the call count.

Hardening: response is parsed defensively (handles `` ```json `` fences, partial schemas, missing keys, exceptions). On any parse failure the result defaults to `sane=false` so the worker parks the task rather than running with garbage.

**Jev destructiveness judgment.** Alongside the regex-based sanity gate above, preflight can ask TypeSafe's Jev (see [Typed judgments (Jev)](../../guides/agent-worker-setup.md#typed-judgments-jev)) two questions about the instructions that will execute: a 5-level harm `Score` (read-only ... irreversible mass/external loss) and an `irreversible` `Noul` (the probability that running the task as written permanently destroys data or sends something unrecallable). Ordinary tasks send their title. A project child also sends a separate, at-most-8,192-character `execution_instructions` value assembled from its child title/notes and bounded parent title/objective/acceptance notes; this bound accommodates the complete 8,097-character maximum assembled context without trimming the parent acceptance tail. Sibling status/output and unrelated task content are excluded. This is additional remote data sent only when the operator configures TypeSafe and leaves the gate enabled. The safety context never enters the Haiku classifier prompt or `jev_task_routing.judge_task`, so parent engine/model words cannot corroborate a route, choose a preset, or grant cloud consent.

`LIFEOS_AGENT_JEV_DESTRUCTIVE_GATE` controls what happens with the answers — `off` (no Jev call), `shadow` (default: the answers are recorded on `PreflightResult.destructive_score`/`destructive_probability` and logged in the preflight transcript event, but never change `sane`/`sane_fatal`), or `block` (a harm score >= 2.5 or an irreversible probability >= 0.85 parks the task for operator confirmation — non-fatal `sane=False`, `sane_fatal` stays `False`, and `destructive_block` is set `True`). This judgment runs immediately after the regex gate, so a title the regex already matched keeps its fatal verdict regardless of what Jev says; the regex stays in force in every gate mode. Unlike an ordinary non-fatal sanity opinion from the classifier, a `destructive_block` park is a code-thresholded verdict over a calibrated probability — `LIFEOS_AGENT_DEFAULT_ROUTE`'s sanity demotion (§ above) checks `destructive_block` and leaves it parked even on an install where a default route is configured, rather than demoting it to advisory the way it demotes the classifier's own inferred opinion. Effectively `off` whenever no TypeSafe key is configured, regardless of the setting. A failed Jev call logs a warning and leaves both fields `None` — no task fails or blocks because of it.

---

## Local executor (Gemma path)

`LocalExecutor.execute(session, task) -> ExecutorOutcome`. Wraps an agent loop against an OpenAI-compatible local LLM server (llama-server with `unsloth/gemma-4-26B-A4B-it-GGUF` by default).

**Remote fallback (`LIFEOS_AGENT_REMOTE_EXECUTOR`, off by default).** When enabled and an OpenAI-compatible remote provider is fully configured (`LIFEOS_REMOTE_LLM_URL`/`_MODEL`/`_API_KEY`, see [configuration.md](../../guides/configuration.md#openai-compatible-remote-provider)), a session-start reachability check that finds the local llama-server unreachable runs the session against the remote provider instead of failing — one cheap `is_available()` probe at session start, not a background prober. This exists for an install with no other agent executor at all (no Claude Code, no Codex, no Managed Agents, no reachable llama-server); flag off, or the remote provider unconfigured, is byte-identical to the local-only path. It is a fallback, not a new route: an explicit `#local` tag on a host with a live llama-server is unaffected. The escalation ladder can never reach this path — its `local` rung goes through `agent_loop.py`'s `_select_client(force_local=True)`, a separate code path that never consults this flag.

**Remote route (`ROUTE_REMOTE`, the `#cloud` tag) — distinct from the fallback above.** `_remote_only_llm_client` builds the same kind of `LocalLLMClient` pointed at the remote provider, but unconditionally: no `agent_remote_executor` flag check, no local-reachability probe. Tagging a task `#cloud` is itself the opt-in. `Worker._get_remote_executor` constructs a `LocalExecutor` from it (cached separately from the local one, so a mixed local + `#cloud` install never has one route silently swap the other's target client), and `_dispatch`'s `ROUTE_REMOTE` branch requires `settings.remote_llm_configured` first — unconfigured parks the task at `#agent-blocked` rather than falling back to local or Anthropic. Attribution and pricing reuse the fallback's own machinery unchanged: `is_remote=True` drives `_record_spend` (priced from `remote_llm_{input,output}_price_per_mtok` when set, else real unpriced spend) and `_served_by()` (the remote model id, surfaced via `_model_label_for_routing`/`_worker_label` as "Remote").

Per-turn flow:

1. Check budgets at top-of-loop, re-read fresh from the row each iteration (not a copy captured before the loop started, so an operator's cap extension on resume takes effect immediately) — yield with `STATUS_YIELDED` and `termination_evidence["budget_breach"]` set if `total_tokens >= max_tokens` (only when a title hint set one) OR `wall_seconds_elapsed >= wall_seconds` OR the lineage budget is breached; see [Breach → yield → ask](#breach--yield--ask). There is no per-session dollar cap check on the local route (local inference is free, so `total_dollars` is always 0) or the remote-forced route (a pre-existing gap this decision doesn't close); the lineage check still enforces the lineage root's own `max_dollars` for a mixed family rooted at a paid managed session.
2. Build the message list (system prompt + prior user/assistant/tool_result turns).
3. Call the local LLM with the tool catalog (`STANDARD_HANDLERS` + `lifeos_agent_*` inter-agent tools + LifeOS MCP tools, all in OpenAI format).
4. Parse response — handles both OpenAI `{"function": {"name", "arguments"}}` and Anthropic `{"name", "input"}` shapes via `_normalize_tool_calls`.
5. If response has tool_calls: dispatch each to `ToolRegistry`, append `tool_result` turns, loop.
6. If no tool_calls and content is non-empty: finalize with `STATUS_COMPLETED` + `final_text`.
7. If tool emitted a yield (sleep: `yield_seconds > 0`; `yield_until`: `yield_seconds == -1`): set `STATUS_YIELDED` and return — the worker's sleep / yield-resumption loops take over.

Cost: `$0` when served by the local llama-server (`local` maps to `$0` in `pricing.py`). A session served by the remote fallback above is priced from `LIFEOS_REMOTE_LLM_INPUT_PRICE_PER_MTOK`/`_OUTPUT_PRICE_PER_MTOK` when configured, else recorded as real unpriced spend rather than $0 — the same convention every `unpriced` usage row uses. Wall-time enforcement still applies either way.

### Working directory

Both `ROUTE_LOCAL` and `ROUTE_REMOTE` share this one executor, so a single guard covers a task pinned to either. A card names a directory with the same `[key:: value]` inline-field convention `assignment.py` reads `host`/`model`/`effort` from: `[working_dir:: /srv/checkouts/example]`. That explicit field is authoritative. A normal board dispatch may otherwise derive a workflow location through the bounded affinity/title rules below; a direct executor call with no resolved `working_dir` runs in the worker process's own working directory.

`resolve_working_directory(task, *, allow_uncloned=True)` asks the Jev fan-out judgment (`jev_task_routing.judge_task`) first: a `location` answer with confidence >= 0.6 resolves against the union of the operator's GitHub repositories (`directory_resolver._github_repos()`, `gh repo list <owner>`, disk-cached 24h with an atomic temp-file + `os.replace` write), the scanned local project directories, LifeOS, the vault, and home — a repo Jev names that isn't cloned on this host still resolves to a path under `code_dir`. Below that confidence, or with no Jev judgment at all, it falls back unchanged to the keyword cascade. Every GitHub repo name — freshly fetched from `gh` or read back from the on-disk cache — is validated (`^[A-Za-z0-9._-]{1,100}$`, and never bare `.`/`..`) before it's trusted for a path; a name that fails validation is dropped, and the cache's own `path` field is never deserialized — every path is rebuilt from the validated name.

`resolve_location_affinity(value)` accepts only a name present in that same catalog and returns `None` for every other value; it never interprets an arbitrary `fields.project` string as a path. A hierarchy child resolves one write-once execution target in this order: explicit child `working_dir`; recognized child affinity; parent `working_dir` when the parent's host and the child's actual execution host are the same; recognized parent affinity; title/Jev fallback. Affinity and title/Jev mappings are API-host paths, so a CLI session assigned to a different `LIFEOS_AGENT_HOSTS` host skips both mappings. A remote child may still use its explicit directory or a parent directory explicitly scoped to that same remote host; otherwise its persisted working directory remains unset and the remote CLI starts in its own default directory. Codex preserves that unset state through command construction: the remote command omits `-C` instead of substituting the API process's current directory, while the local `ssh` client starts from the API process's directory without exposing that path to the remote command.

In-process `local`/`remote` routes accept an affinity or title-derived path only when it is an existing directory on the API host. They call the title resolver with `allow_uncloned=False` and recheck the returned fallback, so an uncloned catalog result or a missing keyword fallback cannot become a frozen invalid path. Explicit child and compatible parent `working_dir` fields remain authoritative and reach the executor's ordinary path validation unchanged. Local CLI routes retain clone-on-demand for recognized repositories.

For a local CLI spawn, the worker clones an uncloned repo (`directory_resolver.ensure_cloned`, `gh repo clone <owner>/<name>`) right before spawning. `ensure_cloned` independently re-validates the target — refusing (no `gh` invocation at all) unless the path resolves to exactly `code_root / <name>` for a `name` that's both well-formed and present in the operator's known repo list — so a manipulated cache file or an arbitrary affinity can't reach a real clone. A clone failure parks the task at `#agent-blocked` naming the repository instead of spawning into a missing directory.

`local_executor._resolve_task_working_dir(task)` runs before conversation seeding or any LLM call, so a refused directory never reaches the model:

1. Unset or blank `working_dir` → `(None, None)` — unchanged behavior.
2. Path doesn't exist, or exists but isn't a directory → refused, naming the path.
3. Path resolves (`Path.resolve()`, so symlinks and `..` are collapsed first) to the worker's own checkout — the repository root containing the running `api/` package, computed from `local_executor.py`'s own file location, never a configured or hardcoded path — or to anything inside it → refused. The live service's source tree can never be a task's target, even one directory level down.
4. Path is an ancestor that would *contain* the checkout (naming a parent directory, however many levels up) → refused with a distinct reason. `tools._resolve_within_base` approves any path under the named base, so a directory one level above the checkout would let Read/Write/Edit reach the checkout's own files just as surely as naming it directly — the guard checks both directions.

A refusal returns `ExecutorOutcome(status=STATUS_FAILED, reason=...)` from `execute()` before the loop starts, which flows through the worker's ordinary `_handle_outcome` → `STATUS_FAILED` path: same vault tag swap and Telegram notify every other executor failure gets, naming the refused path in the reason. No separate failure channel exists for this guard.

Once validated, the resolved directory is threaded through `ToolRegistry.dispatch(name, args, base_dir=...)` for the rest of that `execute()` call — not stored on the (potentially test-injected, longer-lived) `ToolRegistry`/`LocalExecutor` instance, so a working directory from one task can never leak into another dispatched through the same executor object. `tools._resolve_within_base` re-resolves every Read/Write/Edit `file_path` against it (relative paths join onto the base; absolute paths are accepted only if their resolved form still falls inside it) and rejects a `..`/symlink escape the same way the task-level guard does; Bash gets it as `cwd`. This is a path-resolution guard, not a sandbox — a Bash command can still `cd` elsewhere or read/write an absolute path outside the working directory, exactly as an ungoverned shell always could (out of scope for this guard; see the module docstring in `tools.py`). The LifeOS MCP tool surface and the `lifeos_agent_*` inter-agent tools are unaffected by `working_dir` — they reach the filesystem only through the configured `settings.vault_path` over HTTP, never through `base_dir`, and were never part of this threat model.

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

Cost accounting: delta-tracks token spend each poll using `pricing.cost_for(model, …)` and adds `(wall_seconds / 3600) × $0.08` session-hour overhead. A mid-flight dollar or token breach yields (`STATUS_YIELDED`, `termination_evidence["budget_breach"]` set) rather than killing the remote session, leaving the session alive and untouched; see [Breach → yield → ask](#breach--yield--ask). `poll()` itself refuses to make the `get_session_state` provider call at all while the session's `budget` pending question is still open (`SessionStore.has_open_budget_question`), returning the ordinary `STATUS_RUNNING` no-op signal instead — the same early-return shape the stale-lifecycle-turn guard above it uses. This is unrelated to the separate runaway-detection kill (tool-loop / no-progress, `_detect_runaway`), which calls `DELETE /v1/sessions/{id}` and finalizes as `STATUS_BUDGET_EXCEEDED` immediately — that mechanism isn't a budget dimension.

### MCP-init failure handling

The Managed Agents API emits `session.error` events at session-start for any MCP that fails to initialize (URL doesn't match a Vault credential, OAuth invalid, host unreachable, etc.). These are **informational** — the agent works around the missing MCP. The driver filters `mcp_authentication_failed_error` / `mcp_connection_failed_error` types out of the failed-status synthesis. Affected MCP names are persisted in `managed_cursor.init_failed_mcps_json` (across polls, since they fire on the first batch but the session may not idle until later) and surfaced as a footer in the completion Telegram summary.

### Empty-final-text carry-forward

`agent.message` events with the agent's final text can arrive in an earlier poll batch than the `session.status_idle` event. The driver's per-batch `_extract_final_text` would return `None` on the idle-only batch. The executor mitigates this by caching `final_text` to `managed_cursor.final_text` on every poll where the driver returns a non-None value, and reading it back at finalize.

---

## Card assignment

A Kanban card assigns a task to an engine (an assignee tag — `#claude`, `#codex`, `#local`, `#hermes`, `#cloud`, or Managed Agents consent tags `#cloud-haiku`/`#cloud-sonnet`), a model, an effort level, and a host, written as `[key:: value]` inline fields (`model`, `effort`, `host`, `assigned_by` — round-tripped verbatim by `Task.fields`, see [task-management.md](task-management.md)). An engine assignee on a todo/urgent card is itself the claim handoff — the worker's pickup list fans out by those tags (plus `#cloud-haiku` / `#cloud-sonnet`). `assignment.py`'s `extract_assignment()` reads those four fields; `worker.py`'s `_dispatch()` calls it right after preflight and records `host`/`model`/`effort` onto the session row before any executor runs (`SessionStore.set_assignment`) — the same place `claude_code_model` has always been set. `assigned_by` is recorded for bookkeeping only — it plays no role in routing. Reassignment context is bounded and included in the first prompt for fresh local, remote, Hermes, Managed Agents, and CLI routes; native continuations retain their durable session history. The preflight bypass that lets a card's assignee tag skip route corroboration comes from the tag itself: `_apply_tag_overrides` returns as soon as it matches a routing tag, before `_apply_route_corroboration` ever runs (`worker.py`'s comment at the assignment-persist call site notes the same thing).

### Effort mapping

The board's own effort vocabulary is exactly `low | medium | high | max`. Each engine speaks a different vocabulary — `assignment.py`'s `map_effort_for_engine()` translates once, in one place:

| Board effort | Claude Code (`--effort`) | Codex (`-c model_reasoning_effort=`) | Local Gemma | Hermes |
|---|---|---|---|---|
| `low` | `low` | `low` | thinking off | no override |
| `medium` | `medium` | `medium` | thinking off | no override |
| `high` | `high` | `high` | thinking on | no override |
| `max` | `max` | `xhigh` | thinking on | no override |
| unset | CLI default | CLI's own config | `settings.local_agent_enable_thinking` | Hermes reports its own model |

Local Gemma's thinking toggle is a per-SESSION override (`local_executor.py`'s `_call_llm(session_id, effort=...)`), not a mutation of the global `settings.local_agent_enable_thinking` — concurrent sessions with different assigned efforts would otherwise race each other over one process-wide flag. It only ever reaches a real `LocalLLMClient` on the local (not remote-forced) route, mirroring `run_agent_loop`'s identical `isinstance(...) and not force_remote` gate — the remote OpenAI-compatible provider doesn't understand llama-server's `chat_template_kwargs` switch.

### Host registry and ssh spawn

`LIFEOS_AGENT_HOSTS` (`{name: ssh_target}`, e.g. `{"laptop": "user@laptop.example"}`) maps a board-facing host name to an ssh target. The board's host picker (`GET /api/agents/hosts`) reads this same registry — see [Host catalog](#host-catalog) below. `remote_spawn.py` is the shared mechanism:

- `resolve_host_target(host, api_host_name)` — empty/unset `host`, or a match on this API's own hostname, means local (returns `None`, the existing `spawn_fn` seam runs unchanged). Anything else must be a registry key or `resolve_host_target` raises `HostResolutionError` — the executor fails the task closed (`#agent-failed`, reason naming the host) **without ever calling `spawn_fn`**.
- `build_remote_argv(argv, target, unset_env_names, session_id)` — wraps the exact local argv into `ssh -o BatchMode=yes -o ConnectTimeout=<setting> <target> -- <remote command>`. The remote command unsets every credential name `_clean_env` strips locally (mirrored via `env_names_matching_prefixes`, applied to the remote command's `env -u` prefix instead of the local Popen `env=` kwarg), explicitly sets `LIFEOS_AGENT_SESSION_ID=<session_id>`, then wraps the whole thing in `setsid bash -c 'echo "PGID:$$"; exec "$@"' _ …` so the remote process group id is captured as the first stdout line. SSH does not forward arbitrary environment variables; the explicit assignment lets the remote CLI's stdio MCP child inherit the process-bound trusted identity accepted by inter-agent calls. The executor strips that line (`read_remote_pgid_line`) before normal event-stream parsing and persists it via `SessionStore.set_remote_pgid`.
- The Popen call site itself is untouched by any of this — only `cmd` (built beforehand) and the local `_clean_env()`-sourced `env=` differ between the local and remote branches, which is what keeps the injection seam (`spawn_fn`/`binary_resolver`) a pure test seam rather than something remote spawn has to special-case.
- Reading that `PGID:` line back is bounded on its own clock (`remote_spawn.read_line_with_deadline`, `settings.agent_ssh_connect_timeout + 5` seconds), separate from and ahead of each executor's usual wall-clock watchdog — `ssh -o ConnectTimeout` only bounds the TCP handshake, not a stall during auth or a host that accepts the connection but never answers. A timeout here fails the task with `host <name> did not answer within <n>s`, distinct from the ordinary execution-timeout failure.
- Kill: `POST /sessions/{id}/kill` on a session whose `host` is set runs `ssh <target> kill -- -<pgid>` through an injectable runner (`remote_spawn.kill_remote_process_group`) instead of the local `os.killpg` — see `inter_agent.py`'s `_kill_remote_subprocess`. An unregistered host degrades to a DB-only kill, the same way a missing local pid event does.
- Resume/focus (`api/routes/agents.py`): a session whose `cli_sessions.host` names a registered host runs the same rendered launcher template over ssh (`remote_spawn.build_remote_launcher_argv` — no pgid capture, no credential stripping; a launcher is a short-lived terminal spawner, not the long-running CLI session). Its cwd comes from the `cli_sessions` row (populated by the remote host's own hook post) rather than a local transcript-file scan, which a remote session's files were never going to satisfy. `/focus` has no cross-host pane registry to activate an existing pane against (the `cc_wezterm_store` mapping is only ever written for a session that ran on THIS API host), so for a remote session it runs the same launcher `/resume` does and returns `/focus`'s response shape.

**macOS FDA limitation (documented, not solved):** Apple-data tasks (iMessage, Photos, Contacts) require Full Disk Access, which is granted per-app to the process that launched a session — not something an ssh-spawned remote process can inherit. Assigning an Apple-data task to a remote macOS host over this mechanism will not have FDA; the Apple Data Agent's own export/import pipeline ([operations.md](../../guides/operations.md)) remains the supported path for cross-machine Apple data.

### Hermes route

`ROUTE_HERMES = "hermes"` (`preflight.py`) is a new tag-only route — like `claude_code`/`codex`, preflight's own classifier JSON schema never emits it; `#hermes` in `_apply_tag_overrides` sets it. `HermesExecutor` (`hermes_executor.py`) reuses `hermes_proxy._build_envelope` (persona/turn-context resolution) and `_HermesTurnPersister` (conversation + usage persistence) so a board-assigned Hermes turn's rows are indistinguishable from one that came through `/chat` — three small read accessors (`conversation_id`, `content_text`, `done_seen`) exist on that class for this reuse. Runs synchronously on the worker's tick thread (a bounded HTTP round trip, not a long-lived subprocess — unlike the CLI routes, which run off-tick through the CLI dispatch pool). The card's prompt is `task["description"]` plus `task["notes"]`, if any. On completion the session row stores `conversation_id`; the card's `session.open_url` becomes `/chat?conversation=<id>` — `web/chat/main.js` reads that query param on boot (after the backend restore settles) and opens the thread, purely additive to the SSE contract in [client-surfaces.md](client-surfaces.md).

### Board open

`POST /api/agents/board/cards/{id}/open` (`api/routes/agent_assignment.py` — a router sharing the `/api/agents` prefix with `agents.py` rather than appending to that file, keeping the card-assignment surface and the Kanban board UI surface in separate files) requires the card to be in Assigned state: `status == "todo"`, a recognized assignee tag, and no running session (worker-dispatched or a prior interactive open) already against it. `claude`/`codex` spawn the interactive CLI in a terminal — reusing the `cc_resume_cmd`/`codex_resume_cmd` launcher templates, with `{inner_command}` rendered as a fresh `env LIFEOS_TASK_ID=<id> claude|codex "<prompt>"` invocation rather than a `--resume <id>`. `scripts/lifeos-agent-hook.sh` forwards `$LIFEOS_TASK_ID` as `task_id` on every lifecycle event it posts; `POST /cli-sessions/events` moves a `task_id`-bearing `session_start`'s card from `todo` to `in_progress`. `hermes` has no terminal to spawn — returns `{open_url: "/chat?conversation=<id>"}` once the card has one, else 409.

Two more 409s beyond the Assigned-state/tag/already-running checks: `_OPENING_GRACE_SECONDS` (30s) holds a card claimed-but-not-yet-registered so a double-click can't spawn twice before the first spawn's session shows up (`card open is already in progress`); and a card's `host` field naming a value absent from `settings.agent_hosts` fails closed (`host '<name>' is not configured in LIFEOS_AGENT_HOSTS`) rather than silently falling back to local.

### Model catalog

`GET /api/agents/models` (`model_catalog.py`) retains the compatibility response `{engines: {claude, codex, local, hermes, remote}, refreshed_at, stale}`, with each engine's list merged with `pricing.PRICING`. Its additive `engine_states` map reports independent discovery states (`loaded`, `empty-valid`, `unavailable`, `unconfigured`, or `unknown`), `models`, observation and last-success timestamps, staleness, safe reason codes (including `staleness_reason`), and an explicit unknown quota state; the separate top-level `readiness` map reports route state (`configured`, `ready`, `unavailable`, or `unknown`), source, and timestamp. Sources remain bounded: Anthropic uses the SDK's model-list endpoint when configured with a short client timeout; Codex uses its local CLI cache and may use the existing OpenAI model-list fallback only when an API key is configured; local uses the running llama-server's `/v1/models`; Hermes uses the last observed turn (`model_readout.get_hermes_models`) and is never probed; `remote` is declared from `settings.remote_llm_model`/`remote_llm_model_options` rather than discovered — the OpenAI-compatible remote provider has no models-list endpoint to probe. A configured subscription CLI can therefore be route-ready while discovery remains unknown when its local cache and optional API key are absent. Refreshes are isolated per engine: a failed or uncertain engine preserves its last-good model list and is marked stale with its reason, while other engines continue refreshing. `ModelCatalog.facts()` / `facts_from_catalog()` exposes a small policy-neutral projection for execution consumers, including distinct `claude_code` and `remote` route facts that have no model discovery source; it reports facts only and does not select or fall back between engines.

The response also carries a top-level `defaults` map (`{claude, codex, remote}`), recomputed every refresh. For `claude`/`codex`, `pick_family_default()` picks the newest id (by parsed version tuple) in that engine's live list whose family segment matches `settings.agent_default_model_family_claude`/`_codex` (default `opus`/`sol`) — claude ids parse as `claude-<family>-<version...>` (dated snapshot suffix stripped first), codex ids as `gpt-<version...>-<family>`; ties (equal version tuples, e.g. a dated snapshot and its bare alias) are broken by list order. No match, an empty/unavailable list, or an unsupported engine yields `null`. For `remote`, it's `settings.remote_llm_model` or `null`.

`Worker._resolve_session_execution` uses this to fill in `model_id` for a `claude_code`/`codex` dispatch that named none: right after `resolve_execution()` returns, if the resolved spec's executor is a CLI executor and `model_id` is still `None`, `Worker._catalog_default_model(executor)` looks it up and, if found, the spec is replaced with that model_id (provenance `catalog_default`) before persisting — so `--model` is passed and a later retry/resume reuses the same pin (persisted specs are never recomputed). The lookup goes through an injectable `model_catalog_defaults_provider` constructor parameter — `None` (the default; every test and tool that constructs a bare `Worker()` gets this) performs no I/O and yields no default, keeping today's no-`--model` behavior; `main()` wires the real provider, a synchronous `asyncio.run(get_model_catalog().get())` bridge (the worker's poll loop has no running event loop of its own to await on), for the production process. Only `claude_code` and `codex` fall back to a catalog default when no model is pinned; `local`/`hermes`/`remote` dispatch does not. Independently, `SUPPORTED_EXPLICIT_FIELDS["remote"]` permits `model_id`, so a `#cloud` card's board `model` field reaches `ExecutionLayer.from_assignment`/`resolve_execution` and the persisted `execution_spec.model_id` the same way claude/codex do; `local_executor.py`'s `_call_llm` pins `create_kwargs["model"]` from `session.execution_spec["model_id"]` for the remote-forced client. The board's model picker (`assignment.js`, below) is what decides whether a `#cloud` card can offer a model choice at all.

### Host catalog

`GET /api/agents/hosts` (`host_catalog.py`) returns `{hosts: [{name, ssh_target, online, is_api_host}], refreshed_at}` — the source for the board's host picker. `HostCatalog` mirrors `ModelCatalog`'s injectable-everything TTL-cache pattern (`status_runner` + `clock` seams, `probe_call_count` for tests), kept deliberately simpler: one probe instead of four provider fetches, and a much shorter TTL (`_HOST_CATALOG_TTL_SECONDS`, 30s — reachability drifts minute to minute, unlike a model list) that's a module constant rather than a new setting.

The list is always the API host itself (name from `remote_spawn.api_host_name()`, `is_api_host: true`, `online: true` unconditionally — the API process IS this host) plus every entry in `settings.agent_hosts`, deduplicated by name: a registry entry that happens to name the API host merges into that one row (carrying its `ssh_target`) rather than producing a duplicate. Unlike the model catalog, a cache MISS always re-reads `settings.agent_hosts` fresh — the registry is local config, not a network call, so there's no reason to let it drift stale between probes.

Reachability for every other host comes from `tailscale status --json`, run through an injectable `status_runner` (`subprocess.run(..., timeout=_TAILSCALE_TIMEOUT_SECONDS)`, 1.5s — comfortably inside the route's 1.8s ceiling) inside `asyncio.to_thread`. A registry host is matched case-insensitively against each Tailscale peer (including `Self`) by comparing the registry name and the ssh_target — `user@` prefix and `:port` suffix stripped — against the peer's `HostName` and the first label of its `DNSName`. `online` is:

- `true`/`false` — the probe ran, returned valid JSON, and matched this host to a peer; that peer's `Online` boolean.
- `null` — the probe is inconclusive: `tailscale` isn't installed (`FileNotFoundError`), the command exited non-zero, the JSON didn't parse, it timed out (`subprocess.TimeoutExpired`), or it ran fine but simply found no matching peer. A host can be reachable over plain LAN ssh with no Tailscale involvement, so `false` would misrepresent "not probed" as "probed and down."

The `tailscale` subprocess itself degrades to `[]` peers on every expected failure mode (missing binary, non-zero exit, unparseable JSON, timeout) rather than raising — including non-UTF-8 output on stdout/stderr: `_default_status_runner` does not ask `subprocess.run` to decode (`text=True` decodes eagerly and raises `UnicodeDecodeError` — a `ValueError` — before `_probe_peers`'s own except tuple gets a chance); `_probe_peers` keeps raw bytes and decodes with `errors="replace"` itself, and that except tuple also catches `ValueError` as a belt-and-braces.

A `_build()` that raises anyway (not a probe failure — something else going wrong) is handled INSIDE `get()`'s lock rather than propagating to every waiter, so a raising build re-runs at most once per queued waiter rather than once per waiter independently. It caches a probe-free `degraded()` snapshot — every registry host still listed, each at `online: null`, reusing `_build`'s own host-assembly logic with an empty peer list rather than a separate code path — under a much shorter `_HOST_CATALOG_NEGATIVE_TTL_SECONDS` (5s), so a failing probe costs one attempt per negative-TTL window instead of one per waiter. The route (`agent_assignment.py`) wraps `catalog.get()` in `asyncio.wait_for(..., timeout=_HOSTS_ROUTE_TIMEOUT_SECONDS)` (1.8s) as a real ceiling on top of the tailscale probe's own 1.5s timeout — lock queueing and event-loop scheduling can push a call past the probe's own bound — and falls back to the same `degraded()` snapshot on a timeout or any other exception, logged at `error` with a traceback, always preserving the full registry rather than just the API host.

Concurrent cache misses coalesce behind an `asyncio.Lock` (double-checked after acquiring it) — N callers arriving at once share one probe rather than each spawning their own `tailscale status`, and the cache timestamp is stamped only after the build completes, so a slow build never silently shortens the effective TTL.

**Client-side caching** (`web/agents/assignment.js`'s `loadHostCatalog`) is a *short-TTL* cache (`_HOSTS_CLIENT_TTL_MS`, 30s — matched to the server's own `_HOST_CATALOG_TTL_SECONDS`), not the once-per-page-load cache the model catalog uses: reachability markers go stale in seconds, so a drawer opened well after the client TTL re-fetches rather than showing page-load-time data for the rest of a long-lived session. A fetch failure is never cached — the caller for that one call gets a `null` catalog back (the select falls back to its synchronous seed: "this machine" plus the card's saved host, if any, flagged unknown, alongside a disabled "hosts unavailable — reopen to retry" option so a failed load doesn't read as "nothing is registered"), but the next drawer open retries rather than being stuck with an empty picker for the rest of the session. After a SECOND consecutive failure, a 10-second cooldown (`_HOSTS_FAILURE_COOLDOWN_MS`) suppresses further fetches so a permanently-dead endpoint costs one request per cooldown window rather than one per drawer open; a single transient blip still retries on the very next drawer open. A slow fetch that fails after a newer, faster fetch already cached a success does not clobber that newer cache entry — the `.catch` only clears `_hostsCache` if it's still the entry that fetch itself installed.

A host change the server rejects — and, alongside it, whatever effort/model change rode in the same save — reverts the select(s) to their last-known-good value before the error is surfaced, the same way board.js's other drawer controls already revert on a failed save; without this, a rejected host would silently ride along on the next unrelated save with no toast at all.

---

## System prompts

All four model-facing prompts are structured per [Anthropic's Claude 4.6/4.7 prompt-engineering best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) — XML section tags, positive framing, explicit final-summary requirement after tool use.

| Prompt | Location | Audience |
|---|---|---|
| Cloud agent preset | Anthropic console (versioned; current v3) | Claude Sonnet 4.6 on Managed Agents |
| Local Gemma `_system_prompt` | `local_executor.py` (`_SYSTEM_PROMPT_STATIC` + per-task `<this_task>` trailer) | Gemma 4 26B |
| Cloud per-task user message | `managed_executor.py` (`_user_message_for`) | Initial user turn for each managed session |
| Haiku preflight prompt | `preflight.py` (`_PREFLIGHT_INSTRUCTIONS`) | Claude Haiku classifier |

Every executor's task-opening prompt includes the shared [`PROJECT_TASK_GUIDANCE`](../../../api/services/agent_worker/delegation.py#L28) fragment; Hermes adds it only when starting a new conversation. When a Managed preset class narrows its tools, the [cross-cutting set](../../../api/services/agent_worker/tool_filter.py#L44) retains project inspection, lifecycle, resume, and handoff tools; fullstack and unknown classes instead leave the preset unfiltered.

Cache strategy: the local executor's static portion is a module-level constant so prompt caches don't invalidate between sessions; only the small trailing `<this_task>` block (expected_output + soft budget) varies.

The cloud preset YAML is mirrored verbatim in [`guides/agent-worker-setup.md`](../../guides/agent-worker-setup.md) for fresh-clone operators.

---

## Project task context and coordination

Task hierarchy is a vault relationship, not session lineage. A child task
stores `fields.parent_id`; its worker session still uses the child's own task
ID, assignment, review state and lifecycle. The HTTP task payload carries
derived `parent_id`, `parent_title`, `is_project`, `child_count`, hierarchy
validity and compact project progress. Candidate filtering and the atomic claim
boundary both reject project parents, invalid observed hierarchy,
execution-paused tasks, and children whose parent cancellation is pending.

Before dispatching a child, the worker fetches current hierarchy and adds one
bounded `project_context` object to the execution input. It contains the
parent's stable ID/title, objective and acceptance notes, plus a compact sibling
status summary. It does not copy arbitrary sibling notes or unrelated personal
tasks. This keeps the child independently executable while preserving enough
parent intent to judge its own output.

Project planning uses `ProjectTaskService.plan_and_delegate`, which creates an
operator-origin session with a synthetic task ID derived from the project and
stable operation ID. The service stages that session as non-dispatchable,
persists the parent-to-session/request link, then makes it claimable. Retries
recover the same session. For CLI, local, and remote owners, the canonical
execution request is derived from the project owner's route plus its model,
effort and host fields. Its explicit working directory wins; otherwise
`resolve_existing_location_affinity` scans only existing local project,
LifeOS, vault, and home directories for an executor that supports
`working_dir` on the API host. It never consults the GitHub catalog or starts
a subprocess while the project-operation lock is held. Managed Agents
ownership uses the explicit consent alias's model and retains configured
effort and host, but not the configured model, working directory, or affinity
fallback, as described in the
[task-management product spec](../product/task-management.md#projects-and-subtasks).
Planning does not infer a new cloud authorization. The write that first links
the owner session — only when the project has no prior coordinator request
and no prior handoff activation, so a later re-Plan of an already-owned
project never touches it — also records `fields.project_integration_branch`
(`task_projects._integration_branch_name`, `git_worktree.derive_branch_name`
seeded on `title` plus a short hash of the project's own task id, not the id
itself) when the field isn't already set — the project's deterministic
integration branch, following the repository's own `<type>/<slug>-<suffix>`
branch-naming convention, but guaranteed to differ from the work branch
`ensure_worktree` would derive for that same task's own worktree (relevant
for a handed-off project, which keeps the source task's id). `finalize_handoff`
records the same field the same way at handoff activation — inherently a
first-owner creation, so it keeps only the not-already-set check. An
operator-owned project never reaches either write path, so it never gets one;
an operator can clear the field directly to opt a project back out. The
coordination prompt
contains the project objective/acceptance notes and at most 50 current child
IDs, assignments and states. It instructs child creation to use a stable
`operation_key` derived from the project ID, planning operation ID, and child
role; the shared task create path recovers the existing task for a repeated
key. Transcript events supply coordinator state and
result; its terminal state never projects completion or failure onto the
parent task.

Project cancellation persists its operation fence before any teardown, then
uses the existing session cancellation and CLI teardown paths outside the task
write lock. Done children are preserved, unfinished human/unassigned children
are cancelled through guarded task writes, and review-pending output is tagged
abandoned without acceptance. Any failed or unverifiable stop keeps the parent
fenced as cancellation-pending. A retry with the same operation ID recomputes
current state and applies only outstanding work; a different operation is
rejected while the fence remains.

`lifeos_agent_project_handoff` is the only exception to the ordinary
live-parent attachment guard. It accepts 1–20 complete child requests from an
ordinary top-level task's exact current session, attempt, and turn; the MCP
transport attests those identities, so the model cannot select a different
source task. The service records the normalized request in the source
transcript, writes small operation/identity/hash pointers on the parent,
creates deterministic keyed children and a blocked coordinator, and fences all
of them from normal claim or Open. The child assignment and optional execution
request are independent: omitted values are not copied from the source, and
metered targets must already be in that source turn's permitted provider scope.

The executor observes a successful handoff as a terminal action for its turn.
Only after the executor return boundary carries the route's required stop
evidence and is recorded as a matching quiescence event can the worker
terminalize that source session without projecting normal task completion,
clear the handoff fence, and release the coordinator. Hermes requires its
positive upstream `done` event; a client disconnect or deadline is not upstream
stop proof. A missing proof, failed finalization, or cancellation race keeps
work pending and non-runnable. Reconciliation runs before ordinary lifecycle
drift repair; there is no atomic transaction spanning Markdown, SQLite, and an
external executor.

`Worker._reconcile_project_notices()` runs each tick immediately after
`_reconcile_project_handoffs()`, sharing that same tick's `/api/tasks` fetch
(`_fetch_tasks_for_project_reconciliation`) rather than issuing a second one.
It runs before the daily-spend-cap gate — it only sends Telegram notices,
never starts work — and checks two triggers per project task: a
`fields.project_last_handoff_operation_id` with no recorded `"handoff"`
notice (activation, not staging, since a staged handoff never sets that
field), and more than five children with `fields.project_child_origin ==
"agent"` (any status) with no recorded `"agent_children_gt5"` notice. The
fan-out count skips a parent that still carries
`fields.project_handoff_operation_id` — a staged, not-yet-activated
handoff already stamps its children agent-origin, so counting them early
would send a fan-out notice before the activation notice and duplicate it;
deferring until the pending field clears at activation is what lets a
6+-child handoff report both triggers as one combined message. It also
skips a parent whose only agent-origin children came from an aborted
handoff (`fields.project_last_aborted_handoff_operation_id` set and
`project_last_handoff_operation_id` never set) — a cancelled staged
handoff never announces a project that never existed. Durable dedupe
lives in `SessionStore`'s `project_notices` table (`PRIMARY
KEY(project_id, kind)`); a row is written only after `Worker._notify`
reports a successful send, so a failed send is retried on a later tick
instead of being marked done, and a project that trips both triggers on
the same tick gets one combined message with both rows written for that
one send.

`Worker._reconcile_project_owners()` runs each tick after the daily-spend-cap
gate and before `_dispatch_spawned_sessions()`, reusing the same shared
`/api/tasks` fetch. For every non-terminal, non-cancellation/handoff-pending
project whose parent carries an agent assignee (or Managed consent tag) and
an owner session in `fields.project_coordinator_session_id`, it classifies
each child's state (`owner_state`: `awaiting_review`, `blocked`, `failed` —
`agent-failed`/`agent-budget-exceeded`, checked ahead of the plain
`cancelled`/`done` statuses those two tags leave the task at — `done`,
`cancelled`, or `active`) and diffs it against `SessionStore`'s
`project_owner_state` table (`acked_states_json`, keyed on `project_id`,
created on first observation with the current snapshot as its own baseline
so that creation is never itself an event). A non-`active` state that
differs from `acked_states` is an event. A paused project (see the product
spec's pause/resume description) is skipped entirely — the diff against
`acked_states` still grows underneath it, so resuming surfaces whatever
piled up. A live owner session (anything but
`COMPLETED`/`FAILED`/`BUDGET_EXCEEDED`) is left alone; this, together with
`begin_new_execution`'s terminal-only compare-and-set, is what makes two
owner turns impossible at once. Once there is a non-empty diff, its age is
tracked by a single `first_unseen_at` anchor (set once when the diff goes
from empty to non-empty, cleared once a wake is sent) — a wake fires once
that anchor is at least `_OWNER_WAKE_QUIET_SECONDS` (30) old, so several
children changing state in a burst coalesce into one wake rather than one
per child. `Worker._continue_session` is the shared native-continuation
primitive `_resume_as_followup` and the owner-wake reconciler both call,
one route branch per engine:

- **claude_code/codex**: the wake message is enqueued onto the owner's
  still-terminal session *before* `begin_new_execution` reopens it with the
  owner's prior `execution_request` (required, since `begin_new_execution`
  clears `execution_spec_json`, and a CLI resume's working directory is
  resolved from that request). Enqueuing first means a crash between the two
  redelivers on the next tick instead of losing the wake; the reopened
  session is left `CLAIMED` for `_dispatch_spawned_sessions` to pick up the
  same tick or the next. When the owner has no persisted CLI session id (it
  never launched a subprocess, or one launched but never confirmed), the
  same dispatcher's own fresh-vs-resume branch already treats a missing id
  as a fresh spawn — the reconciler exploits that by enqueuing the bounded
  fallback briefing below instead of the terse wake diff, so the route never
  changes and no separate fallback mechanism is needed for this pair.
- **local/remote**: the reverse order — `begin_new_execution` reopens the
  owner first (these routes key their next turn off the session's live
  attempt/turn), then the wake message is appended as the next conversation
  turn and run inline via `_execute_start`, exactly like an ordinary
  follow-up. The conversation always lives in `session_store`, so there is
  no missing-handle case for these two routes.
- **hermes**: reopened first, then submitted off-tick (a Hermes turn is a
  blocking HTTP round trip) with `execute(..., prompt=...)`. The owner's
  `conversation_id` decides native-vs-fresh inside the executor itself, so
  the reconciler just picks which message to send: the terse wake diff when
  a conversation is on record, the bounded fallback briefing when it isn't.
- **claude (Managed Agents)**: reopened first, then `post_user_message`
  posts the wake message to the owner's `managed_agent_session_id` — with a
  freshly computed per-turn identity/proof clause, since `lifeos_agent_*`
  attestation is bound to one exact turn and the turn that just ended has no
  bearing on the one this continuation mints (`SessionStore.
  begin_executor_turn`, since a CAS reopen alone leaves `turn_id` unset).
  When there is no recorded remote session id, or posting to it fails (most
  commonly because Anthropic already garbage-collected it), the reconciler
  starts a brand-new Managed session on the same route instead
  (`ManagedExecutor.start`), seeded with the bounded fallback briefing as
  its task notes. Only reached at all while the project still carries a
  `#cloud-haiku`/`#cloud-sonnet` consent tag — `_project_is_agent_owned` is
  re-checked against the tick's live tags before any owner is looked up.

The fallback briefing (`Worker._owner_fallback_briefing`) is bounded like the
wake message itself: the project's own objective and notes, its current
child table, and the owner's own last recorded card outcome — never a
transcript excerpt, since a fresh start has no native thread to read one
from. Reopening a project's owner reaches every route the same way whether
the project was planned or came from an activated `lifeos_agent_project_handoff`
— the wake reconciler has no separate path for either origin. Requesting
Plan and delegate again for a project whose owner is already terminal
records a wake request (`SessionStore.request_project_owner_wake`, idempotent
per operation ID) instead of creating a second session; the reconciler folds
that into the same event diff it already tracks, and the response adds
`wake_requested: true`.

On the reconciler's next pass, a terminal owner whose recorded
`wake_attempt_id` matches its current attempt reconciles that wake's outcome
first: `COMPLETED` moves
`acked_states` to what was delivered and clears the failure count; `FAILED`
leaves `acked_states` untouched (so the same events redeliver) and
increments a `consecutive_failures` counter, auto-pausing the project
(`POST /project/pause {reason: "owner_failed"}`, one Telegram notice) once
that counter reaches 2; `BUDGET_EXCEEDED` pauses immediately with reason
`owner_budget`, without needing a second occurrence. `resume_pending()` on
worker startup leaves an operator-origin `CLAIMED` session with an
undelivered pending message alone rather than failing it — a wake that was
compare-and-set open but not yet dispatched before a crash/restart is picked
up normally by `_dispatch_spawned_sessions` on a later tick. Resuming a
paused project resets `consecutive_failures`
(`ProjectTaskService.resume_project`) — the one place a pause transition is
unambiguous, rather than the reconciler inferring one happened on every
later tick.

## Inter-agent coordination

Local agents can spawn child sessions and coordinate via the `lifeos_agent_*` tool family:

| Tool | Purpose |
|---|---|
| `lifeos_agent_project_handoff` | Stage 1–20 uniquely keyed durable children from the exact current ordinary-task turn. It is terminal for that turn; staged work remains fenced until the worker proves quiescence and finalizes it. `hermes` is not a valid child `assignee` or `execution.executor` — the schema omits it and the handler rejects it with `hermes_delegation_forbidden`; a metered `execution.executor` (`claude`/`remote`) is refused outside the source turn's own already-authorized scope (`inter_agent.metered_target_out_of_scope`). Staged children are stamped `project_child_origin=agent` plus the source session as `project_child_creator_session`. |
| `lifeos_agent_spawn` | Create a child on `local`, `remote`, `claude`, `hermes`, `claude_code`, or `codex`. Legacy `model=<executor>` and Claude-Code `tier` remain valid; the strict `execution` object carries canonical route/model/effort/location/budget choices. Omitting the route inherits an active bounded override or the caller route. |
| `lifeos_agent_send` | Post a message to a child session's queue. Also a lifecycle transition: a direct parent sending to its own COMPLETED `claude_code`/`codex` child with a persisted CLI session id **reopens** it — the message is enqueued as the child's next turn *before* the status flips back to `claimed` (so a dispatch tick can never claim an empty resume prompt), and the spawned-session dispatcher resumes the CLI session via `-r` with full prior context. All other terminal sends still reject. |
| `lifeos_agent_check` | Poll a child's current state. |
| `lifeos_agent_yield_until` | Pause the caller until specific children reach terminal state — preferred over polling (no idle billing). |
| `lifeos_agent_kill` | Terminate a child early. |
| `lifeos_agent_transcript_read` | Read another session's transcript. |
| `lifeos_agent_sessions_list` | List active + recent sessions. |
| `lifeos_agent_user_ask` | Pause and ask the operator a clarifying question via Telegram (reply-threaded). |
| `lifeos_agent_execution_override` | Set or clear a temporary override for future resolution in the caller's session, or (root callers only) its lineage. Already-resolved snapshots do not change. |

Security: lineage checks ensure a session can only message / kill / yield-on its own descendants (rooted at `root_session_id`).

Lineage budgets: every session tracks `root_session_id` + `spawn_depth`. A `lineage_max_dollars` breach at a descendant yields and asks (see [Breach → yield → ask](#breach--yield--ask)) rather than cascade-killing the lineage — the one dimension a spawned child is allowed to ask about, since the cap it names is the lineage root's own, not anything the child owns. Limits configurable via `LIFEOS_AGENT_MAX_SPAWN_DEPTH`, `LIFEOS_AGENT_MAX_DESCENDANTS_PER_ROOT`, `LIFEOS_AGENT_MAX_CONCURRENT_LOCAL`, `LIFEOS_AGENT_MAX_CONCURRENT_MANAGED`.

### Operator root-spawn

`lifeos_agent_spawn` is same-lineage only (requires a parent session). Operators start agents on demand — with no backing `#agent` vault task — via `create_operator_session()` (`api/services/agent_worker/operator_spawn.py`), reachable from Telegram (`/agent [local|claude] <task>`) and web chat (the same `/agent` slash command).

- **Routing** follows override-then-preflight: an explicit `local`/`claude` keyword wins; otherwise `run_preflight()` decides. On `ROUTE_ASK` the session parks at `blocked` with `routing='ask'` and the caller sends the engine clarification (the worker resolves it on reply).
- **The API needs consent.** `routing=claude` is the only per-token-billed-to-Anthropic route, so preflight may reach it only when the operator asked: a `#cloud-haiku` / `#cloud-sonnet` tag, or a title naming an engine or model (the classifier's `routing_explicit`, corroborated against the title so a hallucinated flag can't dispatch). A cloud route the classifier *inferred* — rule 4's capability cues — is downgraded to `ROUTE_ASK` and confirmed. (Bare `#cloud` is a separate, similarly-gated consent — it dispatches straight to `ROUTE_REMOTE`, the configured remote provider, never Anthropic; a title merely containing "cloud" does not corroborate an inferred `claude` route either, since it isn't in `_TITLE_NAMES_A_CLOUD_ENGINE`.) The confirmation offers `claude code` / `codex` / `local` / `cloud` (remote provider) / `anthropic` or a Claude model name, and a bare "claude" in the reply resolves to the **CLI**, not the API: with both in play, the subscription reading is the one where a misparse costs nothing.
- **Provenance** is marked with the additive `sessions.origin = 'operator'` column. The worker's `_dispatch_spawned_sessions` skip is relaxed to claim parentless sessions when `origin='operator'`, so they dispatch alongside spawned children without colliding with the top-level engine-assignee claim path (which uses NULL origin). The prompt is enqueued as a pending message and drained as the task description on dispatch. The same skip is separately relaxed for a parentless, non-operator `claude_code`/`codex` session carrying an undelivered `pending_messages` row — see "Reopened top-level CLI sessions" below.
- Operator sessions are root sessions (`parent_session_id=None`), so their terminal notifications surface to the operator and register a replyable follow-up. Because they have no backing vault task, `_handle_outcome` and `_resume_as_followup` skip the vault mutations (`_complete_task` / `_swap_tag` / `_set_task_status`) for `origin='operator'` — gated on `has_vault_task` — while still sending the notification + follow-up. The prompt is enqueued *before* the session row is created so the worker can never observe a CLAIMED operator session whose prompt hasn't landed. Default budget comes from the `agent_default_*` settings; local concurrency cap of 1 means operator local spawns queue behind running ones.

### Off-tick CLI dispatch

`claude_code` / `codex` sessions are long-running subprocesses — up to the session's budget wall (14,400s by default) — so their dispatch always runs on a bounded `ThreadPoolExecutor` (`_cli_pool`, sized `2 × agent_max_concurrent_managed`) via `_submit_cli_dispatch`, never inline on the tick thread. This applies to both callers: `_dispatch_spawned_sessions` (spawned children and operator root-spawns) and `_dispatch`'s `ROUTE_CLAUDE_CODE`/`ROUTE_CODEX` branch (top-level engine-assigned tasks) — a single delegated child or top-level CLI task must not park the poll loop and starve new engine-assignee claims, sleeping-session wakes, managed polling, or clarification processing/timeouts. Preflight and the fast blocked/failed/sanity short-circuits still run inline on the tick thread; only the `execute()`/`resume()` subprocess call and everything downstream of its outcome (vault tag swap, Telegram notify) move to the pool, since `_dispatch_claude_code_session`/`_dispatch_codex_session` own outcome handling themselves rather than going through `_handle_outcome`. An `_cli_inflight` set (lock-guarded) prevents a re-scan from re-submitting the same session in the window before its executor flips the row `CLAIMED→RUNNING`; for CLI routes the guard is checked *before* draining pending messages so a skipped re-scan can't discard them. Per-routing concurrency stays bounded at `lifeos_agent_spawn` time (`count_active_by_routing`), independent of dispatch timing. The `local` route stays inline (in-process, GPU-bound, cap 1). `stop()` calls `shutdown(wait=False, cancel_futures=True)`; sessions still running are reconciled by `resume_pending()` on restart. Tests inject a `_SynchronousPool` for deterministic dispatch.

### Git worktree provisioning

A fresh `claude_code`/`codex` dispatch whose resolved working directory sits inside a git repository never runs there directly — that directory can be the operator's own primary checkout, the exact working tree the production API server runs from. `git_worktree.py` (`ensure_worktree(working_dir, task_id, title, host=...)`) gives the session an isolated worktree on a fresh branch instead, called from `_dispatch()` right before `_resolve_session_execution` freezes the working directory onto the session's execution spec — after `(task.get("fields") or {}).get("working_dir") or resolve_working_directory(title)` picks a candidate directory the usual way, but before that candidate is handed to any executor. This runs for every `ROUTE_CLAUDE_CODE`/`ROUTE_CODEX` dispatch, including one pinned to a remote host (`assignment.host`) — see below. A candidate directory that isn't a git repository at all is unaffected either way (`ensure_worktree` returns it verbatim, `is_git=False`).

Both the worktree's path and its branch name are deterministic functions of the repository toplevel and the task id (`worktree_dir_for`: a sibling directory named `<repo>-wt-agent-<task_id>`; `derive_branch_name`: `<type>/<slug>-<task_id suffix>`, `type` always one of `ALLOWED_BRANCH_TYPES` — `feat`/`fix`/`docs`/`test`/`refactor`/`chore`, the project's own branch-naming convention — read off a conventional prefix in the title (`fix: ...`) when present, else a leading keyword (`"Fix the printer"` → `fix`), else `feat`). Fresh provisioning runs `git fetch origin`, detects the default branch (`origin/HEAD` when set, else the first of `main`/`master` that exists), and creates the worktree with `git worktree add -b <branch> <dir> origin/<base>`, where `<base>` is the repository's detected default branch unless `ensure_worktree` was given a `base_branch` — `_dispatch()` passes `_project_location_context["integration_branch"]` (populated from the parent's `fields.project_integration_branch` by `_with_project_context`) for a coding child of a project that has one recorded. When a `base_branch` is given and `origin/<base_branch>` doesn't exist yet, it's created first — `git push origin origin/<default>:refs/heads/<base_branch>`, then a re-fetch — and the push itself is allowed to fail: only the ref still being missing after the re-fetch is a real error, so two children racing to create the same integration branch both succeed regardless of which one's push actually lands it. `base_branch` (or `None` for a task with nothing recorded) is written into the ownership marker below. Reuse asks git's own registry (`git worktree list --porcelain`) for a worktree already at the deterministic path, both before attempting `add` and again if `add` itself fails — two dispatch ticks racing the same task can both miss the first check and then have one `add` fail on the ref the other just created; the second probe catches that and reuses rather than failing a task that already has a valid worktree, keeping whichever base branch that worktree was originally provisioned against. Any other git failure raises `WorktreeError`, which `_dispatch()` treats as failing the task closed (`_mark_failed`) — never a fallback to running inside the caller-supplied directory.

Each provisioned worktree carries a JSON ownership marker in its linked git directory, including the `base_branch` it was provisioned against. Cleanup requires both the `-wt-agent-<task-id>` path shape and that marker; either signal alone is insufficient. This prevents the primary checkout and operator-created worktrees from becoming cleanup targets.

Every command provisioning runs — `test`/`mkdir`/`git fetch`/`git worktree add`/the registry probe — goes through an injectable `Runner` (`resolve_runner_for_host(host)`): local/no host resolves to the plain local subprocess; a registered remote host (the same `settings.agent_hosts` registry and `remote_spawn.resolve_host_target` resolution the executors use for a remote CLI spawn) resolves to an ssh-wrapped runner (`make_ssh_runner`, `BatchMode=yes`, a bounded connect timeout, `cwd` folded into a `cd ... &&` prefix on the remote command since ssh has no cwd of its own) — so a remote-host task is provisioned on the machine that will actually run the session, never this worker's own filesystem standing in for it. An unregistered host raises `WorktreeError` before any command runs, same fail-closed contract as a git failure.

A resumed session (an operator follow-up via `_resume_as_followup`, a Telegram anchor reply, `codex resume`, `claude --resume`) never re-enters this seam at all: the provisioned path is already the `working_dir` on the session's frozen `execution_spec_json` (`ExecutionSpec.working_dir`, the same field every other route already used for this), and every resume path (`_dispatch_claude_code_session`/`_dispatch_codex_session`'s `is_resume` branch, `_execute_resume`) threads that persisted value straight through to `.resume(session, message, working_dir=...)`. An operator-set `[working_dir:: ...]` field already flows through this exact column independent of resume-vs-fresh dispatch, requiring no schema addition — reuse falls out of that for free. Claude Code's own `--resume <id>` session lookup is additionally keyed on cwd (`~/.claude/projects/<cwd-slug>`), so resuming in a different directory than the original dispatch would fail to find the session regardless — reusing the persisted worktree path is required correctness, not just tidiness.

The git-discipline instructions told to a fresh CLI session (its own branch, isolated from the primary checkout, expected to commit as it goes, its PR body — built from its final message — is public so it must carry no personal data) are derived at prompt-assembly time from the working directory itself — `git_worktree.describe_worktree(working_dir)` detects whether it's a linked worktree (its own `.git` is a file, never a directory the way the primary checkout's is) and, if so, its current branch — rather than threaded through the dispatch JSON payload. `ClaudeCodeExecutor` folds this into the `GIT DISCIPLINE`-equivalent bullet inside `_SYSTEM_PROMPT`'s `ENVIRONMENT` section (via `--append-system-prompt`); `CodexExecutor` prepends it as its own `=== GIT DISCIPLINE ===` header, mirroring the existing delegation-header pattern. Both only compute it on the opening turn (`resume_session_id is None` for Claude Code; `execute()` only, never `resume()`, for Codex) — a resumed CLI thread already carries it from the first turn. A task outside any worktree (a vault/home-directory session, an operator `/claude` spawn with no worktree) gets an empty string here and its prompt is unchanged.

The same shared text also carries the `[CLARIFY] <question>` convention for both engines. Claude Code has its own live `[CLARIFY]` handling (`_CLARIFY_OPERATOR`), pausing mid-stream. Codex has no mid-turn pause of its own — a turn always runs to completion — so `CodexExecutor` detects the marker post-hoc instead, reusing Claude Code's own `_CLARIFY_RE` against the final agent message: when the session isn't a spawned child, a match persists `STATUS_BLOCKED`/`REASON_AWAITING_CLARIFICATION` with the extracted question as `final_text` before the completion write ever runs, so it never reaches `STATUS_COMPLETED`. A spawned child has no operator to pause for: its `[CLARIFY]` folds into `[needs clarification] <question>` as ordinary completed `final_text` instead, parity with Claude Code's own `_CLARIFY_CHILD` convention.

### Worktree completion — push, pull request, safety net, Human queue

`git_worktree.finalize_worktree_session(working_dir, open_pr, pr_title, pr_body, base_branch, host, gh_runner)` runs the worker's own git discipline at a CLI session's completion or question-pause, called from `Worker._finalize_worktree_for_session` (resolves `working_dir` off the session's own `execution_spec.working_dir` and `host` off `session.host` — the same board-assigned host provisioning used; no-op `FinalizeResult(applicable=False)` for a spawned child, whose git discipline is its parent's) at points in both `_dispatch_claude_code_session` and `_dispatch_codex_session`: the `STATUS_BLOCKED`/`REASON_AWAITING_CLARIFICATION` branch (`open_pr=False`, both engines), the `STATUS_COMPLETED` branch (`open_pr=True`), and the trailing `FAILED`/`BUDGET_EXCEEDED` branch (`open_pr=False`, unconditionally — including an operator kill, which still gets safety-netted even though it never gets a Telegram notice of its own). `_with_git_status_note` folds the result into whatever notice the branch was already sending; it's a pure no-op when `FinalizeResult.applicable` is False, and a push/PR failure, an unresolvable host, or a branch with nothing to push is always reported in plain text (`⚠️ git: ...` / "nothing to open a pull request for") rather than silently treated as success — the card still reaches its normal lane (Review for a genuine completion) regardless of whether the git half succeeded.

A question-pause additionally moves the backing board task into the Human queue lane the same way `_mark_blocked` does for the preflight/ambiguity block path: `_reconcile_vault_terminal(session, STATUS_BLOCKED)` swaps `agent-running` → `agent-blocked` and sets the vault checkbox status to `blocked`. `_resume_as_followup`'s resumable-tag set and its swap-back loop both include `BLOCKED_TAG` now, so an operator's threaded reply to a question-pause card swaps it back to `agent-running` (and revalidates against it) the same way answering a completed/failed/budget-exceeded card already did — the worktree/branch stay untouched throughout, since the working directory persists on `execution_spec.working_dir` independent of the tag.

Finalization runs entirely through `resolve_runner_for_host(host)` — the same runner resolution provisioning uses — so a session pinned to a registered remote host is finalized on that host, not silently skipped in favor of checking this worker's own filesystem: `is_linked_worktree`/`current_branch`/`repo_toplevel` all accept the resolved runner, and every git/`gh` call (`status`, `add`, `commit`, `push`, `rev-list`, `log`, `gh pr view`/`create`) is threaded through it too. `FinalizeResult.applicable` is False only when there's no `working_dir` at all or it genuinely isn't a linked worktree; a `working_dir` that names a real worktree pinned to a host that fails to resolve gets `applicable=True` with `error` set instead — an unresolvable host is a finalization failure to report, never a silent "nothing to finalize." `gh_runner` overrides the resolved runner for `gh` calls specifically (a test seam); production leaves it unset so `gh` runs through the same runner as git.

`finalize_worktree_session` always runs the same first two steps once a linked worktree is confirmed: commit anything the session itself left uncommitted (`git add -A` — honors `.gitignore` — then `git commit -m "chore: worker safety-net commit for uncommitted session changes"`, only when `git status --porcelain` reports something), then `git push -u origin <branch>` — no `--no-verify`, ever. A failed or timed-out `git status --porcelain` itself raises rather than being read as "the tree is clean" — `_has_uncommitted_changes` surfaces that as `FinalizeResult.error` instead of silently pushing whatever HEAD already had and dropping real uncommitted work. Every subprocess this module runs, provisioning and finalization alike, goes through `_run()`, which catches `TimeoutExpired`/`OSError` itself and turns either into an ordinary non-zero `CompletedProcess` rather than letting it escape — so a hung hook or a dead remote can never crash the dispatch path, only surface as an error like any other git failure. Plain status/rev-parse-shaped calls use `DEFAULT_TIMEOUT` (60s); commit/add/push — the operations a pre-commit/pre-push hook can run against — use the more generous `COMMIT_PUSH_TIMEOUT` (300s).

When `open_pr` is True and the push succeeded: `git rev-list --count origin/<base>..<branch>` decides whether there's anything to open a PR for (`nothing_to_push=True`, no `gh` call, when the branch carries zero commits beyond its own base — never an empty PR); otherwise `gh pr view <branch> --json url` checks for one that already exists for the branch (reused, no `gh pr create` call) before opening a new one. The pull request body (`_build_pr_body`) leads with the card title, then the session's own completion summary, then the branch's commit list (`git log --oneline base..branch`) — the assembled body is passed through `_scrub_secrets` (a fixed set of credential *shapes*: Telegram bot tokens, `sk-`/`ghp_`/`github_pat_`/AWS `AKIA...` key prefixes, `Bearer <token>`, and any other 32+-char hex/base64-ish run — not a personal-data scanner) and then `_bounded()` (`MAX_PR_BODY_CHARS`, 4000, with a `…(truncated — N chars total)` note when cut). The body is delivered to `gh pr create --body-file -` over stdin rather than a temp file or argv — works identically whether the runner is local or ssh-wrapped (ssh forwards local stdin to the remote command), and leaves nothing to clean up on either side. The base for both the pull request and the commits-ahead check is resolved in order: an explicit `base_branch` argument, then the `base_branch` recorded in the worktree's own ownership marker (the same one `ensure_worktree` wrote at provisioning, e.g. a project's integration branch), then the same default-branch detection `ensure_worktree` uses when neither is present — a worktree provisioned with no `base_branch` (an ordinary task, or a project with nothing recorded) carries `base_branch: null` in its marker and finalizes against the detected default exactly as before.

### Reopened top-level CLI sessions

A `#agent` task's `claude_code`/`codex` session is normally dispatched once by `_dispatch`'s fresh-claim path and never revisited by `_dispatch_spawned_sessions` — that method's skip guard drops any parentless, non-`origin='operator'` session, since a fresh claim already handles those inline. A session that has *already run once* and been reopened for a follow-up turn is different: every reopen path — a Telegram followup reply (`_resume_as_followup`), a Telegram status-anchor reply (`_handle_status_anchor_reply`), or the worker's own `code_reopened_for_pending_messages` mid-run reopen — can only enqueue a `pending_messages` row and flip the session back to `claimed`; none of them can invoke the executor inline the way a fresh top-level claim does. Left as originally written, that reopened session simply sat at `claimed` forever: nothing in the tick loop revisited a parentless, non-operator session a second time.

The skip guard admits exactly this shape: a parentless, non-`origin='operator'` session is still skipped unless its routing is `claude_code`/`codex` **and** `session_store.has_pending_messages(session_id)` is true. This is a safe discriminator because a fresh top-level claim never produces it — `_dispatch`'s CLI branch synthesizes its first-turn payload in memory and hands it straight to `_submit_cli_dispatch`, never through `pending_messages` — so this check can never grab a session the same tick's own top-level claim loop claimed moments earlier (that loop also runs *after* `_dispatch_spawned_sessions` within `tick()`, so the ordering alone would already prevent it). A session admitted this way resumes through the same `is_resume` branch (keyed on a persisted `claude_code_session_id`) that any other resume uses — `-r <id>` / `codex exec resume <id>` in the session's original working directory — with every queued `pending_messages` row folded onto that one resume turn.

For a `claude_code`/`codex` route where `session.claude_code_session_id` is already set (a resume, not a first turn), `_dispatch_spawned_sessions` *peeks* the pending rows (`session_store.peek_pending_messages`) rather than draining them — every other case (a fresh CLI dispatch, and every other route) still drains eagerly, attempt/turn-fenced, exactly as before. A resume dispatch runs off-tick and can fail before it ever confirms a subprocess started (a resolution failure, a fenced/rejected turn, a crash in worker glue code); marking the rows delivered before that confirmation exists would silently lose the note on any such failure — and a fresh dispatch has no persisted `claude_code_session_id` yet to key that confirmation on in the first place, so eager draining is both safe and necessary there (a row left undelivered forever would resurface and corrupt a later genuine resume prompt). `_confirm_resume_or_requeue()` is what supplies the resume-path confirmation: after `_execute_resume()` returns (or raises), it compares `_cli_subprocess_launch_count()` (the same launch-count helper the re-execute-averted guard uses, keyed on the `claude_code_spawn`/`codex_spawn` and `..._binary_not_found` transcript markers) before and after the call. Only once a launch is confirmed does it mark the peeked rows delivered (`session_store.mark_pending_delivered`) and send `"▶️ Resumed — continuing from your note."` (`_send_session_message`, anchored the same way a heartbeat is) — this is deliberately the *only* point that confirms a resume, since the deposit-time Telegram ack (both the followup-reply and status-anchor paths) only ever promises the note is queued. Both executors write their spawn marker unconditionally *before* `Popen` and compensate it with a `..._binary_not_found` marker on **any** `OSError` from the spawn call (a missing binary, a permission error, or any other OS-level spawn failure) — not just `FileNotFoundError` — so `_cli_subprocess_launch_count` nets out to zero for every spawn-time failure, not only the one exception type Python happens to raise for a missing executable. A resume that never launches leaves the rows undelivered; how that's reported to the operator depends on how `_execute_resume` failed — an exception unwinds past all the ordinary outcome handling, so `_confirm_resume_or_requeue` sends its own `"didn't start"` notice in that case (`report_failure=True`); an ordinary (non-raising) FAILED outcome instead falls through to the same shared FAILED-outcome handling every other failure uses, which already reports the executor's own accurate reason and reconciles the vault tag, so `_confirm_resume_or_requeue` stays quiet there (`report_failure=False`) rather than duplicating that notice. Spawned children never receive the "Resumed"/"didn't start" messages, consistent with every other operator-facing send these dispatches make.

Two residual gaps are accepted rather than closed: a worker/process crash landing strictly between a confirmed launch and `_confirm_resume_or_requeue`'s own call can still leave a note eligible for a later manual reopen to deliver a second time (at-least-once, not exactly-once, delivery); and a session left `FAILED` after a pre-launch failure has no automatic re-claim of its own — the note rides the next resume only once an operator triggers one (a fresh Telegram reply, or a re-tag).

### Stuck-claimed self-heal

`Worker._reconcile_stuck_claimed_sessions()` runs once per `tick()`, alongside `_reconcile_lifecycle_drift()` and equally ungated by the spend cap. It is the safety net for the reopen path above: under normal operation a parentless `claude_code`/`codex` session with an undelivered `pending_messages` row drains within a tick or two of being reopened, but a stale `_cli_inflight` guard surviving a worker restart, or a dispatch-time crash swallowed before the session could be marked failed, can still leave one stranded at `claimed`.

The sweep lists every `claimed` session and, for each parentless, non-`origin='operator'` `claude_code`/`codex` session with an undelivered pending message, compares `last_activity_at` (bumped by every `update_status` write, including the reopen-to-`claimed` write itself) against `settings.agent_stuck_session_timeout_minutes` (default 15). A session still inside that window is left alone. For one that's aged past it, the sweep re-fetches the backing task and requires `RUNNING_TAG` still be present before alerting — an operator can cancel, retag, or reassign a card after a reopen queued the session's note, and dispatch is correctly skipped for it from then on; that's not an actionable stuck resume, so a candidate whose card lacks the running tag is retired silently (a `stuck_claimed_episode_retired` transcript event, no Telegram message) rather than reported. A candidate whose card is still actively claimed gets a one-time Telegram alert naming the task and session id — the sweep deliberately does **not** re-drive dispatch itself (that already happens every tick via `_dispatch_spawned_sessions`; retrying here risks a second concurrent attempt on a session whose executor may genuinely still be starting up). Both the alert and the silent retirement are idempotent per stuck *episode*: each is recorded in the session's transcript (`stuck_claimed_session_alerted` / `stuck_claimed_episode_retired`) keyed to the `last_activity_at` value at the time, so a session that later gets unstuck and reopened again (a fresh `last_activity_at`) starts a new episode eligible for its own alert or retirement.

### Session scratch and worktree cleanup

Every board-dispatched session has a deterministic private scratch directory under the system temporary directory. Scratch paths accept only the worker's internal `sess_<16 lowercase hex characters>` identifiers and must resolve as a direct child of the `lifeos-agent-worker` temporary container before creation or deletion. The directory is created with mode `0700`; subprocess routes receive it as `TMPDIR`, `TMP`, and `TEMP`. For an SSH-routed CLI, the remote launcher creates the directory on the selected host before starting the CLI and exports the same variables there. Local Bash tool calls receive the session scratch environment through a context-local binding, so concurrent sessions do not mutate the worker process environment.

Any terminal session transition removes the scratch tree recursively through the session's host runner. Remote cleanup uses the same registered SSH runner as provisioning and also removes the local staging directory. A clarification timeout removes scratch even though the blocked session row remains replyable. The periodic resource reconciler repeats idempotent removal for terminal rows, covering worker interruption between the status write and deletion.

The reconciler processes a bounded batch every five minutes, outside task dispatch. It checks completed worktrees with `gh pr view <branch> --json state` and caches the result for the polling interval. A merged pull request, an accepted card, or a cancelled card makes its worktree eligible for removal only when its session is terminal and the executor registry reports no operation in flight. A transient task-fetch failure skips that worktree for the cycle; only a confirmed 404 means its card is gone. Old terminal sessions without a non-terminal card are orphan candidates. For every repository referenced by the complete session history, `git worktree list --porcelain` also discovers registered worktrees whose own session row is absent; only entries with a valid matching ownership marker enter cleanup. Commands use the provisioning runner, so a host-assigned worktree is inspected and removed over SSH on that host.

Removal first changes the ownership marker state to `cleaning`. It then runs the completion safety net, including commit and push of any remaining content. Only a successful push permits `git worktree remove --force`, followed by `git worktree prune`; cleanup never deletes the remote branch. An interrupted attempt retains its `cleaning` marker and is safe to retry on the next pass.

### Earned completion / interrupted CLI sessions

A CLI subprocess exiting cleanly (`returncode == 0`) is **not** proof the agent actually finished its turn — it can hit `--max-turns`, get OOM-killed, or otherwise die mid-thought and still reach the executor's `STATUS_COMPLETED` fallback with a mid-sentence `final_text` and zero notifications sent. A non-zero exit is authoritative regardless of any parsed terminal stream event: both `claude_code_executor.py` and `codex_executor.py` gate their `STATUS_COMPLETED` branch on `if proc.returncode == 0:` alone, so a bad end-of-run can never reach the completed fallback just because a `result`/`turn.completed` event was seen before it. Marking that `#agent-completed` hides the interruption from the operator (field case: `sess_099c0b8ca254486f` — final text a 64-char instruction fragment to itself, `notifications_sent: 0`, no PR, unpushed WIP branch — tagged completed anyway).

Both `_dispatch_claude_code_session` and `_dispatch_codex_session` gate their `STATUS_COMPLETED` branch on `completion_signal.has_positive_completion_signal(final_text, notifications_sent)` before treating the outcome as real completion — a **root** session only; a spawned child (`parent_session_id` set) is exempt, same as the empty-result/no-side-effect-tool-use guard `_handle_outcome` applies to the local/managed routes (that guard lives in a different dispatch path — the CLI routes bypass `_handle_outcome` entirely — so this is a parallel, composing check, not a replacement for it). A cheap, deterministic — not LLM — check is earned by any one of:

- at least one `[NOTIFY]` was sent during the run (`ExecutorOutcome.notifications_sent`; Codex has no notify convention and always reports 0, so it falls through to the next two checks);
- the final text references a PR/issue — a `github.com/…/pull|issues/…` URL is the strong signal; a bare issue/PR number alone only counts alongside merge/PR-ish phrasing nearby (`PR`, `merged`, `opened`, `closes`, `fixes`, `resolves`), to avoid mistaking a passing issue-number mention for "I opened it";
- the final text reads like a finished summary rather than an instruction fragment: non-empty, above a small length floor, and not trailing off mid-clause (ending in `:`/`,`/`;`/a dash, or on a dangling connective word like "the"/"and"/"to").

After a Claude Code `[NOTIFY]` has been delivered, later assistant narrative replaces `final_text` only when it passes that same finished-summary check. A trailing work note or unfinished aside therefore cannot displace the coherent result that already reached the operator; a genuine later summary remains authoritative.

For a non-zero local CLI exit, the outcome reason names the task and uses the last non-empty stderr line. When stderr is empty, the executor uses the most recent structured stream error (`result` error subtype for Claude Code; `error` or `turn.failed` for Codex), or states that no error output was captured. Remote SSH failures retain their host-specific stderr reason.

Codex activity follows the JSONL vocabulary emitted by the installed CLI: completed `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, and compatibility command/tool item types count as tool activity, while `turn.completed.usage` supplies input, cached-input, output, and reasoning token totals and the model-priced cost estimate.

Failing all three routes the outcome to `Worker._handle_cli_interrupted`, which parks it rather than either completing or bare-failing it:

- **Resumable** (a `claude_code_session_id` / codex thread id was persisted): the session row moves to `BLOCKED` and an operator message — "Session interrupted mid-work — reply to resume", the last `final_text` as context, and the WIP branch name if discovered (below) — is sent via the id-capturing sender and registered as a `kind='followup'` `pending_questions` row. This reuses the *exact* round-trip a genuine `[CLARIFY]`/`[GOAL]`/plan `BLOCKED` outcome already uses: `_process_clarification_answers` → `_resume_as_followup`, which for `claude_code`/`codex` routing just re-enqueues the reply and flips the session to `CLAIMED` so the next dispatch tick drains it through `resume()` on the persisted CLI id. The vault tag is deliberately left at `#agent-running` (mirroring the CLARIFY/GOAL/PLAN block path, which also doesn't swap it) — only the session row moves.
- **Unresumable** (no CLI session id was ever persisted — `init` never fired — or Telegram delivery of the notice failed, leaving no reply anchor): the session fails instead, with the same interrupted-context message sent via the plain sender and `_reconcile_vault_terminal(FAILED)` run. This is the documented fallback when resume-on-reply genuinely can't apply.

**WIP-branch discovery** (`Worker._discover_wip_branch`) is best-effort and read-only: it scans the session's own past transcript for `claude_code_tool_use` (`payload.input.command`) / `codex_tool_use` (`payload.preview`) events matching `git switch -c <branch>` / `git checkout -b <branch>`, and surfaces the last match. It never runs `git` itself — a miss just omits the branch name from the message.

**Exit metadata.** Both `_exit_metadata` implementations (`ClaudeCodeExecutor`, `CodexExecutor`) attach `{"returncode": ..., "signal": ... (if returncode < 0), "timed_out": bool, "stream_terminal_event_seen": bool}` to the terminal `claude_code_completed`/`codex_completed` transcript event and to `ExecutorOutcome.exit_meta`, which `_handle_cli_interrupted` copies into the new `cli_session_interrupted` event. `stream_terminal_event_seen` is the load-bearing field — True only when a real `result` (claude_code) / `turn.completed` (codex) event was parsed; False means the returncode==0 fallback fired on stdout just closing, which is exactly the shape of an interrupted run.

**Terminal evidence downgrade.** `executor_lifecycle.normalize_outcome` rejects a completed outcome whose evidence (`termination_evidence` merged with `exit_meta`) explicitly reports `terminal_success`, `done_seen`, or `stream_terminal_event_seen` as `False`, flipping it to `FAILED` with reason `"terminal success evidence missing"`. Because this can turn a session's terminal status without the driver itself ever appending an event, `normalize_outcome` appends a `terminal_evidence_downgrade` transcript event (`route`, `evidence_field`, `evidence`) whenever it fires — every caller (`_Adapter._outcome`, the Hermes and managed-poll call sites, `_handle_outcome`) passes its `transcript_store` through for this.

**Transcript event kinds.** `cli_session_interrupted` (the interrupted disposition itself, payload above), `cli_interrupted_prompt_registered` (message ids + WIP branch once the notice is sent), `cli_interrupted_prompt_undelivered` (Telegram delivery failed, falling through to the unresumable-failed path), `claude_code_dispatch_crashed` / `codex_dispatch_crashed` (the executor's `execute()`/`resume()` call itself raised — `phase` records which — and the session is marked `FAILED` directly, with no `STATUS_COMPLETED` fallback in play).

Deliberately out of scope here: the CLI system prompt's canonical-checkout discipline (the field session also left the shared checkout on its WIP branch, stalling autodeploy) — that's prompt/wrapper text touching live sessions and is tracked separately.

### Delegation tier + single-message

When an agent delegates to a `claude_code` child, two behaviors keep the cost down and the operator's inbox clean:

- **Tier.** `lifeos_agent_spawn`'s optional legacy `tier` (`haiku` / `sonnet` / `opus`) is persisted on the child's `sessions.claude_code_model` column (additive migration) and threaded into `_build_command` as `--model`. When omitted, the column stays NULL and the CLI's configured default is used without a `--model` flag. The legacy field is ignored for non-`claude_code` engines.
- **One operator message.** A spawned child (`parent_session_id` set) stays silent to the operator: `ClaudeCodeExecutor` suppresses live `[NOTIFY]`/heartbeat streaming and instead folds the notify bodies into `final_text` (`_effective_final_text`), and `_dispatch_claude_code_session` skips the terminal Telegram send for children. The child's `final_text` is persisted in its `claude_code_completed` transcript event, where the parent reads it via `_child_final_text` — so the parent's single completion message carries the child's findings. That message is flagged by `_escalation_note` with the engine + tier, e.g. `⤴️ Escalated to Claude Code (haiku)`. Operator `/claude` sessions (no parent) stream and send normally. The `codex` dispatch path applies the same child gate: a codex child's completion neither sends to the operator nor registers a followup anchor, and its `final_text` is persisted in the `codex_completed` event where `_child_final_text` reads it. Failure/budget notices are child-gated too on both CLI paths — the parent's resume turn carries the child's terminal status header, plus a `reason:` line read from the child's `child_failed_internal` / `child_budget_exceeded_internal` transcript event, written by `_handle_outcome` for local/managed children and by the CLI dispatch tails.

---

## Executor lifecycle contract

The worker routes lifecycle operations through the internal executor registry
(`api/services/agent_worker/executor_lifecycle.py`). Each route adapter declares
`start`, `resume`, `resume_after_children`, and `cancel` capabilities; an
unknown or older route returns the stable `unsupported_resume` result before
`yield_waiting_for` or `STATUS_YIELDED` is written. There is no LocalExecutor
fallback for a route that cannot continue natively.

Every normalized outcome carries the LifeOS session/attempt identity, route,
persisted continuation identity, transport-only usage, and termination evidence.
Completed status is accepted only when the executor's terminal evidence is
valid; Hermes specifically requires a `done` event, non-empty content, and no
error. Session/attempt identity and usage fields are transport data; ledger and
served-model provenance remains owned by the usage ledger.

Project-handoff recovery preserves the source turn fence across process
restarts. Reconciliation can activate staged children and the bounded
coordinator only after it verifies the persisted source session, attempt, and
turn have quiesced and the exact source turn is not cancelled. The worker
records an exact-turn executor return before its cancellation guard; when
cancellation already owns a local or Managed turn, it retains the quiescence
event but skips source completion, finalization, and child release. A Hermes
return records quiescence only with a positive upstream `done` event, including
after cancellation; a disconnect, deadline, or local cancellation marker alone
does not prove the upstream turn stopped. Operator teardown can record the same
evidence only when the existing Managed post-kill state probe reports a terminal
provider state and the persisted session, attempt, and turn still match. A
terminal or absent local row, best-effort CLI stop, missing Managed driver, or
registry cancellation alone is not proof. These unknown stops remain pending
with an explicit failure, so the same scoped cancellation can be retried after
evidence arrives. This applies to an interrupted stage before any child exists
as well as to an already-derived project.

`SessionStore` persists an immutable `attempt_id` and attempt number for each
deliberate execution, plus a new immutable `turn_id` for every executor start
or native continuation. The current ids are mirrored on `sessions`; additive
`execution_attempts` and `execution_turns` tables retain prior retries and
turns so late events and usage observations cannot be re-keyed onto a newer
attempt. Legacy rows are backfilled when their first executor turn begins.

Child waits preserve each engine's continuation: local and remote append to the
existing conversation, Managed Agents recreates a remote session with the same
LifeOS lineage, Claude Code/Codex resume their persisted CLI IDs, and Hermes
resumes the same persona/conversation. CLI and Hermes work is submitted through
bounded off-tick dispatch with an in-flight guard. Hermes also enforces its
configured read-idle timeout plus a separate absolute wall deadline bounded by
the session budget; a late result after cancellation/terminal state is recorded
as ignored and cannot reopen the attempt.

## Budget enforcement

### Usage and provenance ledger

`api/services/agent_worker/usage_ledger.py` is the canonical accounting
authority for worker executions. It stores additive `usage_observations` and
the current `usage_ledger` projection in the same SQLite database as
`SessionStore`; it does not change executor selection or catalog/readiness
facts. A row is addressed by the stable execution tuple
`(session_id, attempt_id, turn_id)`. `attempt_id` and `turn_id` are owned by
the executor lifecycle; provider event ids are only delivery keys and never
replace that tuple.

Every quantity carries `measured`, `estimated`, or `unknown` evidence. Billing
is one of `subscription`, `metered`, `local_free`, or `unknown`. Subscription
CLI usage (Codex and Claude Code) remains visible as an API-equivalent estimate
but does not enter metered billed totals. Hermes bridge labels remain requested
identity unless an executor supplies authoritative served evidence; unknown
served engine/model/provider fields stay unknown rather than inheriting a
configured request. Repeated observations are no-ops, cumulative snapshots
become deltas, and an explicit measured correction can replace an earlier
unknown/estimated observation without double counting. `usage_reservations`
holds bounded unknown/estimated admission amounts separately from billed
`daily_spend`; a non-positive daily cap pauses every route, including local and
subscription routes.

`UsageLedger.replay_projection()` updates the additive fields on the existing
`UsageStore` database by stable usage key. The two SQLite files are not treated
as one transaction: the session ledger is authoritative, and the projection is
safe to replay after a crash.

Four overlapping layers, executed in this order. On an in-process route (local, remote-forced, managed), any breach in layers 2–3 **yields and asks instead of ending the session** — see [Breach → yield → ask](#breach--yield--ask) below. The Claude Code and Codex CLI routes are outside all of this: subscription-billed with no marginal cost, they carry no wall/token/dollar enforcement and never yield or ask on a breach.

1. **Daily $-cap** — `SpendTracker.can_start_task(estimated)` short-circuits to False when the day's *effective* cap (`SpendTracker.effective_cap_dollars`) is `<= 0` (operator pause). Otherwise blocks new claims when accumulated day spend + `estimated` would exceed the effective cap. This is not a per-task budget breach and never yields or asks a session — see [Daily-cap notice and reply](#daily-cap-notice-and-reply) below for the once-a-day notice this gate does send.
2. **Per-task token / wall caps** — checked at the top of every executor turn (local) or every poll (managed). The token cap only applies when a title hint set one (`max_tokens` defaults to `None`). The **dollar cap (`max_dollars`) is a real backstop only on the managed/API route and the remote-forced route** — the two with marginal per-task cost. The Claude Code and Codex CLI routes are subscription-billed and the local route is free, so none of them enforce a per-task dollar cap (they track cost for `/agents` reporting but never stop on it). Because that exemption is load-bearing, "subscription-billed" is enforced rather than assumed: `ClaudeCodeExecutor._clean_env` strips every `ANTHROPIC_*` and `CLAUDE*` variable from the CLI subprocess (an inherited `ANTHROPIC_API_KEY` takes precedence over the claude.ai login, and would put an uncapped session on the API), and `inter_agent.spawn` rejects `model="claude"` when the lineage's root is a CLI session.
3. **Lineage caps** — for child sessions, the entire lineage's combined spend is checked against the lineage root's own `max_dollars`. Breach yields and asks exactly like the other dimensions, leaving the rest of the lineage running: sibling conversations survive a cap the operator may simply raise. This is the one breach a spawned child (which otherwise has no operator-facing channel) is allowed to ask about, because the cap it names lives on the lineage root, not the child itself.
4. **Remote (managed only)** — a mid-flight breach for a managed session leaves the remote session alive, untouched (no new message posted, no `DELETE /v1/sessions/{id}` call), so a `yes` reply can genuinely resume the same conversation; `ManagedExecutor.poll`'s own entry guard (`SessionStore.has_open_budget_question`) skips a parked session so leaving it alive costs no further provider calls while unanswered.

### Breach → yield → ask

A breach in layer 2 or 3 above, on an in-process route, ends the executor's turn with session status `YIELDED` (not `BUDGET_EXCEEDED`) and `ExecutorOutcome.termination_evidence["budget_breach"]` set to the dimension name (`"wall_seconds"`, `"max_tokens"`, `"max_dollars"`, or `"lineage_max_dollars"`). `LocalExecutor._finalize_budget_yielded` is the local/remote-forced implementation; `ManagedExecutor.poll`'s dollar/token breach block is the managed one. Both leave the session's stored conversation untouched — nothing is cleared or re-seeded.

`Worker._handle_outcome`'s `STATUS_YIELDED` branch reads that evidence field and, for a non-spawned session (or a spawned child whose breach was specifically `lineage_max_dollars`), calls `_ask_budget_question`: it swaps the vault tag `agent-running` → `agent-blocked` (the same tag a clarification uses — Human queue lane derivation is tag-based, so this works even though `session.status` stays `YIELDED`, not `BLOCKED`) and sends a `kind='budget'` pending question through the same Telegram/Hermes channel `ask_user_via_telegram`/`_ask_user_via_hermes` already serve clarifications with. A spawned child's breach on any *other* dimension keeps the pre-existing terminal treatment (`STATUS_BUDGET_EXCEEDED`, recorded via `child_budget_exceeded_internal` for the parent to consume) — children have no operator channel to ask through.

`Worker._process_clarification_answers`/`_process_claimed_answers` dispatch a `kind='budget'` reply to `_resume_budget`, which reads the breached dimension from the session's `budget_yielded` transcript event (`_recorded_budget_breach`, the same idiom `_recorded_goal_condition` uses) and parses the reply (`_parse_budget_reply`):

- **`stop`** → `_finalize_budget_stop`: restores the `agent-running` tag, kills the remote session first if one exists, then feeds a synthetic `ExecutorOutcome(status=STATUS_BUDGET_EXCEEDED, ...)` through the ordinary `_handle_outcome`, applying the same tag swap, vault status, and cut-off notice any terminal budget outcome gets.
- **A bare `yes`** → `_extend_budget` doubles the current raw value of the breached dimension (seconds for `wall_seconds`, dollars for `max_dollars`/`lineage_max_dollars`, tokens for `max_tokens`); for `lineage_max_dollars` this writes the lineage **root's** `budget_json`, not the breaching (descendant) session's own, via `SessionStore.update_budget`.
- **`yes $12` / `yes 90 min`** → sets the cap to that value instead of doubling it; a reply whose unit doesn't match the breached dimension (e.g. a dollar amount for a wall-clock breach) is treated as unparseable.
- **Anything else** → a short usage-note question is sent (`kind='budget'` again) and the session stays parked.

A successful extension swaps the tag back to `agent-running`, writes `STATUS_RUNNING`, and resumes: on the local/remote-forced route by calling `_execute_start` again (the executor re-reads `budget_json` fresh from the row at the top of its loop each iteration, not a copy captured before the breach, so the extension takes effect immediately and a session that still breaches the new cap asks again); on the managed route, by simply flipping status back to `RUNNING` and letting `_poll_managed_sessions`' ordinary loop continue a remote session that was never actually interrupted.

An unanswered `budget` question leaves the session `YIELDED` indefinitely. `Worker.resume_pending()`'s startup-recovery sweep already skips every `STATUS_YIELDED` session unconditionally (there for a sleeping session's `sleeps` row) — a budget-parked session has no `sleeps` row, but is skipped by the same unconditional check, so it costs nothing and is never re-dispatched until an operator reply resolves it.

### Daily-cap notice and reply

The daily $-cap has no session to yield, so it doesn't reuse the `budget` question kind above. `SpendTracker` (`api/services/agent_worker/spend_tracker.py`) stores, per local date, alongside the running `total_dollars`: `cap_override_dollars` (set by `set_cap_override`, read by `effective_cap_dollars`) and `notified_cap_dollars` (set by `mark_cap_notified`). Both live on the same per-date `daily_spend` row as the spend total, so a raised cap survives a worker restart and reverts on its own once the date rolls over — no separate cleanup.

When `Worker.tick()`'s admission gate finds `can_start_task` false, it compares the day's effective cap against `notified_cap_dollars`: a cap value not yet notified today gets exactly one Telegram notice (`_send_daily_cap_notice`) stating today's spend and the cap to two decimals, then `mark_cap_notified` records that value so a later tick at the *same* cap doesn't repeat it. A cap raised afterward is a different value, so crossing it is a fresh, one-time notice.

The notice is sent via `ask_user_via_telegram(qid, qid, body, kind="daily_cap")` where `qid` is a per-date synthetic id (`daily_cap:<iso-date>`, `SpendTracker.today_key()`) rather than a real session id. `ask_user_via_telegram` already falls back to a plain primary-bot send plus a `pending_questions` row when `session_id` doesn't resolve to a session — the same shape needed here, since there's no session to anchor a Hermes DM against. `pending_questions.kind='daily_cap'` is the one kind `Worker._process_claimed_answers` resolves before its session lookup rather than after, exactly because it's expected to have none. `Worker.tick()` drains it via `_process_daily_cap_replies()` *before* the admission gate runs each tick — like the human-queue and lifecycle-drift calls above it, a `raise to $N` reply must be processed even while the worker is paused at its cap, or the reply that's supposed to lift the cap would never be read. `list_open_questions()` (the board's pending-question feed) excludes `daily_cap` — it's a worker-level notice, not a task's pending question, so it never appears in a card drawer.

`_parse_daily_cap_reply` is a sibling of `_parse_budget_reply`, not an extension of it: `raise to $150` / `raise to 150` is a distinct grammar from `yes`/`stop`, and there is no "double the cap" default for a bare reply. `_resume_daily_cap` sets `SpendTracker.set_cap_override` on a parse and confirms by plain notify; an unparseable reply gets a short usage note (via a fresh `daily_cap` question, so the reply channel stays open) and leaves the cap untouched.

The two admission checks that read `daily_cap_dollars` — `SpendTracker.can_start_task` and the two `UsageLedger.reserve`/`adopt_reservation` calls in `Worker.tick()`'s claim loop — all read `effective_cap_dollars()`, not the configured field directly, so a same-day raise actually lifts the ledger's own atomic admission gate, not just the tracker's.

---

## Restart resumability

The worker is signal-safe and crash-resumable. `resume_pending()` runs on startup and scans non-terminal sessions:

- `YIELDED` with a `sleeps` row → leave alone (sleeps loop wakes it on schedule).
- `BLOCKED` → leave alone (waiting on Telegram reply or operator unblock).
- `CLAIMED`, operator-origin, with an undelivered `pending_messages` row → leave alone. This is a native CLI resume (a follow-up reply, or a persistent-project-owner wake — see [Inter-agent coordination](#inter-agent-coordination)) that was queued but never actually launched before the crash/restart; `_dispatch_spawned_sessions` picks it up normally on a later tick the same way it would have if the worker hadn't restarted, because a resume peeks its pending message rather than draining it, so nothing is lost either way.
- Anything else (`CLAIMED` / `RUNNING` mid-execution) → undo the claim tag (swap `#agent-running` → `#agent` when the card had no engine assignee; otherwise remove `#agent-running` alone so an engine-only card is not injected with `#agent`), mark session `FAILED` in the DB, notify operator.

A managed session's `managed_agent_session_id` is durable across worker restarts — on resume the worker reattaches via `GET /v1/sessions/{id}` and continues polling from `managed_cursor.last_event_id`.

---

## Lifecycle drift reconciliation

`Worker._reconcile_lifecycle_drift()` runs once per `tick()`, after `_process_human_queue()` and `_replay_wait_wakeups()` and ahead of the spend-cap check — deliberately ungated by that cap, since it reconciles existing state and starts no new work.

`update_status` fires the `set_status_projector` hook whenever it lands a terminal status, which reconciles the vault tag through `lifecycle_projector`. A kill instead flips the session row via `mark_cancelled` — a raw status write with no projector hook. A kill landing on a session still being actively polled still gets reconciled on the executor's next poll, but a kill landing on a session parked at `BLOCKED`, or while the worker itself is down, leaves no poll to do that — the vault tag stays stranded at `#agent-running`/`#agent-blocked`.

The sweep lists every task currently carrying `RUNNING_TAG` or `BLOCKED_TAG` via the task API, and for each one looks up its backing session with a local primary-key lookup (`session_store.get`, keyed on `task_id`). A session that doesn't exist, or belongs to an operator root-spawn/spawned child with no real vault task, is skipped. A session whose status is still non-terminal is legitimately live — including one reopened for a follow-up turn — and is left alone; only a task tagged non-terminal with a terminal session is genuine drift.

The heal writes through the same endpoints `_reconcile_vault_terminal` uses: `POST /api/tasks/{id}/swap-tag`, followed by `/complete` or a status write matching the session's terminal outcome. The swap gates the status write — it reports no change when the tag it was told to replace is absent, so an operator retagging the card in the same window is never overwritten.

---

## Telegram clarification flow

When the worker needs operator input mid-task — preflight routing=ask, ambiguity question, or `lifeos_agent_user_ask` mid-loop — it:

1. Sends a Telegram message via `send_message_capture_ids()` (returns the `message_id` of **every** 4096-char chunk).
2. Persists `(session_id, telegram_message_ids, question)` in `pending_questions` — the full chunk list in `sent_message_ids` (JSON), with the first chunk in `sent_message_id`.
3. Swaps the tag to `#agent-blocked` and parks the session.
4. For managed sessions: also calls `driver.kill_session` to stop session-hour billing while waiting.

When the operator replies (using Telegram's native reply feature), the bot's `_maybe_deposit_agent_answer()` hook intercepts the `reply_to_message_id` and calls `SessionStore.deposit_answer()`, which matches a reply landing on **any** chunk (membership in `sent_message_ids`, not just the first). The worker's `_process_clarification_answers()` runs each tick, picks up answered questions, parses the answer (for routing questions: extracts `claude code` / `codex` / `local` / `cloud` from free-text, last mention winning), updates the session, and re-dispatches.

Every worker-originated session message starts with a short, Markdown-escaped card-title prefix. The first delivered message is recorded as the session's thread anchor; later progress, question, completion, failure, and budget messages include Telegram's `reply_to_message_id` pointing to that earliest anchor. All delivered chunk ids remain registered for inbound reply matching.

For local sessions, the parent session resumes via the existing pending_messages drain.
For managed sessions, a new remote session is created with the resolved routing.

Clarifications older than `LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS` (default 72h) are abandoned with a Telegram heads-up; the transcript stays preserved.

### Child clarifications

Spawned CLI children never enter the Telegram flow above — the operator owns no thread to a child, and a BLOCKED child would strand its yielded parent (which only resumes once every child is terminal). Instead, a child's `[CLARIFY]` is folded into its output as `[needs clarification] …` and the child COMPLETES (`_effective_final_text` / `claude_code_child_clarify_folded`). The parent reads the question in the child's relayed output on resume and answers via `lifeos_agent_send`, which reopens the completed child (see [Inter-agent coordination](#inter-agent-coordination)); the parent then `yield_until`s the child again. Operator `/claude` sessions keep the pause-and-reply behavior: their `[CLARIFY]` goes BLOCKED and waits on a threaded Telegram reply.

### Replyable terminal threads

Every terminal-state notification — `#agent-completed`, `#agent-failed`, and `#agent-budget-exceeded` — registers a follow-up (`kind='followup'`) via `register_completion_followup()`, so a reply reopens the session as a new user turn (`_resume_as_followup()` swaps whichever terminal tag is current back to `#agent-running`). This makes failures and budget cut-offs replyable, not just clean completions.

Targeting a thread on Telegram is **explicit only**: a **native reply** to any chunk of a notification resumes that specific thread (works regardless of age). A plain (non-reply) message is always a fresh chat query — there is no implicit "recent thread" capture, so an unrelated question right after a task finishes is never silently swallowed into the agent thread.

A reply landing on any other operator-facing session message — a heartbeat, a `[NOTIFY]` body, an ack — is instead a `kind='status_anchor'` row (`add_reply_anchors`), handled by `telegram.py`'s `_handle_status_anchor_reply()`. On a RUNNING/CLAIMED/BLOCKED session the note is enqueued immediately and simply rides the next `pending_messages` drain. On a terminal (`claude_code`/`codex`) session with a persisted CLI id, the vault-task-backed card's terminal tag is swapped back to `#agent-running` (whichever of `#agent-completed`/`#agent-failed`/`#agent-budget-exceeded` is current) with the task status set to in-progress — the same tag/status pairing `_resume_as_followup` and `code_reopened_for_pending_messages` already perform — and only once BOTH writes succeed is the note enqueued and the session set to `claimed` (`_reopen_vault_task_for_resume()`). An operator root-spawn has no backing card and skips straight to the claim. If the tag swap finds no terminal tag present (the operator retagged or reassigned the card in the meantime), or if the tag swap succeeds but the status write then fails, the reopen fails closed: on a status-write failure the tag swap is undone (best-effort, restoring the original terminal tag) so the card is never left at `#agent-running` with a stale terminal status. Either way nothing is enqueued, the session is left exactly as it was, and the acknowledgment says the resume couldn't be completed rather than the ordinary "queued" ack — a failed reopen never leaves a live note behind to resurface on some later, unrelated reopen. On a successful reopen, the session only actually resumes once a later dispatch tick confirms the CLI subprocess launched (see [Reopened top-level CLI sessions](#reopened-top-level-cli-sessions)) — the deposit-time ack never claims otherwise.

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

## Agent Output notes

On every successful completion (root sessions only — not spawned children or operator root-spawns), `_completion_summary` calls `_write_agent_output` to persist the agent's final text as a Markdown note under `settings.agent_output_dir` (`LIFEOS_AGENT_OUTPUT_DIR`, default `LifeOS/Tasks/Agent Output`). This is unconditional now — it supersedes the old "spill to vault only when the reply exceeds 2000 chars" behavior — so short answers also get a durable note. Failed / blocked / budget-exceeded outcomes write nothing; an empty final text writes nothing.

Two layouts:

- **One-off task** → a new note `<YYYY-MM-DD>-<slug>-<sid>.md` (the trailing 6-char session id prevents same-day/same-slug clobbering), with `task` / `session_id` / `routing` / `created` / `source: agent-worker` frontmatter.
- **Recurring (cron) schedule** → one shared note per schedule. The scheduler stamps the handed-off engine-assigned task with a `sched-<id>` tag (see [scheduler.md](scheduler.md)); `_schedule_id_from_task` reads it on completion, resolves the schedule's name via `GET /api/scheduler/{id}` for a readable filename `<schedule-slug>-<id>.md` (falling back to `recurring-<id>.md`), and `_recurring_content` prepends this fire above prior runs under a `## YYYY-MM-DD HH:MM` heading — newest first, frontmatter `created` preserved and `updated` bumped.

The Telegram summary links the note; over-length replies show a preview + link instead of the full body. When the vault path is unset or the write fails the worker keeps the inline summary so the operator never loses content.

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
| Stuck-claimed self-heal | `LIFEOS_AGENT_STUCK_SESSION_TIMEOUT_MINUTES` |
| Card assignment | `LIFEOS_AGENT_HOSTS`, `LIFEOS_AGENT_SSH_CONNECT_TIMEOUT`, `LIFEOS_AGENT_MODEL_CATALOG_TTL_SECONDS`, `LIFEOS_CODEX_MODELS_CACHE_PATH`, `LIFEOS_OPENAI_API_KEY` |

---

## Related Documents

- [ADR-008: Managed Agents Cloud Routing](../../adr/008-managed-agents-cloud-routing.md) — Decision record for the local-vs-cloud executor split
- [ADR-018: API Spend Requires Operator Consent](../../adr/018-api-spend-requires-consent.md) — Why an inferred cloud route asks, and why the CLI subprocess carries no API credential
- [ADR-026: A Budget Breach Asks; It Does Not Fail](../../adr/026-budget-breach-asks.md) — Decision record for the yield-and-ask contract described in [Budget enforcement](#budget-enforcement)
- [Agent Worker — Product](../product/agent-worker.md) — What `#agent` does, consumer view
- [Agent Worker — Setup](../../guides/agent-worker-setup.md) — Operator setup walkthrough
- [Agent Worker — Setup § Working directory](../../guides/agent-worker-setup.md#working-directory-run-a-local-or-cloud-card-in-an-isolated-checkout-925) — Operator-facing walkthrough for the guard described here
- [Agent Viz — Technical](agent-viz.md) — `/agents` page that reads SessionStore + TranscriptStore here
- [Agent Viz — Product](../product/agent-viz.md) — Board drawer pickers and Open action that write the card-assignment fields read here
- [Task Management](../product/task-management.md) — How engine-assigned tasks sit alongside regular tasks
- [Human Queue](../../guides/human-queue.md) — Cards the worker's poll tick auto-resolves via `done_when`
- [MCP Tools](../product/mcp-tools.md) — Standard MCP catalog including `lifeos_agent_*` family
- [API Reference](../product/api-reference.md) — Task endpoints the worker uses (`/api/tasks/{id}/swap-tag`, `/api/tasks/{id}/complete`)
- [Architecture](architecture.md) — Where the worker fits in the broader code structure
- [Observability](observability.md) — Tracing, perf, and logging patterns the worker uses
- [Client Surfaces](client-surfaces.md) — The `/chat` SSE contract the `?conversation=` deep link is additive to
