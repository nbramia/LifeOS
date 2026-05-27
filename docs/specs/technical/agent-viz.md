# Agent Activity Visualization — Technical

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-05-27

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
| `POST /sessions/{id}/resume` | Resume a Claude Code session — body `{extra_env: {}}`. `cc:`-prefixed ids only. Gated on `LIFEOS_CC_RESUME_ENABLED`. |

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
| `forceX(recencyTargetX) strength 0.18` | Soft pull toward `0.92*W` for now-recent and `0.08*W` for ≥24h-old. Strength balanced against repulsion so same-recency nodes spread vertically without escaping the x-band. |
| `forceY(VIEW_H/2) strength 0.12` | Mild vertical centering — without it, nodes drift off-canvas; too strong and the collide force can't separate them. |
| `forceCollide radius = nodeRadius + 14, strength 0.9` | Hard guarantee that token-based-sized nodes never overlap. |
| `velocityDecay 0.45`, `alphaDecay 0.025` | Settles in ~6s; the 8s `alpha(0).stop()` is a backstop in case extreme node counts prevent natural convergence. |

Node shape varies by source — `circle` (LifeOS), `rect` (Claude Code, `rx`/`ry` rounded), `polygon` (subagent diamond). The simulation tick re-positions whichever element is bound to each datum; size and color are re-applied on every snapshot tick via `applyShapeAttrs`. The actively-writing pulse (white border, 1.4s ease-in-out) is a CSS keyframe applied via the `.pulsing` class — far simpler than the previous vis-network `DataSet.update` polling.

The `viewBox` is `1600 × 1100` with `preserveAspectRatio="xMidYMid meet"` and `overflow: visible` so collision-pushed nodes don't get clipped at the edges.

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

## Claude Code resume

```
POST /api/agents/sessions/{id}/resume   body: {"extra_env": {...}}
```

Resume opt-in via `LIFEOS_CC_RESUME_ENABLED`. Spawns a configured launcher and pushes the actual `claude --resume` command to the system clipboard server-side.

1. Validate the session id (must start with `cc:`, must pass `validate_session_id`). Strip a `:agent:...` suffix if present — operator clicks on a subagent diamond mean "resume the parent terminal".
2. Look up the meta via `discover_sessions(...)` with a widened 365-day lookback (resume is fine on old sessions). 404 if not found or no `decoded_cwd`.
3. Render `LIFEOS_CC_RESUME_CMD` with the substitutions `{session_id}`, `{cwd}`, `{session_id_url}`, `{cwd_url}` (URL-encoded for embedding inside `warp://…` / `vscode://…` URIs).
4. `shlex.split(rendered)` — no `shell=True`, ever.
5. Build the env: inherit `os.environ`, then layer `LIFEOS_CC_RESUME_ENV_FILE` (key=value lines pinning `DISPLAY` / `XAUTHORITY` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS`), then merge `body.extra_env` last.
6. Render `LIFEOS_CC_RESUME_INNER_CMD` and push it to the system clipboard via `wl-copy` (Wayland) or `xclip` (X11). Server-side because the browser Clipboard API silently fails the instant the page loses focus to the spawned terminal.
7. `subprocess.Popen(argv, cwd=decoded_cwd, env=env, start_new_session=True)`. Wait up to 0.5s; rc=0 is success regardless of timing (Warp's URL dispatcher exits clean after handing off to a running desktop app).

Response: `{spawned: true, pid, command, cwd, inner_command, clipboard_copied}`. The frontend falls back to a manual-copy toast if `clipboard_copied` is false.

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

The threat model: the LifeOS MCP HTTP transport is publicly accessible via Tailscale Funnel and is the obvious place an external agent or compromised credential could hit LifeOS endpoints. Both `/kill` and `/resume`:

- Live under `/api/agents/*` — the MCP transport never proxies this prefix.
- Are not registered as MCP tools — so they cannot be invoked through the MCP layer even if an attacker has a bearer token.
- Kill calls into worker primitives that already have an audit trail (every kill emits a transcript event).
- Resume runs a configured launcher via `shlex.split` only — no `shell=True`. Template substitutions are URL-encoded where they go into URI strings.

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
