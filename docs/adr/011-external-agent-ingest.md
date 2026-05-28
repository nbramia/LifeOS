# ADR-011: Read-Only Direct-Access Ingest for External Agent Transcripts

**Status:** Complete
**Last Updated:** 2026-05-27
**Decision:** Accepted

## Context

The `/agents` page (see [agent-viz product spec](../specs/product/agent-viz.md) and [agent-viz technical spec](../specs/technical/agent-viz.md)) shows two kinds of work happening on the host:

1. **LifeOS agent worker sessions** — `#agent`-tagged tasks running through `api/services/agent_worker/`. State lives in the worker's own SQLite `SessionStore`; transcripts in `data/agent_transcripts/`. LifeOS owns this data.

2. **Claude Code sessions** — the operator running `claude` in any terminal on any cwd. State lives in Claude Code's per-session JSONL transcripts at `~/.claude/projects/<encoded-cwd>/*.jsonl`. **Claude Code owns this data.**

The decision is how to surface Claude Code sessions in the `/agents` viz without violating Claude Code's ownership of its data, without coupling LifeOS to Claude Code's release cadence, and without losing the ability to fall back gracefully if the operator doesn't have Claude Code installed at all.

This pattern also matters going forward: future external agent runtimes (Codex CLI, Cursor session logs, other operator-side AI tools) will have the same shape — they each own their transcripts and LifeOS wants to surface them.

## Decision

A **read-only adapter** at `api/services/claude_code/session_ingest.py` translates Claude Code's JSONL format to LifeOS's `{ts, kind, payload}` shape at read time. The adapter:

- Walks `~/.claude/projects/<encoded-cwd>/*.jsonl` (path overridable via `LIFEOS_CLAUDE_CODE_PROJECTS_DIR`).
- Validates paths against a regex + rejects `..` traversal, even though discovery is internal.
- Reads JSONL files newest-first by mtime, capped by `LIFEOS_CLAUDE_CODE_LOOKBACK_DAYS`.
- Filters noise (system messages, permission-mode changes) on the way in.
- Infers session status from file `mtime` + a 10-minute "running" threshold + a `psutil` live-process check.
- Detects spawn relationships by inspecting `Task` / Agent tool-use blocks inside parent transcripts and generates synthetic subagent nodes.
- Caches the snapshot for 30 seconds so the 2-second SSE tick doesn't re-read JSONL every loop.

**Never writes back.** The adapter has no edit, delete, append, or mutate methods. The Claude Code data store is treated as immutable from LifeOS's perspective.

`/agents` API routes dispatch by `session_id` prefix: a `cc:`-prefixed id goes through this adapter; everything else goes through the LifeOS agent-worker SessionStore. The two stores are never joined; the viz layer merges at render time.

The whole path is gated by `LIFEOS_CLAUDE_CODE_VIZ_ENABLED` (default `true`). Operators who don't use Claude Code, or who want to opt out for any reason, can disable the entire adapter from `.env` without any code path running.

## Rationale

- **Zero coupling to Claude Code's roadmap.** Claude Code can rename fields, restructure JSONL events, add or remove message types — only the adapter has to update. Nothing else in LifeOS knows about Claude Code's internal format.
- **Read-only is auditable.** "We don't write to `~/.claude/`" is a property a reviewer can verify in one grep. There's no shared-state risk, no chance of corrupting Claude Code's own session resume.
- **Operator can opt out.** `LIFEOS_CLAUDE_CODE_VIZ_ENABLED=false` skips the whole path. Operators who don't use Claude Code see nothing odd, and the missing `~/.claude/projects/` directory doesn't surface as an error.
- **Pattern reusable for future external agents.** Read-only adapter + foreign-schema translation at read time + `<source>:` session_id prefix for dispatch routing + path-validated lookups + never write back. Same shape works for any future external agent runtime (Codex CLI, Cursor session logs, etc.).
- **Path validation even for internal input.** Even though discovery is by walking a known directory, the adapter rejects `..` and non-matching filenames. Defense in depth: internal inputs become external when the directory contents are operator-controlled (operators can drop files into `~/.claude/projects/`).
- **Snapshot caching matches the SSE tick.** The viz polls every 2 seconds; re-reading every JSONL file at that cadence is wasteful. A 30-second snapshot cache is well under the freshness expectations of the viz and well above the cost of a JSONL walk.

## Alternatives Considered

### Ask Claude Code to push events to LifeOS via webhook

Have Claude Code emit each event as an HTTP POST to a LifeOS endpoint.

**Rejected because:** Claude Code doesn't expose hooks for this and adding one is out of LifeOS's control. Even if it existed, it would create a dependency on an external tool's lifecycle — every Claude Code version bump becomes a potential ingest break.

### Poll a Claude Code API

Read Claude Code state through an official API.

