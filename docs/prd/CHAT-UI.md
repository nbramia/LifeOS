# Chat UI PRD

The primary chat interface for LifeOS, providing AI-powered search and synthesis across your personal knowledge base.

**Primary Use Cases:**
- Natural language queries: "What did we discuss about the product launch?"
- Stakeholder briefings: "Prep me for my meeting with Yoni"
- Task management: "Add a to-do to call the dentist"
- Email drafting: "Draft an email to Kevin about the budget"

**Related Documentation:**
- [API & MCP Reference](../architecture/API-MCP-REFERENCE.md) - API endpoints
- [Data & Sync](../architecture/DATA-AND-SYNC.md) - Data sources and sync
- [Frontend](../architecture/FRONTEND.md) - UI implementation details

---

## Table of Contents

1. [Core Chat Interface](#phase-2-web-interface)
2. [Query Routing](#query-routing)
3. [Conversation Management](#conversation-management)
4. [Memories System](#memories-system)
5. [File Attachments](#file-attachments)

---

## Phase 2: Web Interface

### P2.1: Basic Chat UI

**Status:** Complete

**Features:**
- Single HTML page served by FastAPI
- Chat interface with message bubbles (user/assistant)
- Streaming responses via SSE
- Clickable source links using `obsidian://` URI scheme
- Mobile-responsive layout
- Status indicator (ready/loading/error)

### P2.2: Save to Vault

**Status:** Complete

**Features:**
- "Save to vault" button on assistant responses
- Claude synthesizes save-worthy content (not raw chat)
- Determines folder based on topic
- Writes formatted markdown with YAML frontmatter
- Confirms save with link to new note

### P2.3: Stakeholder Briefings

**Status:** Complete

**Features:**
- "Tell me about [person]" or "Prep me for [person]" queries
- Aggregates context from vault, calendar, email, messages
- Synthesizes into actionable briefing:
  - Role/relationship
  - Last interaction
  - Recent context
  - Open items
  - Suggested topics

---

## Query Routing

The local LLM (Llama 3.2 3B via Ollama) routes queries to appropriate data sources.

| Source | Content | Example Queries |
|--------|---------|-----------------|
| `vault` | Obsidian notes, meeting notes | "What did we discuss about the product launch?" |
| `calendar` | Google Calendar events | "What's on my calendar tomorrow?" |
| `gmail` | Email messages | "What did John email about?" |
| `drive` | Google Drive files | "Find the Q4 budget spreadsheet" |
| `imessage` | iMessage/SMS history | "What did I text Sarah about dinner?" |
| `slack` | Slack DMs and channels | "What did John say in Slack about the project?" |
| `people` | Stakeholder profiles | "Tell me about Alex before my meeting" |
| `photos` | Apple Photos (face recognition) | "When was I last in a photo with Jonathan?" |
| `tasks` | Task index (Obsidian Tasks) | "What are my open tasks?" |
| `memories` | User-saved memories | "What did I want to remember about the project?" |

**Router Prompt:** Configurable at `config/prompts/query_router.txt`

**Person Name Extraction:** The LLM router extracts person names from queries as part of routing (via `people_mentioned` in the JSON response). Falls back to regex patterns when Ollama is unavailable.

**Fallback:** If Ollama is unavailable, keyword-based routing kicks in automatically.

---

## Conversation Management

### P4.1: Conversation Persistence

**Status:** Complete

**Features:**
- Conversations stored in SQLite with full message history
- List all conversations with timestamps
- Resume previous conversations
- Delete conversations
- Search across conversation history

### P4.2: Keyboard Shortcuts

**Status:** Complete

| Shortcut | Action |
|----------|--------|
| `Enter` | Send message |
| `Shift+Enter` | New line |
| `Ctrl/Cmd+K` | New conversation |
| `Ctrl/Cmd+/` | Toggle sidebar |
| `Esc` | Cancel/close modal |

### P4.3: Cost Tracking

**Status:** Complete

**Features:**
- Session cost displayed in header
- Per-conversation cost tracking
- Historical cost viewing
- Stored in `data/cost_tracker.db`

---

## Memories System

### P5.1: Memory Storage

**Status:** Complete

**Features:**
- Create memories with optional Claude synthesis
- Categories: preference, context, person, other
- Search memories by keyword
- Delete memories
- Memories influence future responses

**API Endpoints:**
- `POST /api/memories` - Create memory
- `GET /api/memories` - List (filter by category)
- `DELETE /api/memories/{id}` - Delete

### P5.2: Remember Button

**Status:** Complete

- Quick "Remember this" button in chat
- Opens modal with memory content pre-filled
- Category selection
- Claude synthesizes into clear memory format

---

## File Attachments

### P6.1: Image Attachments

**Status:** Complete

**Features:**
- Drag-and-drop or click to upload images
- Preview in chat before sending
- Sent to Claude for multimodal analysis
- Supports PNG, JPG, GIF, WebP

### P6.2: Document Attachments

**Status:** Complete

**Features:**
- Upload PDF, text files
- Content extracted and included in context
- File preview with type indicator
- Max file size: 10MB

---

## Email Composition

### P7.1: Email Drafting

**Status:** Complete

**Features:**
- Natural language email requests: "Draft an email to Kevin about the budget"
- Creates Gmail draft with proper formatting
- Returns link to open draft in Gmail
- Supports both personal and work accounts

---

## Implementation Details

See [Frontend Architecture](../architecture/FRONTEND.md) for:
- UI component structure
- State management patterns
- SSE streaming implementation
- Obsidian link handling
