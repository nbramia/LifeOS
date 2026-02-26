# Round 2: Agentic Pipeline Cross-Pollination

**Perspective:** Agentic Pipeline / "Do Anything" Architecture
**Inputs:** All five Round 1 audits (backend, frontend, MCP, Telegram/chat, infrastructure)
**Date:** 2026-02-13

---

## 1. The "Do Anything" Architecture: Full Pipeline

Synthesizing all five audits reveals the complete picture of what it takes to go from a Telegram message to arbitrary task execution and back. The current system has pieces of this pipeline scattered across different layers, but no unified orchestration framework.

### Current Architecture (As-Is)

```
Telegram Message
    |
    v
TelegramBotListener (long-polling, text-only)
    |
    |--- /code <task> -----> ClaudeOrchestrator (single session, subprocess)
    |                              |
    |                              |--- Claude CLI with MCP tools + filesystem + browser
    |                              |--- [NOTIFY] -> Telegram
    |                              |--- [CLARIFY] -> pause, ask user
    |                              v
    |                         Result -> Telegram
    |
    v (all other messages)
chat_via_api() -> POST /api/ask/stream (SSE)
    |
    v
Intent Classification (Ollama -> Haiku -> patterns)
    |
    |--- compose/task/reminder -> Direct handlers in chat.py (1800+ lines)
    |--- code intent -> SSE event -> Telegram spawns Claude Code
    |--- none -> Agentic Loop
                    |
                    v
              Claude API (5 tool rounds max, 12 tools)
                    |--- search_vault, search_email, search_calendar, ...
                    |--- manage_tasks, manage_reminders, create_email_draft
                    v
              Streaming text response -> Telegram (4096 char limit)
```

### What's Missing from the Picture

The five audits collectively reveal these gaps in the pipeline:

1. **No input processing layer** (Telegram audit): Voice, images, documents, locations all dropped. The pipeline starts and ends with text.
2. **No task queue** (Infrastructure audit): Everything is synchronous. One request blocks the next.
3. **Two competing dispatch systems** (Backend + Telegram audits): Intent classification in `chat_helpers.py` AND Claude's own tool selection in the agent loop are partially redundant.
4. **Write capability gaps** (MCP audit): Can search everything but can only write to tasks, reminders, and email drafts. No iMessage send, no calendar create, no Slack send, no vault file management.
5. **No persistent execution context** (Backend audit): Claude Code sessions evaporate after 5 minutes. Agent loop starts fresh every conversation.
6. **No output richness** (Frontend + Telegram audits): Text-only responses. No charts, no files, no interactive elements.
7. **No feedback loops** (All audits): System doesn't learn from what worked or failed.

### Target Architecture (To-Be)

```
INPUT LAYER
    Telegram: text, voice, photo, document, location, callback buttons
    Web UI: text, attachments, rich interactions
    MCP: tool calls from Claude Code / external agents
    Scheduled: cron reminders, proactive triggers
        |
        v
PREPROCESSING
    Voice -> Whisper STT (local GPU)
    Image -> Vision model (local GPU) -> description + OCR
    Document -> Text extraction + indexing
    Location -> Geocode + context
        |
        v
INTAKE & ROUTING
    Unified message envelope: {text, media[], source, conversation_id, user_context}
        |
        v
    Autonomy Classifier (see Section 3)
        |
        |--- Level 1-2: Agent Loop (fast, synchronous, streaming)
        |--- Level 3: Agent Loop + Confirmation Gate
        |--- Level 4-5: Task Queue -> Background Worker
        |
        v
EXECUTION ENGINE (see Section 2)
    Task Queue (Redis/SQLite)
        |
        |--- Agent Workers (N concurrent)
        |       |--- Claude API with tools
        |       |--- Local LLM for routing/classification
        |       |--- Tool executor (read + write tools)
        |
        |--- Claude Code Workers (M concurrent)
        |       |--- Filesystem, terminal, browser
        |       |--- MCP tools for LifeOS data access
        |
        v
    Execution Context Store (persistent state per task)
        |
        v
OUTPUT LAYER
    Format adapter per channel:
        Telegram: text (4096 limit), inline keyboards, photos, documents
        Web UI: markdown, charts, interactive widgets
        Vault: persistent notes, task files
        Email: draft creation
        |
        v
FEEDBACK & MEMORY
    - Tool result caching (per conversation)
    - Cross-conversation memory (persistent preferences, learned patterns)
    - Execution logs (what was tried, what worked)
    - Cost tracking (per task, per day, per channel)
```

