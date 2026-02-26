# LifeOS Implementation Roadmap

**Author:** Claude Opus 4.6 (Roadmap Architect)
**Date:** 2026-02-13
**Input:** 10 audit documents (5 Round 1 + 5 Round 2 cross-pollination), codebase review
**Goal:** Turn audit findings into a practical, sequenced implementation plan

---

## Executive Summary

LifeOS is a remarkably complete system -- 120+ API endpoints, 75 service files, 20 sync sources, a powerful agentic pipeline, and a deep CRM. The audits reveal that the *application logic* is strong. What limits the system is *infrastructure*: no task queue, no concurrent execution, no GPU acceleration, batch-only sync, and missing write capabilities.

This roadmap sequences improvements to maximize value at each step. The critical path to "do anything via Telegram" runs through: task queue -> write tools -> voice input -> proactive intelligence.

**Key principle:** Every phase leaves the system fully functional. No big-bang rewrites.

---

## Phase 0: Hardware Migration (Days 1-3)

### Context

Moving from Mac Mini to Corsair AI Workstation 300. This is the foundation for everything that follows. The Mac Mini stays as a data-collection node for macOS-only sources (iMessage, Apple Photos, Phone calls via FDA).

### Items

#### 0.1 Pre-Migration Preparation (Before Hardware Arrives)
- **What:** Export all databases, config files, Google OAuth tokens. Create database dump scripts. Document every `.env` variable and config file path.
- **Why:** Smooth migration requires knowing exactly what to transfer. Google OAuth tokens may need re-authentication on the new machine.
- **Effort:** 4 hours
- **Impact:** 9/10 (migration fails without this)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Partially. AI can write export scripts and document config, but human needs to verify credentials work.

#### 0.2 Workstation OS and GPU Setup
- **What:** Install Ubuntu 24.04 LTS, NVIDIA drivers, CUDA toolkit. Verify GPU is accessible.
- **Why:** Linux has the best CUDA support and systemd for reliable process management. Ubuntu 24.04 LTS is the most tested distro for NVIDIA tooling.
- **Effort:** 4 hours (mostly waiting for downloads/installs)
- **Impact:** 10/10 (nothing works without this)
- **Risk:** Low (well-documented process)
- **Dependencies:** Hardware arrival
- **AI-assistable:** AI can provide step-by-step commands. Human executes.

#### 0.3 Deploy LifeOS on Workstation
- **What:** Clone repo, set up Python venv, install dependencies, restore databases from Mac Mini backups, configure `.env`, start server via `server.sh`, verify `/health/full` passes.
- **Why:** Get the existing system running on new hardware before making changes.
- **Effort:** 4-6 hours (including troubleshooting)
- **Impact:** 10/10
- **Risk:** Medium (Python version differences, missing system dependencies, Google OAuth re-auth may be needed)
- **Dependencies:** 0.1, 0.2
- **AI-assistable:** Yes. AI can troubleshoot errors. Human handles Google OAuth browser flow.

#### 0.4 DNS/Network Cutover
- **What:** Set up Tailscale on workstation (same tailnet). Update DNS or MagicDNS to point `lifeos` to workstation IP. Update Telegram bot polling to connect to workstation. Update MCP server config.
- **Why:** All clients (web UI, Telegram, Claude Code via MCP) need to talk to the new server.
- **Effort:** 2 hours
- **Impact:** 10/10
- **Risk:** Low (DNS-based cutover is instantly reversible by pointing back to Mac Mini)
- **Dependencies:** 0.3
- **AI-assistable:** Partially. AI can update config files.

#### 0.5 Mac Mini Transition to Sync Agent
- **What:** On Mac Mini: stop the API server. Keep only the FDA sync scripts (iMessage, Phone, Photos). Create a sync agent script that pushes data to the workstation API after local sync.
- **Why:** Mac Mini retains macOS-specific data access (FDA, Apple Photos). Workstation handles compute.
- **Effort:** 4-6 hours
- **Impact:** 7/10 (iMessage/Photos data continues flowing)
- **Risk:** Medium (sync agent needs error handling and health monitoring)
- **Dependencies:** 0.4
- **AI-assistable:** Yes. AI can write the sync agent.

#### 0.6 GPU-Accelerated Embeddings
- **What:** Change `device="cpu"` to `device="cuda"` in `api/services/embeddings.py`. Install CUDA-compatible PyTorch. Test with a vault reindex.
- **Why:** 10-50x speedup for embedding generation. Reindexing goes from hours to minutes. Highest ROI of any GPU change.
- **Effort:** 2 hours
- **Impact:** 8/10 (transforms reindex from painful to trivial)
- **Risk:** Low (config change + dependency install)
- **Dependencies:** 0.2, 0.3
- **AI-assistable:** Yes.

#### 0.7 Local LLM Model Download and Setup
- **What:** Install Ollama (or vLLM for production serving). Download Qwen 2.5 72B (4-bit quantized, ~40 GB). Test query routing with the larger model.
- **Why:** Replaces the 7B model with a dramatically more capable 72B model. Faster (GPU vs CPU), more accurate routing, enables local synthesis for simple queries.
- **Effort:** 4 hours (mostly download time)
- **Impact:** 7/10 (better routing accuracy, eliminates Haiku API fallback for classification)
- **Risk:** Low (drop-in replacement, same Ollama API)
- **Dependencies:** 0.2
- **AI-assistable:** Yes.

**Phase 0 Total Effort:** ~3 days
**Phase 0 Exit Criteria:** LifeOS running on workstation, all clients connected, Mac Mini as sync agent, GPU embeddings working, 72B model serving.

---

## Phase 1: Quick Wins (Weeks 1-2)

### Context

Changes that require minimal code but unlock significant value. Bug fixes, quality improvements, and configuration changes found in the audits.

