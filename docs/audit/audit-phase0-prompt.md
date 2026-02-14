# Phase 0: Fix Infrastructure Basics

## Goal

Make LifeOS safe to develop on. Enable SQLite WAL mode everywhere, create automated backups with rotation, fix the launchd plist, and add log rotation. After this phase, a power failure or bad write won't lose data, and the server will survive reboots.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "1. Fix Infrastructure Basics" and "Where LifeOS Stands Today"
- `docs/audit/archive/audit-infrastructure.md` — Full infrastructure audit (launchd, backup, log details, service map)
- `docs/audit/archive/audit-round2-backend.md` — WAL mode analysis and concurrency concerns
- `CLAUDE.md` — Project conventions and dev workflow

## What Needs to Happen

### 1. SQLite WAL Mode
Enable WAL mode on ALL SQLite databases at connection time. The codebase has 5+ SQLite databases that should all use WAL for safe concurrent reads.

**Explore:** Find every SQLite database connection in the codebase. Check if WAL mode is already enabled anywhere. Add `PRAGMA journal_mode=WAL` at connection initialization for each.

### 2. Automated Backups
The `data/backups/` directory doesn't exist. There are zero backups of `crm.db` (556 MB), ChromaDB (1.1 GB), config files, or reminders.

**Implement:** A backup script that:
- Creates `data/backups/` if missing
- Backs up all SQLite databases (using `.backup` command for consistency)
- Backs up `config/people_dictionary.json`, `~/.lifeos/reminders.json`, and other critical config
- Rotates: keep 7 daily backups
- Add to cron (run nightly, after the 3 AM sync completes — suggest 4:00 AM)

### 3. Fix launchd Plist
The launchd plist references `LifeOS.app` which doesn't exist. The API server has no working auto-start mechanism.

**Explore:** Find the launchd plist file(s). Fix the path to point at the actual server start script. Test `launchctl load/unload`. Verify the server starts on boot.

### 4. Log Rotation
`server.log` is 20 MB+ and growing unbounded. Sync logs also accumulate.

**Implement:** Configure log rotation using `newsyslog` or a logrotate config. Cap server.log at 10 MB with 5 rotations. Apply to error logs and sync logs too.

## Files to Explore

- `scripts/server.sh` — How the server starts
- `scripts/service.sh` — launchd management
- `api/main.py` — App initialization, database setup
- `config/` — Configuration files
- `data/` — Data directory structure
- `logs/` — Log files
- Any `.plist` files in `~/Library/LaunchAgents/` related to LifeOS
- Any existing backup scripts

## Boundaries

- Do NOT change any business logic, API endpoints, or service behavior
- Do NOT add monitoring dashboards or health check improvements (that's later)
- Do NOT restructure the data directory
- Keep changes minimal and infrastructure-focused

## Verification

1. `PRAGMA journal_mode` returns `wal` for every database connection
2. Backup script runs successfully and creates restorable copies
3. `launchctl load` starts the server; `launchctl unload` stops it
4. After killing the server process, launchd restarts it automatically
5. Log files rotate and don't grow unbounded
6. `./scripts/test.sh` passes
7. Server starts cleanly: `./scripts/server.sh restart`

## Rollback

All changes are additive (new scripts, config changes). Rollback by reverting the commit. WAL mode can be reverted with `PRAGMA journal_mode=DELETE`.
