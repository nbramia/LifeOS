# Agent Activity Visualization (`/agents`)

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-04

`/agents` is a Kanban board of the operator's work queue — vault tasks, agent questions, and scheduled work in one place, organized into lanes by status and tag. A **Graph** tab keeps the earlier force-directed session graph as a secondary, read-mostly view for watching what's actively running: every LifeOS agent worker task (`#agent`-tagged), local CLI sessions discovered on the filesystem from both Claude Code (`~/.claude/projects/`) and Codex (`~/.codex/sessions/`), and Claude Code / Codex sessions registered from **any other machine** on the tailnet via a lightweight hook script.

The point is one place to see what needs attention: what's waiting on an assignment, what an agent is stuck asking about, what's scheduled to run next, and — when you want to watch the machinery — what's actually executing right now.

---

## Table of Contents

1. [Kanban board](#kanban-board)
2. [Graph tab — what you see](#graph-tab--what-you-see)
3. [Two sources, one graph](#two-sources-one-graph)
4. [Graph tab — Status semantics](#graph-tab--status-semantics)
5. [Graph tab — Filters and chips](#graph-tab--filters-and-chips)
6. [Graph tab — Side panel](#graph-tab--side-panel)
7. [Graph tab — Operator controls — kill](#graph-tab--operator-controls--kill)
8. [Graph tab — Operator controls — resume and Go To](#graph-tab--operator-controls--resume-and-go-to)
9. [Privacy and exposure](#privacy-and-exposure)
10. [Configuration knobs](#configuration-knobs)
11. [Related Documents](#related-documents)

---

## Kanban board

The board is backed by the vault task store (`LifeOS/Tasks/`) — every card is a task, plus one card per upcoming scheduler entry. There is no separate "board" data file: a card's lane is always derived fresh from the task's status and tags, so editing a task from Obsidian, `/chat`, or a Telegram reply moves its card exactly as if it had been dragged.

### Lanes

| Lane | What lands here |
|---|---|
| **Unassigned** | An open task with no assignee tag. |
| **Assigned** | An assignee tag is set (including `#me`) but work hasn't started. |
| **In progress** | Status `in_progress`, or the agent worker's own `#agent-running` tag. |
| **Human queue** | An agent is blocked on a question, or a `#human` card was filed for the operator, or the task's status is `blocked`. |
| **Scheduled** | A scheduler entry (`docs/guides/scheduler.md`) with at least one future fire. |
| **Review** | The agent worker's `#agent-completed` tag is set and the card hasn't been accepted yet. |
| **Done** | Status `done` or `cancelled` (cancelled cards are hidden behind the "include cancelled" filter by default whenever the Done column is shown), plus scheduler entries that have fired (one-off) or been disabled (recurring). Hidden by default in the lane filter below — the least useful lane day to day. |

### Assignee

Assignee is a single tag, one of `#me`, `#claude`, `#codex`, `#hermes`, `#local`. Dropping a card into Assigned sets that tag and clears any other assignee tag; dropping into Unassigned clears it. Setting an assignee here is a labeling action only — it does not dispatch the task to that engine. The drawer's **Open** action starts a CLI session on an Assigned `#claude`/`#codex` card explicitly, and the worker reads the card's model/effort/host fields when it claims an `#agent` task (see [Card assignment](../technical/agent-worker.md#card-assignment-851)); today the agent worker still only claims tasks carrying its own `#agent` tag, same as before this board existed.

### Cards and the drawer

A card shows its title, assignee chip, model/effort chips when the task carries those fields, a host chip when a linked session is running on a known machine, its other tags, and a pulsing dot when a linked session is actively running.

Clicking a card opens a drawer: an editable title and notes (notes save on blur, stored as indented `> ` lines beneath the task — see [task-management.md](task-management.md)), pickers for assignee, tags, and context, and — below those — model, effort, and host pickers for engines that accept them (`#claude`/`#codex` show all three, `#local` shows effort only, `#me`/`#hermes`/unassigned show none; model options come from the model catalog per engine) that write the fields the executors actually read. Host is a dropdown of the known machines — the API host plus every registered host, each labeled `(offline)` or `(unknown)` when it isn't reachable, sourced from Tailscale where available — rather than free text; an empty choice ("this machine") means the card runs wherever the API does, and a card whose saved host has since dropped out of the registry still shows it, labeled `(unknown)`, rather than silently losing the value. A picker save that actually fails shows a toast with the reason; a successful save never does. On a task card, the notes field grows with its content as you type (and when the drawer opens on a card with existing notes), up to two-thirds of the viewport height, after which it scrolls internally rather than growing further; a Scheduled card's message field (see [Scheduled column](#scheduled-column)) keeps a fixed box. When the card has a linked session, the drawer also shows that session's live transcript feed, the same panel the Graph tab uses. Drawer actions: **Open** (an Assigned card tagged `#claude` or `#codex` spawns the CLI on the card), **Focus** (jump to the session's terminal pane), **Kill** (stop a running session), **Answer** (reply to the agent's pending question), **Accept** (move a Review card to Done), and **Resolve** (mark a manually-filed Human queue card handled). Clicking anywhere outside the drawer — the board background, a lane, or another card — closes it exactly like its close button; a click inside the drawer never does. Escape closes the drawer, but does nothing while the New card composer or the Answer prompt is open on top of it.

A **New card** button in the filter bar opens a composer — title, optional notes, a lane picker, and an assignee picker — that creates a task. Each visible lane also carries its own full-width **+** button above its cards, opening the same composer with that lane preselected. A few rules govern how Lane and assignee interact:

- Picking an assignee while Lane still reads Unassigned flips Lane to Assigned, since a task carrying an assignee tag always files there regardless of what Lane says; manually overriding Lane back to Unassigned afterward doesn't change where the card lands.
- Clearing the assignee back to blank while Lane reads Assigned flips Lane back to Unassigned.
- Picking Assigned (from the top-bar button or a lane's own **+**) requires an assignee; the created card carries it as a tag.
- Picking In progress with an agent assignee (`#claude`/`#codex`/`#hermes`/`#local`) is rejected before anything is created — only `#me` can be assigned directly to In progress, since the worker claims agent-assigned tasks itself.
- Review and Scheduled don't get a **+** — neither lane can be set directly; a card reaches Review or Scheduled the same way it always has (the worker's own tags, or the scheduler).
- Creating a card straight into a lane the filter is currently hiding reveals that lane — and persists the change to the saved filter selection — so the new card is actually visible.

### Pending questions

When an agent asks a clarifying question, the card carrying that session shows the question text and an **Answer** button in the drawer. Answering writes the reply through the same path a Telegram reply takes — the worker resumes the session on its next tick exactly as if you'd answered by text.

### Scheduled column

Each card shows the entry's next fire time, a recurring badge for cron entries, and — once it has fired at least once — the most recent run's outcome and a short result snippet. The drawer's title, message, and an enabled checkbox are editable and save on blur/change through the same `PUT /api/scheduler/{id}` the `/api/scheduler` UI uses — there's no separate write path for the board. Schedule type, timing, and executor are not editable from the board; use the existing scheduler UI for those.

### Filters

Which lanes show at all is a multi-select: a checkbox per lane in a dropdown, plus **All** and **Clear** controls (Clear resets to the default: every lane except Done). An unchecked lane's column is removed from the board entirely, not just emptied of cards, so the remaining lanes widen to fill the space; re-checking it puts it back in canonical lane order. The selection is remembered via `localStorage` (per browser/device, not synced) and restored on your next visit; if nothing at all is checked, the board shows a one-line hint instead of going blank.

The rest of the filters AND-compose on top of whichever lanes are showing: free-text search (title and notes), assignee (including "me" and "unassigned"), host, tag, context, recency, and whether to include cancelled cards. The board updates live — an edit made directly in the vault (or by the agent worker, or by the scheduler) shows up within a few seconds without a page reload.

### Out of scope (for now)

Card reordering within a lane — file order is lane order. See the Kanban overhaul issue set for what's next.

---

## Graph tab — what you see

The Graph tab is a force-directed graph of sessions, laid out left-to-right by recency. Each node is one session:

| Encoding | Meaning |
|---|---|
| **Shape — circle** | Cloud agent — routed to Claude (`routing: claude`) |
| **Shape — diamond** | Local agent — routed to local Gemma (`routing: local`) |
| **Shape — rounded square** | CLI session — Claude Code (`source: claude_code`, ids prefixed `cc:`) or Codex (`source: codex`, ids prefixed `cx:`). Tell them apart via the `source` badge in the side panel or the `model_label` chip on the node. |
| **Color** | Status — green running, blue claimed, amber blocked / paused, grey done, red failed |
| **Size** | Log-scaled by total tokens (input + output + cache). A 100k-token session is roughly 2× a 1k-token session, not 100×. Capped so one fat node can't dominate the canvas. |
| **White pulsing border** | Session is `running` AND has written to its transcript in the last 60 seconds (i.e. *actively producing output right now*) |
| **Edge** | Spawn relationship — parent → subagent |
| **X position** | Last-activity recency. Most recent sessions to the right, ≥24h old pinned to the left. The recency rail compresses around center when few nodes are visible — one filtered-down node ends up centered, two sit on a narrow band — and stretches to the full width as more nodes appear. |
| **Edge styling** | Plain curved paths (no arrowheads) between parents and subagents. When a node is selected, edges adjacent to it brighten to white. |

Shape encodes *where the agent runs*, not whether it is a subagent — a Task/Agent-tool subagent takes the same shape as any other session with its routing (a Claude Code subagent is a rounded square, like its parent). Subagents are distinguished by the spawn edge connecting them to their parent, not by shape.

The simulation converges in ~8 seconds and then stops, so the graph stops jittering once it settles. New snapshots arrive every 2 seconds and only nudge nodes whose positions are now misleading.

**Node label** — the text under each node, first non-empty of: an operator-pinned custom label, the AI-generated short summary, the derived label (task description for LifeOS, first non-empty user message for Claude Code), the most recent prompt preview (cross-machine CLI sessions), the routing/model badge, then the session id as a last resort. The AI-generated short summary and the derived label are each skipped when they're not a real label but the raw id the row fell back to (the session id, that id with its `cc:`/`cx:` CLI prefix stripped, or the row's task id) — including a short summary the summarizer itself derived from that same raw id, which it also declines to reformat and hand back (#863 review round 2, finding M). A node never renders a bare `?`; in practice the routing/model badge always resolves to something, so the session-id fallback is a safety net rather than something you'll see on screen.

### Canvas controls

The graph mirrors `/crm/graph`'s pointer model:

- **Drag a node** — pins it where you drop it. Useful when you want to inspect a busy cluster without the simulation nudging things around.
- **Drag the empty background** — pans the whole graph.
- **Scroll-wheel / pinch** — zooms in and out (0.2× – 5×).
- **Click a node** — opens its transcript in the side panel and highlights its parent/child relationships (selected node gets a thick white border, 1-hop neighbors get a thinner white border, everything else dims).
- **Click the same node again, or click empty background** — deselects and closes the panel.
- **Filter change** — releases any drag-pinned positions and resets the pan/zoom transform so the new visible set lays out from scratch at the natural scale.

Between filter operations the simulation **freezes after settling** (~6s). Snapshot ticks every 2s only re-energize the layout if the visible-id set actually changed — new session appeared or one dropped out. Same-set snapshots leave settled nodes alone, so the graph no longer jitters every couple seconds at rest.

---

## Two sources, one graph

The Graph tab unions three ingest paths into one rendered surface:

1. **LifeOS agent worker** — every `#agent` task the worker has claimed, plus its sleeps, yields, terminal outcomes, and any spawned children. This is the same data covered by [product/agent-worker.md](agent-worker.md); the viz is the read-side view of it.

2. **Claude Code CLI** — every transcript jsonl under `~/.claude/projects/`, scanned every snapshot tick (with a 30s cache so the disk isn't hammered). Each `.jsonl` file is one session; subagents spawned via the Task/Agent tool appear as separate nodes attached by spawn edges. Read-only.

3. **Codex CLI** — every rollout jsonl under `~/.codex/sessions/<year>/<month>/<day>/`, ingested the same way. One JSONL per session, `cx:`-prefixed in the snapshot. Read-only.

All three sources are normalized to the same shape before rendering, so filters, chips, and the side panel work identically on each kind of session. Disable an ingest path with `LIFEOS_CLAUDE_CODE_VIZ_ENABLED=false` or `LIFEOS_CODEX_VIZ_ENABLED=false` if you only want a subset.

Every session, from every source, now carries a `host` field — the machine it's running on. LifeOS agent worker sessions and locally-scanned CLI transcripts always report the machine hosting the API; a session registered from elsewhere (see below) reports its own hostname.

### Cross-machine CLI session registration

A Claude Code or Codex session doesn't have to run on the machine hosting the API to show up here. `scripts/lifeos-agent-hook.sh`, installed for both CLIs by `scripts/install-agent-hooks.sh`, posts a lifecycle event on session start, prompt submit, stop, and session end to `POST /api/agents/cli-sessions/events` — from any machine on the tailnet, bearer-token authenticated. The API keeps a small `cli_sessions` record per session (host, cwd, branch, model, status, last prompt preview, and an optional task id read from `$LIFEOS_TASK_ID`) and merges it into the snapshot:

- A session with both a registration and a local transcript (the common case on the API host itself) collapses into **one row** — status comes from the registration events (accurate: `running` right after a prompt, `idle` after Stop, `ended` after SessionEnd), while token counts and dollar cost still come from the transcript.
- A session registered from a machine with no local transcript (every other machine) appears as its own row with `host` set to that machine's name, no token/cost detail (the hook doesn't read usage data), and status directly from the event stream — never inferred from file age.

This is opt-in: the endpoint is disabled (503) until an operator sets `LIFEOS_AGENT_HOOK_TOKEN`, and each machine needs the installer run once plus a small local env file with the API URL and that same token. See [guides/agents-go-to.md](../../guides/agents-go-to.md) for setup.

---

## Graph tab — Status semantics

A node's color is its status. The set is slightly different per source — same broad categories, different precise meaning:

| Status | LifeOS agent worker | CLI (Claude Code or Codex) |
|---|---|---|
| **running** | Currently executing tool calls or LLM turns. | A live `claude` / `codex` process is running with this jsonl's cwd, **or** the file was modified in the last 10 minutes. The first is authoritative; the second is inferred. |
| **claimed** | Worker has picked up the task but hasn't fired the executor yet (preflight is in flight). | n/a |
| **yielded** | Paused waiting for spawned children to finish. | n/a |
| **idle** | n/a | Registered via the session hook (#849): open and waiting for input, after a `session_start` or `stop` event. Live, not finished. |
| **inactive** | n/a | Modified within 24h but no live process — typically you closed the terminal mid-session. Resumable. |
| **blocked** | Waiting on a Telegram clarification from you. | n/a |
| **completed** | Task ran to completion successfully. | jsonl is >24h old, no error in the last event. |
| **failed** | Executor crashed, preflight rejected, or runtime error. | Last event in the jsonl was an error/tool failure (and >24h old). |
| **ended** | n/a | Registered via the session hook (#849): a `session_end` event was received — finished. Hidden by default like completed/failed; Resume available. |
| **budget_exceeded** | Token / wall / dollar cap breached and the session was killed externally. | n/a |

A small `(inferred)` hint appears next to the status on CLI sessions whenever the status came from mtime rather than from a confirmed live process — useful to know when reading "running" on a session you don't remember starting.

---

## Graph tab — Filters and chips

The top toolbar has six filter controls and five count chips. **Filters are AND-composed**; the chips reflect *what's currently visible* after the filter, not the full snapshot.

### Filters

| Filter | Default | Notes |
|---|---|---|
| `include finished` checkbox | off | Off → completed / failed / budget_exceeded / ended are hidden. On → everything shows, and the default recency window widens from 30 min to 7 days. |
| `recency` dropdown | last 30 min (60 min, 6h, 24h, 7d, all) | Filters by `last_activity_at`. Re-defaults to a wider window when `include finished` is enabled, unless the operator has set it manually. |
| `cwd` dropdown | all | Only Claude Code sessions are scoped to a cwd. Dropdown lists every unique cwd present in the current snapshot; auto-hides when empty (no Claude Code sessions visible). |
| `host` dropdown | all | Limit to sessions running on a specific machine. Dropdown lists every unique `host` present in the current snapshot; auto-hides on a single-host deployment (nothing to distinguish). |
| `route` dropdown | all (local / claude / claude_code / codex / hermes / remote / ask) | Filters by where the session ran — operator's local LLM, Managed Agents cloud, Claude Code CLI, Codex CLI, Hermes, the configured remote provider, or a session parked waiting on the operator. |
| `status` dropdown | all | Hard-filter by the status column from the table above. |

### Chips

| Chip | What it counts |
|---|---|
| `running` | Visible sessions with status `running`. |
| `blocked` | Visible sessions waiting on Telegram clarification. |
| `recent` | Visible sessions with status `completed` or `ended`. |
| `cc` | Visible CLI sessions (Claude Code and Codex rolled together). |
| `API spend` | Sum of `total_dollars` across visible **LifeOS** sessions. Both CLIs are intentionally excluded — they're billed against your Claude Pro / ChatGPT subscriptions, not metered API tokens, so adding them would distort the chip's meaning. The per-session dollar columns on CLI nodes still show the equivalent API cost as a relative-cost signal. |

Chips re-compute after every snapshot tick, so toggling `include finished` immediately bumps the API-spend number to include the finished sessions' final cost.

---

## Graph tab — Side panel

Clicking any node opens a panel on the right with that session's metadata header and a live-tailing event feed. The panel header carries:

- **Label** — the operator-pinned custom label if set, else the derived label (task description for LifeOS, first non-empty user message for Claude Code), else the session id. The AI-generated short summary and the most recent prompt preview (cross-machine CLI sessions) are shown as their own separate rows below the header, not folded into this name — see the **Node label** precedence in [Graph tab — what you see](#graph-tab--what-you-see) for the fuller chain the *graph node* uses instead. **Click it to rename:** the title becomes a text box prepopulated with the current name; Enter (or clicking away) saves, Escape cancels. A manual name is pinned durably and overrides every other source everywhere the node is named (graph node, panel, search). Saving an empty value clears the override and reverts to auto-naming.
- **cwd** — Claude Code only; the project directory the session was opened in.
- **Branch** — the git branch of that cwd, when a registration event supplied one. Blank for sessions with no cross-machine registration (e.g. a local Claude Code transcript with no hook installed).
- **Status badge** — same status the node is colored by, with `(inferred)` if applicable.
- **Source** — `LifeOS agent` or `Claude Code`.
- **Host badge** — the machine the session is running on.
- **Routing** — a plain badge, one of `Local`, `Claude Code`, `Codex`, `Remote`, `Hermes`, `Ask` (parked waiting on the operator, no model running), or `Claude` — never a model name. (#863 review) The graph node's `model_label` badge is the richer one for most routings — `Remote` becomes the configured remote provider's own label — but `Hermes` stays plain everywhere. (#863 review round 2, finding O) An earlier revision of this doc described `Hermes` gaining a `Hermes · <model>` suffix from the model Hermes last reported; that value turned out to be a single process-wide "last observed" reading, not scoped to the session on screen, so a finished session's badge could show a model an unrelated Hermes turn reported. The suffix was dropped rather than shipped with that caveat. See **Node label** in [Graph tab — what you see](#graph-tab--what-you-see).
- **Cost** — `total_dollars` to 4 decimals. For Claude Code, this is cache-aware accounting (separately tracking input, output, cache_creation @ 1.25× and cache_read @ 0.10×).
- **Tokens** — `input↓ / output↑`.
- **Depth badge** — if the session is a child, shows spawn depth.
- **Last prompt preview** — the most recent prompt submitted, truncated to 200 characters, when a registration event supplied one.

The event feed is newest-on-top. Backfill arrives first (the last 50 events by default), then live updates stream in via SSE. Each event has:

- A **kind label** — click to filter the feed to just that kind (e.g. `tool_call`, `user_message`, `failed`). Click again to clear. When a session first opens with any `user_message` events present, the filter auto-defaults to `user_message` to focus the view on the operator-visible turns.
- A **timestamp** in your local timezone.
- A **payload preview** — rendered as structured fields (model badge, routing decision, tool-call pills `Name(arg, arg)`, compact budget `90s · $0.30 · 500k tok`, usage summary `↓ in · ↑ out · cache read/create`, free-text fields like `ambiguity` / `question` / `reason`). Noisy fields (`iterations`, nested `ephemeral_*`, etc.) are suppressed. Click anywhere on the event to expand — the raw JSON appears beneath the structured view for diagnostics; click again to collapse.

The panel is **resizable**: drag the left edge to widen or narrow it. The chosen width is remembered across sessions via `localStorage`.

Clicking the same node again, the `×` button, or empty background area closes the panel and clears the graph selection. You can click another node directly to switch focus.

---

## Graph tab — Operator controls — kill

LifeOS agent sessions in non-terminal states get a red **Kill** button in the panel header. Clicking it opens a confirmation modal asking for an optional reason (logged to the transcript) before firing the request.

A kill takes down the target session **and every descendant in its subtree** — not the whole spawn root, only what hangs below the node you clicked:

- The target session gets an `operator_killed` transcript event.
- Each descendant gets a `cascade_killed` event.
- If the target was a Managed Agents (cloud) session, the worker process also tears down the remote session via the Anthropic API so you stop being billed for idle session-hours.
- The task in your vault transitions to whatever the worker writes as the post-kill tag (typically `#agent-failed`).

CLI sessions (Claude Code and Codex) do not get a Kill button — the page has no safe primitive for terminating a CLI process from outside its own terminal. If you need to stop one, do it in the terminal where it's running, or via `kill` on the underlying PID.

---

## Graph tab — Operator controls — resume and Go To

CLI sessions (Claude Code AND Codex) get up to two buttons:

**Resume** (shown on terminal / `inactive` / `yielded` sessions) opens a new WezTerm tab at the session's working directory and launches `claude --resume <session_id>` or `codex resume <session_id>` in it. WezTerm prints the new pane id; LifeOS stores it in a sidecar SQLite mapping (`data/cc_wezterm.db`) keyed by session id (`cc:` or `cx:` prefix disambiguates).

**Go To** (shown on every non-subagent CLI session, including live ones) jumps focus to the existing WezTerm pane for that session. Double-clicking the node in the graph does the same thing. The endpoint resolves the pane id in three steps:

1. **Cached mapping** — first checks `data/cc_wezterm.db`, populated by either a prior Resume click *or* the optional SessionStart hooks (`scripts/claude-session-pane.sh` for Claude Code, `scripts/codex-session-pane.sh` for Codex) which bind every new CLI start to its wezterm pane via `/api/agents/cc-pane-bind` and `/api/agents/cx-pane-bind` respectively. The cache is auto-invalidated when wezterm restarts: each mapping records the wezterm-gui pid it was written under, and a fresh wezterm boot drops the entry rather than blindly activating a stale `pane_id` (which could now belong to an unrelated session).
2. **FD probe** — if the cache misses, `lsof` finds which process holds the session's transcript file open; the holder's controlling TTY is matched against `wezterm cli list --format json`'s `tty_name`. Cwd is not enough to disambiguate when multiple panes share a project; the transcript file is. The result is cached for the next click.
3. **Activate-pane** — once a pane id is known, `wezterm cli activate-pane --pane-id <id>` switches focus. If WezTerm is the focused window the tab switches immediately; if it's hidden, the pane is selected in the background and a `notify-send` urgency hint pulses the dock icon. The OS-level window-raise across applications is restricted by Wayland compositors (no programmatic foreground steal); WezTerm under XWayland can be raised via `wmctrl`/`xdotool` if the operator needs it.

If a cached pane has gone stale (typical: user closed the tab), the activate-pane call fails, the mapping is cleared, and the probe runs once more — the session may have been resumed in a fresh pane. Only when both the cache *and* a fresh probe come up empty does Go To return 404 (toast: "Couldn't locate pane — install the SessionStart hook if claude is running in wezterm"); when a pane existed but is gone and no replacement can be found it returns 410.

Resume + Go To are **off by default** because spawning GUI terminals from a systemd service depends on the operator's desktop environment. Enable with `LIFEOS_CC_RESUME_ENABLED=true` for Claude Code sessions and `LIFEOS_CODEX_RESUME_ENABLED=true` for Codex; each flag also gates Go To for its respective source. Customize launchers via `LIFEOS_CC_RESUME_CMD` / `LIFEOS_CODEX_RESUME_CMD` if you don't use WezTerm — substitutions `{cwd}`, `{cwd_url}`, `{session_id}`, `{session_id_url}`, and `{inner_command}` are available. The probe-based Go To is WezTerm-specific (it reads `wezterm cli list`'s `tty_name`); non-WezTerm launchers can still use Resume but Go To will respond 404.

A session registered from another host (see "Cross-machine CLI session registration" above) resumes and focuses over ssh when that host is one of the operator's registered hosts (see [Card assignment](../technical/agent-worker.md#card-assignment-851)) — the same launcher runs remotely, so Resume and Go To work wherever the session actually lives. Only a host the operator hasn't registered still 409s: the error names that host, so the operator knows to go there instead of getting a silent no-op or a misleading 404.

---

## Privacy and exposure

- The transcript scan and Resume/Go To/kill primitives only ever touch **this** machine. Cross-machine visibility is opt-in and one-directional: another machine's hook posts a small lifecycle event (host, cwd, branch, status, a truncated prompt preview) to this API — this API never reaches out to, or reads files from, another machine.
- The registration endpoint (`POST /api/agents/cli-sessions/events`) is bearer-token gated and disabled by default (503 until `LIFEOS_AGENT_HOOK_TOKEN` is set) — unlike the kill/resume endpoints below, it's meant to be reachable over Tailscale, since that's the whole point.
- Transcript payloads are truncated to 240 chars in the feed previews — click an event to see the full payload only on demand. A registered session's prompt preview is truncated to 200 characters at the source.
- The kill, resume, and pane-bind (`/cc-pane-bind`, `/cx-pane-bind`) endpoints are **local-network only**. They must not be exposed via Tailscale Funnel or the public MCP HTTP transport (the gates live in [api/routes/agents.py](../../../api/routes/agents.py); see the technical spec for the threat model). Resume and Go To act on sessions recorded as running on this API's own host directly, and on a registered host over ssh; a session on an unregistered host returns an error naming that host instead.
- Claude Code ingest is strictly read-only — LifeOS opens jsonl files for reading and never writes back.

---

## Configuration knobs

All in `.env`. None are required — the defaults work for the standard LifeOS install.

| Var | Purpose | Default |
|---|---|---|
| `LIFEOS_CLAUDE_CODE_VIZ_ENABLED` | Surface Claude Code CLI sessions alongside agent worker sessions. Set false to scope the viz to LifeOS sessions only. | `true` |
| `LIFEOS_CLAUDE_CODE_PROJECTS_DIR` | Where to find Claude Code transcripts. | `~/.claude/projects` |
| `LIFEOS_CLAUDE_CODE_LOOKBACK_DAYS` | Discovery window — older jsonl files are excluded from the snapshot (they can still be loaded by direct session id). | `7` |
| `LIFEOS_CC_RESUME_ENABLED` | Enable the Resume and Focus buttons, and the board drawer's **Open** action for `#claude` cards. | `false` |
| `LIFEOS_CC_RESUME_CMD` | Launcher command. Substitutions: `{session_id}`, `{cwd}`, `{session_id_url}`, `{cwd_url}`, `{inner_command}`. The default uses WezTerm's CLI to open a tab AND run the resume in one shot. | `wezterm cli spawn --cwd {cwd} -- {inner_command}` |
| `LIFEOS_CC_RESUME_INNER_CMD` | The command run *inside* the spawned terminal — the actual `claude --resume` invocation. Substituted into `{inner_command}` of the launcher template. | `claude --dangerously-skip-permissions --resume {session_id}` |
| `LIFEOS_CC_RESUME_ENV_FILE` | Optional `key=value` file pinning `DISPLAY` / `XAUTHORITY` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS` for the spawned terminal. | `` (inherit systemd env) |
| `LIFEOS_CODEX_VIZ_ENABLED` | Surface Codex CLI sessions alongside the other sources. | `true` |
| `LIFEOS_CODEX_SESSIONS_DIR` | Where to find Codex rollout JSONLs. | `~/.codex/sessions` |
| `LIFEOS_CODEX_LOOKBACK_DAYS` | Discovery window for Codex rollouts. | `7` |
| `LIFEOS_CODEX_RESUME_ENABLED` | Enable Resume + Go To for `cx:` sessions, and the board drawer's **Open** action for `#codex` cards. | `false` |
| `LIFEOS_CODEX_RESUME_CMD` | Codex launcher template. Same substitution surface as `LIFEOS_CC_RESUME_CMD`. | `wezterm cli spawn --cwd {cwd} -- {inner_command}` |
| `LIFEOS_CODEX_RESUME_INNER_CMD` | Inner command inside the spawned terminal — the actual `codex resume` invocation. | `codex resume {session_id}` |
| `LIFEOS_AGENT_HOOK_TOKEN` | Bearer token required from `scripts/lifeos-agent-hook.sh` on `POST /api/agents/cli-sessions/events`. Empty (default) disables the endpoint (503) — a fresh clone accepts no cross-machine session data until this is set. | `` |

---

## Related Documents

- [ADR-011: External Agent Ingest](../../adr/011-external-agent-ingest.md) — Why Claude Code sessions surface read-only via a foreign-schema adapter
- [Agent Viz — Technical](../technical/agent-viz.md) — Endpoint shapes, D3 force config, status inference rules, security boundaries, and the board's lane-derivation rules
- [Agent Worker](agent-worker.md) — The other half of the picture: how `#agent` tasks get claimed and run
- [Claude Code Orchestration (product)](claude-code-orchestration.md) — The orchestrator that spawns the Claude Code sessions surfaced here
- [Agent Worker — Technical](../technical/agent-worker.md) — Sessions, transcripts, kill primitives
- [Architecture](../technical/architecture.md) — Where the viz fits in the broader code structure
- [Task Management](task-management.md) — The vault task store the board's cards are backed by
- [Human Queue](../../guides/human-queue.md) — How `#human` cards are filed and auto-resolved by agents and the nightly sync
- [Scheduler Guide](../../guides/scheduler.md) — How the Scheduled column's entries are created and edited
- [API Reference](api-reference.md) — Board, pending-question, and lane-move endpoint shapes