### Items

#### 1.1 Enable SQLite WAL Mode on All Databases
- **What:** Add `PRAGMA journal_mode=WAL` to all SQLite database initializations (`crm.db`, `interactions.db`, `conversations.db`, `bm25_index.db`, `sync_health.db`, `usage.db`).
- **Why:** WAL allows concurrent readers during writes. Prerequisite for any parallel execution (task queue, parallel sync). Currently, the sync process writing to `crm.db` blocks all API reads.
- **Effort:** 1 hour
- **Impact:** 8/10 (enables all future parallelism)
- **Risk:** Low (one-line pragma, SQLite handles the rest)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.2 Fix launchd / Auto-Start
- **What:** On workstation: create systemd service files for LifeOS API, ChromaDB, and Ollama/vLLM. Enable auto-restart on failure. On Mac Mini (if still needed as backup): fix the broken plist by replacing the LifeOS.app reference with a direct Python invocation.
- **Why:** The API server currently has no reliable auto-start. The infra audit found the LifeOS.app binary doesn't exist, so launchd fails with exit code 78.
- **Effort:** 2 hours
- **Impact:** 9/10 (system reliability -- server survives reboots)
- **Risk:** Low
- **Dependencies:** Phase 0 (for workstation)
- **AI-assistable:** Yes.

#### 1.3 Create Backup Directory and Automated Database Backups
- **What:** Create `data/backups/` directory. Implement daily SQLite `.backup()` for `crm.db` (556 MB), `interactions.db` (260 MB), `conversations.db`. Keep last 7 daily backups. Add off-machine backup via rsync/rclone to cloud storage (B2 or S3).
- **Why:** The infra audit found the backup directory doesn't exist. 556 MB of irreplaceable CRM data has zero backup. This is the highest-severity data safety issue.
- **Effort:** 4 hours
- **Impact:** 10/10 (data safety)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.4 Add Log Rotation
- **What:** Configure Python's `RotatingFileHandler` in `main.py` for `server.log` (currently 20 MB and growing). Add cleanup script for timestamped sync logs older than 30 days.
- **Why:** server.log grows unbounded. Sync creates a new timestamped log file daily, never cleaned.
- **Effort:** 1 hour
- **Impact:** 5/10 (operational hygiene)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.5 Proper Markdown Rendering in Chat
- **What:** Replace the 6-line regex `formatContent()` in `web/index.html` (line ~3432) with marked.js + highlight.js for code blocks. The current function only handles `**bold**`, `*italic*`, backtick code, and `\n`. No headings, lists, tables, or code blocks.
- **Why:** This affects every AI response in the web UI. The biggest day-to-day UX gap identified by the frontend audit. An AI assistant that can't render its own markdown output properly.
- **Effort:** 3 hours
- **Impact:** 9/10 (every chat response looks better)
- **Risk:** Low (well-known libraries, CDN-loaded)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.6 Remove Embedded CRM from index.html
- **What:** Remove the ~2500 lines of duplicate CRM code from `web/index.html` (lines 2569-5267). Change the chat nav tab's CRM link to navigate to `/crm` (the standalone `crm.html`).
- **Why:** The frontend audit identified massive code duplication. The standalone CRM is far more capable. Two CRM implementations means two places to fix bugs.
- **Effort:** 2 hours
- **Impact:** 6/10 (code hygiene, removes confusion between two CRM views)
- **Risk:** Low (simple removal, no new functionality)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.7 Complete Reminder Agent Tool (Add Update/Delete)
- **What:** Add `update` and `delete` actions to the `manage_reminders` tool in `api/services/agent_tools.py`. Currently only supports `create` and `list`.
- **Why:** The Telegram agent can create reminders but can't modify or delete them. Users must work around this by asking the chat route's hardcoded handlers instead.
- **Effort:** 2 hours
- **Impact:** 6/10 (completes CRUD cycle for reminders via agent)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.8 Fix MCP Server PUT/PATCH Handling
- **What:** Add explicit PUT/PATCH handling in `mcp_server.py`'s `_call_api` method. Currently PUT/PATCH falls through to the else branch which happens to work but is fragile.
- **Why:** MCP audit identified this as a correctness issue. `lifeos_task_update` and future `lifeos_reminder_update` rely on PUT.
- **Effort:** 1 hour
- **Impact:** 4/10 (correctness fix)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.9 Add MCP Tool: lifeos_vault_read
- **What:** Create a new `GET /api/vault/file` endpoint. Extract the file-reading logic from `agent_tools.py:read_vault_file` into a shared service. Expose as MCP tool.
- **Why:** After `lifeos_search` finds relevant document chunks, agents need to read the full file. The Telegram agent has this capability internally but MCP doesn't. This is the most-cited MCP gap.
- **Effort:** 3 hours
- **Impact:** 7/10 (completes the search-then-read flow for MCP)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.10 Add MCP Tool: lifeos_person_update
- **What:** Add `lifeos_person_update` to the `CURATED_ENDPOINTS` list in `mcp_server.py`, mapping to the existing `PATCH /api/crm/people/{id}` endpoint.
- **Why:** Agents can read person profiles but cannot update notes, tags, categories, or birthdays. The endpoint exists; it just needs MCP exposure.
- **Effort:** 30 minutes
- **Impact:** 7/10 (enables "remember John's birthday is March 15" via MCP)
- **Risk:** Low
- **Dependencies:** 1.8 (PUT/PATCH fix)
- **AI-assistable:** Yes.

