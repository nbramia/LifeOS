# LifeOS Cross-Pollination: Infrastructure Perspective

**Auditor:** Infrastructure Specialist (Round 2)
**Date:** 2026-02-13
**Input:** All five Round 1 audits (backend, frontend, MCP, Telegram/chat, infrastructure)

---

## 1. Hardware Upgrade Master Plan: Corsair AI Workstation 300

The new hardware isn't just "faster" -- it fundamentally changes what's architecturally possible. Every audit proposes improvements that bottleneck on compute. Here's how to map them to hardware.

### GPU Allocation Plan

Assuming RTX 5090 (32 GB VRAM) or RTX 4090 (24 GB VRAM):

| Workload | VRAM | Source Audit | Current State | Post-Upgrade |
|----------|------|-------------|---------------|--------------|
| **Local LLM (Qwen 2.5 72B Q4)** | ~20 GB | Backend, Telegram, Infra | 7B CPU model, 45s timeout | 72B GPU, <3s routing |
| **Embedding generation** | ~2 GB | Backend, Infra | CPU (mxbai-embed-large-v1), minutes per batch | GPU, 10-50x faster |
| **Cross-encoder reranker** | ~1 GB | Backend | CPU (MiniLM-L6), lightweight | GPU, upgrade to larger model |
| **Whisper Large V3** | ~3 GB | Telegram | Not available | Local STT for voice messages |
| **Vision model (LLaVA/SigLIP)** | ~4 GB | Backend, Telegram | Not available | Photo analysis, OCR, screenshots |

**VRAM budget**: ~30 GB total. Fits a 32 GB card with room to spare. On a 24 GB card, the 72B model drops to ~13B-34B unless quantized aggressively (3-bit), or you run two cards.

**Key insight**: The LLM and embedding model don't need to run simultaneously at peak. Embedding batches run during sync (3 AM). LLM inference runs during user queries (daytime). A simple scheduling approach can time-share the GPU between them, or dedicate VRAM regions.

**Recommendation**: RTX 5090 (32 GB) is the sweet spot. Single card simplifies management. If budget allows, dual 4090s (48 GB combined) enable running the 72B model AND embedding/Whisper simultaneously, but multi-GPU adds complexity (tensor parallelism, NCCL).

### CPU Allocation Plan

Assuming 16+ core CPU (e.g., Ryzen 9 / Intel i9):

| Workload | Cores | Source Audit | Notes |
|----------|-------|-------------|-------|
| **FastAPI server** | 2-4 | Backend | uvicorn workers, async I/O |
| **Background task workers** | 4-6 | All audits | Celery/Dramatiq workers for sync, fact extraction, briefings |
| **ChromaDB** | 2 | Backend, Infra | HNSW index operations |
| **Sync operations** | 2-4 | Infra | Parallel phase 1 sources |
| **Claude Code sessions** | 1-2 | Telegram | Subprocess management |
| **Ollama/vLLM inference** | 2-4 | Backend, Infra | CPU component of LLM serving |

**Key insight**: The current system is single-threaded by necessity (Mac Mini constraints). With 16+ cores, parallelism becomes the default. The sync pipeline (backend audit: "no parallelism, all syncs run sequentially") and the task queue (every audit: "no background task queue") are the primary beneficiaries.

### RAM Allocation Plan

Assuming 64-128 GB:

| Workload | RAM | Notes |
|----------|-----|-------|
| **ChromaDB hot cache** | 8-16 GB | 1.1 GB data fully in-memory, plus index structures |
| **SQLite memory-mapped** | 4-8 GB | crm.db (556 MB) + bm25_index.db (272 MB) + interactions.db (260 MB) |
| **LLM model loading** | 4-8 GB | CPU-side model weights, KV cache overflow |
| **Embedding model** | 2-4 GB | PyTorch model in system RAM |
| **Python workers** | 8-16 GB | Multiple worker processes |
| **OS + services** | 4-8 GB | Linux overhead |