---

## 2. Task Execution Engine

The infrastructure audit identified the need for a task queue. The MCP audit identified tool gaps. The backend audit found service limitations. The Telegram audit found single-session Claude Code blocking. Here is the unified design.

### Why the Current Model Breaks

The current system has three execution modes, none of which scale:

| Mode | Mechanism | Concurrency | Duration | State |
|------|-----------|-------------|----------|-------|
| Agent Loop | Inline in SSE response | 1 per request | <90s (API timeout) | None after response |
| Claude Code | Subprocess, mutex lock | 1 globally | Up to 1 hour | 5-min follow-up only |
| Reminders | Scheduler thread, 60s poll | 1 at a time | Same as agent loop | None |

Every mode is single-threaded or single-session. A user who says "research flights to Tokyo and also fix the sync bug" must wait for one to finish before starting the other.

### Unified Execution Engine Design

```
                    LifeOS API
                        |
                        v
                  Task Intake
                  (creates TaskRecord in SQLite)
                        |
            +-----------+-----------+
            |                       |
      Synchronous Path        Async Path
      (Level 1-2 tasks)      (Level 3-5 tasks)
            |                       |
            v                       v
      Agent Loop              Task Queue (SQLite-backed)
      (existing, inline)            |
                              +-----+-----+
                              |           |
                         Agent Worker  Claude Code Worker
                              |           |
                              v           v
                        Claude API   Claude CLI subprocess
                        + tools      + MCP + filesystem
```

### TaskRecord Schema

```python
@dataclass
class TaskRecord:
    id: str                    # UUID
    source: str                # "telegram", "web", "scheduled", "mcp"
    input_text: str            # Original user message
    input_media: list[dict]    # Preprocessed media (transcripts, descriptions)
    autonomy_level: int        # 1-5 (see Section 3)
    status: str                # pending, running, awaiting_confirmation, completed, failed
    worker_type: str           # "agent", "claude_code", "local_llm"
    conversation_id: str       # Links to conversation context
    parent_task_id: str | None # For subtasks / workflow steps
    result: str | None         # Final output text
    result_artifacts: list     # Files, images, drafts created
    cost_usd: float            # Running cost total
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    retry_count: int
    max_retries: int
    execution_log: list[dict]  # Step-by-step log of what was done
```

### Why SQLite, Not Redis

The infrastructure audit shows LifeOS already runs 5 SQLite databases totaling 2.3 GB. Adding Redis would introduce a new dependency and process to manage. For a single-user system with <100 concurrent tasks, an SQLite-backed queue is simpler and more reliable:

- WAL mode for concurrent read/write
- Persistent across server restarts (unlike Redis without AOF)
- No additional process to monitor
- Already proven in the codebase (conversations, interactions, sync health all use SQLite)

For the Corsair workstation, Redis could be added later if throughput requires it.

### Worker Pool Architecture

```python
class WorkerPool:
    """Manages concurrent task execution."""

    def __init__(self, max_agent_workers=3, max_code_workers=2):
        self.agent_semaphore = asyncio.Semaphore(max_agent_workers)
        self.code_semaphore = asyncio.Semaphore(max_code_workers)

    async def execute_agent_task(self, task: TaskRecord):
        async with self.agent_semaphore:
            # Run agent loop, store results in task record
            ...

    async def execute_code_task(self, task: TaskRecord):
        async with self.code_semaphore:
            # Spawn Claude Code session, relay notifications
            ...
```

Key changes from current system:
- **Multiple Claude Code sessions**: Replace the single `_lock` mutex with a semaphore (default 2 concurrent sessions)
- **Agent tasks run in background**: Instead of blocking the SSE response, long-running agent tasks are queued and results delivered asynchronously via Telegram
- **Automatic routing**: The autonomy classifier (Section 3) decides whether a task runs inline (fast) or queued (background)

### Write Tool Expansion

The MCP audit identified that 22% of API endpoints are exposed as tools. The most critical write gaps for "do anything":