#### 1.11 Inject Persistent Memories into Agent System Prompt
- **What:** Modify `agent_system_prompt.py` to load the top 10 relevant memories from `memory_store` at the start of each agent loop. Match memories to the query/conversation topic.
- **Why:** The memory system exists (`lifeos_memories_create/search`) but is completely disconnected from the agent. The agent starts fresh every conversation, ignoring stored preferences.
- **Effort:** 3 hours
- **Impact:** 7/10 (makes the assistant smarter over time)
- **Risk:** Low (additive change to system prompt)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 1.12 Add Telegram Bot Command Menu
- **What:** Register commands with BotFather so users see suggestions when typing `/`: `/new`, `/code`, `/status`, `/tasks`, `/reminders`, `/help`.
- **Why:** Users currently have to remember commands. Telegram supports auto-complete menus.
- **Effort:** 30 minutes
- **Impact:** 4/10 (small UX improvement)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

**Phase 1 Total Effort:** ~2 weeks (many items are parallelizable)
**Phase 1 Exit Criteria:** WAL mode enabled, auto-start working, backups running, markdown rendering in chat, MCP has vault read + person update, agent has memories, Telegram has command menu.

---

## Phase 2: Foundation (Weeks 3-4)

### Context

The structural changes that enable everything in later phases. Task queue, unified pipeline, and write capabilities.

### Items

#### 2.1 Implement Task Queue (Dramatiq + Redis)
- **What:** Add Redis as a message broker. Implement Dramatiq task actors for: sync operations, Claude Code sessions, fact extraction, briefing generation, reindexing. Add a `TaskRecord` SQLite table for persistent job tracking. Add API endpoints: `POST /api/admin/jobs/submit`, `GET /api/admin/jobs/{id}/status`, `GET /api/admin/jobs`.
- **Why:** This is the **single highest-leverage change** across all audits. Every audit independently identified the lack of a background task queue as a top-priority gap. It unblocks: parallel sync, Claude Code queuing, async operations, long-running MCP calls, and the Telegram bot during complex queries.
- **Effort:** 3-4 days
- **Impact:** 10/10 (universal bottleneck removal)
- **Risk:** Medium (new infrastructure component, new process to manage)
- **Dependencies:** Phase 0 (workstation setup for Redis)
- **AI-assistable:** Yes (Dramatiq setup is well-documented, AI can write task actors).

**Decision Point:** Dramatiq + Redis vs. Dramatiq + SQLite broker vs. custom asyncio queue.
- **Redis**: More capable (pub/sub, caching), but adds a process to manage.
- **SQLite broker**: Dramatiq supports it via `dramatiq-sqlite`. Simpler, no new process. But lacks pub/sub for event bus.
- **Custom asyncio**: Lightest weight, but reinvents retry logic and worker management.
- **Recommendation:** Start with Redis. It serves double duty as task broker AND caching layer (Phase 3). The added process complexity is worth the capability.

#### 2.2 Move Sync Pipeline to Task Queue
- **What:** Refactor `run_all_syncs.py` from sequential `subprocess.run()` calls to Dramatiq task actors. Phase 1 sources (Gmail, Calendar, LinkedIn, Contacts, Slack, WhatsApp) run in parallel. Phase 2+ sources run sequentially after Phase 1 completes. Add per-source retry logic.
- **Why:** Sync currently runs as a monolithic sequential script. Phase 1 sources are independent and could run in parallel, cutting sync time significantly. Also enables on-demand sync triggers from the web UI.
- **Effort:** 2-3 days
- **Impact:** 7/10 (faster syncs, on-demand triggers, retry on failure)
- **Risk:** Medium (sync ordering dependencies must be preserved for later phases)
- **Dependencies:** 2.1
- **AI-assistable:** Yes.

#### 2.3 Enable Multiple Claude Code Sessions
- **What:** Replace the single `_lock` mutex in `claude_orchestrator.py` with a semaphore (default: 2 concurrent sessions). Route Claude Code tasks through the task queue. Add session queuing when all slots are busy.
- **Why:** Currently, only one Claude Code session can run at a time. "Run my backup AND fix the sync bug" requires sequential execution. Users get "Claude Code is busy" with no queue option.
- **Effort:** 1-2 days
- **Impact:** 7/10 (parallel task execution)
- **Risk:** Medium (concurrent sessions share filesystem, need isolation)
- **Dependencies:** 2.1
- **AI-assistable:** Yes.

#### 2.4 Consolidate Chat Pipeline (Deprecate Legacy)
- **What:** Route ALL intents (compose, task, reminder, code) through the agentic loop instead of the hardcoded handlers in `routes/chat.py`. Add `update_person`, `create_calendar_event` (stub), and `send_email` (stub) as agent tools. The legacy intent classification becomes a pre-filter that annotates the query with detected intent, which the agent uses to guide tool selection.
- **Why:** The backend and Telegram audits identified two competing dispatch systems (intent classification -> specialized handlers vs. agent loop). This creates inconsistent behavior across clients. A compose request with "using notes from our last meeting" bypasses the agent's ability to search for context first. One pipeline means consistent behavior across web, Telegram, and MCP.
- **Effort:** 3-4 days
- **Impact:** 8/10 (consistency, maintainability, enables compound queries)
- **Risk:** High (touching the core chat pipeline affects all clients; need thorough testing)
- **Dependencies:** 1.7 (complete reminder tool)
- **AI-assistable:** Yes (but needs careful manual testing across all clients).

