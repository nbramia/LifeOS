# LifeOS Implementation Plan

This document is the source of truth for orchestrating implementation of the LifeOS improvements identified in the audit. It is maintained by the orchestrator agent and should be read at the start of any session that continues this work.

## Status

| Phase | Status | Agent | Items | Notes |
|-------|--------|-------|-------|-------|
| Phase 0 | **completed** | — | #1 | Infrastructure basics |
| Phase 1 | **completed** | — | #2 | PersonEntity JSON→SQLite migration |
| Phase 2a | **completed** | — | #4 | Chat pipeline unification |
| Phase 2b | **completed** | mcp-agent | #5 (all sub-items) | MCP write tools |
| Phase 2c | **completed** | memory-agent | #7 | Agent memory integration |
| Phase 3 | **completed** | — | #3 | SQLite-backed job queue |
| Phase 4a | **completed** | — | #6 | Reminder pipeline hardening |
| Phase 4b | **completed** | — | #8 | Proactive intelligence modules |

## Approach

### Orchestration Model

One orchestrator (this session or a resumed session) spawns specialist agents for each phase. The orchestrator:

1. **Pre-digests context** for each agent — instead of making every agent read 14 audit docs, the orchestrator writes a focused brief per phase that includes only what that agent needs.
2. **Verifies between phases** — runs tests, checks server starts, catches regressions before the next phase begins.
3. **Writes state summaries** — after each phase lands, writes a brief "what changed" entry in this document so subsequent agents don't need to re-explore.
4. **Manages commits** — each phase gets a clean commit (or set of commits) for rollback.

### Phase Execution Order

```
Phase 0  →  Phase 1  →  Phase 2 (a, b, c in parallel)  →  Phase 3  →  Phase 4a  →  Phase 4b
  #1          #2         #4 | #5 | #7                       #3          #6            #8
```

### Why This Order

