# MCP Server & Tool Audit

**Date:** 2026-02-13
**Auditor:** MCP Integration Specialist
**Scope:** Full audit of `mcp_server.py`, all 35 exposed tools, API coverage, and capability gaps

---

## 1. Architecture Overview

### How the MCP Server Works

The MCP server (`mcp_server.py`) is a **dynamic, OpenAPI-driven tool proxy**. It:

1. Fetches the OpenAPI spec from the running LifeOS API (`/openapi.json`)
2. Matches a curated list of endpoints (`CURATED_ENDPOINTS`) against the spec
3. Builds JSON Schema tool definitions from the spec's parameter/body schemas
4. Proxies tool calls as HTTP requests to the API
5. Formats responses into human-readable markdown

**Protocol:** JSON-RPC over stdin/stdout (MCP 2024-11-05 protocol)
**Transport:** Synchronous httpx client with 30s timeout
**Registration:** `claude mcp add lifeos -s user -- python /path/to/mcp_server.py`

### Key Design Decisions

- **Curated endpoints**: Only endpoints explicitly listed in `CURATED_ENDPOINTS` are exposed. This is intentional filtering, not a limitation.
- **OpenAPI-first with fallback**: The server tries to build schemas from the live OpenAPI spec. If the API is down at startup, it falls back to hardcoded schemas.
- **Response formatting**: Each tool has a custom `_format_response` handler that converts JSON API responses into markdown text.
- **Email body enrichment**: Gmail search automatically fetches full bodies for the top 5 results (via `_fetch_email_body`).

---

## 2. Current Tool Inventory (35 tools)

### 2.1 Knowledge & Search (3 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_ask` | `/api/ask` | POST | RAG synthesis with citations |
| `lifeos_search` | `/api/search` | POST | Raw vector+BM25 search |
| `lifeos_health` | `/health/full` | GET | Service health check |

