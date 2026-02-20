# PRD: MCP Tool — `lifeos_person_update`

## Summary

Expose the existing `PATCH /api/crm/people/{person_id}` endpoint as an MCP tool so Claude Desktop and Claude Code can update person profiles (notes, tags, category, birthday).

## Current State

- The API endpoint exists at `api/routes/crm.py:748` accepting PATCH with fields: `notes`, `tags`, `category`, `birthday`.
- The MCP server has read tools (`lifeos_person_profile`, `lifeos_person_facts`, etc.) but no write tools for people.
- Claude can look up a person's profile but cannot update it.

## What Needs to Happen

Add the MCP tool `lifeos_person_update` to `mcp_server.py` pointing at the existing PATCH endpoint.

## MCP Tool Definition

```python
{
    "name": "lifeos_person_update",
    "description": "Update a person's profile in the CRM. Can set notes, tags, category, or birthday. Requires entity_id — use lifeos_people_search first to find the person. Fields not provided are left unchanged. FOLLOW-UP TOOLS: Use lifeos_person_profile to verify the update.",
    "endpoint": "/api/crm/people/{entity_id}",
    "method": "PATCH",
    "params": {
        "entity_id": {"type": "string", "description": "Person entity ID (from lifeos_people_search)", "required": True},
        "notes": {"type": "string", "description": "Free-text notes about the person (replaces existing notes)", "required": False},
        "tags": {"type": "array", "description": "Classification tags (replaces existing tags)", "required": False},
        "category": {"type": "string", "description": "Category: work, personal, family, or other", "required": False},
        "birthday": {"type": "string", "description": "Birthday in MM-DD format (month-day only). Empty string to clear.", "required": False}
    }
}
```

## Implementation Notes

- The `_call_api` method in `mcp_server.py` may need to explicitly handle PATCH requests. The audit noted that PUT/PATCH "work by accident through the POST fallback" — this should be made explicit.
- The tool description must include the `FOLLOW-UP TOOLS` pattern (gold standard from `lifeos_people_search`).

## Success Criteria

- [ ] Claude Code can update a person's notes, tags, category, and birthday via MCP
- [ ] Partial updates work (sending only `notes` doesn't clear `tags`)
- [ ] Birthday validation works (rejects invalid formats)
- [ ] Empty string birthday clears the field
- [ ] The `_call_api` method handles PATCH explicitly (not via POST fallback)

## Test Coverage

- [ ] Update notes only — other fields unchanged
- [ ] Update tags only — other fields unchanged
- [ ] Update birthday with valid MM-DD format
- [ ] Clear birthday with empty string
- [ ] Invalid entity_id returns appropriate error
- [ ] Invalid birthday format returns validation error
- [ ] Verify update persisted via subsequent profile read
