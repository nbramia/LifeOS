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

A card is a task ([task-management](../specs/technical/task-management.md))
with tag `human` and status `blocked` — no new storage, no schema change.
A Human queue lane on the `/agents` board is that same filter — derived,
not a separate list.

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
instruction files is a manual, per-install step.

## The three tools

Exposed over MCP (stdio for Claude Code, HTTP for Managed Agents and
Hermes) and as a native chat tool (`manage_human_queue`, actions
`add`/`list`/`resolve`) for `/chat`.

- **`lifeos_human_queue_add(title, notes?, key?, done_when?, source_host?, source_cwd?, source_session?)`**
  Files a card. Never blocks or changes the calling session's own status —
  purely fire-and-forget, unlike the worker's blocking
  `lifeos_agent_user_ask`. `key` must match `^[\w.:-]+\Z` (letters, digits,
  `.`, `:`, `-`, `_`) and must contain at least one character other than
  `.` — it's interpolated unescaped into the resolve route's URL path
  segment, so `/`, `?`, `#` are rejected (422). Filing again with
  an already-**open** card's `key` replaces that card's notes instead of
  creating a duplicate (`updated_at` advances). A `key` whose only match is
  a **done** card is unclaimed — filing opens a fresh card.
- **`lifeos_human_queue_resolve(id_or_key, note?)`**
  Marks the matching open card done, appending `note` to its notes body.
  404 for an id or key with no open card.
- **`lifeos_human_queue_list()`**
  Returns open cards: `id`, `title`, `key`, `age_hours`, `source_host`,
  `source_cwd`, `source_session`, `notes`, `done_when`.

## `done_when` — auto-resolve checks

The check runs only while the agent worker is running (off by default —
see [Agent Worker Setup](agent-worker-setup.md)); with the worker stopped,
a `done_when` card simply stays open until someone resolves it.

An optional condition, checked by the agent worker's poll tick
(`LIFEOS_HUMAN_QUEUE_POLL_SECONDS`, default 300s — see
[Configuration](configuration.md)) so a card resolves itself the moment the
underlying problem is actually fixed, without anyone having to remember to
go back and close it. Two types only:

```json
{"type": "endpoint", "path": "/api/example-service/status", "pointer": "/status", "equals": "ok"}
```
All three of `path`, `pointer`, and `equals` are required; `equals` must be
a JSON scalar (string, number, boolean, or null).

`path` must start with `/` and not `//`, and must not contain `?` or `#`
(422 otherwise — a worker-side guard against a `path` like `@host/x`
re-parsing the request's authority, or a query string turning the poll
into a query-driven local GET). The worker GETs `path` against the local
API it talks to, extracts the
value at the JSON Pointer (RFC 6901) `pointer`, and compares it to
`equals`. `pointer` uses the standard `/a/b` syntax (`""` or `"/"` selects
the whole response body).

```json
{"type": "file_exists", "path": "/absolute/path"}
```
Checked with a plain filesystem `exists()` call, run **in the agent-worker
process, on the worker's host** — not the API host. Those are usually the
same machine, but when `LIFEOS_API_URL` points the worker at a remote API
(see [Configuration](configuration.md)), `path` must exist on the worker's
own filesystem, not the API server's.

A card with no `done_when` just sits open until an agent (the filer, on
observing the fix, or `/chat`/Hermes on request) resolves it by hand.

A check that does not pass leaves the card untouched, retried next poll,
but logging differs: an **errored** check (endpoint unreachable, wrong
shape) logs a warning; a check that just evaluates **false** (the condition
isn't true yet) logs nothing — that's the expected steady state while the
operator hasn't acted yet. A passing check resolves the card with a note
naming the check that passed.

**Why no `shell` type.** A `done_when` check that could run an arbitrary
command would be a remote-execution surface reachable by any agent that can
file a card — deliberately excluded. `endpoint` and `file_exists` are
enough to express "the service is healthy again" or "the flag file exists"
without that risk. They are still real capability handed to any agent that
can file a card, not inert status reads: `endpoint` makes the agent worker
issue a GET against the given local API path every poll, and `file_exists`
answers whether a path exists on the worker host — so both should be
treated as available to whatever can create a `#human` task, not just to
whoever reads cards back.

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

## In the morning briefing

The **Morning Briefing** proactive reminder's prompt
(`scripts/seed_proactive_reminders.py`'s `MORNING_BRIEFING_PROMPT`) has a
**Waiting on You** section instructing the orchestrator to call
`manage_human_queue` (action `list`) and report cards shown as 24h old or
older, skipping the section when there are none.

If your Morning Briefing scheduler entry was seeded before this section
existed, it won't pick up the change on its own. Re-running
`scripts/seed_proactive_reminders.py --force` does **not** update it —
`--force` only skips the by-name existence check, and `SchedulerStore.create`
always inserts a new entry, so it would leave you with two Morning Briefing
schedules. Instead, either edit the existing entry's prompt by hand to add
a **Waiting on You** paragraph like the one above, or delete that entry
first and then re-run the seed script to recreate it from the current
prompt.

## Related Documents

### Specifications
- [Task Management — Technical](../specs/technical/task-management.md#human-queue) — The task store this queue is built on
- [Task Management — Product](../specs/product/task-management.md) — Consumer-facing task endpoints, including the Human-queue rows
- [API Reference](../specs/product/api-reference.md#task-endpoints) — REST contracts
- [MCP Tools](../specs/product/mcp-tools.md) — The `lifeos_human_queue_*` tools in the full MCP catalog
- [Agent Worker — Technical](../specs/technical/agent-worker.md) — The poll tick that resolves `done_when` cards
- [Data & Sync — Technical](../specs/technical/data-and-sync.md#failure-notifications) — Sync failures file a card keyed `sync:<source>`
- [Agent Viz — Product](../specs/product/agent-viz.md) — The `/agents` board's Human queue lane is the derived view of these cards

### Guides
- [Configuration](configuration.md) — `LIFEOS_HUMAN_QUEUE_POLL_SECONDS`
- [Doctor Bot](doctor-bot.md) — Files a card when a repair needs the operator
- [Operations](operations.md) — Monarch re-auth procedure

### Code References
- [`api/services/human_queue.py`](../../api/services/human_queue.py) — Card shape, dedupe, `done_when` validation
- [`api/routes/tasks.py`](../../api/routes/tasks.py) — REST routes
- [`api/services/agent_worker/worker.py`](../../api/services/agent_worker/worker.py) — `_process_human_queue` poll tick
- [`mcp_server.py`](../../mcp_server.py) — MCP tool registration
