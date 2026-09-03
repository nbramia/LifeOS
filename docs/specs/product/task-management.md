# Task Management Guide

> **Status:** Complete
> **Owner:** Task Management
> **Last Updated:** 2026-09-03

LifeOS stores tasks as markdown checkboxes in your vault, in a format the Obsidian Tasks plugin can query and display, and manages them via chat, API, or Obsidian. Any checkbox line in `LifeOS/Tasks/*.md` counts as a task — you don't need to type LifeOS's own conventions by hand, and a plain hand-written checklist item is picked up on the next reindex. LifeOS's non-standard statuses (In Progress, Deferred, Blocked, Urgent — see below) render as generic checkboxes in Obsidian until you add them under the Tasks plugin's own "Custom statuses" settings; the plugin doesn't know about them out of the box.

## Storage Format

**Location:** `LifeOS/Tasks/{Context}.md` files in your vault
**Format:** Dataview inline field format
**Index:** `data/task_index.json` (query cache, rebuilt from markdown)

Example task line:
```
- [ ] TODO Call dentist [due:: 2025-02-10] [created:: 2025-02-07] #health <!-- id:abc123 -->
```

A task can also carry a multi-line notes body, stored as indented `> ` lines
beneath the task line, and operator fields (`host`, `effort`, `model`, `key`,
or any custom `[key:: value]`) that round-trip through any edit unchanged:

```
- [ ] TODO Call dentist [due:: 2025-02-10] [created:: 2025-02-07] [host:: laptop] #health <!-- id:abc123 -->
    > Ask about the Tuesday afternoon slot
    > Bring insurance card
```

## Custom Statuses

LifeOS uses checkbox symbols to represent task states:

| Status | Symbol | Usage |
|--------|--------|-------|
| Todo | `[ ]` | Not started |
| Done | `[x]` | Completed |
| In Progress | `[/]` | Currently working on |
| Cancelled | `[-]` | No longer relevant |
| Deferred | `[>]` | Postponed |
| Blocked | `[?]` | Waiting on dependency |
| Urgent | `[!]` | High priority |

In Progress, Deferred, Blocked, and Urgent are LifeOS conventions, not
Obsidian Tasks plugin defaults — add them under the plugin's own settings
(Tasks → Custom statuses) if you want Obsidian to show and filter on them
correctly. Todo and Done need no configuration.

## Creating Tasks

### Via Chat or Telegram

```
"add a to-do to call the dentist"
"create a task to review Q4 report"
"add a work task to finish the presentation"
```

### Via API

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Call dentist",
    "context": "Personal",
    "status": "blocked",
    "priority": "high",
    "due_date": "2025-02-10",
    "tags": ["health"],
    "notes": "Ask about the Tuesday afternoon slot",
    "fields": {"host": "laptop"}
  }'
```

`context`, `status`, `notes`, and `fields` are all optional — omit `context`
for Inbox, `status` for `todo`. Note: the chat/Telegram assistant's task tool
always files new tasks in Inbox regardless of what you say, to avoid it
guessing a wrong context — file directly to a context via the API or MCP
tools, or move the task afterward.

### Via MCP Tools

Use `lifeos_task_create` in Claude Code (registered via MCP server).

## Managing Tasks

### List Tasks

**Via chat:**
```
"show my tasks"
"list open tasks"
"what tasks do I have for work"
```

**Via API:**
```bash
# All open tasks
curl "http://localhost:8000/api/tasks?status=todo"

# Filter by context
curl "http://localhost:8000/api/tasks?context=Work"

# Filter by tag
curl "http://localhost:8000/api/tasks?tag=urgent"

# Search by text
curl "http://localhost:8000/api/tasks?query=dentist"
```

### Complete Tasks

**Via chat:**
```
"mark the dentist task as done"
"complete the Q4 report task"
```

**Via API:**
```bash
curl -X PUT http://localhost:8000/api/tasks/{id}/complete
```

### Edit Tasks

**Via API:**
```bash
curl -X PUT http://localhost:8000/api/tasks/{id} \
  -H "Content-Type: application/json" \
  -d '{
    "status": "in_progress",
    "priority": "high",
    "notes": "Waiting on the pharmacy callback",
    "fields": {"host": null}
  }'
