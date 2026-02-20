# Phase 4a: Full-Agentic Reminder Pipeline

## Goal

Make prompt-type reminders as powerful as a direct Telegram message. When a reminder fires, it should have access to the full agentic pipeline — multi-round tool use, web lookups, all tools, parallel execution. The gap between "what I can ask Telegram to do" and "what a reminder can do" should be zero.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "6. Full-Agentic Reminder Pipeline"
- `docs/audit/archive/audit-telegram-chat.md` — Full agentic pipeline analysis, tool use flow
- `docs/audit/archive/audit-round2-telegram.md` — Unified pipeline design
- `CLAUDE.md` — Project conventions

## Prior Phase State

Read ALL "After Phase" sections in `docs/audit/audit-implementation-plan.md`. This phase depends on:
- Phase 2a: The chat pipeline is now unified (single agentic entry point)
- Phase 3: The task queue exists for background processing

## What Needs to Happen

### 1. Audit the Current Reminder Execution Path
Prompt-type reminders call `chat_via_api()` in `reminder_store.py`. Trace this path and compare it to the direct Telegram message path:
- Does it go through the same agentic loop?
- Does it have access to the same tools?
- Does it support the same number of reasoning rounds?
- Does it support parallel tool execution?
- Does it support web lookups?

Document every gap.

### 2. Close the Gaps
Whatever differences exist between the reminder path and the direct Telegram path, eliminate them. The reminder pipeline should use the exact same agentic loop as a direct message.

### 3. Add Error Handling and Retry
If a tool call fails mid-briefing, the reminder should:
- Retry the failed tool call (up to 2 retries)
- If retry fails, send a partial result with a note about what failed
- Never silently fail — always send something to the user

### 4. Add Execution Logging
Log what each reminder execution did:
- Which tools were called
- How long each took
- Whether any failed
- Total execution time
- Token usage

This logging should be queryable (store in SQLite or write to a structured log).

### 5. Test with Complex Briefings
Create and verify a test reminder that requires multiple tool calls:
- "Check my calendar for today, check my email for anything urgent, check my overdue tasks, and give me a summary"
- This should trigger 3+ tool calls and synthesize the results

## Files to Explore

- `api/services/reminder_store.py` — ReminderScheduler, `_fire_reminder`, `_generate_message`, `chat_via_api`
- `api/services/chat.py` — The chat pipeline (now unified from Phase 2a)
- `api/services/agent_tools.py` — Tool definitions available to the agentic pipeline
- `api/services/telegram_handler.py` — How direct Telegram messages enter the pipeline (compare this path to the reminder path)

## Boundaries

- Do NOT add new intelligence modules (that's Phase 4b)
- Do NOT change the reminder scheduling system (cron expressions, timezone handling, etc.)
- Do NOT change how static or endpoint-type reminders work
- Focus exclusively on prompt-type reminder execution parity
- Do NOT add new tools to the pipeline (just ensure reminders can use all existing ones)

## Verification

1. A prompt-type reminder that says "What are my meetings today?" triggers the calendar tool and sends a real response
2. A prompt-type reminder that requires 3+ tool calls executes all of them successfully
3. A reminder with a failing tool call still sends a partial result (doesn't silently fail)
4. Execution logs show tool calls, timing, and any errors
5. The output is indistinguishable from what you'd get by typing the same prompt into Telegram
6. All existing tests pass: `./scripts/test.sh`
7. Server starts cleanly: `./scripts/server.sh restart`

## Rollback

Revert the commit. Reminder execution falls back to pre-parity behavior.
