# LifeOS Code Structure

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-02-19

Codebase organization and module structure for efficient navigation.

---

## Directory Overview

```
api/
├── __init__.py
├── main.py                    # FastAPI application entry point
├── routes/                    # API route handlers
│   ├── __init__.py            # Router exports
│   ├── admin.py               # Admin endpoints (reindex via job queue)
│   ├── jobs.py                # Job queue status API
│   ├── ask.py                 # Chat endpoints
│   ├── briefings.py           # People briefings
│   ├── calendar.py            # Calendar integration
│   ├── chat.py                # Streaming chat with agentic pipeline
│   ├── conversations.py       # Conversation history
│   ├── crm.py                 # CRM endpoints (~5,100 LOC)
│   ├── crm_models/            # CRM Pydantic models and helpers
│   │   ├── __init__.py        # Re-exports models and utils
│   │   ├── models.py          # All Pydantic models (~600 LOC)
│   │   └── _utils.py          # Shared helper functions
│   ├── drive.py               # Google Drive
│   ├── gmail.py               # Gmail integration
│   ├── imessage.py            # iMessage search
│   ├── memories.py            # Memory store
│   ├── people.py              # Simple people lookup
│   ├── scheduler.py           # Schedule CRUD endpoints (/api/scheduler)
│   ├── reminders.py           # Deprecated /api/reminders alias
│   ├── search.py              # Vector search
│   ├── slack.py               # Slack integration
│   └── tasks.py               # Task CRUD endpoints
├── services/                  # Business logic and data access
│   ├── __init__.py            # Public API exports
│   ├── chat_helpers.py        # Query parsing, intent detection
│   └── ... (40+ service files)
└── utils/                     # Shared utilities
    ├── __init__.py
    ├── datetime_utils.py      # make_aware() - timezone handling
    └── db_paths.py            # get_crm_db_path() - database paths
```

---

## Key Modules

### Routes (api/routes/)

| File | Lines | Endpoints | Purpose |
|------|-------|-----------|---------|
| crm.py | ~5,100 | 57 | Personal CRM API |
| chat.py | ~1,800 | 1 | Streaming chat with agentic pipeline |
| tasks.py | ~180 | 6 | Task CRUD API |
| scheduler.py | ~230 | 7 | Schedule CRUD API (/api/scheduler) |
| reminders.py | ~180 | 6 | Deprecated /api/reminders alias |
| calendar.py | ~400 | 8 | Google Calendar |
| gmail.py | ~350 | 6 | Gmail integration |
| slack.py | ~500 | 10 | Slack integration |
| admin.py | ~300 | 15 | Admin/maintenance |
| jobs.py | ~70 | 3 | Job queue status |

### Services (api/services/)

**People & CRM:**
- `person_entity.py` - PersonEntity model and store
- `people_aggregator.py` - Multi-source aggregation
- `entity_resolver.py` - Entity resolution logic
- `person_facts.py` - Fact extraction and storage
- `person_indexer.py` - Person search indexing
- `person_stats.py` - Statistics computation

**Relationships:**
- `relationship.py` - Relationship store
- `relationship_discovery.py` - Connection discovery
- `relationship_metrics.py` - Strength computation
- `relationship_summary.py` - Summary generation
- `relationship_insights.py` - Therapy note insights

**Source Integration:**
- `source_entity.py` - Raw observation records
- `interaction_store.py` - Interaction storage
- `apple_contacts.py` - Contacts sync
- `slack_integration.py` - Slack OAuth & sync
- `whatsapp_import.py` - WhatsApp import
- `signal_import.py` - Signal import

**Task Management:**
- `task_manager.py` - Task CRUD, markdown I/O, index persistence, fuzzy query

**Background Jobs:**
- `job_queue.py` - SQLite-backed job queue with worker thread (reindex, sync)

**Telegram & Scheduling:**
- `telegram.py` - Telegram bot (message sending, bot listener, internal chat client, `/claude` and `/codex` commands, `chat_via_api_with_log` for execution metadata capture)
- `agent_worker/claude_code_executor.py` - Claude Code subprocess lifecycle, stream parsing, `[NOTIFY]` relay (replaces the retired `claude_orchestrator.py`)
- `agent_worker/claude_code_spawn.py` - Creates a `routing='claude_code'` session row for the worker to dispatch
- `agent_worker/codex_executor.py` - Codex CLI subprocess lifecycle (`codex exec --json`), captures final message via `--output-last-message`, persists thread id for resume
- `agent_worker/codex_spawn.py` - Sibling of `claude_code_spawn.py` for `routing='codex'`
- `codex/session_ingest.py` - Read-only adapter for the `/agents` viz: walks `~/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl`, normalizes events, prices via OpenAI rates (gpt-5.5, gpt-5.4, gpt-5.3-codex)
- `directory_resolver.py` - Maps task keywords to working directories for the CLI executors
- `scheduler_store.py` - Schedule store + scheduler. Markdown source of truth (`LifeOS/Scheduler/Inbox.md`), action dispatch (notify/prompt/endpoint/agent), retry, suppression, run history, and dashboard generation. `reminder_store.py` re-exports it under the legacy `Reminder*` names.
- `scheduler_watcher.py` - Watchdog watcher that reindexes the Scheduler directory on external edits
- `time_parser.py` - Natural language time parsing for schedules

**Search & Retrieval:**
- `vectorstore.py` - ChromaDB wrapper
- `hybrid_search.py` - BM25 + vector search
- `bm25_index.py` - BM25 indexing
- `reranker.py` - Result reranking
- `embeddings.py` - Embedding generation