**64 GB is sufficient. 128 GB provides headroom** for keeping all databases memory-mapped and running multiple workers without contention.

### Storage Plan

| Data | Size | Speed Need | Recommendation |
|------|------|-----------|----------------|
| **ChromaDB** | 1.1 GB (growing) | High (search latency) | NVMe internal |
| **SQLite databases** | ~1.2 GB | High (query latency) | NVMe internal |
| **LLM model weights** | ~40-50 GB | Medium (loaded once) | NVMe internal |
| **Obsidian vault** | Variable | Medium | NVMe internal |
| **Logs + backups** | ~10 GB+ | Low | SATA SSD or secondary NVMe |
| **Backup archive** | Growing | Low | External drive + cloud (B2/S3) |

**Key insight from infra audit**: The current external NVMe dependency is a critical single point of failure. Everything moves to internal NVMe on the workstation. The external NVMe wake-up logic (`run_sync_wrapper.sh`) and Homebrew symlink chain are eliminated entirely.

---

## 2. The Always-On Architecture

Every audit proposes new capabilities that need to run 24/7. Here's the unified architecture.

### Current State (Mac Mini)

```
launchd (broken) -> server.sh (manual) -> uvicorn (single process)
                 -> cron (chromadb watchdog, FDA sync)
                 -> launchd (Ollama, Claude Bridge, Omi)
```

Problems identified across audits:
- LifeOS.app binary doesn't exist (infra audit): no auto-start
- Single uvicorn process handles everything (backend audit): no worker separation
- No task queue (all five audits): blocking operations everywhere
- No external monitoring (infra audit): server death = monitoring death

### Target State (Workstation)

```
systemd (or supervisord)
    |
    +-- lifeos-api (FastAPI, 2-4 uvicorn workers)
    |       Handles: HTTP requests, SSE streaming, health endpoints
    |
    +-- lifeos-worker (Dramatiq/Celery workers, 4-6 processes)
    |       Handles: sync jobs, fact extraction, briefing generation,
    |                embedding batches, relationship discovery
    |
    +-- chromadb (vector store, port 8001)
    |
    +-- redis (message broker + cache, port 6379)
    |
    +-- vllm / ollama (LLM inference, port 11434)
    |       Serves: Qwen 72B for routing/classification/synthesis
    |       Also: Whisper, vision models on demand
    |
    +-- lifeos-scheduler (cron-like, triggers workers)
    |       Handles: nightly sync, reminder execution, proactive checks
    |
    +-- lifeos-monitor (independent health daemon)
            Handles: watchdog for all services, external ping,
                     Telegram alerts, disk/memory/CPU monitoring
```

### Why This Architecture

1. **API and workers are separated** (addresses backend audit's "single-threaded bottlenecks" and telegram audit's "no long-running task queue"). API handles requests fast; workers handle compute.