| Capability | Current State | Required |
|-----------|--------------|----------|
| Update CRM (notes, tags) | MCP: missing, Agent: missing | Agent tool: `update_person` |
| Create calendar event | No endpoint exists | New endpoint + agent tool |
| Send iMessage | No capability | AppleScript bridge + agent tool |
| Send Slack message | Read-only | Slack Web API POST + agent tool |
| Manage vault files | No capability | Create/rename/move notes + agent tool |
| Run shell commands | Claude Code only | Lightweight `run_command` agent tool |

The agent loop should gain these tools incrementally. Each tool needs confirmation gates appropriate to its autonomy level (Section 3).

---

## 3. Autonomy Levels

Not all tasks need the same AI autonomy. The current system has two modes: fully autonomous (agent loop executes without asking) and plan-mode (Claude Code proposes, user approves). This is too coarse. Here is a five-level system.

### Level Definitions

| Level | Type | Examples | Confirmation | Timeout | Worker |
|-------|------|---------|--------------|---------|--------|
| **L1** | Simple lookup | "What's John's email?", "What meetings do I have today?" | None | 30s | Agent (inline) |
| **L2** | Multi-step read | "Catch me up on the project", "Summarize last week's emails" | None | 90s | Agent (inline) |
| **L3** | Write action | "Email John about the meeting", "Create a task to review Q4" | Confirm before send/create | 90s | Agent (inline + gate) |
| **L4** | Long-running autonomous | "Research flights to Tokyo", "Analyze my communication patterns" | Confirm plan, then autonomous | 30m | Background worker |
| **L5** | Scheduled/recurring | "Every morning, brief me on my day", "Weekly review of neglected contacts" | Confirm on first creation | Varies | Scheduler + worker |

### Autonomy Classifier

The current three-tier intent classification (Ollama -> Haiku -> patterns) would be extended:

```python
@dataclass
class AutonomyDecision:
    level: int           # 1-5
    worker_type: str     # "agent_inline", "agent_background", "claude_code"
    needs_confirmation: bool
    confirmation_message: str | None  # "Create task 'Review Q4'?"
    estimated_duration: str           # "seconds", "minutes", "long"

def classify_autonomy(message: str, conversation_context: dict) -> AutonomyDecision:
    """
    Classify message into autonomy level.

    Uses the existing Ollama -> Haiku -> pattern chain, extended with:
    - Write action detection (Level 3): send, create, delete, update, schedule
    - Complexity estimation (Level 4): research, analyze, compare, multi-step
    - Recurrence detection (Level 5): every, daily, weekly, recurring
    """
```

### Confirmation Gates

The Telegram audit noted "no approval workflows for chat actions." The MCP audit noted email drafts use a "read-then-review" safety pattern. Extending this:

**Level 3 confirmations via Telegram inline keyboards:**
```
User: "Email John about the project update"

LifeOS: I'll draft an email to john@example.com:
  Subject: Project Update
  Body: [preview]

  [Send Draft] [Edit] [Cancel]
```

**Level 4 confirmations via plan summary:**
```
User: "Research the best CRM tools and write a comparison doc"

LifeOS: Here's my plan:
  1. Web search for top CRM tools (2026 reviews)
  2. Compare features, pricing, and reviews
  3. Create a comparison note in your vault at Projects/CRM-Comparison.md

  Estimated time: 5-10 minutes
  Estimated cost: ~$0.50

  [Approve] [Modify] [Cancel]
```

This unifies the Claude Code plan-mode concept with the chat pipeline, making confirmation available everywhere, not just in `/code` tasks.

---

## 4. Context & Memory

The audits collectively paint a picture of fragmented context: conversation context expires after 30 minutes, Claude Code sessions expire after 5 minutes, agent loops start fresh, and there is no cross-conversation memory in the agentic system prompt.

### Context Layers

```
Layer 1: Immediate Context (per request)
    - Current message + last 10 conversation messages
    - Active person/topic from conversation_context.py
    - Tool results from current session

Layer 2: Conversation Context (per conversation, hours)
    - Full conversation in conversations.db
    - Routing metadata per message
    - Pending selections / disambiguation state

Layer 3: Session Context (per Claude Code session, minutes)
    - Working directory, files modified
    - Plan state (proposed, approved)
    - Follow-up window (currently 5 minutes)

Layer 4: Persistent Memory (cross-conversation, permanent)
    - memories table (exists but not injected into agent prompt)
    - User preferences (learned patterns)
    - Execution history (what tools were useful for what queries)

Layer 5: Background Knowledge (system-wide, always available)
    - CRM data (people, relationships, facts)
    - Vault content (notes, journals, meeting transcripts)
    - Calendar, email, messages (via tools)
```

