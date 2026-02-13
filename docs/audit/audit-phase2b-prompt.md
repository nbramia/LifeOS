# Phase 2b: Expand MCP Tool Coverage

## Goal

Add all missing high-value MCP tools (person update, reminder update, sync trigger, person facts CRUD) and fix the health formatter and HTTP method handling. After this phase, Claude Code can both read AND write to LifeOS.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "5. Expand MCP Tool Coverage" (sub-items 5.2–5.7)
- Each PRD file for specifications (all in `docs/audit/`):
  - `docs/audit/prd-mcp-update-person.md` (5.2)
  - `docs/audit/prd-mcp-reminder-update.md` (5.3)
  - `docs/audit/prd-mcp-trigger-sync.md` (5.4)
  - `docs/audit/prd-mcp-person-facts.md` (5.5)
  - `docs/audit/prd-mcp-health-detailed.md` (5.6)
- `docs/audit/archive/audit-mcp.md` — Round 1 MCP audit (tool inventory, quality assessment)
- `docs/audit/archive/audit-round2-mcp.md` — MCP cross-pollination (tool chain patterns, write gaps)
- `CLAUDE.md` — Project conventions

## Prior Phase State

Read the "After Phase 0" and "After Phase 1" sections in `docs/audit/audit-implementation-plan.md` for what changed in previous phases.

## Implementation Order

Do these in this exact order. 5.7 is a prerequisite for the write tools.

### Step 1: Fix `_call_api` HTTP method support (5.7)
Add explicit PATCH, PUT, DELETE handling in `mcp_server.py`'s `_call_api` method. Currently these work by accident via POST fallback. Make them explicit.

**This must be done first** — 5.2, 5.3, and 5.5 depend on it.

### Step 2: Add `lifeos_person_update` (5.2)
Wire the existing `PATCH /api/crm/people/{person_id}` endpoint as an MCP tool.

**See full spec:** `prd-mcp-update-person.md`

### Step 3: Add `lifeos_reminder_update` (5.3)
Wire the existing `PUT /api/reminders/{reminder_id}` endpoint as an MCP tool.

**See full spec:** `prd-mcp-reminder-update.md`

### Step 4: Add `lifeos_sync_trigger` (5.4)
Create a unified sync trigger tool that routes to the appropriate endpoint based on a `source` parameter. May require a new unified endpoint or a custom handler.

**See full spec:** `prd-mcp-trigger-sync.md`

### Step 5: Add person facts CRUD (5.5)
Three new tools: `lifeos_person_fact_update`, `lifeos_person_fact_confirm`, `lifeos_person_fact_delete`.

**See full spec:** `prd-mcp-person-facts.md`

### Step 6: Fix `lifeos_health` formatter (5.6)
Update the custom formatter to include per-service status instead of discarding detail.

**See full spec:** `prd-mcp-health-detailed.md`

## Files to Explore

- `mcp_server.py` — The MCP server (all tool definitions, `_call_api`, custom formatters)
- `api/routes/crm.py` — Person update and facts endpoints
- `api/routes/reminders.py` — Reminder update endpoint
- `api/routes/admin.py` — Sync trigger and health endpoints
- `api/routes/calendar.py` — Calendar endpoints (already in MCP, but check)
- `api/main.py` — Health check endpoint and service status

## Boundaries

- Do NOT touch `chat.py` or the chat pipeline (that's Phase 2a)
- Do NOT touch agent memory (that's Phase 2c)
- Do NOT add tools beyond what's specified in the PRDs
- Do NOT restructure the MCP server architecture
- Follow the existing tool definition pattern in `mcp_server.py`
- Use the `FOLLOW-UP TOOLS` pattern in descriptions (gold standard: `lifeos_people_search`)

## Verification

For each new tool, verify:
1. Tool appears in MCP tool list (Claude Desktop can see it)
2. Tool executes successfully with valid parameters
3. Tool returns helpful errors with invalid parameters
4. Tool description is clear enough for Claude to use correctly

Overall:
5. All existing MCP tools still work
6. All existing tests pass: `./scripts/test.sh`
7. MCP server starts without errors
8. Server starts cleanly: `./scripts/server.sh restart`

## Rollback

Revert the commit. All changes are additions to `mcp_server.py`. No existing behavior is modified.
