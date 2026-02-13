# Phase 2a: Unify the Chat Pipeline

## Goal

Consolidate the legacy intent classifier and the agentic loop into a single pipeline, making the agentic loop the sole entry point for all chat messages while preserving the intent classifier as a fast-path optimization within it.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "4. Unify the Chat Pipeline"
- `docs/audit/archive/audit-telegram-chat.md` — Full pipeline analysis, both dispatch systems, tool inventory
- `docs/audit/archive/audit-round2-telegram.md` — Unified pipeline design, "do anything" architecture
- `docs/audit/archive/audit-round3-devils-advocate.md` — Skim for warnings about this change
- `CLAUDE.md` — Project conventions

## Prior Phase State

Read the "After Phase 0" and "After Phase 1" sections in `docs/audit/audit-implementation-plan.md` for what changed in previous phases.

## What Needs to Happen

### 1. Map the Current Dual System
The codebase has two dispatch paths:
- **Legacy intent classifier**: Ollama → Haiku → pattern matching, dispatches to handler functions (task, reminder, compose, code, etc.)
- **Agentic loop**: Gives Claude tools and lets it decide, multi-round tool use with parallel execution

**Explore:** Trace both paths from message receipt to response. Identify every intent handler and which ones bypass the agentic pipeline. Pay special attention to `compose` — it currently bypasses the agentic pipeline entirely.

### 2. Make Agentic Loop the Single Entry Point
All messages should enter through the agentic pipeline. The intent classifier can remain as an optimization within the pipeline (e.g., to decide if tools are needed at all), but it should not be a separate dispatch mechanism.

### 3. Consolidate the `compose` Intent
Route compose/email-drafting requests through the agentic pipeline. The pipeline already has email drafting tools — the compose handler is redundant.

### 4. Preserve Fast-Path for Simple Queries
Simple queries ("What's the capital of France?") shouldn't need tool calls. The agentic pipeline should be able to classify a query as "simple" and respond directly without invoking tools. This preserves the speed of the intent classifier for simple cases.

## Files to Explore

- `api/services/chat.py` — Main chat service, likely contains both dispatch paths
- `api/services/telegram_handler.py` — Telegram message handling
- `api/services/intent_classifier.py` (or equivalent) — Legacy classification
- `api/services/agent_tools.py` — Agentic tool definitions
- `api/services/orchestrator.py` (or equivalent) — If there's a separate orchestrator
- `api/routes/chat.py` — Chat API routes

## Boundaries

- Do NOT touch `mcp_server.py` (that's Phase 2b)
- Do NOT touch memory/agent_tools beyond what's needed for pipeline unification (that's Phase 2c)
- Do NOT add new tools to the agentic pipeline (just unify the entry point)
- Do NOT change the Telegram bot's message handling interface
- Preserve all existing capabilities — nothing should stop working

## Verification

1. Simple query ("What time is it?") still gets a fast response
2. Complex query ("When's my next meeting with Sarah and what did we discuss?") still triggers multi-tool agentic loop
3. Compose/email requests go through the agentic pipeline and use email tools
4. All existing tests pass: `./scripts/test.sh`
5. Telegram bot responds correctly to a variety of message types
6. No duplicate dispatch — one path, not two
7. Server starts cleanly: `./scripts/server.sh restart`

## Rollback

Revert the commit. The dual-system is preserved in git history. Since this is a code-path change (not a data migration), rollback is clean.