### What Needs to Change

**Inject persistent memories into the agent system prompt.** The `memories` system exists (create/search via MCP) but the `agent_system_prompt.py` never includes them. Fix:

```python
def build_system_prompt() -> list[dict]:
    # ... existing static prompt ...

    # Inject relevant memories
    from api.services.memory_store import search_memories
    memories = search_memories("user preferences assistant behavior", limit=10)
    if memories:
        memory_text = "\n## Remembered preferences\n"
        for m in memories:
            memory_text += f"- {m.content}\n"
        # Add as non-cached block (changes with memory updates)
        blocks.append({"type": "text", "text": memory_text})

    return blocks
```

**Extend Claude Code follow-up window.** The current 5-minute window is too short. The infrastructure audit shows sessions can run for up to an hour. A user might need 15 minutes to review results before following up. Increase to 30 minutes, or better, persist session context to disk so it survives server restarts.

**Tool result caching within conversations.** The backend audit noted "if the same person is looked up twice, it hits the API again." Add a per-conversation cache:

```python
class ConversationCache:
    """Cache tool results within a conversation to avoid redundant lookups."""

    def __init__(self, ttl_seconds=300):
        self._cache: dict[str, tuple[float, str]] = {}

    def get(self, tool_name: str, input_hash: str) -> str | None:
        key = f"{tool_name}:{input_hash}"
        if key in self._cache:
            ts, result = self._cache[key]
            if time.time() - ts < self.ttl_seconds:
                return result
        return None
```

**Cross-task context for background tasks.** When a Level 4 task runs in the background, its results should be accessible to subsequent conversations. The TaskRecord's `result` and `execution_log` fields provide this. When a user asks "what did you find about flights?", the agent can look up completed background tasks.

---

## 5. Local LLM Integration with the Corsair AI Workstation

The hardware upgrade fundamentally changes the agentic architecture. Currently, every intelligent decision requires either a slow 7B local model or an API call. With a powerful GPU, the balance shifts dramatically.

### What Runs Locally vs. Claude API

| Function | Current | With Workstation | Rationale |
|----------|---------|-----------------|-----------|
| **Intent classification** | Ollama 7B -> Haiku API | Local 70B | <200ms, zero cost, better accuracy |
| **Autonomy classification** | Not implemented | Local 70B | New capability, needs to be fast and free |
| **Query routing** | Ollama 7B -> Haiku API | Local 70B | Better source selection with larger model |
| **Tool selection** | Claude API (in agent loop) | Claude API | Keep: requires strong reasoning about tool composition |
| **Synthesis (simple)** | Claude Sonnet API | Local 70B | "What's John's email?" doesn't need Claude |
| **Synthesis (complex)** | Claude Sonnet/Opus API | Claude API | Keep: complex multi-source synthesis is Claude's strength |
| **Fact extraction** | Claude + Ollama validation | Local 70B for both | Batch processing, privacy-sensitive |
| **Embedding** | CPU sentence-transformers | GPU sentence-transformers | 10-50x speedup for indexing |
| **Reranking** | CPU cross-encoder | GPU cross-encoder (larger model) | Better quality + faster |
| **Voice transcription** | Not supported | Local Whisper Large V3 | New capability, privacy-preserving |
| **Image understanding** | Not supported | Local vision model (LLaVA/InternVL) | New capability for Telegram photos |
| **Summarization** | Claude API | Local 70B | Daily digests, meeting summaries at zero API cost |
| **Code tasks** | Claude Code (API) | Claude Code (API) | Keep: coding requires frontier model quality |

### Tiered Model Architecture

```
Tier 0: Pattern Matching (no model)
    - Trivial intent detection ("yes", "no", "/status")
    - 0 cost, <1ms

Tier 1: Local Small (Qwen 2.5 7B, existing)
    - Fact validation, simple classification
    - 0 cost, <500ms

Tier 2: Local Large (Qwen 2.5 72B or Llama 3.1 70B)
    - Intent classification, autonomy classification
    - Query routing with full context
    - Simple synthesis ("What time is my meeting?")
    - Summarization, fact extraction
    - 0 cost, 1-5s

Tier 3: Claude Sonnet API
    - Multi-tool agent loop (tool selection + synthesis)
    - Complex queries requiring cross-source reasoning
    - $3/$15 per million tokens

Tier 4: Claude Opus API
    - Large context (>8K tokens), many sources
    - Complex reasoning, nuanced synthesis
    - $15/$75 per million tokens
```