#### 2.5 Add Calendar Event Creation Endpoint
- **What:** New `POST /api/calendar/events` endpoint wrapping the Google Calendar API. Parameters: title, start_time, end_time, description, attendees, calendar (personal/work). Expose as agent tool and MCP tool.
- **Why:** Calendar event creation is missing from ALL clients. The MCP audit and backend audit both identify this as a backend gap, not an MCP gap. Required for "schedule a meeting" workflows.
- **Effort:** 1 day
- **Impact:** 7/10 (new write capability)
- **Risk:** Low (Google Calendar API is well-documented)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 2.6 Add Configurable Auth Middleware
- **What:** Implement API key authentication as FastAPI middleware. Bearer token from `.env` (`LIFEOS_API_KEY`). Health endpoints bypass auth. Localhost bypasses auth (development). Tailscale/external requires auth. MCP server sends the API key in headers.
- **Why:** The backend audit flagged no auth as critical gap #1. CORS is `*`. Anyone on the Tailscale network can access all endpoints. This becomes urgent when write endpoints are added.
- **Effort:** 1 day
- **Impact:** 8/10 (security)
- **Risk:** Low (middleware pattern, configurable)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 2.7 Migrate PersonEntity from JSON to SQLite-Primary
- **What:** Make SQLite the source of truth for PersonEntity records (schema already exists as an index). Remove atomic JSON file read/write in `person_entity.py`. Remove `fcntl` file locking.
- **Why:** The Round 2 backend audit identified this as a "silent reliability risk." The JSON file is loaded into memory on every read, written atomically on every save, and protected only by file locking. Concurrent access from the API server and sync pipeline can corrupt it. At 500+ people, every operation touches the entire file.
- **Effort:** 1-2 days
- **Impact:** 8/10 (data reliability, enables concurrent access)
- **Risk:** Medium (data migration must be verified carefully)
- **Dependencies:** 1.1 (WAL mode)
- **AI-assistable:** Yes (but data migration needs manual verification).

**Phase 2 Total Effort:** ~2 weeks
**Phase 2 Exit Criteria:** Task queue running with workers, sync runs in parallel, 2 concurrent Claude Code sessions, unified chat pipeline, calendar event creation, API auth, PersonEntity in SQLite.

---

## Phase 3: Core Capabilities (Month 2)

### Context

With the foundation in place, build the capabilities that make "do anything via Telegram" real.

### Items

#### 3.1 Telegram Voice Message Support
- **What:** Handle Telegram `voice` and `audio` messages. Download the `.ogg` file. Transcribe via local Whisper Large V3 on GPU. Feed transcript into the chat pipeline. Add a new `POST /api/transcribe` endpoint for reuse by other clients.
- **Why:** Voice-first interaction via Telegram. Users can speak instead of typing. Critical for on-the-go usage.
- **Effort:** 2-3 days
- **Impact:** 8/10 (new interaction modality)
- **Risk:** Medium (Whisper model download, audio format conversion, latency concerns)
- **Dependencies:** Phase 0 (GPU setup)
- **AI-assistable:** Yes.

#### 3.2 Telegram Inline Keyboards for Confirmations
- **What:** Add Telegram `InlineKeyboardMarkup` for: approve/reject Claude Code plans, confirm/edit/cancel for task/reminder creation, send/edit/cancel for email drafts. Add `POST /api/telegram/callback` endpoint for button callbacks.
- **Why:** The Telegram audit rated this as the top quick win. Currently all interaction is text-based. Buttons are dramatically better UX for approve/reject, task selection, and quick actions.
- **Effort:** 2 days
- **Impact:** 7/10 (UX improvement for Telegram)
- **Risk:** Low (Telegram API is well-documented)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 3.3 Write Tools: iMessage Send via AppleScript Bridge
- **What:** Create an AppleScript-based iMessage send capability. On the Mac Mini (which has FDA), create a small API server or script that accepts `POST /send-imessage` with recipient phone/email and message text. LifeOS workstation calls this via Tailscale. Expose as agent tool and MCP tool.
- **Why:** "Text Mom I'll be late" is one of the most natural assistant requests. The MCP audit identified message sending as the most impactful missing write capability.
- **Effort:** 2 days
- **Impact:** 8/10 (killer feature for "do anything")
- **Risk:** Medium (AppleScript can be fragile, macOS permissions may change)
- **Dependencies:** 0.5 (Mac Mini as sync agent, must be running)
- **AI-assistable:** Yes.

#### 3.4 Write Tools: Slack Message Posting
- **What:** Add Slack message posting using the existing OAuth tokens in `slack_integration.py`. New endpoint: `POST /api/slack/send` with channel_id and text. Expose as agent tool and MCP tool.
- **Why:** Slack integration is currently read-only. The backend already has the OAuth tokens for posting.
- **Effort:** 1 day
- **Impact:** 6/10 (new write capability)
- **Risk:** Low (existing OAuth tokens, well-known Slack API)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 3.5 Write Tools: Send Gmail Draft
- **What:** Add a `POST /api/gmail/send/{draft_id}` endpoint that sends an existing draft via the Gmail API. The existing `lifeos_gmail_draft` creates drafts; this sends them. Expose as agent tool and MCP tool.
- **Why:** Currently agents can only create email drafts, not send them. The user has to open Gmail to hit send. This completes the email workflow.
- **Effort:** 4 hours
- **Impact:** 6/10 (completes email workflow)
- **Risk:** Low (Gmail API supports `drafts.send()`)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 3.6 Write Tools: Vault File CRUD
- **What:** New endpoints: `POST /api/vault/files` (create note), `PUT /api/vault/files/{path}` (update), `DELETE /api/vault/files/{path}` (delete). Trigger re-indexing on write. Expose as agent and MCP tools.
- **Why:** Every audit treats the vault as read-only. The "do anything" vision requires creating notes, meeting summaries, research documents, and weekly reviews.
- **Effort:** 1-2 days
- **Impact:** 7/10 (vault becomes a write destination)
- **Risk:** Low (filesystem operations with re-indexing trigger)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 3.7 Notifications Aggregation Endpoint
- **What:** New `GET /api/notifications` endpoint that aggregates: birthdays today/upcoming, communication gaps exceeding threshold, overdue tasks, upcoming deadlines, sync failures, cost alerts. Return as a prioritized list with categories.
- **Why:** Enables both the web UI notification center (frontend audit) and Telegram proactive alerts (Telegram audit). Currently these signals are scattered across different endpoints.
- **Effort:** 1 day
- **Impact:** 6/10 (enables proactive features)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 3.8 Local LLM for Simple Synthesis
- **What:** Add a `local_synthesis` option to `model_selector.py`. When query is simple (Level 1-2 in the autonomy model) and source count is low (<3), synthesize using the local 72B model instead of Claude API.
- **Why:** "What time is my meeting?" doesn't need Claude Sonnet. Local synthesis means zero API cost and lower latency for simple lookups. The backend audit found the "haiku" tier maps to Sonnet, meaning there's no cost savings for simple queries.
- **Effort:** 2 days
- **Impact:** 6/10 (cost savings, latency reduction)
- **Risk:** Medium (local model quality must be validated against Claude for common queries)
- **Dependencies:** 0.7 (72B model running)
- **AI-assistable:** Yes.

