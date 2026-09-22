# Task Management Guide

> **Status:** Complete
> **Owner:** Task Management
> **Last Updated:** 2026-09-22

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
`working_dir`, or any custom `[key:: value]`) that round-trip through any edit
unchanged:

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
| Cancelled | `[-]` | Not relevant anymore |
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

## Projects and Subtasks

A project is an ordinary task with at least one incoming child reference. A
child stores its parent's stable task ID in the normal inline fields map:

```markdown
- [ ] TODO Prepare synthetic launch <!-- id:proj1234 -->
- [ ] TODO Draft synthetic announcement [parent_id:: proj1234] <!-- id:child567 -->
```

There is no project flag, separate project file, or nested checkbox format.
Done and cancelled children keep the parent classified as a project until
their `parent_id` is removed. Set or clear `fields.parent_id` through the
ordinary create/update APIs to attach, move, or detach a child. Hierarchy is
one level: a child cannot itself be a project, and missing, self, cyclic, or
nested links are rejected by API writes.

The separate `fields.project` value records repository affinity for software
work. It does not attach a task to a parent or classify the task as a structural
project. Only valid incoming `fields.parent_id` links derive `is_project`,
`child_count`, and the `project` progress summary returned by read APIs.
Worker location selection treats this value as a catalog name only; an unknown
value is preserved as metadata and is never interpreted as a filesystem path.

Creating the first child pauses automatic execution of the parent. Removing
the final child returns the same parent task to ordinary presentation without
clearing that pause; use `POST /api/tasks/{id}/resume-execution` when the
ordinary task should become executable again. Parent assignment is project
ownership and does not copy to children. Each child keeps its own assignee and
normal execution/review lifecycle.

A child created or updated by an agent — as opposed to the operator — can
never be assigned the external-routing persona (`#hermes`); the operator can
still assign it from the board. An agent-attributed child can only be put on
a paid model route (`#cloud`, `#cloud-haiku`, `#cloud-sonnet`) when the
project's own owner already carries that same route. An agent-created child
is marked internally so later features (board display, notices) can tell it
apart from an operator-created one; that marker cannot be set or cleared
through an ordinary edit.

Projects use explicit lifecycle actions:

- **Start** marks the parent active without creating worker lifecycle tags.
- **Plan and delegate** starts one idempotent, bounded agent-owner coordination
  run with current child state and the project's configured model, effort,
  host, and working directory when its owner supports those fields. Managed
  Agents ownership uses the explicit `#cloud-haiku` or `#cloud-sonnet` consent
  tag and its model choice, retaining configured effort and host assignments
  but not the configured model or working directory. Each child creation uses
  a stable `operation_key`, so retrying the same planning step recovers the
  existing child instead of duplicating it.
- **Complete project** requires every child to be done or cancelled, every
  agent result to be accepted, no live coordinator, and no pending
  cancellation. Closing with cancelled children requires explicit reduced-
  scope acknowledgement. The project's own attested owner session can
  complete its own project through `lifeos_agent_project_owner` even while
  that owner's own turn is still live — the one case this guard otherwise
  refuses; an operator completion is still refused while the coordinator is
  live, exactly as before.
