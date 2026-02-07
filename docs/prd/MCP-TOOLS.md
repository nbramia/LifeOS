# MCP Tools PRD

MCP (Model Context Protocol) server that exposes LifeOS capabilities to AI assistants like Claude Code.

**Primary Use Cases:**
- Enable Claude to search your knowledge base
- Allow AI assistants to query calendar, email, messages
- Create memories and drafts programmatically
- Provide personal context during coding sessions

**Related Documentation:**
- [API & MCP Reference](../architecture/API-MCP-REFERENCE.md) - Full API specs
- [Data & Sync](../architecture/DATA-AND-SYNC.md) - Data sources

---

## Table of Contents

1. [Overview](#overview)
2. [Available Tools](#available-tools)
3. [Setup](#setup)
4. [Tool Specifications](#tool-specifications)

---

## Overview

The LifeOS MCP server dynamically discovers endpoints from the LifeOS OpenAPI spec and exposes them as Claude Code tools. It runs as a subprocess and communicates via JSON-RPC over stdin/stdout.

**Key Features:**
- Auto-discovery from OpenAPI spec
- Curated tool descriptions for optimal AI use
- Formatted responses for human readability
- Fallback schemas when API unavailable

**Architecture:**
```
Claude Code  ←→  MCP Protocol  ←→  mcp_server.py  ←→  LifeOS API
              (JSON-RPC/stdio)                         (HTTP)
```

---

## Available Tools

### Core Tools
| Tool | Description |
|------|-------------|
| `lifeos_ask` | Query knowledge base with synthesized answer |
| `lifeos_search` | Search vault without synthesis (raw results) |

### Calendar & Meeting Tools
| Tool | Description |
|------|-------------|
| `lifeos_calendar_upcoming` | Get upcoming calendar events |
| `lifeos_calendar_search` | Search calendar events |
| `lifeos_meeting_prep` | Get meeting prep context with related notes |

### Communication Tools
| Tool | Description |
|------|-------------|
| `lifeos_gmail_search` | Search emails (includes body for top 5) |
| `lifeos_gmail_draft` | Create Gmail draft |
| `lifeos_drive_search` | Search Google Drive files |
| `lifeos_imessage_search` | Search iMessage/SMS history |
| `lifeos_slack_search` | Semantic search Slack messages |

### People & CRM Tools
| Tool | Description |
|------|-------------|
| `lifeos_people_search` | Search people in network |
| `lifeos_person_profile` | Get full CRM profile for a person |
| `lifeos_person_facts` | Get extracted facts about a person |
| `lifeos_person_timeline` | Get chronological interaction history |
| `lifeos_person_connections` | Get who someone works with/knows |
| `lifeos_relationship_insights` | Get relationship patterns and observations |
| `lifeos_communication_gaps` | Find neglected relationships |

### Reminders & Telegram Tools
| Tool | Description |
|------|-------------|
| `lifeos_reminder_create` | Create a scheduled reminder (cron or one-time) |
| `lifeos_reminder_list` | List all reminders |
| `lifeos_reminder_delete` | Delete a reminder |
| `lifeos_telegram_send` | Send an ad-hoc Telegram message |

### Memory & Admin Tools
| Tool | Description |
|------|-------------|
| `lifeos_memories_create` | Save a memory |
| `lifeos_memories_search` | Search saved memories |
| `lifeos_conversations_list` | List chat conversations |
| `lifeos_health` | Check service health |

---

## Setup

### Register with Claude Code

```bash
# Add MCP server
claude mcp add lifeos -s user -- python /path/to/LifeOS/mcp_server.py

# Verify
claude mcp list
```

### Environment Variables

```bash
LIFEOS_API_URL=http://localhost:8000  # Default
```

### Requirements

- LifeOS server running (`./scripts/server.sh start`)
- Python 3.11+ with httpx installed

---

## Tool Specifications

### lifeos_ask

Query your knowledge base and get a synthesized answer with citations.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| question | string | Yes | The question to ask |
| include_sources | boolean | No | Include source citations (default: true) |

**Example:**
```json
{
  "question": "What did we discuss in the product meeting yesterday?",
  "include_sources": true
}
```

### lifeos_search

Search the vault without synthesis. Returns raw search results.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | Yes | Search query |
| top_k | integer | No | Number of results (1-100, default: 10) |

### lifeos_calendar_upcoming

Get upcoming calendar events.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| days | integer | No | Days to look ahead (default: 7) |

### lifeos_calendar_search

Search calendar events by keyword.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| q | string | Yes | Search query |

### lifeos_gmail_search

Search emails in Gmail. Automatically fetches full body for top 5 results.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| q | string | Yes | Search query |
| account | string | No | Account: personal or work |

### lifeos_gmail_draft

Create a draft email in Gmail.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| to | string | Yes | Recipient email |
| subject | string | Yes | Email subject |
| body | string | Yes | Email body |
| cc | string | No | CC recipients |
| bcc | string | No | BCC recipients |
| html | boolean | No | Send as HTML |
| account | string | No | Account: personal or work |

**Returns:** Draft ID and Gmail URL to open draft.

### lifeos_drive_search

Search files in Google Drive.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| q | string | Yes | Search query (name or content) |
| account | string | No | Account: personal or work |

### lifeos_imessage_search

Search iMessage/SMS text message history.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| q | string | No | Search query for message text |
| phone | string | No | Filter by phone (E.164 format) |
| entity_id | string | No | Filter by PersonEntity ID |
| after | string | No | Messages after date (YYYY-MM-DD) |
| before | string | No | Messages before date (YYYY-MM-DD) |
| direction | string | No | Filter: sent or received |
| max_results | integer | No | Max results (1-200, default: 50) |

### lifeos_slack_search

Semantic search across Slack messages.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | Yes | Search query |
| top_k | integer | No | Number of results (1-50, default: 20) |
| channel_id | string | No | Filter by channel ID |
| user_id | string | No | Filter by user ID |

### lifeos_people_search

Search for people in your network.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| q | string | Yes | Name or email to search |

### lifeos_memories_create

Save a memory for future reference.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| content | string | Yes | Memory content |
| category | string | No | Category (default: facts) |

### lifeos_memories_search

Search saved memories.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | Yes | Search query |

### lifeos_conversations_list

List recent chat conversations.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| limit | integer | No | Max results (default: 10) |

### lifeos_health

Check if all LifeOS services are healthy.

**Parameters:** None

### lifeos_person_profile

Get comprehensive CRM profile for a person including all contact info, relationship metrics, and user annotations.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| person_id | string | Yes | Entity ID from lifeos_people_search |

**Returns:** Full profile with emails, phones, company, relationship_strength, tags, notes, and interaction counts.

### lifeos_person_facts

Get extracted facts about a person (auto-extracted from interactions).

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| person_id | string | Yes | Entity ID from lifeos_people_search |

**Returns:** Facts organized by category (work, personal, preferences, etc.) with confidence scores.

### lifeos_person_timeline

Get chronological interaction history for a person. Use for "catch me up on [person]" queries.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| person_id | string | Yes | Entity ID from lifeos_people_search |
| days_back | integer | No | Days of history (default: 365) |
| source_type | string | No | Filter by source (e.g., "imessage", "gmail,slack") |
| limit | integer | No | Max results (default: 50) |

**Returns:** Chronological list of interactions with source type, timestamp, and summary.

### lifeos_meeting_prep

Get intelligent meeting preparation context for a date.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| date | string | No | Date in YYYY-MM-DD format (default: today) |
| include_all_day | boolean | No | Include all-day events (default: false) |
| max_related_notes | integer | No | Max notes per meeting (1-10, default: 4) |

**Returns:** For each meeting: title, time, attendees, related_notes (people notes, past meetings, topic notes), and attachments.

### lifeos_communication_gaps

Identify people you haven't contacted recently. Requires person_ids from lifeos_people_search.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| person_ids | string | Yes | Comma-separated person IDs |
| days_back | integer | No | Days of history to analyze (default: 365) |
| min_gap_days | integer | No | Minimum gap to report (default: 14) |

**Returns:** Communication gaps with duration, plus per-person summaries showing days_since_contact and average_gap_days.

### lifeos_person_connections

Get people connected to a person through shared meetings, emails, messages, and LinkedIn.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| person_id | string | Yes | Entity ID from lifeos_people_search |
| relationship_type | string | No | Filter by type (e.g., "coworker") |
| limit | integer | No | Max results (default: 50) |

**Returns:** List of connected people with shared_events_count, shared_threads_count, shared_messages_count, relationship_strength, and last_seen_together.

### lifeos_relationship_insights

Get relationship insights and patterns extracted from therapy notes and conversations.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| person_id | string | No | Focus on specific person (defaults to primary relationship) |

**Returns:** Insights grouped by category (focus_areas, recurring_themes, relationship_strengths, growth_patterns, ai_suggestions) with text, source_title, source_link, and confirmed status.

---

## Implementation

See `mcp_server.py` for implementation details:
- Dynamic endpoint discovery from OpenAPI spec
- Curated tool descriptions in `CURATED_ENDPOINTS`
- Response formatting for readability
- Fallback schemas when API unavailable
