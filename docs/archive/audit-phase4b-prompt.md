# Phase 4b: Proactive Intelligence Service

## Goal

Build specific, high-value proactive intelligence modules that use the hardened reminder pipeline to surface actionable insights. Every notification must pass the bar: "Would I be annoyed if this interrupted me?" If yes, don't send it.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "8. Proactive Intelligence Service"
- `docs/audit/archive/audit-round3-blue-sky.md` — Predictive intelligence vision (for inspiration, not for scope)
- `docs/audit/archive/audit-round3-devils-advocate.md` — Over-engineering warnings
- `CLAUDE.md` — Project conventions

## Prior Phase State

Read ALL "After Phase" sections in `docs/audit/audit-implementation-plan.md`. This phase depends on:
- Phase 4a: The reminder pipeline now supports full agentic execution with multi-round tool use

## Design Principles (Non-Negotiable)

1. **High signal, low noise.** Every notification must be clearly valuable and actionable.
2. **Batched, not spammed.** Group into daily digests (morning/evening), not individual alerts. Exception: truly time-sensitive items (meeting in 15 minutes).
3. **User-configurable.** Easy to turn off categories, adjust thresholds, change delivery times.
4. **Built on reminders.** Each module IS a prompt-type reminder with a specific prompt. No new execution infrastructure — use what Phase 4a built.

## What Needs to Happen

### 1. Pre-Meeting Prep Module
A cron reminder that runs 15 minutes before each meeting:
- Check calendar for the next meeting
- Look up the attendees in the CRM
- Surface relevant past interactions, last discussion topics, pending items
- Send as a Telegram message

**Trigger:** Cron that checks for meetings in the next 15-20 minutes. If found, generates prep.

### 2. Morning Briefing Template
A well-crafted prompt-type reminder for the morning briefing. Not just "summarize my day" — a specific prompt that produces consistently useful output:
- Today's calendar with context for each meeting
- Overdue and due-today tasks
- Emails that arrived overnight that seem important
- Any communication gaps flagged

**Implementation:** A cron reminder with a carefully engineered prompt. The prompt should be specific enough that Claude produces a consistent, scannable format every time.

### 3. Communication Gap Nudges
A weekly batch that surfaces people you haven't contacted in a while:
- Configurable threshold (default: 14 days for close contacts, 30 days for others)
- Grouped by relationship tier/category
- Delivered as a weekly digest, not individual alerts

**Explore:** The API already has a communication gaps endpoint. Build on it.

### 4. Task Deadline Warnings
Part of the morning briefing — surface tasks due today or overdue. Not a separate notification.

## Files to Explore

- `api/services/reminder_store.py` — Reminder creation and scheduling
- `api/routes/reminders.py` — Reminder API
- `api/routes/calendar.py` — Calendar and meeting prep endpoints
- `api/routes/crm.py` — Communication gaps endpoint
- `api/routes/tasks.py` — Task listing with filters

## Boundaries

- Do NOT build an "insight engine" or ML prediction system
- Do NOT build a notification framework — use prompt-type reminders
- Do NOT send more than 3 proactive messages per day
- Do NOT build a UI for configuring intelligence modules (use reminder CRUD)
- Each module is a prompt-type reminder — nothing more complex
- Start with 3 modules maximum. Quality over quantity.

## Verification

1. Pre-meeting prep arrives 15 minutes before a real meeting with useful context
2. Morning briefing is scannable, accurate, and consistently formatted
3. Weekly communication gap digest arrives on schedule
4. All notifications are clearly valuable (subjective but important — review output critically)
5. Reminders can be disabled by deleting them (standard reminder CRUD)
6. All existing tests pass: `./scripts/test.sh`
7. Server starts cleanly: `./scripts/server.sh restart`

## Rollback

Each module is a reminder. Delete the reminders to disable. No code rollback needed for the modules themselves. Revert any supporting code changes via git.
