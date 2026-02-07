# LifeOS Code Structure

Codebase organization and module structure for efficient navigation.

**Related Documentation:**
- [API & MCP Reference](API-MCP-REFERENCE.md) - API endpoints
- [Data & Sync](DATA-AND-SYNC.md) - Data sources and sync
- [Frontend](FRONTEND.md) - UI components

---

## Directory Overview

```
api/
├── __init__.py
├── main.py                    # FastAPI application entry point
├── routes/                    # API route handlers
│   ├── __init__.py            # Router exports
│   ├── admin.py               # Admin endpoints
│   ├── ask.py                 # Chat endpoints
│   ├── briefings.py           # People briefings
│   ├── calendar.py            # Calendar integration
│   ├── chat.py                # Streaming chat with RAG
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
│   ├── search.py              # Vector search
│   └── slack.py               # Slack integration
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
| chat.py | ~1,200 | 1 | Streaming chat with RAG |
| calendar.py | ~400 | 8 | Google Calendar |
| gmail.py | ~350 | 6 | Gmail integration |
| slack.py | ~500 | 10 | Slack integration |
| admin.py | ~300 | 15 | Admin/maintenance |

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

**Telegram & Scheduling:**
- `telegram.py` - Telegram bot (message sending, bot listener, internal chat client)
- `reminder_store.py` - Reminder CRUD, persistence, and scheduler

**Search & Retrieval:**
- `vectorstore.py` - ChromaDB wrapper
- `hybrid_search.py` - BM25 + vector search
- `bm25_index.py` - BM25 indexing
- `reranker.py` - Result reranking
- `embeddings.py` - Embedding generation

**Chat & Query Processing:**
- `chat_helpers.py` - Query parsing, intent detection, date extraction
- `query_router.py` - LLM-based query routing with person name extraction

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