**Assessment:**
- `lifeos_ask` and `lifeos_search` are well-differentiated (synthesis vs. raw)
- Descriptions clearly explain when to use which
- Missing: No way to read a specific vault file by name (the Telegram agent has `read_vault_file` but MCP doesn't expose it)

### 2.2 Communication - Read (5 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_gmail_search` | `/api/gmail/search` | GET | Search emails with full body |
| `lifeos_imessage_search` | `/api/imessage/search` | GET | Search iMessage/SMS history |
| `lifeos_slack_search` | `/api/slack/search` | POST | Semantic search across Slack |
| `lifeos_calendar_upcoming` | `/api/calendar/upcoming` | GET | Upcoming calendar events |
| `lifeos_calendar_search` | `/api/calendar/search` | GET | Search past/future events |

**Assessment:**
- Good coverage of primary communication channels
- Gmail enrichment (fetching bodies) is a smart optimization
- Slack search is semantic (vector-based), which is powerful
- Missing: WhatsApp search (data exists in interaction store but no dedicated endpoint), phone call history search
- Missing: iMessage conversation list (endpoint exists at `/api/imessage/conversations`)

### 2.3 Communication - Write (2 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_gmail_draft` | `/api/gmail/drafts` | POST | Create email draft |
| `lifeos_telegram_send` | `/api/reminders/send` | POST | Send Telegram message |

**Assessment:**
- Email drafts are **read-then-review** (user must manually send) -- good safety pattern
- Telegram send is the only truly "fire and forget" write operation
- **Critical gap**: No ability to send iMessages or Slack messages (these would be the most useful write capabilities)
- Missing: No calendar event creation, no email sending (only drafts)

### 2.4 People & CRM (8 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_people_search` | `/api/people/search` | GET | Search people, get entity_id |
| `lifeos_person_profile` | `/api/crm/people/{person_id}` | GET | Full CRM profile |
| `lifeos_person_timeline` | `/api/crm/people/{person_id}/timeline` | GET | Chronological interactions |
| `lifeos_person_facts` | `/api/crm/people/{person_id}/facts` | GET | Extracted facts |
| `lifeos_person_connections` | `/api/crm/people/{person_id}/connections` | GET | Network connections |
| `lifeos_relationship_insights` | `/api/crm/relationship/insights` | GET | Relationship patterns |
| `lifeos_communication_gaps` | `/api/crm/family/communication-gaps` | GET | Neglected relationships |
| `lifeos_meeting_prep` | `/api/calendar/meeting-prep` | GET | Meeting preparation context |

**Assessment:**
- This is the strongest tool category. The multi-tool workflow (search -> profile -> timeline) is well-documented in descriptions.
- Tool descriptions include routing guidance (active channels -> which tool to use next)
- Missing: Person update (PATCH `/api/crm/people/{person_id}` -- notes, tags, category, birthday)
- Missing: Person merge, fact extraction triggers, fact confirmation
- Missing: Relationship detail between two specific people (`/api/crm/relationship/{a}/{b}`)
- Missing: Network graph data, CRM statistics, birthdays endpoints
- Missing: Tone analysis (relationship sentiment over time)

### 2.5 Photos (3 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_photos_person` | `/api/photos/person/{person_id}` | GET | Photos of a person |
| `lifeos_photos_shared` | `/api/photos/shared/{a}/{b}` | GET | Photos of two people together |
| `lifeos_photos_stats` | `/api/photos/stats` | GET | Photo library statistics |

**Assessment:**
- Good coverage for photo discovery
- Returns `source_link` for opening in Apple Photos
- Missing: Photo thumbnail serving (endpoint exists at `/api/photos/thumbnail/{uuid}`)
- Missing: Photo sync trigger

### 2.6 Tasks (5 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_task_create` | `/api/tasks` | POST | Create task |
| `lifeos_task_list` | `/api/tasks` | GET | List/filter tasks |
| `lifeos_task_update` | `/api/tasks/{task_id}` | PUT | Update task |
| `lifeos_task_complete` | `/api/tasks/{task_id}/complete` | PUT | Mark task done |
| `lifeos_task_delete` | `/api/tasks/{task_id}` | DELETE | Delete task |

**Assessment:**
- Full CRUD coverage -- this is one of the most complete tool sets
- Good filter support (status, context, tag, due_before, fuzzy query)
- Obsidian-compatible markdown storage is a strong differentiator

### 2.7 Reminders & Scheduling (3 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_reminder_create` | `/api/reminders` | POST | Create scheduled reminder |
| `lifeos_reminder_list` | `/api/reminders` | GET | List all reminders |
| `lifeos_reminder_delete` | `/api/reminders/{reminder_id}` | DELETE | Delete reminder |

**Assessment:**
- The three reminder types (static, prompt, endpoint) are powerful
- The "prompt" type is especially powerful -- runs full LifeOS chat pipeline at trigger time
- Missing: Reminder update (PUT endpoint exists but not exposed)
- Missing: Manual trigger (POST `/{reminder_id}/trigger` exists)

### 2.8 Other (6 tools)

| Tool | Endpoint | Method | Description |
|------|----------|--------|-------------|
| `lifeos_drive_search` | `/api/drive/search` | GET | Google Drive file search |
| `lifeos_conversations_list` | `/api/conversations` | GET | List past conversations |
| `lifeos_memories_create` | `/api/memories` | POST | Save a memory |
| `lifeos_memories_search` | `/api/memories/search/{query}` | GET | Search memories |

**Assessment:**
- Memories are a good persistent storage mechanism for cross-conversation state
- Drive search covers both personal and work accounts
- Missing: Memory update and delete (endpoints exist)
- Missing: Conversation detail retrieval, conversation continuation
- Missing: Briefing generation (`/api/briefing` and `/api/briefing/{person_name}`)

---

## 3. API Coverage Analysis

### Endpoints Exposed vs. Available

| Route File | Total Endpoints | Exposed via MCP | Coverage |
|-----------|----------------|-----------------|----------|
| `search.py` | 1 | 1 | 100% |
| `ask.py` | 1 | 1 | 100% |
| `tasks.py` | 6 | 5 | 83% |
| `reminders.py` | 7 | 3 | 43% |
| `gmail.py` | 3 | 2 | 67% |
| `calendar.py` | 4 | 3 | 75% |
| `photos.py` | 7 | 3 | 43% |
| `people.py` | ~11 | 1 | 9% |
| `crm.py` | ~70 | 7 | 10% |
| `admin.py` | ~20 | 0 | 0% |
| `chat.py` | 2 | 0 | 0% |
| `briefings.py` | 2 | 0 | 0% |
| `slack.py` | 5 | 1 | 20% |
| `imessage.py` | 4 | 1 | 25% |
| `drive.py` | 3 | 1 | 33% |
| `conversations.py` | 5 | 1 | 20% |
| `memories.py` | 5 | 2 | 40% |
| **TOTAL** | **~156** | **35** | **~22%** |

### Key Unexposed Endpoint Categories

**Admin/System (0% exposed):**
- `GET /api/admin/status` -- Index status
- `POST /api/admin/reindex` -- Trigger reindex
- `GET /api/admin/usage` -- API usage/cost tracking
- `GET /api/admin/calendar/status` -- Calendar sync status
- `POST /api/admin/calendar/sync` -- Trigger calendar sync
- Granola/Omi processor management (status, start, stop, process)

**CRM Write Operations (0% exposed):**
- `PATCH /api/crm/people/{id}` -- Update notes, tags, category, birthday
- `POST /api/crm/people/merge` -- Merge duplicate people
- `POST /api/crm/people/{id}/facts/extract` -- Trigger fact extraction
- `POST /api/crm/people/{id}/facts/{id}/confirm` -- Confirm extracted facts
- `POST /api/crm/people/{id}/hide` -- Hide a person from CRM
- `POST /api/crm/review-queue/{id}/confirm` -- Confirm entity links
- `POST /api/crm/review-queue/{id}/reject` -- Reject entity links

**CRM Analytics (0% exposed):**
- `GET /api/crm/statistics` -- CRM-wide statistics
- `GET /api/crm/network` -- Social network graph data
- `GET /api/crm/birthdays/today` -- Today's birthdays
- `GET /api/crm/birthdays/all` -- All known birthdays
- `GET /api/crm/me/stats` -- Owner's personal stats
- `GET /api/crm/me/timeline` -- Owner's timeline
- `GET /api/crm/family/members` -- Family member list
- `GET /api/crm/family/stats` -- Family statistics
- `GET /api/crm/family/channel-mix` -- Communication channel breakdown
- `POST /api/crm/relationship/tone-analysis` -- Sentiment analysis over time

**Advanced CRM (0% exposed):**
- `GET /api/crm/relationship/{a}/{b}` -- Relationship between two people
- `GET /api/crm/discover` -- Discover new connections
- `POST /api/crm/relationships/discover` -- Run relationship discovery
- `GET /api/crm/cleanup/queue` -- Data cleanup suggestions
- `GET /api/crm/data-health` -- Data quality issues
- `GET /api/crm/sync/health` -- Sync health status
- `POST /api/crm/contacts/sync` -- Sync Apple Contacts

**Chat/Conversation (0% exposed):**
- `POST /api/chat/ask/stream` -- Streaming chat (SSE)
- `GET /api/conversations/{id}` -- Get conversation detail
- `POST /api/conversations/{id}/ask` -- Continue a conversation
- `POST /api/chat/save-to-vault` -- Save chat to Obsidian vault

---

## 4. Tool Quality Analysis

### 4.1 Description Quality

**Excellent descriptions (model for others):**
- `lifeos_people_search` -- Includes RETURNS, FOLLOW-UP TOOLS, and ROUTING GUIDANCE sections
- `lifeos_person_timeline` -- Clear RETURNS, PARAMETERS, WORKFLOW sections
- `lifeos_reminder_create` -- Explains all three message types with examples

**Good descriptions:**
- `lifeos_ask` vs `lifeos_search` -- Clear differentiation
- `lifeos_gmail_draft` -- Safety-conscious (draft, not send)

**Weak descriptions:**
- `lifeos_health` -- "Check if all LifeOS services are healthy" is too vague. Doesn't explain what it returns or when to use it.
- `lifeos_conversations_list` -- Minimal, doesn't explain what conversations are or what you'd do with them.
- `lifeos_drive_search` -- Doesn't mention personal/work account filtering.

### 4.2 Schema Quality

**Issues found:**
- `lifeos_task_update`: The task_id is in the path (`/api/tasks/{task_id}`) but the PUT method doesn't have a request body schema in OpenAPI for the update fields. The tool schema may not include the updatable fields (description, status, context, priority, due_date, tags).
- `lifeos_reminder_create`: `endpoint_config` is typed as `string` in the tool schema but the API expects a `dict/object`. The fallback schema correctly types it as `object` but the OpenAPI-derived schema may not.
- Several tools have path parameters described only as "Path parameter: person_id" -- these should explain they come from `lifeos_people_search`.

### 4.3 Response Formatting

**Well-formatted:**
- `lifeos_person_profile` -- Rich markdown with sections for emails, phones, company, tags
- `lifeos_person_timeline` -- Uses source-type emojis, clean chronological display
- `lifeos_meeting_prep` -- Hierarchical display with related notes and attachments
- `lifeos_communication_gaps` -- Alert indicators for overdue contacts

**Could be improved:**
- `lifeos_gmail_search` -- Body truncation at 2000 chars may lose important content
- `lifeos_health` -- Returns only "LifeOS API status: healthy", losing all the per-service detail
- `lifeos_photos_person` -- Falls through to default JSON dump (no custom formatter)
- `lifeos_gmail_draft` -- Falls through to default JSON dump

### 4.4 Error Handling

**Good patterns:**
- HTTP errors caught and returned as `{"error": "API error 404: ..."}`
- Request errors (network failures) handled separately
- Generic exception catch as fallback

**Gaps:**
- No retry logic for transient failures
- 30-second timeout may be too short for `lifeos_ask` (which calls Claude for synthesis)
- DELETE method doesn't pass query params or body -- may fail for endpoints that need them
- Path parameter extraction uses `arguments.pop()` which mutates the dict -- safe but fragile

---

## 5. MCP Server vs. Telegram Agent Tool Comparison

The Telegram agentic pipeline (`agent_tools.py`) has a **different** set of tools. Key differences:

| Capability | MCP Server | Telegram Agent |
|-----------|-----------|---------------|
| Vault search | `lifeos_search` | `search_vault` |
| Read vault file | **Missing** | `read_vault_file` |
| Web search | **Missing** | `search_web` |
| Calendar search | `lifeos_calendar_search` | `search_calendar` |
| Email search | `lifeos_gmail_search` | `search_email` |
| Drive search | `lifeos_drive_search` | `search_drive` |
| Slack search | `lifeos_slack_search` | `search_slack` |
| Message history | `lifeos_imessage_search` | `get_message_history` |
| Person lookup | `lifeos_people_search` + `lifeos_person_profile` | `person_info` (consolidated) |
| Task management | 5 separate tools | `manage_tasks` (consolidated) |
| Reminders | 3 separate tools | `manage_reminders` (consolidated) |
| Email draft | `lifeos_gmail_draft` | `create_email_draft` |
| Telegram send | `lifeos_telegram_send` | N/A (is the transport) |
| Meeting prep | `lifeos_meeting_prep` | **Missing** |
| Person timeline | `lifeos_person_timeline` | **Missing** |
| Person facts | `lifeos_person_facts` | **Missing** |
| Person connections | `lifeos_person_connections` | **Missing** |
| Relationship insights | `lifeos_relationship_insights` | **Missing** |
| Communication gaps | `lifeos_communication_gaps` | **Missing** |
| Photos | 3 tools | **Missing** |
| Memories | 2 tools | **Missing** |

**Key gap**: The MCP server has NO `read_vault_file` or `search_web` tools, but the Telegram agent does. These are high-value capabilities that should be added to MCP.

---

## 6. Proposed New Tools

### 6.1 High Priority -- Missing Core Capabilities

#### `lifeos_read_vault_file`
**Why:** After `lifeos_search` finds a relevant file, agents need to read the full content. The Telegram agent already has this capability.
**Endpoint:** Would need a new endpoint or adapt the existing `search_vault` file read logic.
**Impact:** Closes a major gap where agents can find but not read files.

#### `lifeos_web_search`
**Why:** The Telegram agent has `search_web` using Claude's native web_search tool. MCP agents (Claude Code) already have web search built-in, but exposing LifeOS's version would ensure consistency across all clients.
**Impact:** Medium -- Claude Code has native web search, but other MCP clients may not.

#### `lifeos_person_update`
**Why:** Agents can read person profiles but cannot update notes, tags, categories, or birthdays. This is the most impactful write operation missing from the CRM.
**Endpoint:** `PATCH /api/crm/people/{person_id}`
**Impact:** Enables "remember that John's birthday is March 15" or "tag Sarah as VIP" workflows.

#### `lifeos_reminder_update`
**Why:** Can create and delete reminders but cannot modify them. Users often want to adjust timing or content.
**Endpoint:** `PUT /api/reminders/{reminder_id}`
**Impact:** Completes the reminder CRUD cycle.

#### `lifeos_birthdays`
**Why:** "Who has a birthday coming up?" is a natural query. Endpoint exists but isn't exposed.
**Endpoint:** `GET /api/crm/birthdays/all` (with optional today-only filter)
**Impact:** Enables proactive birthday reminders and relationship maintenance.

### 6.2 Medium Priority -- Valuable Extensions

#### `lifeos_relationship_detail`
**Why:** See how two specific people in your network relate to each other.
**Endpoint:** `GET /api/crm/relationship/{person_a_id}/{person_b_id}`
**Impact:** Enables "how do Kevin and Sarah know each other?" queries.

#### `lifeos_crm_statistics`
**Why:** Overview of CRM data -- total people, categories, sources, relationship counts.
**Endpoint:** `GET /api/crm/statistics`
**Impact:** Enables "how many people are in my CRM?" and data quality checks.

#### `lifeos_usage_stats`
**Why:** Monitor API costs and usage patterns.
**Endpoint:** `GET /api/admin/usage`
**Impact:** Cost awareness, usage trending.

#### `lifeos_sync_health`
**Why:** Check if data syncs are current and healthy.
**Endpoint:** `GET /api/crm/sync/health/summary`
**Impact:** Proactive monitoring, "are my syncs working?" queries.

#### `lifeos_person_fact_extract`
**Why:** Trigger fact extraction for a person from their interaction history.
**Endpoint:** `POST /api/crm/people/{person_id}/facts/extract`
**Impact:** On-demand CRM enrichment.

#### `lifeos_save_to_vault`
**Why:** Save content to the Obsidian vault as a new note.
**Endpoint:** `POST /api/chat/save-to-vault`
**Impact:** Enables "save this to my vault" workflows from any MCP client.

### 6.3 Lower Priority -- Nice to Have

#### `lifeos_family_members`
**Why:** Quick access to family member list without searching.
**Endpoint:** `GET /api/crm/family/members`

#### `lifeos_me_stats`
**Why:** Personal communication statistics dashboard.
**Endpoint:** `GET /api/crm/me/stats`

#### `lifeos_reindex`
**Why:** Trigger vault reindex after manual file changes.
**Endpoint:** `POST /api/admin/reindex`

#### `lifeos_discover_connections`
**Why:** Surface suggested new connections from email/calendar data.
**Endpoint:** `GET /api/crm/discover`

#### `lifeos_tone_analysis`
**Why:** Sentiment analysis of communication with a person over time.
**Endpoint:** `POST /api/crm/relationship/tone-analysis`

---

## 7. Structural Issues & Improvement Opportunities

### 7.1 Server Architecture

**Issue: Synchronous HTTP client**
The server uses `httpx.Client` (sync), which means all tool calls block. For Claude Code this is fine (one call at a time), but for future multi-client scenarios, `httpx.AsyncClient` would be better.

**Issue: No connection pooling**
Each tool call creates a fresh HTTP request. The `httpx.Client` is created once but doesn't use connection keep-alive headers explicitly.

**Issue: Startup dependency on running API**
If the API isn't running when the MCP server starts, it falls back to hardcoded schemas. This fallback works well but the schemas could drift from the actual API over time.

**Improvement: Health check at startup**
Before building tools, the server could hit `/health` to verify the API is reachable and log clearly if it's not.

### 7.2 Tool Schema Generation

**Issue: OpenAPI schema resolution is limited**
The `_build_input_schema` method handles `$ref` resolution for request bodies but only one level deep. Nested `$ref` or `allOf`/`anyOf` schemas would fail silently.

**Issue: PUT/PATCH methods not handled**
The `_call_api` method only handles GET, POST, and DELETE. PUT and PATCH requests (used by `lifeos_task_update`, `lifeos_reminder_update`) would fail. Looking more carefully, the POST branch handles them because the `else` clause catches everything that isn't GET or DELETE. However, PUT should send JSON body, not query params -- this works by accident but isn't explicit.

**Fix needed:** Add explicit PUT/PATCH handling:
```python
elif method in ("PUT", "PATCH"):
    resp = self.client.request(method, url, json=arguments)
```

### 7.3 Response Size & Truncation

**Issue: No response size limits**
Some endpoints can return very large responses (e.g., `lifeos_task_list` with no filters, `lifeos_person_timeline` with years of history). MCP has no built-in response size limit, but very large responses waste context window.

**Improvement:** Add intelligent truncation in `_format_response` with a summary footer like "Showing 30 of 150 results. Use filters to narrow."

### 7.4 Timeout Handling

**Issue: Single 30s timeout for all tools**
`lifeos_ask` calls Claude for synthesis and can take 15-30 seconds. `lifeos_search` is fast (< 1s). A per-tool timeout would be better.

**Improvement:** Add timeout overrides in `CURATED_ENDPOINTS`:
```python
"/api/ask": {
    "name": "lifeos_ask",
    "timeout": 60.0,
    ...
}
```

### 7.5 Missing Formatters

The following tools fall through to the default JSON dump in `_format_response`:
- `lifeos_photos_person`
- `lifeos_photos_shared`
- `lifeos_photos_stats`
- `lifeos_gmail_draft`

These should have custom formatters for better readability.

---

## 8. "Do Anything" Vision Gap Analysis

For LifeOS to support "anything you can do on a computer via Telegram", here's what's missing from the MCP tool surface:

### Currently Possible via MCP
- Search across all data sources (vault, email, calendar, messages, Slack, Drive)
- Look up anyone in CRM with full context
- Create/manage tasks and reminders
- Draft emails
- Send Telegram messages
- Get meeting prep context
- Track communication gaps
- View photos of people

### NOT Possible via MCP (but data/services exist)
- **Update CRM records** (notes, tags, categories, birthdays)
- **Read specific vault files** (only search chunks)
- **Create calendar events** (no endpoint exists at all)
- **Send emails** (only drafts, not send)
- **Send iMessages** (no macOS AppleScript bridge)
- **Run system commands** (no shell execution tool)
- **Manage files** in the vault (create, move, rename, delete notes)
- **Trigger syncs** (reindex, calendar sync, contacts sync)
- **View API usage/costs**
- **Monitor system health** proactively

### NOT Possible (requires new backends)
- **Home automation** (no HomeKit/smart home integration)
- **Financial tracking** (no bank/Mint integration)
- **Health data** (no Apple Health integration)
- **Music control** (no Spotify/Apple Music integration)
- **Browser automation** (no headless browser)
- **App launching** (no macOS automation bridge)
- **Location awareness** (no GPS/Find My integration)

---

## 9. Summary of Recommendations

### Immediate (fix existing issues)
1. Add explicit PUT/PATCH handling in `_call_api`
2. Add custom formatters for photos and gmail_draft tools
3. Improve `lifeos_health` formatter to show per-service details
4. Add response truncation with "showing X of Y" summaries

### Short-term (new tools from existing endpoints)
5. Add `lifeos_read_vault_file` (adapt from Telegram agent)
6. Add `lifeos_person_update` (PATCH people)
7. Add `lifeos_reminder_update` (PUT reminders)
8. Add `lifeos_birthdays` (GET birthdays/all)
9. Add `lifeos_relationship_detail` (GET relationship between two people)
10. Add `lifeos_crm_statistics` (GET statistics)
11. Add `lifeos_usage_stats` (GET admin/usage)

### Medium-term (new endpoints needed)
12. Create calendar event endpoint + MCP tool
13. Vault file management endpoints (create/update/delete notes)
14. System command execution endpoint (with safety constraints)
15. Apple Contacts search endpoint + MCP tool

### Long-term (new integrations)
16. Apple Shortcuts bridge for macOS automation
17. Apple Health data integration
18. HomeKit integration
19. Financial data aggregation
20. Location-aware triggers
