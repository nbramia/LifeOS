# Python Conventions

> **Status:** Complete
> **Last Updated:** 2026-08-26
> **Audience:** All developers and AI agents

Coding conventions extracted from the LifeOS codebase. Match these patterns when writing new code.

These are rules, not suggestions. Code review should enforce them. When LifeOS conventions conflict with general Python style guides, LifeOS conventions win.

---

## Project Layout

| Directory | Purpose |
|-----------|---------|
| `api/` | FastAPI application (routes, services, utils) |
| `config/` | Settings, people dictionary, family config |
| `scripts/` | Shell scripts (server, deploy, test, sync) |
| `tests/` | Pytest test suite |
| `web/` | Static frontend (HTML, CSS, JS) |
| `data/` | Runtime data (SQLite DBs, indexes, ChromaDB) |
| `docs/` | Documentation and specs |

## Module Organization

| Subpackage | Role | Example |
|------------|------|---------|
| `api/routes/` | API handlers (thin, delegates to services) | `tasks.py`, `perf.py`, `crm.py` |
| `api/services/` | Business logic and data stores | `task_manager.py`, `person_entity.py` |
| `api/utils/` | Shared stateless utilities | `datetime_utils.py`, `db_paths.py` |

There is no `api/models/` directory. Pydantic models are defined inline in route files. Dataclasses are defined in their service files.

## Naming Conventions

| Element | Convention | Examples |
|---------|-----------|----------|
| Functions / variables | `snake_case` | `get_task_manager()`, `task_id`, `due_before` |
| Classes | `PascalCase` | `TaskManager`, `PersonEntity`, `PersonEntityStore` |
| Constants | `UPPER_SNAKE` | `STATUS_TO_SYMBOL`, `VALID_STATUSES`, `DEFAULT_INDEX_PATH` |
| Private helpers | `_leading_underscore` | `_format_task_line()`, `_parse_task_line()`, `_load_family_config()` |
| Route files | Noun (plural or singular) | `tasks.py`, `perf.py`, `calendar.py` |
| Test files | `test_<module>.py` | `test_task_manager.py`, `test_tasks_api.py` |

## Import Ordering

Standard library, then third-party, then local. Blank line between groups.

```python
# stdlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# third-party
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# local
from api.services.task_manager import get_task_manager, Task
from config.settings import settings
```

## Route Handler Pattern

Routes are thin async functions on an `APIRouter`. They delegate to a service singleton and return Pydantic models or dicts.

```python
# api/routes/tasks.py
router = APIRouter(prefix="/api/tasks", tags=["tasks"])

@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status: Optional[str] = None,
    context: Optional[str] = None,
):
    manager = get_task_manager()
    tasks = manager.list_tasks(status=status, context=context)
    return TaskListResponse(
        tasks=[TaskResponse.from_task(t) for t in tasks],
        total=len(tasks),
    )
```

Key patterns:
- Router prefix is `/api/<resource>` with a descriptive tag.
- Request/response Pydantic models are defined at the top of the route file.
- 404s use `raise HTTPException(status_code=404, detail="...")`.
- No try/except in routes -- services handle errors internally.

## Service / Store Pattern

Services use a module-level singleton with a `get_*()` accessor.

```python
# api/services/task_manager.py
_task_manager: Optional[TaskManager] = None

def get_task_manager() -> TaskManager:
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
```

This pattern is consistent across `get_person_entity_store()`, `get_perf_trace_store()`, `get_job_queue()`, etc.

## Database Access Pattern

SQLite connections are created per-operation and closed in a `try/finally` block. WAL mode and `row_factory = sqlite3.Row` are standard.

```python
def _get_connection(self) -> sqlite3.Connection:
    conn = sqlite3.connect(str(self.db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def get_by_id(self, entity_id: str) -> Optional[PersonEntity]:
    conn = self._get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM person_entities WHERE id = ?",
            (entity_id,)).fetchone()
        return self._row_to_entity(row) if row else None
    finally:
        conn.close()
```

## SQLite Connection Closing

`with sqlite3.connect(...)` is a *transaction* context manager — it commits
or rolls back on exit, but does not close the connection. Left alone,
CPython's refcounting closes it once `conn` goes out of scope, which is
usually safe in practice, but that is an implementation detail, not a
guarantee: the bare pattern can leak a file descriptor per batch in a tight
loop and exhaust `ulimit -n`.

Prefer `contextlib.closing`, matching `api/services/imessage.py`:

```python
from contextlib import closing

# Read-only: closing() alone ends the connection deterministically.
with closing(sqlite3.connect(self.storage_path)) as conn:
    conn.row_factory = sqlite3.Row
    ...

# Writes: pair it with the connection's own commit/rollback context too --
# closing() alone silently drops the commit.
with closing(sqlite3.connect(self.storage_path)) as conn, conn:
    conn.execute("UPDATE ...")
```

This is the house style for new and touched code. `api/services/`'s other
SQLite-backed stores (`agent_viz_summary.py`, `agent_viz_label_override.py`,
`job_queue.py`, `hermes_persona_thread_store.py`, `perf_trace.py`,
`gsheet_sync.py`, `usage_store.py`) still use the bare form — leave them as
found unless a change already touches that file.

## Error Handling

| Layer | Pattern |
|-------|---------|
| Routes | `raise HTTPException(status_code=..., detail="...")` |
| Services | Log the error, return `None` or empty list |
| Startup (`main.py`) | `try/except` with `logger.error()`, never crash the app |

## Type Annotations

- Route parameters and Pydantic models are fully typed.
- Service methods use basic type hints (`Optional[str]`, `list[Task]`, `-> bool`).
- Dataclasses have explicit type annotations on all fields.
- `TYPE_CHECKING` guard is used to avoid circular imports.

## Logging

Every module creates its own logger. Messages are descriptive with context values.

```python
logger = logging.getLogger(__name__)

logger.info(f"Created task {task.id}: {description}")
logger.warning(f"Error loading task index: {e}. Rebuilding.")
logger.error(f"Failed to start calendar indexer: {e}")
```

### Privacy Constraints

LifeOS handles maximally sensitive data. Logging rules are strict:

- **NEVER** log personal data content: message bodies, email content, note text, photo metadata.
- **NEVER** log authentication tokens, API keys, or OAuth credentials.
- Log entity **IDs only** — UUIDs and file paths are safe, content is not.
- Log **counts and durations**, not values: `"indexed 47 documents"`, not the documents.

```python
# GOOD — IDs and metadata only
logger.info(f"Indexed person {person_id}, {len(sources)} source entities")

# BAD — leaks personal data
logger.info(f"Indexed {person.display_name}: {person.email}, {person.phone}")
```

## Data Classes

Domain objects use `@dataclass` with `to_dict()` / `from_dict()` classmethods for serialization. Pydantic is reserved for API request/response shapes.

```python
@dataclass
class Task:
    id: str
    description: str
    status: str = "todo"
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
```

## Performance Discipline

- Profile before optimizing — use `api/services/perf_trace.py` for request-level tracing.
- Singleton services are initialized lazily (`get_*()` pattern) to avoid startup overhead.
- SQLite connections are per-operation (not pooled) with WAL mode for concurrent reads.
- ChromaDB queries should batch where possible — each call has network overhead.
- Embedding model loading is expensive — the singleton pattern prevents reloads.

---

## Related Documents

- [specs/technical/architecture.md](../technical/architecture.md) -- system architecture and code structure
- [AGENTS.md](../../../AGENTS.md) -- development workflow and agent instructions
- [Testing Standards](testing-standards.md) -- test patterns and conventions