- **Phase 0 (#1) first**: Infrastructure basics (WAL, backups, launchd, logs) must be in place before any other code changes. Safety net.
- **Phase 1 (#2) second**: PersonEntity migration is a data integrity risk. Do it while the codebase is stable, before adding new features.
- **Phase 2 parallel**: Three independent workstreams that touch different files:
  - **2a (#4)**: Chat pipeline — `api/services/chat.py`, `api/services/telegram_handler.py`, Ollama routing
  - **2b (#5)**: MCP tools — `mcp_server.py`
  - **2c (#7)**: Agent memory — `api/services/agent_tools.py`, new memory service/table
  - These don't overlap. If minor conflicts arise, the orchestrator resolves them.
- **Phase 3 (#3) after Phase 2**: Task queue benefits from the unified chat pipeline (#4) being in place.
- **Phase 4a (#6) after Phase 3**: Reminder pipeline parity depends on having the unified chat pipeline (#4) and ideally the task queue (#3) for background execution.
- **Phase 4b (#8) after Phase 4a**: Proactive intelligence modules are built on top of the hardened reminder pipeline (#6).

### File Ownership per Phase (Conflict Prevention)

| Phase | Primary Files | Shared Files (Orchestrator Resolves) |
|-------|--------------|--------------------------------------|
| 0 (#1) | `scripts/`, launchd plists, cron configs, SQLite init code | `api/main.py` (WAL mode) |
| 1 (#2) | `api/services/person_entity_manager.py`, migration script, `person_entities.json` | Tests |
| 2a (#4) | `api/services/chat.py`, `api/services/telegram_handler.py`, `api/services/intent_classifier.py` | — |
| 2b (#5) | `mcp_server.py`, `api/routes/admin.py` | — |
| 2c (#7) | `api/services/agent_tools.py`, `api/services/memory_service.py` (new) | — |
| 3 (#3) | `api/services/task_queue.py` (new), `api/services/sync_manager.py`, `scripts/` | `api/main.py` (queue startup) |
| 4a (#6) | `api/services/reminder_store.py`, `api/services/chat.py` | — |
| 4b (#8) | `api/services/proactive_intelligence.py` (new), reminder configs | — |

### Agent Context Strategy

Each agent gets a focused brief instead of all 14 audit docs. The brief contains:

1. The relevant section from `audit-vision-v2.md`
2. The specific PRD (if applicable)
3. The "What Changed" summaries from prior phases
4. A list of files to explore in the codebase
5. Explicit boundaries (what NOT to touch)

This keeps each agent's context lean and focused.

## What Changed (Updated After Each Phase)

*This section is updated by the orchestrator after each phase completes. Subsequent agents read this instead of re-exploring the entire codebase.*

### After Phase 0

**WAL Mode (all 9 databases):**
- Added `PRAGMA journal_mode=WAL` to `_init_db()` in: `source_entity.py`, `bm25_index.py`, `interaction_store.py`, `conversation_store.py`, `usage_store.py`, `cost_tracker.py`, `person_facts.py`, `gsheet_sync.py`
- Added WAL to `get_sync_health_db()` in `sync_health.py` and `_get_conn()` in `review_queue.py`
- Added WAL to `_ensure_schema()` in `imessage.py`
- All 9 databases verified: crm.db, interactions.db, imessage.db, conversations.db, bm25_index.db, sync_health.db, usage.db, gsheet_sync.db, cost_tracking.db

**Automated Backups:**
- New: `scripts/backup.sh` — backs up all 9 SQLite databases (using `sqlite3 .backup`) + config files (people_dictionary.json, reminders.json, memories.json)
- 7-day rotation of daily backups, old timestamped sync logs cleaned (>30 days)
- Cron entry added: `0 4 * * *` via `LifeOS exec` (runs after 3 AM sync)
- Tested: 12 files, 1.2 GB total, 0 failures

**launchd Plist Fixed:**
- Updated `config/launchd/com.lifeos.api.plist`: points to `~/.venvs/lifeos/bin/uvicorn` (not non-existent LifeOS.app)
- Added: HOME, OBJC_DISABLE_INITIALIZE_FORK_SAFETY, /opt/homebrew/bin in PATH, LimitLoadToSessionType: Aqua
- Verified: `launchctl load` succeeds (was exit code 78, now PID assigned with exit 0)
- Note: `service.sh install` deploys this plist. Day-to-day use remains `server.sh`

**Log Rotation:**
- `server.sh`: rotates server.log on restart when >10 MB (5 rotations)
- `backup.sh`: cleans old timestamped sync/fda logs >30 days
- `config/newsyslog-lifeos.conf`: ready for manual install (`sudo cp ... /etc/newsyslog.d/lifeos.conf`) for system-level rotation

### After Phase 1

**PersonEntity JSON→SQLite Migration:**
- Rewrote `api/services/person_entity.py`: `PersonEntityStore` now backed by SQLite (`data/crm.db` table `person_entities`) with lookup tables (`person_emails`, `person_phones`, `person_names`) and indices
- Constructor changed from `storage_path: str` to `db_path: str`
- `save()` is now a no-op (writes are immediate via SQL); added `export_json()` for backup
- Migration script: `scripts/migrate_person_entities.py` — reads JSON, inserts into SQLite, verifies integrity
- All 24 person_entity tests + 56 entity_resolver/briefings tests pass
- Data integrity verified: 14,121 entities match between JSON and SQLite
- Commit: `a7fbf99`

### After Phase 2

**Phase 2a — Chat Pipeline Unification:**
- Removed 587 lines of legacy intent handlers from `api/routes/chat.py` (compose, task, reminder, task_and_reminder)
- All intents now flow through the agentic loop which has `create_email_draft`, `manage_tasks`, `manage_reminders` tools
- Only two special-case handlers remain: `ambiguous_task_reminder` (user clarification) and `code` (Claude Code delegation)
- Removed unused `TaskIntentType` import
- Commit: `65b388d`

**Phase 2b — MCP Write Tools:**
- Added 6 new tools to `mcp_server.py` CURATED_ENDPOINTS: `lifeos_person_update` (PATCH), `lifeos_reminder_update` (PUT), `lifeos_sync_trigger` (POST, custom handler), `lifeos_person_fact_update` (PUT), `lifeos_person_fact_confirm` (POST), `lifeos_person_fact_delete` (DELETE)
- Fixed `_call_api`: Added explicit PUT/PATCH/DELETE handling
- Changed health endpoint from `/health/full` to `/health/services` for richer per-service data
- Added `_get_fallback_schema()` and `_fallback_schemas()` methods
- Commit: `c485d27`

**Phase 2c — Agent Memory Integration:**
- Added `save_memory` and `search_memories` tools to `api/services/agent_tools.py`
- Injected relevant memory retrieval into `api/services/agent_loop.py` system prompt (top 5 memories per query)
- Leverages existing `MemoryStore` service and API routes
- Commit: `708797c`

### After Phase 3

**SQLite-Backed Job Queue:**
- New: `api/services/job_queue.py` — SQLite `jobs` table (WAL mode) with fields: id, type, status, params, result, timestamps, attempts, max_attempts, priority, error
- `JobQueue` class: enqueue, get, list, cancel, cleanup operations; pluggable handler registry via `register_job_handler()`
- Background worker thread: polls every 2s, claims one job at a time, retry logic (failed→pending up to max_attempts, then FAILED)
- Built-in handlers: `reindex_vault` (calls IndexerService), `sync_source` (subprocess to sync scripts)
- New: `api/routes/jobs.py` — `GET /api/jobs` (list/filter), `GET /api/jobs/{job_id}` (status), `POST /api/jobs/{job_id}/cancel`
- Updated `api/routes/admin.py`: `POST /api/admin/reindex` now enqueues a job and returns immediately with `job_id`; duplicate prevention (won't enqueue if already pending/running); removed `BackgroundTasks` import and global `_reindex_in_progress` flag
- `POST /api/admin/reindex/sync` kept for initial setup (blocking mode)
- Worker starts in `api/main.py` lifespan, shuts down gracefully on exit
- 20 new tests in `tests/test_job_queue.py` (queue CRUD, worker execution, retry, failure exhaustion, sequential processing)
- Database: `data/jobs.db`

### After Phase 4a

**Reminder Pipeline Hardening:**
- Updated `api/services/reminder_store.py`:
  - `_fire_reminder()`: Added timing, execution logging (tool calls, cost, elapsed time), error notification via Telegram on failure, `mark_triggered` on both success and failure
  - `_generate_message()`: Changed return type to `tuple[Optional[str], Optional[dict]]` to return execution metadata alongside the message
  - New `_execute_prompt_reminder()`: Retry logic (up to 2 attempts) for prompt-type reminders, captures tool statuses/usage/cost
  - New `_should_suppress()`: Sentinel detection for high-frequency reminders (e.g. "NO_MEETING" from pre-meeting prep)
- Updated `api/services/telegram.py`:
  - New `chat_via_api_with_log()`: Like `chat_via_api()` but captures tool_statuses, cost_usd, model, input/output tokens from SSE events
- Updated `tests/test_reminder_store.py`: Added 14 new tests (TestSuppression: 6 tests, TestPromptExecution: 8 tests covering retry, error notification, suppression, and execution logging)
- Updated `tests/test_admin.py`: Fixed reindex tests to mock job queue instead of removed `_do_reindex`

### After Phase 4b

**Proactive Intelligence Modules:**
- New `scripts/seed_proactive_reminders.py`: Seeds three prompt-type cron reminders with carefully crafted prompts. Supports `--dry-run` and `--force` flags, skips duplicates by default.
- Three modules, each a standard prompt-type reminder (no new infrastructure):
  1. **Pre-Meeting Prep** (`*/15 8-18 * * 1-5`): Checks calendar every 15 min during work hours; looks up attendees, recent interactions, pending items. Returns "NO_MEETING" sentinel (suppressed) when no meeting is upcoming.
  2. **Morning Briefing** (`30 6 * * *`): Daily 6:30 AM digest with today's schedule + context, overdue/due tasks, overnight emails, and top relationship gaps.
  3. **Weekly Relationship Digest** (`0 10 * * 0`): Sunday 10 AM check-in surfacing communication gaps — 14-day threshold for close contacts, 30-day for professional.
- All modules use the full agentic pipeline (tool access to calendar, CRM, email, tasks) via the Phase 4a hardened execution path.
- Each module is deletable via standard reminder CRUD — no code changes needed to disable.

## Agent Prompts

Each phase has a pre-written prompt stored in its own file. These are ready to paste into a new Claude Code session or to use with the Task tool for spawning agents.

| Phase | Prompt File |
|-------|-------------|
| Phase 0 (#1) | `audit-phase0-prompt.md` |
| Phase 1 (#2) | `audit-phase1-prompt.md` |
| Phase 2a (#4) | `audit-phase2a-prompt.md` |
| Phase 2b (#5) | `audit-phase2b-prompt.md` |
| Phase 2c (#7) | `audit-phase2c-prompt.md` |
| Phase 3 (#3) | `audit-phase3-prompt.md` |
| Phase 4a (#6) | `audit-phase4a-prompt.md` |
| Phase 4b (#8) | `audit-phase4b-prompt.md` |

## Verification Checklist (Run Between Phases)

```bash
# 1. Tests pass
./scripts/test.sh

# 2. Server starts
./scripts/server.sh restart
sleep 5
curl -s http://localhost:8000/health | jq .status

# 3. MCP server starts (if Phase 2b touched it)
# Check via Claude Desktop connection

# 4. Telegram bot responds (if Phase 2a or 4a touched chat pipeline)
# Send a test message via Telegram

# 5. Commit
git add -A && git status  # Review before committing
```

## Recovery

If a phase fails or introduces regressions:

1. `git stash` or `git checkout .` to undo uncommitted changes
2. If committed, `git revert <commit>` to reverse the phase
3. Fix the issue in the phase's agent prompt and re-run
4. Do NOT proceed to the next phase until the current phase is clean
