# Observability

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-02-19

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
| `model_select` | chat.py | — (records a recommended tier in the trace; the agent loop currently uses the configured `LIFEOS_ANTHROPIC_MODEL` regardless) |
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
| `ollama` | WARNING | Local LLM fallback → pattern matching |
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

## Related Documents

- [Architecture](architecture.md) -- System architecture and code structure
- [Data & Sync](data-and-sync.md) -- Data pipeline (alerting on sync failures)