**LLM Integration:**
- `llm_client.py` - Unified LLM client supporting local (llama-server, OpenAI-compatible API on port 8080) and Anthropic backends. Handles tool format translation between Anthropic and OpenAI schemas. Backend selected via `LIFEOS_LLM_BACKEND` setting (`local` default, `anthropic` optional).

**Chat & Query Processing:**
- `chat_helpers.py` - Query parsing, intent classification (ambiguous task/reminder, code), date extraction. Uses LLM-based classification (local LLM → pattern fallback). Compose/task/reminder intents now flow through the agentic loop.
- `agent_loop.py` - Agentic chat loop: multi-turn conversation where Claude autonomously calls tools. Async generator yielding streamed text, tool status, and final result with cost tracking. Supports prompt caching.
- `agent_tools.py` - Tool definitions and handlers. Tool format translation (Anthropic ↔ OpenAI) handled by `llm_client.py`. Consolidated tools: `manage_tasks`, `manage_schedules` (`manage_reminders` kept as a deprecated alias), `person_info`. Includes `read_vault_file` for full-file reads, `save_memory` and `search_memories` for agent memory.
- `agent_system_prompt.py` - System prompt builder. Returns cached static block + dynamic datetime block. Prompt caching is Anthropic-specific (used when `LIFEOS_LLM_BACKEND=anthropic`).
- `query_router.py` - LLM-based query routing with person name extraction (used by non-agentic paths)
- `conversation_context.py` - Tracks context across follow-up queries (person, reminder, topics)

---

## Shared Utilities (api/utils/)

Common utilities used across multiple services.

### datetime_utils.py

```python
from api.utils.datetime_utils import make_aware

# Convert naive datetime to UTC-aware
aware_dt = make_aware(naive_dt)
```

### db_paths.py

```python
from api.utils.db_paths import get_crm_db_path

# Get path to CRM database
db_path = get_crm_db_path()  # Returns "data/crm.db"
```

---

## CRM Models Package (api/routes/crm_models/)

The CRM models package consolidates Pydantic models and utilities for the CRM API.

### Importing Models

```python
from api.routes.crm_models import (
    PersonDetailResponse,
    TimelineItem,
    NetworkGraphResponse,
)
```

### Importing Utilities

```python
from api.routes.crm_models import (
    compute_person_category,
    person_to_detail_response,
    MY_PERSON_ID,
)
```

### Models Reference

| Category | Models |
|----------|--------|
| Person | PersonDetailResponse, PersonListResponse, PersonUpdateRequest, PersonMergeRequest, PersonSplitRequest |
| Timeline | TimelineItem, TimelineResponse, AggregatedTimelineResponse |
| Relationships | RelationshipResponse, RelationshipDetailResponse, ConnectionResponse |
| Network | NetworkNode, NetworkEdge, NetworkGraphResponse |
| Facts | PersonFactResponse, PersonFactsResponse, FactExtractionResponse |
| Dashboard | MeStatsResponse, MeInteractionsResponse, FamilyInteractionsResponse |
| Health | SyncHealthResponse, ReviewQueueResponse |

---

## Public API Imports

### Services

```python
from api.services import (
    # Person/CRM
    PersonEntity, get_person_entity_store,
    SourceEntity, get_source_entity_store,
    Interaction, get_interaction_store,
    Relationship, get_relationship_store,
    PersonFact, get_person_fact_store,
    # Relationships
    compute_strength_for_person, update_all_strengths,
    run_full_discovery, get_suggested_connections,
    # Chat helpers
    extract_search_keywords, detect_compose_intent,
    extract_date_context, extract_message_date_range,
    # Utilities
    make_aware, get_crm_db_path,
)
```

### Routes

```python
from api.routes import (
    chat_router, crm_router, ask_router, search_router,
    admin_router, gmail_router, calendar_router, slack_router,
)
```

### CRM Models

```python
from api.routes.crm_models import (
    PersonDetailResponse, TimelineItem, NetworkGraphResponse,
    compute_person_category, person_to_detail_response,
    MY_PERSON_ID, FAMILY_EXACT_NAMES,
)
```

---

## Coding Patterns

### Route Handler Pattern

```python
@router.get("/endpoint", response_model=ResponseModel)
async def endpoint_handler(
    param: str = Query(..., description="Required parameter"),
    optional: int = Query(default=10, ge=1, le=100),
):
    """Docstring with description."""
    start_time = time.time()

    # Business logic
    result = service_function(param)

    elapsed = (time.time() - start_time) * 1000
    logger.info(f"endpoint_handler took {elapsed:.1f}ms")

    return ResponseModel(...)
```

### Service Store Pattern

```python
# Singleton pattern with lazy initialization
_store_instance = None

def get_store() -> Store:
    global _store_instance
    if _store_instance is None:
        _store_instance = Store()
    return _store_instance
```

### Database Path Pattern

```python
from api.utils.db_paths import get_crm_db_path

def my_function():
    conn = sqlite3.connect(get_crm_db_path())
    # ...
```

---

## Testing

```bash
# Unit tests (fast)
./scripts/test.sh

# Smoke tests (includes browser tests)
./scripts/test.sh smoke

# All tests
./scripts/test.sh all
```

Tests are in `tests/` with naming convention `test_*.py`.

## Related Documents

- [Data & Sync](data-and-sync.md) -- Data sources and sync pipeline
- [Client Surfaces](client-surfaces.md) -- HTTP consumers and breaking-change policy
- [Frontend](frontend.md) -- UI components and patterns
- [API Reference](../product/api-reference.md) -- API endpoint contracts
- [ADR-001: Python/FastAPI](../../adr/001-python-fastapi.md) -- Why Python/FastAPI was chosen
- [Python Conventions](../standards/python-conventions.md) -- Coding style and module patterns
