# PRD: MCP Tool — `lifeos_sync_trigger`

## Summary

Expose sync trigger capabilities as an MCP tool so Claude Desktop and Claude Code can kick off data sync for specific sources on demand.

## Current State

- Multiple sync trigger endpoints exist across the codebase:
  - `POST /api/admin/reindex` — full vault reindex
  - `POST /api/admin/calendar/sync` — calendar sync
  - `POST /api/crm/sources/{source_type}/sync` — per-source sync
  - `POST /api/crm/slack/sync` — Slack sync
  - `POST /api/crm/contacts/sync` — Apple Contacts sync
  - `POST /api/photos/sync` — Photos sync
- None of these are exposed via MCP.
- Claude Code currently cannot trigger a sync after making changes.

## What Needs to Happen

Add the MCP tool `lifeos_sync_trigger` to `mcp_server.py`. Use a single tool with a `source` parameter rather than one tool per sync source, to keep the tool surface clean.

## MCP Tool Definition

```python
{
    "name": "lifeos_sync_trigger",
    "description": "Trigger a data sync for a specific source. Use 'vault' to reindex the Obsidian vault, 'calendar' for Google Calendar, 'contacts' for Apple Contacts, 'slack' for Slack, 'photos' for Apple Photos, or a source type like 'gmail', 'imessage', 'whatsapp' for CRM source sync. Returns immediately — sync runs in background. FOLLOW-UP TOOLS: Use lifeos_health to check sync status.",
    "endpoint": "dynamic",
    "method": "POST",
    "params": {
        "source": {"type": "string", "description": "Sync source: vault, calendar, contacts, slack, photos, gmail, imessage, whatsapp, phone, facetime, linkedin", "required": True}
    }
}
```

## Implementation Notes

This tool requires a custom handler (not simple endpoint mapping) because it routes to different endpoints based on the `source` parameter:
- `vault` → `POST /api/admin/reindex`
- `calendar` → `POST /api/admin/calendar/sync`
- `contacts` → `POST /api/crm/contacts/sync`
- `slack` → `POST /api/crm/slack/sync`
- `photos` → `POST /api/photos/sync`
- Other source types → `POST /api/crm/sources/{source}/sync`

Alternatively, create a unified `POST /api/admin/sync/{source}` endpoint that does this routing server-side, then point the MCP tool at that single endpoint.

## Success Criteria

- [ ] Claude Code can trigger vault reindex after editing notes
- [ ] Claude Code can trigger calendar sync to get fresh events
- [ ] All sync sources are reachable via the single tool
- [ ] Tool returns immediately (sync runs async/background)
- [ ] Invalid source returns helpful error listing valid sources

## Test Coverage

- [ ] Trigger vault reindex — returns success
- [ ] Trigger calendar sync — returns success
- [ ] Trigger with invalid source — returns error with valid source list
- [ ] Verify sync actually runs (check sync health after trigger)
