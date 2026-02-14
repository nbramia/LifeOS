# PRD: MCP Tool — `lifeos_vault_read`

## Summary

Expose the existing `read_vault_file` capability (currently available only to the Telegram agent) as an MCP tool so Claude Desktop and Claude Code can read full Obsidian vault files.

## Current State

- The Telegram agent has `read_vault_file` in `api/services/agent_tools.py:304-320` with fuzzy filename matching.
- The MCP server (`mcp_server.py`) has `lifeos_search` for searching vault chunks, but no way to read a full file.
- There is no REST API endpoint for reading a vault file by name — the Telegram agent reads directly from the filesystem.

## What Needs to Happen

1. **Add a REST API endpoint** `GET /api/vault/files/{filename}` that reads a vault file by name with the same fuzzy matching logic used by the Telegram agent.
2. **Add the MCP tool** `lifeos_vault_read` to `mcp_server.py` pointing at this endpoint.

## MCP Tool Definition

```python
{
    "name": "lifeos_vault_read",
    "description": "Read the full content of a file from the Obsidian vault by name. Supports fuzzy matching — provide the filename (e.g., 'Taylor.md' or 'Taylor'). Use after lifeos_search finds a relevant file but only returns partial chunks. FOLLOW-UP TOOLS: Use lifeos_ask to synthesize information from multiple files.",
    "endpoint": "/api/vault/files/{filename}",
    "method": "GET",
    "params": {
        "filename": {"type": "string", "description": "File name to read (fuzzy matched)", "required": True}
    }
}
```

## API Endpoint Spec

- **Route**: `GET /api/vault/files/{filename}`
- **Parameters**: `filename` (path param, string)
- **Response 200**: `{"filename": "Taylor.md", "path": "People/Taylor.md", "content": "...", "modified": "2026-02-01T..."}`
- **Response 404**: `{"error": "File not found", "suggestions": ["Taylor Smith.md", "Taylor Notes.md"]}`
- **Matching priority**: Exact → case-insensitive → substring → no match with suggestions

## Success Criteria

- [ ] Claude Code can read any vault file by name via the MCP tool
- [ ] Fuzzy matching works identically to the Telegram agent's implementation
- [ ] 404 returns helpful suggestions for near-matches
- [ ] File content is returned in full (not chunked)

## Test Coverage

- [ ] Exact filename match returns correct file
- [ ] Case-insensitive match works (e.g., "taylor" finds "Taylor.md")
- [ ] Substring match works (e.g., "taylor" finds "People/Taylor Smith.md")
- [ ] Non-existent file returns 404 with suggestions
- [ ] Files with special characters in names are handled
- [ ] Large files are returned completely
