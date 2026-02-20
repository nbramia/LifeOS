# Phase 2c: Agent Memory and Conversation Context

## Goal

Give the agentic pipeline persistent memory across conversations. Users can say "remember that..." to save preferences, facts, and decisions. Relevant memories are retrieved and injected into the agent's context automatically.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "7. Agent Memory and Conversation Context"
- `docs/audit/archive/audit-round2-telegram.md` — Context & memory analysis, 5 context layers
- `docs/audit/archive/audit-round3-blue-sky.md` — Knowledge amplification vision
- `docs/audit/archive/audit-round3-devils-advocate.md` — Memory complexity warnings
- `CLAUDE.md` — Project conventions

## Prior Phase State

Read the "After Phase 0" and "After Phase 1" sections in `docs/audit/audit-implementation-plan.md` for what changed in previous phases.

## What Needs to Happen

### 1. Create Memory Storage
A `memories` table in SQLite (use the existing database pattern). Each memory has:
- `id` (primary key)
- `content` (the memory text)
- `created_at` (timestamp)
- `source` (e.g., "explicit" for user-triggered, future: "extracted")
- `embedding` (vector for semantic retrieval, stored in ChromaDB)

### 2. Add Explicit Save
When a user says "remember that..." or "remember:" or similar patterns, extract the fact and save it. This should work in both Telegram and via MCP.

**Explore:** Check if `lifeos_memories_create` and `lifeos_memories_search` MCP tools already exist (they may). If so, build on that infrastructure rather than creating a parallel system.

### 3. Add Semantic Retrieval
Before the agentic pipeline generates a response, retrieve the top N most relevant memories based on the user's message. Inject them into the system prompt as additional context.

### 4. Inject into Agent System Prompt
The retrieved memories should appear in the agent's system prompt as a "Things I know about you" section. Keep it bounded — top 5-10 most relevant, not all memories.

## Files to Explore

- `api/services/agent_tools.py` — Current agentic tool definitions and system prompt
- `api/services/chat.py` — Where the system prompt is constructed
- `mcp_server.py` — Check for existing `lifeos_memories_*` tools
- `api/routes/` — Check for existing memory endpoints
- `api/services/` — Check for existing memory service
- ChromaDB integration code — How embeddings are stored/queried elsewhere

## Boundaries

- Do NOT build automatic memory extraction from conversations (start with explicit only)
- Do NOT build memory management UI
- Do NOT touch the chat pipeline routing (that's Phase 2a)
- Do NOT touch MCP tool definitions beyond memory tools (that's Phase 2b)
- Keep memory count bounded — if there are 1000+ memories, retrieval must still be fast
- Start simple: explicit save, semantic retrieve, inject into prompt

## Verification

1. "Remember that I prefer morning meetings" saves a memory
2. When asking about scheduling preferences later, the memory is surfaced
3. Memories persist across server restarts
4. Memory retrieval is fast (< 500ms even with many memories)
5. Irrelevant memories don't pollute the context
6. All existing tests pass: `./scripts/test.sh`
7. Server starts cleanly: `./scripts/server.sh restart`

## Rollback

New table and service. Rollback by reverting the commit. No existing data is modified.