**Rejected because:** There isn't one. JSONL transcripts are the only stable interface.

### Replicate transcripts into LifeOS storage

Periodically copy JSONL files into LifeOS's data directory and read from the copy.

**Rejected because:** Write amplification, storage duplication, ownership ambiguity (which copy is canonical?), and it breaks the read-only guarantee that makes the design auditable. The 30-second snapshot cache already solves the "don't re-read every 2 seconds" cost without copying.

### Tail JSONL files via inotify

Use kernel filesystem-event subscriptions to react in real time as Claude Code writes new lines.

**Rejected because:** The snapshot pattern (30s cache) is simpler and sufficient for the 2-second SSE tick rate the viz actually needs. inotify adds a long-running subscription per project directory, more failure modes, and a tighter coupling to JSONL append semantics. The viz isn't real-time-critical — bounded freshness is fine.

### Write a Claude Code SDK wrapper

Build a higher-level abstraction over the JSONL format that other LifeOS components could also use.

**Rejected because:** Speculative abstraction. The viz is the only consumer today. If a second consumer emerges, the adapter can be promoted. Premature abstraction would force the read-once-translate-once contract into a more elaborate interface that isn't earning its keep.

## Consequences

### Positive

- Zero Claude Code coupling: format changes touch one file (`session_ingest.py`).
- Read-only is auditable: one grep verifies no write paths exist.
- Falls back cleanly: missing `~/.claude/projects/` → adapter returns empty list, viz renders fine.
- Pattern is reusable: future external agent runtimes can copy the adapter shape.
- Operator-controllable: `LIFEOS_CLAUDE_CODE_VIZ_ENABLED=false` disables the entire path.

### Negative

- Schema changes in Claude Code can silently break ingest. Mitigation: defensive parsing, log on unknown event types, don't crash on shape mismatches.
- mtime-based status is a heuristic. The 10-minute "running" threshold can mislabel a finished-but-not-yet-touched session as still running for up to 10 minutes; the `psutil` live-process check tightens this but doesn't eliminate the edge case.
- Path-traversal hardening must be maintained in the adapter even though the discovery surface is internal. Any time the adapter accepts a session_id from an HTTP route, that's an external boundary and the validation has to be checked.
- 30-second snapshot cache means a brand-new Claude Code session takes up to 30s to appear in the viz. Acceptable trade-off; configurable via the cache TTL constant.
- Per-session psutil scan is O(N) over running processes. At scale (hundreds of concurrent sessions) the per-tick cost grows. Not a concern at single-operator scale.
- The adapter is the only thing that knows Claude Code's JSONL format. If the adapter is buggy, the bug looks like "the viz misrepresents Claude Code work" — and the operator may not know to look at the adapter. Mitigation: integration tests against fixture JSONL files representing known Claude Code versions.

### Pattern reusability

Future external agent runtimes should follow the same shape:

1. **Read-only adapter** in `api/services/<source>/` that owns all knowledge of the foreign format.
2. **Foreign-schema translation at read time** — translate to LifeOS's `{ts, kind, payload}` shape, not to some intermediate.
3. **`<source>:` session_id prefix** for dispatch routing — viz API routes look at the prefix and dispatch to the right adapter.
4. **Path validation** on every adapter entry point, even for paths discovered internally.
5. **Never write back** to the foreign data store.
6. **Operator opt-out env var** so the whole path can be disabled cleanly.

## Related Documents

### Design Context
- [ADR-007: Linux Migration](007-linux-migration.md) — Established the local-execution / external-process posture this ADR builds on
- [ADR-008: Managed Agents Cloud Routing](008-managed-agents-cloud-routing.md) — Sibling agent runtime, different ownership model (LifeOS owns Managed Agents session state)

### Specifications
- [Agent Viz (product)](../specs/product/agent-viz.md) — Consumer view of the `/agents` page that consumes this adapter
- [Agent Viz (technical)](../specs/technical/agent-viz.md) — Implementation; the reader using this adapter, the SSE feed, the D3 graph, the status inference
- [Agent Worker (technical)](../specs/technical/agent-worker.md) — Sibling SessionStore; how LifeOS-owned sessions differ

### Operational
- [Configuration](../guides/configuration.md) — `LIFEOS_CLAUDE_CODE_VIZ_ENABLED`, `LIFEOS_CLAUDE_CODE_PROJECTS_DIR`, `LIFEOS_CLAUDE_CODE_LOOKBACK_DAYS` env vars

### Code References
- [`api/services/claude_code/session_ingest.py`](../../api/services/claude_code/session_ingest.py) — The read-only adapter (965 lines)
- [`api/routes/agents.py`](../../api/routes/agents.py) — Dispatcher that routes `cc:`-prefixed session_ids to the adapter; `_claude_code_enabled()` and `_claude_code_snapshot()` are the entry points
