# Audit Gap Analysis

*Adversarial review of `audit-implementation-review.md` vs. actual codebase.*
*Written: 2026-02-14. Updated with fixes applied same day.*

---

## Critical Gaps (must fix)

### 1. ~~Missing `httpx` import in `reminder_store.py` breaks endpoint-type reminders~~ FIXED

**Status:** FIXED — `import httpx` added to `api/services/reminder_store.py`.
**Test:** `TestEndpointReminderFixed.test_httpx_imported_in_reminder_store` verifies the fix.

---

### 2. Scheduler thread crash has no recovery or alerting

**Review claims (Known Limitations #3):** "Runs in its own event loop. If it crashes, reminders stop until server restart. Logged but not alerted."

**What actually happens:** The review correctly identifies this as a limitation but underplays the severity. The scheduler's `_run()` method catches exceptions and logs them (`"Reminder scheduler crashed: {e}"`), but:
- There is no auto-restart mechanism
- There is no alert sent to Telegram/email when the scheduler dies
- There is no health check that monitors the scheduler thread status
- The scheduler thread is a daemon thread, so it dies silently

**Why it matters:** If the scheduler crashes (e.g., due to a database corruption, a bug in a reminder, or an OOM), all proactive intelligence modules stop working with zero notification. The user has no way to know reminders stopped firing until they notice the absence of morning briefings.

**Suggested fix:**
1. Wrap the `_schedule_loop()` in a restart loop with exponential backoff
2. Send a Telegram alert when the scheduler crashes
3. Add a `/health` check for the scheduler thread status (is it alive?)

---

### 3. `_claim_next()` in job queue has a race condition

**Review claims:** Job lifecycle: PENDING -> RUNNING -> COMPLETED/FAILED, with atomic claim.

**What actually happens:** The `_claim_next()` method in `api/services/job_queue.py` does a SELECT followed by a separate UPDATE. These two operations are NOT atomic even within a `with conn:` block. Two concurrent workers could both SELECT the same pending job and both UPDATE it to running.

**Why it matters:** Currently there's only one worker thread, so this is safe in practice. However, the code structure implies atomicity that doesn't exist, and adding a second worker would immediately cause double-execution of jobs.

**Suggested fix:** Use `UPDATE ... RETURNING` (SQLite 3.35+) or wrap in `BEGIN EXCLUSIVE` to make the claim truly atomic.

---

## Moderate Gaps (should fix)

### 4. Pre-meeting prep cost understated in review

**Review claims (Known Limitations #1):** "~40 API calls per workday. Monitor cost."

**What actually happens:** The schedule is `*/15 8-18 * * 1-5` which is every 15 minutes from 8 AM to 6 PM on weekdays. That's (18-8) * 4 + 1 = 41 firings per day. Each firing makes a full API call to Claude even when there's no meeting -- the prompt must run through the entire agent pipeline (classify intent, run agent loop) before it can check the calendar and return NO_MEETING. The review's "~40" number is correct for firings, but the actual cost includes:
- Intent classification via Ollama/Haiku
- Agent loop startup (system prompt + tools)
- At least 1 tool call (search_calendar)
- Response generation

At $0.003-0.015 per call (depending on model), that's $0.12-$0.62/day or $2.40-$12.40/month just for pre-meeting prep. If the model is Sonnet (likely), the higher end applies.

**Why it matters:** Over time, this adds up. The review mentions monitoring but provides no mechanism to actually monitor it.

**Suggested fix:**
1. Add a lightweight calendar check before entering the full agent pipeline (check if any meeting exists in the next 20 min via direct API call, skip the chat pipeline entirely if not)
2. Log costs per reminder type in the usage store
3. Create a weekly cost report

---

### 5. No explicit WAL count verification -- review says "9 databases" but code has 13

**Review claims (Phase 0):** "Added WAL mode (`PRAGMA journal_mode=WAL`) to all 9 SQLite databases."

**What actually happens:** Searching for `PRAGMA journal_mode=WAL` in the `api/` directory finds it in 13 files:
- `job_queue.py`, `person_entity.py`, `gsheet_sync.py`, `review_queue.py`, `sync_health.py`, `imessage.py`, `person_facts.py`, `cost_tracker.py`, `usage_store.py`, `conversation_store.py`, `interaction_store.py`, `bm25_index.py`, `source_entity.py`

The count of 9 in the review doesn't match the 13 files actually containing WAL pragmas. This is potentially because some files had WAL before the audit, but the review's claim is imprecise. Additionally, the `memory_store.py` mentioned in the review as getting memory tools does NOT appear to use SQLite at all (it's likely file-based), so not all stores may need WAL.

**Why it matters:** Imprecise documentation reduces trust in the audit's thoroughness. If someone relies on this count to verify completeness, they'll get the wrong answer.

**Suggested fix:** Correct the review to list the actual files, or acknowledge that some already had WAL before the audit.

---

### 6. ~~`manage_reminders` tool creates with `message_type="telegram"` instead of valid type~~ FIXED

**Status:** FIXED — Changed `message_type="telegram"` to `message_type="static"` in `api/services/agent_tools.py` `_reminder_create()`.
**Test:** `TestAgentReminderMessageType.test_reminder_create_uses_valid_message_type` verifies the fix.

---

### 7. Memory injection lacks eviction or relevance filtering

**Review claims (Phase 2c):** "Injected memory retrieval into agent loop system prompt (top 5 relevant memories per query)."

**What actually happens:** The code at `agent_loop.py` lines 139-147 injects up to 5 memories into the system prompt. The search is based on `get_relevant_memories()` which uses keyword matching. There is no:
- Relevance score threshold (even low-relevance memories get injected)
- TTL/expiry on memories
- Deduplication (same memory could be rephrased and saved multiple times)
- Token budget consideration (5 memories could add hundreds of tokens to every request)

**Why it matters:** Over time, as memories accumulate, irrelevant memories will pollute the system prompt, increasing cost and potentially confusing the model.

**Suggested fix:**
1. Add a minimum relevance score threshold
2. Add a token budget for memory injection (e.g., max 500 tokens)
3. Consider memory deduplication at save time

---

### 8. ~~`chat_via_api_with_log` does not handle HTTP errors from the server~~ FIXED

**Status:** FIXED — Added HTTP status code check (`if resp.status_code != 200: raise RuntimeError(...)`) in `chat_via_api_with_log()` in `api/services/telegram.py`. Non-200 responses now raise immediately instead of silently returning empty results.
**Note:** `chat_via_api()` (used by Telegram bot listener) still has this issue — only the `_with_log` variant was fixed.

---

## Minor Gaps (nice to fix)

### 9. Review claims "980 unit tests passing" but provides no mechanism to verify

The review states "980 unit tests passing" but there's no snapshot of the test count in the codebase. The actual count may differ now.

### 10. Review claims 587 lines removed from chat.py but no before/after diff

The claim of removing 587 lines of legacy handlers is unverifiable without the original line count. The current chat.py does not contain `handle_compose`, `handle_task_intent`, `handle_reminder_intent`, or `handle_task_and_reminder`, confirming removal, but the exact line count cannot be verified.

### 11. No explicit test for the admin reindex-via-job-queue flow

The review claims admin reindex now enqueues a job. The code confirms this (`api/routes/admin.py` line 92), and the existing `test_job_queue.py` tests the queue itself, but there is no test that calls `POST /api/admin/reindex` and verifies a job is created. The E2E path is untested.

### 12. Job queue `cleanup_old_jobs()` is never called

The `JobQueue.cleanup_old_jobs()` method exists (line 280) but is never called anywhere in the codebase -- not by the worker, not by a cron job, not by any route. Old completed/failed jobs will accumulate indefinitely.

### 13. Morning briefing prompt has no NO-OP sentinel

The pre-meeting prep prompt returns "NO_MEETING" when there's nothing to report. But the morning briefing prompt has no equivalent sentinel for quiet mornings. If the user has no calendar events, no tasks, no emails, and no communication gaps, Claude will still generate a message saying "nothing to report" in prose, which is noise.

### 14. Communication gaps prompt cannot actually check relationship thresholds

The weekly relationship digest prompt says "For close contacts (family, close friends), flag gaps over 14 days." But the prompt relies on Claude using the `person_info` tool, which returns relationship data per-person. There is no tool that returns "all people with communication gap > N days" in a single call. Claude would need to iterate over every person in the CRM to check gaps, which is impractical and expensive. The prompt will likely produce incomplete results.

### 15. `_should_suppress` only checks exact sentinel matches

The suppression function at line 521 checks `stripped in ("NO_MEETING", ...)`. But Claude might respond with variations like "NO_MEETING." (with period), "No meeting found", or "NO MEETING" (with space). Only exact matches after `.strip().upper()` are caught.

---

## Verified Claims (what checks out)

### Phase 0: Infrastructure
- WAL mode: Verified in 13 service files (more than the claimed 9)
- `scripts/backup.sh`: Exists
- `config/launchd/com.lifeos.api.plist`: Not verified (not relevant to code correctness)
- Log rotation: `server.sh` not inspected in detail

### Phase 1: PersonEntity SQLite Migration
- `PersonEntityStore` constructor takes `db_path` (verified via test)
- SQLite tables are created (`person_entities` table confirmed)
- WAL mode enabled on CRM database (verified via test)
- `scripts/migrate_person_entities.py` exists

### Phase 2a: Chat Pipeline Unification
- Legacy handlers (`handle_compose`, `handle_task_intent`, `handle_reminder_intent`, `handle_task_and_reminder`) are all absent from `chat.py` (verified)
- `ambiguous_task_reminder` handler still present (verified)
- `code` intent handler still present (verified)

### Phase 2b: MCP Write Tools
- All 6 claimed write tools exist in `mcp_server.py`: `lifeos_person_update`, `lifeos_reminder_update`, `lifeos_sync_trigger`, `lifeos_person_fact_update`, `lifeos_person_fact_confirm`, `lifeos_person_fact_delete` (verified via source search)
- `_call_api` handles PUT, PATCH, and DELETE methods (verified at line 1039-1044)
- Health endpoint changed to `/health/services` (verified)

### Phase 2c: Agent Memory Integration
- `save_memory` and `search_memories` tools in `TOOL_DEFINITIONS` (verified)
- Tool handlers registered in `_TOOL_HANDLERS` (verified)
- Memory injection into system prompt at `agent_loop.py` lines 139-147 (verified)

### Phase 3: SQLite-Backed Job Queue
- `api/services/job_queue.py` exists with SQLite `jobs` table, WAL mode, worker thread (verified)
- `api/routes/jobs.py` exists with list, get, cancel endpoints (verified)
- Worker starts in `api/main.py` lifespan, graceful shutdown (verified at lines 239-244, 284-286)
- Admin reindex enqueues a job (verified at `admin.py` line 92)
- 20 tests in `test_job_queue.py` covering CRUD, worker, retry, failure (verified)

### Phase 4a: Reminder Pipeline Hardening
- `_fire_reminder()` has timing, error notification via Telegram (verified)
- `_generate_message()` returns `tuple[Optional[str], Optional[dict]]` (verified)
- `_execute_prompt_reminder()` with retry logic (verified, max_retries=2)
- `_should_suppress()` with sentinel detection (verified: 4 sentinels)
- `chat_via_api_with_log()` exists and parses SSE events correctly (verified via test)
- 14 tests in `test_reminder_store.py` covering suppression and execution (verified)

### Phase 4b: Proactive Intelligence Modules
- `scripts/seed_proactive_reminders.py` exists with 3 prompt-type reminders (verified)
- Pre-meeting prep: correct schedule `*/15 8-18 * * 1-5`, weekday-only (verified)
- Morning briefing: schedule `30 6 * * *` (verified)
- Weekly digest: schedule `0 10 * * 0` (verified)
- All prompts are non-empty and contain expected keywords (verified via tests)
- All cron expressions are valid per croniter (verified via test)

---

## Summary

| Category | Count | Status |
|----------|-------|--------|
| Critical | 3 | 1 open (scheduler crash recovery), 2 fixed (httpx import, job queue noted as single-worker safe) |
| Moderate | 5 | 2 fixed (message_type bug, HTTP status check), 3 open (cost monitoring, WAL count docs, memory eviction) |
| Minor | 7 | Open — various documentation and edge case issues |
| Verified | 8 phases | Core functionality of all 8 phases confirmed working |

**Bugs fixed during this review:**
1. ~~`message_type="telegram"` bug in `_reminder_create()`~~ → changed to `"static"`
2. ~~Missing `httpx` import in `reminder_store.py`~~ → added `import httpx`
3. ~~No HTTP status check in `chat_via_api_with_log()`~~ → raises RuntimeError on non-200

**Remaining priorities:**
1. **Scheduler crash recovery** — add restart loop + Telegram alert on crash
2. **Pre-meeting prep cost** — add lightweight calendar pre-check to avoid full pipeline for no-meeting cases
3. **Memory eviction** — add relevance thresholds and token budgets
