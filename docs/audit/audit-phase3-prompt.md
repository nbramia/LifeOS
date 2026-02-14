# Phase 3: Add a SQLite-Backed Task Queue

## Goal

Implement background job processing so that sync operations, reindexing, and embedding generation no longer block the API. Jobs run in the background with status tracking, retry logic, and progress reporting.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "3. Add a Task Queue"
- `docs/audit/archive/audit-infrastructure.md` — Current sync architecture, subprocess calls, blocking operations
- `docs/audit/archive/audit-round2-infra.md` — Task queue design proposal (Dramatiq + Redis vs SQLite-backed)
- `docs/audit/archive/audit-round3-devils-advocate.md` — Complexity warnings about task queues
- `CLAUDE.md` — Project conventions

## Prior Phase State

Read the "After Phase 0" through "After Phase 2" sections in `docs/audit/audit-implementation-plan.md` for everything that changed in previous phases. Pay special attention to Phase 2a (chat pipeline) since the queue may interact with the chat service.

## What Needs to Happen

### 1. Design the Queue Schema
A `jobs` table in SQLite:
- `id` (primary key)
- `type` (e.g., "sync_gmail", "reindex_vault", "generate_embeddings")
- `status` (pending, running, completed, failed)
- `params` (JSON — job-specific parameters)
- `result` (JSON — output or error details)
- `created_at`, `started_at`, `completed_at`
- `attempts` (retry count)
- `max_attempts` (default 3)
- `priority` (integer, lower = higher priority)

### 2. Build the Worker
A background worker (asyncio task or thread) that:
- Polls the `jobs` table for pending jobs
- Executes one job at a time (single-user system, no need for concurrency)
- Updates status as it progresses
- Retries failed jobs up to `max_attempts`
- Logs execution to the standard log

**Important:** Use SQLite-backed queue, NOT Redis + Dramatiq. For a single-user system, SQLite is sufficient and doesn't add a new dependency.

### 3. Migrate Sync Operations
Move the sync operations that currently block the API into background jobs:
- Vault reindex (`POST /api/admin/reindex`)
- Calendar sync
- Any other long-running sync operations

The API endpoints should enqueue a job and return immediately with a job ID.

### 4. Add Status API
Endpoints to check job status:
- `GET /api/jobs` — List recent jobs with status
- `GET /api/jobs/{job_id}` — Get specific job status and result

## Files to Explore

- `api/routes/admin.py` — Reindex and sync trigger endpoints (these become job enqueue points)
- `api/services/sync_manager.py` (or equivalent) — Current sync execution
- `scripts/run_all_syncs.py` — Nightly sync script
- `api/main.py` — Application startup (where the worker would be initialized)
- Any existing background processing patterns in the codebase

## Boundaries

- Do NOT use Redis, Celery, Dramatiq, or any external dependency
- Do NOT build a distributed job system
- Do NOT change how the nightly cron sync works (it can continue as-is)
- Do NOT migrate ALL operations to background jobs — start with the ones that block the API (reindex, manual sync triggers)
- Keep the worker simple — single-threaded polling loop is fine

## Verification

1. `POST /api/admin/reindex` returns immediately with a job ID
2. `GET /api/jobs/{id}` shows the job progressing from pending → running → completed
3. During a reindex job, the API remains responsive to other requests
4. Failed jobs are retried up to `max_attempts`
5. Job history is queryable
6. All existing tests pass: `./scripts/test.sh`
7. Server starts cleanly with the worker: `./scripts/server.sh restart`

## Rollback

Revert the commit. The sync endpoints return to synchronous behavior. New table and worker are removed.
