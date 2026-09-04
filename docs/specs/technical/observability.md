# Observability

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-09-04

Performance tracing and alerting for LifeOS.

---

## Performance Tracing

Every chat request is automatically traced with per-stage timing. Traces are stored in SQLite (`data/perf_traces.db`) and exposed via API.

### How It Works

- `start_trace()` / `finish_trace()` bookend each request in `chat.py`'s `generate()`
- `trace_span("name")` context manager records wall-clock time for each stage
- Uses Python `contextvars` for async-safe propagation through await chains
- The SSE stream emits a `perf_trace` event (before `done`) with trace_id, total_ms, and all spans

### Instrumented Stages

| Span | Location | Parent |
|------|----------|--------|
| `intent_classify` | chat.py | — |
| `query_expand` | chat.py | — |
| `memory_inject` | agent_loop.py | — |
| `claude_api_round_{n}` | agent_loop.py | — |
| `tool_{name}` | agent_loop.py | — |
| `search_name_expand` | hybrid_search.py | `tool_search_vault` |
| `search_vector` | hybrid_search.py | `tool_search_vault` |
| `search_bm25` | hybrid_search.py | `tool_search_vault` |
| `search_rrf_boost` | hybrid_search.py | `tool_search_vault` |
| `search_rerank` | hybrid_search.py | `tool_search_vault` |

### Key Files

| File | Purpose |
|------|---------|
| `api/services/perf_trace.py` | Request-level performance tracing (spans, SQLite persistence) |
| `api/routes/perf.py` | Performance trace query API (`/api/perf/traces`, `/api/perf/stats`) |
| `tests/test_perf_benchmark.py` | Benchmark suite for query performance and quality |

### API Endpoints

```bash
# Aggregate stats (avg/p50/p95/max per stage)
curl http://localhost:8000/api/perf/stats | jq

# Recent traces
curl "http://localhost:8000/api/perf/traces?limit=10" | jq

# Single trace with all spans
curl http://localhost:8000/api/perf/traces/{trace_id} | jq
```

### Benchmark Suite

`tests/test_perf_benchmark.py` runs queries against a live server, collects perf traces, validates answer quality, and prints a comparison report.

```bash
# Run benchmarks (requires running server)
ssh <user>@<server-ip> "cd ~/Code/LifeOS && ~/.venvs/lifeos/bin/python -m pytest tests/test_perf_benchmark.py -v -s"
```

Test queries and expected results are defined in `BENCHMARK_QUERIES` within the test file. Personal names/topics can be overridden via `tests/fixtures/benchmark_config.json` (gitignored).

---

## Route Timing

Every HTTP request is timed, independently of the chat-turn tracing above. Where perf tracing follows one chat turn through its LLM/tool stages, route timing answers a different question -- "what's slow right now, across the whole API" -- and catches regressions perf tracing can't see, like a CRM endpoint whose response quietly grows to ten seconds with no chat turn involved at all.

### How It Works

- `RouteTimingMiddleware` (`api/services/route_timing.py`) is a pure-ASGI middleware, registered last in `api/main.py` so it wraps outermost around this app's own middleware (CORS, the scoped gzip middleware) -- its duration and byte count cover the full response, including compression.
- Being pure ASGI rather than `BaseHTTPMiddleware` means it never buffers a response: every chunk is passed straight through and the middleware waits for the stream's final chunk before recording. A non-SSE streaming response is timed to that final chunk; an SSE response is recorded by count and bytes only (below), never a duration.
- Routes are keyed by **route template**, not the raw request path -- `request.scope["route"].path` (e.g. `/api/crm/people/{person_id}`), read after routing resolves it, falling back to `"<unmatched>"` for a 404. A raw path or query string never reaches a log line or the in-memory summary, so a person id in a URL is never exposed.
- `/health*` and `/static/*` are excluded entirely (too frequent/trivial to matter), matched as a full path segment (like `_in_gzip_scope` in `api/main.py`) so a future `/healthz-admin` or `/staticmaps` route isn't swept in by accident. Everything else -- every `/api/*` call and every page route (`/crm`, `/me`, `/family`, `/relationship`, `/birthdays`, ...) -- is timed and recorded.
- A request slower than `LIFEOS_SLOW_REQUEST_MS` (default 500ms) logs one WARNING with method, route template, status, duration, and response bytes. A request that raises before any response was sent is recorded and logged (if slow) with status 500; one that raises *after* its response already started (a stream that dies mid-flight) logs the status actually sent to the client instead, plus `aborted=true` -- never a synthetic 500 the client never saw.
- A `text/event-stream` response (SSE) is tracked separately and never subject to any of the above: an SSE connection's duration is however long the client kept its tab or transcript viewer open, not a latency measurement, so timing it like a normal request would let a page-open artifact dominate the summary and fire a false slow-request warning on every disconnect. `RouteTimingStore.record_stream()` keeps only count and total bytes per route, no duration/percentiles/slow_count.
- Per-route stats live in `RouteTimingStore`, a thread-safe, bounded rolling window (last 200 samples) per `(method, route template)`. Process-local and reset on restart -- this is a live signal, not persisted history.

