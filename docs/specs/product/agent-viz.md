# Agent Activity Visualization (`/agents`)

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-05-27

LifeOS exposes a single live page at `/agents` that shows every agent session running on the box — both LifeOS agent worker tasks (`#agent`-tagged) and local Claude Code CLI sessions discovered on the filesystem. The graph updates every 2 seconds, clicking a node opens that session's transcript in a resizable side panel, and the operator can kill any in-flight LifeOS agent or relaunch any Claude Code session straight from the page.

The point is one place to see what your machine is doing on your behalf: whether the agent worker is making progress on the task you left in your vault, whether a Claude Code session you forgot about is still burning tokens, whether two parallel agents have collided on the same project.

---

## Table of Contents

1. [What you see](#what-you-see)
2. [Two sources, one graph](#two-sources-one-graph)
3. [Status semantics](#status-semantics)
4. [Filters and chips](#filters-and-chips)
5. [Side panel](#side-panel)
6. [Operator controls — kill](#operator-controls--kill)
7. [Operator controls — resume](#operator-controls--resume)
8. [Privacy and exposure](#privacy-and-exposure)
9. [Configuration knobs](#configuration-knobs)
10. [Related Documents](#related-documents)

---

## What you see

The page is a force-directed graph of sessions, laid out left-to-right by recency. Each node is one session:

| Encoding | Meaning |
|---|---|
| **Shape — circle** | LifeOS agent worker session |
| **Shape — rounded square** | Claude Code CLI session |
| **Shape — diamond** | Claude Code subagent (Task/Agent tool-use) |
| **Color** | Status — green running, blue claimed, amber blocked / paused, grey done, red failed |
| **Size** | Log-scaled by total tokens (input + output + cache). A 100k-token session is roughly 2× a 1k-token session, not 100×. Capped so one fat node can't dominate the canvas. |
| **White pulsing border** | Session is `running` AND has written to its transcript in the last 60 seconds (i.e. *actively producing output right now*) |
| **Edge** | Spawn relationship — parent → subagent |
| **X position** | Last-activity recency. Most recent sessions to the right, ≥24h old pinned to the left. The recency rail compresses around center when few nodes are visible — one filtered-down node ends up centered, two sit on a narrow band — and stretches to the full width as more nodes appear. |
| **Edge styling** | Plain curved paths (no arrowheads) between parents and subagents. When a node is selected, edges adjacent to it brighten to white. |

The simulation converges in ~8 seconds and then stops, so the graph stops jittering once it settles. New snapshots arrive every 2 seconds and only nudge nodes whose positions are now misleading.

### Canvas controls

The graph mirrors `/crm/graph`'s pointer model:

- **Drag a node** — pins it where you drop it. Useful when you want to inspect a busy cluster without the simulation nudging things around.
- **Drag the empty background** — pans the whole graph.
- **Scroll-wheel / pinch** — zooms in and out (0.2× – 5×).
- **Click a node** — opens its transcript in the side panel and highlights its parent/child relationships (selected node gets a thick white border, 1-hop neighbors get a thinner white border, everything else dims).
- **Click the same node again, or click empty background** — deselects and closes the panel.
- **Filter change** — releases any drag-pinned positions so the new visible set lays out from scratch.

---

## Two sources, one graph

The page unions two ingest paths into one rendered surface:

1. **LifeOS agent worker** — every `#agent` task the worker has claimed, plus its sleeps, yields, terminal outcomes, and any spawned children. This is the same data covered by [product/agent-worker.md](agent-worker.md); the viz is the read-side view of it.

2. **Claude Code CLI** — every transcript jsonl under `~/.claude/projects/`, scanned every snapshot tick (with a 30s cache so the disk isn't hammered). Each `.jsonl` file is one session; subagents spawned via the Task/Agent tool appear as separate nodes attached by spawn edges. Read-only: LifeOS never writes to Claude Code's data.

Both sources are normalized to the same shape before rendering, so filters, chips, and the side panel work identically on either kind of session. Disable the Claude Code half by setting `LIFEOS_CLAUDE_CODE_VIZ_ENABLED=false` if you only want to see worker tasks.

---

## Status semantics

A node's color is its status. The set is slightly different per source — same broad categories, different precise meaning:

| Status | LifeOS agent worker | Claude Code CLI |
|---|---|---|
| **running** | Currently executing tool calls or LLM turns. | A live `claude` process is running with this jsonl's cwd, **or** the file was modified in the last 10 minutes. The first is authoritative; the second is inferred. |
| **claimed** | Worker has picked up the task but hasn't fired the executor yet (preflight is in flight). | n/a |
| **yielded** | Paused waiting for spawned children to finish. | n/a |
| **inactive** | n/a | Modified within 24h but no live process — typically you closed the terminal mid-session. Resumable. |
| **blocked** | Waiting on a Telegram clarification from you. | n/a |
| **completed** | Task ran to completion successfully. | jsonl is >24h old, no error in the last event. |
| **failed** | Executor crashed, preflight rejected, or runtime error. | Last event in the jsonl was an error/tool failure (and >24h old). |
| **budget_exceeded** | Token / wall / dollar cap breached and the session was killed externally. | n/a |

A small `(inferred)` hint appears next to the status on Claude Code sessions whenever the status came from mtime rather than from a confirmed live process — useful to know when reading "running" on a session you don't remember starting.

---

## Filters and chips

The top toolbar has five filter controls and four count chips. **Filters are AND-composed**; the chips reflect *what's currently visible* after the filter, not the full snapshot.

### Filters

| Filter | Default | Notes |
|---|---|---|
| `include finished` checkbox | off | Off → completed / failed / budget_exceeded are hidden. On → everything shows, and the default recency window widens from 30 min to 7 days. |
| `recency` dropdown | last 30 min (60 min, 6h, 24h, 7d, all) | Filters by `last_activity_at`. Re-defaults to a wider window when `include finished` is enabled, unless the operator has set it manually. |
| `cwd` dropdown | all | Only Claude Code sessions are scoped to a cwd. Dropdown lists every unique cwd present in the current snapshot; auto-hides when empty (no Claude Code sessions visible). |
| `route` dropdown | all (local / claude / claude_code) | Filters by where the session ran — operator's local LLM, Managed Agents cloud, or Claude Code CLI. |
| `status` dropdown | all | Hard-filter by the status column from the table above. |

### Chips

| Chip | What it counts |
|---|---|
| `running` | Visible sessions with status `running`. |
| `blocked` | Visible sessions waiting on Telegram clarification. |
| `recent` | Visible sessions with status `completed`. |
| `cc` | Visible Claude Code sessions. |
| `API spend` | Sum of `total_dollars` across visible **LifeOS** sessions. Claude Code is intentionally excluded — that's billed to your Anthropic subscription, not metered API tokens, so adding it would distort the chip's meaning. |

Chips re-compute after every snapshot tick, so toggling `include finished` immediately bumps the API-spend number to include the finished sessions' final cost.

---

## Side panel

Clicking any node opens a panel on the right with that session's metadata header and a live-tailing event feed. The panel header carries:

- **Label** — derived from the task description (LifeOS), or the first non-empty user message (Claude Code), or the session id as a fallback.
- **cwd** — Claude Code only; the project directory the session was opened in.
- **Status badge** — same status the node is colored by, with `(inferred)` if applicable.
- **Source** — `LifeOS agent` or `Claude Code`.
- **Routing** — `Local`, `Claude`, or `Claude Code`.
- **Cost** — `total_dollars` to 4 decimals. For Claude Code, this is cache-aware accounting (separately tracking input, output, cache_creation @ 1.25× and cache_read @ 0.10×).
- **Tokens** — `input↓ / output↑`.
- **Depth badge** — if the session is a child, shows spawn depth.

The event feed is newest-on-top. Backfill arrives first (the last 50 events by default), then live updates stream in via SSE. Each event has:

- A **kind label** — click to filter the feed to just that kind (e.g. `tool_call`, `user_message`, `failed`). Click again to clear. When a session first opens with any `user_message` events present, the filter auto-defaults to `user_message` to focus the view on the operator-visible turns.
- A **timestamp** in your local timezone.
- A **payload preview** — rendered as structured fields (model badge, routing decision, tool-call pills `Name(arg, arg)`, compact budget `90s · $0.30 · 500k tok`, usage summary `↓ in · ↑ out · cache read/create`, free-text fields like `ambiguity` / `question` / `reason`). Noisy fields (`iterations`, nested `ephemeral_*`, etc.) are suppressed. Click anywhere on the event to expand — the raw JSON appears beneath the structured view for diagnostics; click again to collapse.

The panel is **resizable**: drag the left edge to widen or narrow it. The chosen width is remembered across sessions via `localStorage`.

Clicking the same node again, the `×` button, or empty background area closes the panel and clears the graph selection. You can click another node directly to switch focus.

---

## Operator controls — kill

LifeOS agent sessions in non-terminal states get a red **Kill** button in the panel header. Clicking it opens a confirmation modal asking for an optional reason (logged to the transcript) before firing the request.

A kill takes down the target session **and every descendant in its subtree** — not the whole spawn root, only what hangs below the node you clicked:

- The target session gets an `operator_killed` transcript event.
- Each descendant gets a `cascade_killed` event.
- If the target was a Managed Agents (cloud) session, the worker process also tears down the remote session via the Anthropic API so you stop being billed for idle session-hours.
- The task in your vault transitions to whatever the worker writes as the post-kill tag (typically `#agent-failed`).

Claude Code sessions do not get a Kill button — the page has no safe primitive for terminating a Claude Code CLI process from outside its own terminal. If you need to stop one, do it in the terminal where it's running, or via `kill` on the underlying PID.

---

## Operator controls — resume

Claude Code sessions in `inactive` or terminal states get a green **Resume** button. The flow:

1. Click Resume.
2. The server spawns a configured launcher command (`LIFEOS_CC_RESUME_CMD`). The default opens a new tab in Warp Terminal at the project's working directory.
3. The actual `claude --resume <session_id>` command (configured via `LIFEOS_CC_RESUME_INNER_CMD`) is copied to your system clipboard server-side via `wl-copy` (Wayland) or `xclip` (X11) — done from the server because the browser Clipboard API silently fails the moment the page loses focus.
4. A toast confirms "Warp opened. Resume command copied to clipboard — paste it." Paste the command in the new terminal and Claude Code reopens the session.

Resume is **off by default** because spawning GUI terminals from a systemd service depends on the operator's desktop environment. Enable with `LIFEOS_CC_RESUME_ENABLED=true`. Customize the launcher (`LIFEOS_CC_RESUME_CMD`) if you don't use Warp — substitutions `{cwd}`, `{cwd_url}`, `{session_id}`, `{session_id_url}` are available.

---

## Privacy and exposure

- The page only displays sessions running on **this** machine. No cross-host federation.
- Transcript payloads are truncated to 240 chars in the feed previews — click an event to see the full payload only on demand.
- The kill and resume endpoints are **local-network only**. They must not be exposed via Tailscale Funnel or the public MCP HTTP transport (the gates live in [api/routes/agents.py](../../../api/routes/agents.py); see the technical spec for the threat model).
- Claude Code ingest is strictly read-only — LifeOS opens jsonl files for reading and never writes back.

---

## Configuration knobs

All in `.env`. None are required — the defaults work for the standard LifeOS install.

| Var | Purpose | Default |
|---|---|---|
| `LIFEOS_CLAUDE_CODE_VIZ_ENABLED` | Surface Claude Code CLI sessions alongside agent worker sessions. Set false to scope the viz to LifeOS sessions only. | `true` |
| `LIFEOS_CLAUDE_CODE_PROJECTS_DIR` | Where to find Claude Code transcripts. | `~/.claude/projects` |
| `LIFEOS_CLAUDE_CODE_LOOKBACK_DAYS` | Discovery window — older jsonl files are excluded from the snapshot (they can still be loaded by direct session id). | `7` |
| `LIFEOS_CC_RESUME_ENABLED` | Enable the Resume button. | `false` |
| `LIFEOS_CC_RESUME_CMD` | Launcher command. Substitutions: `{session_id}`, `{cwd}`, `{session_id_url}`, `{cwd_url}`. | `warp-terminal warp://action/new_tab?path={cwd_url}` |
| `LIFEOS_CC_RESUME_INNER_CMD` | The command copied to the clipboard (intended for the new terminal). Set empty to disable clipboard copy. | `vt claude --dangerously-skip-permissions --resume {session_id}` |
| `LIFEOS_CC_RESUME_ENV_FILE` | Optional `key=value` file pinning `DISPLAY` / `XAUTHORITY` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS` for the spawned terminal. | `` (inherit systemd env) |

---

## Related Documents

- [Agent Viz — Technical](../technical/agent-viz.md) — Endpoint shapes, D3 force config, status inference rules, security boundaries
- [Agent Worker](agent-worker.md) — The other half of the picture: how `#agent` tasks get claimed and run
- [Agent Worker — Technical](../technical/agent-worker.md) — Sessions, transcripts, kill primitives
- [Architecture](../technical/architecture.md) — Where the viz fits in the broader code structure
