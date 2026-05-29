# Agent Activity Visualization — Technical

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-05-29

Engineering view of the `/agents` page — endpoint shapes, ingest paths, status inference, layout, and security boundaries. For the consumer view see [product/agent-viz.md](../product/agent-viz.md).

---

## Table of Contents

1. [Architecture overview](#architecture-overview)
2. [Endpoints](#endpoints)
3. [Snapshot shape](#snapshot-shape)
4. [LifeOS agent ingest](#lifeos-agent-ingest)
5. [Claude Code ingest](#claude-code-ingest)
6. [Status inference (Claude Code)](#status-inference-claude-code)
7. [Live process detection](#live-process-detection)
8. [Snapshot caching](#snapshot-caching)
9. [D3 force-graph](#d3-force-graph)
10. [Side-panel SSE](#side-panel-sse)
11. [Operator kill](#operator-kill)
12. [Claude Code resume](#claude-code-resume)
13. [Worker resilience](#worker-resilience)
14. [Security boundaries](#security-boundaries)
15. [Related Documents](#related-documents)

---

## Architecture overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          web/agents.html                                  │
│   D3 force-simulation graph, SSE consumer, side panel, kill/resume UI     │
└─────────────────────────────┬────────────────────────────────────────────┘
                              │ HTTP + SSE
                              ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       api/routes/agents.py                                │
│   /api/agents/snapshot · /stream · /sessions/{id}/events · /stream       │
│                       · /kill · /resume                                   │
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

The route file imports the worker's `SessionStore` and `TranscriptStore` directly (read-only) and unions their output with the Claude Code adapter's normalized shape. No second worker process; the API server reads the same SQLite + JSONL files the worker writes.

---

## Endpoints

All under `/api/agents`. Local-network only — the kill and resume endpoints must not be exposed via the public MCP HTTP transport.

| Method + Path | Purpose |
|---|---|
| `GET /snapshot` | One-shot full snapshot. Use for first-paint or when the SSE stream drops. |
| `GET /stream` | SSE: emits a full snapshot every 2s. Tolerant to per-tick failures (yields an `error` event and continues). |
| `GET /sessions/{id}/events?limit=N` | Last N transcript events (default 200, max 2000). Dispatches by `cc:` prefix to the Claude Code ingest path. |
| `GET /sessions/{id}/stream?backfill=N` | Per-session SSE: backfill last N events (default 50, max 500), then live-tail. Closes cleanly when the session reaches terminal status (LifeOS) or after 5 min idle (Claude Code). |
| `POST /sessions/{id}/kill` | Operator kill — body `{reason: ""}`. LifeOS sessions only. Cascades to descendants in the subtree. |
| `PUT /sessions/{id}/label` | Set or clear an operator-pinned manual label — body `{label: ""}`. Non-empty pins a custom node name that overrides the auto-derived label and AI summary label everywhere it's shown; empty clears it. Durable in `data/agent_viz_label_overrides.db` (in-process cache, lazy-loaded). Works for both LifeOS and `cc:` sessions. |
| `POST /sessions/{id}/resume` | Resume a Claude Code session — body `{extra_env: {}}`. `cc:`-prefixed ids only. Gated on `LIFEOS_CC_RESUME_ENABLED`. Spawns a WezTerm tab via `wezterm cli spawn`, captures the pane id from stdout, and stores `session_id → pane_id` in `data/cc_wezterm.db` so Focus can target it later. |
| `POST /sessions/{id}/focus` | Activate the WezTerm pane for this session (Go To). `cc:`-prefixed ids only. Gated on `LIFEOS_CC_RESUME_ENABLED`. Resolves the pane id from the cached mapping first, then falls back to an FD probe (lsof + /proc + wezterm cli list) so it works for sessions never opened via Resume. 404 only when both the cache *and* the probe come up empty; 410 when the pane existed but is gone and no replacement is found. |
| `POST /cc-pane-bind` | Localhost-only endpoint called by the Claude Code SessionStart hook. Body `{session_id, pane_id, cwd}`. Upserts the mapping in `cc_wezterm.db` so Go To can target newly-started `claude` invocations without a probe. 403 from non-loopback callers. |

Heartbeats: per-session SSE emits a `:heartbeat\n\n` comment every 15s when there's no new event, so dropped connections surface quickly through the browser's `EventSource` retry.

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
| `task_id` | str | LifeOS: task id from the worker. Claude Code: bare session uuid. |
| `status` | str | See [Status inference (Claude Code)](#status-inference-claude-code) and product spec. |
| `routing` | str | `local`, `claude`, or `claude_code`. |
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
| `label` | str | Display name. Cached per session id. |
| `custom_label` | str \| null | Operator-pinned manual label (via `PUT /sessions/{id}/label`). When set, the frontend uses it as the node name in preference to the AI `short_label` and `label`. |
| `model_label` | str | Short badge — `Local` / `Haiku` / `Sonnet` / `Opus` / `Claude Code`. |
| `last_event_kind` | str | Most recent transcript event kind — drives the side-panel "last" tooltip. |
| `tool_call_count`, `error_count` | int | Summed across the transcript tail (last 100 events). |
| `source` | str | `lifeos_agent` or `claude_code`. Frontend uses this to pick the shape. |
| `status_inferred` | bool | Claude Code only. `false` means status came from a confirmed live process. |
| `project_key`, `decoded_cwd` | str | Claude Code only. `project_key` is the dir name under `~/.claude/projects/`; `decoded_cwd` is the original cwd. |
| `is_subagent` | bool | True for synthetic Task/Agent tool-use children. |

---

## LifeOS agent ingest

Read paths in `api/routes/agents.py`:

- `_session_to_dict(s, transcript)` — projects a `Session` row to the snapshot shape. Defends against the optional `total_cache_*_tokens` attrs being absent on old rows.
- `_label_for_session(s, events)` — walks the first five transcript events looking for `description` / `task_description` / `prompt`; falls back to the session id. Result cached per session id (capped at 500 entries).
- `_summarize_events(events)` — counts tool calls and errors across the last 100 events. `_is_error_kind` matches the literal set `{failed, managed_failed, child_failed_internal, killed, cascade_killed}` plus any kind ending in `_failed` or `_error` (future-proof).
- `_model_label_for_routing(routing)` — derives the model badge from `settings.agent_managed_model`. Falls back to `Claude` if the model name doesn't match any known family.

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

## Snapshot caching

Two caches:

1. **`_snapshot_cache`** in `session_ingest.py` — keyed by `(projects_dir, lookback_days)`, TTL 30s. The whole `(sessions, edges)` tuple is memoized so a single SSE tick across many connected clients doesn't re-walk the projects dir. Bypass with `cache_ttl=0` (used by tests).

2. **`_label_cache`** in `agents.py` — keyed by session id, capped at 500 entries. Labels are derived from the first 5 transcript events and don't change once a non-fallback label has been resolved.

Both caches are lock-guarded so concurrent FastAPI threads can't see partial entries. Invalidation is on-demand via `invalidate_cache()` / `invalidate_process_cache()` (used by tests and reachable from a future admin endpoint if needed).

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

Response: `{killed: [session_ids], failures: [{session_id, error}], reason: <input>}`.

The endpoint must not be exposed via Tailscale Funnel or the public MCP HTTP transport. The boundary lives in the route layer; see [Security boundaries](#security-boundaries).

---

## Claude Code resume + Go To

```
POST /api/agents/sessions/{id}/resume   body: {"extra_env": {...}}
POST /api/agents/sessions/{id}/focus    body: (none)
POST /api/agents/cc-pane-bind           body: {session_id, pane_id, cwd}   (localhost only)
```

All three opt-in via `LIFEOS_CC_RESUME_ENABLED` (except `/cc-pane-bind`, which is gated by client IP only — `127.0.0.1` / `::1`). Resume spawns a new WezTerm tab and records the new pane id; Go To (`/focus`) revisits the pane for a session, falling back to an FD probe when no mapping is cached; `/cc-pane-bind` is the SessionStart hook entry point that pre-populates the mapping at `claude` startup.

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

The threat model: the LifeOS MCP HTTP transport is publicly accessible via Tailscale Funnel and is the obvious place an external agent or compromised credential could hit LifeOS endpoints. `/kill`, `/resume`, and `/focus`:

- Live under `/api/agents/*` — the MCP transport never proxies this prefix.
- Are not registered as MCP tools — so they cannot be invoked through the MCP layer even if an attacker has a bearer token.
- Kill calls into worker primitives that already have an audit trail (every kill emits a transcript event).
- Resume runs a configured launcher via `shlex.split` only — no `shell=True`. Template substitutions are URL-encoded where they go into URI strings. The rendered `{inner_command}` is split into individual argv tokens before reaching the launcher, so a malicious inner command cannot smuggle shell metacharacters.
- Focus calls `wezterm cli activate-pane` with a fixed argv (no template) using the pane id from the local SQLite mapping. The store is only writeable from the same process (no cross-machine exposure), and pane ids are integers — there is no path for an external caller to inject arbitrary argv. The FD-probe fallback never reads attacker-controlled data: `lsof` is invoked with the transcript path that LifeOS itself derived from `discover_sessions`, and `/proc/<pid>/fd/0` is read as a symlink target whose filtering keeps only `/dev/pts/N` paths.
- `/cc-pane-bind` is bound to loopback by IP check (`127.0.0.1` / `::1`); the public MCP transport runs on the same host but a different prefix and would never route to it. The accepted body is constrained: `session_id` runs through `validate_session_id` (rejects path traversal), `pane_id` must be a non-negative int, `cwd` is opaque text.

Per-source guarantees:

- Claude Code ingest is **read-only**. No code path under `api/services/claude_code/` opens a jsonl with write/append intent; `validate_session_id` rejects path-traversal attempts before any filesystem read.
- LifeOS agent ingest reads `SessionStore` (SQLite) and `TranscriptStore` (JSONL). Both are owned by the worker process and exposed read-only here.
- Transcript content can include personal data (emails, vault paths). Payload previews are truncated to 240 chars in the snapshot summary; full payloads only appear in the per-session SSE on operator click.

---

## Related Documents

- [ADR-011: External Agent Ingest](../../adr/011-external-agent-ingest.md) — Read-only adapter pattern this spec implements
- [Agent Viz — Product](../product/agent-viz.md) — Consumer view: filters, chips, status semantics, operator controls
- [Agent Worker — Technical](agent-worker.md) — Sessions, transcripts, kill primitives, inter-agent coordination
- [Agent Worker — Product](../product/agent-worker.md) — `#agent` task lifecycle, Telegram interactions
- [Architecture](architecture.md) — Where the route + adapter fit in the broader code structure
- [Observability](observability.md) — Adjacent traces / health surfaces
