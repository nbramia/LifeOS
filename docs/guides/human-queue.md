# Human Queue

**Status:** Complete
**Last Updated:** 2026-09-03
**Audience:** Operator, Contributor

The Human queue is a shared, fire-and-forget list of things only the operator
can do — an interactive session that notices an expired login, a nightly
sync that fails on a stale credential, a Codex run that hits a decision it
can't make on its own. Any agent — interactive Claude Code, the agent
worker, `/chat`, or Hermes — can file a card without stopping, and LifeOS
itself files cards for known operator-only failures (sync errors, an
expired Monarch session).

A card is a task (`docs/specs/technical/task-management.md`) with tag
`human` and status `blocked` — no new storage, no schema change. The board's
Human queue lane is that filter, derived, not a separate list.

## The instruction paragraph

Paste this into an operator-level instruction file (`CLAUDE.md`/`AGENTS.md`
on a machine you run agents from) so every agent on that machine knows the
convention:

> When a conversation surfaces a task that only the operator can do — one
> the conversation itself can't finish (a credential to re-enter, an
> approval, a decision only the operator can make) — file it with
> `lifeos_human_queue_add` (a short title, notes on what's needed and why, and
> a stable `key` so re-observing the same problem updates one card instead of
> piling up duplicates). When you later see the thing done, resolve it with
> `lifeos_human_queue_resolve`. Never file work you can do yourself — file
> only what genuinely requires the operator.

The repository ships this paragraph; installing it into an operator's own
instruction files is a manual, per-install step (out of scope for this
repo — see `docs/guides/doctor-bot.md` for the sibling pattern of an
operator-owned instruction file `config/personas/doctor.md` that a guide
merely documents).

## The three tools

Exposed over MCP (stdio for Claude Code, HTTP for Managed Agents and
Hermes) and as a native chat tool (`manage_human_queue`, actions
`add`/`list`/`resolve`) for `/chat`.

- **`lifeos_human_queue_add(title, notes?, key?, done_when?, source_host?, source_cwd?, source_session?)`**
  Files a card. Never blocks or changes the calling session's own status —
  purely fire-and-forget, unlike the worker's blocking
  `lifeos_agent_user_ask`. Filing again with an already-**open** card's `key`
  replaces that card's notes instead of creating a duplicate (`updated_at`
  advances). A `key` whose only match is a **done** card is unclaimed — filing
  opens a fresh card.
- **`lifeos_human_queue_resolve(id_or_key, note?)`**
  Marks the matching open card done, appending `note` to its notes body.
  404 for an id or key with no open card.
- **`lifeos_human_queue_list()`**
  Returns open cards: `id`, `title`, `key`, `age_hours`, `source_host`,
  `source_cwd`, `source_session`, `notes`, `done_when`.

## `done_when` — auto-resolve checks

An optional condition, checked by the agent worker's poll tick
(`LIFEOS_HUMAN_QUEUE_POLL_SECONDS`, default 300s — see
[Configuration](configuration.md)) so a card resolves itself the moment the
underlying problem is actually fixed, without anyone having to remember to
go back and close it. Two types only:

```json
{"type": "endpoint", "path": "/api/example-service/status", "pointer": "/status", "equals": "ok"}
```
GETs `path` on the local API, extracts the value at the JSON Pointer
(RFC 6901) `pointer`, and compares it to `equals`. `pointer` uses the
standard `/a/b` syntax (`""` or `"/"` selects the whole response body).

```json
{"type": "file_exists", "path": "/absolute/path/on/the/api/host"}
```
Checked with a plain filesystem `exists()` call — **on the API host only**,
which matters when the filing agent and the worker run on different
machines.

A card with no `done_when` just sits open until an agent (the filer, on
observing the fix, or `/chat`/Hermes on request) resolves it by hand.

**Why no `shell` type.** A `done_when` check that could run an arbitrary
command would be a remote-execution surface reachable by any agent that can
file a card — deliberately excluded. `endpoint` and `file_exists` are
enough to express "the service is healthy again" or "the flag file exists"
without that risk.

When the check fails or errors (endpoint unreachable, wrong shape, path
missing), the tick logs nothing and leaves the card untouched — it tries
again next poll. A passing check resolves the card with a note naming the
check that passed.

## REST endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/tasks/human-queue` | File (or dedupe-update) a card |
| `GET /api/tasks/human-queue` | List open cards |
| `PUT /api/tasks/human-queue/{id_or_key}/resolve` | Resolve a card |

See [API Reference](../specs/product/api-reference.md#task-endpoints) for
request/response shapes.

## What LifeOS files for itself

- **Sync failures.** The nightly sync (`scripts/run_all_syncs.py`) files a
  card keyed `sync:<source>` for any source the run summary classifies as a
  real failure (not a disabled/unconfigured source, not a dependency skip),
  with the error text in notes. The next successful run of that source
  resolves it.
- **Monarch re-authentication.** When the cached Monarch session is
  `expired` or `missing`, the nightly sync files a card keyed
  `monarch-reauth` with a `done_when` endpoint check against
  `GET /api/monarch/session_status` (`pointer: /status`, `equals: "ok"`).
  Re-authenticating (see [Operations § Monarch Money](operations.md#monarch-money-financial-data))
  makes the worker's next tick resolve it automatically.

## In `/chat`

Asked "what's waiting on me", the orchestrator lists open cards with
`manage_human_queue` (action `list`). It can also file and resolve cards in
conversation, subject to the same "never file work you can do yourself"
rule as every other agent.

## Related Documents

### Specifications
- [Task Management — Technical](../specs/technical/task-management.md#human-queue) — The task store this queue is built on
- [API Reference](../specs/product/api-reference.md#task-endpoints) — REST contracts

### Guides
- [Configuration](configuration.md) — `LIFEOS_HUMAN_QUEUE_POLL_SECONDS`
- [Doctor Bot](doctor-bot.md) — Files a card when a repair needs the operator
- [Operations](operations.md) — Monarch re-auth procedure

### Code References
- [`api/services/human_queue.py`](../../api/services/human_queue.py) — Card shape, dedupe, `done_when` validation
- [`api/routes/tasks.py`](../../api/routes/tasks.py) — REST routes
- [`api/services/agent_worker/worker.py`](../../api/services/agent_worker/worker.py) — `_process_human_queue` poll tick
- [`mcp_server.py`](../../mcp_server.py) — MCP tool registration
