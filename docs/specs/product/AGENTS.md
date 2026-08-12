This directory contains product specifications — what the system does from a consumer perspective.

## Contents

- `agent-viz.md` — The `/agents` page (D3 graph + side panel) that visualizes agent worker and Claude Code sessions
- `agent-worker.md` — The `#agent`-tagged task workflow (Telegram-triggered autonomous worker)
- `api-crm.md` — CRM-specific HTTP endpoints (people, interactions, graph data)
- `api-reference.md` — HTTP endpoint catalog (request/response shapes, query parameters)
- `chat-ui.md` — The web chat interface
- `claude-code-orchestration.md` — How LifeOS exposes itself to Claude Code (skills, MCP, orchestration patterns)
- `crm-analytics.md` — CRM dashboards (Family / Me / Birthdays / Relationship)
- `crm-graph.md` — CRM graph view (pan/zoom/drag, edge semantics)
- `crm-interactions.md` — CRM interaction timeline
- `crm-people.md` — CRM people list and person detail views
- `crm-ui.md` — `/crm` page index (links out to the per-view specs above)
- `data-model.md` — Canonical entities (SourceEntity, PersonEntity) and relationships
- `entity-resolution.md` — How emails, phones, and names link to canonical people
- `journal-analytics.md` — The daily-journal emotion-wheel view (`/journal`)
- `mcp-tools.md` — The MCP tool catalog exposed to Claude Code and other agents
- `task-management.md` — Obsidian-backed task system (create/list/update/complete)

## Key Principles

- Product specs describe **WHAT** (consumer view) — feature behavior, API contracts, semantics. Implementation details belong in `technical/`. If you find yourself describing route handlers, schema, or library internals, that content should move.
- Data model specs define what entities *mean* and how they relate. Structural details (SQLite schema, ChromaDB collections, indexes) go in `technical/`.
- API contracts describe the consumer-facing shape (path, parameters, response schema). Route-handler structure and middleware go in `technical/`.
- Every product spec must include frontmatter: `Status`, `Last Updated`, `Owner`.

## Related Documents

- [Documentation Strategy](../../AGENTS.md) — Rules governing all documentation
- [Technical Specs](../technical/) — Implementation counterpart to these product specs
