# LifeOS Infrastructure Audit

Deep audit of deployment, scheduling, automation, and operational infrastructure.

---

## Table of Contents

1. [Service Architecture Overview](#1-service-architecture-overview)
2. [Process Management (launchd)](#2-process-management-launchd)
3. [Scheduling & Cron](#3-scheduling--cron)
4. [Sync Infrastructure](#4-sync-infrastructure)
5. [Server Management](#5-server-management)
6. [ChromaDB Management](#6-chromadb-management)
7. [Deployment Pipeline](#7-deployment-pipeline)
8. [Monitoring & Health Checks](#8-monitoring--health-checks)
9. [Backup & Recovery](#9-backup--recovery)
10. [Log Management](#10-log-management)
11. [Secrets & Configuration](#11-secrets--configuration)
12. [Local LLM Infrastructure (Ollama)](#12-local-llm-infrastructure-ollama)
13. [Virtual Environment & Dependencies](#13-virtual-environment--dependencies)
14. [Git Hooks & CI](#14-git-hooks--ci)
15. [Auxiliary Services](#15-auxiliary-services)
16. [Infrastructure Gaps & Risks](#16-infrastructure-gaps--risks)
17. [Hardware Upgrade Opportunities](#17-hardware-upgrade-opportunities)
18. [Recommended Improvements](#18-recommended-improvements)

---

## 1. Service Architecture Overview

### Running Services

The system operates as a constellation of processes managed through different mechanisms:

| Service | Port | Manager | Auto-Restart | Purpose |
|---------|------|---------|--------------|---------|
| LifeOS API (FastAPI) | 8000 | launchd (`com.lifeos.api`) | Yes (KeepAlive) | Core API server |
| ChromaDB | 8001 | cron watchdog (every 5 min) | Yes (watchdog) | Vector store |
| Ollama | 11434 | launchd (`homebrew.mxcl.ollama`) | Yes (KeepAlive) | Local LLM inference |
| Claude Bridge | 8008 | launchd (`com.nathan.claude-bridge`) | Yes (KeepAlive) | Claude CLI bridge |
| Omi Sync | - | launchd (`com.omi-sync`) | Every 15 min | Omi device sync |

### Data Stores

| Database | Size | Purpose |
|----------|------|---------|
| `crm.db` | 556 MB | Main CRM (people, sources, relationships) |
| `bm25_index.db` | 272 MB | BM25 keyword search index |
| `interactions.db` | 260 MB | Email/message/call interactions |
| `imessage.db` | 80 MB | iMessage cache |
| `chromadb/` | 1.1 GB | Vector embeddings (multiple collections) |
| `conversations.db` | 1.6 MB | Chat conversation history |
| `sync_health.db` | 124 KB | Sync status tracking |
| `usage.db` | 60 KB | API usage tracking |
| **Total data/** | ~2.3 GB | All databases combined |

### What Works Well

- Clean separation of concerns: each service is independently managed
- Multiple process management strategies used appropriately (launchd for persistent, cron for periodic)
- All services bind to correct interfaces (0.0.0.0 for Tailscale-accessible API, localhost for internal services)
- Tailscale integration for remote access

---

## 2. Process Management (launchd)

### com.lifeos.api.plist

**Location**: `~/Library/LaunchAgents/com.lifeos.api.plist`

```
RunAtLoad: true
KeepAlive: SuccessfulExit = false (restarts on crash, not clean exit)
ThrottleInterval: 10 seconds
LimitLoadToSessionType: Aqua (GUI session)
```

**Binary**: `/Users/nathanramia/Applications/LifeOS.app/Contents/MacOS/LifeOS`

Note: The LifeOS.app binary referenced in the plist does NOT exist on disk. The launchd agent is currently showing exit code 78 (configuration error), meaning the API server is NOT being managed by launchd -- it's being run manually via `server.sh`. This is a significant gap.

**Strengths**:
- KeepAlive on non-zero exit ensures crash recovery
- ThrottleInterval prevents restart loops
- Aqua session gives better permissions
- Environment sets OBJC_DISABLE_INITIALIZE_FORK_SAFETY for macOS safety

**Gaps**:
- The LifeOS.app wrapper binary doesn't exist -- launchd can't actually start the server
- No resource limits (memory, CPU)
- No WatchPaths for auto-restart on config changes
- No StartInterval fallback if process dies between checks

### com.lifeos.crm-sync.plist

**Location**: `~/Library/LaunchAgents/com.lifeos.crm-sync.plist`

```
StartCalendarInterval: 3:00 AM daily
RunAtLoad: false
KeepAlive: false
```

**Binary**: Runs `scripts/run_sync_wrapper.sh --execute --trigger=scheduled`

**Strengths**:
- Uses wrapper script that handles NVMe wake-up
- Telegram alert on failure
- Retry logic (3 attempts with 5s delay)
- Clean separation of concerns

**Gaps**:
- No retry if sync fails (waits until next day)
- RunAtLoad is false -- if machine reboots mid-day, sync doesn't run until next 3 AM
- No jitter/randomization on schedule

### com.lifeos.clear-caches.plist

**Schedule**: Sundays at 4:00 AM

Clears application caches (Google, Spotify, Homebrew, pip, etc.). Simple maintenance task.

---

## 3. Scheduling & Cron

### Cron Jobs

```
*/5 * * * *  chromadb-watchdog.sh    # ChromaDB health check every 5 min
50 2 * * *   run_sync_with_fda.sh    # FDA syncs at 2:50 AM
```

### Schedule Timeline (Daily)

```
2:50 AM  - FDA syncs via cron (phone calls, iMessage via Terminal.app)
3:00 AM  - Main sync via launchd (Gmail, Calendar, LinkedIn, Contacts, WhatsApp, etc.)
4:00 AM  - Cache cleanup (Sundays only)
Every 5m - ChromaDB watchdog
Every 15m - Omi sync
```

### What Works Well

- FDA sync runs 10 minutes before main sync, so main sync can skip already-completed sources
- ChromaDB watchdog provides reliable auto-recovery
- NVMe wake-up logic handles external drive sleep

### Gaps

- No API server watchdog (if launchd fails, only manual restart)
- No Ollama watchdog (relies on Homebrew launchd which has less visibility)
- No health check that verifies ALL services are up together
- No scheduling for backup operations beyond pre-sync interaction backup

---

## 4. Sync Infrastructure

### Architecture

The sync system is the most sophisticated infrastructure component. It operates in 6 phases:

1. **Data Collection**: Pull from Gmail, Calendar, LinkedIn, Contacts, WhatsApp, Slack
2. **Entity Processing**: Link source entities to canonical PersonEntity records
3. **Relationship Building**: Discover relationships, compute strength scores
4. **Vector Store Indexing**: Reindex vault content and CRM data
5. **Content Sync**: Pull Google Docs/Sheets into vault
6. **Post-Sync Cleanup**: Auto-hide non-humans, queue duplicate review

### Key Files

| File | Purpose |
|------|---------|
| `scripts/run_all_syncs.py` | Master sync orchestrator (30KB, 809 lines) |
| `scripts/run_fda_syncs.py` | FDA-required syncs (phone/iMessage) |
| `scripts/run_sync_wrapper.sh` | NVMe pre-flight + Telegram alert on failure |
| `scripts/run_sync_with_fda.sh` | Opens Terminal.app for FDA permission |
| `api/services/sync_health.py` | Sync health tracking database |

### Sync Sources (20 total)

**Phase 1 - Data Collection (8 sources)**: gmail, calendar, linkedin, contacts, phone*, imessage*, whatsapp, slack
**Phase 2 - Entity Processing (4)**: link_slack, link_imessage, link_source_entities, photos
**Phase 3 - Relationship Building (3)**: relationship_discovery, strengths, push_birthdays
**Phase 4 - Vector Store (2)**: vault_reindex, crm_vectorstore
**Phase 5 - Content Sync (2)**: google_docs, google_sheets
**Phase 6 - Cleanup (1)**: entity_cleanup

*phone and imessage require Full Disk Access, run separately via cron

### Sync Health Tracking

- SQLite database (`sync_health.db`) records every sync run
- Tracks: start time, duration, records processed/created/updated, errors
- Stale threshold: 24 hours without successful sync
- Summary endpoint: `/api/crm/sync/health/summary`
- Telegram notifications after every sync run (success or failure)
- Markdown error log in Obsidian vault for visibility

### What Works Well

- Phased ordering ensures upstream data is fresh for downstream processing
- Per-source timeouts (default 60 min, vault_reindex unlimited)
- Recently-synced detection (skips if synced <1 hour ago)
- Output parsing extracts stats from subprocess stdout
- Pre-sync backup of interactions.db
- Categorized stats tracking (people/interactions/source entities by source)
- Work integration safety toggles (disabled by default)

### Gaps

- **No parallelism**: All syncs run sequentially. Phase 1 sources are independent and could run in parallel
- **No task queue**: Syncs are subprocess.run() calls -- blocking, no retry, no priority
- **Single-process**: If one sync hangs, it blocks all subsequent syncs
- **No incremental progress reporting**: Long syncs (vault_reindex) provide no progress updates
- **No manual trigger from UI**: Must use CLI to trigger sync
- **No partial retry**: If sync #15 fails, can't retry just that source easily from the scheduler
- **60-minute timeout per source**: Some syncs might legitimately need more time

---

## 5. Server Management

### scripts/server.sh

Provides start/stop/restart/status/wait commands.

**Startup Process**:
1. Check ChromaDB is running (auto-start if not)
2. Kill any existing server processes (by port + by process name)
3. Clean HuggingFace lock files
4. Start uvicorn via nohup with `~/.venvs/lifeos/bin/python`
5. Wait up to 180 seconds for health check
6. Report Tailscale URL if available

**Strengths**:
- Multi-method process kill (by port, by name, lock file cleanup)
- ChromaDB dependency check with auto-start
- 180-second timeout accommodates ML model loading
- Proper 0.0.0.0 binding for Tailscale access
- Status command shows binding info

**Gaps**:
- Uses `kill -9` as first resort (no graceful SIGTERM first)
- No PID file management (relies on lsof)
- No log rotation (server.log is already 20 MB)
- Post-commit hook restarts server on every commit (slow development cycle)
- Startup time of 30-60 seconds is significant for development iteration

---

## 6. ChromaDB Management

### scripts/chromadb.sh

Manages ChromaDB as a standalone process on port 8001.

**Data location**: `data/chromadb/` (1.1 GB)
**Binary**: `~/.venvs/lifeos/bin/chroma`

### scripts/chromadb-watchdog.sh

Runs every 5 minutes via cron. If ChromaDB is not responding, restarts it via `chromadb.sh restart`.

**Strengths**:
- PID file tracking with fallback to port-based detection
- Watchdog provides reliable auto-recovery
- Logged restarts for debugging

**Gaps**:
- ChromaDB runs on localhost only (not accessible via Tailscale, which is correct for internal use)
- No data persistence verification after restart
- No collection health check (heartbeat only checks process is alive)
- Single-node ChromaDB has no replication
- 1.1 GB data with no backup strategy specific to ChromaDB

---

## 7. Deployment Pipeline

### scripts/deploy.sh

5-step deployment:
1. Run smoke tests (unit + critical browser test)
2. Restart server (via server.sh)
3. Verify health check + API response
4. Commit changes (git add -A, auto-generated message if not provided)
5. Push to remote

**Strengths**:
- Smoke tests gate deployment
- Health verification after restart
- API endpoint test
- Tailscale URL shown in summary
- `--skip-tests` and `--no-push` flags for flexibility

**Gaps**:
- `git add -A` stages everything (could accidentally commit sensitive files)
- No rollback mechanism if deployment fails after commit
- No blue-green or canary deployment
- No version tagging
- No changelog generation
- Auto-generated commit messages are generic ("Deploy: Update N file(s)")

---

## 8. Monitoring & Health Checks

### Health Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Basic check (API key configured) |
| `GET /health/full` | Comprehensive check (tests all API endpoints) |
| `GET /health/services` | Service registry status |
| `GET /api/crm/sync/health` | Per-source sync status |
| `GET /api/crm/sync/health/summary` | Aggregate sync health |
| `GET /api/crm/data-health` | Data quality statistics |

### Service Health Registry (`service_health.py`)

Tracks 9 services with severity levels:

- **CRITICAL** (immediate alert): chromadb, embedding_model, vault_filesystem
- **WARNING** (nightly batch): ollama, bm25_index, google_calendar, google_gmail, backup_storage
- **INFO** (log only): telegram

Features:
- Degradation event recording (when fallbacks are used)
- Thread-safe state tracking
- Alert rate limiting (5-minute cooldown, state-transition only)

### Notification System (`notifications.py`)

- In-memory failure tracker with 24-hour retention
- Email alerts for critical failures
- Telegram as notification channel (sync summaries, alerts)
- Markdown error log in Obsidian vault

### What Works Well

- Comprehensive health checking at multiple levels
- Service-specific severity levels with appropriate escalation
- Degradation tracking (not just up/down, but "degraded with fallback")
- Multiple notification channels (email + Telegram)

### Gaps

- **No external monitoring**: All monitoring is self-contained. If the API server goes down, monitoring goes with it
- **No uptime tracking**: No historical availability metrics
- **No dashboard**: Health data is API-only, no visual dashboard
- **In-memory failure log**: Lost on server restart
- **No alerting for disk space, memory, CPU**
- **No SLA tracking or reporting**
- **Health endpoints don't test ChromaDB collections** (only heartbeat)

---

## 9. Backup & Recovery

### Current State

**Interactions.db backup**: Created before every sync run, keeps 2 most recent, stored at `LIFEOS_BACKUP_PATH` (defaults to `./data/backups`).

**Note**: The `data/backups/` directory does NOT exist on disk, meaning backups are either being created elsewhere or failing silently.

**PersonEntity backup**: "happens automatically on save" (per comments in run_all_syncs.py).

**ChromaDB**: No backup strategy.

### What's NOT Backed Up

- `crm.db` (556 MB) -- the largest and most important database
- `bm25_index.db` (272 MB) -- can be rebuilt but takes time
- `chromadb/` (1.1 GB) -- would need to reindex all vault content
- `conversations.db` -- chat history
- `sync_health.db` -- sync tracking history
- Configuration files (.env, config/*.json, config/*.yaml)
- Obsidian vault (managed externally)

### Disaster Recovery

If the machine dies, recovery would require:
1. Git clone (code only)
2. Rebuild all databases from scratch (sync all sources)
3. Reindex vault into ChromaDB
4. Reconfigure .env and all config files
5. Re-authenticate Google APIs

**Estimated recovery time**: Hours to days depending on data volumes.

### Critical Gaps

- **No automated backup of crm.db** (the most valuable database at 556 MB)
- **No off-machine backups** (everything is on the same disk)
- **No backup verification** (are backups valid/restorable?)
- **No point-in-time recovery** for SQLite databases
- **backup_path defaults to `./data/backups`** which is inside the .gitignored data/ directory but doesn't exist
- **ChromaDB has zero backup** -- 1.1 GB of embeddings would need full reindexing

---

## 10. Log Management

### Log Files

| File | Size | Rotation |
|------|------|----------|
| `logs/server.log` | 20 MB | None (grows unbounded) |
| `logs/crm-sync.log` | 72 KB | None |
| `logs/lifeos-api.log` | 3.5 KB | newsyslog (if configured) |
| `logs/lifeos-api-error.log` | 3.7 KB | newsyslog (if configured) |
| `logs/chromadb.log` | 2.5 KB | None |
| `logs/sync_*.log` | Many files | Per-run (never cleaned up) |
| `logs/slack_sync.log` | 6 MB | None |
| `logs/fda_sync_*.log` | Per-run | Never cleaned up |

### What Works Well

- Separate log files per service
- Per-sync-run log files with timestamps
- Error log separation (lifeos-api-error.log)
- newsyslog config available (100MB max, 5 archives)

### Gaps

- **server.log grows unbounded** (already 20 MB, will grow indefinitely)
- **sync log files accumulate** -- each daily sync creates a new timestamped file, never cleaned up
- **newsyslog setup is optional** and may not be configured
- **No log aggregation** -- must check multiple files to debug issues
- **No structured logging** -- plain text makes parsing difficult
- **No log shipping** to external system for analysis

---

## 11. Secrets & Configuration

### Secrets Storage

All secrets are in `.env` (35 lines):
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET`
- Google OAuth tokens in `config/token-*.json`
- Google credentials in `config/credentials-*.json`

### Configuration Files

| File | Purpose | Sensitive |
|------|---------|-----------|
| `.env` | API keys, tokens, feature flags | Yes |
| `config/people_dictionary.json` | Known people and aliases | Yes (PII) |
| `config/family_members.json` | Family relationships | Yes (PII) |
| `config/relationship_overrides.json` | Manual relationship adjustments | Yes |
| `config/credentials-*.json` | Google OAuth credentials | Yes |
| `config/token-*.json` | Google OAuth tokens | Yes |
| `config/settings.py` | Pydantic settings with defaults | No |
| `config/crm_settings.yaml` | CRM configuration | Mild |
| `config/launchd/*.plist.template` | Service config templates | No |

### What Works Well

- `.gitignore` comprehensively excludes sensitive files
- Example files provided for personal configs (`.example.json`)
- Pydantic settings with validation and defaults
- Work integration toggles default to disabled (safe-by-default)

### Gaps

- **Secrets in plain text files** -- no encryption at rest
- **No secret rotation mechanism**
- **Google OAuth tokens auto-refresh** but credentials are static
- **No backup of config files** (if `.env` is lost, significant reconfiguration needed)
- **Secrets loaded from .env via dotenv** -- readable by any process with file access
- **Launchd plist files contain hardcoded paths** (not portable)

---

## 12. Local LLM Infrastructure (Ollama)

### Current Setup

- **Manager**: Homebrew launchd service (`homebrew.mxcl.ollama`)
- **Models installed**: qwen2.5:7b-instruct (4.7 GB), llama3.2:3b (2.0 GB)
- **Port**: 11434 (default)
- **Usage**: Query routing and intent classification

### Fallback Chain

When Ollama is unavailable:
1. Try Ollama (local, fast, free)
2. Fall back to Haiku LLM via API
3. Fall back to pattern matching (regex-based intent classification)

### What Works Well

- Graceful degradation chain (Ollama -> Haiku -> patterns)
- Small efficient models appropriate for classification tasks
- Managed by Homebrew with auto-start

### Gaps

- **No GPU acceleration** (current Mac Mini likely CPU-only for Ollama)
- **Only 2 small models** -- limited local capability
- **No model version pinning** (Ollama auto-updates can break things)
- **No watchdog** for Ollama specifically (relies on Homebrew launchd)
- **No embedding via local models** (uses sentence-transformers which is CPU-based PyTorch)
- **No local synthesis capability** (always calls Claude API for answers)
- **7B model with 45s timeout** suggests slow inference

---

## 13. Virtual Environment & Dependencies

### Setup

- **Location**: `~/.venvs/lifeos` (external to project directory)
- **Reason**: macOS TCC security scanning causes 30+ second delays for venvs in `~/Documents/`
- **Python**: Uses system Python via venv
- **Dependencies**: `requirements.txt` (51 packages)

### Key Dependencies

- **torch** (large): Required for sentence-transformers, significant disk and memory footprint
- **sentence-transformers**: mxbai-embed-large-v1 (1024-dim embeddings)
- **chromadb**: Client library for vector store
- **playwright**: Browser testing

### NVMe Dependency

The system has a critical dependency on an external NVMe drive:
- `/opt/homebrew` is a symlink to the NVMe
- The Python venv links through Homebrew
- At 3 AM the NVMe may be asleep, requiring wake-up logic in `run_sync_wrapper.sh`
- Failure to wake results in Telegram alert

### Gaps

- **No lock file** (requirements.txt only, no pip freeze output)
- **NVMe single point of failure** -- if the drive fails, everything breaks (Python, Homebrew, Ollama models)
- **No version pinning** -- `>=` constraints mean installs can vary
- **No health check for the venv itself**
- **torch is CPU-only** -- massive package with no GPU benefit currently

---

## 14. Git Hooks & CI

### Pre-commit Hook

Runs fast unit tests (`pytest -m "unit and not slow"`). Blocks commit on failure.

### Post-commit Hook

Auto-restarts server after every commit via `server.sh restart`. Takes 30-60 seconds.

### What Works Well

- Pre-commit prevents shipping broken code
- Post-commit ensures running server matches committed code

### Gaps

- **No CI/CD pipeline** -- no GitHub Actions, no external testing
- **Post-commit restart is slow** -- 30-60 second delay after every commit
- **No linting or formatting enforcement** (no black, ruff, mypy)
- **No dependency vulnerability scanning**
- **No automated testing beyond pre-commit**

---

## 15. Auxiliary Services

### Claude Bridge (port 8008)

A separate FastAPI service (`claude_bridge_server`) that bridges Claude CLI access. Runs as a launchd service with KeepAlive. Appears to be a custom tool for Claude Code integration.

### Omi Sync

Syncs data from Omi AI device every 15 minutes via launchd. Currently showing exit code -6 (likely crashing/disabled).

### Claude Code Orchestrator

Built into the LifeOS API (`api/services/claude_orchestrator.py`). Spawns headless Claude CLI sessions for task execution triggered via Telegram. Features:
- Session management with UUID tracking
- Plan-then-implement workflow
- `[NOTIFY]` line extraction for Telegram relay
- `[CLARIFY]` support for interactive clarification
- Cost limits ($2 per session default)
- Turn limits (50 max turns)
- Timeout safety net (3600s)

### Resilience Utilities

`api/services/resilience.py` provides:
- Async retry decorator with exponential backoff
- Configurable retry parameters (max retries, base/max delay)
- ServiceUnavailableError and PartialResultError types

---

## 16. Infrastructure Gaps & Risks

### Critical Risks

1. **LifeOS.app doesn't exist**: The launchd plist references a binary that doesn't exist. The API server has no reliable auto-start mechanism -- it must be manually started via `server.sh`.

2. **No backups directory exists**: The backup path `data/backups/` doesn't exist on disk. Either backups are silently failing, or the path was changed and not updated.

3. **Single machine, no redundancy**: All data, services, and compute on one machine. Disk failure = total data loss.

4. **NVMe external drive as critical dependency**: Python, Homebrew, and model weights all depend on the NVMe. If it disconnects or fails, the entire system is dead.

5. **No task queue / job runner**: All work happens synchronously in the API process or as blocking subprocess calls. No Celery, RQ, or equivalent for long-running tasks.

6. **No background worker**: No separate worker process for async task execution, batch processing, or scheduled jobs.

### High-Priority Gaps

7. **No external monitoring**: If the API server dies, all monitoring dies with it.
8. **No database backups** for crm.db (556 MB of relationship data), ChromaDB (1.1 GB of embeddings), or config files.
9. **No crash recovery for API server** (launchd plist is broken).
10. **server.log grows unbounded** (20 MB and climbing).
11. **sync log files accumulate forever**.
12. **No structured logging** for programmatic analysis.
13. **In-memory failure tracker** lost on restart.

### Medium-Priority Gaps

14. No parallel sync execution (phases could overlap).
15. No UI for triggering or monitoring syncs.
16. No Docker containerization.
17. No version tagging or changelog.
18. No CI/CD pipeline.
19. No dependency lock file.
20. No GPU acceleration for any workload.

---

## 17. Hardware Upgrade Opportunities

### Corsair AI Workstation 300 Capabilities

With powerful GPU(s), significantly more RAM, and fast local storage, the following becomes possible:

### GPU-Accelerated Workloads

1. **Local embedding generation**: Replace CPU-based sentence-transformers with GPU-accelerated model. Current model (mxbai-embed-large-v1, 1024-dim) would run 10-50x faster on GPU, dramatically reducing reindex time.

2. **Local LLM inference**: Move from 7B models to 70B+ parameter models (e.g., Llama 3.1 70B, Qwen 72B, Mixtral 8x22B) for:
   - Query routing (currently Ollama 7B)
   - Intent classification
   - Fact extraction from messages
   - Partial synthesis (reduce Claude API costs)
   - Summarization of meeting notes
   - Entity resolution improvements

3. **Local synthesis for non-sensitive queries**: Use local 70B model for synthesis when the query doesn't require Claude's capabilities, dramatically reducing API costs.

4. **Image understanding**: Process photos for context (scene description, text extraction from screenshots) using multimodal local models.

5. **Speech-to-text**: Whisper models for processing voice memos, meeting recordings, phone call transcriptions.

### Memory & Storage Improvements

6. **In-memory ChromaDB**: With 64+ GB RAM, keep hot ChromaDB collections in memory for sub-millisecond vector search.

7. **Larger BM25 index**: Current 272 MB index could be expanded with more content indexed.

8. **Database caching**: Keep SQLite databases in memory-mapped mode for faster queries.

9. **Local NVMe storage**: Eliminate the external NVMe dependency. All data, models, and venvs on fast internal storage.

### Always-On Architecture

10. **Process supervisor**: Replace launchd with systemd (if Linux) or a proper process manager (supervisord, PM2) for reliable service lifecycle management.

11. **Health check daemon**: Independent process that monitors all services and can restart them.

12. **Task queue**: Redis + Celery (or Dramatiq, Huey) for background task execution:
    - Sync operations as async tasks
    - Claude Code sessions as queue jobs
    - Reindexing as background tasks
    - Scheduled jobs with retry logic

13. **Background worker pool**: Multiple worker processes for parallel execution of independent tasks.

### Local LLM Upgrade Path

| Model | Size | VRAM | Use Case |
|-------|------|------|----------|
| Qwen 2.5 72B | 40 GB | 48 GB | General synthesis, fact extraction |
| Llama 3.1 70B | 38 GB | 48 GB | Coding tasks, analysis |
| Mixtral 8x22B | ~90 GB | 2x GPU | MoE for diverse tasks |
| DeepSeek V3 | varies | varies | Reasoning, planning |
| Whisper Large V3 | 3 GB | 4 GB | Speech-to-text |
| CLIP/SigLIP | 2 GB | 4 GB | Image understanding |

With a high-end GPU (e.g., RTX 4090 with 24GB, or RTX 5090 with 32GB), a 70B model quantized to 4-bit would fit comfortably.

---

## 18. Recommended Improvements

### Tier 1: Fix Critical Issues (Do First)

1. **Fix the LifeOS.app launchd entry or replace it**: Either rebuild the app wrapper or update the plist to directly invoke the Python server. The API server currently has no auto-start on boot.

2. **Create the backups directory and verify backup functionality**: `mkdir -p data/backups` and test that `InteractionStore.create_backup()` actually works.

3. **Add automated database backups**: Daily backup of crm.db, interactions.db, and ChromaDB data to a separate location (ideally off-machine).

4. **Add log rotation**: Configure newsyslog or logrotate for server.log (already 20 MB). Add cleanup for timestamped sync logs older than 30 days.

5. **Add server watchdog**: Either fix the launchd plist or add a cron-based watchdog similar to chromadb-watchdog.sh.

### Tier 2: Reliability Improvements

6. **Task queue**: Add Redis + a task queue library (Celery, Dramatiq, or Huey) for:
   - Background sync execution
   - Claude Code task orchestration
   - Scheduled jobs with retry
   - Priority queuing

7. **Parallel sync execution**: Phase 1 sources (Gmail, Calendar, LinkedIn, etc.) are independent. Run them concurrently using asyncio.gather() or task queue workers.

8. **External monitoring**: Set up an external health check (e.g., Uptime Kuma, Healthchecks.io) that pings `/health` and alerts if it's down.

9. **Structured logging**: Switch to JSON logging (structlog or python-json-logger) for better parsing and analysis.

10. **Database WAL mode**: Enable SQLite WAL mode for concurrent read/write performance.

### Tier 3: Hardware Upgrade Preparation

11. **Containerize with Docker Compose**: Create Dockerfile and docker-compose.yml for:
    - LifeOS API
    - ChromaDB
    - Redis (for task queue)
    - Worker processes

    This makes the system portable and reproducible.

12. **GPU-accelerated embeddings**: Add CUDA-compatible torch and use GPU for embedding generation. This alone could cut reindex time by 10-50x.

13. **Larger local models**: After hardware upgrade, switch Ollama to 70B models for improved local inference.

14. **Local synthesis engine**: Add a configurable LLM backend that can use local models for synthesis instead of always calling Claude API.

15. **Eliminate NVMe dependency**: On the new workstation, use internal storage for everything. Remove all NVMe wake-up logic.

### Tier 4: Advanced Architecture

16. **Service mesh**: Replace ad-hoc process management with a proper orchestrator:
    - Docker Compose for development
    - Potentially Kubernetes for production (may be overkill for single-machine)
    - Or supervisord for simpler process management

17. **Event-driven sync**: Instead of nightly batch sync, use webhooks/streaming for real-time data ingestion:
    - Gmail push notifications
    - Calendar webhook events
    - Slack real-time events API

18. **Background worker architecture**: Separate API server from background processing:
    ```
    API Server (FastAPI) -> Message Queue (Redis) -> Workers (N processes)
    ```
    Workers handle: syncs, reindexing, Claude tasks, fact extraction, embedding generation.

19. **Backup to cloud storage**: Encrypted backups to S3/B2/GCS for off-machine disaster recovery.

20. **Multi-model inference server**: Run vLLM or TGI for high-throughput local inference with batching, quantization, and model management.

---

## Summary

LifeOS has a remarkably complete infrastructure for a self-hosted system, with thoughtful solutions for macOS-specific challenges (FDA permissions, NVMe sleep, TCC scanning). The sync system is well-architected with phased execution, health tracking, and Telegram notifications.

The most critical gaps are:
1. **Broken launchd auto-start** (LifeOS.app doesn't exist)
2. **Missing database backups** (556 MB of irreplaceable CRM data)
3. **No task queue** for background work
4. **No external monitoring**

The hardware upgrade to the Corsair AI Workstation 300 opens transformative possibilities: local 70B models for synthesis (reducing API costs), GPU-accelerated embeddings (10-50x faster reindexing), and enough resources to run a proper task queue with background workers. The system could evolve from a scheduled-batch architecture to a real-time, always-on AI assistant that processes data as it arrives.