### API Endpoint

```bash
# Per-route rolling summary: count, p50/p95/max duration, slow_count, last_slow_at,
# plus a separate "streams" list (SSE routes: count and total bytes only)
curl http://localhost:8000/api/perf/routes | jq
```

### Key Files

| File | Purpose |
|------|---------|
| `api/services/route_timing.py` | `RouteTimingMiddleware` and `RouteTimingStore` |
| `api/routes/perf.py` | Adds `GET /api/perf/routes` to the existing perf router |
| `config/settings.py` | `slow_request_ms` (`LIFEOS_SLOW_REQUEST_MS`, default 500) |
| `tests/test_route_timing.py` | Middleware, store, endpoint, overhead, and thread-safety tests |

---

## Log Redaction

`api/services/log_redaction.py` installs two logging filters at process startup (`configure_telegram_log_redaction()`): a Telegram bot token redaction filter, and `RequestQueryStringRedactionFilter` / `install_query_string_redaction_filter()`, which strips everything from the first `?` onward in uvicorn's own access-log line (`uvicorn.access`) for every route. This exists because that access logger writes the full request line — path *and* query string — at INFO regardless of what a route handler itself logs, so raw text typed into a search box (e.g. `GET /api/crm/people?q=<text>`) would otherwise still reach `logs/server.log` even after a handler-level fix. `RouteTimingMiddleware` above already keys its own log lines and in-memory summary by route template rather than raw path for the same reason, but that guarantee is specific to route timing — this filter is what covers the access logger itself, for every route.

### Key Files

| File | Purpose |
|------|---------|
| `api/services/log_redaction.py` | `RequestQueryStringRedactionFilter`, `TelegramTokenRedactionFilter`, `configure_telegram_log_redaction()` |

---

## Alerting

### Severity Levels

| Severity | When Sent | Examples |
|----------|-----------|----------|
| **CRITICAL** | Immediately (rate-limited) | ChromaDB down, embedding model failed, vault inaccessible |
| **WARNING** | Batched nightly (7 AM ET) | Local LLM unavailable, backup failed, >5 degradation events |
| **INFO** | Log only | Telegram retry, config defaults used |

### Rate Limiting

CRITICAL alerts use rate limiting to avoid alert storms:
- Only sent on state transition (healthy → failed), not repeated failures
- 5-minute cooldown between alerts for the same service (handles flapping)

### Alert Delivery

Alerts are sent via both channels simultaneously:
- **Email**: Sent via Gmail API using `LIFEOS_ALERT_EMAIL` as recipient
- **Telegram**: Sent via Telegram bot API if `telegram_bot_token` and `telegram_chat_id` are configured

Both channels are attempted on every alert. Email failure does not block Telegram delivery and vice versa.

### Alert Configuration

Set in `.env`:
- `LIFEOS_ALERT_EMAIL` - Email address for alerts
- `telegram_bot_token` + `telegram_chat_id` - Telegram channel

### Tracked Services

| Service | Severity | Fallback |
|---------|----------|----------|
| `chromadb` | CRITICAL | None (core functionality) |
| `embedding_model` | CRITICAL | None (core functionality) |
| `vault_filesystem` | CRITICAL | None (core functionality) |
| `bm25_index` | WARNING | Vector-only search |
| `google_calendar` | WARNING | Cached data |
| `google_gmail` | WARNING | Cached data |
| `backup_storage` | WARNING | Skips backup |
| `telegram` | INFO | Email-only alerts |

### Maintenance Mode

Suppress CRITICAL alerts during planned operations (nightly sync, manual ChromaDB restart, etc.) to avoid false alarms.

- **Enter**: `POST /api/admin/maintenance?duration_seconds=14400` (default: 4 hours, auto-expires)
- **Exit early**: `DELETE /api/admin/maintenance`

While in maintenance mode, CRITICAL alerts from `service_health` are suppressed. WARNING and INFO severity are unaffected.

### Degradation Tracking

When a service fails and a fallback is used, this is recorded as a "degradation event". These are collected and reported in the nightly health check if there are 5+ in 24 hours.

Services are tracked on-use, not by polling. Status updates when a service is actually called.

### Sync Duration-Collapse Detection

`run_all_syncs` flags a successful sync run that completed suspiciously fast — the source typically takes >60s but finished in under max(2s, 5% of typical) — the signature of a silent no-op. Detection records a `duration_collapse` row in `sync_errors` and adds a "Suspiciously fast" section to the Telegram sync summary; the `sync_runs` row stays `success`, so the health dashboard still shows green for that run.

## Related Documents

- [Architecture](architecture.md) -- System architecture and code structure
- [Data & Sync](data-and-sync.md) -- Data pipeline (alerting on sync failures)
- [API Reference](../product/api-reference.md#get-apiperfroutes) -- `GET /api/perf/routes` request/response shape
- [Operations](../../guides/operations.md) -- Quick commands for the perf-tracing and alerting endpoints documented here
- [Configuration](../../guides/configuration.md) -- `LIFEOS_SLOW_REQUEST_MS`, `LIFEOS_VRAM_ALERT_PCT`, and other env vars this doc's behavior depends on
