# LifeOS Code Structure

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-09-04

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
│   ├── crm.py                 # CRM endpoints + models (~5,100 LOC) -- see below
│   ├── crm_models/            # NOT wired into the app -- see "CRM Models Package"
│   │   ├── __init__.py        # Re-exports models and utils
│   │   ├── models.py          # Unused parallel Pydantic models (~600 LOC)
│   │   └── _utils.py          # is_family_member() is kept in sync by a test
│   ├── _proxy.py              # Shared reverse-proxy router factory (agent/hermes)
│   ├── agent_proxy.py         # Agent text-backend reverse proxy
│   ├── hermes_proxy.py        # Hermes text-backend reverse proxy + persona envelope
│   ├── voice.py               # Voice gateway (whisper-relay) reverse proxy
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
| hermes_proxy.py | ~360 | 2 (via factory) | Hermes text-backend reverse proxy, persona envelope, turn persistence |
| _proxy.py | ~160 | 2 (factory) | Shared reverse-proxy router factory used by `agent_proxy.py` and `hermes_proxy.py` |
| agent_proxy.py | ~40 | 2 (via factory) | Agent text-backend reverse proxy |
| voice.py | ~80 | 1 | Voice gateway (whisper-relay) reverse proxy |

**Reverse-proxy layer.** `/chat` targets one of three text backends (`lifeos` native, `agent`, `hermes`), plus a separate voice transport — the latter three are reverse proxies, not LifeOS-native logic:

- `_proxy.py` — `make_backend_router()`, a factory building the shared `GET /status` + `POST /ask/stream` pair (header filtering, bearer injection, streaming relay) that both text-backend proxies mount. Optional `transform_body`/`make_observer` hooks let a backend rewrite the outbound body or tee the relayed stream without touching the shared relay loop.
- `agent_proxy.py` — mounts that factory at `/api/agent`; a byte-for-byte relay to `LIFEOS_AGENT_BACKEND_URL` with no persona or context injection.
- `hermes_proxy.py` — mounts the same factory at `/api/hermes`, adding a `transform_body` that resolves the LifeOS persona and attaches a `lifeos_context` envelope, and a `make_observer` that persists the turn (conversation + usage) from the relayed SSE stream.
- `voice.py` — a separate catch-all reverse proxy (`/api/voice/*` → `LIFEOS_VOICE_GATEWAY_URL`) fronting the whisper-relay voice gateway; the gateway calls back into one of the three text-backend paths above to answer a spoken turn. Frontend counterpart: `web/chat/voice.js`.

Per-backend capabilities (personas, handoff, history ownership, usage capture) and the full voice turn contract are specified in [Client Surfaces](client-surfaces.md) — this section covers code layout only.

### Services (api/services/)

**People & CRM:**
- `person_entity.py` - PersonEntity model and store
- `people_aggregator.py` - Multi-source aggregation
- `entity_resolver.py` - Entity resolution logic
- `person_facts.py` - Fact extraction and storage
- `person_indexer.py` - Person search indexing
- `person_stats.py` - Statistics computation

`PersonEntityStore.get_all()` keeps a process-local cache of hydrated
`PersonEntity` objects for the people list and CRM aggregate views. Cache
entries are keyed by hidden/merged inclusion flags, SQLite `PRAGMA
data_version` from a long-lived read connection, and a local generation
counter bumped by store write methods and merged-ID reloads. This means any
commit to `data/crm.db` from the API process or a separate sync process
invalidates the cached people list on the next read. Callers receive a new
list object on each call, but entity objects are shared, so write paths that
mutate a person fetched from the full list must refetch that person by ID
before persisting changes.

**Relationships:**
- `relationship.py` - Relationship store
- `relationship_discovery.py` - Connection discovery
- `relationship_metrics.py` - Strength computation
- `relationship_summary.py` - Summary generation
- `relationship_insights.py` - Therapy note insights
- `tone_analysis_store.py` - Persisted per-person/month iMessage tone scores

The CRM people-list endpoints (`GET /api/crm/people`, `GET /birthdays/today`)
compute each returned person's category dynamically, which needs that
person's source entities when they don't already qualify as "work" via their
own email domain or a `slack` source tag. Rather than calling
`SourceEntityStore.get_for_person()` once per returned person,
`SourceEntityStore.get_for_people_batch()` fetches the whole page in one
round trip (a compound `UNION ALL` of per-person, `LIMIT`-bounded
subqueries — chosen over a single `WHERE ... IN (...)` query because SQLite
has no per-group "top-K" optimization for that shape, so it would have to
fully rank every matching row for the highest-interaction people before
applying any per-person cap). The category fetch also caps at 50 source
entities per person rather than the single-person path's 500, verified
against the full production dataset to produce identical categories either
way — compute_person_category() only needs to find one qualifying entity,
not all of them.

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