#### 3.9 Tool Result Caching per Conversation
- **What:** Add a per-conversation cache in the agent loop. If the same person is looked up twice, or the same calendar query runs twice, return the cached result. TTL: 5 minutes.
- **Why:** The backend audit noted redundant tool calls within conversations. Each round of the agent loop can re-query the same data.
- **Effort:** 3 hours
- **Impact:** 4/10 (reduced latency and cost)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 3.10 Extend Claude Code Follow-Up Window
- **What:** Increase from 5 minutes to 30 minutes. Persist session context to SQLite so it survives server restarts.
- **Why:** Users need time to review Claude Code results before following up. 5 minutes is too short for reviewing code changes.
- **Effort:** 3 hours
- **Impact:** 5/10 (better UX for Claude Code sessions)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

**Phase 3 Total Effort:** ~4 weeks
**Phase 3 Exit Criteria:** Voice messages working, inline keyboards in Telegram, can send iMessages/Slack/emails, vault file CRUD, local synthesis for simple queries.

---

## Phase 4: Intelligence Layer (Month 3)

### Context

Proactive intelligence, better context management, and operational visibility.

### Items

#### 4.1 Proactive Intelligence Service
- **What:** Implement a `ProactiveAgent` background service that runs every 30 minutes. Checks: communication gaps (family, close contacts), upcoming meetings needing prep (2 hours ahead), overdue/due-today tasks, relationship cooling trends, birthdays in 3 days, sync health anomalies. Sends prioritized notifications via Telegram (max 3 per cycle, max 8 per day). Uses deduplication keys to avoid repeating notifications.
- **Why:** The system only responds to messages today. It never initiates. This is the transformation from assistant to proactive partner.
- **Effort:** 3-4 days
- **Impact:** 9/10 (transforms the system from reactive to proactive)
- **Risk:** Medium (notification fatigue if poorly tuned; need good deduplication and rate limiting)
- **Dependencies:** 3.7 (notifications endpoint), 3.2 (Telegram inline keyboards for action buttons)
- **AI-assistable:** Yes.

#### 4.2 Telegram Image/Document Input
- **What:** Handle Telegram photo and document messages. Photos: download, run through local vision model (LLaVA or Qwen-VL on GPU) for description + OCR, feed into chat pipeline. Documents: extract text, feed into pipeline.
- **Why:** Users can't currently say "what's in this screenshot" or send a receipt for OCR. Vision model requires GPU, which is now available.
- **Effort:** 2-3 days
- **Impact:** 7/10 (new interaction modality)
- **Risk:** Medium (vision model quality varies, download VRAM management with other models)
- **Dependencies:** Phase 0 (GPU setup, vision model download)
- **AI-assistable:** Yes.

#### 4.3 Structured Logging with Correlation IDs
- **What:** Replace Python `logging.basicConfig` with `structlog` for JSON structured logging. Add a FastAPI middleware that generates a correlation ID per request and propagates it through all service calls. Log tool executions, model calls, and costs with the correlation ID.
- **Why:** Currently impossible to trace a single user request through the pipeline. Debugging requires reading multiple unstructured log files.
- **Effort:** 2 days
- **Impact:** 6/10 (debuggability and operational insight)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 4.4 SSE Push Channel for Web UI
- **What:** New `GET /api/events/stream` SSE endpoint. Publishes events: sync completion, new messages, Claude Code progress, system alerts, task updates. Web UI subscribes and updates in real-time.
- **Why:** The frontend audit wants real-time updates. Currently, the web UI is request-response only. If a sync completes or a Telegram message arrives, the web UI doesn't know.
- **Effort:** 2 days
- **Impact:** 6/10 (live web UI)
- **Risk:** Low (SSE is already used for chat streaming)
- **Dependencies:** 2.1 (task queue with Redis pub/sub for event sourcing)
- **AI-assistable:** Yes.

#### 4.5 System Dashboard Web Page
- **What:** New web page (`web/system.html`) showing: service health grid, sync timeline (per-source freshness), database sizes, API cost chart (daily/weekly/monthly by model), Claude Code session history, active background jobs, log tail (last 50 lines, filterable). Action buttons: trigger sync, trigger reindex.
- **Why:** The infra audit found a broken launchd auto-start, missing backups, and accumulating logs -- all invisible. Making operational health visible is the precondition for operational excellence.
- **Effort:** 3-4 days
- **Impact:** 7/10 (operational visibility)
- **Risk:** Low (all backend APIs exist)
- **Dependencies:** 4.4 (SSE for live updates)
- **AI-assistable:** Yes.

