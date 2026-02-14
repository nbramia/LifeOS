# LifeOS: The Definitive Vision (v2)

*Synthesized from 13 audit documents across 3 rounds of analysis.*
*Revised with owner feedback — February 2026*

---

## Executive Summary

LifeOS is a remarkably capable self-hosted AI assistant. In roughly a year of solo development, it has grown to 120+ API endpoints, 75 services, a hybrid search pipeline (vector + BM25 + cross-encoder reranking), a powerful agentic Telegram interface, an MCP server for Claude Desktop/Code, and a CRM that tracks relationships across emails, messages, calendar, and notes. It works. People would pay for this.

But it has grown organically, and the cracks are showing. Route files span thousands of lines. PersonEntity data lives in a single JSON file that could corrupt. The launchd service references an app bundle that doesn't exist. There are no backups. Two competing chat dispatch systems coexist. The MCP server exposes only a fraction of available endpoints. And every long-running operation blocks the request thread because there is no task queue.

The good news: none of these problems are architectural dead ends. They are the natural debt of building something real, fast, alone. The path forward is clear — and most of the highest-impact fixes are surprisingly small.

**The thesis of this document:** LifeOS doesn't need a rewrite. It needs focused, surgical improvements in the right order. A few weeks of disciplined work addresses the critical infrastructure gaps. A few months transforms it into something that feels like a personal JARVIS — an assistant that knows your world, anticipates your needs, and can execute on your behalf through any interface.

---

## Where LifeOS Stands Today

### What Works Well

**The search pipeline is genuinely sophisticated.** Hybrid retrieval (ChromaDB vectors + BM25 keyword scoring), cross-encoder reranking, Reciprocal Rank Fusion, query expansion, source diversification — this is production-grade information retrieval. Most commercial products don't do this well.

**The agentic Telegram pipeline is the crown jewel.** 12 tools, up to 5 rounds of autonomous tool use, parallel execution via `asyncio.gather`, streaming responses with typing indicators. When you ask it a compound question like "When's my next meeting with Sarah and what did we discuss last time?", it fires off parallel searches, synthesizes the results, and responds conversationally. This is the interaction model that makes LifeOS feel alive.

**The data model is sound.** The two-tier SourceEntity → PersonEntity architecture cleanly separates raw observations from canonical records. Entity resolution across emails, phone numbers, and name variants works. The ChromaDB + SQLite hybrid gives you both semantic and structured queries.

**Prompt-type reminders are brilliant.** Scheduling a natural language prompt that runs through the full chat pipeline at a specified time — morning briefings, pre-meeting prep, end-of-day reviews — is a genuinely novel capability that most AI assistants lack entirely.

**The sync infrastructure covers real ground.** Gmail, Google Calendar, Google Drive, iMessage, phone calls, FaceTime, Apple Contacts, Obsidian vault, Slack, WhatsApp, LinkedIn, Monarch Money — 12+ data sources with incremental sync, health tracking, and rate limiting.

### What Needs Work

