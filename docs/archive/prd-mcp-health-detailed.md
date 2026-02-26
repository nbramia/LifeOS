# PRD: MCP Tool — `lifeos_health_detailed`

## Summary

Improve MCP health tool to expose per-service detail instead of a single "healthy/degraded" summary.

## Current State

- `lifeos_health` exists in MCP but its custom formatter discards per-service detail.
- The API endpoint `GET /health/services` returns rich per-service status, degradation events, and critical issues.
- Claude Code sees only "LifeOS is healthy" or "LifeOS is degraded" with no actionable detail.

## What Needs to Happen

Either fix the existing `lifeos_health` formatter to include per-service detail, or add a new `lifeos_health_detailed` tool pointing at `/health/services`.

**Recommendation:** Fix the existing tool's formatter. No need for a second health tool.

## Improved Formatter Output

Instead of:
```
LifeOS Status: healthy
```

Return:
```
LifeOS Status: healthy

Services:
  chromadb: healthy
  embedding_model: healthy
  vault_filesystem: healthy
  ollama: healthy
  bm25_index: healthy
  google_calendar: healthy (last sync: 2h ago)
  google_gmail: healthy (last sync: 3h ago)
  telegram: healthy

Degradation Events (24h): 0
Critical Issues: none
```

## Success Criteria

- [ ] `lifeos_health` returns per-service status breakdown
- [ ] Degradation events from last 24h are included
- [ ] Critical issues are highlighted
- [ ] Last sync time is shown for sync-dependent services

## Test Coverage

- [ ] Healthy system returns full breakdown
- [ ] Degraded service is clearly identified
- [ ] Degradation event count is accurate
