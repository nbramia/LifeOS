# Operations Reference

> **Status:** Complete
> **Owner:** Operations
> **Last Updated:** 2026-07-07
> **Audience:** Operators

Operational procedures that don't belong in the day-to-day coding reference: the Apple Data Agent, Monarch Money auth, and quick observability commands. Moved here from `AGENTS.md` to keep the agent-facing file lean.

---

## Apple Data Agent (optional, macOS only)

If you have a Mac with iMessage/phone data, it can export Apple ecosystem data and sync it to the Linux server nightly via `scripts/apple_data_agent.sh`.

### macOS FDA (Full Disk Access)

`/Applications/LifeOS.app` is a bash-script-based .app bundle with **Full Disk Access**. macOS cron cannot access `~/Library/Messages/` without FDA, so the Apple Data Agent cron job routes through this wrapper.

If adding new cron jobs or scripts on macOS that need to access protected directories, route them through `LifeOS exec`.

## Monarch Money (Financial Data)

Auth uses a cached session token at `data/monarch_session.pickle`. Monthly sync runs on the 1st via `run_all_syncs.py` (phase 5). Live queries at `/api/monarch/*`.

Re-authenticate when token expires (401/525):
```bash
~/.venvs/lifeos/bin/python -c "
import asyncio
from monarchmoney import MonarchMoney
mm = MonarchMoney()
asyncio.run(mm.interactive_login())
mm.save_session('data/monarch_session.pickle')
print('Session saved!')
"
```

## Performance Tracing — Quick Commands

Every chat request is traced with per-stage timing (SQLite, `data/perf_traces.db`). Full design in [Observability](../specs/technical/observability.md).

```bash
curl http://localhost:8000/api/perf/stats | jq                    # Aggregate stats
curl "http://localhost:8000/api/perf/traces?limit=10" | jq        # Recent traces
curl http://localhost:8000/api/perf/traces/{trace_id} | jq        # Single trace
```

## Alerting Severities

| Severity | When Sent | Examples |
|----------|-----------|----------|
| **CRITICAL** | Immediately (rate-limited) | ChromaDB down, embedding model failed, vault inaccessible |
| **WARNING** | Batched nightly (7 AM ET) | LLM API errors, backup failed, >5 degradation events |
| **INFO** | Log only | Telegram retry, config defaults used |

Set `LIFEOS_ALERT_EMAIL` in `.env` for alerts. Telegram backup via `telegram_bot_token` + `telegram_chat_id`.

Services are tracked on-use, not by polling. Degradation events (fallback usage) are collected and reported in the nightly health check if there are 5+ in 24 hours.

---

## Related Documents

- [AGENTS.md](../../AGENTS.md) — Agent-facing project reference (points here for operational detail)
- [Apple Health Import](apple-health.md) — Health/Fitness data import specifics
- [Observability — Technical](../specs/technical/observability.md) — Perf tracing and monitoring design
- [Data & Sync](../specs/technical/data-and-sync.md) — Nightly sync pipeline phases
- [Troubleshooting](troubleshooting.md) — General operational troubleshooting