2. **Redis serves double duty**: message broker for task queue AND caching layer (addresses backend audit's "no caching layer"). People lookups, recent conversations, calendar data cached in Redis with TTL.

3. **Independent monitor** (addresses infra audit's "no external monitoring"). If the API dies, the monitor still runs and alerts via Telegram. Simple Python script that pings `/health` every 60 seconds.

4. **vLLM replaces Ollama** for production inference. vLLM provides: batched inference, continuous batching, PagedAttention for efficient VRAM use, OpenAI-compatible API. Ollama is great for development; vLLM is better for always-on serving.

### Process Management: systemd vs supervisord

If the workstation runs **Linux** (likely Ubuntu/Debian for CUDA support):
- **systemd** is the natural choice. Native, well-documented, supports dependencies, restart policies, resource limits, journald logging.
- Each service gets a `.service` unit file with `Restart=on-failure`, `After=redis.service`, etc.

If staying on **macOS** (less likely for a dedicated workstation):
- **supervisord** replaces the broken launchd setup. Single config file, reliable restart, log management.
- Or fix launchd properly (rebuild LifeOS.app wrapper or update plist to call Python directly).

**Recommendation**: Linux (Ubuntu 24.04 LTS) on the workstation. macOS-specific code (iMessage, Apple Photos, Apple Contacts, Full Disk Access) stays on the Mac Mini as a sync source that pushes data to the workstation. This cleanly separates "data collection" (Mac) from "data processing and serving" (workstation).

### The Hybrid Architecture

This is a new insight that only emerges from seeing all five audits:

```
Mac Mini (data collector)              Corsair Workstation (brain)
  - iMessage access (FDA)              - FastAPI + workers
  - Apple Photos (FDA)                 - ChromaDB + Redis
  - Apple Contacts                     - vLLM (72B model)
  - Phone calls (FDA)                  - Embedding generation
  - Omi device sync                    - All search and synthesis
  - Granola meeting notes              - Task queue
                                       - Telegram bot
  Pushes data via API or               - Claude Code orchestration
  shared database sync                 - Web UI serving
```

The Mac Mini becomes a lightweight data ingest node. It runs a slim sync agent that pushes observations to the workstation's API. The workstation handles all heavy compute, inference, and serving. This solves:
- FDA permission issues (Mac keeps them)
- GPU compute (workstation has it)
- Always-on reliability (workstation is a server, not a laptop)
- The NVMe dependency (workstation uses internal storage)

---

## 3. Task Queue Design

This is the single most requested infrastructure component across all five audits.

### Requirements (from all audits)

| Requirement | Source |
|-------------|--------|
| Background sync execution | Infra audit: "no task queue, syncs are subprocess.run()" |
| Parallel Claude Code sessions | Telegram audit: "single session limit" |
| Background fact extraction | Backend audit: "long operations block the request" |
| Background briefing generation | Backend audit: "no background task queue" |
| Reindex as background job | Infra audit: "no manual trigger from UI" |
| Progress tracking | Infra audit: "no incremental progress reporting" |
| Retry logic | Infra audit: "no partial retry" |
| Priority queuing | Telegram audit: "no queue/rate limiting" |
| Scheduled jobs | Infra audit: replace cron-based scheduling |

### Technology Choice: Dramatiq + Redis

**Why Dramatiq over Celery:**
- Simpler API, less boilerplate
- Better error handling (middleware-based retry, dead letter queues)
- Native support for result storage
- Lighter weight (no complex broker configuration)
- Good enough for single-machine deployment (this isn't a distributed system)

**Why not Celery:**
- More complex than needed for a single-machine setup
- Celery's configuration surface area is large
- Beat scheduler is a separate process to manage

**Why not a custom solution:**
- Tempting for simplicity, but reinventing retry logic, worker management, and priority queuing is a time sink
- The audits show the system is already complex enough

### Architecture

```python
# tasks.py -- task definitions
import dramatiq
from dramatiq.brokers.redis import RedisBroker

broker = RedisBroker(host="localhost")
dramatiq.set_broker(broker)

@dramatiq.actor(queue_name="sync", priority=0)
def run_sync_source(source: str, force: bool = False):
    """Run a single sync source as a background task."""
    ...

@dramatiq.actor(queue_name="compute", priority=5)
def extract_person_facts(person_id: str):
    """Extract facts for a person using LLM pipeline."""
    ...

@dramatiq.actor(queue_name="compute", priority=10)
def generate_briefing(person_name: str):
    """Generate a stakeholder briefing."""
    ...

@dramatiq.actor(queue_name="index", priority=0)
def reindex_vault():
    """Full vault reindex."""
    ...

@dramatiq.actor(queue_name="claude", max_retries=0)
def run_claude_code_task(task_text: str, chat_id: str):
    """Execute a Claude Code task."""
    ...
```

### Queue Design

| Queue | Workers | Priority | Purpose |
|-------|---------|----------|---------|
| `sync` | 4 | High (0) | Data sync operations (parallelizable) |
| `compute` | 2 | Medium (5-10) | LLM-heavy tasks (fact extraction, briefings) |
| `index` | 1 | Low (15) | Reindexing (GPU-bound, sequential) |
| `claude` | 2 | Medium (5) | Claude Code sessions (allows 2 concurrent) |
| `default` | 2 | Normal (10) | Everything else |

### Progress Tracking

Dramatiq doesn't have built-in progress tracking (unlike Celery with result backends). Add a lightweight Redis-based progress system:

```python
# progress.py
import redis
import json

r = redis.Redis()

def update_progress(task_id: str, progress: float, status: str, detail: str = ""):
    r.setex(f"task:{task_id}:progress", 3600, json.dumps({
        "progress": progress,
        "status": status,
        "detail": detail,
        "updated_at": time.time()
    }))

def get_progress(task_id: str) -> dict:
    data = r.get(f"task:{task_id}:progress")
    return json.loads(data) if data else None
```

Expose via API: `GET /api/admin/tasks/{task_id}/progress`
Surface in Telegram: periodic progress messages for long-running tasks
Surface in Web UI: progress bars in an admin dashboard (frontend audit's "missing admin dashboard")

### Retry Policy

```python
# Sync tasks: retry 3 times with exponential backoff
@dramatiq.actor(max_retries=3, min_backoff=30000, max_backoff=300000)
def run_sync_source(source: str): ...

# LLM tasks: retry once (expensive)
@dramatiq.actor(max_retries=1, min_backoff=10000)
def extract_person_facts(person_id: str): ...

# Claude Code: no retry (user-facing, interactive)
@dramatiq.actor(max_retries=0)
def run_claude_code_task(task_text: str): ...
```

---

## 4. Local LLM Strategy

With serious GPU power, the local LLM strategy changes from "fallback for routing" to "primary engine for most tasks."

### Model Selection

| Role | Current | Proposed | VRAM | Rationale |
|------|---------|----------|------|-----------|
| **Query routing** | Qwen 2.5 7B (CPU, 45s) | Qwen 2.5 72B Q4 (GPU, <1s) | ~20 GB | 10x better accuracy, 45x faster |
| **Intent classification** | Ollama 7B -> Haiku -> patterns | Same 72B model | Shared | Eliminate the 3-tier fallback chain |
| **Simple synthesis** | Claude Sonnet 4.5 (API) | Qwen 72B (local) | Shared | Zero API cost for "what time is my meeting?" |
| **Complex synthesis** | Claude Sonnet/Opus (API) | Claude Sonnet/Opus (API) | - | Keep API for multi-source, long-context reasoning |
| **Fact extraction** | Claude + Ollama 7B validation | Qwen 72B (local) | Shared | Better extraction, zero API cost |
| **Fact validation** | Ollama 7B | Same 72B model | Shared | Much better validation accuracy |
| **Embedding** | mxbai-embed-large-v1 (CPU) | Same model (GPU) | ~2 GB | Same quality, 10-50x faster |
| **Reranking** | ms-marco-MiniLM-L6 (CPU) | Upgrade to ColBERT v2 (GPU) | ~1 GB | Better reranking quality |
| **Voice STT** | None | Whisper Large V3 (GPU) | ~3 GB | New capability for Telegram voice |
| **Vision** | None | LLaVA 13B or Qwen-VL (GPU) | ~4 GB | New capability for photo analysis |

### What Stays with Claude API

The backend audit shows Claude is used for:
1. Complex multi-source synthesis (keeps using Claude)
2. Agent loop tool selection (could move to local for simple queries)
3. System prompt + RAG synthesis (split: simple local, complex API)
4. Claude Code orchestration (must stay with Claude -- it IS Claude)

**Decision framework:**
- **Local**: Routing, classification, intent detection, fact extraction, simple Q&A, summarization, embedding, STT
- **Claude API**: Complex reasoning, multi-step agentic tasks, long-context synthesis (>8K tokens), Claude Code sessions, anything requiring tool use with high reliability

### Cost Savings Estimate

From the backend audit, model pricing is:
- Sonnet: $3/$15 per million tokens (input/output)
- Opus: $15/$75 per million tokens

The telegram audit shows the "haiku" tier uses Sonnet pricing (no savings). With local LLM handling routing + intent classification + simple synthesis:

| Task | Current Cost | Post-Upgrade Cost | Volume Estimate |
|------|-------------|-------------------|-----------------|
| Query routing | $0.003/query (Haiku fallback) | $0 (local) | ~50 queries/day |
| Intent classification | $0.003/query | $0 (local) | ~50/day |
| Simple synthesis | $0.01/query (Sonnet) | $0 (local) | ~20/day |
| Fact extraction | $0.05/person (Claude + Ollama) | $0 (local) | ~5/day |
| Complex synthesis | $0.03/query (Sonnet) | $0.03 (stays API) | ~30/day |
| **Daily total** | ~$1.50-3.00 | ~$0.90-1.50 | - |
| **Monthly savings** | - | **$18-45/month** | - |

Not transformative in dollar terms, but the bigger win is **latency reduction** (no network round-trip for simple queries) and **independence from API availability**.

### Serving Infrastructure

**vLLM** is the recommended inference server:
- OpenAI-compatible API (drop-in replacement for Ollama)
- Continuous batching (handles multiple concurrent requests)
- PagedAttention (efficient VRAM utilization)
- Quantization support (AWQ, GPTQ, FP8)
- Model loading from HuggingFace

```bash
# Start vLLM serving Qwen 72B
vllm serve Qwen/Qwen2.5-72B-Instruct-AWQ \
    --dtype auto \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.85 \
    --port 11434
```

The existing `ollama_client.py` calls would need minimal changes -- both expose OpenAI-compatible endpoints. The fallback chain (Ollama -> Haiku -> patterns) simplifies to (vLLM -> patterns) since vLLM serving a 72B model is more reliable than Ollama serving a 7B model.

---

## 5. Migration Plan: Mac Mini to Corsair Workstation

### Phase 0: Preparation (Before Hardware Arrives)

1. **Containerize**: Create Docker Compose for LifeOS (API, ChromaDB, Redis). Test on Mac Mini to validate.
2. **Extract macOS dependencies**: Identify all code paths that require macOS-specific access (iMessage, Apple Photos, Apple Contacts, FDA). These stay on Mac.
3. **Create sync agent**: A lightweight script that runs on the Mac Mini and pushes data to the workstation's API.
4. **Document all configuration**: Export `.env`, config files, Google OAuth tokens, Slack credentials.
5. **Create database export scripts**: SQLite dump for all databases, ChromaDB export.

### Phase 1: Workstation Setup (Day 1)

1. Install Ubuntu 24.04 LTS
2. Install NVIDIA drivers + CUDA toolkit
3. Install Docker + Docker Compose
4. Set up Tailscale (same tailnet as Mac Mini)
5. Clone LifeOS repo
6. Install vLLM + download Qwen 72B model
7. Restore databases from Mac Mini backup
8. Configure `.env` and all config files
9. Start Docker Compose stack
10. Verify `/health/full` passes

### Phase 2: Cutover (Day 1-2, Zero Downtime)

The key insight: both machines can run simultaneously during migration.

1. **Mac Mini stays running** serving production traffic
2. **Workstation comes up** with restored data
3. Run sync on workstation to catch up to current state
4. Update Tailscale DNS / MagicDNS to point `lifeos` to workstation
5. Update Telegram bot webhook/polling to connect to workstation
6. Update MCP server config to point to workstation
7. Verify all clients (web UI, Telegram, Claude Code) work against workstation
8. **Mac Mini transitions to sync-agent mode**: disable API server, keep only data collection scripts running
9. Configure Mac Mini sync agent to push data to workstation on schedule

### Phase 3: Post-Migration (Week 1)

1. Monitor workstation stability for 7 days
2. Set up automated backups (local + cloud)
3. Configure systemd service files
4. Set up external monitoring (Uptime Kuma or Healthchecks.io)
5. Performance benchmark: embedding speed, LLM inference latency, search response time
6. Tune vLLM parameters (batch size, max tokens, GPU utilization)

### Risk Mitigation

- **Mac Mini stays available as fallback** for 30 days post-migration
- **Database backups before and after each phase**
- **DNS-based cutover** means instant rollback (point DNS back to Mac Mini)
- **Google OAuth tokens** may need re-authentication on new machine (test this first)
- **iMessage/Photos data** continues flowing from Mac Mini -- no disruption

---

## 6. Security Architecture

The backend audit identifies critical security gaps. With more capabilities comes more attack surface.

### Current Vulnerabilities (from all audits)

| Issue | Source | Severity |
|-------|--------|----------|
| No authentication on API | Backend audit | Critical |
| CORS is `*` (all origins) | Backend audit | Critical |
| No rate limiting | Backend audit | High |
| Claude Code runs `--dangerously-skip-permissions` | Telegram audit | High |
| XSS via innerHTML in frontend | Frontend audit | High |
| CDN dependencies without SRI | Frontend audit | Medium |
| Secrets in plain text `.env` | Infra audit | Medium |
| No CSP headers | Frontend audit | Medium |
| `git add -A` in deploy script | Infra audit | Medium |

### Security Improvements for Workstation

**1. API Authentication (Critical)**

Add API key authentication as a middleware. Every audit mentions the open API.

```python
# middleware/auth.py
API_KEYS = {os.getenv("LIFEOS_API_KEY")}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/health"):
        return await call_next(request)
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if key not in API_KEYS:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return await call_next(request)
```

Scope: API key for external access (Tailscale, MCP, Telegram bridge). Localhost traffic can optionally bypass.

**2. Network Segmentation**

On the workstation with Docker:
- Redis: bind to Docker network only (not exposed to host)
- ChromaDB: bind to Docker network only
- vLLM: bind to Docker network only
- Only the FastAPI API exposed on port 8000 (via Tailscale)

**3. Claude Code Sandboxing**

The telegram audit flags `--dangerously-skip-permissions`. On the workstation:
- Run Claude Code sessions in a Docker container with limited filesystem access
- Mount only the project directory and vault as volumes
- No network access to internal services (Redis, ChromaDB) from the container
- Resource limits (CPU, memory, time)

**4. Secrets Management**

Move from `.env` to a secrets manager:
- **Minimum**: `docker secret` for Docker Compose deployments
- **Better**: HashiCorp Vault or SOPS (encrypted secrets in git)
- **Practical**: At minimum, restrict `.env` file permissions to `600` and ensure it's never committed

**5. Rate Limiting**

The backend audit notes no rate limiting. Add per-endpoint rate limiting:

```python
from slowapi import Limiter
limiter = Limiter(key_func=get_remote_address)

@app.post("/api/ask/stream")
@limiter.limit("30/minute")
async def ask_stream(request: Request): ...

@app.post("/api/ask")
@limiter.limit("30/minute")
async def ask(request: Request): ...
```

This prevents a runaway MCP client or bug from exhausting Claude API credits.

---

## 7. New Ideas from the Full Picture

These only emerge from reading all five audits together.

### 7.1 Unified Event Bus

Every audit describes a different communication pattern:
- Backend: SSE for streaming responses
- Frontend: polling API endpoints, no real-time updates
- Telegram: long-polling getUpdates
- MCP: synchronous request/response
- Infrastructure: cron for scheduling, threads for background work

**Proposal**: A Redis Pub/Sub event bus that all components subscribe to.

```
Events:
  sync.completed.gmail      -> Frontend refreshes, Telegram notifies
  person.updated.{id}       -> CRM invalidates cache, MCP gets fresh data
  task.created.{id}         -> Frontend shows notification, Telegram confirms
  claude_code.progress      -> Telegram sends update, frontend shows status
  health.service.degraded   -> Monitor alerts, frontend shows warning
  message.received.telegram -> Queued for processing
```

This solves:
- Frontend audit's "no real-time updates"
- Telegram audit's "no proactive intelligence"
- Backend audit's "no WebSocket support" (WebSocket subscribes to the event bus)
- MCP audit's "synchronous HTTP client"
- Infra audit's "in-memory failure tracker lost on restart" (events are persisted in Redis)

### 7.2 The Data Freshness Ladder

Currently, data freshness is binary: "synced at 3 AM" or "not synced." The audits reveal different freshness needs:

| Data Source | Current Freshness | Ideal Freshness | Method |
|-------------|-------------------|-----------------|--------|
| Vault notes | Real-time (watchdog) | Real-time | Already done |
| Granola notes | Real-time (file watcher) | Real-time | Already done |
| Calendar | 3x/day (8AM, noon, 3PM) | Near-real-time | Google webhook |
| Gmail | Once/day (3 AM) | 5-minute delay | Google push notification |
| iMessage | Once/day (3 AM) | Near-real-time | macOS observer on Mac Mini |
| Slack | Once/day (3 AM) | Real-time | Slack Events API |
| Phone calls | Once/day (2:50 AM) | Near-real-time | macOS observer on Mac Mini |

With the task queue in place, each data source can have its own freshness policy. The sync pipeline transforms from a monolithic nightly batch into a continuous ingestion system where each source updates at its own cadence.

### 7.3 The "Intelligence Layer" Pattern

Reading across the backend, telegram, and MCP audits reveals three different intelligence surfaces that share no state:

1. **Telegram agent**: Has tools, conversation context, intent classification
2. **MCP tools**: Stateless, tool-per-endpoint, no context
3. **Web UI chat**: Same pipeline as Telegram but different SSE consumer

**Proposal**: Extract a shared "Intelligence Layer" service that all three consume:

```
Intelligence Layer
    |-- Agent Loop (tools, multi-turn reasoning)
    |-- Intent Classifier (routing, action detection)
    |-- Memory Store (cross-conversation, cross-client)
    |-- Context Manager (person tracking, topic tracking)
    |-- Cost Governor (per-client budgets)

Consumers:
    Telegram Bot -> Intelligence Layer -> Response
    Web UI Chat  -> Intelligence Layer -> SSE Stream
    MCP Tools    -> Intelligence Layer -> Tool Results
    Scheduled    -> Intelligence Layer -> Proactive alerts
```

This means:
- A memory created via MCP is available in Telegram (currently siloed)
- A conversation started in web UI can be continued in Telegram
- Cost budgets apply across all clients
- The proactive intelligence system (telegram audit's proposal) has the same context as the chat system

### 7.4 Graduated Compute Allocation

The model selection audit (backend + telegram) shows a flat model: everything uses Sonnet. With local LLM + Claude API, introduce a three-tier compute model:

| Tier | Engine | Cost | Latency | When |
|------|--------|------|---------|------|
| **Instant** | Local 72B | $0 | <2s | Routing, intent, simple Q&A, greetings |
| **Standard** | Claude Sonnet | $0.01-0.05 | 3-10s | Multi-source synthesis, complex Q&A |
| **Deep** | Claude Opus | $0.10-0.50 | 10-30s | Complex reasoning, large context, code generation |

The router decides which tier based on query complexity. The current system already has this concept (model_selector.py) but maps all tiers to the same model. With local LLM, the Instant tier becomes truly free.

### 7.5 Observability Stack

The infra audit identifies: no structured logging, no metrics, no dashboard, no request tracing. The frontend audit identifies: no admin dashboard. With the workstation, deploy a lightweight observability stack:

```
FastAPI -> structlog (JSON) -> file / stdout
         -> Prometheus client (metrics endpoint)

Prometheus (scrapes /metrics every 15s)
    |
    v
Grafana (dashboards)
    |-- API latency (p50, p95, p99)
    |-- LLM inference time (local vs API)
    |-- Token usage and cost
    |-- Sync health and freshness
    |-- Queue depth and worker utilization
    |-- ChromaDB collection stats
    |-- Error rates by endpoint
```

This is lightweight (Prometheus + Grafana in Docker, ~200 MB RAM total) and solves the observability gap without overengineering.

### 7.6 Frontend Serving Strategy

The frontend audit identifies 25,000 lines of monolithic HTML. On the workstation, the infrastructure can support a proper build pipeline:

- **Development**: Keep vanilla JS for now (the CLAUDE.md says "simplicity first")
- **Serving**: nginx reverse proxy in front of FastAPI, serving static files directly
- **CDN**: Tailscale Funnel (if exposing to internet) or just direct serving
- **Caching**: nginx caches static assets, API responses have appropriate Cache-Control headers

This is a small infrastructure win that doesn't require rewriting the frontend but makes it faster and more reliable.

---

## Summary: Prioritized Infrastructure Roadmap

### Phase 1: Pre-Hardware (Now)

1. Fix the broken launchd plist (or replace with reliable server.sh watchdog)
2. Create backup directory and verify backup functionality
3. Add automated daily backup of crm.db + ChromaDB
4. Add log rotation for server.log and sync logs
5. Dockerize the application (Docker Compose for API + ChromaDB + Redis)

### Phase 2: Hardware Arrival (Day 1-7)

6. Set up Ubuntu + NVIDIA drivers + CUDA
7. Deploy Docker Compose stack on workstation
8. Migrate databases from Mac Mini
9. Install vLLM + Qwen 72B
10. Configure Mac Mini as sync-agent
11. Cutover DNS/Telegram to workstation

### Phase 3: Foundation (Week 2-4)

12. Implement Dramatiq task queue with Redis
13. Move sync pipeline to task queue (parallel Phase 1 sources)
14. Move embedding generation to GPU
15. Add API key authentication
16. Set up external monitoring (Uptime Kuma)
17. Add structured logging (structlog)

### Phase 4: Intelligence (Month 2-3)

18. Route simple queries to local LLM (zero API cost)
19. Add Whisper for Telegram voice messages
20. Implement Redis event bus for real-time updates
21. Add WebSocket support to frontend
22. Implement cross-client memory sharing
23. Set up Prometheus + Grafana

### Phase 5: Advanced (Month 3-6)

24. Event-driven sync (Gmail push, Calendar webhook, Slack Events API)
25. Multi-session Claude Code via task queue
26. Proactive intelligence system (communication gap alerts, meeting prep)
27. Vision model for photo analysis and OCR
28. Background briefing generation

---

## Appendix: Technology Choices Summary

| Component | Choice | Rationale |
|-----------|--------|-----------|
| **OS** | Ubuntu 24.04 LTS | Best CUDA support, systemd, stable |
| **Process manager** | systemd + Docker Compose | Native Linux, reliable, well-documented |
| **Task queue** | Dramatiq + Redis | Simpler than Celery, good enough for single-machine |
| **Message broker** | Redis | Also serves as cache and pub/sub bus |
| **LLM serving** | vLLM | Best throughput, OpenAI-compatible API |
| **Primary local model** | Qwen 2.5 72B (AWQ 4-bit) | Best open model at this size, fits 32 GB VRAM |
| **STT** | Whisper Large V3 | Best open STT model |
| **Monitoring** | Prometheus + Grafana | Lightweight, standard, Docker-native |
| **Logging** | structlog (JSON) | Structured, searchable, parseable |
| **External monitoring** | Uptime Kuma (Docker) | Self-hosted, simple, Telegram alerts |
| **Backup** | rsync + rclone to B2 | Cheap cloud storage, encrypted |
| **Reverse proxy** | nginx | Static file serving, rate limiting, SSL |
