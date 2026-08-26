# Chat UI PRD

> **Status:** Complete
> **Owner:** Chat
> **Last Updated:** 2026-08-26

The primary chat interface for LifeOS, providing AI-powered search and synthesis across your personal knowledge base.

**Primary Use Cases:**
- Natural language queries: "What did we discuss about the product launch?"
- Stakeholder briefings: "Prep me for my meeting with Yoni"
- Task management: "Add a to-do to call the dentist"
- Email drafting: "Draft an email to Kevin about the budget"

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

## Backend Selector

**Status:** Complete

The composer carries a three-way backend selector — **LifeOS | Agent | Hermes** — rather than a two-way toggle. LifeOS is always available; Agent and Hermes each only appear once configured server-side (`GET /api/agent/status` / `GET /api/hermes/status`). With no stored preference, a fresh session defaults to Hermes if it's configured and reachable, else LifeOS; an explicit user choice — including explicitly picking LifeOS — always wins over that default.

The three backends are not interchangeable: LifeOS is the native orchestrator (full personas, handoff, per-turn model picker); Agent has no personas, no model picker, and no persisted history; Hermes keeps the persona picker but hides the model picker, and its history is LifeOS-owned. Selecting a backend hides the pickers it doesn't support and continues that backend's own conversation thread on refresh. See [Client Surfaces](../technical/client-surfaces.md#text-backends) for the full per-backend capability contract — this doc covers only the selector's product behavior.

---

## Query Routing

The orchestrator LLM (Claude Haiku via Anthropic API by default; configurable via `LIFEOS_LLM_BACKEND` / `LIFEOS_ANTHROPIC_MODEL`) chooses which tools to call. A lightweight intent classifier runs first to short-circuit a few special cases — Claude Code tasks, ambiguous task/reminder phrasing, and explicit engine handoffs (below) — and everything else flows through the agentic loop where the model picks tools per query.

**Per-query escalation & engine handoff.** On the Anthropic backend, a turn can run on a stronger model or hand off to a CLI engine instead of answering inline (off unless `LIFEOS_AGENT_ESCALATION_MODEL` is set):

- **User-directed** (imperative, leading or trailing): "escalate to opus" / "use sonnet" runs that turn on the named model via the API; "use codex" / "use claude code" (also "add the games using codex") hands the task to that CLI worker session. Negations and questions ("why did you use codex?") don't trigger it.
- **Automatic ladder:** when a turn refuses ("hasn't been released") and the user pushes back ("do research", "you're wrong"), the next turn climbs the escalation ladder — a Claude Code handoff on the 1st pushback, Codex on the 2nd (tunable via `LIFEOS_AGENT_ESCALATION_LADDER`). The handoff forwards the *original* request, not the pushback. Automatic rungs are limited to engines that cost nothing per token; escalating to a stronger *API* model is user-directed only, so LifeOS never spends credits on a guess about what the user wanted.

CLI handoffs run as async worker sessions and report results via Telegram and `/agents`; on Telegram and the web chat the trigger is the same.

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
| `web` | External info via web search | "What's the weather?" / "Trash pickup schedule?" |

**Router Prompt:** Configurable at `config/prompts/query_router.txt`

**Person Name Extraction:** The LLM router extracts person names from queries as part of routing (via `people_mentioned` in the JSON response). Falls back to regex patterns when the local LLM is unavailable.

**Fallback:** If the local LLM is unavailable, keyword-based routing kicks in automatically.

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
- A conversation starts with a placeholder title and is retitled once, automatically, after its second user message — a short title generated from the exchange itself, replacing the placeholder or first-message truncation. No manual rename exists; the system-generated title is what shows from then on.
- A conversation on a non-default persona shows that persona's label alongside its date in the sidebar, so a fitness/journal/therapist/etc. thread is distinguishable from a primary one at a glance.

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
- Stored in `data/usage.db` (`api/services/usage_store.py`)

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

### P7.1: Email Drafting & Sending

**Status:** Complete

**Features:**
- Natural language email requests: "Draft an email to Kevin about the budget"
- Creates Gmail draft with proper formatting
- Returns link to open draft in Gmail
- Supports both personal and work accounts
- **Gated sending:** even a request phrased as "send an email to X" always drafts first, presents the draft, and waits for explicit confirmation. The in-process agent loop refuses drafts created in the current turn, and the Gmail send endpoint enforces the same guarantee for HTTP/MCP callers: a send with the same `X-LifeOS-Turn-ID` as draft creation is refused regardless of elapsed time, as long as that turn-id record is still in the ledger (turn-tagged records are capped by count rather than by age, so they don't expire on a timer, but the ledger isn't unbounded); without an exact different turn id, LifeOS-created drafts are refused during the configured cooling-off window. Hand-written Gmail drafts are not in the LifeOS ledger and can be sent.

---

## Agent Threads

**Status:** Complete

Web `/chat` speaks to the same agent-thread model as Telegram (#236, Phase 3 of #233):

- **Threads panel** (sidebar) — lists recent/resumable agent sessions (root sessions only) with a status badge (running / completed / failed / budget / blocked) and a route badge showing where the agent runs: **🖥 local** (Gemma) or **☁ cloud** (Claude / Managed Agents), with the model name on hover. Backed by `GET /api/agents/threads`; polled for live-ish updates (the `/agents` page uses the `/api/agents/stream` SSE for the full graph).
- **Open a thread** — clicking a thread loads its full conversation into the main chat body: user prompts and the agent's replies render as chat bubbles, with the agent's tool calls shown as collapsible detail under each reply. Backed by `GET /api/agents/threads/{id}`, which returns a reconstructed `conversation` (from the session's message history, or the managed transcript for cloud sessions). A banner — carrying the same local/cloud route badge — marks the thread; **Exit to chat** (or **+**) returns to normal chat.
- **Reply / continue** — while a thread is open the composer continues *that thread*: sending a message posts to `POST /api/agents/threads/{id}/reply`, which reopens the session as a follow-up turn via the shared follow-up table (the same path a Telegram reply takes). The view polls and re-renders as the agent's new turns land. Only completed/failed threads are continuable; a running thread opens read-only.
- **Run as agent** — the composer has a model selector (auto / local / cloud) and a spawn button; it sends the composer text to `POST /api/agents/spawn` (Phase 2's `create_operator_session`), and the new thread appears in the panel. `auto` routes via preflight; an ambiguous auto-route returns 409 asking for an explicit model.

## Implementation Details

See [Frontend Architecture](../technical/frontend.md) for:
- UI component structure
- State management patterns
- SSE streaming implementation
- Obsidian link handling

## Related Documents

- [API Reference](api-reference.md) -- API endpoint contracts
- [Client Surfaces](../technical/client-surfaces.md) -- HTTP consumers and breaking-change policy
- [Frontend](../technical/frontend.md) -- UI implementation details
- [MCP Tools](mcp-tools.md) -- MCP tool specifications
