"""
Performance tracing for LifeOS request pipeline.

Lightweight span recorder using contextvars for async-safe context propagation.
Persists traces to SQLite for analysis via /api/perf/* endpoints.

Usage:
    from api.services.perf_trace import start_trace, trace_span, finish_trace

    start_trace(conversation_id, question, model_tier)
    with trace_span("search_vector", parent="tool_search_vault"):
        results = vector_store.search(...)
    trace = finish_trace()
"""
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Context variable holding the active trace for this async task
_current_trace: ContextVar[Optional["Trace"]] = ContextVar("_current_trace", default=None)


@dataclass
class Span:
    name: str
    duration_ms: float = 0.0
    parent: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Trace:
    trace_id: str
    conversation_id: str
    question: str
    model_tier: str
    spans: list[Span] = field(default_factory=list)
    created_at: str = ""
    _start_time: float = field(default=0.0, repr=False)

    @property
    def total_ms(self) -> float:
        return (time.monotonic() - self._start_time) * 1000 if self._start_time else 0.0


def start_trace(conversation_id: str, question: str, model_tier: str = "") -> Trace:
    """Start a new trace for the current async context."""
    trace = Trace(
        trace_id=uuid.uuid4().hex[:12],
        conversation_id=conversation_id,
        question=question[:200],
        model_tier=model_tier,
        created_at=datetime.now().isoformat(),
        _start_time=time.monotonic(),
    )
    _current_trace.set(trace)
    return trace


@contextmanager
def trace_span(name: str, parent: Optional[str] = None, **metadata):
    """Time a code block and record it as a span on the current trace."""
    trace = _current_trace.get()
    if trace is None:
        yield
        return

    t0 = time.monotonic()
    try:
        yield
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        trace.spans.append(Span(
            name=name,
            duration_ms=round(duration_ms, 2),
            parent=parent,
            metadata=metadata if metadata else {},
        ))


def finish_trace() -> Optional[Trace]:
    """Finish the current trace, persist it, and return it."""
    trace = _current_trace.get()
    if trace is None:
        return None

    _current_trace.set(None)

    try:
        store = get_perf_trace_store()
        store.save_trace(trace)
    except Exception as e:
        logger.warning(f"Failed to persist perf trace: {e}")

    return trace


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

class PerfTraceStore:
    """SQLite store for performance traces."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(settings.chroma_path).parent / "perf_traces.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    question TEXT,
                    model_tier TEXT,
                    total_ms REAL,
                    created_at TEXT NOT NULL,
                    span_data TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_conv ON traces(conversation_id)")
            conn.commit()

    def save_trace(self, trace: Trace):
        span_data = json.dumps([asdict(s) for s in trace.spans])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO traces (trace_id, conversation_id, question, model_tier, total_ms, created_at, span_data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (trace.trace_id, trace.conversation_id, trace.question,
                 trace.model_tier, round(trace.total_ms, 2), trace.created_at, span_data),
            )
            conn.commit()

    def get_trace(self, trace_id: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def get_traces(
        self,
        conversation_id: str = None,
        since: str = None,
        limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM traces WHERE 1=1"
        params = []
        if conversation_id:
            query += " AND conversation_id = ?"
            params.append(conversation_id)
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_stage_stats(self, since: str = None, limit: int = 100) -> dict:
        """Aggregate avg/p50/p95/max per span name across recent traces."""
        query = "SELECT span_data FROM traces WHERE 1=1"
        params = []
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        # Collect durations per stage
        stage_durations: dict[str, list[float]] = {}
        for (span_data_json,) in rows:
            spans = json.loads(span_data_json)
            for s in spans:
                name = s["name"]
                stage_durations.setdefault(name, []).append(s["duration_ms"])

        # Compute stats
        stages = {}
        for name, durations in sorted(stage_durations.items()):
            durations.sort()
            n = len(durations)
            stages[name] = {
                "avg_ms": round(sum(durations) / n, 1),
                "p50_ms": round(durations[n // 2], 1),
                "p95_ms": round(durations[int(n * 0.95)], 1) if n >= 2 else round(durations[-1], 1),
                "max_ms": round(durations[-1], 1),
                "count": n,
            }

        return {"trace_count": len(rows), "stages": stages}

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        d["spans"] = json.loads(d.pop("span_data"))
        return d


# Singleton
_perf_trace_store: Optional[PerfTraceStore] = None


def get_perf_trace_store() -> PerfTraceStore:
    global _perf_trace_store
    if _perf_trace_store is None:
        _perf_trace_store = PerfTraceStore()
    return _perf_trace_store


def reset_perf_trace_store() -> None:
    """Reset singleton (for testing)."""
    global _perf_trace_store
    _perf_trace_store = None
