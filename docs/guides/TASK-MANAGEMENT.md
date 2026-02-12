# Task Management Guide

LifeOS provides a task management system fully integrated with Obsidian Tasks plugin. Tasks are stored as markdown checkboxes in your vault and can be managed via chat, API, or Obsidian.

## Storage Format

**Location:** `LifeOS/Tasks/{Context}.md` files in your vault
**Format:** Dataview inline field format
**Index:** `data/task_index.json` (query cache, rebuilt from markdown)

Example task line:
```
- [ ] TODO Call dentist [due:: 2025-02-10] [created:: 2025-02-07] #health <!-- id:abc123 -->
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
    "priority": "high",
    "due_date": "2025-02-10",
    "tags": ["health"]
  }'
```

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
    "priority": "high"
  }'
```

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

The system will:
1. Create the task
2. Create a reminder for Friday 3pm
3. Link them via `reminder_id` field

## Obsidian Dashboard

View and manage all tasks in Obsidian via the Tasks Dashboard:

**Location:** `LifeOS/Tasks/Dashboard.md`

The dashboard includes:
- All open tasks (grouped by file)
- Tasks due this week
- In progress tasks
- Blocked tasks
- Recently completed tasks

The dashboard uses Obsidian Tasks plugin queries and updates automatically as tasks change in Obsidian. It is created by TaskManager on initialization if it doesn't already exist.

## API Reference

| Method | Endpoint | Parameters | Description |
|--------|----------|------------|-------------|
| POST | `/api/tasks` | description, context, priority, due_date, tags, reminder_id | Create a task |
| GET | `/api/tasks` | status, context, tag, due_before, query | List/filter tasks |
| GET | `/api/tasks/{id}` | - | Get specific task |
| PUT | `/api/tasks/{id}` | description, status, context, priority, due_date, tags | Update a task |
| PUT | `/api/tasks/{id}/complete` | - | Mark as done |
| DELETE | `/api/tasks/{id}` | - | Delete a task |

## Technical Details

- Task files are the source of truth (markdown in vault)
- `data/task_index.json` is a query cache rebuilt from markdown
- Vault file watcher triggers automatic reindexing on changes
- Compatible with Obsidian Tasks plugin for viewing/editing in Obsidian
- Uses Dataview inline field format for metadata
