# Agent Activity Visualization — Technical

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-08

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
10. [Remote transcript mirror](#remote-transcript-mirror)
11. [Snapshot caching](#snapshot-caching)
12. [Delegation timeline](#delegation-timeline)
13. [Card metadata and cross-tab linking](#card-metadata-and-cross-tab-linking)
14. [Side-panel SSE](#side-panel-sse)
15. [Operator kill](#operator-kill)
16. [Claude Code resume + Go To](#claude-code-resume--go-to)
17. [Worker resilience](#worker-resilience)
18. [Security boundaries](#security-boundaries)
19. [Related Documents](#related-documents)

---

## Architecture overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│  web/agents.html (shell + tabs)                                           │
│    web/agents/board.js  — Kanban board, drag/drop, drawer                 │
│    web/agents/graph.js  — D3 delegation timeline (Graph tab, lazy-init)   │
│    web/agents/graph_encoding.js — pure node encodings (label, shape, size)│
│    web/agents/panel.js  — shared session-detail panel (both tabs)         │
│    web/agents/assignment.js — model/effort/host pickers (board drawer)    │
│    web/agents/lanes.js  — shared lane table + colour palette              │
│    web/agents/linking.js — shared filters + cross-tab focus/tab bus       │
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
| `GET /sessions/{id}/events?limit=N` | Last N transcript events (default 200, max 2000). Dispatches by `cc:` prefix to the Claude Code ingest path. Falls back to each mirrored host's copy of the transcript when the local scan finds nothing. |
| `GET /sessions/{id}/stream?backfill=N` | Per-session SSE: backfill last N events (default 50, max 500), then live-tail. Closes cleanly when the session reaches terminal status (LifeOS) or after 5 min idle (Claude Code). Same local-then-mirrored fallback as `/events`. |
| `POST /sessions/{id}/kill` | Operator kill — body `{reason: ""}`. LifeOS sessions only. Cascades to descendants in the subtree. |
| `PUT /sessions/{id}/label` | Set or clear an operator-pinned manual label — body `{label: ""}`. Non-empty pins a custom node name that overrides the auto-derived label and AI summary label everywhere it's shown, except where it is itself the row's raw id (`session_id`, the prefix-stripped id, or `task_id`), which the graph node and search dropdown skip; empty clears it. Durable in `data/agent_viz_label_overrides.db` (in-process cache, lazy-loaded). Works for both LifeOS and `cc:` sessions. |
| `POST /sessions/{id}/resume` | Resume a Claude Code or Codex session — body `{extra_env: {}, target_host: null}`. `cc:`/`cx:`-prefixed ids only. Gated on `LIFEOS_CC_RESUME_ENABLED` / `LIFEOS_CODEX_RESUME_ENABLED`. Spawns a WezTerm tab via `wezterm cli spawn`, captures the pane id from stdout, and stores `session_id → pane_id` in `data/cc_wezterm.db` so Focus can target it later. `target_host` ("resume here") overrides the launch target: unset/blank falls through to `_check_session_host_or_409` — 409 if the `cli_sessions` row for this id records a `host` other than this API's own and that host isn't registered (see [Cross-machine CLI session registration](#cross-machine-cli-session-registration)); set to this API's own host name or a `LIFEOS_AGENT_HOSTS` entry launches there instead, regardless of the session's recorded host; set to anything else 400s with `detail = {error, command}` — `command` is the exact resume command, cwd-prefixed, for the operator to copy and run themselves. |
| `POST /sessions/{id}/focus` | Activate the WezTerm pane for this session (Go To). `cc:`/`cx:`-prefixed ids. Gated on `LIFEOS_CC_RESUME_ENABLED` for `cc:` ids, `LIFEOS_CODEX_RESUME_ENABLED` for `cx:` ids. Resolves the pane id from the cached mapping first, then falls back to an FD probe (lsof + /proc + wezterm cli list) so it works for sessions never opened via Resume. 404 only when both the cache *and* the probe come up empty; 410 when the pane existed but is gone and no replacement is found. Same `target_host` override and 409/400 rules as `/resume`. |
| `POST /cc-pane-bind` | Localhost-only endpoint called by the Claude Code SessionStart hook. Body `{session_id, pane_id, cwd}`. Upserts the mapping in `cc_wezterm.db` so Go To can target newly-started `claude` invocations without a probe. 403 from non-loopback callers. |
| `POST /cx-pane-bind` | Codex sibling of `/cc-pane-bind`. Same body shape and localhost-only gate; keys `cx:`-prefixed rows in the same store. |
| `POST /cli-sessions/events` | Cross-machine session registration (#849) — see [Cross-machine CLI session registration](#cross-machine-cli-session-registration). Bearer-token gated, reachable from any host. Body `{engine, event, session_id, host, cwd?, transcript_path?, branch?, model?, prompt_preview?, task_id?, pane_id?, wezterm_pid?}`. |

Heartbeats: per-session SSE emits a `:heartbeat\n\n` comment every 15s when there's no new event, so dropped connections surface quickly through the browser's `EventSource` retry.

`GET /sessions/{id}/stream` dispatches by prefix: `cc:` to the Claude Code ingest path, `cx:` to the Codex ingest path (`_stream_codex_session`, mirroring `_stream_claude_code_session` — same 1s poll loop, same 5-minute idle close, no DB status to read), everything else to the LifeOS transcript store. Before this the `cx:` branch was missing and fell through to the LifeOS path, which 400'd on the prefix — opening a Codex session's panel never streamed.

---

## Kanban board

`api/services/agent_board.py` holds every pure decision the board makes — lane derivation, lane-move planning, the shared card-action decision function, and the scheduler-entry Scheduled/Done split — with no I/O. `api/routes/agents.py` does the reading and writing; it never re-derives a rule the service module already owns. Unit tests in `tests/test_agent_board.py` cover one case per row of the lane table plus the priority-ordering edge cases (e.g. an `agent-completed` tag beats a terminal status, so a worker-finished task still surfaces in Review instead of silently landing in Done), plus an exhaustive (assignee × claim-state × action) decision table for `evaluate_card_action`.

### Endpoints

| Method + Path | Purpose |
|---|---|
| `GET /board` | Full view model, always built fresh — `run_in_threadpool(_build_board)` on every call, never served from the stream's cache (see below). `_build_board()` reads `TaskManager.list_tasks()`, joins each task's linked session (matched by `task_id` against the same `_build_snapshot()` sessions list `/snapshot` returns) and any open pending question (matched by `task_id`), derives its lane via `agent_board.derive_lane`, computes its `policy` block (see below), and separately buckets every `SchedulerStore` entry into `scheduled` or `done` via `agent_board.is_schedule_active`. |
| `GET /board/stream` | SSE. Ticks every `_BOARD_STREAM_INTERVAL = 0.5s`, reads the board through the shared `_board_cache` (TTL `_BOARD_CACHE_TTL = 0.25s`), and only emits a `board` event when a JSON-serialized signature of `lanes` differs from the last sent tick — an idle board doesn't push empty ticks to a connected client. |
| `PUT /board/cards/{id}/lane` | Body `{lane, assignee?}`. Reads the task, looks up whether a live session actually backs it (see `has_live_session` below), calls `agent_board.plan_lane_move` (which itself calls `evaluate_card_action` first — see below), and applies the resulting `status`/`tags` patch via one `TaskManager.update` call (or raises the planned error and writes nothing). 400 for an unknown or undroppable (`review`/`scheduled`) lane; 409 for a claimed card dropped on any lane; 409 for an agent-engine-assigned-but-unclaimed card dropped on `in_progress`; 409 for the same unclaimed agent-owned card dropped on `human_queue` or `done` ("agent-owned cards are managed by the agent"); 409 for a pending Review card (`agent-completed` without `accepted`) dropped on `in_progress` or `human_queue` (dropping it on `done` still doubles as accept — see below). A 200 response's `lane` is the card's actual landed lane, which for the tags-only `assigned`/`unassigned` targets may differ from the requested lane if a higher-priority signal (e.g. Human queue) still applies — the frontend toasts when this happens (see [Frontend module split](#frontend-module-split)). |
| `POST /board/cards/{id}/accept` | Adds the `accepted` tag (see `ACCEPTED_TAG`) and sets `status="done"` if either isn't already true; a no-op write-wise (no `TaskManager.update` call at all) when both already hold, so the endpoint is genuinely idempotent — not just safe to call twice. Successful responses include an opaque `undo_token` etag for the matching toast action. |
| `POST /board/cards/{id}/undo-accept` | Body `{token?}`. Removes the `accepted` tag from an accepted card, preserving unrelated tags and fields and ensuring `agent-completed` remains present so the card returns to Review; a supplied token must still match the Accept transition or the endpoint returns 409. Legacy callers may omit the token. |
| `POST /board/cards/{id}/review-action` | Body `{action, note?, assignee?}`. `respond` deposits a required note into the card's open blocked question and Hermes sessions continue through their existing conversation; `reject` requires a note, commits the card transition before publishing the follow-up row, then resumes the prior session; `reassign` moves a Review card to Assigned for a validated assignee, optionally appends a context note, retires old completion anchors, and retains the prior session's messages/transcript and compatible native handles for the next claim. Fresh local, remote, Hermes, Managed Agents, and CLI routes receive bounded task notes plus a bounded synthetic prior transcript/output in their first prompt or durable message. Task writes recompute tag/note patches against the latest CAS snapshot; failed paired steps roll back only action-owned changes when the transition version still matches, otherwise they return a clear rollback-conflict response without clobbering concurrent notes, tags, or claims. |
| `POST /board/cards/{id}/cancel` | Reads the task, then checks ownership before anything else: `is_review_pending` first, then `is_agent_owned` on the raw tags, so a `me` card or a Review card that somehow already carries `status="cancelled"` gets its real 409 instead of a misleading idempotent 200. Once ownership/review clears, looks up any live `cli_sessions` row for the card before the idempotent short-circuit, so a repeat call reports the same untorn-down CLI session every time rather than only the first. `status == "cancelled"` then short-circuits to a response that still carries that CLI-session failure list but touches no session. Otherwise calls `evaluate_card_action(..., "cancel")`, which also refuses an already-**finished** card — `status` `"done"` (e.g. accepted) as well as `"cancelled"` — with `CANCEL_ALREADY_FINISHED_ERROR`, "this card is already finished — nothing to cancel" (the route's own idempotent short-circuit for a legitimately-already-cancelled card is a deliberate exception applied before ever reaching this check). Finds the card's live LifeOS-agent session with a direct `SessionStore.get(card_id)` primary-key read — `task_id` is the `sessions` table's PK — not the 200-row, most-recently-started `_build_snapshot()` window `_task_card` uses for display, which could silently miss an older still-running session. If one exists and isn't terminal, tears down its whole subtree via `_kill_session_subtree` (shared with the kill endpoint, reason `"cancelled from the board"`). A `SubtreeTeardownError` from that call (a genuine mid-subtree failure, distinct from an individual managed-agent teardown failure, which is folded into `failures` instead) stops the task from ever being marked cancelled — the response reports which sessions were already stopped before the failure and invites a retry; a `TaskConflictError` on the final status write, after a successful teardown, says the session is already stopped and invites a retry rather than the generic conflict text. A live cc:/cx: CLI session (opened via the drawer's Open button) lives in the separate `cli_sessions` table, keyed by its own session_id, not task_id, so the primary-key lookup above can never find it, and Cancel can't kill a CLI process (a separate concern) — each one found is appended to `failures` with a reason naming it, rather than the endpoint silently claiming a teardown it didn't perform. Strips `agent-running`/`agent-blocked`/`human` from the tags (any of which would otherwise outrank Done in `derive_lane`) and writes `status="cancelled"` — `TaskManager` stamps `[cancelled:: <date>]` and the `- [-]` checkbox. Response: `{id, lane, status, tags, killed, failures}`. |
| `GET /pending-questions` | `session_store.list_open_questions()` — unanswered, unprocessed, not-timed-out `pending_questions` rows whose `kind` is `clarification` or `goal_approval`; `followup` (completion notices) and `status_anchor` (routing plumbing) rows are excluded so a Review card never renders a fake pending-question badge. |
| `POST /pending-questions/{id}/answer` | `session_store.deposit_answer_by_id(question_id, answer)` — writes `answer`/`answered_at` on that exact row id, then invalidates `_board_cache` so the stream's next tick reflects it immediately. |

### Card action policy

Each guarded write re-runs its own decision inside `TaskManager.update`'s CAS, through the `_precondition` hook, against the exact snapshot the write lands on and under the store's lock. A route plans against a read taken before the write, so the two can disagree: a card the worker claims in that window would otherwise take the planned write, and the lane endpoint would strip the assignee tag off a live card. A fresh evaluation that refuses raises `agent_board.CardDecisionChanged`, carrying that refusal's own status and detail so the caller reports the real reason rather than a generic conflict. The check is decision-level rather than a version pin: a concurrent change the decision does not read (a notes edit) evaluates to the same answer and proceeds, and the lane endpoint writes the re-planned tags so a tag added in that window survives. The guarded paths are `PUT /board/cards/{id}/lane`, `POST /board/cards/{id}/cancel`, and a board-marked `PUT /api/tasks/{id}`; an unguarded request carries no precondition and pays for no extra work.

`agent_board.evaluate_card_action(current_status, current_tags, action, target_lane=None, has_live_session=False)` is the ONE decision every server write path that can touch an agent-owned card's lane, assignee, or status consults — `None` means allowed, `(http_status, detail)` means refused with that exact status/detail and no write. `action` must be one of `CARD_ACTIONS` (`"lane_move"` [needs `target_lane`], `"assignee_change"`, `"field_edit"`, `"cancel"`) — any other value raises `ValueError` rather than falling through to an implicit allow. Its rule precedence, evaluated top to bottom:

1. An unknown or undroppable (`review`/`scheduled`) `target_lane` — 400 (lane_move only).
2. `claimed` — every `lane_move` target is refused, and so is `assignee_change`/`field_edit`; `cancel` is still allowed — unless the card is also a pending review, which `cancel` evaluates ahead of the claim check (rule 3). `is_claimed(status, tags, has_live_session)` is true whenever `agent-running`/`agent-blocked` is present, regardless of `has_live_session` — OR, when `has_live_session` is true, for `status == "in_progress"` on an agent-owned, non-review card. That second path exists because `cli_session_event` sets that status the first time the drawer's Open button spawns a CLI session on a `#claude`/`#codex` card, before the worker itself ever adds `agent-running` — a live session genuinely backs the card even though no claim tag does yet. `has_live_session` is the caller's own answer (see `SessionStore.has_live_session`) to whether an actual session — not just a status value a vault edit or a board reassignment can produce with nothing running behind it — exists; `evaluate_card_action` itself takes no I/O and trusts whatever it's given. The non-review exclusion matters because the same status can linger at `in_progress` after the worker completes a card that was earlier opened via CLI (nothing resets it) — without it, a Review card could be swallowed by this check and lose the accept-by-drag Done carve-out (see `tests/test_agent_board.py::test_cli_opened_review_card_still_treated_as_review_not_claimed`). `is_agent_owned(tags)` — used both by this claim check and by the ownership rules below — is true when the assignee tag names an agent engine OR the tags already carry `agent-running`/`agent-blocked`, regardless of whether an assignee tag is also present — a card the worker claimed while it still carries `#me` (the worker selects on the bare `#agent` tag alone, and its claim swap never touches assignee tags) is agent-owned too, the same as the bare-`#agent`-queue-card shape with no assignee tag at all.
3. A pending review (`agent-completed` without `accepted`) — `lane_move` to `in_progress`/`human_queue` refused ("accept the review first"), `done`/`unassigned`/`assigned` allowed; `cancel` refused ("accept or reject the review").
4. `agent_owned`, unclaimed, not a pending review — `lane_move` to `in_progress` refused ("only the worker claims agent-assigned tasks"), to `human_queue`/`done` refused ("agent-owned cards are managed by the agent — reassign, unassign, or cancel this card instead"), to `unassigned`/`assigned` allowed; `cancel` allowed UNLESS `status` is already `"done"` or `"cancelled"` — `CANCEL_ALREADY_FINISHED_ERROR`, "this card is already finished — nothing to cancel" (the route's own idempotent short-circuit for a legitimately-already-cancelled card is a deliberate exception it applies before ever reaching this check — see the endpoint table above).
5. Otherwise (`me` assignee, or none) — every rule and outcome matches ordinary human-card behavior; `cancel` refused ("cancel is only available for agent-assigned cards" — there's no worker to tear down or reassign).

`plan_lane_move` calls this first (passing `"lane_move"`) and returns its error as-is on refusal; the write-planning half (which tags/status to actually set) only runs once it's cleared. `PUT /api/tasks/{id}` (`api/routes/tasks.py`) calls it for `"field_edit"`/`"assignee_change"` — see the guard below. `_card_policy(task)` (`api/routes/agents.py`) calls it once per lane per card to build the `policy` block every task card in `GET /board`/`GET /board/stream` carries:

```json
"policy": {
  "claimed": false,
  "agent_owned": true,
  "cancel":   {"allowed": true, "reason": null},
  "assignee": {"allowed": true, "reason": null},
  "fields":   {"allowed": true, "reason": null},
  "lanes": {
    "in_progress":{"allowed": false, "reason": "only the worker claims agent-assigned tasks"},
    "human_queue":{"allowed": false, "reason": "agent-owned cards are managed by the agent — reassign, unassign, or cancel this card instead"},
    "review":     {"allowed": false, "reason": "lane 'review' cannot be set directly"},
    "scheduled":  {"allowed": false, "reason": "lane 'scheduled' cannot be set directly"},
    "done":       {"allowed": false, "reason": "agent-owned cards are managed by the agent — reassign, unassign, or cancel this card instead"}
  }
}
```

`lanes` lists ONLY refused lanes — `unassigned`/`assigned` are allowed for this card, so they're simply absent rather than present-and-`true`, matching what the client already assumes (`onCardDropped` treats a missing lane entry as allowed). `review` and `scheduled` are refused for every card unconditionally, so both always appear regardless of state — a consumer following "absent means allowed" never has to special-case either one as the exception. This keeps the per-card bytes `GET /board/stream` re-serializes to compute its change signature every 0.5s down to just the lanes actually worth mentioning.

`web/agents/board.js` and `web/agents/assignment.js` read `card.policy` directly — a drag checks `card.policy.lanes[targetLane]?.allowed` before issuing the PUT (a fast client-side path; the server still refuses independently, so a stale board is caught by the normal error-toast path; Review/Scheduled are refused via `DIRECT_LANE_IDS` ahead of the policy read entirely), the drawer's assignee select, the Tags field, and the assignment pickers (its own engine select included, though the drawer hides that row in favour of the drawer's Assignee select) disable themselves and render `reason` as visible text — in its own neutral `.drawer-field-reason` element, never the `.assignment-error` element reserved for an actual failed save — when their policy entry is refused. Cancel is offered on every task card that carries a policy block, disabled-and-explained rather than hidden when `policy.cancel.allowed` is false — a claimed card, agent-assigned or not, always keeps at least this one recovery action visible. Mark Done (a drop onto Done under the hood) is gated on `(card.policy.lanes.done ?? {}).allowed !== false` — guarding the possibly-absent `.done` leaf. Clicking Cancel tears down the drawer's session panel through its own cleanup path and re-renders the open drawer explicitly afterward, rather than relying on `fetchBoard()`'s own `updateOpenDrawer`, which skips the rebuild while the just-clicked button still holds focus — without either, a stale Open button on a card that just moved to Done would 409 on the next click, and the panel's own in-flight requests would abort visibly mid-teardown. Delete carries no policy gate at all — it's offered on every card the drawer can open, task or schedule — and its confirmation re-resolves the card from live board state at confirm time, working around the same `updateOpenDrawer` staleness as the Cancel path above, so the kill it discloses matches the kill it performs. A disclosed kill runs through the shared `POST /sessions/{id}/kill` first and blocks the delete unless that call returns 2xx with an empty `failures` list; the delete itself is `DELETE /api/tasks/{id}` for a task card and `DELETE /api/scheduler/{id}` for a schedule card. Schedule cards carry no `policy` at all (there's nothing to derive it from), and any task card lacking one is treated as fully allowed everywhere — the frontend never re-implements the rules, so an absent policy defaults open rather than guessing.

The drawer's Tags field (`web/agents/board.js`) is gated by `policy.assignee` — the same policy entry the Assignee select uses, since a tags-only edit can change the same claim state — reading the card's current tags fresh from board state (not the render-time closure) so a claim written by another process while the field holds focus is never overwritten by a stale re-append. It never renders a worker lifecycle tag (an explicit allowlist — `agent-running`, `agent-blocked`, `agent-completed`, `agent-failed`, `agent-budget-exceeded`, `accepted`, `agent-reassigned` — not a prefix match, so the bare `agent` queue tag and an operator label that happens to start with `agent-` stay editable) as an editable token — mirroring how it already excludes assignee-name tokens — rejects one if typed, and always re-appends the card's own lifecycle tags on save regardless of what's in the box, so an edit on a still-editable card (e.g. Review, which isn't claimed) can't silently strip one either. The server backstops this independently of any client marker: a `PUT /api/tasks/{id}` whose `tags` patch would drop a claim tag from a currently-claimed card, change the assignee-tag set on a claimed card, or add a worker-only lifecycle tag (`agent-running`, `agent-blocked`, `agent-completed`, `accepted`) to any card at all is refused, keyed on the card's own current claim state rather than on any marker in the request — this is what actually stops a bare `{"tags": [...]}` PUT (the exact shape the drawer's Tags field sends, with no `fields` key at all) from silently stripping a claim.

### `assigned_by: "board"` guard on `PUT /api/tasks/{id}`

The board's assignment pickers (`web/agents/assignment.js`) write `model`/`effort`/`host`, and its engine picker writes the assignee tag, through `PUT /api/tasks/{id}` rather than the lane endpoint — every one of those writes stamps `fields.assigned_by: "board"`. `api/routes/tasks.py`'s `update_task` checks for that marker (case-insensitive, stripped) ONLY when `request.fields` is present, and reads the current task to run the claimed-card guard whenever the marker is present alongside a `model`/`effort`/`host`/`status` change, or whenever the patch carries a `tags` key at all (the tags guard runs independently of the marker — see below) — a request with neither never pays for the extra `TaskManager.get`. When the field-edit guard runs: a patch touching `model`, `effort`, `host`, or a raw `status` field runs `evaluate_card_action(..., "field_edit")`; a patch whose `tags` would change the normalized assignee-tag SET or claim-tag SET (`agent_board.normalize_tags(new) & {ASSIGNEE_TAGS...}` vs. the old, and likewise for `{RUNNING_TAG, BLOCKED_TAG, COMPLETED_TAG, ACCEPTED_TAG}`) runs `evaluate_card_action(..., "assignee_change")`, and separately, adding any of those four lifecycle tags to the set unconditionally 409s regardless of the card's current claim state — no HTTP caller in this codebase legitimately adds one this way. Either refusal raises the `HTTPException` before `TaskManager.update` is ever called — no partial write. Comparing the tag SETS (not just `derive_assignee`'s single first-match-wins value) is what catches a second assignee tag added alongside the first, or a claim tag dropped while the derived assignee stays the same — both trip the guard because the tag SETS differ even when the single derived value doesn't.

### Why the board SSE isn't event-driven

The issue's target is "reflects an external vault edit within ~3 seconds," and the task watcher's own debounce (`api/services/task_watcher.py`, `_DEBOUNCE_SECONDS = 2.0`) already spends most of that budget before `TaskManager`'s in-memory index even updates. Wiring a real push (an `asyncio.Event` set from the watcher's background thread via `loop.call_soon_threadsafe`, fanned out to every open SSE connection) would work but adds real cross-thread state for a three-second target that a fast poll already meets comfortably: both `TaskManager` and `SchedulerStore` serve `list_tasks()`/`list_all()` from an in-memory dict, so rebuilding the board costs a dict walk, not disk or DB I/O. `tests/test_agents_board_watch.py` proves the actual (not sped-up) production debounce lands well inside 3 seconds by starting a real `TaskWatcher` against a temp vault, writing an external edit, and polling the stream's own cached read (`_get_board_cached()`) until the change shows up.

### Board cache

`_board_cache` is a module-level `(built_at, board)` tuple used ONLY by `GET /board/stream`'s own tick — never by `GET /board`, which always calls `_build_board()` fresh. The cache exists to de-duplicate simultaneous stream connections within the same instant (multiple open board tabs shouldn't each pay the full build cost on every tick), not to skip rebuilds between ticks: its TTL (`_BOARD_CACHE_TTL = 0.25s`) is far shorter than the tick interval (`_BOARD_STREAM_INTERVAL = 0.5s`). `_invalidate_board_cache()` drops it immediately after every board write — lane-move, accept, cancel, and pending-question answer — so a stream tick right after a write never serves pre-write data. Worst-case latency from an external vault edit to every open board tab reflecting it is the sum of three independent legs: the task watcher's debounce (2.0s) + the cache TTL (0.25s, only matters if a tick lands mid-window) + the stream's own tick interval (0.5s) = 2.75s, inside the 3s budget. A direct `GET /board` skips the last two legs entirely since it never reads the cache.

### Pending-question answer path

`SessionStore.deposit_answer_by_id`, alongside `deposit_answer` keyed by Telegram message id and `deposit_answer_by_session_id` keyed by session, sets exactly the columns `deposit_answer` sets — `answer` and `answered_at` on the matched `pending_questions` row, gated on `answered_at IS NULL AND timed_out = 0 AND kind != 'status_anchor'`. `worker.py::_process_clarification_answers` atomically claims answered rows with the existing `processed` integer (`2` means in-flight), verifies the claim immediately before processing, and only then resumes; reassignment retires both queued and in-flight follow-ups so a stale list cannot revive a retired session. Failed executor resumes release the in-flight marker for retry.

### Frontend module split

Review cards render shared **Accept**, **Reject**, and **Reassign** actions.
Reject requires a note and resumes the existing session through the follow-up
queue, so it needs a linked prior session — without one it renders disabled
with that reason rather than being dropped from the row. Reassign validates the
selected assignee and returns the card to Assigned whether or not a prior
session exists; when one does, it preserves that session's messages and
transcript for the next worker claim and reports `context_preserved`
accordingly. Human queue completion is labeled **Mark Done** in the action row
while retaining the existing Done-lane semantics.

`web/agents.html` is a shell (CSS + tab markup) around eight ES modules under `web/agents/`, served the same way `web/chat/`'s module split is — `<script type="module">` tags resolving against the existing `/static` mount, no bundler:

- **`session_actions.js`** — the single source of truth for the drawer/panel action row: `decideActions(session, card)` is the pure decision function (which of Open, Rename, Go To, Resume, Kill, Answer, Accept, Reject, Reassign, Mark Done, Cancel, Delete apply to a given session + optional linked card, and whether each is enabled or disabled-with-a-reason — no DOM, directly unit-testable), and `renderActionRow(container, opts)` is the shared DOM builder both `board.js`'s drawer and `panel.js`'s header call to render it. Also owns Kill's confirmation modal (with its cascade preview of non-terminal descendants — `getDescendants` may return an array or a `Promise` of one, so the modal itself renders immediately rather than waiting on a fetch, but its destructive confirm button stays disabled until `getDescendants` settles; a rejected `getDescendants` shows an explicit "couldn't check for descendant sessions" notice instead of being indistinguishable from a resolved empty list), Resume's host select + copy-command box, and Go To's pane lookup, so neither surface implements its own copy. `card` is optional throughout: a session with no linked board card (the board never tracked it, or hasn't loaded yet) skips any card-only decision (Open, Accept, Reject, Reassign, Mark Done, Cancel, Delete) rather than inventing one; Rename is session-level and applies whenever a session is present at all. The action row's signature — what decides whether a rebuild is skipped when the decided set hasn't changed — includes the pending question's own id, so a question answered elsewhere and replaced while the row stays open still rebuilds and rebinds Answer to the new one. Also the canonical home of `TERMINAL`, `isSubagentSession` (a session is a subagent either because the worker flagged it directly or, for a routing-derived CLI subagent, because it carries `parent_session_id` with no such flag — `graph.js`'s node rendering and every action decision here test both, so a routing-derived subagent can't be treated as resumable/focusable on one surface and correctly refused on another), `showResumeFor`, `sourceLabelFor`, and the small cross-cutting helpers (`escapeHtml`, `escapeAttr`, `showToast`) `panel.js` re-exports for `board.js`/`graph.js`'s existing imports. Imports `nodeLabel` from `graph_encoding.js` (for the kill-modal's target/descendant names) and never imports from `panel.js`, so the two never form a cycle.
`fetchBoard()` resolves `true` when it actually refreshed the board and `false` when the fetch failed, so a caller that chains work on can tell a current board from a stale one; `moveCard` carries that outcome through on its own resolved value as `boardRefreshed`. The drawer rebuild that follows an assignee change skips painting when the refresh failed and leaves `openCardSnapshot` unadvanced — the drawer is holding the operator's own picker choice at that point, so painting from an unrefreshed board would show a committed value as though it had been reset. `DRAWER_EDITABLE_FIELDS` includes `fields` for the same reason: the model/effort/host pickers write there, and a frame whose only change is a picker value would otherwise read as "nothing changed", leaving a stale drawer with no frame that could ever converge it.

- **`card_actions.js`** — the network call, toast, and (for Delete) the kill-then-delete confirmation modal behind each card-only action (Open, Accept, Reject, Reassign, Mark Done, Cancel, Delete): `openCard`/`acceptCard`/`resolveCard`/`cancelCard`/`openDeleteCardModal`, plus a `cardActionHandlers(card, { findCard, onChanged })` convenience bundle matching `renderActionRow`'s `handlers` shape. `onChanged` fires after a write succeeds, so a caller with its own card cache can refresh it; `findCard` (Delete only) resolves the freshest copy of the card at confirm time, defaulting to the one captured when the button was clicked. Imports `TERMINAL`/`sourceLabelFor`/`escapeHtml`/`showToast` from `session_actions.js` and `LANES` from `lanes.js`; never imports `panel.js` or `board.js`, so both can import it without a cycle.
- **`panel.js`** — the shared session-detail panel: header render (delegating its action row to `session_actions.js`'s `renderActionRow`), inline label edit, backfill + live SSE transcript tail, and LLM summary fetch. Exports a `SessionPanel` class constructed with a `container` element rather than hardcoded ids, so the Graph tab's side panel and the Board tab's drawer can each hold an independent instance without DOM id collisions (both tabs' markup stays mounted; only one is visible via `[hidden]`). A `showActions: false` constructor option (the Board drawer's embedded instance) suppresses the header's own action row entirely, since the drawer renders one itself (`board.js`'s `renderDrawerActions`) covering the card-only actions the embedded panel has no card to decide — without it, the same session's Kill/Resume/Go To would render twice. `open(session, card)` and `updateMeta(s, card)` both accept an optional card, forwarded straight to `decideActions`. When `showActions` is true and a card is present, `_renderActions` wires the card-only actions via `card_actions.js`'s `cardActionHandlers` (constructor options `findCard`/`onCardChanged` reach it there) and always wires Rename to its own `startRename()` (also reachable as a public method, for a caller — `board.js`'s drawer action row — that renders its own row but wants this instance's rename-edit UI). Imports `nodeLabel`/`isRawIdValue`/`routingLabel`/`engineOf`/`ENGINE_SHAPES` from `graph_encoding.js`, `TERMINAL`/`sourceLabelFor`/`showResumeFor`/`isSubagentSession`/`escapeHtml`/`escapeAttr`/`showToast`/`renderActionRow` from `session_actions.js`, and `cardActionHandlers` from `card_actions.js`, re-exporting the `session_actions.js` set (plus `routingLabel`) for `board.js`/`graph.js`'s existing imports.
- **`graph_encoding.js`** — the graph's pure encoding functions: `isRawIdValue`, `nodeLabel`, `engineOf`, `ENGINE_SHAPES`, `shapeTagFor`, `radiusForActiveSeconds`, `ringWidthForToolCalls`, `isKnownSearchField`/`SEARCH_TIER`/`SEARCH_BADGE`, `hoverCardRows`, `descendantsOf(sessions, session)` (the BFS-via-`parent_session_id` walk behind Kill's cascade preview — the same function `graph.js` calls synchronously over its own in-memory session list and `board.js` calls over a `GET /api/agents/snapshot` fetched on demand), and `routingLabel`; also re-exports `LANE_COLORS`/`laneColor` from `lanes.js`. No DOM, no d3, no fetch — directly unit-testable (`tests/test_agents_graph_encoding_browser.py`). Never imports from `panel.js` or `session_actions.js`, even though both import from it — importing `nodeLabel` from either instead would form a cycle.
- **`lanes.js`** — the board's `LANES` table and `LANE_COLORS` palette, imported by `board.js` (lane columns), `graph.js` (lane sub-band layout), `graph_encoding.js` (node fill + legend), and `card_actions.js` (Mark Done's landed-elsewhere lane name).
- **`graph.js`** — the deterministic D3 delegation timeline, filters, chips, and search; rendering pulls from `graph_encoding.js` and `lanes.js` for every pure encoding decision, and `isSubagentSession` from `panel.js` (re-exported from `session_actions.js`) for the node dblclick handler's own subagent check. Exports `initGraph(boardApi)`, called once, lazily, the first time the operator opens the Graph tab, with the object `board.js`'s `initBoard()` returns — so loading the board (the default view) doesn't also open a second SSE connection (`/api/agents/stream`) nobody is watching, and the side panel can find a session's linked card without a second `GET /api/agents/board`. `boardApi.getCardForSession(sessionId)` is read both when a node is clicked (`panel.open`) and on every snapshot tick for the currently-selected node (`panel.updateMeta`) — the latter is what lets an already-open panel pick up a card the board hadn't finished loading yet when the panel first opened, without throwing or rendering a half-decided action set in the meantime. `boardApi.findCard(id)` (by card id, not session id) is passed straight through as the panel's own `findCard` constructor option, so Delete's confirm-time re-resolution (`card_actions.js`'s `openDeleteCardModal`) reads the same live board state on the Graph tab that the Board drawer's own `findCard` wiring does, rather than the card captured when the panel was opened. `boardApi.refresh()` is called from the panel's `onCardChanged` hook so a card action taken from the Graph tab doesn't wait for the board's own next SSE tick to be reflected.
- **`board.js`** — the Kanban board: fetch + SSE, lane rendering, filters, and the drawer, including its action row (`renderDrawerActions`, delegating decision + rendering to `session_actions.js`; supplies handlers for the card-only actions via `card_actions.js`'s `cardActionHandlers`, overriding Cancel and Delete with the extra drawer-specific bookkeeping — closing/rebuilding this drawer — a bare `fetchBoard` handoff doesn't cover). Card and tray dragging share one Pointer Events state machine for mouse, pen, and touch; vertical gestures are left to scrolling, while horizontal drags resolve lane, hidden-Done, and assignee-card targets with policy-aware allowed/refused state. The bottom tray also provides keyboard/click selection as the non-drag assignment path, and the hidden-Done target routes Review cards through the same acceptance endpoint as the drawer. Kill's cascade preview (`getDescendants`) fetches `GET /api/agents/snapshot` on demand — once per Kill click, not polled continuously — and walks it with `graph_encoding.js`'s `descendantsOf`, the same function the Graph tab uses over its own already-in-memory session list; a failed fetch throws rather than resolving with an empty list, so `session_actions.js`'s kill modal can tell "the lookup failed" apart from "this session genuinely has no descendants". Imports `LANES`/`laneColor` from `lanes.js` for the lane columns and each lane header's accent colour. Exports `initBoard()`, called immediately on page load, returning `{ getCardForSession(sessionId), findCard(id), refresh }` for `graph.js`'s `initGraph` to reuse.
- **`assignment.js`** — the card-assignment pickers (engine/model/effort/host) that `board.js` mounts into the drawer; writes `model`/`effort`/`host` (+ `assigned_by`) through `PUT /api/tasks/{id}` and reads `GET /api/agents/models` for the model options and `GET /api/agents/hosts` for the host picker's options. A standalone module wired into `board.js`. Saves are serialized through a single in-flight promise chain rather than fired independently — a queued save reads the controls' CURRENT values only once its turn arrives, then PUTs those, so at most one save is ever outstanding; a rejected save reverts each picker to exactly what the last *successful* save sent, never to a live control value some other save's resolution happened to catch mid-flight.
- **`linking.js`** — the one shared surface between the two tabs: the seven-key shared filter store and the cross-tab focus/tab-activation bus. Owns no DOM. See [Card metadata and cross-tab linking](#card-metadata-and-cross-tab-linking).

**Drag and drop is pointer-based, not the native HTML5 Drag and Drop API.** `draggable="true"` + `dragstart`/`dragover`/`drop` only fires through the browser's OS-level drag gesture and does not provide a consistent touch path. `board.js` tracks `pointerdown` → `pointermove` (past an 8px threshold, distinguishing a drag from a tap) → `pointerup`/`pointercancel`, rendering a floating ghost and using `document.elementFromPoint` to resolve lane, hidden-Done, or card targets. Draggable card and assignee sources use `touch-action: pan-y`, reserving horizontal movement for the custom drag while leaving vertical page/lane scrolling to the UA; the tray remains horizontally scrollable when a gesture starts on its label or gaps. `pointercancel` clears the ghost, target styles, pointer capture, and body state. Trailing clicks are suppressed only after an actual drag, so a tap still opens its drawer. Assignee buttons also support selecting a target first and activating a card, which avoids requiring drag-and-drop for keyboard and touch users.

A successful drop always re-fetches the board (`fetchBoard()`) rather than mutating the DOM optimistically — the server is the single source of truth for a card's lane, and a rejected move (400/409/500) leaves the card exactly where the last successful fetch put it, with a toast surfacing the server's error text.

`board.js` also owns two pieces of client-side state (a third, the lane-selection filter, moved to the shared filter store in `linking.js` — see [Card metadata and cross-tab linking](#card-metadata-and-cross-tab-linking)). The drawer's click-outside-close guard requires the `mousedown`, `mouseup`, **and** `click` to all target the backdrop element itself — the same event-target plumbing the drag/drop paragraph above relies on — because a single `click` listener alone would also close the drawer on a text selection or scrollbar drag that starts inside the drawer and ends on the backdrop. And the New-card composer's create is two calls, not one: `POST /api/tasks` creates the task, then, for any lane other than Unassigned, `PUT /api/agents/board/cards/{id}/lane` moves it there; if that second call fails, the card still exists at its tag-derived resting lane, an error toast reports the failure, and the board re-fetches to reflect what's actually true server-side.

---

## Snapshot shape

```json
{
  "sessions": [{ /* see below */ }],
  "edges":    [{ "from": "<parent_session_id>", "to": "<child_session_id>", "type": "spawn" }],
  "generated_at": 1716777600,
  "api_host": "this-api-host"
}
```

`api_host` is `api_host_name()` — the same value every session row's own `host` falls back to. It lets the drawer's "resume here" host picker (`web/agents/panel.js`) offer THIS machine as a launch target from `/snapshot` alone, when `GET /api/agents/hosts` is unreachable or fails — see [Resume target host](#resume-target-host) below.

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
| `custom_label` | str \| null | Operator-pinned manual label (via `PUT /sessions/{id}/label`). When set, the frontend uses it as the node name in preference to every other source, unless it equals the row's own raw id (session id, prefix-stripped session id, or task id), which is skipped like `short_label` and `label`. |
| `model_label` | str | Short badge — `Local` / `Haiku` / `Sonnet` / `Opus` / `Claude Code` / `Codex` / `Waiting on you` (routing `ask`) / the configured `remote_llm_label` (routing `remote`) / `Hermes` or `Hermes · <model>` (routing `hermes`) — see [LifeOS agent ingest](#lifeos-agent-ingest). |
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
| `mirrored` | bool | Present and `true` only on a row ingested from a mirrored remote host's transcript — see [Remote transcript mirror](#remote-transcript-mirror) below. |
| `lane` | str | The board lane this session's node renders in — `lane_for_session(status, task_status, task_tags)` (`api/services/agent_board.py`): the linked task's own `derive_lane` result when `task_id` resolves to a real task, else a mapping from the session's own `status` (`running`/`claimed`/`yielded` -> `in_progress`, `blocked` -> `human_queue`, a terminal status -> `done`, else `unassigned`). Additive; computed uniformly over every row in `_build_snapshot`, regardless of source. Stays non-null even for a session with no linked card — unlike `card_id`/`card_title`/`assignee`/`card_tags` below, which are all null together for one — since the graph's node fill colour and lane legend need a lane for every node. |
| `pending_question` | dict \| null | `{id, session_id, question, asked_at, bot}` for the session's own open question, or `null`. Built by `_pending_question_view`, the same function `_task_card` (board endpoint) uses for a card's `pending_question` — so the graph node and the board card can never disagree about whether a question is open. |
| `card_id` | str \| null | The linked task's id — the same value as `task_id` once a `TaskManager` lookup confirms the task exists, `null` when `task_id` is unset or names an id the store has nothing for. The graph uses it for board navigation and card metadata, never as a topology node. |
| `card_title` | str \| null | The linked task's `description` — the same text the board card's own title shows. `null` alongside `card_id`. |
| `assignee` | str \| null | `agent_board.derive_assignee(task.tags)` for the linked task — the same assignee value the board card shows. `null` alongside `card_id`, including when the linked task exists but carries no assignee tag. |
| `card_tags` | list[str] | The linked task's own tags — `[]` alongside a null `card_id`, never `null` itself. |

`card_id`/`card_title`/`assignee`/`card_tags` are looked up once per distinct `task_id` present in the snapshot (`_tasks_for_session_dicts`), not once per row — `_lane_for_session_dict` accepts that same pre-built `{task_id: task}` map so a session's lane and its card-join fields share one `TaskManager` read rather than two. A store error degrades every one of the four fields to its null/empty value for the affected rows rather than failing the snapshot.

---

## LifeOS agent ingest

Read paths in `api/routes/agents.py`:

- `_session_to_dict(s, transcript)` — projects a `Session` row to the snapshot shape. Defends against the optional `total_cache_*_tokens` attrs being absent on old rows.
- `_label_for_session(s, events)` — walks the first five transcript events looking for `description` / `task_description` / `prompt`; falls back to the session id. Result cached per session id (capped at 500 entries).
- `_summarize_events(events)` — counts tool calls and errors across the last 100 events. `_is_error_kind` matches the literal set `{failed, managed_failed, child_failed_internal, killed, cascade_killed}` plus any kind ending in `_failed` or `_error` (future-proof).
- `_model_label_for_routing(routing, hermes_model=None)` — `local` → `Local`; `ask` → `Waiting on you` (a session parked waiting on the operator has no model running, so it must never render a Claude-tier guess); `remote` → `settings.remote_llm_label` (falls back to `Remote` if unset or settings import fails); `hermes` → `Hermes · <hermes_model>` if `hermes_model` is truthy, else plain `Hermes`; `claude_code`/`code` → `Claude Code`; `codex` → `Codex`; otherwise derives the badge from `settings.agent_managed_model`, falling back to `Claude` if the model name doesn't match any known family. `Session.model` (the board's model *picker* value) is exposed separately as the `model` field above but does not feed this badge — `HermesExecutor` never reads or writes it, and a conversation-rooted Hermes session (the dominant path) passes no `model` at all.
  - `hermes_model` is `_session_to_dict`'s `s.hermes_model` — `Session.hermes_model`, a nullable `sessions` column written only by `HermesExecutor.execute`, for the session it is executing, right after that turn's own `_HermesTurnPersister.reported_model` is known (on both the completion and failure exit paths — a turn whose connection dropped after Hermes reported usage still ran on that model). No other writer touches this column, so it can never be overwritten by an unrelated session's or surface's turn, and a completed session's value stays fixed once no more of its own turns run — unlike a process-wide "last observed" reading (`api/services/model_readout.py`'s `_last_observed_hermes_chat_model()`, written by `hermes_proxy._handle_usage` in whichever process ran the turn and scoped to the `/models`/`/api/health` surface it serves), which in the API process holds whatever model the last `/chat` Hermes turn reported and so would risk showing a finished session an unrelated turn's model. An in-memory per-session cache is not used instead: `HermesExecutor` runs in the agent-worker process while the snapshot is served by the API process (two separate systemd units), so an in-memory dict keyed by session id would be invisible to the reader; the value goes through the shared `SessionStore` sqlite db. The `/chat` Hermes proxy path threads no session id into `_HermesTurnPersister` at all, so it writes nothing here — including onto the deterministic `sess_herm<hash>` conversation-anchor row `resolve_hermes_caller_session_id` creates, which is never dispatched and so never runs a turn of its own; that row's `model_label` value stays plain `Hermes`, forever. The `/chat` threads panel never carries a model either: `_thread_dict` calls `_model_label_for_routing` with one argument, so its `model_label` is always plain `Hermes`, and `routeBadgeHtml` (`web/index.html`) renders no badge at all for `hermes` routing, so that value reaches no pixel there.
  - `model_label` reaching `Hermes · <model>` in the API response renders in the side panel's Routing badge: `web/agents/panel.js`'s `routingBadgeText(s)` shows it verbatim, in place of the plain `Hermes` name, once a Hermes session's own turn has reported a model; a Hermes session with no turn yet shows plain `Hermes`. The hover card (`hoverCardRows`) always shows `model_label` as its Model row when present; the panel's `.panel-chips` row (`panelChipsHtml`) shows it too, except it's dropped there when it already equals the Routing badge's own text — which for a Hermes session with a reported model, it does. `web/agents/graph_encoding.js`'s `nodeLabel()` never reads `model_label` at all — see [Node shape, colour, size, and badges](#node-shape-colour-size-and-badges) — so the graph node's own name is unaffected either way. See [agent-viz.md](../product/agent-viz.md#graph-tab--side-panel) for the product-facing description.

Per snapshot tick the route calls `session_store.list_sessions(limit=200)` (newest-first) and emits one edge per session with a `parent_session_id`. Subagents that exist only inside the transcript (no SessionStore row) do not appear in this path — they show up via the Claude Code ingest below.

---

## Claude Code ingest

`api/services/claude_code/session_ingest.py` is a read-only adapter that translates Claude Code's per-message JSONL schema into the LifeOS shape. Public surface used by the route:

| Function | Purpose |
|---|---|
| `discover_sessions(projects_dir, lookback_days)` | Walks `<projects_dir>/<project_key>/*.jsonl`, returns one `SessionMeta` per file modified within the window, newest-first. |
| `parse_session(meta)` | Reads the jsonl, sums usage, extracts label / subagents / last event kind, infers status. |
| `build_snapshot(...)` | Combines discovery + parse + process detection + subagent expansion. Cached 30s per `(projects_dir, lookback_days, live_counts)`; returns per-row copies. |
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

## Remote transcript mirror

`api/services/agent_transcript_mirror.py` closes the gap registration alone leaves: a `cli_sessions` row gives a remote session status and a prompt preview, but token counts, cost, tool-call counts, and the transcript feed all require an actual jsonl on this host's filesystem. The mirror pulls one, read-only, over ssh+rsync.

### Layout

`mirror_root()` (default `data/agent-transcript-mirror`, `LIFEOS_AGENT_TRANSCRIPT_MIRROR_DIR`) holds one subdirectory per registered host, mirroring each source directory's own internal structure so the existing `discover_sessions()` scanners work against it unmodified:

```
<mirror_root>/<host>/claude_code/<encoded-cwd>/<session>.jsonl
<mirror_root>/<host>/codex/<year>/<month>/<day>/rollout-*.jsonl
```

`host_dirs(host)` sanitizes `host` before deriving these paths via a positive allowlist (alphanumeric first/last character, `.`/`-`/`_` allowed in the middle only, nothing starting with `-` or `.`, no `/`, `\`, or whitespace anywhere) rather than a reject-list — a malformed `LIFEOS_AGENT_HOSTS` key that would otherwise be misread as an rsync/ssh option (`-e`, `--delete`, `~`, `$HOME`) is rejected (`ValueError`) before ever reaching a path join, so it can't write outside the mirror root.

### The pull

`mirror_host(host, ssh_target, *, runner=...)` runs one `rsync` invocation per engine (`-rtz`, `--include=*/ --include=*.jsonl --exclude=*`, a `--` separator before the source/destination positionals so an `agent_hosts` value beginning with `-` can't be misread as a flag, over `ssh -o BatchMode=yes -o ConnectTimeout=<agent_ssh_connect_timeout>`, its own `--timeout` bounding a half-open connection). Read-only pull: no `--delete` on the remote side, never a push. `-t` (preserve mtimes) is what makes rsync's default quick-check (size + mtime) skip an unchanged file on the next tick — the source `remote_dir` string (`claude_code_projects_dir` / `codex_sessions_dir`, which may contain `~`) is passed through verbatim so the REMOTE shell expands it, never this process. `runner` is an injectable `Callable[[list[str]], subprocess.CompletedProcess]` (mirrors `remote_spawn.kill_remote_process_group`'s seam) so tests never shell out.

Both engines are attempted even if one fails (a host that only runs one CLI shouldn't lose the other engine's mirror). Exit codes 23 ("partial transfer due to error") and 24 ("partial transfer due to vanished source files") are treated as SUCCESS — both are the expected outcome of pulling a transcript a live CLI is actively writing to, not a real failure — but are still worth knowing about: the diagnostic stderr line (`_diagnostic_stderr_line`, the first non-blank line that's neither rsync's own generic trailer nor ssh's benign "Permanently added ... to the list of known hosts" first-connect notice) is logged at `debug` every time, and at `warning` once per `(host, engine, message)` for the life of the process when it specifically names a source directory rsync couldn't enter (`change_dir ... failed`) — so a host that legitimately never runs one engine, or has a permissions problem, is surfaced exactly once per `(host, engine, message)` rather than on every tick. Any OTHER exit code produces one `warning` line for the host — `host <name> failed: <diagnostic line>` — and a `MirrorResult(ok=False)`; nothing raises. `mirror_once(*, runner=...)` mirrors every host in `settings.agent_hosts` except the API's own host (`remote_spawn.api_host_name()`) concurrently in a small `ThreadPoolExecutor`, so one slow or unreachable host never delays another. `start()`/`stop()` wrap this in an `asyncio.to_thread`-driven interval loop (`agent_transcript_mirror_interval_seconds`, default 120s), wired into `api/main.py`'s lifespan next to `agent_viz_summary_prefetch`'s identical shape; a no-op — no task scheduled, no per-tick logging — when disabled or when `agent_hosts` is empty.

### Ingest + status merge

`mirrored_snapshot()` walks every host subdirectory that exists and calls the SAME `cc.build_snapshot()` / `cx.build_snapshot()` the local scan uses, pointed at the mirrored directory instead of the local one, then stamps every row `host = <mirrored host name>` and `mirrored = True`.

`_build_snapshot()` merges these rows in one pass, placed after the existing local cc/cx blocks and before the leftover-`cli_sessions` synthetic-row pass: a mirrored row whose `session_id` already appears among the locally-discovered rows is skipped (local transcript wins — it can only be fresher, since the mirror lags by up to one interval); otherwise it's popped against the same `cli_by_id` map the local rows already consume from, so `_apply_cli_session_to_dict` applies identically — **status comes from the hook event, token/cost/tool-call detail from the mirrored transcript**. When there's no matching hook row, the mirrored row's own `host` (the remote host's name) is left as-is rather than overwritten with `api_host_name()` — that overwrite is only correct for a row that genuinely ran on this API host.

`mirrored_snapshot()` also returns each host's own parent-subagent spawn edges, which `_build_snapshot()` extends `edges` with. An edge is kept only when both endpoints are ids the merge loop above actually *appended* a row for, and both endpoints came from the same source host's batch (`appended_mirrored_host_by_id`, a dict mapping each appended id to the host it was appended from) — not the broader `local_ids`, which contains every id whether appended or skipped for a collision. Keying on the source host, not just id membership, is what tells apart two distinct cases: a session id that lost an id collision to a local transcript (its edge must not attach to the unrelated local row that won the collision), and a session id present on *two mirrored hosts* (an edge from one host's batch must not attach to the other host's unrelated parent row, since they merely happen to share an id). Edges are also deduped on `(from, to, type)`: the same session mirrored from two hosts, or an id collision on the child end, can otherwise derive the identical edge twice.

`cc.build_snapshot()`/`cx.build_snapshot()` hand back per-row copies (`[dict(row) for row in ...]`) on both the cache-hit and cache-populate paths, so `_build_snapshot()`'s hook-event overlay (`_apply_cli_session_to_dict`, applied to the SAME dict `mirrored_snapshot()` stamps `host`/`mirrored`/demotion onto) writes to a row the 30s ingest cache (see "Snapshot caching" below) doesn't own — a warm-cache read on the next tick observes the cache's own pristine row, so an event overlay from an earlier tick can never survive past the hook row that produced it. `mirrored_snapshot()`'s own `dict(row)` copy before stamping `host`/`mirrored`/demotion is defensive layering on top of that guarantee rather than the only thing preventing cache corruption.

### Liveness exclusion

Both `cc.build_snapshot()` and `cx.build_snapshot()` take an optional `live_counts: dict[str, int] | None = None`. `None` (the local route wrappers in `api/routes/agents.py`) scans this machine's own processes via `live_claude_cwd_counts()` / `live_codex_cwd_counts()`. `mirrored_snapshot()` passes `{}`, guaranteeing a mirrored row's cwd can never match a "live" entry — a mirrored transcript existing on this host says nothing about whether the CLI process is actually running here, so the per-cwd process-count promotion to authoritative `running` (see "Live process detection" above) must never fire for one.

`live_counts={}` alone isn't sufficient, though: `_infer_status`'s mtime-under-10-minutes branch returns inferred `running` regardless of `live_counts` — it's a separate rule from the process-scan branch, and rsync preserves the remote mtime, so a freshly-pulled mirrored transcript trips it every time. `mirrored_snapshot()` closes this by demoting an inferred `running` row (`status_inferred is True`) to `inactive` before returning it. `_build_snapshot()`'s existing hook-event overlay (`_apply_cli_session_to_dict`) then restores `running` afterwards for any row that also has a matching `cli_sessions` entry reporting it — so a mirrored session's `running` status can, in the end, only come from that merged event row, via this demote-then-restore step rather than from the mtime-based inference alone.

### Events, stream, and summary

`_read_cli_transcript_events(session_id)` (in `api/routes/agents.py`) tries the local transcript root first, then each mirrored host's copy in turn via `mirrored_transcript_dirs(engine)`, returning the first non-empty result — used by `GET /sessions/{id}/events`, `/stream`, `/summary`, and the viz prefetcher (`agent_viz_summary_prefetch._summarize_one`) so a mirrored session's transcript reads identically to a local one everywhere the API reads events. `_lookup_cc_session_meta` / `_lookup_cx_session_meta` (the `/focus` FD-probe's session-metadata resolver) apply the same local-then-mirrored search, so a mirrored-only session (no live `cli_sessions` row) still resolves for `/focus` and for `_resume_command_text`'s cwd lookup rather than a bare "not found."

### Resume target host

`CCResumeRequest.target_host` (optional, on both `/resume` and `/focus`) lets the operator override the launch target regardless of the session's recorded host — `_resolve_target_host(session_id, target_host)`: blank/unset falls through to `_check_session_host_or_409` (including its 409); a value equal to `api_host_name()` launches locally; a value in `settings.agent_hosts` launches over ssh to that target; anything else 400s with `detail = {"error": ..., "command": <the resume command text>}` so the drawer can render it for copying. `_resume_command_text(session_id)` is called from THREE sites — `_resolve_target_host`'s 400 above, plus both launcher functions' own cwd-failure 400 branches described below — but only for rendering that copyable command text; the launchers still build their own `inner_rendered` independently for their success path, and it isn't a byte-for-byte match: this function prepends `cd <cwd> && `. It resolves cwd local-transcript-first, then mirrored, then the `cli_sessions` row's own `cwd` as a last resort — the common case for a `target_host` override on a session this host has never mirrored. When no cwd resolves at all, it returns `""` rather than a bare inner command missing its `cd`, which would otherwise resume in whatever directory the operator's terminal happens to be in; the drawer suppresses the command box entirely on an empty string.

The two launcher functions (`_resume_claude_code_launcher`, `_resume_codex_session`) have their own local (`target_host` is this API host, or unset) fallback chain, independent of `_resume_command_text`'s cwd resolution: the local `discover_sessions` scan first, then the same mirrored-transcript lookup `/focus` uses, then the `cli_sessions` row's own `cwd` as a last resort — so "resume here" onto this API host also resolves a cwd for a session this host has only ever seen mirrored or via a hook event, not just one with a local transcript. Because a mirrored session's cwd is the REMOTE machine's path, that cwd may not exist, may not be a directory, or may not be accessible on this host at all on the local (non-ssh) branch; the child's failed `os.chdir` then raises `FileNotFoundError` (ENOENT), `NotADirectoryError` (ENOTDIR — the cwd is a regular file), or `PermissionError` (EACCES) — all `OSError` subclasses — and the launcher distinguishes a cwd failure from any other spawn failure via `exc.filename` (set to the cwd by the failed `os.chdir`, never to argv[0] for these three) checked in a single merged `except OSError` handler: a cwd failure 400s with the copyable command (calling `_resume_command_text` above) instead of falling through to a 500 — "resume binary not found" specifically preserved for a genuine missing-executable `FileNotFoundError`, "resume spawn failed" for anything else.

---

## Snapshot caching

Two caches:

1. **`_snapshot_cache`** in `session_ingest.py` — keyed by `(projects_dir, lookback_days, live_counts)`, TTL 30s. The whole `(sessions, edges)` tuple is memoized so a single SSE tick across many connected clients doesn't re-walk the projects dir; `build_snapshot()` returns per-row copies of the cached session dicts on every call, so a caller mutating a returned row never writes into the cache. Bypass with `cache_ttl=0` (used by tests).

2. **`_label_cache`** in `agents.py` — keyed by session id, capped at 500 entries. Labels are derived from the first 5 transcript events and don't change once a non-fallback label has been resolved.

Both caches are lock-guarded so concurrent FastAPI threads can't see partial entries. Invalidation is on-demand via `invalidate_cache()` / `invalidate_process_cache()` (used by tests and reachable from a future admin endpoint if needed).

A third cache, `agent_viz_summary.py`'s in-process + disk-backed short-label cache, treats a session as terminal (cacheable forever) using `TERMINAL_STATUSES` from `session_store.py` **unioned with** `{"ended", "inactive"}` — those two are CLI-only statuses (`cli_sessions` events / the transcript scan's file-age guess) that don't exist in the worker's own terminal set; without the union, a CLI session's fallback label would never cache and `agent_viz_summary_prefetch.py`'s background loop would retry it every tick forever. The same caching applies to a summarizer call that raises (not just the deterministic empty-transcript fallback): `_cache_if_terminal(session_id, last_activity_at, status, result, *, is_error_fallback=False)` is the single helper both paths call, and it only writes for a terminal status — a live session's fallback stays uncached so it can still pick up real content later. The prefetcher's own dispatch (`_summarize_one`) mirrors the `/summary` route's three-way `cc:`/`cx:`/else split, so a Codex session's events resolve through the `cx:` branch rather than `TranscriptStore().read()` (which returns `[]` for a `cx:` id). The in-process half is keyed by session id and capped at `_CACHE_MAX` (500) entries with FIFO eviction of the oldest-inserted key; every write goes through `_cache_put`, so the bound holds on the real-summary, terminal-fallback, and disk-hit promotion paths alike, and re-putting a key already present updates it in place without evicting anything.

`is_error_fallback=True` marks an entry cached from a summarizer call that *raised* (a timeout, an LLM queue backup, a JSON-parse failure) rather than the deterministic empty-transcript fallback — that path is deliberately kept **in-process only**, bounded by `_FAILURE_FALLBACK_TTL_SECONDS` (10 minutes), and is never written to disk. A transient failure isn't a genuine dead end the way "no transcript content" is; the same session's real transcript is still sitting there and a retry could succeed, so an exception fallback must not permanently poison a terminal session's summary for the life of the install the way a disk write effectively would (`prune_disk_cache` has no scheduled caller). The TTL matches `agent_viz_summary_prefetch._FAILURE_BACKOFF_TICKS` (30 × 20s = 10 min) so the prefetcher's own retry-after-cooldown isn't silently absorbed by a cache hit here. One consequence: AC 7's literal wording ("the fallback is never re-called for that (session, last_activity_at)") is knowingly relaxed for this path — after the TTL, a deterministically-failing input **is** re-summarized, roughly every 10 minutes, for as long as the failure persists. That's the correct trade for the reason above, and `_FAILURE_BACKOFF_TICKS` bounds the retry rate; it just isn't literally "never".

`_is_frozen(status)` is a strict subset of "terminal": terminal minus `"inactive"`. `"inactive"` is the transcript scan's file-age guess for a Claude Code session idle more than 30 minutes, not a real terminal event — the session can resume, and `web/agents/panel.js`'s own `TERMINAL` set agrees that only `"ended"` (not `"idle"`) is truly done. `_is_fresh_enough` uses `_is_frozen` to decide whether a cached entry can be served "regardless of new activity": a frozen status grants that leniency unconditionally for a *real* summary, but withholds it when the cached content is only the deterministic `_NO_CONTENT_SUMMARY` sentinel — re-deriving a no-content fallback costs no LLM call, and a frozen status guaranteeing "no more content will ever arrive" is exactly the kind of claim a later real summary could prove wrong. An *error* fallback never reaches `_is_fresh_enough` at all — it's checked directly against `_FAILURE_FALLBACK_TTL_SECONDS` from the in-process cache, since it's never written to disk.

---

## Delegation timeline

`web/agents/graph.js` uses D3 v7 for SVG data binding, zoom, and interaction, but not for force simulation. `delegationTimelineLayout` in `graph_encoding.js` computes coordinates as a pure function: sessions are sorted by `started_at` (falling back to `created_at`, `last_activity_at`, then zero), with `session_id` as a stable tie-breaker and ancestors constrained ahead of descendants. Horizontal coordinates use fixed 190-unit columns from left to right. Vertical coordinates use fixed 190-unit rows by `parent_session_id` depth; a missing or filtered parent makes the visible session a root. Cycles terminate at depth zero rather than recursing indefinitely.

The viewport renders **EARLIER/LATER** labels and a dashed guide for every occupied depth (`ROOT`, `LEVEL 1`, ...). Coordinates are recomputed from the snapshot values on every render, so the same visible input always produces the same positions. Hosts, board cards, lanes, and engines do not influence coordinates; they remain filters and node/panel metadata. See `tests/test_agents_graph_encoding_browser.py` for direct layout tests.

Delegation links are derived from each visible session's `parent_session_id`, not from grouping metadata. The graph therefore contains only session nodes and actual parent-child edges; cards and machines never create synthetic nodes or edges.

### Node shape, colour, size, and badges

`shapeTagFor(engineOf(d))` (`graph_encoding.js`) picks the SVG element created for a node's `.node-shape`: `rect` (claude_code), `polygon` (codex — hexagon points, and local — diamond points, same tag, different point set), `path` (hermes — a five-point star), `circle` (claude). `applyShapeAttrs` sets that element's geometry attributes from `nodeRadius(d)` (= `radiusForActiveSeconds(d.total_active_seconds)`) every render, and stamps `data-shape` with `ENGINE_SHAPES[engineOf(d)].glyph` — the actual discriminator two engines sharing an SVG tag (codex/local, both `polygon`) render differently by. Fill is `laneColor(d.lane)` (reduced opacity once terminal); stroke is `STATUS_COLORS[d.status]`, thicker for `blocked`. A second `.node-ring-tools` circle, sized by `ringWidthForToolCalls(d.tool_call_count)`, rings the shape. The actively-writing pulse (white border, 1.4s ease-in-out) is a CSS keyframe via the `.pulsing` class.

Four more elements per node, toggled by `applyBadges`: `.node-badge-question`(-ring) when `d.pending_question` is non-null, `.node-badge-errors` when `d.error_count > 0`, and `.node-badge-children` on a parent with any direct children in the filtered set (`_totalChildren > 0`) — text `+N` while `N` are hidden, or a collapse glyph (`−`) once fully expanded, so the badge stays a clickable affordance even at zero hidden children (see below) — plus `.node-badge-children-hit` (`r = 16` viewBox units, about 9 CSS px at the default zoom), a transparent circle behind the child-count badge that enlarges its click target beyond the glyph's own rendered size. All are offset from the node centre so they never collide with the label.

### Subagent collapse

`applyCollapse(filtered)` first marks every non-terminal session and its ancestors as an active branch. Active branches remain visible. A terminal-only child branch is dropped unless its parent is in `expandedParents`; a child whose parent was filtered out is treated as a visible root. `totalChildCounts(filtered)` counts terminal direct children for the parent's badge. Clicking the badge (`toggleParentExpanded`, with propagation stopped) toggles that parent and re-renders. `expandAncestorsFor(s)` expands the complete parent chain for search results and deep links. Edges disappear automatically when either endpoint is collapsed.

Hosts have no layout role and produce no nodes or columns. `updateHostOptions` still derives the host filter from the complete snapshot, and host remains visible through node hover and panel metadata.

### Hover card

`#graph-hover-card` is a plain HTML `<div>` (`position: fixed; pointer-events: none`), not a child of the SVG. `showHoverCard(event, d)` fills it (`nodeLabel(d)` as its own first line, then `hoverCardRows(d)`) and unhides it before positioning it — `positionHoverCard` measures the card's rendered size to clamp it inside the viewport, which needs the card laid out (non-`hidden`) first. `hoverCardRows(d)` always renders Duration and Cost; Model, Host, Branch/Cwd, and Last event are each omitted when empty. `positionHoverCard` tracks `mousemove`, offsetting the card from the cursor by 16px and clamping both `left` and `top` so the card never renders past the viewport's right or bottom edge; `hideHoverCard` on `mouseleave`. All three listeners are attached directly on the `.node` `<g>` (not a descendant shape) — `mouseenter`/`mouseleave` don't bubble, so a handler anywhere else would silently never fire. There is no native `<title>` tooltip or `nodeTitle`/`<title>` element.

`nodeLabel(d)` (`graph_encoding.js`) picks the rendered label, first non-empty of: `custom_label` → `label` → `short_label` → `prompt_preview` → `routingLabel(d.routing)` → `session_id.slice(0, 8)`. `label` outranks `short_label`: for a card-linked session, `label` is the linked card's title (`_label_for_session` on the server), more authoritative than an LLM-generated summary. `custom_label`, `label`, and `short_label` are each checked and skipped, independently, whenever they are not a real label but the raw id the row fell back to — equal, by trimmed string equality, to `session_id`, to `session_id` with a `cc:`/`cx:` prefix stripped, or to `task_id`. `model_label` is never a candidate — it renders only as a chip (hover card, panel `.panel-chips`, Hermes routing badge). On the server side, `agent_viz_summary.py`'s `_fallback_label` independently refuses to hand back a raw identifier as `short_label` in the first place — it returns `""` for a whitespace-free input (a session/task id has no word boundaries of its own) or for an input that's genuinely non-empty but tokenizes to zero words, which includes any non-Latin (CJK/Cyrillic/Greek/Arabic) or emoji-only title, not only a raw id; `"Untitled"` is reserved for a genuinely empty/whitespace-only input, where there's no real title in `label` to fall through to. Never emits a literal `'?'`. In practice the `session_id.slice(0, 8)` tail is unreachable: `routingLabel()` returns `'Claude'` for any unrecognized routing and never an empty string, so it always wins before the final fallback is tried — the fallback remains as a safety net, not something a node actually shows.

`web/agents/panel.js` imports `nodeLabel`/`isRawIdValue`/`routingLabel` from `graph_encoding.js` (and re-exports `routingLabel` so `board.js` and `graph.js`'s existing imports keep working) — the panel header, kill-modal target, and rename prefill/cancel all route through the same guard the node and search dropdown use, instead of reading `custom_label \|\| label \|\| session_id` directly. `_startLabelEdit`'s prefill uses the same per-field raw-id guard (not the full `nodeLabel` chain, which would also surface `prompt_preview`/`routingLabel` fallbacks — not appropriate to pre-fill into a rename box) — empty when neither `custom_label` nor `label` is a real name.

`web/agents/graph.js`'s search-results dropdown renders every title through the same guard as the node: `sessionDisplayName` is `nodeLabel`, so the dropdown's sort order and fallback title share one precedence chain, and `searchResultTitle(s, field)` shows the matched field's own value (`custom_label || label` for the label tier, `short_label` for the short-label tier) when that value is non-empty and not a raw id, and the display name otherwise (always, for a summary-tier match). `consider(sessionId, field, snippet)` first checks `isKnownSearchField(field)` (an `Object.prototype.hasOwnProperty` check against `SEARCH_TIER`, not a plain `field in SEARCH_TIER`, which would resolve an `Object.prototype` member name like `"constructor"` to a real property) and returns early for an unrecognized field, so a `/api/agents/search` match naming anything but `label`/`short_label`/`summary` never reaches the DOM or the tier comparison.

The `viewBox` is `1000 × 700` with `preserveAspectRatio="xMidYMid meet"` and `overflow: visible`. The timeline can extend beyond it and remains pannable; **Fit** reveals the complete extent.

### Interaction model

Both link and node layers live inside a single `<g class="viewport">` whose `transform` is mutated by `d3.zoom` (scaleExtent 0.2–5); the zoom handler also stamps `data-zoom-k` on `#graph-svg` with the current scale on every zoom event, so a test (or the operator) can read the live transform without reaching into d3 internals. Because `d3.pointer` returns viewBox-space coordinates when the target carries a `viewBox`, pan tracks the cursor 1:1 without explicit screen↔viewBox compensation.

**Fit / Reset**: `#graph-zoom-fit` computes the bounding box of every visible node's position plus its radius and label footprint in the SVG's `1000 × 700` viewBox coordinate space, then derives a transform that centers and fits it. `#graph-zoom-reset` applies `d3.zoomIdentity`. Both transition over 300ms, or instantly under `prefers-reduced-motion: reduce`.

**Label legibility**: a `.node-label`'s actual on-screen CSS pixel size is its font size (12px, in the SVG's own user-space units) times BOTH the zoom transform's scale (`k`) AND `#graph-svg`'s real user-space→screen scale. `#graph-svg` carries `preserveAspectRatio="xMidYMid meet"` and sits next to `#panel-outer` (the side panel), so that real scale is `min(rect.width / VIEW_W, rect.height / VIEW_H)` — the SMALLER of the width and height ratios, since a `meet`-fit SVG letterboxes to whichever dimension is tighter — never the width ratio alone, and well under 1 at any realistic viewport, not the ~1 a `k`-only calculation would assume. `updateLabelLegibility(k)`, called from the zoom handler on every zoom event (including the interpolated frames of a Fit/Reset transition), from `renderGraph` after every node/label (re)render, and from a `ResizeObserver` on `#graph-svg` (the screen-scale ratio changes with the side-panel-resizer drag or a window resize, independent of any zoom event), computes that real scale (`svgScreenScale()`) and the natural on-screen size at the current `k`. When that's already at or above `LABEL_MIN_SCREEN_PX` (`11`), every `.node-label`'s font-size is left at the CSS default (boost factor `1`). Below it, each label's font-size (in SVG user-space units) is set just high enough to hold the on-screen size at the floor — counter-scaling against the combined transform — unless the needed size exceeds `LABEL_MAX_BOOST_PX` (`36`), at which point no reasonable font-size avoids the labels themselves overlapping and cluttering a dense, zoomed-far-out graph, so `updateLabelLegibility` toggles a `labels-below-legible` class on the node layer (`<g class="nodes">`) instead — CSS then hides every `.node-label` under that class — and relies on the hover card (`nodeLabel(d)`, independent of the node's own DOM label) to name a node. The identity transform (`k = 1`, what Reset restores) counter-scales the same way every other `k` does, so labels stay at the legibility floor at the resting zoom level too, not just above it.

The current boost factor (`_labelBoost`, `1` when unboosted or hidden) drives label line spacing: `applyLabelBoost(boost)` re-derives the `y` gap from its node and each non-first tspan's `dy`, so multi-line labels remain readable. A relabel (`onLabelSaved`/`onSummaryFetched` in `initGraph`) re-runs `renderNodeLabel` for that node and then reapplies the current boost. Timeline nodes are not draggable because their coordinates carry meaning.

**Click** toggles/opens the panel synchronously — no timer. `event.detail > 1` (the browser's own click count for the second click of a double-click) short-circuits the handler, so only the pair's second click is ignored — the first click still opens or closes the panel exactly as a standalone click would. **Double-click** on a node whose `engineOf(d)` is `claude_code` or `codex` and that isn't a subagent per `isSubagentSession(d)` (`session_actions.js`, re-exported from `panel.js` — true for either a worker-flagged subagent or a routing-derived one with no flag but a `parent_session_id`) calls `focusSessionQuick(d)` (`POST /sessions/{id}/focus`), first re-opening the panel for that session when the pair's own first click toggled an already-selected node's panel closed (`selectedSessionId !== d.session_id`), so a double-click on a selected CLI node still leaves its panel showing; `svg.on('dblclick.zoom', null)` disables d3-zoom's own double-click-to-zoom so a double-click anywhere on the canvas — including a non-CLI node — never also changes the zoom transform.

Every render assigns coordinates directly from `delegationTimelineLayout`; there is no settling or size-driven movement. A local filter change resets pan/zoom before rendering the new deterministic set. Shared-filter changes preserve pan/zoom so typing in search or tag controls does not fight the operator's viewport.

Selection state is tracked in `selectedSessionId` and re-applied on every render plus on `openPanel`/`closePanel` via `applySelectionStyles()`. The helper walks the selected node's connected delegation tree and toggles `.selected` (5px white border), `.related` (3px translucent white), and `.dimmed` classes on nodes, labels, and edges. Clicking the SVG background closes the panel; clicking an already-selected node toggles it off.

### Transcript event rendering

`prettyPayload(payload)` is a field-aware formatter shared by backfill + live tail. It recognizes routing decisions (`routing` + `routing_reason`), assistant text (with a `(no text — called tools)` placeholder when `text === ""` alongside non-empty `tool_uses`), tool-call pills (`Name(input_keys)`), labeled text fields (`question`, `answer`, `prompt`, `reason`, `ambiguity`, `sane_reason`, `description`, `label`), compact `budget` and `usage` lines, scalar badges (`model`, `task_id`, `expected_output`, `speed`, `service_tier`, `source`, parent/child ids), and a `pp-extra` tail for unrecognized scalars. Noisy fields (`iterations`, `inference_geo`, `server_tool_use`, the nested `cache_creation` ephemeral buckets, zero `thinking_chars`) are suppressed. Click-to-expand reveals the raw JSON in a sibling `<pre class="payload-raw">` for diagnostic inspection.

---

## Card metadata and cross-tab linking

`web/agents/linking.js` is the one shared surface between the Board and Graph tabs — a persisted filter store and a cross-tab focus/tab-activation bus. It owns no DOM: `board.js` and `graph.js` each bind their own filter controls to it and drain their own pending focus intent, applying it to their own view.

`card_id`, `card_title`, and `host` remain fields on each session row. They drive labels, filters, hover/panel metadata, and cross-tab navigation, but do not create graph nodes or edges. This keeps execution location and work-item identity available without mixing either into delegation topology.

### Card actions (`#graph-panel-actions`)

A `<div id="graph-panel-actions">` sits inside `#panel-outer`, above the transcript panel proper, and is populated by `renderPanelActions(session)` from the selected session. It also calls `setSelectedGraphCardId(session.card_id || null)` (`linking.js`), while `closePanel` clears that value. This is the persistent selection hint `board.js` reads when the Board tab activates with no pending focus intent:

- **Show on board** renders whenever `source.card_id` is non-null; its click calls `requestBoardFocus(cardId, {openDrawer: false})` then `activateTab('board')`.
- **Answer** is rendered by `SessionPanel`'s shared action row whenever the selected session has a pending question, using the same prompt and endpoint as the board drawer.

`closePanel` and `openPanel` clear or repopulate this container alongside the transcript panel and `selectedSessionId`.

### Shared filter store

`getFilters()`/`setFilter(key, value)`/`setFilters(partial)`/`resetFilters()`/`subscribe(fn)` hold the seven shared keys — `search`, `lanes` (array of lane ids), `assignee`, `host`, `engine`, `tag`, `recency` — as one object, persisted whole under `localStorage['lifeos.agents.filters.v1']`. `setFilter` and `setFilters` both replace `filters` wholesale (never mutate in place) so every `subscribe` callback, including the caller's own, sees a fresh reference and reconciles its controls against the passed-in state rather than assuming it's someone else's change; `setFilters` applies several keys and notifies subscribers exactly once, which callers relaxing more than one key for the same target (`relaxSharedFiltersFor` in `graph.js`, `revealCard` in `board.js`) use instead of one `setFilter` call per key — a subscriber's own synchronous reconciliation of an earlier key, run inside that earlier call, would otherwise read a `filters` snapshot that doesn't have the later keys' changes yet and write a control back to a now-stale value. Reads and writes are wrapped in try/catch; a corrupt or missing value falls back to defaults.

`DEFAULT_FILTERS.recency` is `null` — "the operator has never set it" — rather than a concrete value, since the two tabs disagree on what a default recency window should be (the board's is all time, the graph's is its own auto-computed window). Each tab treats a `null` shared value as its own default and a concrete value (written by an explicit change on either tab, via `setFilter`/`setFilters`) as one both tabs then honour identically; `null` round-trips through `JSON.stringify`/`JSON.parse` and survives a reload the same way. See [Kanban board — Filters](../product/agent-viz.md#filters) / [Graph tab — Filters and chips](../product/agent-viz.md#graph-tab--filters-and-chips) for what each tab's default resolves to.

`DEFAULT_FILTERS.lanes` is every lane but Done — the board's own default lane selection. On first load, when the v1 key is absent, `loadFilters()` seeds `lanes` from the board's own legacy key (`lifeos.agents.board.lanes`, read once as a bare string literal — importing it from `board.js` would form an import cycle, since `board.js` imports FROM `linking.js`) if that key holds a value, so an operator's already-customized lane selection survives; `board.js` writes only to the shared store. `sanitizeLaneIds` drops any id that doesn't name a current lane but keeps a deliberately-empty selection (`[]`) as one — the same tolerance the board-only lane store has for the legacy key it's seeded from.

Both tabs' filter controls are two-way bound to the store: a control's own `change`/`input` listener calls `setFilter`, and each module's `subscribe` callback writes every other control and re-renders from the state passed in. The graph's `#filter-route` is the shared engine control; `#filter-lane` maps its single selection onto the shared lane array. `applyFilters` reads lane, assignee, tag, engine, host, and search directly from the shared store. A session with no lane is not excluded by lane filtering, while the Done lane remains governed by **include finished**. The board's engine match uses `routingFilterValue`, preserving distinct routing values. Shared search matches `nodeLabel`, `short_label`, `card_title`, and `card_tags` before the timeline is laid out.

`host` is the one shared key whose `<select>` options are populated asynchronously from live snapshot/board data rather than static markup (`updateHostOptions` in `graph.js`, `updateFilterOptions` in `board.js`), which the two-way binding above has to account for: a sync attempted while the real option list is still just the static `all` fallback silently fails to apply a persisted or cross-tab value (assigning an absent value to a `<select>` coerces it to `""`, which a naive read then treats as "all"). Both option-population functions re-check the shared `host` value once they rebuild the option list, preferring it over the select's own prior value when it's now a valid option — so a `host` filter restored from `localStorage`, or set from the other tab, lands as soon as its option actually exists. Host changes filter sessions but never reposition them into machine columns.

`DEFAULT_FILTERS.recency` is `null` (see above) — the graph tab reads its OWN `#filter-recency` select directly in `applyFilters` (never the shared store) and keeps deciding its own default via `applyRecencyDefault` (30 min off / 7 days on, gated by module-level `recencyManuallySet`) for as long as the shared value stays `null`. `graph.js`'s `syncSharedFilterControls` sets `recencyManuallySet = true` (and assigns `filterRecencyEl.value`) only when `state.recency` is a concrete value; when it's `null` it resets `recencyManuallySet = false` and calls `applyRecencyDefault()` again instead, so a Clear (or a fresh install) restores the auto-default rather than getting stuck on whatever the last concrete value happened to be. An explicit change on either tab's own recency control routes through `setFilter('recency', …)` — including `relaxFiltersFor`'s own recency-widening branch, which writes through `setFilter` rather than the select directly so the widened value reaches the shared store (and therefore the board and `localStorage`) rather than only the DOM.

### Cross-tab focus and deep links

`requestGraphFocus(sessionId)`/`takeGraphFocus()` and `requestBoardFocus(cardId, {openDrawer})`/`takeBoardFocus()` are a tiny pending-intent store — `request*` sets it, `take*` reads and clears it in one call, so a caller who takes something finds nothing pending on a later drain. `onTabActivate(fn)`/`activateTab(name)` is a parallel tab-activation pub/sub `web/agents.html`'s own tab-switch code (and the deep-link handler below) calls into; `board.js`/`graph.js` each subscribe to drain their own intent when their tab activates.

A drain (`graph.js`'s `drainGraphFocus`, `board.js`'s `drainBoardFocus`) can fire from an `onTabActivate` callback BEFORE that module's own first data fetch (`/api/agents/snapshot`, `GET /api/agents/board`) has resolved — a lazily-initialized Graph tab activated by a session chip click registers its `onTabActivate` subscriber, and possibly runs it, before `fetchSnapshotOnce()`'s promise settles. Resolving against an empty `allSessions`/`board.lanes` at that point would wrongly report a real session or card as unknown and consume the intent (via `take*`) before the data that would have resolved it correctly exists. Both drains guard against this with a loaded flag (`snapshotLoaded` / `boardLoaded`, set inside `applySnapshot`/`applyBoard`): when the intent is taken but data hasn't loaded yet, the drain calls `request*Focus` again (re-queuing it) instead of resolving, and returns — `applySnapshot`/`applyBoard`'s own call to the same drain function, once their data has actually landed, is what resolves it. Whichever drain call finds the intent first (already-loaded tab activation, or the first post-load payload) wins; the other finds nothing pending, which is also what makes "resolve exactly once, never re-toast on every tick" hold — the intent is consumed on whichever `take*` call is the first to see it as non-null, resolution or toast either way.

`focusNode(sessionId)` (`graph.js`) relaxes whichever LOCAL filters (`relaxFiltersFor` — recency (via `setFilter`, see above), cwd, status) and SHARED filters (`relaxSharedFiltersFor` — lane/assignee/host/engine/tag/search, via the batched `setFilters`) currently hide the session, expands any collapsed ancestor, then `panToNode` + `openPanel`; `selectSearchResult` (the search dropdown's own click/Enter handler) is a thin wrapper that just calls `hideSearchResults()` then delegates to it, so a dropdown result for a session hidden by a shared filter — including one that matched only via the server-side transcript-summary search, not `sessionMatchesSearch`'s own label/tag fields, in which case `relaxSharedFiltersFor` clears the shared `search` rather than leaving it permanently unreachable — resolves the same way a card-chip jump or a `?session=` deep link does. An id that doesn't resolve to a known session calls `showToast(..., true)` and calls `activateTab('board')`, returning to the default view rather than stranding the operator on a Graph tab that never resolved to anything.

`revealCard(cardId, {openDrawer})` (`board.js`) is the board's counterpart: it computes, then applies through one batched `setFilters` call, whichever of the seven shared keys (lanes, assignee, host, engine, tag, search, recency) currently hide the card — `setFilters` notifies synchronously, so by the time it returns `board.js`'s own `syncSharedFilterControls` subscriber has already re-rendered against the widened filters — then queries the DOM for the card element, and only `scrollIntoView({block: 'nearest'})`s and highlights it once that query confirms it's actually there (a board-local filter, e.g. context or include-cancelled, can still hide it — those are left as-is, the operator set them on purpose, and revealing does nothing further in that case). The highlight itself is tracked in module state (`revealedCardId`, a ~2s `revealHighlightTimer`), not just added to the DOM node once: `renderTaskCard`/`renderScheduleCard` stamp `.reveal-highlight` onto a freshly-built card element whenever its id matches `revealedCardId`, so a `render()` mid-window (a board-stream SSE tick rebuilds every card element from scratch, and reliably fires once on a cold `?card=` load right after the initial `GET /api/agents/board`) re-applies it instead of silently losing it. An unknown card id toasts and returns.

Activating the Board tab itself can also trigger a reveal, independent of any pending `request*Focus` intent: `linking.js` tracks `selectedGraphCardId`, a persistent value `graph.js` updates on every session selection change. `board.js` checks for a pending explicit intent first and otherwise reveals that selected session's linked card, if any.

`web/agents.html`'s boot script parses `location.search` once: `?session=<id>` calls `requestGraphFocus(id)` then `activateTab('graph')`; `?card=<id>` calls `requestBoardFocus(id, {openDrawer: true})` then `activateTab('board')` — the intent is always stored before `activateTab` fires, so it's already there for whichever drain (tab-activation or first-payload) resolves it first.

### Board→graph and graph→board jumps

A task card's session chip (`.board-chip-session`, rendered by `cardChips` whenever `card.session` exists) `stopPropagation()`s its click so it never also opens the drawer, then calls `requestGraphFocus(card.session.session_id)` + `activateTab('graph')`. The graph's `renderPanelActions` "Show on board" button is the reverse direction, as is switching to the Board tab while a card-linked session stays selected.

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
2. The actual subtree teardown is `_kill_session_subtree(target, reason)` (`api/routes/agents.py`) — shared with `POST /board/cards/{id}/cancel`, which needs the identical mechanics on the card's linked session. It:
   - Walks the subtree via `_collect_subtree(session_store, target)` — BFS from the target through `parent_session_id`, **not** from `root_session_id`. Non-root targets only take down their own descendants, leaving unrelated peers under the same root alone.
   - For each session in the subtree (target first, then descendants): skips already-terminal entries, emits `operator_killed` (target) or `cascade_killed` (descendants) to the transcript, and calls `api.services.agent_worker.inter_agent.teardown_session(...)` to actually mark the session terminal in the store and tear down the managed remote if one exists.
   - Managed-Agents teardown uses a `ManagedAgentsDriver` instance constructed lazily from `settings.anthropic_api_key`. If the key isn't set, kill degrades to local-only and the worker's next managed poll reconciles the remote side.
   - For a session whose `host` is set, `teardown_session` signals the process over ssh (`ssh <target> kill -- -<pgid>`, the `<pgid>` recorded from the remote executor's spawn — see [Card assignment](agent-worker.md#card-assignment-851)) instead of the local `os.killpg` path. A missing `remote_pgid` or an unregistered host degrades to a DB-only kill, the same as a missing local pid event does.
3. `operator_kill_session` itself is the 404/terminal pre-checks plus a call into `_kill_session_subtree` — no teardown logic lives in the route handler directly.

Response: `{killed: [session_ids], failures: [{session_id, reason}]}`, or `{killed: [], failures: [], reason: "already <status>"}` when the target is already terminal. Cancel's teardown call passes reason `"cancelled from the board"` and folds `killed`/`failures` into its own response alongside `id`/`lane`/`status`/`tags` — see [Card action policy](#card-action-policy).

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
- [Agent Worker Setup — Guide](../../guides/agent-worker-setup.md#remote-session-parity-transcript-mirror) — Operator setup for the host registry and the transcript mirror's settings/prerequisites