### The "Local-First" Agent Loop

For Tier 2-eligible queries (the majority of simple lookups), the entire pipeline runs without API calls:

```
Message -> Local 70B classifies intent
    -> Local 70B selects tools + generates tool calls
    -> Tools execute (same as today)
    -> Local 70B synthesizes response
    -> Response sent via Telegram

Total cost: $0.00
Total latency: 3-8 seconds
```

For complex queries, the local model handles routing and the API handles synthesis:

```
Message -> Local 70B classifies intent + routes sources
    -> Claude Sonnet runs agent loop with tools
    -> Response sent via Telegram

Saved: classification API call (~$0.01 per query)
```

### Implementation Path

1. **GPU embeddings first** (immediate, highest ROI): Change `device="cpu"` to `device="cuda"` in `embeddings.py`. This alone transforms reindex time.
2. **Larger routing model** (week 1): Replace `qwen2.5:7b` with `qwen2.5:72b` in Ollama. Update timeouts (larger model is slower but more accurate).
3. **Local synthesis for simple queries** (week 2-3): Add a `local_synthesis` flag to model_selector. When query is simple (Level 1) and source count is low, synthesize locally.
4. **Voice + vision** (week 3-4): Add Whisper and a vision model. Wire into Telegram input preprocessing.

---

## 6. Proactive Intelligence

Every audit identifies this gap: the system only responds to messages. It never initiates. The infrastructure for proactive behavior already exists in pieces -- prompt-type reminders can run any query on a schedule. What's missing is the intelligence layer that decides WHAT to proactively communicate and WHEN.

### Proactive Trigger Types

| Trigger | Source | Example |
|---------|--------|---------|
| **Communication gap** | CRM relationship metrics | "You haven't talked to Mom in 18 days" |
| **Calendar prep** | Calendar + CRM | "You have a meeting with Kevin in 2 hours. Here's context..." |
| **Task deadline** | Task manager | "Task 'Review Q4 report' is due tomorrow" |
| **Pattern detection** | Interaction history | "You usually email the team update on Fridays. Want me to draft it?" |
| **Data freshness** | Sync health | "Gmail sync failed for 2 days. Your email data is stale." |
| **Birthday** | CRM facts | "Sarah's birthday is in 3 days. Want me to set a reminder?" |
| **Follow-up needed** | Email/message analysis | "John asked you a question 2 days ago that you haven't answered" |
| **Relationship cooling** | Relationship metrics | "Your relationship strength with Alex dropped from 72 to 45" |

### Architecture: Proactive Agent

```python
class ProactiveAgent:
    """Runs periodically to detect situations worth proactively notifying about."""

    # Runs every 30 minutes (not every 60 seconds like reminders)
    INTERVAL = 1800

    # Each check is a function that returns Optional[ProactiveNotification]
    checks = [
        check_communication_gaps,
        check_upcoming_meetings,
        check_task_deadlines,
        check_unanswered_messages,
        check_relationship_trends,
        check_birthday_proximity,
        check_sync_health,
    ]

    async def run_cycle(self):
        notifications = []
        for check in self.checks:
            result = await check()
            if result and not self._recently_sent(result.dedup_key):
                notifications.append(result)

        # Prioritize and rate-limit (max 3 per cycle, max 8 per day)
        for notification in self._prioritize(notifications):
            await send_telegram(notification.message, keyboard=notification.actions)
            self._mark_sent(notification.dedup_key)
```

### Smart Delivery Timing

The infrastructure audit identified the 2:30 AM health check and 7:00 AM nightly alert batching. Proactive notifications should respect the user's schedule:

- **Morning brief (7:00 AM)**: Calendar prep, task deadlines, communication gaps
- **Pre-meeting (2 hours before)**: Meeting prep with attendee context
- **Evening review (8:00 PM)**: Unanswered messages, follow-up suggestions
- **Suppress during**: Focus time (detected from calendar "Focus" blocks), late night (11 PM - 7 AM)