**Not currently wired into the running API.** The Pydantic models the CRM API
actually serves — `PersonDetailResponse` (including `has_profile_photo`,
added in #875), `PersonListResponse`, `TimelineItem`, and the rest — are
defined directly inside `api/routes/crm.py`, alongside the route handlers
that use them; `api/main.py` mounts only `crm.router`. `crm_models/` is a
separate, parallel module tree (`models.py`, `_utils.py`, `__init__.py`)
that predates or anticipated a split of `crm.py` into smaller files but was
never finished wiring up — nothing in `api/main.py` or `api/routes/crm.py`
imports from it.

It isn't dead code, though: `api.routes.crm_models._utils.is_family_member`
is a second, independent implementation of the family-matching logic that
`api.services.person_entity._is_family_member` also implements for the live
path, and `tests/test_family_matching.py` exercises both directly so the two
can't silently drift apart. Anyone touching family-matching rules needs to
update both. If `crm_models/` is ever finished and wired up (or removed),
that test and this note both need to move with it.

### Importing the family-matching utility

```python
from api.routes.crm_models._utils import is_family_member
```

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

`api/routes/crm.py` defines and uses its own response models directly — they
aren't re-exported for other modules to import. The one thing from
`api/routes/crm_models/` that another module does import (see [CRM Models
Package](#crm-models-package-apiroutescrm_models) above):

```python
from api.routes.crm_models._utils import is_family_member
```

---

## Coding Patterns

### Route Handler Pattern

```python
@router.get("/endpoint", response_model=ResponseModel)
def endpoint_handler(
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

**`def` vs. `async def` (#868).** LifeOS runs one uvicorn process with a
single event loop; every client surface (chat, Telegram, voice, MCP, the
agent worker, and the CRM/people UI) shares it. A handler declared `async
def` with nothing to `await` still runs inline on that loop, so its
synchronous DB/CPU work blocks every other in-flight request until it
returns. A handler declared plain `def` is dispatched by FastAPI to the
worker threadpool (via Starlette's `run_in_threadpool`, anyio's default
capacity of 40) automatically, which restores fairness between requests.
The CRM, people, and photos routers (`api/routes/crm.py`, `api/routes/people.py`,
`api/routes/photos.py`) follow this rule as of #868: a handler is `async def`
only if its own body actually awaits something (an LLM call, `await
file.read()`); otherwise it is `def`. A handler that keeps `async def`
pushes its blocking store or LLM calls onto a thread explicitly with `await
asyncio.to_thread(...)` (e.g. `api/routes/investments.py`, `api/routes/crm.py`'s
fact-extraction and source-import endpoints) rather than doing them inline.
This is a fairness fix, not a throughput one — CPU-bound work still holds
the GIL while it runs; per-endpoint throughput is tracked separately
(issues #869-#874). **Scope:** this is the rule for these three routers
specifically, enforced by `tests/test_route_handlers_sync.py`; the rest of
`api/routes/` still has `async def` handlers with no `await` that #868 did
not touch — converting them is a separate, unstarted effort, not a silent
exception to the rule above.
Moving these handlers off the loop also removed the implicit serialization
an inline `async def` gave every request against every other one. Handlers
that mutate shared on-disk state (`merge_people`, `split_person`,
`hide_person`, the review-queue confirm/reject endpoints, and the
sync-trigger POSTs) now hold a module-level `threading.Lock`
(`api/routes/crm.py`'s `_mutation_lock`) across their bodies to restore
that serialization explicitly, since two of them interleaving could
otherwise corrupt shared bookkeeping (e.g. `scripts/merge_people.py`'s
merge-intent log).

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

## Write Endpoint Failure Contract

**Guarantee:** a curated write endpoint must never report a failure inside an
HTTP 2xx without a top-level `error` key — a caller (human, MCP client, or
the agent worker) must be able to tell success from failure from the status
code or that key alone, without parsing prose. This was violated once (#603,
`fitness.py`) and the class of defect was audited end to end for #609.

**How it's enforced, mechanically:**

- MCP surface (`mcp_server.py: dispatch()`) sets `result["isError"] = True`
  whenever a tool's `_call_api()` result is a dict containing an `"error"`
  key. `_call_api()` derives that key either from `resp.raise_for_status()`
  raising on a non-2xx, **or** from a 2xx JSON body that already carries a
  top-level `"error"` key (the latter matters for `lifeos_sync_trigger`,
  whose custom handler — `_handle_sync_trigger` — passes a 2xx body through
  unmodified rather than routing it through `_call_api`'s own try/except; see
  `test_sync_trigger_2xx_with_embedded_error_sets_is_error`). Either way this
  is generic and correct for any tool **only if** the underlying route
  actually raises on failure or embeds a top-level `error` key.
- Agent worker (`api/services/agent_worker/tools.py: ToolRegistry.dispatch`)
  routes MCP-backed tools through that same `_call_api`/`_format_response`
  pair and derives `ToolResult.is_error` identically (`"error" in data`,
  proved tool-name-agnostic by `test_registry_lifeos_tool_error_surfaced` in
  `tests/test_agent_worker_tools.py`) — it inherits both the guarantee and
  any exemption to it, with no code of its own to go stale.
- Native chat/Hermes orchestrator (`api/services/agent_loop.py`) never calls
  the HTTP layer — it dispatches in-process through
  `api/services/agent_tools.py`, where every handler returns a plain string
  and a failure is required to start with the literal `"Error:"`.
  `agent_loop.py` derives the Anthropic `tool_result.is_error` field from that
  prefix (`tool_result_str.startswith("Error:")`), which becomes the
  structured signal the model actually receives.

Both HTTP-facing mechanisms are correct by construction; they only fail when
a *route* returns 2xx with a failure embedded in the body without a
top-level `error` key. The table below is the audit of every curated write
endpoint against that condition, plus which of the three mechanisms above
actually consumes it — an endpoint can be honest while every consumer of it
is blind, which is how the incident that prompted #609 happened. This is
what makes the guarantee above true rather than aspirational (docs/AGENTS.md:
no document may claim an unenforced guarantee).

| MCP tool | Route | Status | Consumers | Regression coverage |
|---|---|---|---|---|
| `lifeos_vault_write` | `POST /api/vault/write` | Safe — `OSError` → `HTTPException(500)` | MCP, worker (no native-loop equivalent) | `tests/test_vault_write_route.py` |
| `lifeos_memories_create` | `POST /api/memories` | Safe — unhandled exception → default 500 | MCP, worker, native (`_tool_save_memory`) | `tests/test_memories_api.py` |
| `lifeos_person_update` | `PATCH /api/crm/people/{id}` | Safe — 404 on missing person | MCP, worker (no native-loop equivalent) | `tests/test_crm_api.py::test_update_person_not_found` |
| `lifeos_person_fact_update/confirm/delete` | `{PUT,POST,DELETE} .../facts/{id}[/confirm]` | Safe — 404/400 on missing or mismatched fact | MCP, worker (no native-loop equivalent) | `tests/test_crm_api.py::test_{update,confirm,delete}_fact_not_found` |
| `lifeos_gmail_draft` | `POST /api/gmail/drafts` | Safe — 500 on a `None` draft; broad `except` re-raises as `HTTPException` | MCP, worker, native (`_tool_create_email_draft`) | `tests/test_gmail.py::test_create_draft_failure_returns_500` |
| `lifeos_gmail_send` | `POST /api/gmail/send` | Safe — 409 (send gate) / 500 | MCP, worker, native (`_tool_send_email_draft`) | `tests/test_gmail.py::test_send_draft_failure_returns_500` |
| `lifeos_reminder_create/update/delete` | `{POST,PUT,DELETE} /api/reminders[/{id}]` | Safe — 400/404 | MCP, worker; native covers create only (deprecated `_reminder_create` alias — no native update/delete) | `tests/test_reminders_api.py` |
| `lifeos_telegram_send` | `POST /api/reminders/send` | Safe — 400 not configured / 500 send failure | MCP, worker (no native-loop equivalent) | `tests/test_reminders_api.py` |
| `lifeos_schedule_create/update/delete` | `{POST,PUT,DELETE} /api/scheduler[/{id}]` | Safe — 400/404 | MCP, worker, native (`_schedule_create/_update/_delete`) | `tests/test_scheduler_api.py` |
| `lifeos_sync_trigger` (`source=vault`) | `POST /api/admin/reindex` | Safe — reports enqueue state honestly, never claims completion | MCP, worker (no native-loop equivalent — see note below) | `tests/test_admin.py` |
| `lifeos_sync_trigger` (`source=calendar`) | `POST /api/admin/calendar/sync` | **Fixed for #609/#614.** `except Exception` returns a `JSONResponse` with a top-level `"error"` key alongside the existing fields *and* an explicit 500 status (#614: a total failure means the requested sync did not happen, and a consumer that only checks HTTP status should get correct behavior without knowing the body convention). A `partial` outcome (some calendar accounts synced, one failed) is a real, non-error result and stays 200 — it's returned via the normal `CalendarSyncResponse` path above the `except`, never through this branch. | MCP, worker (no native-loop equivalent) | `tests/test_calendar_indexer.py::test_trigger_calendar_sync_failure_carries_top_level_error` (500 + error key), `tests/test_calendar_indexer.py::test_trigger_calendar_sync_partial_status_stays_200` (partial stays 200), `tests/test_mcp_server.py::test_sync_trigger_2xx_with_embedded_error_sets_is_error` |
| `lifeos_sync_trigger` (`source=contacts`, `slack`) | `POST /api/crm/{contacts,slack}/sync` | Safe — raise `HTTPException` on failure | MCP, worker (no native-loop equivalent) | `tests/test_crm_api.py` |
| `lifeos_sync_trigger` (`source=photos`) | `POST /api/photos/sync` | **Fixed for #609/#614.** Same mechanism as the calendar case — the error previously lived only nested inside `stats["error"]`, invisible to the generic top-level check; now also present at the top level, with an explicit 500 status (same #614 reasoning as above; this endpoint has no `partial` outcome to preserve). | MCP, worker (no native-loop equivalent) | `tests/test_photos_sync_api.py::test_sync_failure_carries_top_level_error` |
| `lifeos_sync_trigger` (`source=gmail`, `imessage`, `phone`, `facetime`, `linkedin`) | `POST /api/crm/sources/{type}/sync` | Stub — always returns `{"status": "queued"}`; no sync is actually implemented yet, so there is no failure path to mis-report. Not a #609 defect. | MCP, worker (no native-loop equivalent) | — |
| `lifeos_workout_manage` | `POST /api/fitness/workouts` | Safe — fixed in #603 | MCP, worker, native (`_tool_manage_workouts`) | `tests/test_workout_mcp_route.py` |
| `lifeos_task_create/update/complete/delete` | `{POST,PUT,DELETE} /api/tasks[/{id}[/complete]]` | Safe — unhandled exception → 500; not-found → 404 | MCP, worker, native covers create/update/complete (`_task_create/_update/_complete`) — no native delete | `tests/test_tasks_api.py::test_create_task_failure_is_never_success_shaped` (create); 404 cases elsewhere in the same file |
| `lifeos_calendar_create/update/delete` | `{POST,PUT,DELETE} /api/calendar/events[/{id}]` | Safe — 401/500 | MCP, worker, native (`_tool_create/update/delete_calendar_event`) | `tests/test_calendar_api.py::TestCalendarWriteFailures` |

Every `lifeos_sync_trigger` row has no native-loop equivalent because that
loop has no sync-trigger-shaped tool at all — see the paragraph below.

Native-tool-loop write handlers in `api/services/agent_tools.py` (`_task_*`,
`_reminder_create`, `_schedule_*`, `_tool_create_email_draft`,
`_tool_send_email_draft`, `_tool_*_calendar_event`, `_tool_save_memory`,
`_workout_*`) were each checked for a path that returns success-shaped text
on failure; none exists — every failure either propagates as an exception
(caught by `execute_tool`/`execute_tool_parallel`'s `except Exception` →
`"Error: {e}"`) or is an explicit `"Error: ..."`-prefixed string.

The table above is a point-in-time audit; it does not by itself catch a
*future* write endpoint that reintroduces the anti-pattern.
`tests/test_mcp_server.py::TestWriteEndpointNeverReturnsSuccessShapedFailure`
closes that gap mechanically: it enumerates every curated write endpoint
straight from `CURATED_ENDPOINTS` (plus the sub-routes `_handle_sync_trigger`
fans `lifeos_sync_trigger` out to) and statically flags any `except` block
that returns normally without raising, setting an explicit non-2xx status,
or embedding a top-level `error` key, so a newly added endpoint with this
shape fails a test that already exists rather than needing a new one. It is
a static approximation, not a substitute for the failure-injection tests
cited in the table above.
`tests/test_mcp_server.py::TestMCPServerToolDiscovery::test_task_create_tags_advertised_as_array`
separately pins the specific schema mistyping (`tags` as `string` instead of
`array`) that #603 fixed and #609 traced the original incident to.

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
