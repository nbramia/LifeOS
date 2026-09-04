# Agent Activity Visualization — Technical

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-04

Engineering view of the `/agents` page — endpoint shapes, ingest paths, status inference, layout, and security boundaries. For the consumer view see [product/agent-viz.md](../product/agent-viz.md).

---

## Table of Contents

1. [Architecture overview](#architecture-overview)
2. [Endpoints](#endpoints)
3. [Kanban board](#kanban-board)
4. [Snapshot shape](#snapshot-shape)
5. [LifeOS agent ingest](#lifeos-agent-ingest)
6. [Claude Code ingest](#claude-code-ingest)
7. [Status inference (Claude Code)](#status-inference-claude-code)
8. [Live process detection](#live-process-detection)
9. [Cross-machine CLI session registration](#cross-machine-cli-session-registration)
10. [Snapshot caching](#snapshot-caching)
11. [D3 force-graph](#d3-force-graph)
12. [Side-panel SSE](#side-panel-sse)
13. [Operator kill](#operator-kill)
14. [Claude Code resume + Go To](#claude-code-resume--go-to)
15. [Worker resilience](#worker-resilience)
16. [Security boundaries](#security-boundaries)
17. [Related Documents](#related-documents)

---

## Architecture overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│  web/agents.html (shell + tabs)                                           │
│    web/agents/board.js  — Kanban board, drag/drop, drawer                 │
│    web/agents/graph.js  — D3 force-simulation graph (Graph tab, lazy-init)│
│    web/agents/panel.js  — shared session-detail panel (both tabs)         │
│    web/agents/assignment.js — model/effort/host pickers (board drawer)    │
└─────────────────────────────┬────────────────────────────────────────────┘
                              │ HTTP + SSE
                              ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       api/routes/agents.py                                │
│   /api/agents/snapshot · /stream · /sessions/{id}/events · /stream       │
│                       · /kill · /resume                                   │
│   /api/agents/board · /board/stream · /board/cards/{id}/lane · /accept   │
│   /api/agents/pending-questions · /pending-questions/{id}/answer         │
└────┬───────────────────────────────────┬─────────────────────────────────┘
     │ LifeOS agent worker               │ Claude Code CLI
     ▼                                   ▼
┌─────────────────────────┐    ┌──────────────────────────────────────────┐
│ SessionStore            │    │ api/services/claude_code/                │
│ TranscriptStore         │    │ session_ingest.py                        │
│ (SQLite + JSONL)        │    │   - discover_sessions()                  │
│                         │    │   - parse_session()                      │
│ Owned by the worker     │    │   - live_claude_cwd_counts() via psutil  │
│ process; read here.     │    │   - build_snapshot() (cached 30s)        │
└─────────────────────────┘    └──────────────────────────────────────────┘
```

The board joins two more read paths not pictured above: `TaskManager` (the
vault task store, `api/services/task_manager.py`) and `SchedulerStore`
(`api/services/scheduler_store.py`) — both already-owned singletons the
board route reads from directly, same pattern as `SessionStore`/`TranscriptStore`.

The route file imports the worker's `SessionStore` and `TranscriptStore` directly (read-only) and unions their output with the Claude Code adapter's normalized shape. No second worker process; the API server reads the same SQLite + JSONL files the worker writes.

---

## Endpoints

All under `/api/agents`. Local-network only, with one deliberate exception: `POST /cli-sessions/events` (#849) is meant to be reachable over Tailscale, gated by a bearer token instead of an IP check — see [Security boundaries](#security-boundaries). The kill, resume, focus, and pane-bind endpoints must not be exposed via the public MCP HTTP transport.

| Method + Path | Purpose |
|---|---|
| `GET /snapshot` | One-shot full snapshot. Use for first-paint or when the SSE stream drops. |
| `GET /stream` | SSE: emits a full snapshot every 2s. Tolerant to per-tick failures (yields an `error` event and continues). |
| `GET /sessions/{id}/events?limit=N` | Last N transcript events (default 200, max 2000). Dispatches by `cc:` prefix to the Claude Code ingest path. |
| `GET /sessions/{id}/stream?backfill=N` | Per-session SSE: backfill last N events (default 50, max 500), then live-tail. Closes cleanly when the session reaches terminal status (LifeOS) or after 5 min idle (Claude Code). |
| `POST /sessions/{id}/kill` | Operator kill — body `{reason: ""}`. LifeOS sessions only. Cascades to descendants in the subtree. |
| `PUT /sessions/{id}/label` | Set or clear an operator-pinned manual label — body `{label: ""}`. Non-empty pins a custom node name that overrides the auto-derived label and AI summary label everywhere it's shown; empty clears it. Durable in `data/agent_viz_label_overrides.db` (in-process cache, lazy-loaded). Works for both LifeOS and `cc:` sessions. |
| `POST /sessions/{id}/resume` | Resume a Claude Code session — body `{extra_env: {}}`. `cc:`-prefixed ids only. Gated on `LIFEOS_CC_RESUME_ENABLED`. Spawns a WezTerm tab via `wezterm cli spawn`, captures the pane id from stdout, and stores `session_id → pane_id` in `data/cc_wezterm.db` so Focus can target it later. 409 if the `cli_sessions` row for this id records a `host` other than this API's own (see [Cross-machine CLI session registration](#cross-machine-cli-session-registration)). |
| `POST /sessions/{id}/focus` | Activate the WezTerm pane for this session (Go To). `cc:`-prefixed ids only. Gated on `LIFEOS_CC_RESUME_ENABLED`. Resolves the pane id from the cached mapping first, then falls back to an FD probe (lsof + /proc + wezterm cli list) so it works for sessions never opened via Resume. 404 only when both the cache *and* the probe come up empty; 410 when the pane existed but is gone and no replacement is found. Same 409-for-remote-host rule as `/resume`. |
| `POST /cc-pane-bind` | Localhost-only endpoint called by the Claude Code SessionStart hook. Body `{session_id, pane_id, cwd}`. Upserts the mapping in `cc_wezterm.db` so Go To can target newly-started `claude` invocations without a probe. 403 from non-loopback callers. |
| `POST /cx-pane-bind` | Codex sibling of `/cc-pane-bind`. Same body shape and localhost-only gate; keys `cx:`-prefixed rows in the same store. |
| `POST /cli-sessions/events` | Cross-machine session registration (#849) — see [Cross-machine CLI session registration](#cross-machine-cli-session-registration). Bearer-token gated, reachable from any host. Body `{engine, event, session_id, host, cwd?, transcript_path?, branch?, model?, prompt_preview?, task_id?, pane_id?, wezterm_pid?}`. |

Heartbeats: per-session SSE emits a `:heartbeat\n\n` comment every 15s when there's no new event, so dropped connections surface quickly through the browser's `EventSource` retry.

`GET /sessions/{id}/stream` dispatches by prefix: `cc:` to the Claude Code ingest path, `cx:` to the Codex ingest path (`_stream_codex_session`, mirroring `_stream_claude_code_session` — same 1s poll loop, same 5-minute idle close, no DB status to read), everything else to the LifeOS transcript store. Before this the `cx:` branch was missing and fell through to the LifeOS path, which 400'd on the prefix — opening a Codex session's panel never streamed.

---

## Kanban board

`api/services/agent_board.py` holds every pure decision the board makes — lane derivation, lane-move planning, and the scheduler-entry Scheduled/Done split — with no I/O. `api/routes/agents.py` does the reading and writing; it never re-derives a rule the service module already owns. Unit tests in `tests/test_agent_board.py` cover one case per row of the lane table plus the priority-ordering edge cases (e.g. an `agent-completed` tag beats a terminal status, so a worker-finished task still surfaces in Review instead of silently landing in Done).

### Endpoints

| Method + Path | Purpose |
|---|---|
| `GET /board` | Full view model, always built fresh — `run_in_threadpool(_build_board)` on every call, never served from the stream's cache (see below). `_build_board()` reads `TaskManager.list_tasks()`, joins each task's linked session (matched by `task_id` against the same `_build_snapshot()` sessions list `/snapshot` returns) and any open pending question (matched by `task_id`), derives its lane via `agent_board.derive_lane`, and separately buckets every `SchedulerStore` entry into `scheduled` or `done` via `agent_board.is_schedule_active`. |
| `GET /board/stream` | SSE. Ticks every `_BOARD_STREAM_INTERVAL = 0.5s`, reads the board through the shared `_board_cache` (TTL `_BOARD_CACHE_TTL = 0.25s`), and only emits a `board` event when a JSON-serialized signature of `lanes` differs from the last sent tick — an idle board doesn't push empty ticks to a connected client. |
| `PUT /board/cards/{id}/lane` | Body `{lane, assignee?}`. Reads the task, calls `agent_board.plan_lane_move`, and applies the resulting `status`/`tags` patch via one `TaskManager.update` call (or raises the planned error and writes nothing). 400 for an unknown or undroppable (`review`/`scheduled`) lane; 409 for a worker-owned card (`agent-running`/`agent-blocked` tag present) dropped on `in_progress` or `done`; 409 for an agent-engine-assigned-but-unclaimed card dropped on `in_progress`; 409 for a pending Review card (`agent-completed` without `accepted`) dropped on `in_progress` or `human_queue` (dropping it on `done` still doubles as accept — see below). A 200 response's `lane` is the card's actual landed lane, which for the tags-only `assigned`/`unassigned` targets may differ from the requested lane if a higher-priority signal (e.g. Human queue) still applies — the frontend toasts when this happens (see [Frontend module split](#frontend-module-split)). |
| `POST /board/cards/{id}/accept` | Adds the `accepted` tag (see `ACCEPTED_TAG`) and sets `status="done"` if either isn't already true; a no-op write-wise (no `TaskManager.update` call at all) when both already hold, so the endpoint is genuinely idempotent — not just safe to call twice. |
| `GET /pending-questions` | `session_store.list_open_questions()` — unanswered, unprocessed, not-timed-out `pending_questions` rows whose `kind` is `clarification` or `goal_approval`; `followup` (completion notices) and `status_anchor` (routing plumbing) rows are excluded so a Review card never renders a fake pending-question badge. |
| `POST /pending-questions/{id}/answer` | `session_store.deposit_answer_by_id(question_id, answer)` — writes `answer`/`answered_at` on that exact row id, then invalidates `_board_cache` so the stream's next tick reflects it immediately. |

### Why the board SSE isn't event-driven

The issue's target is "reflects an external vault edit within ~3 seconds," and the task watcher's own debounce (`api/services/task_watcher.py`, `_DEBOUNCE_SECONDS = 2.0`) already spends most of that budget before `TaskManager`'s in-memory index even updates. Wiring a real push (an `asyncio.Event` set from the watcher's background thread via `loop.call_soon_threadsafe`, fanned out to every open SSE connection) would work but adds real cross-thread state for a three-second target that a fast poll already meets comfortably: both `TaskManager` and `SchedulerStore` serve `list_tasks()`/`list_all()` from an in-memory dict, so rebuilding the board costs a dict walk, not disk or DB I/O. `tests/test_agents_board_watch.py` proves the actual (not sped-up) production debounce lands well inside 3 seconds by starting a real `TaskWatcher` against a temp vault, writing an external edit, and polling the stream's own cached read (`_get_board_cached()`) until the change shows up.

### Board cache

`_board_cache` is a module-level `(built_at, board)` tuple used ONLY by `GET /board/stream`'s own tick — never by `GET /board`, which always calls `_build_board()` fresh. The cache exists to de-duplicate simultaneous stream connections within the same instant (multiple open board tabs shouldn't each pay the full build cost on every tick), not to skip rebuilds between ticks: its TTL (`_BOARD_CACHE_TTL = 0.25s`) is far shorter than the tick interval (`_BOARD_STREAM_INTERVAL = 0.5s`). `_invalidate_board_cache()` drops it immediately after every board write — lane-move, accept, and pending-question answer — so a stream tick right after a write never serves pre-write data. Worst-case latency from an external vault edit to every open board tab reflecting it is the sum of three independent legs: the task watcher's debounce (2.0s) + the cache TTL (0.25s, only matters if a tick lands mid-window) + the stream's own tick interval (0.5s) = 2.75s, inside the 3s budget. A direct `GET /board` skips the last two legs entirely since it never reads the cache.

### Pending-question answer path

`SessionStore.deposit_answer_by_id` (new in #850, alongside the pre-existing `deposit_answer` keyed by Telegram message id and `deposit_answer_by_session_id` keyed by session) sets exactly the columns `deposit_answer` sets — `answer` and `answered_at` on the matched `pending_questions` row, gated on `answered_at IS NULL AND timed_out = 0 AND kind != 'status_anchor'`. `worker.py::_process_clarification_answers` drains any row with `answered_at IS NOT NULL AND processed = 0` on its next tick regardless of which `deposit_answer*` method wrote it — the board's answer endpoint needed no change to `worker.py`.

### Frontend module split

`web/agents.html` used to be one 2,600-line file (inline CSS + a single IIFE covering the graph, side panel, filters, and search). It's now a shell (CSS + tab markup) plus four ES modules under `web/agents/`, served the same way `web/chat/`'s split (#360) is — `<script type="module">` tags resolving against the existing `/static` mount, no bundler:

- **`panel.js`** — the shared session-detail panel: header render, inline label edit, backfill + live SSE transcript tail, LLM summary fetch, and the kill/resume/focus actions, plus the small cross-cutting helpers (`routingLabel`, `escapeHtml`, `showToast`, `prettyPayload`, …) both other modules import. Exports a `SessionPanel` class constructed with a `container` element rather than hardcoded ids, so the Graph tab's side panel and the Board tab's drawer can each hold an independent instance without DOM id collisions (both tabs' markup stays mounted; only one is visible via `[hidden]`).
- **`graph.js`** — the D3 force-simulation graph, filters, chips, and search, moved with the rendering/simulation/interaction code unchanged; only the panel-specific calls were swapped for a `SessionPanel` instance. Exports `initGraph()`, called once, lazily, the first time the operator opens the Graph tab — so loading the board (the default view) doesn't also open a second SSE connection (`/api/agents/stream`) nobody is watching.
- **`board.js`** — the Kanban board: fetch + SSE, lane rendering, filters, and the drawer. Exports `initBoard()`, called immediately on page load.
- **`assignment.js`** — the card-assignment pickers (engine/model/effort/host) that `board.js` mounts into the drawer; writes `model`/`effort`/`host` (+ `assigned_by`) through `PUT /api/tasks/{id}` and reads `GET /api/agents/models` for the model options and `GET /api/agents/hosts` for the host picker's options. A standalone module wired into `board.js`. Saves are serialized through a single in-flight promise chain rather than fired independently — a queued save reads the controls' CURRENT values only once its turn arrives, then PUTs those, so at most one save is ever outstanding; a rejected save reverts each picker to exactly what the last *successful* save sent, never to a live control value some other save's resolution happened to catch mid-flight.

**Drag and drop is pointer-based, not the native HTML5 Drag and Drop API.** `draggable="true"` + `dragstart`/`dragover`/`drop` only fires through the browser's OS-level drag gesture — synthetic mouse events (Playwright's included) can't reliably trigger it, which would have made the server-free browser test (`tests/test_agents_board_ui_browser.py`) unable to drive a drag at all. `board.js` instead tracks `mousedown` → `mousemove` (past a 4px threshold, to distinguish a drag from a click) → `mouseup`, rendering a floating ghost card and using `document.elementFromPoint` to resolve the lane under the cursor. The trailing `click` event that a `mouseup` also fires is suppressed via a `suppressNextClick` flag set only when a real drag happened, so the same gesture never both moves a card and opens its drawer.

A successful drop always re-fetches the board (`fetchBoard()`) rather than mutating the DOM optimistically — the server is the single source of truth for a card's lane, and a rejected move (400/409/500) leaves the card exactly where the last successful fetch put it, with a toast surfacing the server's error text.

`board.js` also owns three pieces of client-side state. The lane-selection filter persists to `localStorage` under the key `lifeos.agents.board.lanes`, defaulting to every lane except Done; a missing or malformed (non-JSON, non-array) stored value falls back to that default, and an unknown lane id inside an otherwise-valid array is dropped individually, only falling back to the default when nothing valid survives, while a deliberately-emptied selection (`[]`) round-trips as empty rather than being treated as malformed. The drawer's click-outside-close guard requires the `mousedown`, `mouseup`, **and** `click` to all target the backdrop element itself — the same event-target plumbing the drag/drop paragraph above relies on — because a single `click` listener alone would also close the drawer on a text selection or scrollbar drag that starts inside the drawer and ends on the backdrop. And the New-card composer's create is two calls, not one: `POST /api/tasks` creates the task, then, for any lane other than Unassigned, `PUT /api/agents/board/cards/{id}/lane` moves it there; if that second call fails, the card still exists at its tag-derived resting lane, an error toast reports the failure, and the board re-fetches to reflect what's actually true server-side.

---

## Snapshot shape

```json
{
  "sessions": [{ /* see below */ }],
  "edges":    [{ "from": "<parent_session_id>", "to": "<child_session_id>", "type": "spawn" }],
  "generated_at": 1716777600
}
```

One session row, unified shape (both sources):

| Field | Type | Notes |
|---|---|---|
| `session_id` | str | LifeOS: bare uuid. Claude Code: `cc:<uuid>`. Subagent: `cc:<parent>:agent:<tool_use_id>`. |
| `task_id` | str \| null | LifeOS: task id from the worker. Claude Code / Codex: `null` for a locally scanned session — a scanned session's `raw_session_id`/tool_use_id is not a LifeOS task link — overlaid with a real LifeOS task id only when a hook-registered `cli_sessions` row supplies one (`_apply_cli_session_to_dict`). |
| `status` | str | See [Status inference (Claude Code)](#status-inference-claude-code) and product spec. |
| `routing` | str | `local`, `claude`, `ask`, `remote`, `hermes`, `claude_code`, or `codex`. `code` is a legacy value — `session_store.py`'s schema migration runs `UPDATE sessions SET routing = 'claude_code' WHERE routing = 'code'` on open, so no persisted row carries it any more; `_model_label_for_routing`'s `code` arm is defensive, not live. |
| `parent_session_id` | str \| null | Spawn parent. Used by graph edges. |
| `root_session_id` | str \| null | Top of the spawn tree. Used by the kill subtree walk. |
| `spawn_depth` | int | 0 for root, 1+ for children. |
| `yield_waiting_for` | list[str] | LifeOS sessions paused on children — child session ids. |
| `managed_agent_session_id` | str \| null | Anthropic Managed Agents session id (cloud LifeOS sessions only). |
| `started_at`, `last_activity_at` | int | Unix epoch seconds. |
| `total_input_tokens`, `total_output_tokens` | int | Net tokens. |
| `total_cache_creation_tokens`, `total_cache_read_tokens` | int | Anthropic cache accounting. |
| `total_dollars` | float | Cost so far. Cache-aware via `cost_for(model, input, output, cache_creation, cache_read)`. |
| `total_active_seconds` | float | LifeOS wall-time accounting. Always 0 for Claude Code (no wall meter). |
| `expected_output` | str \| null | LifeOS preflight classification — `text` / `file` / `external_action` / `structured`. |
| `label` | str | Display name. Cached per session id. On a synthetic remote-CLI row (`_cli_session_to_dict`) this is `prompt_preview` when non-empty, else the session id. |
| `custom_label` | str \| null | Operator-pinned manual label (via `PUT /sessions/{id}/label`). When set, the frontend uses it as the node name in preference to every other source. |
| `model_label` | str | Short badge — `Local` / `Haiku` / `Sonnet` / `Opus` / `Claude Code` / `Codex` / `Waiting on you` (routing `ask`) / the configured `remote_llm_label` (routing `remote`) / `Hermes` (routing `hermes`). See [LifeOS agent ingest](#lifeos-agent-ingest). |
| `model` | str \| null | LifeOS only. Board-assignment model id from `Session.model` — the operator's model *picker* value, not what actually ran. Does not feed the Hermes badge — see `_model_label_for_routing` below. |
| `effort` | str \| null | LifeOS only. Board-assignment effort value from `Session.effort`. |
| `conversation_id` | str \| null | LifeOS only, `hermes` routing. `Session.conversation_id`. |
| `bot` | str \| null | LifeOS only. Telegram bot persona that owns this session's operator-facing messages (`Session.bot`; `null` = primary). |
| `origin` | str \| null | LifeOS only. `Session.origin` — e.g. `operator`, `hermes`. |
| `last_event_kind` | str | Most recent transcript event kind — drives the side-panel "last" tooltip. |
| `tool_call_count`, `error_count` | int | Summed across the transcript tail (last 100 events). |
| `source` | str | `lifeos_agent`, `claude_code`, or `codex`. Frontend uses this to pick the shape. |
| `status_inferred` | bool | `false` means status came from a confirmed live process or from a `cli_sessions` registration event; `true` means it was guessed from transcript mtime. |
| `project_key`, `decoded_cwd` | str | Claude Code only. `project_key` is the dir name under `~/.claude/projects/`; `decoded_cwd` is the original cwd. |
| `is_subagent` | bool | True for synthetic Task/Agent tool-use children. |
| `host` | str | The machine this session is running on (#849). Always present — LifeOS and locally-scanned CLI rows get the API's own host (`api_host_name()`); a row that also has (or only has) a `cli_sessions` registration gets that row's `host` instead. |
| `branch` | str \| null | Git branch of the session's cwd, from the most recent registration event. Only present on a session with at least one `cli_sessions` row. |
| `prompt_preview` | str \| null | Most recent user prompt, truncated to 200 chars, from the most recent `user_prompt_submit` registration event. Only present on a session with at least one `cli_sessions` row. |

---

## LifeOS agent ingest

Read paths in `api/routes/agents.py`:

- `_session_to_dict(s, transcript)` — projects a `Session` row to the snapshot shape. Defends against the optional `total_cache_*_tokens` attrs being absent on old rows.
- `_label_for_session(s, events)` — walks the first five transcript events looking for `description` / `task_description` / `prompt`; falls back to the session id. Result cached per session id (capped at 500 entries).
- `_summarize_events(events)` — counts tool calls and errors across the last 100 events. `_is_error_kind` matches the literal set `{failed, managed_failed, child_failed_internal, killed, cascade_killed}` plus any kind ending in `_failed` or `_error` (future-proof).
- `_model_label_for_routing(routing)` takes only a routing value — `local` → `Local`; `ask` → `Waiting on you` (a session parked waiting on the operator has no model running, so it must never render a Claude-tier guess); `remote` → `settings.remote_llm_label` (falls back to `Remote` if unset or settings import fails); `hermes` → `Hermes`, plain, always; `claude_code`/`code` → `Claude Code`; `codex` → `Codex`; otherwise derives the badge from `settings.agent_managed_model`, falling back to `Claude` if the model name doesn't match any known family. `Session.model` (the board's model *picker* value) is exposed separately as the `model` field above but does not feed this badge — `HermesExecutor` never reads or writes it, and a conversation-rooted Hermes session (the dominant path) passes no `model` at all. `Hermes` always stays plain rather than appending a `· <model>` suffix: `api/services/model_readout.py`'s `_last_observed_hermes_chat_model()` is a single process-wide "last observed" value written by *any* Hermes turn (an agent-worker session and `/chat`'s Hermes proxy alike), not a per-session attribution, so a badge built from it could retroactively show a finished session whatever model an unrelated, later Hermes turn reported — exactly the cross-surface borrowing `model_readout.py`'s own docstring rejects as dishonest. The readout is correctly scoped on the `/models`/`/api/health` surface it was built for; it just doesn't feed this per-node badge.

Per snapshot tick the route calls `session_store.list_sessions(limit=200)` (newest-first) and emits one edge per session with a `parent_session_id`. Subagents that exist only inside the transcript (no SessionStore row) do not appear in this path — they show up via the Claude Code ingest below.

---

## Claude Code ingest

`api/services/claude_code/session_ingest.py` is a read-only adapter that translates Claude Code's per-message JSONL schema into the LifeOS shape. Public surface used by the route:

| Function | Purpose |
|---|---|
| `discover_sessions(projects_dir, lookback_days)` | Walks `<projects_dir>/<project_key>/*.jsonl`, returns one `SessionMeta` per file modified within the window, newest-first. |
| `parse_session(meta)` | Reads the jsonl, sums usage, extracts label / subagents / last event kind, infers status. |
| `build_snapshot(...)` | Combines discovery + parse + process detection + subagent expansion. Cached 30s per `(projects_dir, lookback_days)`. |
| `read_normalized_events(session_id)` | For the `/sessions/{id}/events` and `/stream` endpoints. |
| `validate_session_id(session_id)` | Path-traversal guard. Rejects `/`, `\`, `..`, anything outside `[A-Za-z0-9_\-:]`. Strips the `cc:` prefix and returns the bare id. |

Event normalization (`normalize_event`) maps three Claude Code message types into LifeOS events:

- **assistant** → `assistant_text`, `tool_call`, `extended_thinking`. Usage block is summed into the session totals (input, output, cache_creation @ 1.25× the input rate, cache_read @ 0.10×).
- **user** → `user_message` (operator's text input) or `tool_result` (model's tool-call response). Tool results are truncated to 240 chars.
- **system** → `system_message`. Permission-mode changes etc. are dropped as noise.

**Session label precedence.** `parse_session` picks a Claude Code session's display label, most human-intentful first: the user's explicit `/rename` (the CLI's `custom-title` record → `customTitle`), then the CLI's auto-generated `ai-title` (`aiTitle`), then the most recent user prompt (truncated to 60), then the working-directory basename, then the raw session id. The `custom-title` / `ai-title` records are dropped from the normalized *event* stream as noise but read here for labeling; the latest record of each kind wins.

Subagent spawns are detected when an assistant message contains a `tool_use` block with `name in {"Agent", "Task"}`. Each such tool-use becomes a synthetic session node — `subagent_session_dict(parent, subagent)` — with id `<parent>:agent:<tool_use_id>` and a spawn edge from the parent. These nodes don't have their own jsonl; clicking one currently loads the parent's transcript (filtered-by-tool-use-id is future work).

**`task_id`.** `to_session_dict` and `subagent_session_dict` both emit `task_id: None` — `SessionMeta` carries no LifeOS task link of its own (it only has `raw_session_id`, the Claude Code UUID, and subagents only have a `tool_use_id`), and either leaking into `task_id` poisoned `_build_board`'s `sessions_by_task` join in `api/routes/agents.py`, which keys purely on truthiness. A locally scanned session gets a real `task_id` only when overlaid afterwards by `_apply_cli_session_to_dict` from a hook-registered `cli_sessions` row that named one (see [Snapshot union](#snapshot-union) below).

---

## Status inference (Claude Code)

`_infer_status(mtime, last_assistant_had_pending_tool, last_event_was_error, has_live_process)` — returns `(status, inferred)`:

| Signal | Status | `inferred` |
|---|---|---|
| Live `claude` process matches the project cwd | `running` | `False` (authoritative) |
| `mtime` within the last 10 min | `running` | `True` |
| `mtime` within the last 24h | `inactive` | `True` |
| Older + last event was an error | `failed` | `True` |
| Older + pending tool in flight | `inactive` | `True` |
| Otherwise | `completed` | `True` |
| `cli_sessions` row present, latest event `session_start` or `stop` | `idle` | `False` |
| `cli_sessions` row present, latest event `user_prompt_submit` | `running` | `False` |
| `cli_sessions` row present, latest event `session_end` | `ended` | `False` |

When a session has a `cli_sessions` registration (see [Cross-machine CLI session registration](#cross-machine-cli-session-registration)), that row's event-driven status replaces this file-age inference entirely — `status_inferred` is `False` regardless of what the mtime-based signals above would have guessed.

The 10-minute `running` window is wider than the wall-clock-precise definition because Claude Code appends in bursts during a single turn and a tight 60-second threshold flipped sessions to `inactive` mid-pause. The `(inferred)` hint in the side panel lets the operator distinguish authoritative from heuristic status reads.

---

## Live process detection

`live_claude_cwd_counts(now)` enumerates `/proc` via `psutil` and returns a `{cwd: count}` map of running `claude` processes. The matcher is strict:

- `proc.name() == "claude"` **or** `basename(proc.exe()) == "claude"`.
- Wrapper processes are excluded explicitly: `vt`, `vibetunnel`, `node`, `bash`, `sh`, `zsh`. (An earlier loose argv match pulled `vt claude` and `vibetunnel fwd claude` in and inflated the running count.)
- Versioned shipping binaries with numeric basenames (e.g. `claude/versions/2.1.152`) are still caught because the exe path contains `claude/`.

Failure modes degrade gracefully — `psutil` missing or a transient `AccessDenied` returns an empty dict, and the rest of the snapshot falls back to mtime alone.

The scan result is cached per-process for 5 seconds so one snapshot tick across many sessions enumerates `/proc` once, not once per session.

In `build_snapshot`, the second pass uses this map to **per-cwd promote the top-N most-recently-modified sessions to authoritative `running`** — where N is the live process count for that cwd. This is the key fix for the earlier bug where one live session in a project flipped every historical jsonl in that project to `running`.

---

## Cross-machine CLI session registration

Local process detection and the transcript scan both stop at this machine's filesystem — they cannot see a Claude Code or Codex session running on a laptop or a second box. Issue #849 adds an independent, push-based path for that: `scripts/lifeos-agent-hook.sh` posts a lifecycle event from wherever the CLI is running to `POST /api/agents/cli-sessions/events`, and the API keeps a small per-session record that the snapshot builder unions onto whatever the transcript scan already found.

### Storage: `cli_sessions`

Table lives in the same SQLite file `SessionStore` already owns (`api/services/agent_worker/session_store.py`, `CREATE TABLE IF NOT EXISTS` — no migration needed, it's a new table). One row per session, keyed the same way the snapshot union already keys transcript-derived rows:

| Column | Notes |
|---|---|
| `session_id` | PK. `cc:<uuid>` or `cx:<uuid>` — `CLI_ENGINE_PREFIXES = {"claude_code": "cc", "codex": "cx"}` maps the event's `engine` field to the prefix. |
| `engine` | `claude_code` or `codex`. |
| `host` | Hostname the hook posted from, verbatim (no validation against a known-hosts list). |
| `cwd`, `transcript_path`, `branch`, `model` | Optional; a `None` on an incoming event leaves the stored value alone rather than blanking it — a `stop` event with no `branch` field doesn't erase what `session_start` recorded. |
| `status` | `idle` \| `running` \| `ended` — see the status machine below. |
| `prompt_preview` | Truncated to `CLI_PROMPT_PREVIEW_MAX = 200` chars, set only by `user_prompt_submit` events. |
| `task_id` | Opaque string from the CLI's `$LIFEOS_TASK_ID` env var. Stored and exposed verbatim — **never validated against the task store** (issue #853, built in parallel; this issue takes no dependency on its schema or code). |
| `pane_id`, `wezterm_pid` | Only meaningful when the hook ran inside a WezTerm pane; `None` otherwise. |
| `started_at`, `last_event_at`, `ended_at` | Unix epoch seconds. `ended_at` is set on `session_end` and cleared on a subsequent `session_start` (a resumed session sends `session_start` again — see below). |

`SessionStore.record_cli_session_event(engine, event, session_id, host, ...)` applies one event: **status machine** — `session_start` → `idle`, `user_prompt_submit` → `running` (+ prompt preview), `stop` → `idle`, `session_end` → `ended`. A row is created on whichever event is first seen for a session id — not necessarily `session_start` — so a hook installed mid-session, or a lost `session_start` post, still registers the session rather than silently never appearing.

### Endpoint: `POST /cli-sessions/events`

`_check_agent_hook_auth(request)` mirrors `hermes_proxy._check_hermes_inbound_auth`: empty `LIFEOS_AGENT_HOOK_TOKEN` → 503 (endpoint disabled by default — a fresh clone accepts no unauthenticated writes from the tailnet); missing/wrong bearer → 401; `hmac.compare_digest` for the comparison. `engine` not in `CLI_ENGINE_PREFIXES` or `event` not in `CLI_SESSION_EVENTS` → 422.

On a successful call, if the event's `host` equals this API's own host (`api_host_name()`) **and** it carries a `pane_id`, the handler also upserts into `CCWezTermStore` — the same table `/cc-pane-bind` and `/cx-pane-bind` write to — so Go To keeps working for a session registered this way instead of via the pane-bind hook specifically. A remote host's `pane_id` has nowhere local to activate, so it's stored on the `cli_sessions` row only, never mirrored into the pane store.

### Snapshot union

`_build_snapshot()` reads every `cli_sessions` row into a dict keyed by `session_id` before running the Claude Code and Codex transcript scans. Each transcript-derived row `.pop()`s its match out of that dict:

- **Match found** (`_apply_cli_session_to_dict`) — the transcript row's `status`/`status_inferred` are overwritten from the registration (event-driven status always wins over the transcript's file-age guess); `host`, `branch`, `prompt_preview` are copied in; `task_id` is copied in only if the event supplied one. Token/dollar fields are left as the transcript computed them — the hook posts no usage data.
- **No match** (`_cli_session_to_dict`) — a fully synthetic row: `host`/`branch`/`prompt_preview`/`task_id` from the `cli_sessions` row, `source` = the engine name, `status_inferred = False` always (there's no inference here, only events), zero token/dollar fields, `decoded_cwd` from the row's `cwd`. `label` prefers `prompt_preview` when non-empty, falling back to the session id. `model_label` comes from the matching ingest module's `model_label()` helper for `claude_code`/`codex`; an engine value outside that pair (defensive only — the route only ever writes `claude_code`/`codex`) title-cases the engine name instead of guessing a Claude tier.

Whatever's left in the dict after both scans ran — a remote host, or (rarely) a local hook post that raced ahead of the transcript scan's 30s cache — is bounded to the same recency window each engine's transcript scan already applies (`claude_code_lookback_days` / `codex_lookback_days`) before becoming a synthetic row, so a stale registration doesn't linger in the snapshot forever.

Worker (`lifeos_agent`) rows use `s.host or api_host_name()` directly in `_session_to_dict` — `Session.host` is the board-assignment field a worker was dispatched to run on; unset (legacy rows, or no board assignment) falls back to the machine hosting this API process. They never need a `cli_sessions` lookup.

### Focus / Resume and remote hosts

`_check_session_host_or_409(session_id)` looks up the id's `cli_sessions` row. A session with no row at all (never registered, or registered before this feature existed) is unaffected — it falls through to the pre-existing cache/probe resolution unchanged. When a row exists and its `host` differs from `api_host_name()` (#851): a host name present in `settings.agent_hosts` resolves to its ssh target, which both `/focus` and `/resume` then use instead of 409ing — see [Card assignment](agent-worker.md#card-assignment-851) for the mechanism (ssh-wrapped launcher, cwd sourced from the `cli_sessions` row since a remote session's transcript file isn't on this host's filesystem, and `/focus`'s fallback to running the same launcher `/resume` does, since there's no cross-host pane registry to activate an existing pane against). A host name NOT in `settings.agent_hosts` still 409s, with the recorded host in `detail` — the honest answer for an operator-config gap, not a silent no-op or a misleading 404.

### Hook script and installer

`scripts/lifeos-agent-hook.sh` is a single portable script (bash 3.2-compatible for macOS) that serves every hook event — engine and event name are passed as argv (`lifeos-agent-hook.sh claude_code session_start`). It reads the hook's JSON stdin payload via `jq`, adds the hostname (`hostname` with any domain suffix stripped), the cwd's git branch (`git -C "$cwd" rev-parse --abbrev-ref HEAD`), and `$LIFEOS_TASK_ID`, and POSTs to `/cli-sessions/events` with `curl --max-time 2`. It sources a small env file (`~/.config/lifeos/agent-hook.env`, override via `$LIFEOS_AGENT_HOOK_ENV`) for `LIFEOS_API_URL` / `LIFEOS_AGENT_HOOK_TOKEN` — values already in the environment take precedence over the file. Every non-fatal condition (missing `jq`/`curl`, empty stdin, no token configured, API unreachable) exits 0 silently and writes nothing to stdout, so it can never block or corrupt the CLI's own hook processing. Pane fields are included only when `$WEZTERM_PANE` is set — unlike `claude-session-pane.sh`, running outside WezTerm is a normal case, not a silent no-op, since registration doesn't depend on a pane to exist.

`scripts/install-agent-hooks.sh` appends one entry per event to `~/.claude/settings.json` and `~/.codex/hooks.json` (overridable via `LIFEOS_CLAUDE_SETTINGS` / `LIFEOS_CODEX_HOOKS` for testing), identifying a prior install by the substring `lifeos-agent-hook.sh` in an existing entry's `command` so re-running it is a no-op. Every other tool's entries — Orca, atuin, a legacy `claude-session-pane.sh` / `codex-session-pane.sh` entry — are left untouched. Writes via temp file + `mv`. The installed command wraps the absolute path of the script in *this* checkout so a moved or deleted checkout degrades to a no-op instead of an error: `bash -c 's="<path>"; [ -x "$s" ] && exec "$s" <engine> <event>; exit 0'`. It never writes the token itself — it prints setup instructions for the operator.

---

## Snapshot caching

Two caches:

1. **`_snapshot_cache`** in `session_ingest.py` — keyed by `(projects_dir, lookback_days)`, TTL 30s. The whole `(sessions, edges)` tuple is memoized so a single SSE tick across many connected clients doesn't re-walk the projects dir. Bypass with `cache_ttl=0` (used by tests).

2. **`_label_cache`** in `agents.py` — keyed by session id, capped at 500 entries. Labels are derived from the first 5 transcript events and don't change once a non-fallback label has been resolved.

Both caches are lock-guarded so concurrent FastAPI threads can't see partial entries. Invalidation is on-demand via `invalidate_cache()` / `invalidate_process_cache()` (used by tests and reachable from a future admin endpoint if needed).

A third cache, `agent_viz_summary.py`'s in-process + disk-backed short-label cache, treats a session as terminal (cacheable forever) using `TERMINAL_STATUSES` from `session_store.py` **unioned with** `{"ended", "inactive"}` — those two are CLI-only statuses (`cli_sessions` events / the transcript scan's file-age guess) that don't exist in the worker's own terminal set; without the union, a CLI session's fallback label would never cache and `agent_viz_summary_prefetch.py`'s background loop would retry it every tick forever. The same caching applies to a summarizer call that raises (not just the deterministic empty-transcript fallback): `_cache_if_terminal(session_id, last_activity_at, status, result, *, is_error_fallback=False)` is the single helper both paths call, and it only writes for a terminal status — a live session's fallback stays uncached so it can still pick up real content later. The prefetcher's own dispatch (`_summarize_one`) mirrors the `/summary` route's three-way `cc:`/`cx:`/else split, so a Codex session's events resolve through the `cx:` branch rather than `TranscriptStore().read()` (which returns `[]` for a `cx:` id).

`is_error_fallback=True` marks an entry cached from a summarizer call that *raised* (a timeout, an LLM queue backup, a JSON-parse failure) rather than the deterministic empty-transcript fallback — that path is deliberately kept **in-process only**, bounded by `_FAILURE_FALLBACK_TTL_SECONDS` (10 minutes), and is never written to disk. A transient failure isn't a genuine dead end the way "no transcript content" is; the same session's real transcript is still sitting there and a retry could succeed, so an exception fallback must not permanently poison a terminal session's summary for the life of the install the way a disk write effectively would (`prune_disk_cache` has no scheduled caller). The TTL matches `agent_viz_summary_prefetch._FAILURE_BACKOFF_TICKS` (30 × 20s = 10 min) so the prefetcher's own retry-after-cooldown isn't silently absorbed by a cache hit here. One consequence: AC 7's literal wording ("the fallback is never re-called for that (session, last_activity_at)") is knowingly relaxed for this path — after the TTL, a deterministically-failing input **is** re-summarized, roughly every 10 minutes, for as long as the failure persists. That's the correct trade for the reason above, and `_FAILURE_BACKOFF_TICKS` bounds the retry rate; it just isn't literally "never".

`_is_frozen(status)` is a strict subset of "terminal": terminal minus `"inactive"`. `"inactive"` is the transcript scan's file-age guess for a Claude Code session idle more than 30 minutes, not a real terminal event — the session can resume, and `web/agents/panel.js`'s own `TERMINAL` set agrees that only `"ended"` (not `"idle"`) is truly done. `_is_fresh_enough` uses `_is_frozen` to decide whether a cached entry can be served "regardless of new activity": a frozen status grants that leniency unconditionally for a *real* summary, but withholds it when the cached content is only the deterministic `_NO_CONTENT_SUMMARY` sentinel — re-deriving a no-content fallback costs no LLM call, and a frozen status guaranteeing "no more content will ever arrive" is exactly the kind of claim a later real summary could prove wrong. An *error* fallback never reaches `_is_fresh_enough` at all — it's checked directly against `_FAILURE_FALLBACK_TTL_SECONDS` from the in-process cache, since it's never written to disk.

---

## D3 force-graph

`web/agents.html` uses D3 v7 force-simulation, mirroring the patterns in `/crm/graph`. Replaced a vis-network 9.x implementation in #173 — the migration eliminated the jitter-without-converging bug and recovered the recency-x layout.

```js
const VIEW_W = 1600, VIEW_H = 1100;
const simulation = d3.forceSimulation()
  .force('link',     d3.forceLink().id(d => d.session_id).distance(80).strength(0.04))
  .force('charge',   d3.forceManyBody().strength(-220).distanceMax(600))
  .force('center-y', d3.forceY(VIEW_H / 2).strength(0.12))
  .force('recency-x',d3.forceX(recencyTargetX).strength(0.18))
  .force('collide',  d3.forceCollide().radius(d => nodeRadius(d) + 14).strength(0.9))
  .alphaDecay(0.025).alphaMin(0.001).velocityDecay(0.45);

setTimeout(() => simulation.alpha(0).stop(), 8000);  // 8s convergence backstop
```

| Tuning | Reason |
|---|---|
| `link.strength(0.04)` | Spawn edges are *informational*, not load-bearing — weak so they don't yank parent + child off the recency-x rail. |
| `charge -220, distanceMax(600)` | Strong-ish repulsion within 600 vbox units. Caps the influence radius so far-apart clusters don't push each other. |
| `forceX(recencyTargetX) strength 0.18` | Soft pull along the recency rail, newer to the right. Rail *width* scales with the visible-node count via `recencyRailSpan()` — `0` (centered) for one visible node, `~0.30·W` for two, growing logarithmically to `0.84·W` at 32+. This way filtered-down sets re-center instead of getting pinned to their absolute time-position. |
| `forceY(VIEW_H/2) strength 0.12` | Mild vertical centering — without it, nodes drift off-canvas; too strong and the collide force can't separate them. |
| `forceCollide radius = nodeRadius + 14, strength 0.9` | Hard guarantee that token-based-sized nodes never overlap. |
| `velocityDecay 0.45`, `alphaDecay 0.025` | Settles in ~6s; the 8s `alpha(0).stop()` is a backstop in case extreme node counts prevent natural convergence. |

Node shape varies by routing (`nodeShapeTag`) — `rect` (Claude Code CLI, `rx`/`ry` rounded; `source`/`routing` = `claude_code`), `polygon` (local-agent diamond; `routing` local/unset), `circle` (cloud-agent dot; routed to Claude). Subagents follow the same routing rule rather than getting a dedicated shape. The simulation tick re-positions whichever element is bound to each datum; size and color are re-applied on every snapshot tick via `applyShapeAttrs`. The actively-writing pulse (white border, 1.4s ease-in-out) is a CSS keyframe applied via the `.pulsing` class — far simpler than the previous vis-network `DataSet.update` polling.

`nodeLabel(d)` picks the rendered label, first non-empty of: `custom_label` → `short_label` → `label` → `prompt_preview` → `model_label` → `routingLabel(d.routing)` → `session_id.slice(0, 8)`. **Both** `short_label` and `label` are checked and skipped, independently, whenever either is not a real label but the raw id the row fell back to — equal, by trimmed string equality, to `session_id`, to `session_id` with a `cc:`/`cx:` prefix stripped, or to `task_id`. On the server side, `agent_viz_summary.py`'s `_fallback_label` independently refuses to hand back a raw identifier as `short_label` in the first place — it returns `""` for a whitespace-free input (a session/task id has no word boundaries of its own) or for an input that's genuinely non-empty but tokenizes to zero words, which includes any non-Latin (CJK/Cyrillic/Greek/Arabic) or emoji-only title, not only a raw id; `"Untitled"` is reserved for a genuinely empty/whitespace-only input, where there's no real title in `label` to fall through to. Never emits a literal `'?'`. In practice the `session_id.slice(0, 8)` tail is unreachable: `routingLabel()` returns `'Claude'` for any unrecognized routing and never an empty string, so it always wins before the final fallback is tried — the fallback remains as a safety net, not something a node actually shows.

`web/agents/graph.js`'s search-results dropdown renders every title through the same guard as the node: `sessionDisplayName` is `nodeLabel`, so the dropdown's sort order and fallback title share one precedence chain, and `searchResultTitle(s, field)` shows the matched field's own value only when `isRawIdValue` accepts it as a real label — otherwise the display name. `buildSearchResults`' matching and tiers do not depend on either.

The `viewBox` is `1600 × 1100` with `preserveAspectRatio="xMidYMid meet"` and `overflow: visible` so collision-pushed nodes don't get clipped at the edges.

### Interaction model

Both link and node layers live inside a single `<g class="viewport">` whose `transform` is mutated by `d3.zoom` (scaleExtent 0.2–5). Because `d3.pointer` returns viewBox-space coordinates when the target carries a `viewBox`, pan tracks the cursor 1:1 without explicit screen↔viewBox compensation.

Per-node `d3.drag` sets `fx`/`fy` on the bound datum so positions persist after drop. The drag handler's `mousedown` stops propagation, so node-drags don't also pan the canvas. Drag pins persist across snapshot ticks but are cleared on filter change (`releasePins()`) so the new visible set reflows.

`renderGraph` keeps `_lastVisibleIdsKey` (the sorted-and-joined session-id set of the previous tick) and only fires `simulation.alpha(0.3).restart()` when that key changes. Same-set snapshot ticks therefore leave settled nodes alone — the periodic jitter that previously fell out of the every-2s SSE refresh is gone. Filter changes additionally reset the pan/zoom transform via `svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity)` so the operator's prior zoom doesn't strand the freshly-filtered set in empty space.

Selection state is tracked in `selectedSessionId` and re-applied on every render plus on `openPanel`/`closePanel` via `applySelectionStyles()`. The helper computes the 1-hop neighbor set from the link layer's bound data and toggles `.selected` (5px white border, overrides `.pulsing`), `.related` (3px translucent white), and `.dimmed` (0.28 opacity) classes on nodes / labels / edges. Clicking the SVG background (target === svg root) calls `closePanel()`; clicking an already-selected node toggles it off.

### Transcript event rendering

`prettyPayload(payload)` is a field-aware formatter shared by backfill + live tail. It recognizes routing decisions (`routing` + `routing_reason`), assistant text (with a `(no text — called tools)` placeholder when `text === ""` alongside non-empty `tool_uses`), tool-call pills (`Name(input_keys)`), labeled text fields (`question`, `answer`, `prompt`, `reason`, `ambiguity`, `sane_reason`, `description`, `label`), compact `budget` and `usage` lines, scalar badges (`model`, `task_id`, `expected_output`, `speed`, `service_tier`, `source`, parent/child ids), and a `pp-extra` tail for unrecognized scalars. Noisy fields (`iterations`, `inference_geo`, `server_tool_use`, the nested `cache_creation` ephemeral buckets, zero `thinking_chars`) are suppressed. Click-to-expand reveals the raw JSON in a sibling `<pre class="payload-raw">` for diagnostic inspection.

---

## Side-panel SSE

Per-session streams (`/sessions/{id}/stream`) live-tail the transcript:

- **LifeOS** — `transcript_store.read(session_id)` (re-reads the on-disk JSONL each 1s tick). Closes when `session_store.get_by_session_id(...)` reports a terminal status; the terminal check is rate-limited to every 5s to avoid hammering SQLite.
- **Claude Code** — same 1s read loop against the jsonl. Closes after 5 minutes of no new events (Claude Code has no DB status to read). Heartbeats do **not** postpone the idle close — only real new events do — so a sleeping session releases its SSE slot reliably.

Backfill delivers the most recent N events oldest-first, then live updates stream as they arrive. The frontend prepends each event so the final visual order is newest-on-top. Backfill events are tagged so the frontend can mute the actively-writing pulse for them.

---

## Operator kill

```
POST /api/agents/sessions/{id}/kill   body: {"reason": "..."}
```

1. Resolve the target via `session_store.get_by_session_id(id)`. 404 if missing. Idempotent on terminal status (returns `{killed: [], reason: "already <status>"}`).
2. Walk the subtree via `_collect_subtree(session_store, target)` — BFS from the target through `parent_session_id`, **not** from `root_session_id`. Non-root targets only take down their own descendants, leaving unrelated peers under the same root alone.
3. For each session in the subtree (target first, then descendants):
   - Skip already-terminal entries.
   - Emit `operator_killed` (target) or `cascade_killed` (descendants) to the transcript.
   - Call `api.services.agent_worker.inter_agent.teardown_session(...)` to actually mark the session terminal in the store and tear down the managed remote if one exists.
4. Managed-Agents teardown uses a `ManagedAgentsDriver` instance constructed lazily from `settings.anthropic_api_key`. If the key isn't set, kill degrades to local-only and the worker's next managed poll reconciles the remote side.
5. (#851) For a session whose `host` is set, `teardown_session` signals the process over ssh (`ssh <target> kill -- -<pgid>`, the `<pgid>` recorded from the remote executor's spawn — see [Card assignment](agent-worker.md#card-assignment-851)) instead of the local `os.killpg` path. A missing `remote_pgid` or an unregistered host degrades to a DB-only kill, the same as a missing local pid event does.

Response: `{killed: [session_ids], failures: [{session_id, error}], reason: <input>}`.

The endpoint must not be exposed via Tailscale Funnel or the public MCP HTTP transport. The boundary lives in the route layer; see [Security boundaries](#security-boundaries).

---

## Claude Code resume + Go To

```
POST /api/agents/sessions/{id}/resume   body: {"extra_env": {...}}
POST /api/agents/sessions/{id}/focus    body: (none)
POST /api/agents/cc-pane-bind           body: {session_id, pane_id, cwd}   (localhost only)
```

All three opt-in via `LIFEOS_CC_RESUME_ENABLED` (except `/cc-pane-bind`, which is gated by client IP only — `127.0.0.1` / `::1`). Resume spawns a new WezTerm tab and records the new pane id; Go To (`/focus`) revisits the pane for a session, falling back to an FD probe when no mapping is cached; `/cc-pane-bind` is the SessionStart hook entry point that pre-populates the mapping at `claude` startup. Both Resume and Go To also check `_check_session_host_or_409` first (#849) — a session whose `cli_sessions` row names a different host than this API's own 409s immediately, before any local wezterm work runs; see [Cross-machine CLI session registration](#cross-machine-cli-session-registration).

### Resume

1. Validate the session id (must start with `cc:`, must pass `validate_session_id`). Strip a `:agent:...` suffix if present — operator clicks on a subagent node mean "resume the parent terminal".
2. Look up the meta via `discover_sessions(...)` with a widened 365-day lookback (resume is fine on old sessions). 404 if not found or no `decoded_cwd`.
3. Render `LIFEOS_CC_RESUME_INNER_CMD` with `{session_id}` / `{cwd}` first, then render `LIFEOS_CC_RESUME_CMD` with the same substitutions plus `{session_id_url}` / `{cwd_url}` (URL-encoded for legacy URI-scheme launchers) and `{inner_command}` (which expands to the rendered inner command's argv tokens, picked apart by `shlex.split`).
4. `shlex.split(rendered)` — no `shell=True`, ever.
5. Build the env: inherit `os.environ`, then layer `LIFEOS_CC_RESUME_ENV_FILE` (key=value lines pinning `DISPLAY` / `XAUTHORITY` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS`), then merge `body.extra_env` last.
6. Push the rendered inner command to the system clipboard via `wl-copy` (Wayland) or `xclip` (X11) as a backup — redundant for the default WezTerm path (which runs the inner command directly) but useful if the operator has overridden `LIFEOS_CC_RESUME_CMD` to a legacy launcher that opens an empty terminal.
7. `subprocess.Popen(argv, cwd=decoded_cwd, env=env, stdout=PIPE, stderr=PIPE, start_new_session=True)`. `proc.communicate(timeout=1.5)` to drain stdout — that's where `wezterm cli spawn` prints the new pane id. rc=0 with no integer on stdout is still success (operator may have configured a non-WezTerm launcher); rc≠0 surfaces stderr as 500. A `TimeoutExpired` keeps the launcher alive and returns `pane_id: null` — for launchers that BECOME the terminal.
8. If stdout's first token parses as an int, persist `session_id → pane_id` via `CCWezTermStore.upsert` (SQLite at `data/cc_wezterm.db`).

Response: `{spawned: true, pid, pane_id, command, cwd, inner_command, clipboard_copied}`. The frontend uses `pane_id` to decide whether the Focus button can target this session; if `null`, Focus will respond 404.

### Go To (`/focus`)

1. Validate `cc:` prefix and the `LIFEOS_CC_RESUME_ENABLED` gate.
2. **Cache lookup.** `CCWezTermStore.get(session_id)` — populated by Resume *and* by the SessionStart hook → `/cc-pane-bind` write path. Each row carries a `wezterm_pid` recorded at write time (the most-recently-modified `$XDG_RUNTIME_DIR/wezterm/gui-sock-<pid>`). If that pid is no longer in the live set, the mapping is discarded and the probe runs as if the cache had missed — pane ids reset when wezterm-gui restarts, so a pre-restart `pane_id=5` would otherwise silently activate an unrelated session's pane in the new wezterm. `wezterm_pid=0` (pre-#257 rows or writers that couldn't determine the live pid) is also treated as stale. Multi-mux caveat: the cache accepts *any* live wezterm pid, but `WEZTERM_UNIX_SOCKET` (set by `_resume_env`) targets the most-recently-modified mux. If two wezterm-gui processes are running and the cache was written under the non-primary one, activate-pane fails because that mux doesn't own the pane — the existing 410-then-reprobe path self-heals via a fresh FD probe against the currently-targeted mux.
3. **Probe fallback (cache miss or boot-id stale).** Resolve the session's `transcript_path` via `discover_sessions(...)`, then call `cc_pane_locate.locate_pane_for_transcript(jsonl_path)`:
   - `lsof -t -- <jsonl_path>` → PIDs holding the file open.
   - For each PID, read `/proc/<pid>/fd/0` and keep entries that resolve to `/dev/pts/N` (interactive `claude` processes have fd 0 attached to their controlling pts).
   - `wezterm cli list --format json` → match `tty_name` to the holder's pts; first match wins. Wezterm's JSON output does not expose pane.pid in any supported version, but `tty_name` is reliable.
   - On hit, upsert the mapping so subsequent calls are O(1).
   - All subprocess calls are timeout-bounded (lsof 2s, wezterm cli 2s) and any failure (missing binary, malformed JSON, no holders) returns `None` rather than raising.
4. **Activate.** `subprocess.run(["wezterm", "cli", "activate-pane", "--pane-id", str(pane_id)], capture_output=True, timeout=3.0)`.
5. **Stale-mapping re-probe.** If activate-pane returns rc≠0 on a *cached* mapping, the pane has likely been closed. Delete the mapping, re-run the probe once; if the second probe finds a new pane, retry activate. Only after both attempts fail does the endpoint return 410. A freshly-probed mapping that fails to activate skips straight to 410 (no second probe — we just generated this pane id).
6. Best-effort `notify-send --urgency=critical` so a hidden WezTerm window pulses the dock icon — GNOME Wayland disallows cross-client window raise, so this is the strongest attention hint we can issue from outside the focused client.

Response: `{focused: true, pane_id, cwd}`.

404 means neither the cache nor the probe surfaced a pane (session not running, non-wezterm terminal, hook not installed). 410 means a pane was identified at some point but is now gone and no replacement could be found.

### `/cc-pane-bind` (SessionStart hook entry point)

1. Reject any request whose `request.client.host` is not in `{"127.0.0.1", "::1"}` with 403.
2. `validate_session_id(body.session_id)` — strips any `cc:` prefix and rejects path-traversal characters; 400 on failure.
3. Re-prefix unconditionally (`storage_id = f"cc:{bare}"`) so the keying matches `/resume`'s upsert convention.
4. `CCWezTermStore.upsert(storage_id, body.pane_id, body.cwd or "", wezterm_pid=_current_wezterm_pid(xdg))`. `pane_id` is validated by pydantic (`ge=0`); `wezterm_pid` captures the live wezterm-gui pid so the focus path can invalidate after a restart.

The hook script (`scripts/claude-session-pane.sh`) is invoked by Claude Code's SessionStart hook. It reads the standard SessionStart JSON payload (`{session_id, cwd, transcript_path, source}`) from stdin, picks up `$WEZTERM_PANE` from the env, and POSTs to this endpoint. No-ops gracefully if any of those are missing (non-wezterm terminal, `jq`/`curl` not installed, server unreachable) — never blocks `claude` startup.

---

## Worker resilience

The agent worker process (`lifeos-agent-worker.service`) wraps the executors that the viz observes. Its systemd unit (`config/systemd/lifeos-agent-worker.service`) is tied to `lifeos-api`:

```ini
Requires=lifeos-api.service
BindsTo=lifeos-api.service
PartOf=lifeos-api.service
StartLimitIntervalSec=300
StartLimitBurst=5

Restart=always
RestartSec=10
```

- `Requires=` cascades a stop when the API stops.
- `BindsTo=` ties lifecycle: if the API unit goes into failed state, so does the worker.
- `PartOf=` adds the reverse — when the API restarts (post-commit hook fires whenever `api/` or `config/` files change), the worker restarts with it. Before this, the post-commit restart left the worker stopped indefinitely.
- `Restart=always` keeps the worker up through unhandled exceptions and OOM kills.
- `StartLimit*` is the circuit breaker — more than 5 crashes in 5 minutes pauses restarts and the operator has to intervene. (Note: `StartLimit*` belongs in `[Unit]` in modern systemd; in `[Service]` it's silently ignored.)

---

## Security boundaries

The threat model: the LifeOS MCP HTTP transport is publicly accessible via Tailscale Funnel and is the obvious place an external agent or compromised credential could hit LifeOS endpoints. `/cli-sessions/events` is the one endpoint in this file deliberately reachable over Tailscale rather than local-network-only — it's gated by `LIFEOS_AGENT_HOOK_TOKEN` instead of an IP check, disabled entirely (503) until an operator sets one, and `hmac.compare_digest` avoids a timing side-channel on the comparison. It carries no more authority than "register a session and its metadata" — it cannot kill, resume, or focus anything, and the `host` field it accepts is trusted as-given rather than verified: a bearer-token holder can name any host, so a fabricated session can appear to run somewhere it doesn't. The one place this matters is the pane-store mirror: `pane_id`/`wezterm_pid` are written into `cc_wezterm_store` — the table `/focus` reads to pick a real WezTerm pane — only when the reported `host` matches this API's own AND the request itself arrived from loopback (the same IP check `/cc-pane-bind` and `/cx-pane-bind` use). A remote or spoofed-host event still records its metadata and status on the `cli_sessions` row, but never touches the shared pane store, so it cannot redirect Go To for a real local session. `/kill`, `/resume`, `/focus`, and the board's own write surface — `PUT /board/cards/{id}/lane`, `POST /board/cards/{id}/accept`, and `POST /pending-questions/{id}/answer` — sit on the same footing:

- Live under `/api/agents/*` — the MCP transport never proxies this prefix.
- Are not registered as MCP tools — so they cannot be invoked through the MCP layer even if an attacker has a bearer token.
- Kill calls into worker primitives that already have an audit trail (every kill emits a transcript event).
- Resume runs a configured launcher via `shlex.split` only — no `shell=True`. Template substitutions are URL-encoded where they go into URI strings. The rendered `{inner_command}` is split into individual argv tokens before reaching the launcher, so a malicious inner command cannot smuggle shell metacharacters.
- Focus calls `wezterm cli activate-pane` with a fixed argv (no template) using the pane id from the local SQLite mapping. The store is only writeable from the same process (no cross-machine exposure), and pane ids are integers — there is no path for an external caller to inject arbitrary argv. The FD-probe fallback never reads attacker-controlled data: `lsof` is invoked with the transcript path that LifeOS itself derived from `discover_sessions`, and `/proc/<pid>/fd/0` is read as a symlink target whose filtering keeps only `/dev/pts/N` paths.
- `/cc-pane-bind` is bound to loopback by IP check (`127.0.0.1` / `::1`); the public MCP transport runs on the same host but a different prefix and would never route to it. The accepted body is constrained: `session_id` runs through `validate_session_id` (rejects path traversal), `pane_id` must be a non-negative int, `cwd` is opaque text.
- `/pending-questions/{id}/answer` deliberately drops the `bot` scoping `deposit_answer` has (`session_store.py::deposit_answer_by_id`, above). Bot scoping exists to disambiguate Telegram's multi-bot inbound channel — which reply belongs to which persona's chat — not because the operator lacks authority over a question; the board is a single local surface with strictly less authority than the pre-existing, equally ungated `POST /spawn` and `POST /threads/{id}/reply`, so an unscoped write here adds no new exposure.

Per-source guarantees:

- Claude Code ingest is **read-only**. No code path under `api/services/claude_code/` opens a jsonl with write/append intent; `validate_session_id` rejects path-traversal attempts before any filesystem read.
- LifeOS agent ingest reads `SessionStore` (SQLite) and `TranscriptStore` (JSONL). Both are owned by the worker process and exposed read-only here.
- Transcript content can include personal data (emails, vault paths). Payload previews are truncated to 240 chars in the snapshot summary; full payloads only appear in the per-session SSE on operator click.

---

## Related Documents

- [ADR-011: External Agent Ingest](../../adr/011-external-agent-ingest.md) — Read-only adapter pattern this spec implements
- [Agent Viz — Product](../product/agent-viz.md) — Consumer view: filters, chips, status semantics, operator controls, the board's lanes and drawer
- [Agent Worker — Technical](agent-worker.md) — Sessions, transcripts, kill primitives, inter-agent coordination
- [Agent Worker — Product](../product/agent-worker.md) — `#agent` task lifecycle, Telegram interactions
- [API Reference](../product/api-reference.md) — `POST /api/agents/cli-sessions/events` and other agent endpoint contracts
- [Architecture](architecture.md) — Where the route + adapter fit in the broader code structure
- [Observability](observability.md) — Adjacent traces / health surfaces
- [Task Management — Technical](task-management.md) — `TaskManager`, the store the board's cards are read from
- [Scheduler — Technical](scheduler.md) — `SchedulerStore`, the store the Scheduled column is read from
