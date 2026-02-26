# Audit: Telegram Bot & Chat/Agentic Pipeline

**Date:** 2026-02-13
**Auditor:** Claude (telegram-auditor)
**Scope:** Full message flow from Telegram input through processing to response delivery, including the agentic pipeline, Claude Code orchestration, and scheduling systems.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Message Flow: End-to-End](#2-message-flow-end-to-end)
3. [Telegram Bot Implementation](#3-telegram-bot-implementation)
4. [Intent Classification System](#4-intent-classification-system)
5. [Agentic Pipeline (Agent Loop)](#5-agentic-pipeline-agent-loop)
6. [Claude Code Orchestrator](#6-claude-code-orchestrator)
7. [Reminder & Scheduling System](#7-reminder--scheduling-system)
8. [Conversation Context Management](#8-conversation-context-management)
9. [Model Selection & Cost Management](#9-model-selection--cost-management)
10. [Current Capabilities Assessment](#10-current-capabilities-assessment)
11. [Limitations & Gaps](#11-limitations--gaps)
12. [Scenario Analysis](#12-scenario-analysis)
13. [Proposals for the "Do Anything" Vision](#13-proposals-for-the-do-anything-vision)

---

## 1. Architecture Overview

### System Topology

```
Telegram App
    |
    v (long-polling)
TelegramBotListener (background thread, api/services/telegram.py)
    |
    |--- /new, /clear, /status, /help  -->  Direct responses
    |--- /code <task>                  -->  Claude Code Orchestrator
    |--- Agent approval/clarification  -->  Claude Code session resume
    |--- Follow-up to recent session   -->  Claude Code session resume
    |
    v (all other messages)
chat_via_api() --- HTTP POST ---> /api/ask/stream (SSE endpoint)
    |                                   |
    |                                   v
    |                          Intent Classification (Ollama -> Haiku -> patterns)
    |                                   |
    |     +-----------------------------+-----------------------------+
    |     |             |               |              |              |
    |     v             v               v              v              v
    |  compose      task_*         reminder_*       code          (default)
    |  (drafts)     (CRUD)         (CRUD)           intent        Agentic Loop
    |                                                  |              |
    |                                                  v              v
    |                                            code_intent    run_agent_loop()
    |                                            SSE event        |
    |                                                  |          v
    |                                                  |     Claude API (streaming)
    |                                                  |     + Tool execution
    v                                                  |          |
TypingIndicator                                        v          v
+ send_message_async()                          TelegramBot    Agent Result
                                                handles it     (text + tool logs)
```

### Key Files

| File | Role | Lines |
|------|------|-------|
| `api/services/telegram.py` | Bot listener, message sending, chat_via_api bridge | ~642 |
| `api/routes/chat.py` | SSE streaming endpoint, intent handling, action dispatching | ~1900 |
| `api/services/agent_loop.py` | Multi-turn Claude tool-use loop | ~262 |
| `api/services/agent_tools.py` | Tool definitions + execution handlers | ~825 |
| `api/services/agent_system_prompt.py` | System prompt builder with caching | ~118 |
| `api/services/chat_helpers.py` | Intent classification, parameter extraction | ~800 |
| `api/services/claude_orchestrator.py` | Claude Code subprocess management | ~617 |
| `api/services/query_router.py` | Query routing (legacy, partially used) | ~683 |
| `api/services/conversation_store.py` | SQLite conversation persistence | ~463 |
| `api/services/conversation_context.py` | Follow-up context tracking | ~250 |
| `api/services/reminder_store.py` | Reminder CRUD + scheduler | ~487 |
| `api/services/model_selector.py` | Complexity-based model selection | ~253 |

---

## 2. Message Flow: End-to-End

### Step-by-Step for a Telegram Message

1. **Telegram long-polling** (`TelegramBotListener._poll_loop`): Background thread with 30s timeout polls `getUpdates`. Only responds to the configured `TELEGRAM_CHAT_ID`.

2. **Command check** (`_handle_command`): If message starts with `/`, checks known commands (`/new`, `/clear`, `/status`, `/code`, `/help`). Unknown `/commands` fall through to chat.

3. **Agent approval check** (`_check_agent_approval`): If a Claude Code session is `awaiting_approval`, short keywords like "yes", "approve", "no", "reject" are intercepted.

4. **Clarification check** (`_check_agent_clarification`): If a session is `awaiting_clarification`, any non-command text is forwarded as the answer.

5. **Code follow-up check** (`_check_code_followup`): If a session completed within the last 5 minutes and Claude Code is idle, the message is treated as a follow-up (session resume).

6. **Chat pipeline** (`chat_via_api`): POSTs to `http://localhost:{port}/api/ask/stream` and collects SSE events. Typing indicator runs throughout.

7. **SSE endpoint** (`/api/ask/stream`):
   - Creates/retrieves conversation from SQLite store
   - Checks for pending numeric selections (disambiguation responses)
   - Runs intent classification via LLM (Ollama -> Haiku -> pattern fallback)
   - Dispatches to specialized handlers (compose, task, reminder, code) OR the agentic loop
   - Streams responses as SSE events

8. **Agentic loop** (`run_agent_loop`): Multi-round Claude API calls with tool use. Up to 5 tool rounds. Streams text and status events.

9. **Response delivery**: `send_message_async()` sends the final text back to Telegram with Markdown formatting, splitting at 4096-char boundaries.

### Event Types in SSE Stream

| Event | Purpose | Consumer |
|-------|---------|----------|
| `conversation_id` | New/existing conversation ID | Web UI, Telegram |
| `routing` | Which sources/path was chosen | Web UI (debug) |
| `content` | Streamed text chunks | Both |
| `status` | Tool execution status messages | Both |
| `sources` | Source documents used | Web UI |
| `usage` | Token counts and cost | Web UI |
| `code_intent` | Signals Claude Code should handle this | Telegram only |
| `error` | Error messages | Both |
| `done` | Stream complete | Both |

---

## 3. Telegram Bot Implementation

### Strengths

- **Clean separation**: Bot listener in its own thread, communicates with the chat pipeline via internal HTTP. This means web UI and Telegram share the exact same pipeline.
- **Typing indicators**: `TypingIndicator` context manager sends "typing..." every 4 seconds during processing. Also present in Claude Code orchestrator.
- **Message splitting**: Smart splitting at 4096-char limit with newline-aware breakpoints.
- **Markdown fallback**: Tries Markdown parse mode first, falls back to plain text if it fails.
- **Authorization**: Only responds to the configured `TELEGRAM_CHAT_ID`, logs unauthorized attempts.

### Weaknesses

- **Text-only input**: No support for voice messages, photos, documents, or location sharing from Telegram. The bot only processes `message.text`.
- **No inline keyboards**: All interaction is text-based. No buttons for approve/reject, task selection, or quick actions.
- **Single conversation**: Uses a dict `{chat_id: conversation_id}` for context tracking. `/new` or `/clear` resets it, but there's no way to manage multiple threads.
- **No message editing**: Can't edit previously sent messages (e.g., to update a progress status in-place).
- **No queue/rate limiting**: Messages are processed sequentially within the polling loop. A long-running agentic query blocks subsequent messages.
- **Error messages are generic**: `f"Error processing your message: {str(e)[:200]}"` doesn't guide the user on what to do next.
- **Long-polling only**: Not using webhooks. Fine for a single-user system, but less efficient than webhook mode.

---

## 4. Intent Classification System

### Architecture

```
User Message
    |
    v
classify_action_intent() [chat_helpers.py]
    |
    |--- Try 1: Ollama (local LLM, free, fast)
    |--- Try 2: Claude Haiku (remote, cheap, reliable)
    |--- Try 3: Pattern matching (offline fallback)
    |
    v
ActionIntent { category, sub_type }
```

### Supported Intents

| Category | Sub-types | Handler |
|----------|-----------|---------|
| `compose` | - | Email draft creation |
| `task` | create, list, complete, edit, delete | Task CRUD in chat.py |
| `reminder` | create, list, edit, delete | Reminder CRUD in chat.py |
| `task_and_reminder` | - | Creates both simultaneously |
| `ambiguous_task_reminder` | - | Asks user to clarify |
| `code` | - | Emits `code_intent` SSE event, Telegram spawns Claude Code |
| `none` | - | Falls through to agentic loop |

### Strengths

- **Three-tier fallback**: Ollama -> Haiku -> patterns. System never fails completely.
- **Conversation context**: Recent messages are included in the classification prompt for follow-up understanding ("Both", "the second one").
- **Code intent detection**: Complex tasks that need terminal/filesystem/browser access are correctly routed to Claude Code rather than the chat pipeline.

### Weaknesses

- **Two classification systems**: Intent classification in `chat_helpers.py` AND the agentic loop's own tool selection via Claude are somewhat redundant. The intent classifier handles actions, while the agentic loop handles information retrieval. This split means:
  - A request like "find my latest email from John and create a task to reply" crosses both systems.
  - The `action_after` compound query support in `query_router.py` is partially implemented but may not be exercised through the agentic path.
- **LLM classification adds latency**: Every message goes through intent classification before reaching the agentic loop. This adds 200-2000ms depending on whether Ollama or Haiku is used.
- **Pattern matching fallback is limited**: Only covers the most obvious patterns. Edge cases like "text John about the meeting" could miss task/reminder intent.
- **No confidence thresholds for agentic handoff**: If intent classification returns `none` with low confidence, it always falls through to the agentic loop. There's no way to express "I'm not sure, let the agent decide."

---

## 5. Agentic Pipeline (Agent Loop)

### Architecture

```
run_agent_loop()
    |
    v
Build system prompt (static + datetime, cached)
    |
    v
Build messages (last 10 from conversation history + current)
    |
    v
FOR round 1..5:
    |
    |--- Claude API call (streaming, with tools)
    |       |
    |       |--- Text chunks -> yield {"type": "text"}
    |       |--- Tool use blocks -> collect
    |       |
    |--- If no tool calls: DONE
    |--- Execute tools in parallel (asyncio.gather)
    |       |--- yield {"type": "status"} per tool
    |       |--- Each tool returns string result
    |--- Append assistant message + tool results to messages
    |--- Continue loop
    |
IF exhausted 5 rounds:
    |--- Run one more round WITHOUT tools (synthesis-only)
    |
    v
yield {"type": "result", "result": AgentResult}
```

### Available Tools (13 total)

| Tool | Type | Description |
|------|------|-------------|
| `search_vault` | Retrieval | Hybrid search (vector + BM25) across Obsidian vault |
| `read_vault_file` | Retrieval | Read full file content by fuzzy name match |
| `search_calendar` | Retrieval | Google Calendar (personal + work) |
| `search_email` | Retrieval | Gmail (personal + work) |
| `search_drive` | Retrieval | Google Drive files |
| `search_slack` | Retrieval | Slack messages (semantic search) |
| `search_web` | Retrieval | Web search via Claude's native tool |
| `get_message_history` | Retrieval | iMessage/WhatsApp chat logs by entity_id |
| `person_info` | Lookup/Briefing | Person lookup or comprehensive briefing |
| `manage_tasks` | Action | Create, list, complete tasks |
| `manage_reminders` | Action | Create, list reminders |
| `read_vault_file` | Retrieval | Full file read with fuzzy matching |
| `create_email_draft` | Action | Create Gmail draft |

### Strengths

- **Streaming with parallel tool execution**: Text is streamed to the client in real-time. Multiple tools execute in parallel via `asyncio.gather`.
- **Thread-safe sync handlers**: Sync handlers (vault search, person lookup) run in `asyncio.to_thread()` to avoid blocking the event loop.
- **Cost tracking**: Per-round token counting with prompt caching awareness. Cache reads cost 0.1x, creation 1.25x.
- **Prompt caching**: System prompt uses `cache_control: ephemeral` for Anthropic's prompt caching. Tool definitions also have a cache breakpoint.
- **Synthesis fallback**: If 5 tool rounds are exhausted, a final synthesis-only round produces a coherent answer.
- **Well-designed system prompt**: Clear instructions about when NOT to use tools, multi-tool patterns, and response format. The "NEVER output text between tool rounds" rule prevents chatty intermediate responses.

### Weaknesses

- **5-round limit is global**: All queries get the same `max_tool_rounds=5`. Complex multi-step research could benefit from more rounds, while simple lookups waste time if the model decides to over-search.
- **No memory across conversations**: Each conversation starts fresh. The agent can't say "last time you asked about X, I found..." Previous conversation messages are included (last 10), but cross-conversation memory is absent from the agentic loop.
- **No planning step**: The agent doesn't explicitly plan before acting. It relies on Claude's implicit planning in the system prompt. A dedicated planning round could improve efficiency for complex queries.
- **Tool results are raw strings**: No structured output from tools. The agent receives plain text which it must parse. This makes it harder for the model to reliably extract specific fields.
- **No tool result caching**: If two queries in the same conversation ask about the same person, `person_info` is called again. Tool results could be cached within a conversation.
- **Limited error recovery**: If a tool fails, the error string is passed to Claude, which may or may not handle it gracefully. There's no automatic retry or alternative source suggestion.
- **No image/document generation**: Can search and retrieve, but can't generate charts, PDFs, or other artifacts.
- **Conversation history truncated to 10 messages**: Long conversations lose early context. No summarization of older messages.

---

## 6. Claude Code Orchestrator

### Architecture

```
/code <task>  or  code_intent from chat pipeline
    |
    v
ClaudeOrchestrator.run_task()
    |
    v
_spawn() -> subprocess.Popen(claude CLI)
    |
    |--- Stream reader thread (parses stream-json)
    |       |--- [NOTIFY] lines -> Telegram notification
    |       |--- [CLARIFY] lines -> pause session, ask user
    |       |--- tool_use blocks -> update last_activity (heartbeat context)
    |       |--- result event -> cost tracking, session completion
    |
    |--- Watchdog timer (configurable timeout)
    |--- Heartbeat timer (5 min intervals, sends "Still working...")
    |--- Typing indicator thread (every 4 sec)
    |
    v
Session states: running -> awaiting_approval/awaiting_clarification -> implementing -> completed/failed
```

### Capabilities

- **Plan-then-implement**: Tasks matching heuristic keywords ("refactor", "migrate", etc.) use plan mode. Claude produces a plan, user approves via "approve"/"reject", then implementation proceeds.
- **Session resume**: Sessions can be resumed for clarification responses, plan approval, and follow-up within 5 minutes of completion.
- **[CLARIFY] protocol**: Claude Code can ask the user questions mid-task. The session pauses and the user's answer is forwarded.
- **Heartbeat**: Every 5 minutes, if no [NOTIFY] was sent, a progress update is pushed ("Still working -- reading config.py (3m elapsed | $0.12)").
- **Cost cap**: Sessions are terminated if cost exceeds `claude_max_cost_usd`.
- **Working directory resolution**: `directory_resolver.py` maps task keywords to appropriate directories (vault, LifeOS, other projects, home).
- **Clean environment**: Child processes strip `CLAUDE_*` env vars to avoid session contamination.

### Strengths

- **Full system access**: Claude Code has filesystem, terminal, browser, and MCP tool access. This is the "do anything" executor.
- **Interactive workflows**: Clarification and plan approval create genuine human-in-the-loop interaction via Telegram.
- **Notification relay**: [NOTIFY] messages provide real-time progress without flooding the user with every intermediate step.
- **Robust lifecycle**: Watchdog timeout, cost cap, cancel support, and cleanup on all exit paths.

### Weaknesses

- **Single session limit**: Only one Claude Code session at a time. "Run my backup and also fix the bug in sync" requires sequential execution.
- **No task queue**: If Claude Code is busy, the user gets "Claude Code is busy" with no option to queue the task.
- **Plan mode heuristic is simplistic**: Keyword matching (`"refactor"`, `"implement"`) is fragile. "Add a small feature" might not trigger plan mode when it should.
- **No persistent session history**: Completed sessions are only kept for 5 minutes for follow-up. After that, all context is lost.
- **No progress streaming to Telegram**: Only [NOTIFY] messages are relayed. The user can't see what Claude Code is actually doing in real-time (unlike the web UI).
- **No approval for destructive actions**: Claude Code runs with `--dangerously-skip-permissions`. While the system prompt instructs caution, there's no Telegram-side confirmation for risky operations.
- **Cost visibility only on completion**: The user doesn't know how much a session costs until it finishes (except via heartbeat).

---

## 7. Reminder & Scheduling System

### Architecture

- **Storage**: JSON file at `~/.lifeos/reminders.json`
- **Scheduler**: Background thread polling every 60 seconds for due reminders
- **Message types**: `static` (literal text), `prompt` (runs through full chat pipeline), `endpoint` (calls a LifeOS API endpoint)
- **Timezone**: Cron expressions are interpreted in the reminder's timezone (default Eastern), stored as UTC

### Strengths

- **Prompt-type reminders**: Can run complex queries on schedule, e.g., "Every morning at 7am, summarize my calendar and email." This is a powerful primitive.
- **Vault dashboard**: Automatically generates `LifeOS/Reminders/Dashboard.md` in Obsidian with recurring/upcoming/past tables.
- **Timezone-aware**: Cron expressions are interpreted in the user's timezone, stored in UTC.

### Weaknesses

- **60-second polling granularity**: Reminders can fire up to 60 seconds late. Not suitable for time-critical notifications.
- **No chaining**: Can't say "after this reminder fires, do X." Reminders are independent.
- **No conditional logic**: Can't say "remind me about X only if I haven't done Y." No integration with task completion status.
- **Agent tools limited**: The `manage_reminders` agent tool only supports `create` and `list`. Missing: `update`, `delete`, `toggle`. These are handled in the chat route directly but not available to the agentic loop.
- **No "smart" prompts**: Prompt-type reminders run a static query string. They can't adapt based on what happened since they were created.

---

## 8. Conversation Context Management

### How It Works

- **SQLite storage**: Conversations and messages are persisted in `conversations.db`
- **Context extraction**: `extract_context_from_history()` scans recent messages for person names, sources, reminder references, and pending selections
- **Follow-up expansion**: Two mechanisms:
  1. `expand_followup_query()` in `chat_helpers.py` - pattern-based pronoun resolution
  2. `expand_followup_with_context()` in `conversation_context.py` - context-aware expansion using routing metadata
- **Staleness check**: Context expires after 30 minutes

### Strengths

- **Routing metadata preservation**: Each assistant message stores routing metadata (which sources were used, person referenced, reminder created). This enables follow-up understanding.
- **Pending selection state**: Disambiguating numbered lists (which reminder to delete) works across turns.
- **Person pronoun resolution**: "their email" after asking about John correctly resolves to John.

### Weaknesses

- **Telegram has one conversation per chat**: The bot tracks `{chat_id: conversation_id}`, so only one active conversation. No way to reference or resume old conversations from Telegram.
- **No cross-conversation memory**: If you asked about a person last week, the agent starts from scratch.
- **Context window is small**: Last 10 messages for the agent loop, last 6 for context extraction. Long conversations lose early context without summarization.
- **No entity linking in context**: The conversation context tracks person names as strings. If the user says "him" referring to someone mentioned 8 messages ago (outside the 6-message lookback), context is lost.

---

## 9. Model Selection & Cost Management

### How It Works

- **Query complexity classifier**: Keyword-based scoring that maps queries to haiku/sonnet/opus
- **Source count factor**: 4+ sources upgrades to opus
- **Context size factor**: 8000+ tokens upgrades to opus
- **Agent loop defaults to the recommended model for all rounds**

### Current Model Config

```python
CLAUDE_MODELS = {
    "haiku": "claude-sonnet-4-5-20250929",  # Sonnet as minimum
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-5-20251124",
}
```

Note: "haiku" tier actually uses Sonnet 4.5, not Haiku. This means there's no actual cost differentiation between haiku and sonnet tiers.

### Cost Tracking

- Per-conversation usage recorded in `usage_store`
- Agent loop tracks input/output/cache tokens per round
- Claude Code sessions track `total_cost_usd` from the result event

### Strengths

- **Prompt caching**: System prompt and tool definitions are cached, reducing costs on subsequent rounds.
- **Cost awareness**: Usage tracking per conversation enables cost monitoring.

### Weaknesses

- **No budget controls in chat**: Claude Code has `claude_max_cost_usd`, but the chat pipeline has no per-query or daily budget limits.
- **Haiku = Sonnet**: The "cheap" tier isn't actually cheap. All queries pay Sonnet pricing.
- **No model routing for tool use**: All tool rounds use the same model. A cheaper model could handle simple tool dispatch while a more capable model does final synthesis.

---

## 10. Current Capabilities Assessment

### What the System Can Do Today

| Capability | Quality | Notes |
|-----------|---------|-------|
| Answer questions about personal data | Good | Vault, email, calendar, messages, Slack, Drive |
| Look up people and relationships | Good | Entity resolution, relationship strength, facts, CRM profiles |
| Create/list/complete tasks | Good | Full CRUD with natural language, smart context detection |
| Set/list/edit/delete reminders | Good | One-time and recurring, timezone-aware, prompt-type |
| Draft emails | Good | Extract recipients, subject, body from natural language |
| Web search | Good | Claude's native web_search tool |
| Multi-turn conversation | Good | Context tracking, follow-up expansion, pronoun resolution |
| Execute system tasks (Claude Code) | Good | Full filesystem/terminal/browser access with notifications |
| Meeting prep briefings | Good | Aggregates CRM, vault, calendar, email data |
| Scheduled briefings | Basic | Prompt-type reminders can run complex queries on schedule |
| Voice messages | None | Not supported |
| Image/document input | None (Telegram) | Supported in web UI via attachments, but not Telegram |
| Real-time progress | Basic | Typing indicator + [NOTIFY] for Claude Code |
| Multi-step workflows | Limited | Claude Code can do multi-step, but chat pipeline is single-turn |

---

## 11. Limitations & Gaps

### Critical Gaps (Blocking the "Do Anything" Vision)

1. **No long-running task queue**: A request like "research flights to Tokyo" that takes 5+ minutes blocks the entire bot. No way to queue, parallelize, or background tasks.

2. **No proactive intelligence**: The system only responds to messages. It can't observe patterns and proactively suggest things ("You haven't followed up with John in 3 weeks").

3. **No multi-modal Telegram input**: Can't process voice notes, photos, documents, or locations from Telegram. Users can't say "what's in this screenshot" or send a voice command.

4. **No workflow orchestration**: Can't chain actions: "Search for flights, compare prices, create a summary doc, and remind me to book tomorrow." Each step requires a separate message.

5. **Single Claude Code session**: Can't run parallel tasks. "Fix the bug AND update the readme" must be sequential.

### Significant Gaps

6. **No persistent memory**: The agent doesn't learn from past interactions. Memories are only in the conversation store, not in the agentic system prompt.

7. **No media output**: Can't send images, charts, files, or formatted documents back through Telegram.

8. **No approval workflows for chat actions**: Creating tasks, setting reminders, and drafting emails happen without confirmation. "Remind me to call mom" immediately creates the reminder with no "Is this right?" step.

9. **Limited error context**: When something fails, the user gets a generic error. No diagnostic info or suggested alternatives.

10. **No webhook support**: Long-polling works but is less efficient and adds latency vs. webhooks.

### Minor Gaps

11. **No inline keyboards/buttons**: All interaction is text-based. Approve/reject, quick actions, and selections could use Telegram's button interface.

12. **No message editing**: Status updates could replace previous messages instead of sending new ones.

13. **No command auto-complete**: Telegram supports bot command menus but they're not configured.

14. **Reminder agent tool is incomplete**: Only create/list available to the agentic loop; update/delete are chat-route only.

---

## 12. Scenario Analysis

### Scenario 1: "Research the best flights to Tokyo next month and summarize options"

**Current behavior**: Intent classification returns `none` (not a task, reminder, compose, or code intent). Falls through to the agentic loop. The agent would call `search_web` with a flights query. Claude's web search returns some results, and the agent synthesizes them into a response.

**Limitations**:
- Single web search call may not be comprehensive enough
- No ability to visit specific flight search engines (Kayak, Google Flights)
- No price comparison across dates
- Response is within the 90-second API timeout
- Cannot bookmark results or create a follow-up task automatically

**What it would take**:
- The agentic loop handles this reasonably well for a quick answer
- For deep research: Claude Code could browse flight search sites, but the `code` intent detection would need to trigger
- True flight research would benefit from a dedicated "research agent" that can spend 5+ minutes browsing and comparing

### Scenario 2: "Run my backup, check if it succeeded, and tell me"

**Current behavior**: Intent classification detects `code` intent. Claude Code is spawned with the task. It would:
1. Find and execute the backup script
2. Check exit code and logs
3. Send a [NOTIFY] with results

**Limitations**:
- Works well if the backup completes within the timeout
- If backup takes 30+ minutes, the watchdog timer may kill the session
- No way to check on the backup status mid-execution (only heartbeat)

**What it would take**:
- This mostly works today via Claude Code
- Longer timeouts or a "background task" mode for long-running scripts
- An explicit "run shell command and report" tool in the agentic loop would avoid the overhead of spawning a full Claude Code session

### Scenario 3: "Every morning at 7am, check my calendar and Slack, and send me a briefing"

**Current behavior**: Intent classification detects `reminder` with `create` sub-type. Creates a prompt-type reminder with:
```json
{
  "schedule_type": "cron",
  "schedule_value": "0 7 * * *",
  "message_type": "prompt",
  "message_content": "Check my calendar and Slack for today and send a briefing"
}
```

Every morning at 7 AM ET, the scheduler fires, runs the message through `chat_via_api`, and sends the result via Telegram.

**Limitations**:
- The prompt is static -- it can't adapt if the user's preferences change
- If the chat pipeline is slow (e.g., Ollama down, multiple API calls), the briefing may be delayed
- No way to customize the briefing format after creation
- The reminder creates a new conversation each time (no continuity)
- If the server is down at 7 AM, the briefing is missed (no retry logic)

**What it would take**:
- This actually works today! It's one of the system's strongest features
- Improvements: retry logic, customizable prompt templates, delivery confirmation

### Scenario 4: "Draft an email to John about the project update, using the notes from our last meeting"

**Current behavior**: Intent classification detects `compose`. Extracts draft params using Haiku. BUT: the chat pipeline's compose handler doesn't search for meeting notes first. It creates a draft with whatever context Haiku can infer from the query alone.

**What it would take**:
- The agentic loop could handle this better: first `person_info` to find John's email, then `search_calendar` and `search_vault` for meeting notes, then `create_email_draft` with rich context
- Currently, compose intent is handled before the agentic loop, so it bypasses the agent's ability to gather context first
- Fix: detect compound compose intents and route them through the agentic loop instead of the shortcut path

---

## 13. Proposals for the "Do Anything" Vision

### Tier 1: Quick Wins (Low Effort, High Impact)

#### 1A. Telegram Inline Keyboards for Confirmations
Add button-based interactions for approve/reject, task selection, and quick actions. Telegram's `InlineKeyboardMarkup` provides a much better UX than typing "approve" or "1".
```python
# Example: Confirmation before creating a reminder
keyboard = {
    "inline_keyboard": [[
        {"text": "Confirm", "callback_data": "confirm_reminder_123"},
        {"text": "Change Time", "callback_data": "edit_reminder_123"},
        {"text": "Cancel", "callback_data": "cancel_reminder_123"},
    ]]
}
```

#### 1B. Complete the Reminder Agent Tool
Add `update` and `delete` actions to the `manage_reminders` tool so the agentic loop can handle all reminder operations without falling back to the chat route's hardcoded handlers.

#### 1C. Route Compound Compose Intents to Agent Loop
When a compose request references personal data ("using notes from our last meeting"), route through the agentic loop instead of the shortcut compose handler. The agent can gather context before drafting.

#### 1D. Add Telegram Bot Command Menu
Register commands with `BotFather` so users see suggestions when typing `/`:
```
/new - Start new conversation
/code - Run a task with Claude Code
/status - Check system health
/tasks - List open tasks
/reminders - List active reminders
```

### Tier 2: Architecture Improvements (Medium Effort, Transformative)

#### 2A. Background Task Queue
Replace the synchronous "one request at a time" model with an async task queue:

```
User message -> Intent Classification -> Task Queue
                                            |
                                            v
                                    Task Runner (N workers)
                                            |
                                    Notification on completion
```

- Allow multiple Claude Code sessions in parallel
- Queue tasks when busy instead of rejecting
- Support cancellation and priority
- Report results asynchronously via Telegram

#### 2B. Workflow Engine (Multi-Step Action Chains)
Enable compound requests like "search for X, summarize it, create a doc, and remind me":

```python
@dataclass
class WorkflowStep:
    action: str        # "search", "synthesize", "create_doc", "remind"
    input_ref: str     # Reference to previous step's output
    params: dict

class Workflow:
    steps: list[WorkflowStep]
    status: str        # pending, running, completed, failed
    results: dict      # step_name -> result
```

The agentic loop already does multi-step reasoning within a single query. The workflow engine would extend this to:
- Persist state across API calls
- Handle long-running steps (background execution)
- Report progress per step
- Allow manual intervention between steps

#### 2C. Voice Message Support
Add Telegram voice message handling:
1. Receive `voice` or `audio` message
2. Download the `.ogg` file
3. Transcribe via Whisper (local with hardware upgrade) or API
4. Feed transcript into the chat pipeline
5. Optionally respond with voice (TTS)

#### 2D. Persistent Agent Memory
Integrate the existing `memory_store` into the agentic system prompt:
- Load relevant memories at the start of each agent loop
- Let the agent create memories from important interactions
- Include user preferences ("Nathan prefers bullet points", "Always check both email accounts")

#### 2E. Proactive Intelligence
Add a "proactive check" system that runs periodically:
- Communication gap detection (haven't talked to X in Y days)
- Calendar prep (tomorrow's meetings need prep)
- Task deadline reminders (task X is due tomorrow)
- Pattern detection ("you usually email Y on Fridays")

This could use prompt-type reminders as the execution mechanism, with a dedicated "proactive agent" prompt.

### Tier 3: Hardware-Enabled Capabilities (Requires Corsair AI Workstation)

#### 3A. Local LLM for Intent Classification and Simple Queries
With the workstation's GPU capacity:
- Run a strong local model (e.g., Llama 3.1 70B or Qwen 2.5 72B) for intent classification, eliminating the Ollama/Haiku fallback chain
- Handle simple queries entirely locally (zero API cost)
- Use Claude API only for complex reasoning and synthesis
- Target: <200ms intent classification, <2s simple query response

#### 3B. Local Whisper for Voice Transcription
- Run Whisper Large locally for high-quality transcription
- No API cost or privacy concerns
- Enable voice-first interaction with Telegram

#### 3C. Local Embedding Model for Real-Time Search
- Replace or supplement the current embedding model
- Enable real-time re-embedding when vault files change
- Faster hybrid search responses

#### 3D. Browser Automation Agent
With more compute:
- Dedicated browser automation for research tasks
- Can browse multiple sites, compare results, take screenshots
- Sends periodic updates via Telegram
- Claude Code already has `--chrome` support; this would be a purpose-built version

### Tier 4: Aspirational (Long-term)

#### 4A. Multi-Agent Orchestration
Instead of one Claude Code session at a time:
- A "coordinator" agent that breaks tasks into subtasks
- Multiple specialist agents running in parallel (research, code, communication)
- Coordinator synthesizes results and reports to user

#### 4B. Bi-directional Telegram Integration
- Send formatted rich content (images, documents, interactive cards)
- Use Telegram's web app feature for complex interactions (timeline views, dashboard)
- File upload/download through Telegram

#### 4C. Smart Scheduling
- Observe the user's patterns (when they're most responsive, when they have free time)
- Schedule reminders and briefings at optimal times
- Automatically reschedule if the user is in a meeting or DND

#### 4D. Cross-Platform Communication
- iMessage sending (requires macOS Shortcuts or AppleScript bridge)
- Slack message sending (already have the integration for reading)
- WhatsApp sending via API

---

## Summary: Architecture Health

| Dimension | Score | Notes |
|-----------|-------|-------|
| **Message handling** | 7/10 | Solid for text, missing voice/media/buttons |
| **Intent classification** | 8/10 | Three-tier fallback is resilient; some redundancy with agent |
| **Agentic reasoning** | 8/10 | Well-designed loop with parallel tools and streaming |
| **Action execution** | 7/10 | Good CRUD for tasks/reminders/drafts, but no chaining |
| **Claude Code integration** | 9/10 | Excellent orchestration with plan mode, clarification, follow-ups |
| **Scheduling** | 7/10 | Prompt-type reminders are powerful; needs retry and chaining |
| **Context management** | 6/10 | Works within a conversation; no cross-conversation or long-term memory |
| **Error handling** | 5/10 | Generic errors; no retry, no alternative suggestions |
| **Scalability** | 4/10 | Single-threaded processing, one Claude Code session, no queue |
| **Multi-modal** | 3/10 | Web UI supports attachments; Telegram is text-only |

### Top 5 Priorities for Transformation

1. **Background task queue** -- unblocks parallelism and long-running tasks
2. **Voice message support** -- enables phone-first interaction
3. **Persistent agent memory** -- makes the assistant smarter over time
4. **Telegram inline keyboards** -- immediate UX improvement
5. **Workflow engine** -- enables compound "do X then Y then Z" requests

The existing architecture is solid and well-engineered. The agentic loop and Claude Code orchestrator are genuinely impressive. The main bottleneck is the synchronous, single-session model -- once background task execution and queuing are added, the "do anything from Telegram" vision becomes realistic.