This can be implemented as a prompt-type reminder that runs a proactive agent prompt:

```
Reminder: "Morning Brief"
Schedule: cron "0 7 * * *"
Type: prompt
Content: "Check my calendar for today, identify meetings needing prep,
          check for communication gaps with family and close contacts,
          list any tasks due today or overdue. Format as a concise morning brief."
```

The key insight from reading all audits: the reminder system already supports this via prompt-type reminders. What's needed is:
1. A library of well-crafted proactive prompts
2. A meta-scheduler that enables/disables prompts based on context
3. Deduplication so the same notification isn't sent repeatedly

---

## 7. New Ideas from the Full Picture

Reading all five audits together reveals insights that no single audit could surface.

### 7.1 The Dual-Brain Architecture

The system currently has two "brains" with different capabilities and no coordination:

| | Agent Loop (Chat) | Claude Code (Tasks) |
|---|---|---|
| **Tools** | 12 LifeOS tools | Full filesystem + MCP + browser |
| **Duration** | <90 seconds | Up to 1 hour |
| **Concurrency** | Per-request | 1 globally |
| **Memory** | 10 messages | Session only |
| **Cost** | $0.01-0.50 per query | $0.10-2.00 per task |
| **Strengths** | Fast retrieval, data synthesis | Complex multi-step execution |

These should be unified into a **single orchestration layer** that decides which brain to use for which subtask. A complex request like "Research the best CRM tools, compare them to what we use at work, and write a recommendation doc" should be decomposed:

1. **Agent Loop**: Search vault for current CRM usage, search email for CRM-related discussions (fast, cheap)
2. **Claude Code**: Web research on CRM tools, write comparison document (long-running, needs browser)
3. **Agent Loop**: Synthesize findings and create task to review the document

The orchestrator creates subtasks, routes them to the appropriate worker, and synthesizes results.

### 7.2 The "Personal API" Concept

The MCP audit shows 156 endpoints but only 35 exposed as tools. The frontend audit shows backend capabilities with no UI. Rather than building individual UIs for each capability, LifeOS should embrace being a **personal API** where the AI is the universal interface.

Instead of building a calendar UI, task UI, email UI, and admin UI in the web frontend, invest in making the agent loop so capable that the natural language interface IS the primary interface. The web UI becomes a dashboard for monitoring and visualization (heatmaps, graphs, timelines), while all actions flow through conversation.