```

`notes` replaces the notes body outright. `fields` is a merge, not a
replacement: a string value sets that field, a `null` value removes it, and
any field you don't mention is left alone.

### Delete Tasks

**Via chat:**
```
"delete the dentist task"
```

**Via API:**
```bash
curl -X DELETE http://localhost:8000/api/tasks/{id}
```

## Task-Reminder Linking

Create a task with an associated reminder in one command:

```
"add a task to call the dentist and remind me Friday at 3pm"
```

This is chat orchestration, not a single API call: the assistant creates the
task, creates a schedule for Friday 3pm, and links them by passing the
schedule's id as the task's `reminder_id`. Calling the task and schedule
APIs directly does the same in two calls — pass `reminder_id` on
`POST /api/tasks` (see [Scheduler Guide](../../guides/scheduler.md) for
creating the schedule itself).

## Obsidian Dashboard

View and manage all tasks in Obsidian via the Tasks Dashboard:

**Location:** `LifeOS/Tasks/Dashboard.md`

The dashboard includes:
- All open tasks (grouped by file)
- Tasks due this week
- In progress tasks
- Blocked tasks
- Recently completed tasks

The dashboard uses Obsidian Tasks plugin queries and regenerates on every
task change through the API immediately, and within a few seconds of an
edit made directly in Obsidian (the file watcher debounces external edits
before reindexing). It is created by TaskManager on initialization if it
doesn't already exist.

A Syncthing conflict copy or in-progress temp file in `LifeOS/Tasks/` is
never shown on the dashboard and never indexed as a task — it's surfaced
instead via `GET /api/tasks/conflicts` so a client can prompt you to resolve
it by hand.

## API Reference

| Method | Endpoint | Parameters | Description |
|--------|----------|------------|-------------|
| POST | `/api/tasks` | description, context, status, priority, due_date, tags, reminder_id, notes, fields | Create a task |
| GET | `/api/tasks` | status, context, tag, due_before, query | List/filter tasks |
| GET | `/api/tasks/conflicts` | - | List Syncthing conflict/temp files sitting in the tasks folder |
| GET | `/api/tasks/{id}` | - | Get specific task |
| PUT | `/api/tasks/{id}` | description, status, context, priority, due_date, tags, notes, fields | Update a task |
| PUT | `/api/tasks/{id}/complete` | - | Mark as done |
| DELETE | `/api/tasks/{id}` | - | Delete a task |

A task response also includes `updated_at` (an ISO-8601 timestamp with a UTC
offset, stamped on every create/update/complete/swap-tag) alongside the
fields above.

## Technical Details

- Task files are the source of truth (markdown in vault); writes are atomic (a reader never sees a partial file)
- `data/task_index.json` is a query cache rebuilt from markdown
- Vault file watcher triggers automatic reindexing on changes
- A task keeps its identity across an external edit that shifts its line number — writes locate it by id, not by a cached position
- Compatible with Obsidian Tasks plugin for viewing/editing in Obsidian
- Uses Dataview inline field format for metadata

See [Task Management — Technical](../technical/task-management.md) for how
id-addressed writes, the notes body, and conflict-file handling actually work.

## Related Documents

- [API Reference](api-reference.md) -- Task API endpoint contracts
- [Task Management — Technical](../technical/task-management.md) -- Engineering internals: id-addressed CAS writes, notes body, external-edit detection, conflict files
- [Scheduler Guide](../../guides/scheduler.md) -- Schedules; the `agent` action writes `#agent` tasks here
- [Agent Worker](agent-worker.md) -- Tasks tagged `#agent` are picked up by the autonomous worker for hands-free completion
