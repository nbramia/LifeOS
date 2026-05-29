# API Reference

**Status:** Complete
**Owner:** API Gateway
**Last Updated:** 2026-05-27

Catalog of every HTTP endpoint LifeOS exposes, with request/response shapes. Two adjacent catalogs split out for size:

- **CRM endpoints** (`/api/crm/*`) → [api-crm.md](api-crm.md)
- **MCP tool catalog** (Claude Code / Managed Agents tools) → [mcp-tools.md](mcp-tools.md)

---

## Table of Contents

1. [API Overview](#api-overview)
2. [Chat & Search Endpoints](#chat--search-endpoints)
3. [Google Integration](#google-integration)
4. [Messaging Endpoints](#messaging-endpoints)
5. [CRM Endpoints — see api-crm.md](api-crm.md)
6. [Memories Endpoints](#memories-endpoints)
7. [Conversations Endpoints](#conversations-endpoints)
8. [Briefing Endpoints](#briefing-endpoints)
9. [People Endpoints](#people-endpoints)
10. [Photos Endpoints](#photos-endpoints)
11. [Task Endpoints](#task-endpoints)
12. [Scheduler & Telegram Endpoints](#scheduler--telegram-endpoints)
13. [Monarch Money Endpoints](#monarch-money-endpoints)
14. [Job Queue Endpoints](#job-queue-endpoints)
15. [Performance Trace Endpoints](#performance-trace-endpoints)
16. [Admin Endpoints](#admin-endpoints)
17. [MCP Tools — see mcp-tools.md](mcp-tools.md)

---

## API Overview

**Base URL:** `http://localhost:8000`

**Authentication:** None (Tailscale-only access)

**OpenAPI Spec:** `GET /openapi.json`

---

## Chat & Search Endpoints

### POST /api/ask/stream

Streaming chat with an agentic pipeline. Claude autonomously decides which tools to call, iterating when initial results are insufficient. Returns an SSE stream.

**Request:**
```json
{
  "question": "What did we discuss in the product meeting?",
  "conversation_id": "optional-uuid",
  "include_sources": true
}
```

**Response:** Server-Sent Events stream with event types:

| Event | Fields | Description |
|-------|--------|-------------|
| `routing` | `sources`, `reasoning`, `latency_ms` | Which pipeline path was selected |
| `status` | `message` | Tool execution status (e.g. "Searching notes...") |
| `content` | `content` | Streamed response text chunk |
| `sources` | `sources` | Data sources used (vault, calendar, gmail, etc.) |
| `code_intent` | `task` | Query requires Claude Code (terminal/filesystem) |
| `usage` | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd` | Token usage and cost |
| `done` | — | Stream complete |

**Pipeline routing (in order of priority):**
1. **Ambiguous task/reminder** — asks user for clarification (task vs reminder vs both).
2. **Code intent** — terminal, filesystem, browser tasks. Yields `code_intent` event for Telegram to spawn Claude Code.
3. **Agentic loop** — everything else (including compose, tasks, reminders). Claude gets 18 tools and up to 5 rounds to fetch data and synthesize an answer. See `api/services/agent_tools.py::TOOL_DEFINITIONS` for the canonical list.

**Agentic loop tools (18):**

| Tool | Description |
|------|-------------|
| `search_vault` | Obsidian notes, journals, meeting transcripts |
| `read_vault_file` | Read full vault file by name (fuzzy matched) |
| `search_calendar` | Google Calendar (personal + work) |
| `search_email` | Gmail (personal + work) |
| `search_drive` | Google Drive files |
| `search_slack` | Slack messages |
| `search_web` | Web search (weather, news, public info) |
| `get_message_history` | iMessage/WhatsApp (requires entity_id) |
| `person_info` | Lookup or briefing (action: lookup/briefing) |
| `search_finances` | Monarch Money live data (action: accounts/transactions/cashflow/budgets) |
| `manage_tasks` | Create, list, or complete tasks (action: create/list/complete) |
| `manage_schedules` | Create or list schedules (action: create/list; schedule_action: notify/prompt/endpoint/agent). `manage_reminders` kept as a deprecated alias |
| `create_email_draft` | Gmail draft (returns a draft_id; never sends) |
| `send_email_draft` | Send an existing Gmail draft by draft_id (gated: only after the user confirms in a later turn — a draft created this turn cannot be sent) |
| `create_calendar_event` | Create a Google Calendar event |
| `update_calendar_event` | Update a Google Calendar event |
| `delete_calendar_event` | Delete a Google Calendar event |
| `save_memory` | Save a memory for future reference |
| `search_memories` | Search previously saved memories |

**Prompt caching (Anthropic backend only):** System prompt and tool definitions use Anthropic `cache_control` breakpoints. Cache reads cost 0.1x input price; repeated queries within 5 minutes hit the cache.

### POST /api/search

Vector similarity search across indexed content.

**Request:**
```json
{
  "query": "budget planning",
  "filters": {
    "note_type": ["meeting"],
    "people": ["John"],
    "date_from": "2023-01-01",
    "date_to": "2023-01-31"
  },
  "top_k": 20
}
```

---

## Google Integration

### GET /api/calendar/upcoming

Get upcoming calendar events.

**Query Parameters:**
- `days` (int): Days to look ahead (default: 7)

### GET /api/calendar/search

Search calendar events.

**Query Parameters:**
- `q` (string): Search query
- `attendee` (string): Filter by attendee

### GET /api/calendar/meeting-prep

Get intelligent meeting preparation context for a date.

**Query Parameters:**
- `date` (string): Date in YYYY-MM-DD format (defaults to today)
- `include_all_day` (bool): Include all-day events (default: false)
- `max_related_notes` (int): Max notes per meeting (1-10, default: 4)

**Response:**
```json
{
  "date": "2023-02-03",
  "count": 5,
  "meetings": [
    {
      "event_id": "...",
      "title": "1:1 with Kevin",
      "start_time": "10:00 AM",
      "end_time": "10:30 AM",
      "attendees": ["kevin@example.com"],
      "related_notes": [
        {
          "title": "Kevin",
          "path": "/path/to/People/Kevin.md",
          "relevance": "attendee"
        },
        {
          "title": "1:1 with Kevin 20230127",
          "path": "/path/to/Meetings/...",
          "relevance": "past_meeting",
          "date": "2023-01-27"
        }
      ],
      "attachments": []
    }
  ]
}
```

### GET /api/gmail/search

Search emails.

**Query Parameters:**
- `q` (string): Search query
- `from` (string): Filter by sender
- `after` (string): After date
- `before` (string): Before date
- `account` (string): personal or work

### POST /api/gmail/drafts

Create a Gmail draft.

**Request:**
```json
{
  "to": "recipient@example.com",
  "subject": "Subject line",
  "body": "Email content",
  "cc": "optional@example.com",
  "html": false,
  "account": "personal"
}
```

**Response:**
```json
{
  "draft_id": "draft-id",
  "gmail_url": "https://mail.google.com/..."
}
```

### POST /api/gmail/send

Send an existing draft (created via `POST /api/gmail/drafts`) by its `draft_id`. The exact draft is sent — there is no compose-and-send shortcut, which keeps a review step in front of every outbound email. Only send after the user has reviewed the draft and explicitly confirmed.

**Request:**
```json
{
  "draft_id": "draft-id"
}
```
Query param: `account` (personal or work; must match where the draft was created).

**Response:**
```json
{
  "message_id": "sent-message-id",
  "source_account": "personal"
}
```

### GET /api/drive/search

Search Google Drive files.

**Query Parameters:**
- `q` (string): Search query
- `account` (string): personal or work

---

## Messaging Endpoints

### GET /api/imessage/search

Search iMessage/SMS history.

**Query Parameters:**
- `q` (string): Text content search
- `phone` (string): Filter by phone (E.164 format)
- `entity_id` (string): Filter by PersonEntity ID
- `after` (string): Messages after date (YYYY-MM-DD)
- `before` (string): Messages before date
- `direction` (string): sent or received
- `max_results` (int): Max results (1-200, default: 50)

### GET /api/imessage/conversations

Recent conversations summary.

### GET /api/imessage/statistics

Message database statistics.

### GET /api/imessage/person/{entity_id}

Messages with a specific person.

### GET /api/slack/status

Slack integration status and index statistics.

### POST /api/slack/search

Semantic search across Slack messages.

**Request:**
```json
{
  "query": "project update",
  "top_k": 20,
  "channel_id": "optional",
  "user_id": "optional"
}
```

### GET /api/slack/conversations

List DMs and channels.

### POST /api/slack/sync

Trigger full or incremental sync.

### GET /api/slack/channels/{channel_id}/messages

Get live messages from a channel.

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

List all conversations.

### POST /api/conversations

Create new conversation.

### GET /api/conversations/{id}

Get conversation with messages.

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

Search people by name or email.

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

Trigger Photos sync (matches faces to PersonEntity, creates interactions).

### GET /api/photos/thumbnail/{uuid}

Get a thumbnail image for a photo by UUID. Returns the image file from the Photos library derivatives folder. Returns 410 (Gone) with `X-iCloud-Only` header if the photo is in iCloud only.

### GET /api/photos/profile/{person_id}

Get a profile photo thumbnail for a person. Returns the most recent available photo thumbnail for use as an avatar.

### GET /api/photos/open/{uuid}

Open a photo in Preview or Photos app. Tries the original file first, falls back to thumbnail, then the Photos app.

---

## Task Endpoints

Tasks can also be created, completed, listed, and deleted via natural language through the chat interface (`POST /api/ask/stream`). See [Task Management Guide](../guides/TASK-MANAGEMENT.md).

### POST /api/tasks

Create a task. Stored as an Obsidian Tasks-compatible markdown checkbox in the vault.

**Request:**
```json
{
  "description": "Call dentist",
  "context": "Personal",
  "priority": "high",
  "due_date": "2025-02-10",
  "tags": ["health"],
  "reminder_id": "optional-linked-reminder-uuid"
}
```

### GET /api/tasks

List/filter tasks.

**Query Parameters:**
- `status` (string): Filter by status (todo, done, in_progress, cancelled, deferred, blocked, urgent)
- `context` (string): Filter by context (Work, Personal, Finance, etc.)
- `tag` (string): Filter by tag
- `due_before` (string): YYYY-MM-DD, tasks due before this date
- `query` (string): Fuzzy text search across task descriptions

### GET /api/tasks/{id}

Get a specific task.

### PUT /api/tasks/{id}

Update a task (description, status, context, priority, due_date, tags).

### PUT /api/tasks/{id}/complete

Mark a task as done (adds done date automatically).

### DELETE /api/tasks/{id}

Delete a task.

---

## Scheduler & Telegram Endpoints

Schedules can also be created, edited, listed, and deleted via natural language through the chat interface (`POST /api/ask/stream`). See [Scheduler Guide](../guides/scheduler.md).

> The legacy `/api/reminders*` endpoints remain mounted as deprecation-logged aliases over the same store.

### POST /api/scheduler

Create a schedule. Supports `schedule_type` of `once` (ISO datetime) or `cron`, and `action` of `notify` (static text), `prompt` (runs through the chat pipeline), `endpoint` (calls a LifeOS API endpoint), or `agent` (hands off to the agent worker via an `#agent` task, with `executor` = `local`/`cloud`/`cloud-haiku`/`cloud-sonnet`).

### GET /api/scheduler

List all schedules.

### GET /api/scheduler/{id}

Get a specific schedule.

### PUT /api/scheduler/{id}

Update a schedule.

### DELETE /api/scheduler/{id}

Delete a schedule.

### POST /api/scheduler/{id}/trigger

Manually fire a schedule (for testing).

### POST /api/scheduler/send

Send an ad-hoc message via Telegram.

---

## Monarch Money Endpoints

Live financial data from Monarch Money. Monthly summaries are also synced to the vault.

### GET /api/monarch/accounts

List all financial accounts with current balances.

**Response:** Array of `{name, type, subtype, balance, institution, last_updated}`

### GET /api/monarch/transactions

Search recent transactions.

**Query Parameters:**
- `start_date` (string): YYYY-MM-DD (default: 30 days ago)
- `end_date` (string): YYYY-MM-DD (default: today)
- `category` (string): Filter by category name
- `search` (string): Search merchant names
- `limit` (int): Max results (default: 500)

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

Basic health check. Verifies API key and scheduler status.

### GET /health/full

Comprehensive health check. Tests all services (ChromaDB, vault search, calendar, Gmail, Drive, people, conversations, memories, iMessage) with per-service latency.

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

Trigger calendar sync.

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

Get usage summary with stats for 24h, 7d, 30d, and all-time. Includes daily cost breakdown for charting.

---


## Related Documents

- [api-crm.md](api-crm.md) — `/api/crm/*` HTTP endpoints (split out from this file)
- [mcp-tools.md](mcp-tools.md) — MCP tool catalog (the canonical home — was previously duplicated here)
- [Data & Sync](../technical/data-and-sync.md) — Data sources and sync pipeline
- [Chat UI](chat-ui.md) — Chat interface product spec
- [CRM UI](crm-ui.md) — CRM index pointing at the four product sub-specs
- [Configuration](../../guides/configuration.md) — Env vars referenced by several endpoints (LIFEOS_USER_NAME, LIFEOS_WORK_DOMAIN, etc.)