#### 4.6 Task Management Web Page
- **What:** New web page (`web/tasks.html`) showing: list view grouped by context (Work, Personal, Finance, Inbox), inline editing, priority indicators, due date badges, tag chips, filter by status/context/tag.
- **Why:** Full CRUD backend exists. No web UI. Tasks are a core "life OS" feature.
- **Effort:** 2-3 days
- **Impact:** 7/10 (core missing feature)
- **Risk:** Low (backend is complete)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 4.7 Conversation Summarization for Extended Context
- **What:** When conversation exceeds 10 messages, summarize older messages into a compact context block using the local 72B model. Include relevant memories. Include person context from CRM. The agent sees: [summary] + [memories] + [person context] + [recent 10 messages].
- **Why:** Conversation history is truncated to 10 messages. Long conversations lose early context. This provides richer context without exceeding token limits.
- **Effort:** 2 days
- **Impact:** 6/10 (better multi-turn conversations)
- **Risk:** Medium (summarization quality affects downstream reasoning)
- **Dependencies:** 3.8 (local LLM for cost-free summarization)
- **AI-assistable:** Yes.

#### 4.8 Expand MCP Tool Surface (Batch)
- **What:** Add these MCP tools from existing endpoints (no new backend work): `lifeos_reminder_update`, `lifeos_save_to_vault`, `lifeos_birthdays`, `lifeos_crm_statistics`, `lifeos_sync_health`, `lifeos_usage_stats`, `lifeos_family_members`, `lifeos_me_stats`, `lifeos_relationship_detail`, `lifeos_tone_analysis`, `lifeos_memory_update`, `lifeos_memory_delete`. Add chain guidance ("NEXT STEPS" section) to all tool descriptions.
- **Why:** Only 22% of API endpoints are exposed via MCP. These tools have existing backend endpoints and just need to be added to `CURATED_ENDPOINTS` with proper descriptions and formatters.
- **Effort:** 1-2 days
- **Impact:** 6/10 (richer MCP tool surface)
- **Risk:** Low (adding, not changing)
- **Dependencies:** None
- **AI-assistable:** Yes.

**Phase 4 Total Effort:** ~4 weeks
**Phase 4 Exit Criteria:** Proactive notifications working, Telegram handles images, structured logging, system dashboard, task management page, richer MCP tools.

---

## Phase 5: Polish & Expansion (Month 4+)

### Context

Refinements, advanced features, and longer-term architectural improvements.

### Items

#### 5.1 Event-Driven Sync (Gmail Push, Calendar Webhooks)
- **What:** Replace nightly batch sync with near-real-time event-driven sync for Gmail (Google Pub/Sub push notifications) and Calendar (webhook subscriptions). Slack can use Events API for real-time. Keep the nightly batch as a catch-all for edge cases.
- **Why:** Currently, data synced at 3 AM is stale by 9 AM. If someone emails you at 9 AM, it won't be in the system until the next 3 AM sync.
- **Effort:** 5-7 days
- **Impact:** 8/10 (near-real-time data freshness)
- **Risk:** High (Google push notifications require a publicly accessible webhook URL, which means SSL + domain + Tailscale Funnel or similar)
- **Dependencies:** 2.1 (task queue for async processing of events), 2.6 (auth for webhook security)
- **AI-assistable:** Partially (Google Pub/Sub setup has cloud-side configuration).