- An agent-owned project's attested owner session can also accept or reject
  a review-pending child of its own project (`lifeos_agent_project_owner`,
  actions `accept_child`/`reject_child`), scoped to that project and the
  owner's own current turn. This shares its underlying logic with the
  operator's board Accept/Reject: an acceptance is recorded distinctly from
  an operator one, and a rejection (which requires a note, and resumes the
  child's session with it) is refused while the project is paused, since
  rejecting starts new child work.
- **Cancel project** previews affected open, running, and review-pending
  children before confirmation. Confirmation persists intent before stopping
  sessions, cancels unfinished human work, and abandons review results without
  accepting them. Partial teardown remains visibly pending and can be retried
  with the operation ID returned by the next cancellation preview after
  operator intervention.

Cancelling one child never cancels its siblings or parent. Completing or
reopening a parent never fabricates child completion or restarts cancelled
children.

- **Pause project** and **Resume project** toggle a durable paused state,
  recorded with a reason (`operator`, or `owner_failed`/`owner_budget` for a
  future automatic pause). While paused, every child's worker claim and
  interactive Open are refused, and Plan and delegate is refused. A child
  already mid-turn when the pause takes effect finishes normally and its
  result still lands in Review — pause never interrupts running work. Cancel
  and operator Complete remain available while paused. An agent may pause a
  project; only the operator may resume one — a resume request carrying the
  caller-asserted `X-LifeOS-Agent-Session` header is refused.

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

The generated dashboard lists child tasks like ordinary tasks rather than
grouping them beneath a parent. Their relationship is visible only through
the task line's `[parent_id:: ...]` inline field.

A Syncthing conflict copy or in-progress temp file in `LifeOS/Tasks/` is
never shown on the dashboard and never indexed as a task — it's surfaced
instead via `GET /api/tasks/conflicts` so a client can prompt you to resolve
it by hand.

## API Reference

| Method | Endpoint | Parameters | Description |
|--------|----------|------------|-------------|
| POST | `/api/tasks` | description, context, status, priority, due_date, tags, reminder_id, notes, fields, operation_key | Create a task |
| GET | `/api/tasks` | status, context, tag, due_before, query | List/filter tasks |
| GET | `/api/tasks/conflicts` | - | List Syncthing conflict/temp files sitting in the tasks folder |
| GET | `/api/tasks/{id}` | - | Get specific task |
| GET | `/api/tasks/{id}/children` | limit, offset | List every child by stable parent ID |
| PUT | `/api/tasks/{id}` | description, status, context, priority, due_date, tags, notes, fields | Update a task |
| PUT | `/api/tasks/{id}/complete` | - | Mark as done |
| DELETE | `/api/tasks/{id}` | - | Delete a task |
| POST | `/api/tasks/{id}/project/start` | - | Start a project without worker lifecycle tags |
| POST | `/api/tasks/{id}/project/complete` | acknowledge_cancelled_children | Complete a resolved project |
| POST | `/api/tasks/{id}/project/plan` | operation_id | Start or recover an idempotent coordinator run |
| POST | `/api/tasks/{id}/project/cancel` | confirm, operation_id | Preview or execute resumable cascade cancellation |
| POST | `/api/tasks/{id}/project/pause` | reason | Pause a project — blocks child claims, Open, and Plan and delegate |
| POST | `/api/tasks/{id}/project/resume` | - | Resume a paused project; 403 for an agent-attributed caller |
| POST | `/api/tasks/{id}/resume-execution` | - | Resume an execution-paused ordinary task |
| POST | `/api/tasks/human-queue` | title, notes, key, done_when, source_host, source_cwd, source_session | File (or dedupe-update) a Human-queue card |
| GET | `/api/tasks/human-queue` | - | List open Human-queue cards |
| PUT | `/api/tasks/human-queue/{id_or_key}/resolve` | note | Resolve a Human-queue card |

A task response also includes `updated_at` (an ISO-8601 timestamp with a UTC
offset, stamped on every create/update/complete/swap-tag) and additive
hierarchy fields: `parent_id`, `parent_title`, `is_project`, `child_count`,
`hierarchy_valid`, `hierarchy_error`, and a compact `project` progress and
coordination summary. A child also reports `parent_cancellation_pending`,
`parent_handoff_pending`, and `parent_project_paused` so clients can freeze
writes, or refuse claim/Open, appropriately. Summaries are computed from the
complete task set before list filters, so a filtered response does not
undercount hidden children.

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
- [Human Queue](../../guides/human-queue.md) -- Human-queue cards are tasks tagged `human` with status `blocked`
- [Agent Viz](agent-viz.md) -- The `/agents` Kanban board is a card-per-task view backed by this store