This means:
- Every backend endpoint should be accessible as an agent tool
- The MCP server should mirror all agent tools (currently they're different sets)
- Tool descriptions should be rich enough that the AI reliably selects the right tool

### 7.3 Conversation as Operating System

The Telegram audit identified the pipeline as request-response. The infrastructure audit identified no task queue. Combined insight: conversations should be **persistent execution contexts**, not ephemeral request-response cycles.

A conversation should be able to:
- **Span days**: "I'm researching CRM tools this week. Keep findings in this conversation."
- **Have running tasks**: "This conversation has 2 background tasks running. Task 1 completed: [results]. Task 2: 60% done."
- **Accumulate context**: Each tool result enriches the conversation's knowledge, accessible to future queries.
- **Branch**: "Let's explore option A in a side thread."

The `conversations.db` already stores messages with routing metadata. Extending it with task associations and persistent context would transform conversations from chat threads into project workspaces.

### 7.4 The Observation Layer

Five audits, and none mention observing user behavior to improve the system. The interaction store has rich data about communication patterns. The vault has journals and meeting notes. The CRM has relationship dynamics. But this data is only used when explicitly queried.

An observation layer would continuously (but cheaply) analyze incoming data to:
- **Learn tool selection patterns**: "When user asks about a person, they almost always want message history, not email. Adjust tool ordering."
- **Detect life events**: "Three calendar entries this week mention 'moving'. Create a 'Moving' project in tasks."
- **Track goals**: "User created tasks about exercise 4 times this month but completed 0. This might be worth surfacing."
- **Build user model**: "User prefers bullet points over prose. User asks about family on weekends and work on weekdays."

This runs as a lightweight background process using the local 70B model (zero API cost), processing each new sync's data and updating a `user_model.json` that feeds into the system prompt.

### 7.5 Cross-Channel Intelligence

Each audit examined its channel in isolation. The combined view reveals cross-channel opportunities:

- **Telegram message about a person -> Auto-pull CRM context**: When a person's name is mentioned, preload their profile in a sidebar or append a brief context line ("Last contact: 3 days ago via email. Relationship: 78/100").
- **Email received -> Proactive Telegram notification**: "John replied to your project update email. Key point: deadline moved to March 1."
- **Calendar event approaching -> Meeting prep via Telegram**: "Your 1:1 with Sarah starts in 30 min. Key topics from recent emails: [bullets]"
- **Task completed in vault -> Telegram confirmation**: "Task 'Review Q4' was checked off in Obsidian. Shall I notify the team?"

The infrastructure audit's event-driven architecture recommendation (Gmail push notifications, calendar webhooks) is the enabler. Without real-time data ingestion, cross-channel intelligence is limited to daily batch processing at 3 AM.

### 7.6 The Confidence Framework

Across all audits, a pattern emerges: the system makes many decisions (intent classification, entity resolution, tool selection, autonomy level) but has no unified way to express or act on confidence.

A system-wide confidence framework would:
- **Entity resolution**: Already has `link_confidence` (0-1) and review queue. Good model.
- **Intent classification**: Currently returns a category with no confidence score. Should return `(category, confidence)`. Below threshold -> ask user.
- **Tool selection**: Claude decides tools implicitly. Could log confidence and route low-confidence selections to user confirmation.
- **Autonomy classification**: Use confidence to decide between "execute immediately" and "ask first."
- **Synthesis**: When the agent's answer is based on sparse data, flag it: "Low confidence: I only found 1 relevant source."

### 7.7 Artifact System

The frontend audit noted no export capability. The Telegram audit noted text-only output. The MCP audit noted no vault file management. Combined insight: the system needs an **artifact system** for structured outputs.

```python
@dataclass
class Artifact:
    type: str          # "note", "report", "chart", "email_draft", "task_list"
    title: str
    content: str       # Markdown, HTML, or base64 for images
    vault_path: str | None  # If saved to Obsidian
    telegram_message_id: int | None  # If sent via Telegram
    web_url: str | None  # If viewable in web UI
```

When the agent generates a long response, a comparison table, or a briefing document, it should create an artifact that can be:
- Sent as a Telegram document (bypassing the 4096 char limit)
- Saved to the Obsidian vault
- Viewed in the web UI
- Referenced in future conversations

This bridges the gap between the text-only Telegram interface and the rich web frontend.

---

## Summary: Priority Roadmap

### Phase 1: Foundation (Weeks 1-2)
1. **SQLite task queue** with TaskRecord schema
2. **Worker pool** allowing 2+ concurrent Claude Code sessions
3. **Inject memories into agent system prompt**
4. **Telegram inline keyboards** for Level 3 confirmations
5. **Complete reminder agent tool** (add update/delete)

### Phase 2: Intelligence (Weeks 3-4, hardware-dependent)
6. **GPU embeddings** (device="cuda")
7. **Local 70B model** for routing + simple synthesis
8. **Voice message support** (Whisper on GPU)
9. **Autonomy classifier** (5-level system)
10. **Proactive morning brief** (prompt-type reminder with rich prompt)

### Phase 3: Scale (Weeks 5-8)
11. **Unified tool surface** (agent tools = MCP tools)
12. **Write tools**: calendar create, CRM update, vault file management
13. **Artifact system** for rich outputs
14. **Image input** (Telegram photos -> local vision model)
15. **Event-driven sync** (Gmail push, calendar webhooks)

### Phase 4: Autonomy (Weeks 9-12)
16. **Proactive agent** with full trigger library
17. **Observation layer** for user model building
18. **Cross-channel intelligence** (real-time event processing)
19. **Multi-agent orchestration** (coordinator decomposes complex tasks)
20. **Conversation-as-workspace** (persistent execution contexts)

The agentic pipeline is the core of the "do anything" vision. The current implementation is impressively well-engineered -- the agent loop with parallel tools, the Claude Code orchestrator with plan mode, and the prompt-type reminders are genuine strengths. The transformation requires: concurrent execution (task queue), richer I/O (voice, images, artifacts), persistent intelligence (memory, proactive triggers), and local compute (GPU models for cost-free routine operations). Each phase builds on the last, and the system remains functional throughout.
