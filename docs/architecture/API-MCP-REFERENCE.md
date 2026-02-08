# API & MCP Reference

Complete reference for LifeOS API endpoints and MCP tools.

**Related Documentation:**
- [MCP Tools PRD](../prd/MCP-TOOLS.md) - MCP server setup and tool specs
- [Data & Sync](DATA-AND-SYNC.md) - Data sources and sync

---

## Table of Contents

1. [API Overview](#api-overview)
2. [Chat & Search Endpoints](#chat--search-endpoints)
3. [Google Integration](#google-integration)
4. [Messaging Endpoints](#messaging-endpoints)
5. [CRM Endpoints](#crm-endpoints)
6. [Task Endpoints](#task-endpoints)
7. [Reminders & Telegram Endpoints](#reminders--telegram-endpoints)
8. [Admin Endpoints](#admin-endpoints)
9. [MCP Tools](#mcp-tools)

---

## API Overview

**Base URL:** `http://localhost:8000`

**Authentication:** None (Tailscale-only access)

**OpenAPI Spec:** `GET /openapi.json`

---

## Chat & Search Endpoints

### POST /api/ask/stream

Streaming chat with RAG. Returns SSE stream with routing, sources, content, and done events.

**Request:**
```json
{
  "question": "What did we discuss in the product meeting?",
  "conversation_id": "optional-uuid",
  "include_sources": true
}
```

**Response:** Server-Sent Events stream

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

### GET /api/search/recent

Get recently modified documents.

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

### GET /api/crm/people/{id}/strength-breakdown

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
  "primary_person_id": "uuid",
  "secondary_person_id": "uuid"
}
```

**Response:**
```json
{
  "status": "completed",
  "primary_person_id": "uuid",
  "secondary_person_id": "uuid",
  "merged": {
    "emails": 3,
    "phones": 1,
    "aliases": 2,
    "sources": 4,
    "interactions": 156,
    "source_entities": 12,
    "relationships": 8,
    "facts": 5
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

---

## People Endpoints

### GET /api/people/{name}

Person information.

### GET /api/people/{name}/briefing

Stakeholder briefing.

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

## Admin Endpoints

### GET /api/admin/health

Health check.

### GET /health/full

Full health check including all services.

### POST /api/admin/reindex

Trigger vault reindex (background).

### POST /api/admin/reindex/sync

Trigger vault reindex (blocking).

### GET /api/admin/calendar/status

Calendar indexer status.

### POST /api/admin/calendar/sync

Trigger calendar sync.

### POST /api/admin/calendar/scheduler/start

Start calendar scheduler.

### POST /api/admin/calendar/scheduler/stop

Stop calendar scheduler.

### GET /api/admin/granola/status

Granola processor status.

### POST /api/admin/granola/process

Process Granola inbox.

### GET /api/admin/omi/status

Omi processor status.

### POST /api/admin/omi/process

Process Omi events.

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
| `lifeos_people_search` | GET /api/crm/people | Search people |
| `lifeos_person_profile` | GET /api/crm/people/{id} | Full CRM profile |
| `lifeos_person_facts` | GET /api/crm/people/{id}/facts | Extracted facts |
| `lifeos_person_timeline` | GET /api/crm/people/{id}/timeline | Interaction history |
| `lifeos_person_connections` | GET /api/crm/people/{id}/connections | Who someone works with |
| `lifeos_relationship_insights` | GET /api/crm/relationship/insights | Relationship patterns |
| `lifeos_communication_gaps` | GET /api/crm/family/communication-gaps | Find neglected relationships |
| `lifeos_photos_person` | GET /api/photos/person/{id} | Photos of a person |
| `lifeos_photos_shared` | GET /api/photos/shared/{a}/{b} | Photos of two people together |
| `lifeos_photos_stats` | GET /api/photos/stats | Photos library statistics |
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
| `lifeos_health` | GET /health/full | Health check |

See [MCP Tools PRD](../prd/MCP-TOOLS.md) for detailed tool specifications.
