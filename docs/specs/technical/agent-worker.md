# Agent Worker — Technical

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-03

Engineering view of the agent worker — the stand-alone process that consumes `#agent`-tagged tasks and runs them on either a local LLM or Anthropic Managed Agents. For consumer-facing behavior, see [product/agent-worker.md](../product/agent-worker.md). For operator setup, see [guides/agent-worker-setup.md](../../guides/agent-worker-setup.md).

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
9. [Card assignment (#851)](#card-assignment-851)
10. [System prompts](#system-prompts)
11. [Inter-agent coordination](#inter-agent-coordination)
12. [Budget enforcement](#budget-enforcement)
13. [Restart resumability](#restart-resumability)
14. [Telegram clarification flow](#telegram-clarification-flow)
15. [Transcripts](#transcripts)
16. [Agent Output notes](#agent-output-notes)
17. [Configuration surface](#configuration-surface)
18. [Related Documents](#related-documents)

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
| `worker.py` | Main poll loop, claim/dispatch, startup resume, signal handling, Telegram delivery, completion summaries, Agent Output notes |
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

## Session store schema

`session_store.py` creates eight tables (SQLite, `data/agent_sessions.db` by default). The schema is deliberately permissive — new columns and tables have been added issue-by-issue rather than designed up front — so this list is read directly off the table-creation code (`_SCHEMA` in `session_store.py`) rather than written from memory; re-check the source if it looks stale.

### `sessions`

One row per agent session (1:1 with a claimed task, or a root-spawned operator session — see `origin` below). `task_id` is the primary key, **not** `session_id` — the two are related but distinct ids.

| Column | Meaning |
|---|---|
| `task_id` (PK) | The `#agent` task this session was claimed from (or a synthetic id for operator sessions). |
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
| `origin` | NULL or `"agent"` = claimed from an `#agent` task; `"operator"` = root-spawned on demand with no backing task (#235). |
| `claude_code_session_id` | Claude Code (or Codex) CLI session/thread id, for `routing="claude_code"`/`"codex"` — the column is reused for both; `routing` disambiguates which CLI it belongs to. |
| `claude_code_model` | Claude tier for `routing="claude_code"` (`haiku`/`sonnet`/`opus`); NULL falls back to the CLI's own default (`opus`). |
| `bot` | Telegram bot that owns this session's notices; NULL = primary bot (#348). |
| `unpriced` | Sticky flag: set once any turn was priced against a model `pricing.py` doesn't recognize, so a reader can tell "$0.00 total" apart from "some turns couldn't be priced" (#669). |
| `host` | Board-assigned host name from `settings.agent_hosts` (#851); NULL/`""` = this API host. Read by the `claude_code`/`codex` executors to decide whether to spawn locally or wrap the argv in `ssh`. |
| `model`, `effort` | Board-assigned model id and effort level (`low`/`medium`/`high`/`max`, #851), threaded into the executor's argv — see [Card assignment](#card-assignment-851) below. Distinct from `claude_code_model`, which predates this and is set by `lifeos_agent_spawn`'s `tier` argument, not the board. |
| `conversation_id` | Hermes conversation id (#851, `routing="hermes"` only) — set by `HermesExecutor` once the turn's `conversation_id` SSE event arrives, and what a card's `session.open_url` points `/chat?conversation=` at. |
| `remote_pgid` | Process-group id a remote-spawned subprocess echoed back on its first stdout line (#851) — see [Host registry and ssh spawn](#host-registry-and-ssh-spawn-851). Used by the operator kill endpoint to reach the process over ssh; NULL for a local session. |

Indexed on `status`, `parent_session_id`, `root_session_id`, and (partial index) `status = 'yielded'`.

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

Open clarification questions sent to the operator via Telegram, and completion-message follow-ups (an operator reply to a finished task's Telegram message that continues the thread). `kind` distinguishes the two: `"clarification"` blocks the session until answered; `"followup"` reopens an already-`completed` session and appends the reply as a new turn.

| Column | Meaning |
|---|---|
| `id` (PK, autoincrement) | Row id. |
| `session_id`, `task_id` | The session and task this question belongs to. |
| `question` | Question text sent to the operator. |
| `sent_message_id` | Telegram message id the answer is matched against. |
| `sent_at` | Unix epoch seconds. |
| `answer`, `answered_at` | Populated once the operator replies. |
| `processed` | 0/1 — whether the worker has resumed the session on this answer. |
| `timed_out` | 0/1 — set if the clarification aged out (default 72h) before an answer arrived. |
| `kind` | `"clarification"` or `"followup"` (default `"clarification"`). |
| `sent_message_ids` | JSON array of every Telegram chunk id for this notification (a long completion splits across multiple messages); NULL for legacy rows, which still match via `sent_message_id` alone. |
| `bot` | Telegram bot that sent this question; NULL = primary. Scopes reply-matching so a doctor-bot reply can't collide with a primary-bot question sharing the same numeric message id (#348). |

### `daily_spend`

The daily $-cap ledger `spend_tracker.py` reads and increments.

| Column | Meaning |
|---|---|
| `date` (PK) | Calendar date, as text. |
| `total_dollars` | Total spend booked against that date. |

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
| `tool_loop_signature`, `tool_loop_count`, `tool_calls_since_message` | Runaway-loop detection counters (#139 §5), persisted so cross-poll signals survive worker restarts mid-session. |

### `cli_sessions`

One row per Claude Code / Codex CLI session registered from any host via `POST /api/agents/cli-sessions/events` (#849), keyed `cc:<uuid>` / `cx:<uuid>` and carrying event-driven status (`idle` / `running` / `ended`) instead of a file-age guess. See [Cross-machine CLI session registration](agent-viz.md#cross-machine-cli-session-registration) for the column table and status machine.

---

## Lifecycle of a task

```
poll → resolve Human-queue cards whose done_when now passes (throttled by
        LIFEOS_HUMAN_QUEUE_POLL_SECONDS; runs before the spend guard — it
        never spends)
     → spend tracker check (`can_start_task(default_budget)`)
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
             routing in {local, claude, claude_code, codex, ask}
             expected_output in {text, file, external_action, structured}
             ambiguity: question | null
             sane: bool
         dispatch on routing:
             local              → LocalExecutor.execute(session, task)              — inline, tick thread
             claude             → ManagedExecutor.start(session, task)              — inline, tick thread
             claude_code / codex → _submit_cli_dispatch(...)                        — off-tick, on _cli_pool (#753)
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
                 ├────► BUDGET_EXCEEDED  (terminal — wall/tokens/dollars cap)
                 ├────► BLOCKED     (waiting on Telegram, or Managed Agents not configured)
                 └────► YIELDED     (sleep tool / yield_until — wake on timer or child completion)
```

Each session also tracks `routing`, `budget`, `expected_output`, `total_input_tokens`, `total_output_tokens`, `total_dollars`, `managed_agent_session_id`, `started_at`, `parent_session_id`, `root_session_id`, `spawn_depth` (for lineage budgets), and `yield_waiting_for` (when in `YIELDED` from `yield_until`).

---

## Preflight

The preflight classifies a task before executor dispatch, cheap (~$0.001) and fast (~1s). Which LLM client runs the classifier call is controlled by `LIFEOS_AGENT_PREFLIGHT_ENGINE` (#808), default `auto`:

- **`auto`** (default) — the pre-existing priority order: Anthropic (`claude-haiku-4-5` by default) when `ANTHROPIC_API_KEY` is set, with no reachability probe on this branch; else the local llama-server if reachable; else the remote provider described under "Local executor" below, if configured and enabled (`LIFEOS_AGENT_REMOTE_EXECUTOR` + `remote_llm_configured`); else the call raises, which `run_preflight()` degrades to `sane=False`/`routing=ask` like any other preflight failure. This keeps an install with no Anthropic key from failing every `#agent` task at the classification step, before the local-executor fallback below ever gets a chance to run. Every existing install is byte-identical to pre-#808 behavior under this default.
- **`remote`** — build the remote OpenAI-compatible provider (e.g. Fireworks running DeepSeek) first, when `remote_llm_configured`. Built the same way the `auto` chain's own remote fallback is, and used **unprobed** by design (the same #706 convention the `auto` chain already follows for that branch — the remote client is trusted, not health-checked) — so this never adds a reachability check that wasn't already implicit in the request itself. A failure of the completion call is not caught specially; it propagates to `run_preflight()`'s existing except-clause exactly like a failure on any other engine. If the provider *isn't* configured, the call raises — a forced engine never silently falls back to another one, and in particular never to the Anthropic API, which is the spend `remote` exists to avoid; `run_preflight()` degrades the raise to `routing=ask`, so the operator sees a confirmation question rather than a surprise API bill. Operator motivation (#808): all five observed field instruction-deviations were Haiku's, while the remote provider has executed real tasks cleanly — classifier engine choice is a quality lever, not a safety dependency (routing/ambiguity/sanity opinions already can't cancel, bypass the default route, or block under one — see #747/#751/#757/#803 below).
- **`anthropic`** — force the Anthropic branch. Falls through to `auto` (with a logged warning) if no API key is configured.
- **`local`** — force the local llama-server client. Still probed via `is_available()`, same as the `auto` chain's own local branch — but since there's no further engine to fall back to for a forced value, an unreachable server raises (degrading via the same except-clause) rather than silently trying something else.
- Any other value is treated as `auto`, with a logged warning — never a crash over a typo'd env var, mirroring `LIFEOS_AGENT_DEFAULT_ROUTE`'s own invalid-value handling below.

None of this changes which engine an `#agent` task itself dispatches to — only which client classifies it. Spend attribution: preflight calls do not write to the usage store (`usage_store.record_usage`) on **any** engine today, including the pre-#808 Anthropic/local/remote branches — usage recording is caller-side and lives only in the chat and Hermes-proxy routes, not in `llm_client.py` or the agent worker. `#808` preserves that: a preflight call on the remote engine is exactly as unattributed as one on Anthropic or local was before it, so `remote_llm_*_price_per_mtok` never sees a preflight-driven row. Returns:

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
    demoted_ambiguity: str | None   # #751: ambiguity text demoted to advisory, if any
    demoted_routing: str | None     # #757: LLM route demoted to the default, if any
    demoted_sanity: str | None      # #803: non-fatal sane_reason demoted to advisory, if any
    raw: dict                       # the parsed JSON for debugging
```

Routing precedence (per the prompt instructions):

1. `#local` tag → routing=local
2. `#cloud` tag → routing=remote (#809: the configured remote OpenAI-compatible provider, never Anthropic); `#cloud-haiku` / `#cloud-sonnet` tag → routing=claude
3. Title contains explicit model cue ("use claude", "with opus", "using gemma", "use the anthropic api") → claude / local
4. Title contains capability-implying phrase ("search my gmail", "google drive", "send a slack message", etc.) → claude (those tools require cloud connectors)
5. Otherwise → `LIFEOS_AGENT_DEFAULT_ROUTE` if set, else ask (worker pauses the task and asks via Telegram)

`LIFEOS_AGENT_DEFAULT_ROUTE` (empty by default) applies when preflight would otherwise land on `ask` purely for lack of routing cues — not when a *fatal* sanity failure is the reason, and not when the classifier inferred a cloud route that the API-consent downgrade below sends to `ask` — **and also** (#757) when the model returned a `local`/`claude_code`/`codex` route on its own initiative but the title doesn't corroborate it. A *non-fatal* sanity failure (#803) is demoted before this check runs, so it no longer excludes a task from the substitution either. It exists for a single-executor install (e.g. local-only, no Claude Code/Codex/Managed Agents) where a multi-engine clarification question has nothing useful to offer. Tag overrides (`#local`, `#cloud`, etc.) always take precedence over it. An invalid value logs an error and falls back to `ask` rather than crashing the worker loop.

**Ambiguity demotion (#751).** When `LIFEOS_AGENT_DEFAULT_ROUTE` is set to a valid route, a non-null `ambiguity` no longer blocks the task, regardless of what routing was ultimately picked (default-route substitution, a corroborated LLM route, or a tag override). Configuring a default route is the operator saying "run untagged tasks without asking me"; a cheap classifier's hedging shouldn't override that standing instruction — especially since string-matching the hedge's prose (the #748 fix) proved to be whack-a-mole once the model started rephrasing around the pattern. The question is preserved on `demoted_ambiguity` and logged to the session transcript as advisory context rather than discarded, and the executing agent can still ask a specific question mid-run via `lifeos_agent_user_ask` if it genuinely needs to. The #584 unconfirmed-cloud downgrade still blocks either way (never auto-spend on inferred cloud routing). With no default route configured, ambiguity blocks exactly as before.

**Sanity demotion (#803).** The same standing-instruction argument applies to sanity: a *non-fatal* `sane=false` — the classifier's own inferred "this isn't executable" opinion, as opposed to a `sane_fatal` verdict the code itself established (empty title, the deterministic destructive-title regex, or a preflight-call/parse error) — is demoted to advisory under the identical gate (`LIFEOS_AGENT_DEFAULT_ROUTE` set and valid). This exists because the classifier has repeatedly misjudged ordinary feature requests as "a product specification or feature request, not a task an agent can execute" — building features is half the point of this pipeline, and #747 turning that misjudgment into a park (rather than a cancel) still cost the operator a confirmation round-trip for legitimate work every time it fired. `sane_reason` is preserved on `demoted_sanity` and logged to the session transcript the same way `demoted_ambiguity` is, and `sane` itself flips back to `True` — which also means a demoted sanity objection no longer blocks the default-route substitution below it (a task that was both sanity-flagged and routing-`ask` now both demotes *and* routes on the same pass). `sane_fatal` verdicts are completely unaffected by this setting in either direction — they fail closed regardless of whether a default route is configured. The preflight prompt itself was also updated with an explicit line ("feature requests and product specifications ARE executable tasks... never mark them insane") as defense in depth, not the fix — the classifier has ignored negative constraints in its prompt before, so the demotion is what actually holds. With no default route configured, non-fatal sanity still parks exactly as under #747.

**Route corroboration (#757).** A default route only rescues a genuine `ask` outcome for a *cloud* route — the classifier naming `local`/`claude_code`/`codex` on its own used to always stand unchallenged, because those routes aren't `ask` and so skipped the substitution above entirely. That let a noncompliant classifier invent an explicit-looking route (e.g. `routing="local"` with a plausible-sounding but non-cue reason) and have it silently beat a configured default. Now, whenever a default route is configured and valid, an LLM-chosen `local`/`claude_code`/`codex` route must be corroborated by the title — `routing_explicit=true` from the model *and* a matching cue (the rule-3 phrasing for `local`; "claude code" / "codex" for the CLI routes) — or it's demoted to the configured default and logged, mirroring the ambiguity demotion above (`demoted_routing` holds the route the model actually picked). `routing_explicit=false` never corroborates, regardless of the title. Tag overrides are unaffected (a tag is the operator's own corroboration, checked first). `ROUTE_CLAUDE` is out of scope for this check — the pre-existing #584 downgrade below already corroborates cloud routes against the title, and on a miss sends them to `ask` (a confirmation question) rather than to the default, since unconfirmed API spend must stay a question even on a default-route install. With no default route configured, this is a no-op.

Hardening: response is parsed defensively (handles `` ```json `` fences, partial schemas, missing keys, exceptions). On any parse failure the result defaults to `sane=false` so the worker parks the task rather than running with garbage.

---

## Local executor (Gemma path)

`LocalExecutor.execute(session, task) -> ExecutorOutcome`. Wraps an agent loop against an OpenAI-compatible local LLM server (llama-server with `unsloth/gemma-4-26B-A4B-it-GGUF` by default).

**Remote fallback (`LIFEOS_AGENT_REMOTE_EXECUTOR`, off by default).** When enabled and an OpenAI-compatible remote provider is fully configured (`LIFEOS_REMOTE_LLM_URL`/`_MODEL`/`_API_KEY`, see [configuration.md](../../guides/configuration.md#openai-compatible-remote-provider)), a session-start reachability check that finds the local llama-server unreachable runs the session against the remote provider instead of failing — one cheap `is_available()` probe at session start, not a background prober. This exists for an install with no other `#agent` executor at all (no Claude Code, no Codex, no Managed Agents, no reachable llama-server); flag off, or the remote provider unconfigured, is byte-identical to the local-only path. It is a fallback, not a new route: an explicit `#local` tag on a host with a live llama-server is unaffected. The escalation ladder can never reach this path — its `local` rung goes through `agent_loop.py`'s `_select_client(force_local=True)`, a separate code path that never consults this flag.

**Remote route (`ROUTE_REMOTE`, the `#cloud` tag, #809) — distinct from the fallback above.** `_remote_only_llm_client` builds the same kind of `LocalLLMClient` pointed at the remote provider, but unconditionally: no `agent_remote_executor` flag check, no local-reachability probe. Tagging a task `#cloud` is itself the opt-in. `Worker._get_remote_executor` constructs a `LocalExecutor` from it (cached separately from the local one, so a mixed local + `#cloud` install never has one route silently swap the other's target client), and `_dispatch`'s `ROUTE_REMOTE` branch requires `settings.remote_llm_configured` first — unconfigured parks the task at `#agent-blocked` rather than falling back to local or Anthropic. Attribution and pricing reuse the fallback's own machinery unchanged: `is_remote=True` drives `_record_spend` (priced from `remote_llm_{input,output}_price_per_mtok` when set, else real unpriced spend) and `_served_by()` (the remote model id, surfaced via `_model_label_for_routing`/`_worker_label` as "Remote").

Per-turn flow:

1. Check budgets at top-of-loop — kill with `STATUS_BUDGET_EXCEEDED` if `total_tokens >= max_tokens` OR `wall_seconds_elapsed >= wall_seconds` OR lineage budget breached. Cascade-kill descendants on lineage breach. There is no per-session dollar cap on the local route (local inference is free, so `total_dollars` is always 0); the lineage check still enforces an ancestor's `max_dollars` for a mixed family rooted at a paid managed session.
2. Build the message list (system prompt + prior user/assistant/tool_result turns).
3. Call the local LLM with the tool catalog (`STANDARD_HANDLERS` + `lifeos_agent_*` inter-agent tools + LifeOS MCP tools, all in OpenAI format).
4. Parse response — handles both OpenAI `{"function": {"name", "arguments"}}` and Anthropic `{"name", "input"}` shapes via `_normalize_tool_calls`.
5. If response has tool_calls: dispatch each to `ToolRegistry`, append `tool_result` turns, loop.
6. If no tool_calls and content is non-empty: finalize with `STATUS_COMPLETED` + `final_text`.
7. If tool emitted a yield (sleep: `yield_seconds > 0`; `yield_until`: `yield_seconds == -1`): set `STATUS_YIELDED` and return — the worker's sleep / yield-resumption loops take over.

Cost: `$0` when served by the local llama-server (`local` maps to `$0` in `pricing.py`). A session served by the remote fallback above is priced from `LIFEOS_REMOTE_LLM_INPUT_PRICE_PER_MTOK`/`_OUTPUT_PRICE_PER_MTOK` when configured, else recorded as real unpriced spend rather than $0 — the same convention every `unpriced` usage row uses. Wall-time enforcement still applies either way.

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

## Card assignment (#851)

A Kanban card assigns a task to an engine (an assignee tag — `#claude`, `#codex`, `#local`, `#hermes`), a model, an effort level, and a host, written as `[key:: value]` inline fields (`model`, `effort`, `host`, `assigned_by` — round-tripped verbatim by `Task.fields`, see [task-management.md](task-management.md)). `assignment.py`'s `extract_assignment()` reads those four fields; `worker.py`'s `_dispatch()` calls it right after preflight and records `host`/`model`/`effort` onto the session row before any executor runs (`SessionStore.set_assignment`) — the same place `claude_code_model` has always been set.

### Effort mapping

The board's own effort vocabulary is exactly `low | medium | high | max`. Each engine speaks a different vocabulary — `assignment.py`'s `map_effort_for_engine()` translates once, in one place:

| Board effort | Claude Code (`--effort`) | Codex (`-c model_reasoning_effort=`) | Local Gemma | Hermes |
|---|---|---|---|---|
| `low` | `low` | `low` | thinking off | no override |
| `medium` | `medium` | `medium` | thinking off | no override |
| `high` | `high` | `high` | thinking on | no override |
| `max` | `max` | `xhigh` | thinking on | no override |
| unset | CLI default | CLI's own config | `settings.local_agent_enable_thinking` | Hermes reports its own model |

Local Gemma's thinking toggle is a per-SESSION override (`local_executor.py`'s `_call_llm(session_id, effort=...)`), not a mutation of the global `settings.local_agent_enable_thinking` — concurrent sessions with different assigned efforts would otherwise race each other over one process-wide flag. It only ever reaches a real `LocalLLMClient` on the local (not #809 remote-forced) route, mirroring `run_agent_loop`'s identical `isinstance(...) and not force_remote` gate — the remote OpenAI-compatible provider doesn't understand llama-server's `chat_template_kwargs` switch.

### Host registry and ssh spawn (#851)

`LIFEOS_AGENT_HOSTS` (`{name: ssh_target}`, e.g. `{"laptop": "user@laptop.example"}`) maps a board-facing host name to an ssh target. `remote_spawn.py` is the shared mechanism:

- `resolve_host_target(host, api_host_name)` — empty/unset `host`, or a match on this API's own hostname, means local (returns `None`, the existing `spawn_fn` seam runs unchanged). Anything else must be a registry key or `resolve_host_target` raises `HostResolutionError` — the executor fails the task closed (`#agent-failed`, reason naming the host) **without ever calling `spawn_fn`**.
- `build_remote_argv(argv, target, unset_env_names)` — wraps the exact local argv into `ssh -o BatchMode=yes -o ConnectTimeout=<setting> <target> -- <remote command>`. The remote command unsets every credential name `_clean_env` strips locally (mirrored via `env_names_matching_prefixes`, applied to the remote command's `env -u` prefix instead of the local Popen `env=` kwarg), then wraps the whole thing in `setsid bash -c 'echo "PGID:$$"; exec "$@"' _ …` so the remote process group id is captured as the very first stdout line. The executor strips that line (`read_remote_pgid_line`) before the normal event-stream parsing begins, and persists it via `SessionStore.set_remote_pgid`.
- The Popen call site itself is untouched by any of this — only `cmd` (built beforehand) and the local `_clean_env()`-sourced `env=` differ between the local and remote branches, which is what keeps the injection seam (`spawn_fn`/`binary_resolver`) a pure test seam rather than something remote spawn has to special-case.
- Kill: `POST /sessions/{id}/kill` on a session whose `host` is set runs `ssh <target> kill -- -<pgid>` through an injectable runner (`remote_spawn.kill_remote_process_group`) instead of the local `os.killpg` — see `inter_agent.py`'s `_kill_remote_subprocess`. An unregistered host degrades to a DB-only kill, the same way a missing local pid event does.
- Resume/focus (`api/routes/agents.py`): a session whose `cli_sessions.host` names a registered host runs the same rendered launcher template over ssh (`remote_spawn.build_remote_launcher_argv` — no pgid capture, no credential stripping; a launcher is a short-lived terminal spawner, not the long-running CLI session). Its cwd comes from the `cli_sessions` row (populated by the remote host's own hook post, #849) rather than a local transcript-file scan, which a remote session's files were never going to satisfy. `/focus` has no cross-host pane registry to activate an existing pane against (the `cc_wezterm_store` mapping is only ever written for a session that ran on THIS API host), so for a remote session it runs the same launcher `/resume` does and returns `/focus`'s response shape.

**macOS FDA limitation (documented, not solved):** Apple-data tasks (iMessage, Photos, Contacts) require Full Disk Access, which is granted per-app to the process that launched a session — not something an ssh-spawned remote process can inherit. Assigning an Apple-data task to a remote macOS host over this mechanism will not have FDA; the Apple Data Agent's own export/import pipeline ([operations.md](../../guides/operations.md)) remains the supported path for cross-machine Apple data.

### Hermes route

`ROUTE_HERMES = "hermes"` (`preflight.py`) is a new tag-only route — like `claude_code`/`codex`, preflight's own classifier JSON schema never emits it; `#hermes` in `_apply_tag_overrides` sets it. `HermesExecutor` (`hermes_executor.py`) reuses `hermes_proxy._build_envelope` (persona/turn-context resolution) and `_HermesTurnPersister` (conversation + usage persistence) so a board-assigned Hermes turn's rows are indistinguishable from one that came through `/chat` — three small read accessors (`conversation_id`, `content_text`, `done_seen`) were added to that class for this reuse. Runs synchronously on the worker's tick thread (a bounded HTTP round trip, not a long-lived subprocess — unlike the CLI routes, which run off-tick through the CLI dispatch pool). The card's prompt is `task["description"]` plus `task["notes"]`, if any. On completion the session row stores `conversation_id`; the card's `session.open_url` becomes `/chat?conversation=<id>` — `web/chat/main.js` reads that query param on boot (after the backend restore settles) and opens the thread, purely additive to the SSE contract in [client-surfaces.md](client-surfaces.md).

### Board open (#851)

`POST /api/agents/board/cards/{id}/open` (`api/routes/agent_assignment.py` — a new router sharing the `/api/agents` prefix with `agents.py` rather than appending to that file, so this issue and the concurrent Kanban board UI issue touch different files) requires the card to be in Assigned state: `status == "todo"`, a recognized assignee tag, and no running session (worker-dispatched or a prior interactive open) already against it. `claude`/`codex` spawn the interactive CLI in a terminal — reusing the `cc_resume_cmd`/`codex_resume_cmd` launcher templates, with `{inner_command}` rendered as a fresh `env LIFEOS_TASK_ID=<id> claude|codex "<prompt>"` invocation rather than a `--resume <id>`. `scripts/lifeos-agent-hook.sh` already forwards `$LIFEOS_TASK_ID` as `task_id` on every lifecycle event it posts (#849); `POST /cli-sessions/events` now moves a `task_id`-bearing `session_start`'s card from `todo` to `in_progress` — the one piece #849 didn't need, since nothing produced a task-linked interactive session until this endpoint did. `hermes` has no terminal to spawn — returns `{open_url: "/chat?conversation=<id>"}` once the card has one, else 409.

### Model catalog (#851)

`GET /api/agents/models` (`model_catalog.py`) returns `{engines: {claude, codex, local, hermes}, refreshed_at, stale}`, each engine's list merged with `pricing.PRICING`. Sources: Anthropic via the SDK's own `models.list()` (never a hand-maintained table — that's exactly what went stale in `pricing.py` before #655/#656); Codex via its own `~/.codex/models_cache.json`, falling back to a live OpenAI models list only when `LIFEOS_OPENAI_API_KEY` is set; local via the running llama-server's `/v1/models` (`model_readout._probe_live_model`); Hermes via the last observed turn's model (`model_readout.get_hermes_models`) — never probed, since Hermes can serve a different model per turn. Cached for `LIFEOS_AGENT_MODEL_CATALOG_TTL_SECONDS` (default 24h); a refresh failure (any single engine's fetch raising) falls back to the last successful catalog with `stale: true` rather than 500ing the picker.

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
| `lifeos_agent_spawn` | Create a new agent session (local or claude). Returns `session_id`. Optional `tier` (`haiku`/`sonnet`/`opus`) picks the Claude Code child's CLI model — see [Delegation tier + single-message (#349)](#delegation-tier--single-message-349). |
| `lifeos_agent_send` | Post a message to a child session's queue. Also a lifecycle transition: a direct parent sending to its own COMPLETED `claude_code`/`codex` child with a persisted CLI session id **reopens** it — the message is enqueued as the child's next turn *before* the status flips back to `claimed` (so a dispatch tick can never claim an empty resume prompt), and the spawned-session dispatcher resumes the CLI session via `-r` with full prior context. All other terminal sends still reject. |
| `lifeos_agent_check` | Poll a child's current state. |
| `lifeos_agent_yield_until` | Pause the caller until specific children reach terminal state — preferred over polling (no idle billing). |
| `lifeos_agent_kill` | Terminate a child early. |
| `lifeos_agent_transcript_read` | Read another session's transcript. |
| `lifeos_agent_sessions_list` | List active + recent sessions. |
| `lifeos_agent_user_ask` | Pause and ask the operator a clarifying question via Telegram (reply-threaded). |

Security: lineage checks ensure a session can only message / kill / yield-on its own descendants (rooted at `root_session_id`).

Lineage budgets: every session tracks `root_session_id` + `spawn_depth`. Budget breaches at any descendant cascade-kill the entire lineage. Limits configurable via `LIFEOS_AGENT_MAX_SPAWN_DEPTH`, `LIFEOS_AGENT_MAX_DESCENDANTS_PER_ROOT`, `LIFEOS_AGENT_MAX_CONCURRENT_LOCAL`, `LIFEOS_AGENT_MAX_CONCURRENT_MANAGED`.

### Operator root-spawn (#235)

`lifeos_agent_spawn` is same-lineage only (requires a parent session). Operators start agents on demand — with no backing `#agent` vault task — via `create_operator_session()` (`api/services/agent_worker/operator_spawn.py`), reachable from Telegram (`/agent [local|claude] <task>`) and web chat (the same `/agent` slash command).

- **Routing** follows override-then-preflight: an explicit `local`/`claude` keyword wins; otherwise `run_preflight()` decides. On `ROUTE_ASK` the session parks at `blocked` with `routing='ask'` and the caller sends the engine clarification (the worker resolves it on reply).
- **The API needs consent (#584).** `routing=claude` is the only per-token-billed-to-Anthropic route, so preflight may reach it only when the operator asked: a `#cloud-haiku` / `#cloud-sonnet` tag, or a title naming an engine or model (the classifier's `routing_explicit`, corroborated against the title so a hallucinated flag can't dispatch). A cloud route the classifier *inferred* — rule 4's capability cues — is downgraded to `ROUTE_ASK` and confirmed. (#809: bare `#cloud` is a separate, similarly-gated consent — it dispatches straight to `ROUTE_REMOTE`, the configured remote provider, never Anthropic; a title merely containing "cloud" no longer corroborates an inferred `claude` route either, since #809 dropped it from `_TITLE_NAMES_A_CLOUD_ENGINE`.) The confirmation offers `claude code` / `codex` / `local` / `cloud` (remote provider) / `anthropic` or a Claude model name, and a bare "claude" in the reply resolves to the **CLI**, not the API: with both in play, the subscription reading is the one where a misparse costs nothing.
- **Provenance** is marked with the additive `sessions.origin = 'operator'` column. The worker's `_dispatch_spawned_sessions` skip is relaxed to claim parentless sessions when `origin='operator'`, so they dispatch alongside spawned children without colliding with the top-level `#agent` claim path (which uses NULL origin). The prompt is enqueued as a pending message and drained as the task description on dispatch.
- Operator sessions are root sessions (`parent_session_id=None`), so their terminal notifications surface to the operator and register a replyable follow-up (Phase 1 / #234). Because they have no backing vault task, `_handle_outcome` and `_resume_as_followup` skip the vault mutations (`_complete_task` / `_swap_tag` / `_set_task_status`) for `origin='operator'` — gated on `has_vault_task` — while still sending the notification + follow-up. The prompt is enqueued *before* the session row is created so the worker can never observe a CLAIMED operator session whose prompt hasn't landed. Default budget comes from the `agent_default_*` settings; local concurrency cap of 1 means operator local spawns queue behind running ones.

### Off-tick CLI dispatch (#299, #753)

`claude_code` / `codex` sessions are long-running subprocesses — up to the session's budget wall (14,400s by default) — so their dispatch always runs on a bounded `ThreadPoolExecutor` (`_cli_pool`, sized `2 × agent_max_concurrent_managed`) via `_submit_cli_dispatch`, never inline on the tick thread. This applies to both callers: `_dispatch_spawned_sessions` (spawned children and operator root-spawns) and `_dispatch`'s `ROUTE_CLAUDE_CODE`/`ROUTE_CODEX` branch (top-level `#agent` tasks, #753) — a single delegated child or top-level CLI task can no longer park the poll loop and starve new `#agent` claims, sleeping-session wakes, managed polling, or clarification processing/timeouts. Preflight and the fast blocked/failed/sanity short-circuits still run inline on the tick thread; only the `execute()`/`resume()` subprocess call and everything downstream of its outcome (vault tag swap, Telegram notify) move to the pool, since `_dispatch_claude_code_session`/`_dispatch_codex_session` own outcome handling themselves rather than going through `_handle_outcome`. An `_cli_inflight` set (lock-guarded) prevents a re-scan from re-submitting the same session in the window before its executor flips the row `CLAIMED→RUNNING`; for CLI routes the guard is checked *before* draining pending messages so a skipped re-scan can't discard them. Per-routing concurrency stays bounded at `lifeos_agent_spawn` time (`count_active_by_routing`), independent of dispatch timing. The `local` route stays inline (in-process, GPU-bound, cap 1). `stop()` calls `shutdown(wait=False, cancel_futures=True)`; sessions still running are reconciled by `resume_pending()` on restart. Tests inject a `_SynchronousPool` for deterministic dispatch.

### Earned completion / interrupted CLI sessions (#760)

A CLI subprocess exiting cleanly (`returncode == 0`) or emitting a terminal-looking stream event is **not** proof the agent actually finished its turn — it can hit `--max-turns`, get OOM-killed, or otherwise die mid-thought and still reach the executor's `STATUS_COMPLETED` fallback with a mid-sentence `final_text` and zero notifications sent. Marking that `#agent-completed` hides the interruption from the operator (field case: `sess_099c0b8ca254486f` — final text a 64-char instruction fragment to itself, `notifications_sent: 0`, no PR, unpushed WIP branch — tagged completed anyway).

Both `_dispatch_claude_code_session` and `_dispatch_codex_session` gate their `STATUS_COMPLETED` branch on `completion_signal.has_positive_completion_signal(final_text, notifications_sent)` before treating the outcome as real completion — a **root** session only; a spawned child (`parent_session_id` set) is exempt, same as the empty-result/no-side-effect-tool-use guard `_handle_outcome` applies to the local/managed routes (that guard lives in a different dispatch path — the CLI routes bypass `_handle_outcome` entirely — so this is a parallel, composing check, not a replacement for it). A cheap, deterministic — not LLM — check is earned by any one of:

- at least one `[NOTIFY]` was sent during the run (`ExecutorOutcome.notifications_sent`; Codex has no notify convention and always reports 0, so it falls through to the next two checks);
- the final text references a PR/issue — a `github.com/…/pull|issues/…` URL is the strong signal; a bare `#123` only counts alongside merge/PR-ish phrasing nearby (`PR`, `merged`, `opened`, `closes`, `fixes`, `resolves`), to avoid mistaking a passing issue-number mention for "I opened it";
- the final text reads like a finished summary rather than an instruction fragment: non-empty, above a small length floor, and not trailing off mid-clause (ending in `:`/`,`/`;`/a dash, or on a dangling connective word like "the"/"and"/"to").

Failing all three routes the outcome to `Worker._handle_cli_interrupted`, which parks it rather than either completing or bare-failing it:

- **Resumable** (a `claude_code_session_id` / codex thread id was persisted): the session row moves to `BLOCKED` and an operator message — "Session interrupted mid-work — reply to resume", the last `final_text` as context, and the WIP branch name if discovered (below) — is sent via the id-capturing sender and registered as a `kind='followup'` `pending_questions` row. This reuses the *exact* round-trip a genuine `[CLARIFY]`/`[GOAL]`/plan `BLOCKED` outcome already uses: `_process_clarification_answers` → `_resume_as_followup`, which for `claude_code`/`codex` routing just re-enqueues the reply and flips the session to `CLAIMED` so the next dispatch tick drains it through `resume()` on the persisted CLI id. The vault tag is deliberately left at `#agent-running` (mirroring the CLARIFY/GOAL/PLAN block path, which also doesn't swap it) — only the session row moves.
- **Unresumable** (no CLI session id was ever persisted — `init` never fired — or Telegram delivery of the notice failed, leaving no reply anchor): the session fails instead, with the same interrupted-context message sent via the plain sender and `_reconcile_vault_terminal(FAILED)` run. This is the documented fallback when resume-on-reply genuinely can't apply.

**WIP-branch discovery** (`Worker._discover_wip_branch`) is best-effort and read-only: it scans the session's own past transcript for `claude_code_tool_use` (`payload.input.command`) / `codex_tool_use` (`payload.preview`) events matching `git switch -c <branch>` / `git checkout -b <branch>`, and surfaces the last match. It never runs `git` itself — a miss just omits the branch name from the message.

**Exit metadata.** Both `_exit_metadata` implementations (`ClaudeCodeExecutor`, `CodexExecutor`) attach `{"returncode": ..., "signal": ... (if returncode < 0), "timed_out": bool, "stream_terminal_event_seen": bool}` to the terminal `claude_code_completed`/`codex_completed` transcript event and to `ExecutorOutcome.exit_meta`, which `_handle_cli_interrupted` copies into the new `cli_session_interrupted` event. `stream_terminal_event_seen` is the load-bearing field — True only when a real `result` (claude_code) / `session.completed`/`exec.completed` (codex) event was parsed; False means the returncode==0 fallback fired on stdout just closing, which is exactly the shape of an interrupted run.

New transcript event kinds: `cli_session_interrupted` (the interrupted disposition itself, payload above), `cli_interrupted_prompt_registered` (message ids + WIP branch once the notice is sent), `cli_interrupted_prompt_undelivered` (Telegram delivery failed, falling through to the unresumable-failed path).

Deliberately out of scope for #760: the CLI system prompt's canonical-checkout discipline (the field session also left the shared checkout on its WIP branch, stalling autodeploy) — that's prompt/wrapper text touching live sessions and is tracked separately.

### Delegation tier + single-message (#349)

When an agent delegates to a `claude_code` child, two behaviors keep the cost down and the operator's inbox clean:

- **Tier.** `lifeos_agent_spawn`'s optional `tier` (`haiku` / `sonnet` / `opus`, default `opus`) is persisted on the child's `sessions.claude_code_model` column (additive migration; NULL → CLI default) and threaded into `_build_command` as `--model`, so the worker can run a simple lookup on Haiku instead of Opus. Ignored for non-`claude_code` engines.
- **One operator message.** A spawned child (`parent_session_id` set) stays silent to the operator: `ClaudeCodeExecutor` suppresses live `[NOTIFY]`/heartbeat streaming and instead folds the notify bodies into `final_text` (`_effective_final_text`), and `_dispatch_claude_code_session` skips the terminal Telegram send for children. The child's `final_text` is persisted in its `claude_code_completed` transcript event, where the parent reads it via `_child_final_text` — so the parent's single completion message carries the child's findings. That message is flagged by `_escalation_note` with the engine + tier, e.g. `⤴️ Escalated to Claude Code (haiku)`. Operator `/claude` sessions (no parent) stream and send as before. The `codex` dispatch path applies the same child gate (#429): a codex child's completion neither sends to the operator nor registers a followup anchor, and its `final_text` is persisted in the `codex_completed` event where `_child_final_text` reads it. Failure/budget notices are child-gated too on both CLI paths (#431) — the parent's resume turn carries the child's terminal status header, plus a `reason:` line read from the child's `child_failed_internal` / `child_budget_exceeded_internal` transcript event (#433; written by `_handle_outcome` for local/managed children and by the CLI dispatch tails).

---

## Budget enforcement

Four overlapping layers, executed in this order:

1. **Daily $-cap** — `SpendTracker.can_start_task(estimated)` short-circuits to False when `daily_cap_dollars <= 0` (operator pause). Otherwise blocks new claims when accumulated day spend ≥ cap.
2. **Per-task token / wall caps** — checked at the top of every executor turn (local) or every poll (managed). The **dollar cap (`max_dollars`) is enforced only on the managed/API route** — the only route with marginal per-task cost. The Claude Code and Codex CLI routes are subscription-billed and the local route is free, so none of them enforce a per-task dollar cap (they track cost for `/agents` reporting but never stop on it). Because that exemption is load-bearing, "subscription-billed" is enforced rather than assumed: `ClaudeCodeExecutor._clean_env` strips every `ANTHROPIC_*` and `CLAUDE*` variable from the CLI subprocess (an inherited `ANTHROPIC_API_KEY` takes precedence over the claude.ai login, and would put an uncapped session on the API), and `inter_agent.spawn` rejects `model="claude"` when the lineage's root is a CLI session.
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

1. Sends a Telegram message via `send_message_capture_ids()` (returns the `message_id` of **every** 4096-char chunk).
2. Persists `(session_id, telegram_message_ids, question)` in `pending_questions` — the full chunk list in `sent_message_ids` (JSON), with the first chunk in `sent_message_id`.
3. Swaps the tag to `#agent-blocked` and parks the session.
4. For managed sessions: also calls `driver.kill_session` to stop session-hour billing while waiting.

When the operator replies (using Telegram's native reply feature), the bot's `_maybe_deposit_agent_answer()` hook intercepts the `reply_to_message_id` and calls `SessionStore.deposit_answer()`, which matches a reply landing on **any** chunk (membership in `sent_message_ids`, not just the first). The worker's `_process_clarification_answers()` runs each tick, picks up answered questions, parses the answer (for routing questions: extracts `claude code` / `codex` / `local` / `cloud` from free-text, last mention winning), updates the session, and re-dispatches.

For local sessions, the parent session resumes via the existing pending_messages drain.
For managed sessions, a new remote session is created with the resolved routing.

Clarifications older than `LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS` (default 72h) are abandoned with a Telegram heads-up; the transcript stays preserved.

### Child clarifications (#422 / #428)

Spawned CLI children never enter the Telegram flow above — the operator owns no thread to a child, and a BLOCKED child would strand its yielded parent (which only resumes once every child is terminal). Instead, a child's `[CLARIFY]` is folded into its output as `[needs clarification] …` and the child COMPLETES (`_effective_final_text` / `claude_code_child_clarify_folded`). The parent reads the question in the child's relayed output on resume and answers via `lifeos_agent_send`, which reopens the completed child (see [Inter-agent coordination](#inter-agent-coordination)); the parent then `yield_until`s the child again. Operator `/claude` sessions keep the pause-and-reply behavior: their `[CLARIFY]` goes BLOCKED and waits on a threaded Telegram reply.

### Replyable terminal threads

Every terminal-state notification — `#agent-completed`, `#agent-failed`, and `#agent-budget-exceeded` — registers a follow-up (`kind='followup'`) via `register_completion_followup()`, so a reply reopens the session as a new user turn (`_resume_as_followup()` swaps whichever terminal tag is current back to `#agent-running`). This makes failures and budget cut-offs replyable, not just clean completions.

Targeting a thread on Telegram is **explicit only**: a **native reply** to any chunk of a notification resumes that specific thread (works regardless of age). A plain (non-reply) message is always a fresh chat query — there is no implicit "recent thread" capture, so an unrelated question right after a task finishes is never silently swallowed into the agent thread.

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
- **Recurring (cron) schedule** → one shared note per schedule. The scheduler stamps the handed-off `#agent` task with a `sched-<id>` tag (see [scheduler.md](scheduler.md)); `_schedule_id_from_task` reads it on completion, resolves the schedule's name via `GET /api/scheduler/{id}` for a readable filename `<schedule-slug>-<id>.md` (falling back to `recurring-<id>.md`), and `_recurring_content` prepends this fire above prior runs under a `## YYYY-MM-DD HH:MM` heading — newest first, frontmatter `created` preserved and `updated` bumped.

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
| Card assignment (#851) | `LIFEOS_AGENT_HOSTS`, `LIFEOS_AGENT_SSH_CONNECT_TIMEOUT`, `LIFEOS_AGENT_MODEL_CATALOG_TTL_SECONDS`, `LIFEOS_CODEX_MODELS_CACHE_PATH`, `LIFEOS_OPENAI_API_KEY` |

---

## Related Documents

- [ADR-008: Managed Agents Cloud Routing](../../adr/008-managed-agents-cloud-routing.md) — Decision record for the local-vs-cloud executor split
- [ADR-018: API Spend Requires Operator Consent](../../adr/018-api-spend-requires-consent.md) — Why an inferred cloud route asks, and why the CLI subprocess carries no API credential
- [Agent Worker — Product](../product/agent-worker.md) — What `#agent` does, consumer view
- [Agent Worker — Setup](../../guides/agent-worker-setup.md) — Operator setup walkthrough
- [Agent Viz — Technical](agent-viz.md) — `/agents` page that reads SessionStore + TranscriptStore here
- [Task Management](../product/task-management.md) — How `#agent` tasks sit alongside regular tasks
- [Human Queue](../../guides/human-queue.md) — Cards the worker's poll tick auto-resolves via `done_when`
- [MCP Tools](../product/mcp-tools.md) — Standard MCP catalog including `lifeos_agent_*` family
- [API Reference](../product/api-reference.md) — Task endpoints the worker uses (`/api/tasks/{id}/swap-tag`, `/api/tasks/{id}/complete`)
- [Architecture](architecture.md) — Where the worker fits in the broader code structure
- [Observability](observability.md) — Tracing, perf, and logging patterns the worker uses
- [Client Surfaces](client-surfaces.md) — The `/chat` SSE contract the `?conversation=` deep link (#851) is additive to