**Infrastructure basics are missing.** No backups (the backup directory doesn't even exist). The launchd plist references `LifeOS.app` which was never created. `server.log` grows unbounded (20MB+ and counting). No external health monitoring. A power failure or disk issue could lose everything.

**The monolith is getting heavy.** `crm.py` is 5,670 lines. `chat.py` is 1,800 lines. `index.html` duplicates the entire CRM. Route files mix HTTP handling, business logic, and data access. This isn't fatal, but it's slowing down every change.

**PersonEntity lives in JSON.** A single `person_entities.json` file stores all canonical people records. No transactions, no concurrent access safety, no partial updates. One bad write corrupts everything. This is the single scariest data integrity risk.

**No task queue.** Every operation — sync, reindex, embedding generation — runs synchronously in the request thread. Long operations block the API. There's no way to track progress, retry failures, or run things in the background. This is the universal bottleneck that touches every subsystem.

**Two chat pipelines compete.** The legacy intent classifier dispatches to handler functions. The new agentic loop gives Claude tools and lets it decide. Both exist, creating confusion about which path handles what. The agentic pipeline is clearly the future.

**MCP coverage gaps remain.** While 48 curated tools exist (more than the initial audit estimated), key write operations are missing: person profile updates, reminder updates, sync triggers, and fact management. The read-to-write ratio still skews heavily toward read-only.

**The reminder system is close but not there.** Prompt-type reminders can run through the chat pipeline, but they need to be as powerful as a direct Telegram message — full multi-round tool use, web lookups, MCP-level capabilities. A morning briefing that checks 10 different data sources and does web lookups should work just as reliably as typing the same request into Telegram.

### By the Numbers

| Metric | Value |
|--------|-------|
| API Endpoints | ~156 |
| Service Files | 75 |
| Route Files | 12 (largest: 5,670 lines) |
| Frontend Files | 3 HTML (~25,000 lines total) |
| MCP Tools | 48 (curated) |
| Agentic Tools | 12 |
| Data Sources Synced | 12+ |
| Data Store Size | ~2.3 GB |
| ChromaDB Collections | 8 |
| Test Coverage | Unit tests exist; integration/E2E sparse |

---

## The Vision: What LifeOS Becomes

LifeOS becomes the operating system for your life — not in the grandiose "replace everything" sense, but in the Unix sense: a reliable, composable layer that connects your information, understands your context, and acts on your behalf.

### The Day-in-the-Life Experience

**7:00 AM** — Your phone buzzes with a Telegram message. Not a generic "good morning." LifeOS has checked your calendar, scanned overnight emails, reviewed your task list, checked the weather, and composed a briefing: "You have 4 meetings today. The 10 AM with the design team — Sarah shared updated mockups last night that you haven't seen. Your 2 PM is with David, who you haven't spoken to in 3 weeks; last time you discussed the API migration timeline. The insurance renewal you've been putting off is due Friday. Want me to draft that email?"

**9:45 AM** — Fifteen minutes before your meeting, a notification appears: "Pre-meeting prep for Design Review: Sarah's mockups are in the shared Drive folder. Last meeting's action items were: finalize color palette (done), review mobile layouts (pending). Alex mentioned concerns about accessibility in Slack yesterday."

**12:30 PM** — You voice-message LifeOS while walking: "Remind me to follow up with the contractor about the kitchen estimate." It transcribes, creates the task, and sets a contextual reminder for tomorrow morning.

**6:00 PM** — "Your day in review: 3 of 4 meetings completed, 2 new tasks created, 5 tasks completed. You missed replying to Mom's text from this morning. Tomorrow looks lighter — good time to tackle the insurance renewal."

### The Core Principles

1. **Context is everything.** LifeOS knows who you're meeting, what you discussed, what's pending, and what matters. Every interaction is informed by your full context.

2. **Proactive, not just reactive.** The shift from "answer when asked" to "surface what matters before you ask" is the difference between a search engine and an assistant.

3. **Any interface, same brain.** Telegram, web, Claude Desktop, Claude Code — same data, same intelligence, same capabilities. The interface adapts; the understanding persists.

4. **Execute, don't just inform.** "Draft that email." "Create that task." "Schedule that meeting." LifeOS doesn't just tell you what to do — it does it, with appropriate confirmation.

5. **Privacy by architecture.** Self-hosted, local-first, your data stays on your hardware. The only external dependency is Claude API for synthesis (and eventually, local LLMs reduce even that).

---

## The Highest-Impact Improvements

Ranked by the intersection of effort, impact, and risk reduction. Each includes a reality check from the devil's advocate analysis.

### 1. Fix Infrastructure Basics (3 days)

**What:** Enable SQLite WAL mode everywhere. Create automated backups with rotation. Fix the launchd plist. Add log rotation. Set up basic health monitoring.

**Why:** This is existential. Without backups, a single disk failure or bad write loses everything. WAL mode prevents database locks during concurrent reads. Log rotation prevents disk fill. These aren't features — they're table stakes.

**Devil's Advocate:** "This is obvious. Just do it. Stop building features until this is done."

**Agreed.** This is Priority Zero.

**Relevant audit docs:** `archive/audit-infrastructure.md` (launchd, backup, log details), `archive/audit-round2-backend.md` (WAL mode analysis)

### 2. Migrate PersonEntity from JSON to SQLite (3 days)

**What:** Move `person_entities.json` to a SQLite table with proper transactions, concurrent access, and partial updates. Keep the JSON export as a backup/migration tool.

**Why:** The JSON file is the single biggest data integrity risk. Every write replaces the entire file. No transactions. No recovery from partial writes. One corruption event and you lose your entire people graph.

**Devil's Advocate:** "The migration itself is straightforward — it's the 75 service files that read/write PersonEntity that make this scary. Do it carefully, with a rollback plan, and a JSON snapshot before migrating."

**Approach:** Write the SQLite schema, build a migration script, update the PersonEntityManager to use SQLite, keep `to_json()` for exports. Test with a copy of production data.

**Relevant audit docs:** `archive/audit-backend.md` (PersonEntity architecture), `archive/audit-round2-backend.md` (migration risk analysis)

### 3. Add a Task Queue (1-2 weeks)

**What:** Background job processing for sync operations, reindexing, embedding generation, and any operation that takes more than a few seconds.

**Why:** This is the universal bottleneck. Every sync blocks the API. Reindexing blocks everything. Embedding generation for large documents blocks. Without async job processing, LifeOS can't grow.

**Devil's Advocate:** "Everyone underestimates task queue complexity by 3x. Redis + Dramatiq adds a new process to keep alive, a new failure mode, and a new thing to monitor. For a single-user system, consider SQLite-backed queues first. You don't need Redis until you need Redis."

**Revised approach:** Start with a SQLite-backed queue (e.g., `sqlite-worker` pattern or a simple polling loop with a `jobs` table). Status tracking, retry logic, progress reporting. Upgrade to Redis + Dramatiq only if SQLite proves insufficient. For a single-user system, it probably won't.

**Relevant audit docs:** `archive/audit-infrastructure.md` (current sync architecture), `archive/audit-round2-infra.md` (task queue design), `archive/audit-round3-devils-advocate.md` (complexity warnings)

### 4. Unify the Chat Pipeline (1 week)

**What:** Consolidate the legacy intent classifier and the agentic loop into a single pipeline. The agentic loop is the future — the intent classifier becomes a fast-path optimization within it, not a separate system.

**Why:** Two dispatch systems means two places to add features, two sets of bugs, and confusion about which handles what. The `compose` intent currently bypasses the agentic pipeline entirely, losing access to all tools.

**Devil's Advocate:** "The agentic pipeline is clearly better. But the intent classifier is fast and cheap for simple queries. Don't throw it away — let the agentic pipeline use classification as a tool to decide whether it needs tools at all."

**Approach:** Make the agentic loop the single entry point. Use Ollama-based classification as the first step within the loop to determine complexity. Simple queries get direct Claude responses. Complex queries get tools. This preserves the speed advantage while eliminating the dual-system confusion.

**Relevant audit docs:** `archive/audit-telegram-chat.md` (full pipeline analysis), `archive/audit-round2-telegram.md` (unified pipeline design)

### 5. Expand MCP Tool Coverage

**What:** Fill the remaining gaps in MCP tool coverage, focusing on write operations and tools that exist in the Telegram agent but not in MCP. Each sub-item below has its own PRD with specific implementation details, success criteria, and test coverage.

**Why:** MCP is how Claude Desktop and Claude Code interact with LifeOS. The curated 48-tool set covers reads well, but key write operations and cross-agent tool parity are missing. Adding these transforms MCP from a search interface to a control plane.

**Devil's Advocate:** "Don't blindly expose every endpoint. Each MCP tool is an attack surface and a maintenance burden. Prioritize by what Claude Code actually needs for daily workflows."

**Note:** The initial audit reported 35 MCP tools at 22% coverage. The actual count is 48 curated tools — calendar CRUD, task CRUD, and reminder create/list/delete already exist. The sub-items below cover what's genuinely missing.

#### 5.2 — `lifeos_person_update`: Update Person Profiles via MCP

The PATCH endpoint exists at `api/routes/crm.py:748` but is not exposed via MCP. Claude can look up a person but cannot update notes, tags, category, or birthday.

**PRD:** [`prd-mcp-update-person.md`](prd-mcp-update-person.md)
**Effort:** 1-2 hours (MCP tool wiring + PATCH support in `_call_api`)

#### 5.3 — `lifeos_reminder_update`: Update Reminders via MCP

Create, list, and delete exist in MCP. Update does not, despite the PUT endpoint existing at `api/routes/reminders.py:148`. Users must delete and recreate to change a reminder.

**PRD:** [`prd-mcp-reminder-update.md`](prd-mcp-reminder-update.md)
**Effort:** 1-2 hours (MCP tool wiring)

#### 5.4 — `lifeos_sync_trigger`: Trigger Data Sync via MCP

Multiple sync endpoints exist but none are exposed via MCP. Claude Code cannot trigger a reindex after editing vault files or request a calendar refresh.

**PRD:** [`prd-mcp-trigger-sync.md`](prd-mcp-trigger-sync.md)
**Effort:** 2-4 hours (unified routing handler or new API endpoint)

#### 5.5 — Person Facts CRUD: Update, Confirm, Delete Facts via MCP

`lifeos_person_facts` (read) exists. The write endpoints exist in the API. Three new tools complete the CRUD cycle for fact management.

**PRD:** [`prd-mcp-person-facts.md`](prd-mcp-person-facts.md)
**Effort:** 2-3 hours (three tool wirings + DELETE support in `_call_api`)

#### 5.6 — Fix `lifeos_health` Formatter

The existing health tool discards per-service detail, returning only "healthy/degraded." The underlying API provides rich per-service status with degradation events.

**PRD:** [`prd-mcp-health-detailed.md`](prd-mcp-health-detailed.md)
**Effort:** 1 hour (formatter fix)

#### 5.7 — Fix `_call_api` HTTP Method Support

The MCP server's `_call_api` method doesn't explicitly handle PATCH, PUT, or DELETE — they work by accident via the POST fallback. This should be made explicit before adding write tools.

**Effort:** 30 minutes (prerequisite for 5.2, 5.3, 5.5)
**Implementation:** Add explicit `elif method == "PATCH"`, `elif method == "PUT"`, `elif method == "DELETE"` branches in `_call_api`.

**Total effort for all of #5:** ~2-3 days

### 6. Full-Agentic Reminder Pipeline (1-2 weeks)

**What:** Make the reminder system as powerful as a direct Telegram message. When a prompt-type reminder fires, it should have access to the full agentic pipeline — multi-round tool use, web lookups, all MCP-level capabilities, parallel tool execution. A morning briefing that needs to check calendar, search emails, look up weather, query tasks, and synthesize everything should work exactly as reliably as typing that request into Telegram.

**Why:** Prompt-type reminders are the foundation for proactive intelligence. They're already the mechanism for morning briefings, pre-meeting prep, and scheduled digests. But if they can't do multi-step tool use, web searches, or complex synthesis, they're limited to simple lookups. The gap between "what I can ask Telegram to do" and "what a reminder can do" should be zero.

**Current state:** Prompt-type reminders call `chat_via_api()` which goes through the chat pipeline. But this path may not support the same multi-round tool use, streaming, or tool breadth as a direct Telegram message. The goal is full parity.

**Approach:**
1. Audit the `chat_via_api()` path vs the direct Telegram message path. Identify every capability gap.
2. Ensure prompt-type reminders go through the identical agentic loop with the same tools, the same number of reasoning rounds, and the same parallel execution.
3. Add error handling and retry logic for reminder execution — if a tool call fails mid-briefing, the reminder should retry or send a partial result, not silently fail.
4. Add execution logging so you can see what a reminder did, which tools it called, and how long it took.
5. Test with a complex morning briefing that requires 5+ tool calls across different data sources.

**Success criteria:**
- A prompt-type reminder can do anything a direct Telegram message can do.
- A morning briefing reminder that says "Check my calendar, check my email for anything urgent, look up today's weather in [city], check my overdue tasks, and check if any bills are due this week" executes successfully with all data sources.
- Reminder execution is logged with tool calls and timing.
- Failed tool calls within a reminder don't cause silent failure.

**Relevant audit docs:** `audit-telegram-chat.md` (agentic pipeline), `audit-round2-telegram.md` (unified pipeline design)

### 7. Agent Memory and Conversation Context (1 week)

**What:** Give the agentic pipeline persistent memory across conversations. User preferences, past decisions, learned context, established facts. Not chat history — distilled knowledge.

**Why:** Every Telegram conversation starts from zero. LifeOS knows your data but forgets that you prefer morning meetings, that "the project" means the kitchen renovation, or that you already decided to use Dramatiq over Celery. Persistent memory makes interactions compound over time.

**Devil's Advocate:** "Memory is deceptively hard. Unbounded memory becomes noise. Stale memory becomes lies. Start with explicit user-triggered memories ('remember that I prefer...') and simple fact extraction from conversations. Don't try to build automatic memory synthesis yet."

**Approach:** A `memories` table in SQLite. Explicit save via "remember that..." commands. Retrieval via semantic search over memory embeddings. Injected into Claude's system prompt as relevant context. Simple, bounded, user-controlled.

**Relevant audit docs:** `archive/audit-round2-telegram.md` (context & memory analysis), `archive/audit-round3-blue-sky.md` (knowledge amplification)

### 8. Proactive Intelligence Service (2-3 weeks)

**What:** A background service that monitors your data and surfaces insights without being asked. Communication gap detection, meeting prep, deadline warnings, relationship maintenance nudges.

**Why:** This is the leap from "assistant that answers" to "assistant that thinks." But the bar for unsolicited outreach must be high — every proactive notification must be clearly valuable and actionable, not noise.

**Devil's Advocate:** "The concept is brilliant. The execution risk is high. Start with scheduled prompts (which already work via prompt-type reminders) and hardcoded intelligence rules. Don't build an 'insight engine' — build specific, valuable notifications. Communication gaps > 14 days. Meetings in the next hour. Tasks overdue. Start there."

**Design principles for proactive outreach:**
- **High signal, low noise.** Every notification must pass the test: "Would I be annoyed if this woke me up?" If yes, don't send it.
- **Specific and actionable.** Not "You have meetings today" but "Your 2 PM with David — you haven't spoken in 3 weeks, last topic was the API migration."
- **Batched, not spammed.** Group notifications into daily digests (morning/evening) rather than sending individual alerts throughout the day. Exception: truly time-sensitive items (meeting starting in 15 minutes).
- **User-configurable.** Easy to turn off categories, adjust thresholds, change delivery times.

**Approach:** Build on top of the full-agentic reminder pipeline (#6). Each intelligence module is a prompt-type reminder with a specific, well-crafted prompt:
1. Pre-meeting prep (15 min before meetings with rich context)
2. Communication gap nudges (weekly batch, configurable threshold)
3. Task deadline warnings (morning briefing inclusion)
4. Weekly relationship digest

Each module is a function that queries existing data and formats a notification. No ML, no prediction models — just smart queries over rich data.

**Important:** This item depends on #6 (Full-Agentic Reminder Pipeline). The proactive intelligence modules are only as capable as the reminder execution engine.

**Relevant audit docs:** `archive/audit-round3-blue-sky.md` (predictive intelligence), `archive/audit-round3-devils-advocate.md` (over-engineering warnings)

---

## Deferred Items

These were in the original top 10 but are deferred for specific reasons.

### Telegram Voice and Inline Keyboards — Not Needed

**Reason:** Native voice transcription already exists on phone and computer, usable with Telegram directly. No need to rebuild this natively. Inline keyboards can be revisited later if the "type yes to confirm" pattern proves genuinely painful.

### Tasks Web Page — Using Obsidian

**Reason:** Task management works well within Obsidian today. Deprioritized in favor of higher-impact improvements. Can be revisited if the Obsidian workflow becomes limiting.

### Hardware Migration — Planned Separately

**Reason:** The Corsair AI Workstation 300 isn't arriving for 1-2 weeks. The migration will be planned and executed as its own project, independent of the software improvements above. All software improvements should be done on current hardware first — they'll carry over cleanly.

---

## The Critical Path

The ordering matters. Each phase builds on the previous one. Phases 2a/2b/2c can run in parallel because they touch different files. Everything else is sequential.

**Orchestration plan:** See `audit-implementation-plan.md` for full orchestration details, agent prompts, and state tracking.

### Phase 0: Stop the Bleeding — Improvement #1

*Before anything else, make what exists safe.*

**Agent prompt:** `audit-phase0-prompt.md`

- [ ] Enable SQLite WAL mode on all databases
- [ ] Create automated backup script with rotation (daily, keep 7)
- [ ] Fix launchd plist (remove LifeOS.app reference, test restart)
- [ ] Add log rotation for server.log and error.log
- [ ] Verify backup restores actually work

**Exit criteria:** Backups run nightly and restore successfully. Server survives a reboot. Logs don't fill the disk.

### Phase 1: Data Integrity — Improvement #2

*Protect the most valuable data.*

**Agent prompt:** `audit-phase1-prompt.md`

- [ ] Design PersonEntity SQLite schema
- [ ] Build migration script with rollback capability
- [ ] Snapshot current JSON as backup
- [ ] Migrate PersonEntityManager to SQLite
- [ ] Update all PersonEntity consumers (test thoroughly)
- [ ] Verify no data loss via comparison

**Exit criteria:** PersonEntity operations are transactional. Concurrent access is safe. JSON export still works as a backup mechanism.

### Phase 2: Pipeline, Tools, Memory — Improvements #4, #5, #7 (parallel)

*Unify the brain. Expand the hands. Add memory. Three agents, three workstreams, no file conflicts.*

**Phase 2a — Chat Pipeline Unification (#4)**
**Agent prompt:** `audit-phase2a-prompt.md`
**Files owned:** `chat.py`, `telegram_handler.py`, `intent_classifier.py`
- [ ] Make agentic loop the single entry point
- [ ] Consolidate `compose` intent into agentic pipeline
- [ ] Preserve fast-path for simple queries

**Phase 2b — MCP Tool Coverage (#5)**
**Agent prompt:** `audit-phase2b-prompt.md`
**Files owned:** `mcp_server.py`
**PRDs:** `prd-mcp-*.md` (5 documents)
- [ ] Fix `_call_api` HTTP method support (5.7 — prerequisite)
- [ ] Add `lifeos_person_update` (5.2)
- [ ] Add `lifeos_reminder_update` (5.3)
- [ ] Add `lifeos_sync_trigger` (5.4)
- [ ] Add person facts CRUD (5.5)
- [ ] Fix `lifeos_health` formatter (5.6)

**Phase 2c — Agent Memory (#7)**
**Agent prompt:** `audit-phase2c-prompt.md`
**Files owned:** `agent_tools.py`, new memory service
- [ ] Create memories table and service
- [ ] Add explicit save ("remember that...")
- [ ] Add semantic retrieval
- [ ] Inject relevant memories into agent system prompt

**Exit criteria:** Single chat pipeline. MCP write tools functional. Agent remembers across conversations.

### Phase 3: Background Processing — Improvement #3

*The structural work that enables everything after.*

**Agent prompt:** `audit-phase3-prompt.md`

- [ ] Design and create SQLite-backed job queue
- [ ] Build background worker (polling loop)
- [ ] Migrate reindex and manual sync triggers to background jobs
- [ ] Add job status tracking API

**Exit criteria:** No sync operation blocks the API. Job status is queryable.

### Phase 4a: Reminder Pipeline Parity — Improvement #6

*Make reminders as powerful as direct messages.*

**Agent prompt:** `audit-phase4a-prompt.md`
**Depends on:** Phase 2a (unified pipeline), Phase 3 (task queue)

- [ ] Audit reminder execution path vs direct Telegram path
- [ ] Close every capability gap
- [ ] Add error handling, retry logic, and partial result delivery
- [ ] Add execution logging (tools called, timing, errors)
- [ ] Test with complex multi-tool briefings

**Exit criteria:** Prompt-type reminders can do anything a direct Telegram message can.

### Phase 4b: Proactive Intelligence — Improvement #8

*Build on the hardened reminder pipeline.*

**Agent prompt:** `audit-phase4b-prompt.md`
**Depends on:** Phase 4a (full-agentic reminders)

- [ ] Pre-meeting prep module (15 min before meetings)
- [ ] Morning briefing template (well-crafted prompt)
- [ ] Communication gap nudges (weekly batch)
- [ ] Task deadline warnings (in morning briefing)

**Exit criteria:** Proactive notifications are high-signal and actionable. Each module is a prompt-type reminder.

### Future: Polish (Ongoing)

*Revisit when the core is solid.*

- [ ] Frontend design system (shared components, consistent styling)
- [ ] Admin/settings web page
- [ ] Advanced memory (automatic fact extraction, conversation summarization)
- [ ] WhatsApp and Slack deep integration
- [ ] End-to-end test suite

---

## Quick Wins (Do This Week)

These can each be done in hours, not days. They require no architectural changes.

1. **Enable WAL mode** — Add `PRAGMA journal_mode=WAL` to SQLite connection initialization. One line. Eliminates database lock errors during concurrent reads.

2. **Create backup script** — `rsync` the data directory to an external location nightly. Add to cron. 30 minutes including testing.

3. **Fix launchd plist** — Remove the `LifeOS.app` bundle reference. Point directly to the server script. Test `launchctl load/unload`. 15 minutes.

4. **Add log rotation** — Use `newsyslog` or `logrotate` config. Cap `server.log` at 10MB with 5 rotations. 15 minutes.

5. **Consolidate `compose` intent** — Route compose requests through the agentic pipeline instead of the legacy handler. The agentic pipeline already has email drafting tools. 2-3 hours.

---

## Architecture Evolution

### From Monolith to Modular Monolith

LifeOS should NOT become microservices. It's a single-user application. But the current monolith needs internal structure.

**Current state:** Route files contain business logic, data access, and HTTP handling interleaved. `crm.py` at 5,670 lines is the symptom.

**Target state:** A modular monolith with clear layers:

```
Routes (HTTP handling, request/response)
  → Services (business logic, orchestration)
    → Repositories (data access, queries)
      → Models (data structures, validation)
```

**How to get there incrementally:**
- Don't rewrite. Extract as you touch.
- When fixing a bug in `crm.py`, extract the relevant functions into a service.
- When adding a feature, write it in the new pattern.
- Over 6 months, the monolith naturally modularizes.

### The Unified Pipeline

```
Any Client (Telegram, Web, MCP, CLI, Reminder)
  → Agentic Pipeline
    → Fast Classification (local LLM: general/personal/action/web)
    → Tool Selection (Claude decides which tools to use)
    → Tool Execution (parallel where possible)
    → Synthesis (Claude composes final response)
    → Response (formatted for the originating client)
```

All clients share the same pipeline. The only difference is input format (text, voice, structured) and output format (Telegram markdown, HTML, MCP response). **Critically, reminders are first-class clients of this pipeline** — a prompt-type reminder has the same capabilities as a direct Telegram message.

### Data Flow

```
External Sources          Sync Layer              Core Storage
─────────────────    ──────────────────    ─────────────────────
Gmail              → Gmail Sync Agent   → SourceEntity (SQLite)
Google Calendar    → Calendar Sync      → ChromaDB (vectors)
iMessage           → FDA Sync Agent     → PersonEntity (SQLite)
Obsidian Vault     → Vault Watcher      → BM25 Index
Slack              → Slack Sync         → Relationship Graph
WhatsApp           → WhatsApp Sync      →
Monarch Money      → Monarch Sync       →
...                                     →

                    Task Queue (SQLite-backed)
                    ─────────────────────────
                    Handles: sync jobs, reindex, embeddings,
                    notifications, scheduled intelligence
```

---

## What To NOT Build

The devil's advocate analysis was invaluable here. Roughly half of the proposals from the blue-sky and cross-pollination rounds were identified as over-engineered for a single-user system. Heed these warnings.

### 1. Don't Build Event-Driven Architecture

Pub/sub, event buses, event sourcing — these solve distributed system problems you don't have. A single-user app with one database doesn't need eventual consistency. Direct function calls are simpler, faster, and debuggable. If you need "something happens after X," just call the function or enqueue a job.

### 2. Don't Build a Plugin System

Dynamic tool loading, sandboxed execution, a plugin marketplace — this is months of work for an audience of one. Hard-code your tools. When you need a new one, add it to the codebase. A plugin system is a premature abstraction.

### 3. Don't Add Docker/Kubernetes

For a single-user app on known hardware, containers add complexity without value. You know the OS, you know the dependencies, you control the environment. A virtualenv and a systemd/launchd service are sufficient. Docker is justified only if you plan to distribute LifeOS to other users — and that's a different product.

### 4. Don't Add Prometheus/Grafana

An observability stack for one user's personal assistant is resume-driven development. SQLite tables for health metrics, a simple web dashboard, and Telegram alerts cover your monitoring needs. If you're staring at Grafana dashboards for your personal assistant, something has gone wrong.

### 5. Don't Build Real-Time Sync

File watchers, webhooks, push notifications from every data source — the complexity explodes and the value is marginal. Your email doesn't need to be indexed within seconds. Batch sync every 3 hours (or on-demand trigger) is sufficient. Optimize frequency per source based on actual need.

### 6. Don't Build Multi-User Support

Authentication, authorization, tenant isolation, shared vs. private data — this is a different product. LifeOS is a personal system. If you want to share it, fork it. Don't architect for multi-tenancy.

### 7. Don't Build a Custom Frontend Framework

Don't create a design system, component library, or build pipeline for three HTML pages. If the vanilla JS approach becomes truly untenable, adopt a lightweight framework (Alpine.js, htmx) — don't build one.

### 8. Don't Over-Invest in Autonomy Levels

A 5-tier autonomy system (inform → suggest → confirm → auto-reversible → full auto) sounds elegant but is over-engineered. Two levels work: "do it and tell me" (for safe operations like search and draft) and "ask first" (for anything that sends, deletes, or modifies). Keep it binary.

### 9. Don't Rebuild Native Voice Transcription

Phone and computer already have excellent voice transcription that works with Telegram. Building native Whisper integration is unnecessary until the hardware migration makes it free.

---

## Open Questions

These are genuine decisions that need user input, not problems with obvious solutions.

### 1. Frontend Strategy: Vanilla JS or Framework?

The current vanilla JS approach works but doesn't scale well to interactive features (task boards, drag-and-drop, real-time updates). Options:
- **Stay vanilla** — Simpler, no build step, matches existing code. But increasingly painful for complex UI.
- **htmx + Alpine.js** — Server-rendered with progressive enhancement. Minimal JS, no build step. Good fit for the current architecture.
- **React/Vue/Svelte** — Full SPA capability. Requires a build step, introduces toolchain complexity, but enables rich interactivity.

**Recommendation:** htmx + Alpine.js for the next phase. Revisit if you need complex client-side state.

### 2. Local LLM: How Much to Offload?

With the workstation's GPU, you could route many queries locally and reduce Claude API costs. But local models are worse at complex synthesis. Where's the line?

**Recommendation:** Use local LLM for classification, simple Q&A over retrieved data, and formatting. Keep Claude for complex synthesis, multi-step reasoning, and tool orchestration. Measure API costs to calibrate.

### 3. Sync Strategy: Batch vs. Event-Driven per Source?

Some sources benefit from near-real-time sync (calendar changes), while others are fine with batch (old emails). Should sync frequency be per-source configurable?

**Recommendation:** Yes, but keep it simple. A cron-like schedule per source (every 30 min for calendar, every 3 hours for email, daily for vault). No file watchers or webhooks unless a specific source proves to need it.

### 4. Memory Architecture: Explicit vs. Automatic?

Explicit memory ("remember that...") is safe and predictable. Automatic memory (extracting facts from every conversation) is more powerful but risks noise, staleness, and privacy surprises.

**Recommendation:** Start explicit. Add selective automatic extraction later (e.g., extract facts about people from conversations, with user review/confirmation).

---

## Final Thoughts

LifeOS is closer to the vision than it might feel in the middle of debugging launchd plists and staring at 5,000-line route files. The hard problems — data model, search pipeline, agentic reasoning, multi-source sync — are already solved. What remains is mostly infrastructure hardening, interface expansion, and a reminder pipeline powerful enough to be the foundation for proactive intelligence.

The three most important things to internalize:

1. **Fix infrastructure before building features.** Backups, WAL mode, log rotation, launchd — do these first. They're boring but existential.

2. **The agentic pipeline is the future.** Every investment in making the tool-using, parallel-executing, streaming agentic loop better pays dividends across every interface — including reminders.

3. **Resist the Second System Effect.** The devil's advocate was right: roughly half of the proposed improvements are over-engineered. LifeOS doesn't need event buses, plugin systems, or Kubernetes. It needs focused execution on a small number of high-impact improvements.

A few weeks of disciplined work makes LifeOS reliable. A few months makes it indispensable. The path is clear.
