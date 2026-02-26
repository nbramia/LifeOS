# API & MCP Reference

> **Status:** Complete
> **Owner:** API Gateway
> **Last Updated:** 2026-02-19

Complete reference for LifeOS API endpoints and MCP tools.

---

## Table of Contents

1. [API Overview](#api-overview)
2. [Chat & Search Endpoints](#chat--search-endpoints)
3. [Google Integration](#google-integration)
4. [Messaging Endpoints](#messaging-endpoints)
5. [CRM Endpoints](#crm-endpoints)
6. [Memories Endpoints](#memories-endpoints)
7. [Conversations Endpoints](#conversations-endpoints)
8. [Briefing Endpoints](#briefing-endpoints)
9. [People Endpoints](#people-endpoints)
10. [Photos Endpoints](#photos-endpoints)
11. [Task Endpoints](#task-endpoints)
12. [Reminders & Telegram Endpoints](#reminders--telegram-endpoints)
13. [Monarch Money Endpoints](#monarch-money-endpoints)
14. [Job Queue Endpoints](#job-queue-endpoints)
15. [Performance Trace Endpoints](#performance-trace-endpoints)
16. [Admin Endpoints](#admin-endpoints)
17. [MCP Tools](#mcp-tools)

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
3. **Agentic loop** — everything else (including compose, tasks, reminders). Claude gets 15 tools and up to 5 rounds to fetch data and synthesize an answer.

**Agentic loop tools (15):**

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
| `manage_reminders` | Create or list reminders (action: create/list) |
| `create_email_draft` | Gmail draft |
| `save_memory` | Save a memory for future reference |
| `search_memories` | Search previously saved memories |

**Prompt caching:** System prompt and tool definitions use Anthropic `cache_control` breakpoints. Cache reads cost 0.1x input price; repeated queries within 5 minutes hit the cache.

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

### GET /api/crm/people

List/search people with filters.

**Query Parameters:**
- `q` (string): Search query (name, email, company)
- `category` (string): work, personal, family
- `source` (string): gmail, calendar, slack, etc.
- `has_pending` (bool): Has pending links
- `sort` (string): name, last_seen, interaction_count, strength
- `order` (string): asc, desc
- `limit` (int): Results per page (default: 50)
- `offset` (int): Pagination offset

### GET /api/crm/people/{id}

Get person detail with source entities.

### GET /api/crm/people/{id}/timeline

Chronological interaction history.

**Query Parameters:**
- `source_type` (string): Filter by source
- `days_back` (int): Lookback period
- `limit` (int): Max items

### GET /api/crm/people/{id}/connections

Related people with overlap scores.

**Query Parameters:**
- `relationship_type` (string): Filter by type (e.g., "coworker")
- `limit` (int): Max connections to return (default: 50)

**Response:**
```json
{
  "connections": [
    {
      "person_id": "uuid",
      "name": "Alex Johnson",
      "company": "Acme Corp",
      "relationship_type": "coworker",
      "shared_events_count": 42,
      "shared_threads_count": 5,
      "shared_messages_count": 0,
      "shared_whatsapp_count": 0,
      "shared_slack_count": 0,
      "relationship_strength": 91.5,
      "last_seen_together": "2023-02-26T14:00:00"
    }
  ],
  "count": 15
}
```

### GET /api/crm/relationship/insights

Get relationship insights and patterns extracted from therapy notes and conversations.

**Query Parameters:**
- `person_id` (string): Optional, focus on specific person (defaults to primary relationship)

**Response:**
```json
{
  "insights": [
    {
      "id": "uuid",
      "person_id": "uuid",
      "category": "focus_areas",
      "text": "Lead with feelings before facts in conflicts",
      "source_title": "Couples therapy 20230120",
      "source_link": "obsidian://...",
      "source_date": "2023-01-20T00:00:00",
      "confirmed": true,
      "created_at": "2023-02-01T19:54:45",
      "category_icon": "📝"
    }
  ],
  "last_generated": "2023-02-01T23:56:20",
  "confirmed_count": 7,
  "unconfirmed_count": 33
}
```

**Categories:** focus_areas, recurring_themes, relationship_strengths, growth_patterns, for_me, for_partner, ai_suggestions

### GET /api/crm/people/{id}/strength

Detailed relationship strength components.

### GET /api/crm/network

Network graph data (nodes + edges).

**Query Parameters:**
- `center_on` (string): Person ID to center on
- `depth` (int): Graph depth
- `min_strength` (float): Minimum edge strength
- `category` (string): Filter by category

**Response includes edge source breakdown:**
- shared_events_count
- shared_threads_count
- shared_messages_count
- shared_whatsapp_count
- shared_slack_count
- is_linkedin_connection

### GET /api/crm/relationship/{person_a_id}/{person_b_id}

Detailed edge data between two people.

### GET /api/crm/statistics

Dashboard stats (counts by category, source, strength distribution).

### GET /api/crm/people/{id}/source-entities

Get raw source entities linked to a person (low-level, paginated).

**Query Parameters:**
- `limit` (int): Max entities to return (default: 500, max: 5000)
- `offset` (int): Pagination offset

**Response:**
```json
{
  "person_id": "uuid",
  "person_name": "Name",
  "total_count": 49987,
  "returned_count": 500,
  "has_more": true,
  "source_entities": [...]
}
```

### GET /api/crm/people/{id}/contact-sources

**Recommended for split UI.** Get aggregated contact sources (emails, phones, etc.) linked to a person.

Contact sources are the meaningful units for entity splitting - each represents a unique identifier (email address, phone number) rather than individual messages.

**Response:**
```json
{
  "person_id": "uuid",
  "person_name": "Alex Johnson",
  "total_contact_sources": 3,
  "total_observations": 49987,
  "contact_sources": [
    {
      "identifier": "alex.johnson@email.com",
      "identifier_type": "email",
      "source_types": ["gmail", "calendar", "contacts"],
      "observation_count": 49984,
      "source_entity_ids": ["uuid1", "uuid2", "..."],
      "observed_names": ["Alex Johnson", "Alex"],
      "first_seen": "2024-01-15T...",
      "last_seen": "2023-01-29T..."
    },
    {
      "identifier": "+15551234567",
      "identifier_type": "phone",
      "source_types": ["imessage", "whatsapp"],
      "observation_count": 2,
      "source_entity_ids": ["uuid3", "uuid4"],
      "observed_names": ["Alex"],
      "first_seen": "2024-06-01T...",
      "last_seen": "2023-01-28T..."
    }
  ]
}
```

**Identifier Types:**
- `email` - Email address (appears in gmail, calendar, contacts, etc.)
- `phone` - Phone number in E.164 format (appears in imessage, whatsapp, phone)
- `slack_user` - Slack workspace user ID
- `linkedin_profile` - LinkedIn profile URL
- `name_only` - Vault/Granola mentions with no email/phone

### POST /api/crm/people/split

Split source entities from one person to another.

**Request:**
```json
{
  "from_person_id": "uuid",
  "to_person_id": "uuid",           // OR
  "new_person_name": "New Person",  // Create new person
  "source_entity_ids": ["uuid1", "uuid2"],
  "create_overrides": true          // Create disambiguation rules
}
```

**Response:**
```json
{
  "status": "completed",
  "from_person_id": "uuid",
  "to_person_id": "uuid",
  "source_entities_moved": 5,
  "interactions_moved": 10,
  "overrides_created": 2
}
```

### GET /api/crm/link-overrides

List disambiguation rules that prevent future entity mis-linking.

### DELETE /api/crm/link-overrides/{id}

Delete a link override rule.

### POST /api/crm/people/merge

Merge two person records. Combines all interactions, relationships, and source entities from the secondary person into the primary person.

**Request:**
```json
{
  "primary_id": "uuid",
  "secondary_ids": ["uuid1", "uuid2"]
}
```

**Response:**
```json
{
  "status": "completed",
  "primary_id": "uuid",
  "merged_ids": ["uuid1", "uuid2"],
  "stats": {
    "interactions_updated": 156,
    "source_entities_updated": 12,
    "emails_merged": 3,
    "phones_merged": 1,
    "aliases_added": 2
  }
}
```

### POST /api/crm/relationships/discover

Trigger full relationship discovery. Scans interactions to find/update relationships between people.

**Response:**
```json
{
  "status": "completed",
  "duration_seconds": 12.5,
  "relationships_created": 45,
  "relationships_updated": 120
}
```

### POST /api/crm/strengths/update

Recalculate relationship strength for all people.

**Response:**
```json
{
  "status": "completed",
  "updated": 542,
  "failed": 0,
  "total": 542
}
```

### GET /api/crm/discover

Get suggested connections and relationship insights for UI.

**Query Parameters:**
- `person_id` (string, optional): Focus on specific person
- `limit` (int): Max suggestions to return

**Response:**
```json
{
  "suggested_connections": [
    {
      "person_a": {"id": "uuid", "name": "Alex"},
      "person_b": {"id": "uuid", "name": "Jordan"},
      "reason": "3 shared calendar events, 5 email threads",
      "confidence": 0.85
    }
  ],
  "network_insights": {
    "total_people": 542,
    "connected_people": 380,
    "bridge_people": ["uuid1", "uuid2"]
  }
}
```

### GET /api/crm/people/{id}/facts

Get extracted facts about a person (auto-extracted from interactions).

**Response:**
```json
{
  "person_id": "uuid",
  "person_name": "Alex Johnson",
  "facts": [
    {
      "id": "uuid",
      "category": "work",
      "content": "Works at Acme Corp as VP Engineering",
      "confidence": 0.9,
      "source": "calendar:meeting-uuid",
      "created_at": "2023-01-15T...",
      "confirmed": false
    }
  ]
}
```

### POST /api/crm/people/{id}/facts/extract

Trigger fact extraction for a person using LLM.

### PUT /api/crm/people/{id}/facts/{fact_id}

Update a fact's content or category.

### DELETE /api/crm/people/{id}/facts/{fact_id}

Delete a fact.

### POST /api/crm/people/{id}/facts/{fact_id}/confirm

Mark a fact as confirmed/verified.

### GET /api/crm/review-queue

Get pending entity links requiring human review.

**Query Parameters:**
- `min_confidence` (float): Minimum confidence threshold
- `limit` (int): Max items to return

### POST /api/crm/review-queue/{entity_id}/confirm

Confirm an entity link (mark as correct).

### POST /api/crm/review-queue/{entity_id}/reject

Reject an entity link (mark as incorrect, will be unlinked).

### GET /api/crm/data-health

Data coverage and sync health report.

### GET /api/crm/data-health/summary

Summary for UI display.

### GET /api/crm/config

Get CRM configuration values for frontend (owner person ID, work email domain, partner ID, family default selected IDs).

### Me Dashboard

#### GET /api/crm/me/stats

Aggregate statistics for the owner's personal dashboard. Returns total people, emails, meetings, and messages across the CRM.

#### GET /api/crm/me/timeline

Chronological interaction history for the owner. Returns ALL interactions across all people (since all interactions involve the owner).

**Query Parameters:**
- `source_type` (string): Filter by source type (comma-separated for compound filters)
- `days_back` (int): Lookback period (default: 365)
- `date` (string): Filter to specific date (YYYY-MM-DD)
- `offset` (int): Pagination offset
- `limit` (int): Max results (default: 50)

#### GET /api/crm/me/interactions

Aggregated interaction data for the "Me" dashboard. Returns pre-aggregated data for heatmaps, charts, trends, network growth, and messaging volume by Dunbar circle.

**Query Parameters:**
- `days_back` (int): Days of history (default: 365, max: 3660)
- `trend_period` (string): Trend comparison period (week, month, quarter, year)
- `health_period` (string): Health score history period (month, quarter, year)

### Family

#### GET /api/crm/family/members

Get configured family members with relationship data.

#### GET /api/crm/family/stats

Aggregate family statistics.

**Query Parameters:**
- `member_ids` (string): Comma-separated person IDs to include

#### GET /api/crm/family/timeline

Family interaction timeline across selected members.

**Query Parameters:**
- `member_ids` (string): Comma-separated person IDs
- `source_type` (string): Filter by source type
- `days_back` (int): Lookback period
- `limit` (int): Max results

#### GET /api/crm/family/interactions

Aggregated family interaction data for charts and heatmaps.

**Query Parameters:**
- `member_ids` (string): Comma-separated person IDs
- `days_back` (int): Days of history

#### GET /api/crm/family/channel-mix

Communication channel breakdown for family members. Shows distribution across email, calendar, messaging, etc.

**Query Parameters:**
- `member_ids` (string): Comma-separated person IDs
- `days_back` (int): Lookback period

### Birthdays

#### GET /api/crm/birthdays/today

Get all people with birthdays today.

#### GET /api/crm/birthdays/all

Get all people with birthdays, grouped by date (MM-DD format).

### Sync Health

#### GET /api/crm/sync/health

Get health status for all sync sources. Returns staleness, last sync time, and error status for each source.

#### GET /api/crm/sync/health/summary

Summary of sync health across all sources. Returns counts of healthy, stale, and failed sources.

#### GET /api/crm/sync/health/{source}

Get health status for a specific sync source.

#### GET /api/crm/sync/errors

Get recent sync errors for debugging.

**Query Parameters:**
- `source` (string): Filter by source
- `limit` (int): Max results (default: 50)

### Cleanup

#### GET /api/crm/cleanup/queue

Entity cleanup review queue. Returns entities needing human review (duplicates, non-human, over-merged).

**Query Parameters:**
- `review_type` (string): Filter by type (duplicate, non_human, over_merged)
- `limit` (int): Max results (default: 5000)
- `offset` (int): Pagination offset

#### GET /api/crm/cleanup/stats

Statistics about the cleanup review queue. Returns counts by status and type.

#### POST /api/crm/cleanup/{item_id}/skip

Skip a cleanup item (mark as different people / not a duplicate).

#### POST /api/crm/cleanup/{item_id}/keep

Keep a non-human candidate as a real person (false positive).

#### POST /api/crm/cleanup/{item_id}/hide

Hide a non-human entity and add to blocklist.

#### POST /api/crm/cleanup/{item_id}/merge

Merge entities from the cleanup queue.

**Query Parameters:**
- `primary_id` (string): ID of person to keep (required for non_human items)

### Relationship Insights

#### GET /api/crm/relationship/insights

Get relationship insights extracted from therapy notes and conversations (already documented above).

#### POST /api/crm/relationship/insights/generate

Generate new relationship insights using Claude. Keeps confirmed insights, regenerates unconfirmed ones.

**Query Parameters:**
- `person_id` (string, optional): Target person (defaults to partner)
- `category` (string, optional): Only regenerate for this category

#### POST /api/crm/relationship/insights/{insight_id}/confirm

Mark an insight as confirmed. Confirmed insights persist across regenerations.

#### DELETE /api/crm/relationship/insights/{insight_id}

Delete/dismiss an insight.

### Tone Analysis

#### POST /api/crm/relationship/tone-analysis

Analyze tone/sentiment in iMessage conversations over time. Samples messages monthly and uses Claude to classify emotional warmth (0-100 scale).

**Query Parameters:**
- `person_id` (string, optional): Target person (defaults to partner)
- `months` (int): Months to analyze (default: 12)

#### POST /api/crm/relationship/tone-analysis-detailed

Detailed tone analysis with separate scores for the user and their partner. Groups messages by week, analyzes each person separately, then aggregates to monthly averages.

**Query Parameters:**
- `person_id` (string, optional): Target person (defaults to partner)
- `months` (int): Months to analyze (default: 12)

### CRM Slack Integration

#### GET /api/crm/slack/status

Get Slack OAuth integration status (configured, connected, workspaces).

#### GET /api/crm/slack/oauth/start

Start Slack OAuth flow. Returns the authorization URL.

#### GET /api/crm/slack/callback

Handle Slack OAuth callback. Exchanges authorization code for access token.

#### POST /api/crm/slack/sync

Sync Slack users to the CRM. Creates SourceEntity records for workspace users.

**Query Parameters:**
- `workspace_id` (string): Workspace to sync (default: "default")

#### DELETE /api/crm/slack/disconnect

Disconnect a Slack workspace. Removes the stored OAuth token.

**Query Parameters:**
- `workspace_id` (string): Workspace to disconnect (default: "default")

### Contacts Sync

#### GET /api/crm/contacts/status

Get Apple Contacts integration status (availability and authorization).

#### POST /api/crm/contacts/sync

Sync Apple Contacts to the CRM. Creates SourceEntity records for all contacts. Requires macOS and Contacts permission.

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

## Reminders & Telegram Endpoints

Reminders can also be created, edited, listed, and deleted via natural language through the chat interface (`POST /api/ask/stream`). See [Reminders Guide](../guides/REMINDERS.md).

### POST /api/reminders

Create a scheduled reminder. Supports `schedule_type` of `once` (ISO datetime) or `cron`, and `message_type` of `static`, `prompt` (runs through chat pipeline), or `endpoint` (calls a LifeOS API endpoint).

### GET /api/reminders

List all reminders.

### GET /api/reminders/{id}

Get a specific reminder.

### PUT /api/reminders/{id}

Update a reminder.

### DELETE /api/reminders/{id}

Delete a reminder.

### POST /api/reminders/{id}/trigger

Manually trigger a reminder (for testing).

### POST /api/reminders/send

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

Basic health check. Verifies API key and reminder scheduler status.

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

### Granola Processor

#### GET /api/admin/granola/status

Granola processor status (running, pending files, interval).

#### POST /api/admin/granola/process

Process all pending files in the Granola inbox immediately. Classifies and moves files to appropriate destinations.

#### POST /api/admin/granola/start

Start the Granola processor (runs every 5 minutes).

#### POST /api/admin/granola/stop

Stop the Granola processor.

#### POST /api/admin/granola/reclassify

Reclassify Granola files that may have been incorrectly categorized.

**Request:**
```json
{
  "folder": "Work/ML/People/Hiring"
}
```

#### POST /api/admin/granola/deduplicate

Find and remove duplicate Granola files in the vault (based on granola_id).

### Omi Processor

#### GET /api/admin/omi/status

Omi events processor status (running, pending files, interval).

#### POST /api/admin/omi/process

Process all pending files in the Omi/Events folder immediately.

#### POST /api/admin/omi/start

Start the Omi processor (runs every 5 minutes).

#### POST /api/admin/omi/stop

Stop the Omi processor.

#### POST /api/admin/omi/reclassify

Reclassify Omi files that may have been incorrectly categorized.

**Request:**
```json
{
  "folder": "Personal/Omi"
}
```

#### POST /api/admin/omi/deduplicate

Find and remove duplicate Omi files in the vault (based on omi_id).

### Usage Tracking

#### GET /api/admin/usage

Get usage summary with stats for 24h, 7d, 30d, and all-time. Includes daily cost breakdown for charting.

---

## MCP Tools

The MCP server exposes curated API endpoints as Claude Code tools.

### Setup

```bash
claude mcp add lifeos -s user -- python /path/to/LifeOS/mcp_server.py
```

### Available Tools

| Tool | Maps To | Description |
|------|---------|-------------|
| `lifeos_ask` | POST /api/ask | Query with synthesis |
| `lifeos_search` | POST /api/search | Raw search results |
| `lifeos_calendar_upcoming` | GET /api/calendar/upcoming | Upcoming events |
| `lifeos_calendar_search` | GET /api/calendar/search | Search events |
| `lifeos_meeting_prep` | GET /api/calendar/meeting-prep | Meeting prep context |
| `lifeos_gmail_search` | GET /api/gmail/search | Search emails |
| `lifeos_gmail_draft` | POST /api/gmail/drafts | Create draft |
| `lifeos_drive_search` | GET /api/drive/search | Search Drive |
| `lifeos_imessage_search` | GET /api/imessage/search | Search messages |
| `lifeos_slack_search` | POST /api/slack/search | Search Slack |
| `lifeos_people_search` | GET /api/people/search | Search people |
| `lifeos_person_profile` | GET /api/crm/people/{id} | Full CRM profile |
| `lifeos_person_facts` | GET /api/crm/people/{id}/facts | Extracted facts |
| `lifeos_person_timeline` | GET /api/crm/people/{id}/timeline | Interaction history |
| `lifeos_person_connections` | GET /api/crm/people/{id}/connections | Who someone works with |
| `lifeos_relationship_insights` | GET /api/crm/relationship/insights | Relationship patterns |
| `lifeos_communication_gaps` | GET /api/crm/family/communication-gaps | Find neglected relationships |
| `lifeos_photos_person` | GET /api/photos/person/{id} | Photos of a person |
| `lifeos_photos_shared` | GET /api/photos/shared/{a}/{b} | Photos of two people together |
| `lifeos_photos_stats` | GET /api/photos/stats | Photos library statistics |
| `lifeos_monarch_accounts` | GET /api/monarch/accounts | Financial account balances |
| `lifeos_monarch_transactions` | GET /api/monarch/transactions | Search transactions |
| `lifeos_monarch_cashflow` | GET /api/monarch/cashflow | Income/expense summary |
| `lifeos_monarch_budgets` | GET /api/monarch/budgets | Budget vs actual |
| `lifeos_task_create` | POST /api/tasks | Create a task |
| `lifeos_task_list` | GET /api/tasks | List/filter tasks |
| `lifeos_task_update` | PUT /api/tasks/{id} | Update a task |
| `lifeos_task_complete` | PUT /api/tasks/{id}/complete | Mark task done |
| `lifeos_task_delete` | DELETE /api/tasks/{id} | Delete a task |
| `lifeos_reminder_create` | POST /api/reminders | Create scheduled reminder |
| `lifeos_reminder_list` | GET /api/reminders | List all reminders |
| `lifeos_reminder_delete` | DELETE /api/reminders/{id} | Delete a reminder |
| `lifeos_telegram_send` | POST /api/reminders/send | Send Telegram message |
| `lifeos_memories_create` | POST /api/memories | Save memory |
| `lifeos_memories_search` | GET /api/memories/search | Search memories |
| `lifeos_conversations_list` | GET /api/conversations | List chats |
| `lifeos_person_update` | PATCH /api/crm/people/{id} | Update person profile |
| `lifeos_person_fact_update` | PUT /api/crm/people/{id}/facts/{fid} | Update a fact |
| `lifeos_person_fact_confirm` | POST /api/crm/people/{id}/facts/{fid}/confirm | Confirm a fact |
| `lifeos_person_fact_delete` | DELETE /api/crm/people/{id}/facts/{fid} | Delete a fact |
| `lifeos_reminder_update` | PUT /api/reminders/{id} | Update a reminder |
| `lifeos_sync_trigger` | Custom routing (see [mcp-tools.md](mcp-tools.md)) | Trigger data sync by source |
| `lifeos_calendar_create` | POST /api/calendar/events | Create calendar event |
| `lifeos_calendar_update` | PUT /api/calendar/events/{id} | Update calendar event |
| `lifeos_calendar_delete` | DELETE /api/calendar/events/{id} | Delete calendar event |
| `lifeos_health` | GET /health/services | Service health check |

See [MCP Tools PRD](../prd/MCP-TOOLS.md) for detailed tool specifications.

## Related Documents

- [MCP Tools](mcp-tools.md) -- MCP server setup and tool specs
- [Data & Sync](../technical/data-and-sync.md) -- Data sources and sync pipeline
- [Chat UI](chat-ui.md) -- Chat interface product spec
- [CRM UI](crm-ui.md) -- CRM interface product spec
