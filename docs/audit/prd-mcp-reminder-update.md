# PRD: MCP Tool — `lifeos_reminder_update`

## Summary

Expose the existing `PUT /api/reminders/{reminder_id}` endpoint as an MCP tool so Claude Desktop and Claude Code can modify existing reminders.

## Current State

- The MCP server has `lifeos_reminder_create`, `lifeos_reminder_list`, and `lifeos_reminder_delete` — but no update.
- The API endpoint exists at `api/routes/reminders.py:148` accepting PUT.
- Users can create and delete reminders via MCP but cannot reschedule or change the message without delete-and-recreate.

## What Needs to Happen

Add the MCP tool `lifeos_reminder_update` to `mcp_server.py` pointing at the existing PUT endpoint.

## MCP Tool Definition

```python
{
    "name": "lifeos_reminder_update",
    "description": "Update an existing reminder's schedule, message, or other properties. Use lifeos_reminder_list to find reminder IDs. Only provided fields are changed. FOLLOW-UP TOOLS: Use lifeos_reminder_list to verify the update.",
    "endpoint": "/api/reminders/{reminder_id}",
    "method": "PUT",
    "params": {
        "reminder_id": {"type": "string", "description": "Reminder ID (from lifeos_reminder_list)", "required": True},
        "name": {"type": "string", "description": "Display name for the reminder", "required": False},
        "schedule_type": {"type": "string", "description": "Schedule type: 'once' or 'cron'", "required": False},
        "schedule_value": {"type": "string", "description": "ISO datetime for 'once', cron expression for 'cron'", "required": False},
        "message_type": {"type": "string", "description": "Message type: 'static', 'prompt', or 'endpoint'", "required": False},
        "message_content": {"type": "string", "description": "The message text, prompt, or endpoint path", "required": False},
        "timezone": {"type": "string", "description": "IANA timezone (e.g., 'America/New_York')", "required": False},
        "enabled": {"type": "boolean", "description": "Whether the reminder is active", "required": False}
    }
}
```

## Success Criteria

- [ ] Claude Code can reschedule a reminder without recreating it
- [ ] Claude Code can change a reminder's message content
- [ ] Claude Code can enable/disable a reminder
- [ ] Partial updates work (changing only schedule doesn't affect message)

## Test Coverage

- [ ] Update schedule_value only — other fields unchanged
- [ ] Update message_content only — other fields unchanged
- [ ] Toggle enabled flag
- [ ] Invalid reminder_id returns appropriate error
- [ ] Invalid cron expression returns validation error
- [ ] Verify update persisted via subsequent reminder list
