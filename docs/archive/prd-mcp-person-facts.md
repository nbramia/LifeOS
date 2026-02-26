# PRD: MCP Tools — Person Facts CRUD

## Summary

Expose person fact management endpoints as MCP tools so Claude Desktop and Claude Code can view, update, confirm, and delete extracted facts about people.

## Current State

- `lifeos_person_facts` exists in MCP (read-only, returns all facts for a person)
- API endpoints exist for fact management but are not exposed via MCP:
  - `PUT /api/crm/people/{person_id}/facts/{fact_id}` — update a fact
  - `DELETE /api/crm/people/{person_id}/facts/{fact_id}` — delete a fact
  - `POST /api/crm/people/{person_id}/facts/{fact_id}/confirm` — confirm a fact
  - `POST /api/crm/people/{person_id}/facts/extract` — extract new facts from data
- Claude can read facts but cannot curate them (confirm correct ones, fix wrong ones, delete outdated ones).

## What Needs to Happen

Add three MCP tools to `mcp_server.py`:

### `lifeos_person_fact_update`

```python
{
    "name": "lifeos_person_fact_update",
    "description": "Update an extracted fact about a person. Use lifeos_person_facts first to get fact IDs. FOLLOW-UP TOOLS: Use lifeos_person_facts to verify the update.",
    "endpoint": "/api/crm/people/{entity_id}/facts/{fact_id}",
    "method": "PUT",
    "params": {
        "entity_id": {"type": "string", "required": True},
        "fact_id": {"type": "string", "required": True},
        "value": {"type": "string", "description": "Updated fact value", "required": False},
        "category": {"type": "string", "description": "Fact category (family, work, interests, dates, etc.)", "required": False}
    }
}
```

### `lifeos_person_fact_confirm`

```python
{
    "name": "lifeos_person_fact_confirm",
    "description": "Confirm an extracted fact as accurate. Confirmed facts are weighted higher in person profiles. Use lifeos_person_facts to find fact IDs.",
    "endpoint": "/api/crm/people/{entity_id}/facts/{fact_id}/confirm",
    "method": "POST",
    "params": {
        "entity_id": {"type": "string", "required": True},
        "fact_id": {"type": "string", "required": True}
    }
}
```

### `lifeos_person_fact_delete`

```python
{
    "name": "lifeos_person_fact_delete",
    "description": "Delete an incorrect or outdated fact about a person. Use lifeos_person_facts to find fact IDs.",
    "endpoint": "/api/crm/people/{entity_id}/facts/{fact_id}",
    "method": "DELETE",
    "params": {
        "entity_id": {"type": "string", "required": True},
        "fact_id": {"type": "string", "required": True}
    }
}
```

## Implementation Notes

- The `_call_api` method needs explicit DELETE support (same concern as PATCH).
- These three tools together with the existing `lifeos_person_facts` (read) complete full CRUD for person facts.

## Success Criteria

- [ ] Claude Code can correct a wrong fact about a person
- [ ] Claude Code can confirm accurate facts
- [ ] Claude Code can delete outdated facts
- [ ] DELETE and PUT HTTP methods work explicitly in `_call_api` (not via fallback)

## Test Coverage

- [ ] Update a fact's value — verify persisted
- [ ] Update a fact's category — verify persisted
- [ ] Confirm a fact — verify confirmed status
- [ ] Delete a fact — verify removed from list
- [ ] Invalid entity_id returns appropriate error
- [ ] Invalid fact_id returns appropriate error
