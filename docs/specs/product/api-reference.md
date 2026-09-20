# API Reference

**Status:** Complete
**Owner:** API Gateway
**Last Updated:** 2026-09-15

Catalog of every HTTP endpoint LifeOS exposes, with request/response shapes. Four adjacent catalogs split out for size:

- **Chat, Google & Messaging endpoints** (`/api/ask`, `/api/chat/*`, `/api/search`, `/api/calendar/*`, `/api/gmail/*`, `/api/drive/*`, `/api/imessage/*`, `/api/slack/*`) → [api-communications.md](api-communications.md)
- **CRM endpoints** (`/api/crm/*`) → [api-crm.md](api-crm.md)
- **MCP tool catalog** (Claude Code / Managed Agents tools) → [mcp-tools.md](mcp-tools.md)
- **Agent activity endpoints** (`/api/agents/*` — snapshot, kill, resume, cross-machine CLI session registration) → [Agent Viz — Technical](../technical/agent-viz.md#endpoints)

---

## Table of Contents

1. [API Overview](#api-overview)
2. [Chat, Google & Messaging Endpoints — see api-communications.md](api-communications.md)
3. [CRM Endpoints — see api-crm.md](api-crm.md)
4. [Agent Activity Endpoints — see agent-viz.md](../technical/agent-viz.md#endpoints)
5. [Memories Endpoints](#memories-endpoints)
6. [Conversations Endpoints](#conversations-endpoints)
7. [Briefing Endpoints](#briefing-endpoints)
8. [People Endpoints](#people-endpoints)
9. [Photos Endpoints](#photos-endpoints)
10. [Task Endpoints](#task-endpoints)
11. [Agent Board Endpoints](#agent-board-endpoints)
12. [Scheduler & Telegram Endpoints](#scheduler--telegram-endpoints)
13. [Monarch Money Endpoints](#monarch-money-endpoints)
14. [Job Queue Endpoints](#job-queue-endpoints)
15. [Performance Trace Endpoints](#performance-trace-endpoints)
16. [Admin Endpoints](#admin-endpoints)
17. [Card Assignment Endpoints](#card-assignment-endpoints)
18. [Home Endpoints (Eero)](#home-endpoints-eero)
19. [MCP Tools — see mcp-tools.md](mcp-tools.md)

---

## API Overview

**Base URL:** `http://localhost:8000`

**Authentication:** None (Tailscale-only access)

**External HTTP clients:** See [Client Surfaces](../technical/client-surfaces.md) for consumers (web, Telegram, whisper-relay) and breaking-change policy.

**OpenAPI Spec:** `GET /openapi.json`

---

## Chat, Google & Messaging Endpoints

Chat/search (`/api/ask`, `/api/chat/*`, `/api/search`), Google integration (`/api/calendar/*`, `/api/gmail/*`, `/api/drive/*`), and messaging (`/api/imessage/*`, `/api/slack/*`) endpoints live in [api-communications.md](api-communications.md). Pulled out into a separate file for the same reason as the CRM catalog below.

---

## CRM Endpoints

CRM endpoints (`/api/crm/*`) live in [api-crm.md](api-crm.md). Pulled out into a separate file because the CRM catalog is large enough on its own to warrant one.

---

## Performance Trace Endpoints

### GET /api/perf/traces

List recent performance traces with summary.

**Query Parameters:**
- `conversation_id` (string, optional): Filter by conversation
- `since` (string, optional): ISO timestamp to filter from
- `limit` (int): Max results (default: 50)

### GET /api/perf/traces/{trace_id}

Get a single trace with all spans.

### GET /api/perf/stats

Aggregate performance stats: avg/p50/p95/max per stage across recent traces.

**Query Parameters:**
- `since` (string, optional): ISO timestamp to filter from
- `limit` (int): Number of recent traces to aggregate (default: 100)

### GET /api/perf/routes

Rolling per-route request timing summary -- every HTTP request, not just chat turns. Backed by an in-memory rolling window (last 200 samples per route), process-local, reset on restart. See [Observability](../technical/observability.md#route-timing).

Returns `{"routes": [...], "count": N, "streams": [...], "stream_count": M}`. Each `routes` row: `method`, `route` (route template, e.g. `/api/crm/people/{person_id}`, never the raw path), `count`, `p50_ms`, `p95_ms`, `max_ms`, `slow_count`, `last_slow_at`. Sorted by `p95_ms` descending.

`streams` holds `text/event-stream` (SSE) responses separately -- `method`, `route`, `count`, `bytes` only, no duration or percentiles. An SSE connection's duration is however long the client kept it open (a browser tab, a live agent transcript), not a latency signal, so it's excluded from `routes` and the slow-request log entirely rather than dominating either. Sorted by `bytes` descending.

---

## Memories Endpoints

### POST /api/memories

Create a new memory.

**Request:**
```json
{
  "content": "Remember to follow up with Alex about the proposal",
  "category": "context"
}
```

### GET /api/memories

List all memories.

**Query Parameters:**
- `category` (string): Filter by category

### GET /api/memories/{id}

Get a specific memory.

### PUT /api/memories/{id}

Update an existing memory's content.

**Request:**
```json
{
  "content": "Updated memory text"
}
```

### DELETE /api/memories/{id}

Delete a memory.

### GET /api/memories/search/{query}

Search memories by keyword.

---

## Conversations Endpoints

### GET /api/conversations

List conversations for a persona (most recent first, up to 50).

**Query params:**
- `persona_id` (optional, default `"primary"`) — scope to a persona's threads (e.g. `?persona_id=fitness`). Omitting it returns the `primary` persona's threads, preserving web-chat behavior. Persona ids come from [`GET /api/personas`](api-communications.md#get-apipersonas).
- `backend` (optional, default unset) — scope to threads tagged with that backend (e.g. `?backend=hermes`). Unset returns threads from every backend, preserving behavior for every caller that predates this filter.

**Response:**
```json
{
  "conversations": [
    {
      "id": "conv-uuid",
      "title": "Budget planning",
      "created_at": "2026-06-01T12:00:00",
      "updated_at": "2026-06-01T12:05:00",
      "message_count": 4,
      "persona_id": "primary",
      "backend": "lifeos"
    }
  ]
}
```

Conversations are tagged with `persona_id` when created via `POST /api/ask/stream` (default `primary`); rows that predate this field backfill to `primary`. They're tagged with `backend` too (default `"lifeos"`; rows predating the column backfill to it) — see the `backend` field on [`POST /api/ask/stream`](api-communications.md#post-apiaskstream) for how a thread ends up tagged `"hermes"` instead.

### POST /api/conversations

Create new conversation.

### GET /api/conversations/{id}

Get conversation with messages. Access by id is **not** persona-scoped — any valid id resolves regardless of its persona.

**Response:**
```json
{
  "id": "conv-uuid",
  "title": "Budget planning",
  "created_at": "2026-06-01T12:00:00",
  "updated_at": "2026-06-01T12:05:00",
  "messages": [
    {
      "id": "msg-uuid",
      "role": "user",
      "content": "What did we discuss last week?",
      "created_at": "2026-06-01T12:00:00",
      "sources": null,
      "routing": null
    }
  ],
  "pending_question": null,
  "active_turn": null
}
```

`role` is `user` or `assistant`.

`pending_question` is present only while a spawned **orchestrating-persona** session (e.g. `doctor`) started from this conversation is awaiting an answer — `{ "session_id": "...", "question": "...", "kind": "..." }` — and is `null`/absent otherwise. `kind` is `goal_approval` (a `[GOAL]` awaiting approval) or `followup` (a `[CLARIFY]`). The client renders an answer affordance when it's present and posts to `/answer` below.

`active_turn` is present only while a chat turn is running server-side for this conversation — native or Hermes-relayed alike — and `null` otherwise: `{ "turn_id": "...", "conversation_id": "...", "started_at": "2026-08-21T09:14:22" }`. Lets a client that reconnects mid-turn show "still working..." and reach the cancel affordance below. A completed turn's message carries no marker on it in the normal case; an *interrupted* one (cancelled, timed out, caught in a server shutdown, or a genuine stream error) has its content suffixed with a visible cut-off marker and `routing: {"truncated": true, "truncation_reason": "cancelled" | "deadline" | "shutdown" | "stream_error"}` — see [client-surfaces.md § Turn lifetime and cancellation](../technical/client-surfaces.md#turn-lifetime-and-cancellation).

### POST /api/conversations/{id}/cancel

Stop a chat turn in flight for this conversation — native or Hermes-relayed. Since a disconnect does not stop a turn on its own, this is the explicit way to do it (also used internally when a new turn on the same conversation supersedes an old one still running).

**Response:**
```json
{ "ok": true, "cancelled": true }
```

`cancelled` is `false` when nothing was in flight (already finished, or never started) — not an error. **Errors:** `404` no such conversation.

### POST /api/conversations/{id}/answer

Answer a pending `[CLARIFY]`/`[GOAL]` from a spawned orchestrating-persona session (e.g. `doctor`) started from web/voice — without Telegram. Deposits the answer onto the session's existing open question (preserving its `kind`); the worker's normal tick then resumes the session via the **same** single resume path a Telegram reply uses (no second resume mechanism). Only meaningful while `GET /api/conversations/{id}` reports a `pending_question`.

**Request:**
```json
{ "answer": "yes" }
```

**Response:**
```json
{ "ok": true, "session_id": "sess-uuid", "status": "claimed" }
```

**Errors:** `400` empty answer · `404` no such conversation · `409` no spawned session linked, or no open question (never asked, already answered, or timed out).

### DELETE /api/conversations/{id}

Delete conversation.

### POST /api/conversations/{id}/ask

Ask a question within a conversation. Streams the response via SSE. Persists both the question and answer to the conversation, auto-generates a title on the first message.

**Request:**
```json
{
  "question": "What did we discuss last week?"
}
```

---

## Briefing Endpoints

### POST /api/briefing

Generate a comprehensive briefing about a person for meeting prep or relationship context.

**Request:**
```json
{
  "person_name": "John Smith",
  "email": "john@example.com"
}
```

### GET /api/briefing/{person_name}

Generate a briefing by person name (convenience GET endpoint).

**Query Parameters:**
- `email` (string, optional): Email for better entity resolution

---

## People Endpoints

### GET /api/people/person/{name}

Get a specific person by name.

### GET /api/people/search

Search people by name, email, alias, or display name. Matches sort exact
canonical-name matches first, then most recently seen.

**Query Parameters:**
- `q` (string, required): Search text
- `limit` (integer, optional, default 20, max 200): Max results to return

**Response:** adds a `total` field (count of all matching people, not just
the returned page) alongside the existing `people`/`count`/`query` fields.

### GET /api/people/list

List people ordered by most recently seen first, optionally filtered by
category.

**Query Parameters:**
- `limit` (integer, optional, default 50, max 500): Max results to return
- `category` (string, optional): Filter to this category

---

## Photos Endpoints

### GET /api/photos/stats

Get Apple Photos library statistics (named people, face detections, multi-person photos).

### GET /api/photos/people

List people recognized in Photos with match status to PersonEntity.

### GET /api/photos/person/{person_id}

Get photos containing a specific person.

### GET /api/photos/shared/{person_a_id}/{person_b_id}

Get photos where two people appear together.

### POST /api/photos/sync

Trigger Photos sync (matches faces to PersonEntity, creates interactions). **Errors:** `500` on a total sync failure (body still carries `success: false` and a top-level `error` key).

### GET /api/photos/thumbnail/{uuid}

Get a thumbnail image for a photo by UUID. Returns the image file from the Photos library derivatives folder. Returns 410 (Gone) with `X-iCloud-Only` header if the photo is in iCloud only.

### GET /api/photos/profile/{person_id}

Get a profile photo thumbnail for a person. Returns the most recent available photo thumbnail for use as an avatar. Also accepts `HEAD` (same status as `GET`, no body), so a client can check availability without downloading the image. Both the `200` and `404` responses carry `Cache-Control: public, max-age=3600`, so a client that already knows a person has no reachable photo doesn't need to ask again within the hour.

### GET /api/photos/open/{uuid}

Open a photo in Preview or Photos app. Tries the original file first, falls back to thumbnail, then the Photos app.

---

## Task Endpoints

Tasks can also be created, completed, listed, and deleted via natural language through the chat interface (`POST /api/ask/stream`). See [Task Management](task-management.md).

### POST /api/tasks

Create a task. Stored as an Obsidian Tasks-compatible markdown checkbox in the vault.

**Request:**
```json
{
  "description": "Call dentist",
  "context": "Personal",
  "status": "todo",
  "priority": "high",
  "due_date": "2025-02-10",
  "tags": ["health"],
  "reminder_id": "optional-linked-reminder-uuid",
  "notes": "Ask about the Tuesday afternoon slot",
  "fields": {"host": "laptop"}
}
```
`context`, `status`, `notes`, `fields`, and `reminder_id` are all optional.

### GET /api/tasks

List/filter tasks.

**Query Parameters:**
- `status` (string): Filter by status (todo, done, in_progress, cancelled, deferred, blocked, urgent)
- `context` (string): Filter by context (Work, Personal, Finance, etc.)
- `tag` (string): Filter by tag
- `due_before` (string): YYYY-MM-DD, tasks due on or before the given date
- `query` (string): Fuzzy text search across task descriptions

### GET /api/tasks/conflicts

List Syncthing conflict copies / in-progress temp files sitting in the tasks
folder (`name`, `mtime` each) — never indexed as tasks, surfaced so a client
can prompt the operator to resolve them by hand.

### GET /api/tasks/{id}

Get a specific task.

### PUT /api/tasks/{id}

Update a task (description, status, context, priority, due_date, tags,
notes, fields). `fields` merges into the task's operator/unknown fields — a
string value sets a field, a `null` value removes it.

A patch that includes a `tags` key is checked regardless of whether
`fields.assigned_by` is `"board"`: adding any of the worker's lifecycle
tags (`agent-running`, `agent-blocked`, `agent-completed`, `accepted`)
returns **409** no matter the card's current claim state, and a patch that
changes the normalized assignee-tag set or the claim-tag set on an
already-claimed card returns **409** with the same claimed-card reason the
board's lane endpoint uses. Both of those refusals are skipped for a patch
that asserts `actor: "worker"`, leaves the assignee-tag set unchanged,
targets an already-claimed card, and carries a `status` different from the
one on file — the shape of the agent worker's own lifecycle projector
recording its session's status transition, not a reassignment. `actor` is
caller-asserted and not persisted. When `fields.assigned_by` is `"board"` (the
marker the `/agents` board's own pickers stamp on every write) and the
patch changes `model`, `effort`, or `host` in `fields`, or carries a raw
`status` key, this endpoint enforces the claimed-card rule on that
field-edit path too — **409** on a card the worker has claimed, with no
write. Requests that carry neither a `tags` key nor the board marker are
unaffected. See [Agent Viz —
Product](agent-viz.md#human-moves-on-agent-owned-cards).

### PUT /api/tasks/{id}/complete

Mark a task as done (adds done date automatically).

### DELETE /api/tasks/{id}

Delete a task.

### POST /api/tasks/human-queue

File a Human-queue card — a task with status `blocked` and tag `human` —
for something only the operator can do. See the
[Human Queue guide](../../guides/human-queue.md).

**Request:**
```json
{
  "title": "Re-authenticate the example-service session",
  "notes": "Login expired; re-run the interactive login script.",
  "key": "example-service-reauth",
  "done_when": {"type": "endpoint", "path": "/api/example-service/status", "pointer": "/status", "equals": "ok"},
  "source_host": "example-host",
  "source_cwd": "/home/example/project"
}
```
`notes`, `key`, `done_when`, `source_host`, `source_cwd`, and `source_session`
are all optional. Filing again with an already-open `key` updates that
card's notes instead of creating a duplicate. The request returns `422` for
a malformed `key` or `done_when` — see the
[Human Queue guide](../../guides/human-queue.md#done_when--auto-resolve-checks)
for the exact rules.

### GET /api/tasks/human-queue

List open Human-queue cards. Returns `{"cards": [...], "total": N}`; each
card carries `id`, `title`, `key`, `age_hours`,
`source_host`/`source_cwd`/`source_session`, `notes`, `done_when`.

### PUT /api/tasks/human-queue/{id_or_key}/resolve

Mark a card done, by task id or dedupe key. `note` (optional) is appended to
the card's notes. Returns `404` for an id or key with no open card.

All task-mutating endpoints can return `409` if a concurrent external edit
keeps winning a compare-and-swap race on the underlying file — retry the
request.

---

## Agent Board Endpoints

The `/agents` Kanban board (see [Agent Viz](agent-viz.md)). Lanes are derived
from task status/tags on every read — there is no stored lane field. See
[Agent Viz — Technical](../technical/agent-viz.md) for the derivation rules
and the SSE update cadence.

### GET /api/agents/board

Full board view model, always built fresh (never cached): `{lanes:
{unassigned, assigned, in_progress, human_queue, scheduled, review, done,
snoozed}, generated_at, api_host}`. `api_host` names the machine running the API, so
a client can tell a card's assigned host (`fields.host`) apart from "this
machine". `kind` (`"task"` | `"schedule"`) is the first field of every
card and the only discriminator between the two shapes below — both kinds
can land in the `done` lane. Each task card carries `kind: "task"`, `id,
title, notes, status, tags, assignee, fields, context, updated_at, session
(nullable), pending_question (nullable)`. Each scheduled card carries `kind:
"schedule"`, `id, name, message_content, enabled, next_fire_at, recurring,
last_run {at, outcome, snippet} (nullable)`.

### GET /api/agents/board/stream

SSE stream of the board view model — emits a full `board` event whenever
the lanes change. Reads through a short-lived shared cache (see
[Agent Viz — Technical](../technical/agent-viz.md#board-cache)); `GET
/api/agents/board` above never does.

### PUT /api/agents/board/cards/{id}/lane

Move a task card to a lane, writing the corresponding status/tag at once —
or writing nothing and returning an error. Body: `{lane, assignee?}`.
`lane: "done"` marks the task done; `lane: "unassigned"` clears the
assignee tag; `lane: "assigned"` requires `assignee` (one of
`me`/`claude`/`codex`/`hermes`/`local`) and replaces any existing assignee
tag. `review`, `scheduled`, and `snoozed` can't be set directly (derived
from a tag, the scheduler store, and the `snoozed_until` field,
respectively — see below) and return **400**. Every successful move also
clears a card's snooze, whether or not it was actually snoozed.

Four **409** cases, no write in any of them:
- The card is worker-owned — `agent-running`/`agent-blocked` tag present,
  or the card's status is `in_progress` with a live CLI session actually
  linked to it (opened via the drawer's **Open** action on an Assigned
  `#claude`/`#codex` card, before the worker itself ever adds
  `agent-running`) — **every** `lane` is refused; the worker owns this
  card while it's running or waiting on an answer; answer the question or
  kill the session first.
- The card's assignee is an agent engine that hasn't been claimed by the
  worker yet and `lane` is `in_progress` — only the agent worker claims
  agent-assigned tasks.
- The card's assignee is an agent engine that hasn't been claimed by the
  worker yet (and isn't a pending review) and `lane` is `human_queue` or
  `done` — agent-owned cards are managed by the agent; reassign, unassign,
  or cancel (see below) instead of dragging it there.
- The card is a pending review (`agent-completed` tag without `accepted`)
  and `lane` is `in_progress` or `human_queue` — accept or reject the
  review first. `lane: "done"` on a pending review still succeeds and acts
  as accept (adds `accepted`), same as `POST .../accept`.

On success, the response's `lane` is the card's actual landed lane, which
for `lane: "assigned"`/`"unassigned"` (tags-only writes) can differ from
the requested lane if a higher-priority signal still applies — e.g. a
Human-queue card assigned to someone stays in Human queue. The web board
toasts when this happens.

Every task card in `GET /api/agents/board`'s response also carries a
server-computed `policy` block (`{claimed, agent_owned, cancel, assignee,
fields, lanes}`, each of `cancel`/`assignee`/`fields`/`lanes.<lane>` an
`{allowed, reason}` pair) derived from the exact same rules this endpoint
enforces — see [Agent Viz — Technical](../technical/agent-viz.md) for the
shape. The board and drawer read this instead of re-implementing the
rules, so they can't disagree with what a write actually does.

### PUT /api/agents/board/cards/{id}/snooze

Set a card's wake-up time, deriving it into the Snoozed lane. Body:
`{until}` — an ISO-8601 timestamp with a UTC offset, in the future.
Missing, unparseable, offset-less, or non-future `until` returns **400**
without touching the task. Eligible on any card whose status/tags alone
(ignoring any snooze already in effect) derive to Unassigned, Assigned,
Human queue, or Review; a card that derives to In progress or Done returns
**409**, and a scheduler entry (never resolves through the task store)
returns **404**. Only the `snoozed_until` field changes — status, tags,
and notes are left exactly as they were. Response: `{id, lane, status,
tags, snoozed_until}`.

### DELETE /api/agents/board/cards/{id}/snooze

Clear a card's wake-up time, restoring its status/tag-derived lane. A
no-op success (no write) on a card that isn't currently snoozed. Response:
`{id, lane, status, tags, snoozed_until: null}`.

### POST /api/agents/board/cards/{id}/accept

Move a Review card to Done by adding the `accepted` tag. Idempotent.
Returns **409** if the card isn't in the Review lane and isn't already
accepted.

### POST /api/agents/board/cards/{id}/undo-accept

Accepts an optional `{ "token": "..." }` body returned by the matching
Accept call; a stale token is rejected with 409. Legacy callers may omit it.

Restore an accepted card to Review by removing its `accepted` tag. The
transition is server-authoritative and preserves unrelated tags and fields;
returns **409** when the card is not accepted.

### POST /api/agents/board/cards/{id}/review-action

Apply an operator action to a blocked or Review card. Body:
`{action, note?, assignee?}`. `respond` requires a note and deposits it into
the card's open question; `reject` requires a note, queues a follow-up, and
returns the card to In progress; `reassign` requires a supported assignee,
optionally records a context note, and returns the card to Assigned while
preserving prior session context. Returns **409** for stale card/session
state or a CAS conflict, and **422** for a task-write validation failure.

### POST /api/agents/board/cards/{id}/cancel

Cancel an agent-assigned card — available whether or not the worker has
claimed it, unlike a lane drag. A card counts as agent-assigned if its
assignee tag names an agent engine (`#claude`/`#codex`/`#hermes`/`#local`)
OR it already carries `agent-running`/`agent-blocked` even with no
assignee tag. If a live session is linked, kills it and every descendant in its
subtree (the same teardown `POST /sessions/{id}/kill` performs), then
marks the task `cancelled`, which derives to the Done lane (behind
"include cancelled"). Idempotent: calling this on an already-`cancelled`
card returns the current state and touches no session. Returns **409**
`"accept or reject the review"` for a pending review, **409** `"cancel is
only available for agent-assigned cards"` for a card that is neither
engine-assigned nor claimed (`#me` or unassigned), and **409**
`"this card is already finished — nothing to cancel"` for a
card whose status is already `done` (e.g. accepted). Response: `{id, lane,
status, tags, killed: [session_id, ...], failures: [{session_id, reason},
...]}`.

### GET /api/agents/pending-questions

List unanswered agent questions: `{questions: [{id, task_id, session_id,
question, asked_at, bot}]}`.

### POST /api/agents/pending-questions/{id}/answer

Answer a pending question. Body: `{answer}`, 1-4096 characters — an empty,
whitespace-only, or over-length answer returns **400**. Writes the same
columns a Telegram reply would — the agent worker resumes the session on
its next tick unchanged.

---

## Scheduler & Telegram Endpoints

Schedules can also be created, edited, listed, and deleted via natural language through the chat interface (`POST /api/ask/stream`). See [Scheduler Guide](../../guides/scheduler.md).

> The legacy `/api/reminders*` endpoints remain mounted as deprecation-logged aliases over the same store.

### POST /api/scheduler

Create a schedule. Supports `schedule_type` of `once` (ISO datetime), `cron`, or `manual` (no trigger of its own — fires only via `POST .../trigger`; `schedule_value` is ignored and stored blank), and `action` of `notify` (static text), `prompt` (runs through the chat pipeline), `endpoint` (calls a LifeOS API endpoint), or `agent` (hands off to the agent worker via an engine-assigned task, with `executor` = `local`/`cloud`/`cloud-haiku`/`cloud-sonnet`).

`bot` (optional) selects which Telegram bot delivers the notification. Valid values are `primary` and the names registered in `config/telegram_bots.json`; anything else returns **422** with the accepted names. Omit it, or send an empty string, for the primary bot.

`timezone` must resolve as a valid IANA zone, and `schedule_value` must parse for its `schedule_type` (a cron expression for `cron`, an ISO datetime for `once`; unchecked for `manual`) — either failure returns **422** with a detail naming what's wrong, and nothing is stored.

The resulting action must have what it needs to fire, checked after every other field above: for `action: "endpoint"`, `endpoint_config` must have a `method` of `GET` or `POST` (case-insensitive; stored upper-case), an `endpoint` path starting with `/api/`, and `params` absent or a JSON object; for `notify`/`prompt`/`agent`, `message_content` must be non-blank; `budget_dollars`/`wall_seconds` are accepted only for `action: "agent"` (**422** naming the field otherwise) and, when present, must each be a finite, non-negative number. Either failure returns **422** naming the specific field. An `agent` action's execution context — `persona_id`, `model_id`, `effort`, `host`, `working_dir` — round-trips as plain top-level fields alongside `executor`; all default to empty (no override). `budget_dollars`/`wall_seconds` default to `null` (no override) and, when set, are rendered into the created task's title on every fire (see [agent-worker.md § Budgets](agent-worker.md#budgets)).

### GET /api/scheduler

List all schedules.

### GET /api/scheduler/{id}

Get a specific schedule.

### GET /api/scheduler/bots

List the Telegram bot names a schedule's `bot` field may use: `primary` plus every configured registry bot (an entry in `config/telegram_bots.json` counts only once its `token_env` is set), as `{"bots": [...]}`. Exactly the names `POST`/`PUT` accept. Backs the board drawer's bot picker.

### POST /api/scheduler/preview

Preview the next fire times a trigger produces, without creating a schedule. Body: `schedule_type` (`once` or `cron`), `schedule_value`, and `timezone` (optional, defaults to the configured `LIFEOS_TIMEZONE`). Returns `{"next": [...], "timezone": "<resolved IANA zone>"}` — up to three ISO-8601 UTC datetimes, using the same cron-in-timezone evaluation the scheduler itself fires on, plus the IANA zone that evaluation actually used (the request's `timezone` if supplied, else the configured default). `once` returns at most one time (the value itself, converted to UTC) when it's still in the future, else an empty list. An unrecognised `schedule_type` returns **400**; an invalid cron expression, ISO datetime, or timezone returns **422** with the same detail wording `POST`/`PUT /api/scheduler` use. Declared ahead of `GET /{id}` so `preview` is never captured as a schedule id. Backs the board's create-schedule composer's live preview, whose Timezone field is blank by default (meaning the configured default) rather than pre-filled from the browser's locale.

### PUT /api/scheduler/{id}

Update a schedule. Only the fields present in the request body are changed, and nothing is written if any check fails. An unrecognised `bot` returns **422** with the accepted names. `schedule_type` (must be `once`, `cron`, or `manual`) and `action` (must be one of `notify`/`prompt`/`endpoint`/`agent`) return **400**, matching `POST /api/scheduler`. `timezone` (must resolve as an IANA zone) and `schedule_value` (must parse as a cron expression for a `cron` schedule, or an ISO datetime for a `once` schedule — using `schedule_type` from the request if given, otherwise the entry's stored type; unchecked for `manual`) return **422** with a detail naming what's wrong. A request that sends `schedule_type` **without** `schedule_value` is validated against the entry's stored value under the new type, and returns the same **422** when that value doesn't parse — converting a schedule means sending both fields in one request. Setting `schedule_type: "manual"` clears `schedule_value` regardless of what (if anything) was sent — a manual schedule has no trigger of its own.

The same resulting-action check `POST /api/scheduler` runs applies here too, but only when the request touches `action`, `message_content`, `endpoint_config`, `budget_dollars`, or `wall_seconds` — a patch that touches none of these (e.g. `{"enabled": false}`) is never re-validated, regardless of whether the stored entry itself currently satisfies the rule. Whichever field the request doesn't supply falls back to the entry's currently stored value to decide the resulting action's inputs — so `{"action": "endpoint"}` alone is rejected with **422** against an entry with no `endpoint_config`, `{"endpoint_config": {...}}` alone is checked against the entry's current `action`, and `{"budget_dollars": 2.0}` alone against an entry whose stored `action` isn't `"agent"` is rejected the same way.

### DELETE /api/scheduler/{id}

Delete a schedule.

### POST /api/scheduler/{id}/trigger

Manually fire a schedule immediately. For a `once` schedule this consumes it: the fire disables it and clears its next-fire time, exactly as an unattended fire would. A `manual` schedule — one with no trigger of its own — stays enabled and can be triggered again; this is its only way to ever fire.

### POST /api/scheduler/send

Send an ad-hoc message via Telegram.

---

## Monarch Money Endpoints

Live financial data from Monarch Money. Monthly summaries are also synced to the vault.

### GET /api/monarch/session_status

Report cached Monarch session age and expiry-soon warnings, without making a network call — used by `/health/services` and dashboards to surface an impending re-auth requirement before the monthly sync hits a 401/525.

**Response:** `{exists: bool, age_days: float | null, status: "missing" | "expired" | "expiring_soon" | "ok", message: str}`

### GET /api/monarch/accounts

List all financial accounts with current balances.

**Response:** Array of `{name, type, subtype, balance, institution, last_updated}`

### GET /api/monarch/holdings

Investment holdings for one account.

**Query Parameters:**
- `account_id` (string, required)

**Response:** `{holdings: [...], count: int}` — empty list if the institution doesn't supply holdings through Plaid.

### GET /api/monarch/history

Daily balance snapshots for one account.

**Query Parameters:**
- `account_id` (string, required)
- `start_date` (string, optional): YYYY-MM-DD; omit for full history.

**Response:** `{history: [{date, balance}], count: int}`

### GET /api/monarch/transactions

Search recent transactions.

**Query Parameters:**
- `start_date` (string): YYYY-MM-DD (default: 30 days ago)
- `end_date` (string): YYYY-MM-DD (default: today)
- `category` (string): Filter by category name
- `search` (string): Search merchant names
- `limit` (int): Max results (default: 500)
- `sort` (string, optional): `asc` (oldest first) or `desc` (newest first) by date. Omit for the default order (unchanged for existing callers).

**Response:** Array of `{date, merchant, category, amount, account, notes, pending}`

### GET /api/monarch/cashflow

Cashflow summary for a date range.

**Query Parameters:**
- `start_date` (string): YYYY-MM-DD (default: 1st of current month)
- `end_date` (string): YYYY-MM-DD (default: today)

**Response:** `{summary: {total_income, total_expenses, savings, savings_rate}, categories: [{category, amount}]}`

### GET /api/monarch/budgets

Budget status (budgeted vs actual).

**Query Parameters:**
- `start_date` (string): YYYY-MM-DD (default: 1st of current month)
- `end_date` (string): YYYY-MM-DD (default: today)

**Response:** Array of `{category, budgeted, actual, remaining}`

---

## Job Queue Endpoints

### GET /api/jobs

List recent jobs with optional filtering.

**Query Parameters:**
- `status` (string): Filter by status (pending, running, completed, failed, cancelled)
- `type` (string): Filter by job type (e.g., reindex_vault, sync_source)
- `limit` (int): Max results (default: 50)

### GET /api/jobs/{job_id}

Get a specific job's status, result, and retry information.

### POST /api/jobs/{job_id}/cancel

Cancel a pending job. Only pending jobs can be cancelled.

---

## Admin Endpoints

### GET /health

Basic health check. Verifies API key and scheduler status. `api_key_configured` reports whether `ANTHROPIC_API_KEY` is set — a supported `LIFEOS_LLM_BACKEND=local` or `=remote` install with no Anthropic key still reports this as unset/degraded here; that's expected, not a sign the install is broken.

### GET /health/full

Comprehensive health check. Tests all services (ChromaDB, vault search, calendar, Gmail, Drive, people, conversations, memories, iMessage) with per-service latency. The `local_llm` check reports `{"status": "not_in_use", "detail": "LIFEOS_LLM_BACKEND=... not in use"}` rather than probing reachability when `LIFEOS_LLM_BACKEND` isn't `local` — an honest "not applicable" instead of a stale "ok".

### GET /health/services

Real-time external service health. Returns per-service status, degradation events (last 24h), and critical issues.

### POST /api/admin/reindex

Enqueue a vault reindex job. Returns immediately with a job ID. Use `GET /api/jobs/{job_id}` to check progress. Prevents duplicates (won't enqueue if already pending/running).

**Response:**
```json
{
  "status": "started",
  "message": "Reindex enqueued. Check /api/jobs/{job_id} for progress.",
  "job_id": "abc123"
}
```

### POST /api/admin/reindex/sync

Trigger vault reindex (blocking). Use for initial setup only.

### GET /api/admin/calendar/status

Calendar indexer status.

### POST /api/admin/calendar/sync

Trigger calendar sync. A partial result (some calendar accounts synced, one failed) is a `200` with `status: "partial"`. **Errors:** `500` on a total sync failure (body still carries `status: "error"` and a top-level `error` key).

### POST /api/admin/calendar/start

Start calendar sync scheduler. Supports time-of-day scheduling (default: 8 AM, noon, 3 PM) or fixed interval.

### POST /api/admin/calendar/stop

Stop calendar sync scheduler.

### Maintenance Mode

#### POST /api/admin/maintenance

Enter maintenance mode. Suppresses CRITICAL alerts for the given duration (default: 4 hours). Use before operations that cause transient service unavailability.

**Query Parameters:**
- `duration_seconds` (int): Duration to suppress alerts (default: 14400)

#### DELETE /api/admin/maintenance

Exit maintenance mode early. Re-enables CRITICAL alerts.

### Usage Tracking

#### GET /api/admin/usage

Get usage summary with stats for 24h, 7d, 30d, and all-time. Includes daily cost breakdown for charting. Totals include Hermes-proxied (external-backend) turns alongside native ones — the usage store has no per-model or per-backend filtering, so any row written to it (native or relayed) counts.

---

## Card Assignment Endpoints

Card kill/resume/focus/registration endpoints are documented in [agent-viz.md § Endpoints](../technical/agent-viz.md#endpoints) alongside the rest of the `/agents` operator-control surface, per item 6 of this file's Table of Contents. These two are new to this issue and don't fit that page's visualization framing, so they're listed here instead — full mechanism in [agent-worker.md § Card assignment](../technical/agent-worker.md#card-assignment).

`POST /sessions/{id}/resume` and `/focus` also accept an optional `target_host` in the body — "resume here". Full contract in [agent-viz.md § Resume target host](../technical/agent-viz.md#resume-target-host).

### GET /api/agents/models

Per-engine model catalog for the board's assignment pickers. The compatibility
fields remain `{engines: {claude: [...], codex: [...], local: [...], hermes: [...], remote: [...]}, refreshed_at, stale}`, with each entry `{id, label, pricing}`. `remote` is declared, not discovered: the configured `LIFEOS_REMOTE_LLM_MODEL` (labeled from `LIFEOS_REMOTE_LLM_LABEL`) first, then each `LIFEOS_REMOTE_LLM_MODEL_OPTIONS` entry, deduplicated — this is the list the board's `cloud` assignee's model picker reads. The additive `engine_states` map reports each engine's `models`, `state` (`loaded`, `empty-valid`, `unavailable`, `unconfigured`, or `unknown`), `observed_at`, `last_success_at`, `stale`, `reason_code`, `staleness_reason`, a `readiness` object, and `quota.state` (currently `unknown` unless an existing source provides evidence). The separate top-level `readiness` map reports route state (`configured`, `ready`, `unavailable`, or `unknown`), source, and observation timestamp; model discovery does not decide routing. Cached for `LIFEOS_AGENT_MODEL_CATALOG_TTL_SECONDS` (default 24h); a partial refresh preserves each affected engine's last-good model list and marks that engine stale rather than failing the whole response. A subscription CLI may be ready while model discovery is `unknown` when its local cache and optional API key are absent.

The additive top-level `defaults` map (`{claude, codex, remote}`) names the model id each engine currently runs on absent a board model override. For `claude`/`codex`, it's the newest id in that engine's live list whose family segment (`claude-<family>-<version...>` / `gpt-<version...>-<family>`) matches `LIFEOS_AGENT_DEFAULT_MODEL_FAMILY_CLAUDE`/`_CODEX` (default `opus`/`sol`), or `null` when nothing matches; for `remote`, the configured `LIFEOS_REMOTE_LLM_MODEL`, or `null` when unconfigured. Recomputed every refresh, so a newer release in the preferred family becomes the default with no operator action. A `claude_code`/`codex` board dispatch that names no model resolves against this default at dispatch time — see [agent-worker.md § Model catalog](../technical/agent-worker.md#model-catalog).

### GET /api/agents/hosts

Host registry for the board's host picker: `{hosts: [{name, ssh_target, online, is_api_host}], refreshed_at}`. The list is always the API host itself (`is_api_host: true`, `online: true`) plus every entry in `LIFEOS_AGENT_HOSTS`, deduplicated by name. `online` is `true`/`false` when a `tailscale status --json` probe ran successfully and matched the host to a peer, `null` when the signal is inconclusive — `tailscale` isn't installed, the probe failed/timed out, it ran fine but simply found no matching peer (a host can still be reachable over plain LAN ssh), or the catalog build itself failed for any other reason (every registry host still listed, just without a fresh probe). The route enforces a real ~1.8s ceiling on top of the probe's own bound, falling back to that same registry-preserving degraded response — never the bare API host alone, unless the degraded build itself fails — on a timeout or any other failure. Cached server-side for 30 seconds. The board client re-fetches when a drawer is opened more than ~30s after the last fetch, rather than once per page load or on a fixed cadence; a drawer left open never refetches on its own. After two consecutive client-side fetch failures, the client stops retrying for 10 seconds rather than issuing one request per drawer open against a dead endpoint. See [agent-worker.md § Host catalog](../technical/agent-worker.md#host-catalog) for the probe/matching/degrade mechanism.

### POST /api/agents/board/cards/{id}/open

Open an Assigned card (`status == "todo"`, a recognized assignee tag, no session already running against it) — `409` otherwise. `claude`/`codex` spawn the interactive CLI in a terminal (local, or over ssh for a registered `host` field), seeded with the card's title/notes and `LIFEOS_TASK_ID` in the environment; `hermes` returns `{open_url: "/chat?conversation=<id>"}` once the card has a Hermes conversation, else `409`.

`409` also covers: a second open on the same card within a 30-second grace window after the first open claimed it but before its session has registered (`card open is already in progress` — guards a double-click racing the spawn, not a permanent lock); and a `host` field naming a value absent from `LIFEOS_AGENT_HOSTS` (`host '<name>' is not configured in LIFEOS_AGENT_HOSTS`).

---

## Home Endpoints (Eero)

Pause/resume household internet access via eero — the `home` integration namespace's first provider. Every route is gated 503 until an eero session token is available; see [Home — eero guide](../../guides/home-eero.md) for setup and failure alerting.

### GET /api/home/eero/status

Every configured target's current state, read live from the vendor. Returns `{targets: [{name, type, paused, resume_at}]}`. `resume_at` is the pending scheduled auto-resume time (ISO datetime), or `null`.

### POST /api/home/eero/{name}/pause

Pause a profile or device's internet access. Idempotent and state-reconciling: sets the value, reads it back, and reports what the vendor actually has. `name` matches a configured target case-insensitively.

**Request body (optional):**
```json
{"minutes": 60}
```
`minutes` (1-1440) schedules an automatic resume via the scheduler. Omitted, the target's configured `default_minutes` applies if it has one, else the pause is indefinite and any existing pending resume is cleared. `indefinite: true` forces an indefinite pause regardless of `default_minutes` and cannot be combined with `minutes` (`422`). `scheduled: true` marks this as a scheduler fire (a recurring cron schedule or a timed pause's own auto-resume): a success with no `mismatch` then returns an empty `scheduler_message`, which the scheduler's fire loop sends nothing for.

**Response:**
```json
{
  "name": "Kid's iPad",
  "type": "profile",
  "requested_paused": true,
  "paused": true,
  "mismatch": false,
  "resume_at": "2026-09-15T19:00:00+00:00",
  "scheduler_message": "Kid's iPad: paused"
}
```
`mismatch` is `true` when the read-back state differs from what was requested (the write succeeded at the transport level but the vendor didn't apply it). **Errors:** `404` for an unknown target name (lists configured names, no vendor call made); `502` if the vendor rejects the write or returns an unrecognized response shape, or if the session is dead (a refresh was attempted and failed) — both alert via Telegram and a human-queue card.

### POST /api/home/eero/{name}/resume

Resume a profile or device's internet access. Same idempotent, state-reconciling, and error shape as pause. Cancels any pending scheduled auto-resume for the target.

**Request body (optional):**
```json
{"scheduled": true}
```
`scheduled: true` marks this as the scheduler's own fire of a timed pause's auto-resume: the route retries the vendor write up to 3 times with backoff before giving up, alerting (human-queue key `eero-resume-failed:<name>`) and returning `502` only after every attempt fails — the scheduler marks a one-off entry fired before calling the endpoint and never re-fires it, so this route is the only chance to retry. As with pause, a `scheduled: true` call that succeeds with no `mismatch` returns an empty `scheduler_message`.

---

## Related Documents

- [api-communications.md](api-communications.md) — Chat/search, Google integration, and messaging HTTP endpoints (split out from this file)
- [api-crm.md](api-crm.md) — `/api/crm/*` HTTP endpoints (split out from this file)
- [mcp-tools.md](mcp-tools.md) — MCP tool catalog (the canonical home for this content)
- [Agent Viz — Technical](../technical/agent-viz.md) — `/api/agents/*` endpoints in detail, including cross-machine CLI session registration
- [Agent Worker — Technical](../technical/agent-worker.md) — Card assignment mechanism behind the Card Assignment Endpoints above
- [Data & Sync](../technical/data-and-sync.md) — Data sources and sync pipeline
- [Chat UI](chat-ui.md) — Chat interface product spec
- [Client Surfaces](../technical/client-surfaces.md) — HTTP consumers and breaking-change policy
- [CRM UI](crm-ui.md) — CRM index pointing at the four product sub-specs
- [Configuration](../../guides/configuration.md) — Env vars referenced by several endpoints (LIFEOS_USER_NAME, LIFEOS_WORK_DOMAIN, etc.)
- [Observability](../technical/observability.md#route-timing) — How `/api/perf/routes` is populated (`RouteTimingMiddleware`, the slow-request threshold)
- [Agent Viz](agent-viz.md) — The `/agents` Kanban board these endpoints back
- [Home — eero](../../guides/home-eero.md) — Login, target configuration, and failure alerting for the Home Endpoints above