#### 5.2 Split Monolithic Route Files
- **What:** Split `routes/crm.py` (5,670 lines) into: `crm_people.py`, `crm_family.py`, `crm_relationship.py`, `crm_analytics.py`, `crm_admin.py`. Split `routes/chat.py` (1,800 lines) into: `chat_streaming.py`, `chat_actions.py`, `chat_helpers.py` (route-level).
- **Why:** The two largest files are unmaintainable. Every change risks breaking unrelated features.
- **Effort:** 2-3 days
- **Impact:** 5/10 (maintainability, not user-facing)
- **Risk:** Medium (must preserve all import paths and avoid circular imports)
- **Dependencies:** None
- **AI-assistable:** Yes (refactoring is AI's strong suit, but needs thorough testing).

#### 5.3 Shared CSS/JS Design System
- **What:** Extract common CSS variables, reset styles, nav component, modal system, and utility functions from `crm.html` and `index.html` into `shared.css` and `shared.js`. Import from all pages.
- **Why:** Each HTML file redefines everything from scratch. Adding new pages means duplicating thousands of lines. The frontend audit identified inconsistent color tokens and no shared components.
- **Effort:** 3-4 days
- **Impact:** 6/10 (enables faster UI development, consistency)
- **Risk:** Medium (large refactor across multiple files)
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 5.4 Calendar View Web Page
- **What:** New web page with a week view showing events, color-coded by source (personal/work). Click an event to see meeting prep panel: attendees with CRM links, related vault notes, past meetings with these people.
- **Why:** The backend has calendar APIs and the powerful meeting prep endpoint. No web visualization exists.
- **Effort:** 3-4 days
- **Impact:** 6/10 (nice to have, but Telegram handles most calendar queries)
- **Risk:** Low
- **Dependencies:** 5.3 (shared design system)
- **AI-assistable:** Yes.

#### 5.5 Reminder Management Web Page
- **What:** New web page showing all reminders with: schedule visualization (next fire time), enable/disable toggles, inline editing, creation form. Show execution history for prompt-type reminders.
- **Why:** Backend has full CRUD. No web UI. Users can't see or manage their reminders outside Telegram.
- **Effort:** 2 days
- **Impact:** 5/10
- **Risk:** Low
- **Dependencies:** 5.3 (shared design system)
- **AI-assistable:** Yes.

#### 5.6 Command Palette (Cmd+K)
- **What:** Universal command palette in the web UI that searches people, conversations, tasks, and reminders. Navigate to any page. Launch quick actions (new chat, new task, trigger sync). Keyboard shortcut: Cmd+K (Mac) / Ctrl+K (Linux).
- **Why:** The frontend audit identified no keyboard shortcuts beyond Enter/Shift+Enter. A command palette is the power-user entry point for everything.
- **Effort:** 2-3 days
- **Impact:** 6/10 (power user UX)
- **Risk:** Low
- **Dependencies:** 5.3 (shared JS)
- **AI-assistable:** Yes.

#### 5.7 PWA Manifest + Service Worker
- **What:** Add `manifest.json` for home screen installability. Add a service worker for: offline cached CRM data, cached conversations, push notification support (for birthday reminders, meeting prep alerts).
- **Why:** Makes the web UI installable on iOS/Android home screen. Enables push notifications that bridge the gap between Telegram and web.
- **Effort:** 2-3 days
- **Impact:** 5/10
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 5.8 Agentic Pipeline Visualization in Chat
- **What:** Render the `status` SSE events from the agentic loop as a collapsible step-by-step view in the web UI. Show: which tools are being called, their execution time, data sources found, token usage, cost.
- **Why:** When you ask a question, you see "Thinking..." dots. The rich intermediate process -- tool selection, parallel execution, source gathering -- is invisible. The SSE stream already emits this data.
- **Effort:** 2 days
- **Impact:** 5/10 (transparency and trust)
- **Risk:** Low
- **Dependencies:** None
- **AI-assistable:** Yes.

#### 5.9 Observability Stack (Prometheus + Grafana)
- **What:** Add Prometheus client to FastAPI for metrics: endpoint latency (p50/p95/p99), LLM inference time, token usage and cost, sync health, queue depth, error rates. Deploy Grafana in Docker for dashboards.
- **Why:** No metrics or dashboards exist. Health data is API-only.
- **Effort:** 2-3 days
- **Impact:** 5/10 (operational insight, but lower priority than functional features)
- **Risk:** Low (lightweight stack)
- **Dependencies:** Phase 0 (workstation running Docker)
- **AI-assistable:** Yes.

#### 5.10 External Monitoring (Uptime Kuma)
- **What:** Deploy Uptime Kuma in Docker. Monitor `/health/ping` (new minimal endpoint). Alert via Telegram if API goes down. Monitor ChromaDB, Ollama/vLLM health separately.
- **Why:** All monitoring is self-contained. If the API dies, monitoring dies with it. Independent monitoring is required for reliability.
- **Effort:** 2 hours
- **Impact:** 6/10 (catches outages)
- **Risk:** Low
- **Dependencies:** Phase 0 (Docker)
- **AI-assistable:** Yes.

**Phase 5 Total Effort:** 6-8 weeks (items are largely independent and can be done in any order)

---

## Critical Path Analysis

### Minimum Sequence to "Do Anything via Telegram"

```
Phase 0: Hardware + GPU + 72B model
    |
    v
Phase 1: WAL + backups + memories in agent prompt
    |
    v
Phase 2: Task queue + multiple Claude Code sessions + unified pipeline + auth
    |
    v
Phase 3: Voice input + inline keyboards + iMessage send + Slack send + vault write
    |
    v
Phase 4: Proactive intelligence + image input
```

The shortest path that gets to "do anything via Telegram" is:

1. **Phase 0** (3 days): Hardware migration + GPU embeddings + 72B model
2. **1.1** (1 hour): WAL mode (enables parallelism)
3. **1.7** (2 hours): Complete reminder tool
4. **1.11** (3 hours): Memories in agent prompt
5. **2.1** (3-4 days): Task queue
6. **2.3** (1-2 days): Multiple Claude Code sessions
7. **2.4** (3-4 days): Unified pipeline
8. **2.5** (1 day): Calendar creation
9. **3.1** (2-3 days): Voice messages
10. **3.2** (2 days): Inline keyboards
11. **3.3** (2 days): iMessage send
12. **4.1** (3-4 days): Proactive intelligence

**Total: ~5-6 weeks to "do anything via Telegram"** (overlapping with other Phase items).

---

## Cut List: What to Explicitly Defer

These items from the audits should NOT be done now:

| Item | Why Defer |
|------|-----------|
| **Docker containerization** | Not needed for a single-machine deployment. Adds complexity. Use venvs and systemd. Containerize later if multi-machine deployment is needed. |
| **API versioning (/api/v1/)** | Only one consumer set exists. Add versioning when the first breaking change is needed, not preemptively. |
| **Component framework migration** (Svelte/Preact) | The frontend audit suggests it but CLAUDE.md says "simplicity first." Vanilla JS with shared CSS/JS is sufficient for a personal tool. |
| **Multi-agent orchestration** (coordinator + specialist agents) | Fascinating but premature. The single agentic loop + Claude Code sessions cover 95% of use cases. |
| **Home automation / Financial / Health integrations** | No backend exists. These are entire new product domains, not improvements to existing capabilities. |
| **Kubernetes** | Overkill for single-machine. systemd + Docker Compose is sufficient. |
| **Full event-driven architecture** (internal event bus) | The task queue + Redis pub/sub provides 80% of the value. A formal event bus adds complexity without proportional benefit for a single-user system. |
| **Light/dark theme toggle** | Nice to have, not a priority for a personal tool. |
| **Conversation branching** | Complex to implement, low usage likelihood for a personal assistant. |
| **vLLM (replacing Ollama)** | Only matters if inference throughput becomes a bottleneck. Ollama with 72B model on GPU is fast enough for a single user. Revisit if batched inference for proactive features causes contention. |
| **Redis caching layer** | Redis is added for the task queue. Caching can be added incrementally when specific endpoints show latency issues, not as a blanket layer. |
| **Relationship constellation map** (Dunbar-zoomable viz) | Beautiful but the existing D3.js network graph works. High effort for visual polish. |
| **Life timeline visualization** | Cool concept, but requires significant frontend work for a feature used infrequently. |
| **MCP Tool Playground** | Developer tool, not user-facing. Use curl or Claude Code for testing. |

---

## Decision Points

### Decision 1: Workstation OS -- Linux vs macOS
- **Linux (Ubuntu 24.04):** Best CUDA support, systemd, standard deployment. But loses macOS-specific access (iMessage, Apple Photos, Apple Contacts directly).
- **macOS:** Keeps all data access local. But CUDA support is nonexistent (MPS only, limited model support).
- **Recommendation:** Linux on workstation. Mac Mini stays as data collector. This is the hybrid architecture from the Round 2 infra audit.
- **When:** Phase 0 (must decide before setup).

### Decision 2: Task Queue Technology
- **Dramatiq + Redis:** Simpler API, good error handling, Redis serves as broker + cache + pub/sub.
- **Celery + Redis:** More mature, more features, but more complex configuration.
- **Custom asyncio queue:** Lightest weight, but reinvents retry/worker management.
- **Recommendation:** Dramatiq + Redis for simplicity with room to grow.
- **When:** Phase 2 start.

### Decision 3: Local LLM Serving
- **Ollama:** Simple, already in use, easy model management. Good for development.
- **vLLM:** Better throughput via continuous batching, but more complex setup.
- **Recommendation:** Start with Ollama on GPU. Switch to vLLM only if concurrent inference requests cause contention (e.g., proactive agent + user query + fact extraction all at once).
- **When:** Phase 0 initially; revisit in Phase 4.

### Decision 4: Sync Architecture -- Push vs Batch
- **Event-driven push:** Gmail Pub/Sub, Calendar webhooks, Slack Events API. Near-real-time.
- **Batch with faster cadence:** Keep the existing sync pipeline but run it more frequently (every hour instead of daily).
- **Recommendation:** Start with faster batch cadence (Phase 2). Add push for Gmail and Calendar later (Phase 5) since they require public webhook URLs and more infrastructure.
- **When:** Phase 2 (faster batch), Phase 5 (push).

### Decision 5: Frontend Architecture
- **Keep vanilla JS + shared CSS:** Matches CLAUDE.md philosophy, no build step, AI can easily modify.
- **Migrate to lightweight framework (Svelte/Preact):** Better component reuse, state management.
- **Recommendation:** Keep vanilla JS. Extract shared CSS/JS first. The codebase is a personal tool where developer iteration speed (no build step) outweighs framework benefits.
- **When:** Phase 5 (shared CSS/JS). Framework migration explicitly deferred.

---

## Cost-Benefit Summary

| Phase | Effort | Key Unlocks | Monthly Value |
|-------|--------|------------|---------------|
| **Phase 0** | 3 days | GPU embeddings (10-50x reindex), 72B model, workstation running | Foundation for everything |
| **Phase 1** | 2 weeks | Data safety (backups), markdown chat, memories in agent, MCP write tools | Immediate quality of life |
| **Phase 2** | 2 weeks | Task queue (universal unblock), parallel execution, unified pipeline, auth | Architectural transformation |
| **Phase 3** | 4 weeks | Voice input, message sending, vault writes, proactive start | "Do anything" becomes real |
| **Phase 4** | 4 weeks | Proactive intelligence, image input, system visibility, task/reminder UI | System becomes proactive |
| **Phase 5** | 6-8 weeks | Event-driven sync, calendar UI, observability, file splitting | Polish and operations |

**Total estimated timeline: 4-5 months for full implementation.**

The system is fully functional at every phase boundary. Phase 2 completion (~5 weeks in) is the major inflection point where the architecture transforms from synchronous/single-session to async/parallel. Phase 3 completion (~9 weeks in) is where "do anything via Telegram" becomes credible.

---

## Appendix: Items by Effort (for Sprint Planning)

### Under 2 Hours
- 1.1 WAL mode (1h)
- 1.4 Log rotation (1h)
- 1.8 MCP PUT/PATCH fix (1h)
- 1.10 MCP person_update (30min)
- 1.12 Telegram command menu (30min)
- 5.10 External monitoring (2h)

### 2-4 Hours
- 1.5 Markdown rendering (3h)
- 1.6 Remove embedded CRM (2h)
- 1.7 Complete reminder tool (2h)
- 1.9 MCP vault read (3h)
- 1.11 Agent memories (3h)
- 3.5 Gmail send (4h)
- 3.9 Tool result caching (3h)
- 3.10 Follow-up window extension (3h)
- 0.1 Pre-migration prep (4h)

### 1 Day
- 1.2 Auto-start / systemd (2h + verification)
- 1.3 Database backups (4h)
- 2.5 Calendar event creation (1d)
- 2.6 Auth middleware (1d)
- 3.4 Slack send (1d)
- 3.7 Notifications endpoint (1d)

### 2-3 Days
- 2.3 Multiple Claude Code sessions (1-2d)
- 2.7 PersonEntity SQLite migration (1-2d)
- 3.1 Voice messages + Whisper (2-3d)
- 3.2 Telegram inline keyboards (2d)
- 3.3 iMessage send (2d)
- 3.6 Vault file CRUD (1-2d)
- 3.8 Local synthesis (2d)
- 4.3 Structured logging (2d)
- 4.4 SSE push channel (2d)
- 4.6 Task management page (2-3d)
- 4.7 Conversation summarization (2d)
- 4.8 MCP tool batch expansion (1-2d)
- 5.2 Split route files (2-3d)

### 3-4 Days
- 2.1 Task queue (3-4d)
- 2.4 Consolidate chat pipeline (3-4d)
- 4.1 Proactive intelligence (3-4d)
- 4.2 Image input (2-3d)
- 4.5 System dashboard (3-4d)
- 5.3 Shared CSS/JS (3-4d)
- 5.4 Calendar page (3-4d)

### 5+ Days
- 2.2 Sync to task queue (2-3d, depends on 2.1)
- 5.1 Event-driven sync (5-7d)
