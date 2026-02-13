# LifeOS Backend Architecture Audit

**Auditor:** Claude Opus 4.6 (Backend Systems Architect)
**Date:** 2026-02-13
**Scope:** Full backend — API routes, services, data models, search pipeline, sync infrastructure, orchestration

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [API Routes Map](#2-api-routes-map)
3. [Service Layer Deep Dive](#3-service-layer-deep-dive)
4. [Data Model & Entity Resolution](#4-data-model--entity-resolution)
5. [Search Pipeline](#5-search-pipeline)
6. [Chat & Agentic Pipeline](#6-chat--agentic-pipeline)
7. [Claude Code Orchestration](#7-claude-code-orchestration)
8. [Sync Infrastructure](#8-sync-infrastructure)
9. [Observability & Resilience](#9-observability--resilience)
10. [Architectural Limitations & Gaps](#10-architectural-limitations--gaps)
11. [Hardware Upgrade Opportunities](#11-hardware-upgrade-opportunities)
12. [Recommendations for "Do Anything via Telegram" Vision](#12-recommendations-for-do-anything-via-telegram-vision)

---

## 1. Architecture Overview

### Stack
- **Framework:** FastAPI (async) on port 8000, bound to `0.0.0.0` for Tailscale access
- **Vector Store:** ChromaDB server (HTTP on port 8001), collection `lifeos_vault`, cosine similarity, HNSW index
- **Keyword Search:** SQLite FTS5 (BM25) — `data/bm25_index.db`
- **Embedding Model:** `mixedbread-ai/mxbai-embed-large-v1` (1024 dims, sentence-transformers, CPU)
- **Reranker:** `cross-encoder/ms-marco-MiniLM-L6-v2` (query-aware, protects BM25 exact matches)
- **Local LLM:** Ollama with `qwen2.5:7b-instruct` for query routing
- **Cloud LLM:** Anthropic Claude API (Sonnet 4.5 default, Opus 4.5 for complex queries)
- **Databases:** 5 SQLite databases in `data/` — conversations, interactions, BM25 index, CRM (source_entities, person_entities), sync health
- **Virtual Env:** `~/.venvs/lifeos` (external to avoid macOS TCC delays)
- **Process Management:** launchd service + `server.sh` script (no auto-reload)

### Startup Services (lifespan)
File: `api/main.py:168-236`
1. **GranolaProcessor** — file watcher for meeting note transcripts
2. **OmiProcessor** — file watcher for Omi wearable transcripts
3. **CalendarIndexer** — scheduled sync at 8AM, noon, 3PM Eastern
4. **HealthCheckThread** — 2:30 AM + 7:00 AM checks, collects failures, sends alerts
5. **TelegramBotListener** — long-polling thread for inbound messages
6. **ReminderScheduler** — cron/once trigger scheduler for Telegram reminders

### Entry Points
- **Web UI:** `/` (home), `/chat`, `/crm`, `/me`, `/family`, `/relationship`, `/birthdays`
- **API:** `/api/*` (17 route modules)
- **Telegram:** Bot listener polls `getUpdates`, dispatches to chat pipeline
- **MCP Server:** Separate process (not in this audit scope) calling the same API endpoints

---

## 2. API Routes Map

### Core Chat & Search
| Endpoint | File | Purpose |
|----------|------|---------|
| `POST /api/ask/stream` | `routes/chat.py:915` | Main SSE streaming chat (agentic loop or legacy pipeline) |
| `POST /api/ask` | `routes/ask.py:78` | Simple sync RAG (search + synthesize) |
| `POST /api/search` | `routes/search.py:81` | Raw hybrid search (vector + BM25) |
| `POST /api/save-to-vault` | `routes/chat.py:1803` | Save chat content to Obsidian vault |

### People & CRM (~60+ endpoints)
| Endpoint Group | File | Purpose |
|----------------|------|---------|
| `/api/crm/people` (GET/PATCH) | `routes/crm.py:595-808` | List, detail, update people |
| `/api/crm/people/{id}/timeline` | `routes/crm.py:1611-1858` | Chronological interaction history |
| `/api/crm/people/{id}/connections` | `routes/crm.py:1857` | Co-occurring people via shared events |
| `/api/crm/people/{id}/facts` | `routes/crm.py:1964-2150` | LLM-extracted facts CRUD |
| `/api/crm/people/{id}/strength` | `routes/crm.py:1925` | Relationship strength breakdown |
| `/api/crm/people/merge` | `routes/crm.py:803` | Merge duplicate people |
| `/api/crm/people/split` | `routes/crm.py:1336` | Split incorrectly merged people |
| `/api/crm/me/*` | `routes/crm.py:2991-3660` | Owner's stats, timeline, interactions |
| `/api/crm/family/*` | `routes/crm.py:3660-4334` | Family dashboard with communication gaps |
| `/api/crm/review-queue` | `routes/crm.py:4482-4637` | Entity resolution review workflow |
| `/api/crm/data-health` | `routes/crm.py:4637-4858` | Data quality diagnostics |
| `/api/crm/relationship/insights` | `routes/crm.py:4951-5227` | Relationship pattern extraction |
| `/api/crm/relationship/tone-analysis` | `routes/crm.py:5082-5473` | Communication tone analysis |
| `/api/crm/cleanup/*` | `routes/crm.py:5473-5670` | CRM cleanup/dedup workflow |
| `/api/crm/discover` | `routes/crm.py:2191` | Suggest new connections |
| `/api/crm/network` | `routes/crm.py:2390` | Network graph data |
| `/api/crm/statistics` | `routes/crm.py:2361` | CRM-wide stats |
| `/api/crm/birthdays/*` | `routes/crm.py:551-595` | Birthday tracking |
| `/api/crm/sync/health` | `routes/crm.py:4334-4430` | Sync monitoring |
| `/api/crm/slack/*` | `routes/crm.py:2663-2779` | Slack OAuth + sync |
| `/api/crm/contacts/*` | `routes/crm.py:2779-2991` | Apple Contacts sync |

### Integrations
| Endpoint Group | File | Purpose |
|----------------|------|---------|
| `/api/calendar/*` | `routes/calendar.py` | Calendar upcoming, search, meeting prep |
| `/api/gmail/*` | `routes/gmail.py` | Email search, message detail, draft creation |
| `/api/drive/*` | `routes/drive.py` | Drive file search and content |
| `/api/slack/*` | `routes/slack.py` | Slack status, search, conversations, sync |
| `/api/imessage/*` | `routes/imessage.py` | iMessage search, conversations, stats |
| `/api/photos/*` | `routes/photos.py` | Apple Photos face recognition, thumbnails |

### System
| Endpoint Group | File | Purpose |
|----------------|------|---------|
| `/api/admin/*` | `routes/admin.py` | Reindex, Granola/Omi/Calendar management, usage stats |
| `/api/conversations/*` | `routes/conversations.py` | Conversation thread CRUD |
| `/api/memories/*` | `routes/memories.py` | Persistent memory CRUD |
| `/api/tasks/*` | `routes/tasks.py` | Obsidian task management |
| `/api/reminders/*` | `routes/reminders.py` | Scheduled reminder CRUD |
| `/api/briefings/*` | `routes/briefings.py` | Stakeholder briefing generation |
| `/health`, `/health/full`, `/health/services` | `main.py` | Health checks |

**Total endpoint count: ~120+ endpoints across 17 route modules.**

---

## 3. Service Layer Deep Dive

### 3.1 Core Services (75 files in `api/services/`)

**Search Pipeline:**
- `vectorstore.py` — ChromaDB HTTP client, cosine similarity search
- `bm25_index.py` — SQLite FTS5 keyword index, porter stemming
- `hybrid_search.py` — RRF fusion (vector + BM25), recency boost, filename boost, name expansion
- `reranker.py` — Cross-encoder reranking with query-aware BM25 protection
- `query_classifier.py` — Factual vs semantic query classification
- `embeddings.py` — Sentence-transformers embedding service (lazy-loaded singleton)

**Chat Intelligence:**
- `agent_loop.py` — Multi-turn agentic loop (5 tool rounds max, parallel tool execution)
- `agent_tools.py` — 12 tool definitions (search_vault, search_calendar, search_email, search_drive, search_slack, search_web, get_message_history, person_info, manage_tasks, manage_reminders, create_email_draft, read_vault_file)
- `agent_system_prompt.py` — Cached static prompt + dynamic datetime
- `query_router.py` — Ollama-based source routing with keyword fallback
- `model_selector.py` — Haiku/Sonnet/Opus selection based on keyword complexity
- `synthesizer.py` — Claude API wrapper for RAG synthesis
- `chat_helpers.py` — Intent classification (compose/task/reminder), date extraction, follow-up expansion
- `conversation_context.py` — Tracks person/topic/reminder context across turns
- `conversation_store.py` — SQLite conversation persistence
- `web_search.py` — Claude's native web_search tool integration

**People System:**
- `person_entity.py` — PersonEntity dataclass + JSON/SQLite store (canonical records)
- `source_entity.py` — SourceEntity dataclass + SQLite store (raw observations)
- `entity_resolver.py` — 3-pass resolution: email anchoring, fuzzy name matching, disambiguation
- `people_aggregator.py` — Merges source entities into person entities
- `people.py` — Legacy people dictionary + name resolution
- `person_facts.py` — Multi-stage LLM fact extraction pipeline (Claude extract + Ollama validate)
- `person_indexer.py` — Indexes people into ChromaDB for semantic search
- `person_stats.py` — Aggregates interaction counts per person

**Relationship System:**
- `relationship.py` — Relationship store (pairs of people with shared context)
- `relationship_discovery.py` — Discovers connections via co-occurrence in events/emails
- `relationship_metrics.py` — Relationship strength: recency + frequency + diversity scoring
- `relationship_insights.py` — LLM-extracted relationship patterns from therapy notes
- `relationship_summary.py` — Summary generation for relationships
- `meeting_prep.py` — Aggregates notes + history for meeting preparation

**Interaction Store:**
- `interaction_store.py` — SQLite store for lightweight interaction records (email, calendar, vault mentions, messages)
- `link_override.py` — Manual entity linking overrides
- `review_queue.py` — Queue for human review of entity resolution

**Data Ingestion:**
- `indexer.py` — Vault file watcher (watchdog), incremental indexing, debounced processing
- `granola_processor.py` — Meeting transcript processor (file watcher)
- `omi_processor.py` — Omi wearable transcript processor
- `calendar_indexer.py` — Google Calendar scheduled sync
- `imessage.py` — Direct SQLite access to macOS chat.db
- `slack_integration.py` + `slack_indexer.py` + `slack_sync.py` — Slack workspace integration
- `gmail.py` — Gmail API wrapper
- `calendar.py` — Google Calendar API wrapper
- `drive.py` — Google Drive API wrapper
- `apple_contacts.py` — Apple Contacts DB access
- `apple_photos.py` + `apple_photos_sync.py` — Photos.sqlite face recognition data

**External Integrations:**
- `google_auth.py` — OAuth2 for personal + work Google accounts
- `ollama_client.py` — Local LLM inference (routing, fact validation)
- `telegram.py` — Bot listener + message sending + chat-via-API client
- `claude_orchestrator.py` — Spawns headless Claude CLI sessions

**Infrastructure:**
- `service_health.py` — Real-time service status registry with severity levels
- `sync_health.py` — Sync freshness tracking (stale detection)
- `notifications.py` — Alert routing (email + Telegram)
- `resilience.py` — Retry decorators and graceful degradation
- `cost_tracker.py` — Claude API usage and cost tracking
- `reminder_store.py` — Scheduled reminders with cron support
- `task_manager.py` — Obsidian-compatible markdown task management

---

## 4. Data Model & Entity Resolution

### Two-Tier Model
File: `source_entity.py`, `person_entity.py`

**SourceEntity** (immutable raw observations):
- 14 source types: gmail, calendar, slack, imessage, whatsapp, signal, contacts, phone_contacts, linkedin, vault, granola, phone_call, phone, photos
- Fields: observed_name, observed_email, observed_phone, metadata (JSON)
- Linkage: `canonical_person_id` + `link_confidence` (0-1) + `link_status` (auto/confirmed/rejected)
- Storage: SQLite `data/crm.db`

**PersonEntity** (canonical records):
- Primary identifier: emails list (most reliable cross-source anchor)
- Secondary: canonical_name + aliases for fuzzy matching
- Professional: company, position, linkedin_url
- Context: category (work/personal/family), vault_contexts
- Stats: meeting_count, email_count, mention_count, message_count, slack_message_count
- Phone: phone_numbers list (E.164), phone_primary
- Storage: JSON file (`data/people_entities.json`) + SQLite index

### Entity Resolution Pipeline
File: `entity_resolver.py`

Three-pass resolution:
1. **Email anchoring** — exact email match across all source entities
2. **Fuzzy name matching** — rapidfuzz scoring with context boost (same company, same event)
3. **Disambiguation** — creates separate entities when confidence is too low

Key details:
- Name parsing strips prefixes (Dr., Mr.) and suffixes (MD, PhD, Jr)
- Nickname expansion via `config/nicknames.csv` (e.g., "Al" → "Alex")
- Domain context mapping (`config/people_config.py`) — work domains tagged automatically

### Strengths
- Clean separation between raw observations and canonical records
- Confidence scoring enables review workflows
- Link status (auto/confirmed/rejected) allows human override
- Interaction store provides lightweight event tracking without duplicating content

### Limitations
- **PersonEntity storage is JSON-file-based** — `data/people_entities.json` is loaded/saved atomically. Works fine at hundreds of people but would struggle at thousands. No concurrent write safety beyond file-level locking (`fcntl`).
- **Entity resolution is batch-only** — no real-time resolution as new data arrives. Must wait for sync scripts.
- **No merge undo** — once two people are merged, the operation is not easily reversible (split exists but requires manual intervention).
- **Nickname lookup is CSV-based** — `config/nicknames.csv` at 74KB. No learning from user corrections.

---

## 5. Search Pipeline

### Architecture
```
Query → Name Expansion → [Vector Search, BM25 Search] → RRF Fusion → Recency Boost → Filename Boost → Reranker → Results
```

File: `hybrid_search.py`

**Step 1: Name Expansion** — Nicknames/aliases in query expanded to canonical names
**Step 2: Dual Search** — Vector (ChromaDB cosine) + BM25 (FTS5, OR semantics) run in parallel
**Step 3: RRF Fusion** — `score = Σ 1/(60 + rank)` combines both result lists
**Step 4: Boosting** — Recency (0-50% boost) + Filename match (2x for person queries)
**Step 5: Reranking** — Cross-encoder reranker with query-aware protection for factual queries

### Query Router
File: `query_router.py`

- Routes to appropriate sources: vault, calendar, gmail, drive, people, actions, slack, photos, web
- Primary: Ollama local LLM (qwen2.5:7b, 45s timeout)
- Fallback: Keyword pattern matching (when Ollama unavailable)
- Also determines: fetch_depth (shallow/normal/deep), recommended_model, action_after (task/reminder/compose)
- Extracts person names and populates `relationship_context` from CRM

### Model Selection
File: `model_selector.py`

| Tier | Model | When Used |
|------|-------|-----------|
| Haiku | `claude-sonnet-4-5` (!) | Simple lookups ("what time", "who is") |
| Sonnet | `claude-sonnet-4-5` | Standard synthesis (default) |
| Opus | `claude-opus-4-5` | Complex reasoning, large context (>8K tokens), many sources |

**Observation:** Haiku tier maps to Sonnet 4.5, not Haiku 4.5 — `model_selector.py:15`. This means there's no real cost savings for simple queries. The comment says "Use Sonnet 4.5 as minimum (Haiku 4.5 may not be available)."

### Strengths
- Hybrid search with RRF is solid — handles both semantic and exact match queries
- Query-aware reranker protection prevents losing BM25 exact matches for factual queries
- Recency boost ensures recent notes rank higher
- Filename boost is smart for person queries ("Alex.md" ranks first)

### Limitations
- **Embedding model runs on CPU** — sentence-transformers with mxbai-embed-large-v1 generates 1024-dim embeddings on CPU. Slow for batch re-indexing.
- **No GPU acceleration** — embedding and reranking are CPU-bound. Major bottleneck for indexing throughput.
- **BM25 index rebuild is full-scan** — no incremental keyword index updates for individual file changes.
- **ChromaDB is single-collection** — all vault content in one flat collection. No collection-per-source or hierarchical filtering.
- **No query expansion** — beyond nickname lookup, no synonym or conceptual expansion.
- **Reranker is lightweight** — ms-marco-MiniLM-L6-v2 is fast but not state-of-the-art. Could benefit from a larger model on GPU.

---

## 6. Chat & Agentic Pipeline

### Two Pipelines
File: `routes/chat.py` (1800+ lines)

The `POST /api/ask/stream` endpoint at line 915 runs one of two pipelines:

**1. Agentic Pipeline (default for Telegram and complex queries):**
- File: `agent_loop.py` — multi-turn loop with up to 5 tool rounds
- Uses `agent_tools.py` — 12 tools (search_vault, search_calendar, search_email, etc.)
- Parallel tool execution via `asyncio.gather`
- Streaming SSE events: `text`, `status`, `result`
- Prompt caching: static system prompt cached via `cache_control: ephemeral`
- Cost tracking per conversation

**2. Legacy Pipeline (fallback for simpler queries):**
- Runs inside `routes/chat.py` directly
- Query router selects sources → parallel data fetching → synthesis
- Handles action intents: reminder create/edit/delete, task create/complete, email compose
- Date extraction, person resolution, follow-up expansion
- Conversation context tracking

### Agent Tools
File: `agent_tools.py`

12 tools with consolidated actions:
- **Retrieval:** search_vault, search_calendar, search_email, search_drive, search_slack, search_web, get_message_history, read_vault_file
- **People:** person_info (lookup/briefing)
- **Actions:** manage_tasks (create/list/complete), manage_reminders (create/list), create_email_draft

### Strengths
- Agentic loop with parallel tool execution is well-designed
- System prompt uses Anthropic caching for cost savings
- Multi-turn conversation with tool-result injection
- Good tool descriptions guide Claude's tool selection
- Status messages streamed to UI during tool execution

### Limitations
- **Legacy pipeline is 1800+ lines of spaghetti** — `routes/chat.py` is the largest file in the codebase. Complex intent detection (compose, reminder, task) with deeply nested if/else chains. Difficult to extend.
- **5 tool rounds max** — some complex queries might need more rounds. No dynamic adjustment.
- **No tool-result caching** — if the same person is looked up twice in a conversation, it hits the API again.
- **No streaming for tool execution** — tools run to completion before results are sent. Long tool runs (e.g., Gmail search across both accounts) block the round.
- **Agent has no memory across conversations** — each conversation starts fresh. No persistent agent memory for learned preferences.
- **No vision/image understanding in chat** — attachments are supported (images, PDFs) but only for synthesis, not for tool use. Can't "look at this screenshot and..."

---

## 7. Claude Code Orchestration

File: `api/services/claude_orchestrator.py`

### Architecture
- Spawns headless Claude CLI (`claude -p ... --output-format stream-json --dangerously-skip-permissions`)
- Single session at a time (mutex via `_lock`)
- Stream reader thread parses JSON events from stdout
- `[NOTIFY]` lines relayed to Telegram
- `[CLARIFY]` lines pause session, wait for user response
- Plan mode: Claude proposes plan → user approves → implements
- Follow-up window: 5 minutes to continue completed sessions
- Heartbeat: every 5 minutes if no `[NOTIFY]`
- Cost cap: configurable `LIFEOS_CLAUDE_MAX_COST` (default $2.00)
- Timeout: configurable `LIFEOS_CLAUDE_TIMEOUT` (default 3600s = 1 hour)
- Max turns: configurable `LIFEOS_CLAUDE_MAX_TURNS` (default 50)

### System Prompt
File: `claude_orchestrator.py:26-131`

Rich system prompt includes:
- Creative task interpretation guidelines
- Scope-limiting rules (proportional changes)
- Clarification protocol
- Persistence requirements (try 3 approaches before giving up)
- Full LifeOS MCP tool descriptions for data access
- Key filesystem locations (vault, LifeOS project, other code)

### Working Directory Resolution
File: `directory_resolver.py`

Resolves task text to appropriate working directory:
- "LifeOS" → `~/Documents/Code/LifeOS`
- Other code projects → `~/Documents/Code/`
- Default → home directory

### Strengths
- Well-thought-out orchestration with plan mode, clarification, and follow-ups
- Heartbeat keeps user informed during long operations
- Cost cap prevents runaway sessions
- Clean environment stripping prevents CLAUDE_* env var conflicts
- Typing indicators provide UX feedback

### Limitations
- **Single session at a time** — can't queue tasks or run parallel sessions. If a session is running, new requests are rejected.
- **No persistent context across sessions** — each Claude Code session starts fresh. Doesn't remember what it did last time.
- **5-minute follow-up window is short** — if user takes 6 minutes to respond, context is lost.
- **No progress persistence** — if server restarts during a session, session state is lost.
- **Working directory resolution is naive** — only handles a few hardcoded patterns. Doesn't understand project context.
- **No output capture** — the results of Claude Code sessions aren't stored. Can't review what it did later.
- **No approval workflow for destructive actions** — `--dangerously-skip-permissions` means no guardrails on file system operations.
- **Browser tasks possible but fragile** — `--chrome` flag enables browser, but no structured web automation framework.

---

## 8. Sync Infrastructure

### Daily Sync
File: `scripts/run_all_syncs.py` (800+ lines)

Runs via launchd at 3:00 AM. Multi-phase pipeline:

**Phase 1: Data Collection**
- Gmail + Calendar interactions (personal + work accounts)
- LinkedIn connections (CSV import)
- Apple Contacts
- WhatsApp (via wacli)
- Slack

**Phase 2: Entity Resolution**
- Source entity cleanup
- Entity relinking
- Source entity merging (link_source_entities.py)

**Phase 3: Enrichment**
- Vault reindex
- Person stats aggregation
- Relationship strength computation
- Relationship discovery
- Photos sync

**FDA Sync (2:50 AM via cron):**
- Phone calls + iMessage require Full Disk Access
- Runs from Terminal.app which has FDA permission
- `scripts/run_sync_with_fda.sh` → `scripts/run_fda_syncs.py`

### Sync Health Tracking
File: `api/services/sync_health.py`

- Tracks last sync time per source
- Staleness detection (>24h = stale)
- Error logging per source
- Summary endpoint at `/api/crm/sync/health`

### Strengths
- Comprehensive multi-phase pipeline
- Per-source health tracking
- FDA separation is smart (avoids permission issues)
- Dry-run support for testing

### Limitations
- **Sync is batch-only (daily at 3 AM)** — no real-time or near-real-time sync. If someone emails you at 9 AM, it won't be in the interaction store until 3 AM the next day.
- **No incremental sync** — most sync scripts do full re-scan. Gmail/Calendar have date-based incremental, but others don't.
- **No webhook integration** — doesn't listen for Gmail push notifications or calendar webhooks. Polling only.
- **WhatsApp via wacli is fragile** — third-party CLI tool, not an official API.
- **LinkedIn is CSV-based** — requires manual export. No API integration.
- **Sync errors don't retry** — if a sync fails, it waits until the next day.
- **No sync progress tracking** — can't see "currently syncing Gmail, 50% done" from the UI.

---

## 9. Observability & Resilience

### Service Health Registry
File: `api/services/service_health.py`

- 9 tracked services: chromadb, ollama, google_calendar, google_gmail, telegram, embedding_model, vault_filesystem, backup_storage, bm25_index
- Severity levels: CRITICAL (immediate alert), WARNING (batched nightly), INFO (log only)
- Degradation event recording (when fallbacks are used)
- Rate-limited alerts (5-minute cooldown between same-service alerts)

### Cost Tracking
File: `api/services/cost_tracker.py`

- SQLite-based usage tracking
- Per-conversation cost aggregation
- Model pricing: Haiku $0.25/$1.25, Sonnet $3/$15, Opus $15/$75 per million tokens

### Resilience
File: `api/services/resilience.py`

- Async retry decorator with exponential backoff
- ServiceUnavailableError with partial result support
- PartialResultError for degraded responses

### Strengths
- Good service health tracking with severity-based alerting
- Degradation event recording helps identify flapping services
- Cost tracking provides visibility into API spend

### Limitations
- **No structured logging** — uses Python's `logging` module with ad-hoc formats. No JSON structured logging for log aggregation.
- **No request tracing** — no correlation IDs across services. Hard to trace a single user request through the pipeline.
- **No metrics/dashboards** — no Prometheus/Grafana or equivalent. Health data only available via API endpoints.
- **Alert fatigue potential** — batched nightly alerts may be ignored. No escalation policy.
- **No performance profiling** — no endpoint latency tracking, no slow query detection.
- **Memory monitoring exists but unused** — `api/utils/memory_monitor.py` exists but doesn't appear to be integrated into health checks.

---

## 10. Architectural Limitations & Gaps

### Critical Gaps

1. **No Authentication/Authorization**
   - CORS is `*` (all origins allowed) — `main.py:283-289`
   - No API keys, no OAuth, no session management
   - Anyone on the Tailscale network can access all endpoints
   - Risk: Any device on the tailnet can read/modify all personal data

2. **No Rate Limiting**
   - No request rate limiting on any endpoint
   - A runaway client could exhaust Claude API credits rapidly
   - No per-conversation cost limits beyond the Claude Code orchestrator

3. **Single-Threaded Bottlenecks**
   - Background services (Granola, Omi, Calendar, Health) run in threads but share the GIL
   - Embedding generation is CPU-bound and blocks during re-indexing
   - ChromaDB operations are synchronous HTTP calls

4. **Monolithic Route Files**
   - `routes/crm.py` is **204KB / 5670+ lines** — the single largest file
   - `routes/chat.py` is **90KB / 1800+ lines**
   - These are difficult to navigate, test, and maintain

5. **No Database Migrations**
   - SQLite schemas are created inline (`CREATE TABLE IF NOT EXISTS`)
   - No migration framework (alembic or similar)
   - Schema changes require manual intervention

6. **No Backup Verification**
   - Backup path exists in settings but no automated backup testing
   - No backup restoration workflow

### Moderate Gaps

7. **No WebSocket Support**
   - Chat uses SSE (Server-Sent Events) for streaming
   - No bidirectional communication
   - Can't push real-time updates to the UI (new messages, sync progress)

8. **Telegram as Only Mobile Interface**
   - No push notifications via APNs/FCM
   - No native app integration
   - Message length limit (4096 chars) constrains rich responses

9. **No Caching Layer**
   - No Redis/memcached for frequent queries
   - People lookups, recent conversations, and calendar data could benefit from caching
   - Each request re-instantiates services (though singletons help)

10. **No Background Task Queue**
    - No Celery/RQ/dramatiq for async work
    - Long operations (fact extraction, briefing generation) block the request
    - Sync runs as a monolithic script, not queued jobs

11. **Embedding Model is CPU-Only**
    - `sentence-transformers` uses PyTorch on CPU
    - No CUDA/MPS acceleration configured
    - Batch re-indexing is slow

---

## 11. Hardware Upgrade Opportunities

With a Corsair AI Workstation 300 (likely NVIDIA RTX 4090/5090 GPU + high-core-count CPU):

### Immediate Impact

1. **GPU-Accelerated Embeddings**
   - Move sentence-transformers to CUDA
   - Expected: 10-50x speedup for batch embedding (re-indexing goes from hours to minutes)
   - `embeddings.py` — add `device="cuda"` to SentenceTransformer init

2. **GPU-Accelerated Reranking**
   - Cross-encoder reranker benefits massively from GPU
   - Could upgrade to a larger reranker model (e.g., `cross-encoder/ms-marco-TinyBERT-L6` → `colbert-ir/colbertv2.0`)

3. **Larger Local LLMs**
   - Replace `qwen2.5:7b` with 13B/34B/70B models for better routing accuracy
   - Could run Llama 3.1 70B or Mistral 8x22B for local synthesis (reduce Claude API costs)
   - Local fact extraction/validation (currently uses Ollama 7B + Claude)

4. **Local Vision Models**
   - Could run LLaVA or similar for image understanding
   - OCR pipeline for screenshots, documents, receipts
   - Photo analysis beyond face recognition

### Medium-Term Impact

5. **Real-Time Embedding Pipeline**
   - GPU enables real-time re-embedding on file save (sub-second)
   - Could enable live search-as-you-type with instant results

6. **Local Speech-to-Text**
   - Whisper large-v3 on GPU for voice message transcription
   - Could transcribe Omi recordings locally instead of relying on their API

7. **Local Summarization**
   - Daily email/message summaries using local LLM (no API cost)
   - Meeting note summarization before indexing

---

## 12. Recommendations for "Do Anything via Telegram" Vision

### Priority 1: Expand Agentic Capabilities

1. **Concurrent Claude Code Sessions**
   - Allow queueing multiple tasks
   - Implement session persistence across server restarts
   - Store session results for review

2. **Web Automation Framework**
   - Structured browser automation via Playwright/Puppeteer
   - Predefined workflows: "book a restaurant on OpenTable", "order groceries on Instacart"
   - Screenshot + vision model for verification

3. **File Operations**
   - Upload/download files via Telegram (documents, images)
   - OCR + indexing of received images
   - Generate and send PDFs/reports

### Priority 2: Real-Time Data Pipeline

4. **Event-Driven Architecture**
   - Gmail push notifications via Google Pub/Sub
   - Calendar webhook subscriptions
   - iMessage/phone call hooks via macOS observers
   - Slack real-time events API
   - Replace daily batch sync with near-real-time updates

5. **Background Task Queue**
   - Implement job queue (e.g., Dramatiq with Redis)
   - Async fact extraction, briefing generation, relationship discovery
   - Progress tracking and notifications

### Priority 3: Local Intelligence

6. **Local LLM for Routine Tasks**
   - Query routing, intent classification, fact validation on local GPU
   - Reduce Claude API dependency for simple operations
   - Local summarization for daily digests

7. **Multi-Modal Understanding**
   - Voice messages → Whisper STT → chat pipeline
   - Image messages → LLaVA analysis → context
   - Document screenshots → OCR → indexing

### Priority 4: Platform Hardening

8. **API Authentication**
   - API key authentication for external access
   - Per-client rate limiting
   - Audit logging for all data access

9. **Database Layer Modernization**
   - Migrate PersonEntity from JSON file to SQLite
   - Add proper migration framework
   - Connection pooling for SQLite (currently opens/closes per operation)

10. **Structured Logging & Monitoring**
    - JSON structured logging
    - Request correlation IDs
    - Prometheus metrics endpoint
    - Dashboard for API latency, error rates, cost tracking

---

## Appendix: File Size Analysis (Largest Files)

| File | Size | Lines (est.) | Notes |
|------|------|-------------|-------|
| `routes/crm.py` | 204 KB | ~5,670 | Largest file, needs splitting |
| `routes/chat.py` | 90 KB | ~1,800 | Second largest, complex intent logic |
| `services/person_facts.py` | 63 KB | ~1,800 | Fact extraction pipeline |
| `services/relationship_discovery.py` | 53 KB | ~1,500 | Connection discovery |
| `services/source_entity.py` | 45 KB | ~1,200 | Source entity store |
| `services/interaction_store.py` | 44 KB | ~1,200 | Interaction store |
| `services/entity_resolver.py` | 41 KB | ~1,100 | Entity resolution |
| `services/person_entity.py` | 39 KB | ~1,100 | Person entity store |
| `services/people_aggregator.py` | 36 KB | ~1,000 | People aggregation |
| `services/granola_processor.py` | 32 KB | ~900 | Meeting transcript processing |
| `services/relationship.py` | 32 KB | ~900 | Relationship store |
| `services/relationship_insights.py` | 32 KB | ~900 | Relationship insights |
| `services/imessage.py` | 32 KB | ~900 | iMessage store |
| `scripts/run_all_syncs.py` | 30 KB | ~850 | Daily sync orchestration |
| `services/agent_tools.py` | 29 KB | ~800 | Agent tool definitions |
| `services/query_router.py` | 28 KB | ~800 | Query routing |
| `services/claude_orchestrator.py` | 27 KB | ~617 | Claude Code orchestration |
