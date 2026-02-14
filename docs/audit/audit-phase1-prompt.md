# Phase 1: Migrate PersonEntity from JSON to SQLite

## Goal

Move `person_entities.json` to a SQLite table with proper transactions, concurrent access safety, and partial updates, eliminating the single biggest data integrity risk in the system.

## Context

All audit docs are in `docs/audit/`. Archive docs are in `docs/audit/archive/`. Read these files before planning:
- `docs/audit/audit-vision-v2.md` — Section "2. Migrate PersonEntity from JSON to SQLite"
- `docs/audit/archive/audit-backend.md` — PersonEntity architecture, data model details
- `docs/audit/archive/audit-round2-backend.md` — Migration risk analysis, concurrency concerns
- `docs/audit/archive/audit-round3-devils-advocate.md` — Skim for warnings about this migration
- `CLAUDE.md` — Project conventions

## Prior Phase State

Read the "After Phase 0" section in `docs/audit/audit-implementation-plan.md` for what changed in the previous phase. WAL mode is now enabled on all SQLite databases.

## What Needs to Happen

### 1. Design the SQLite Schema
PersonEntity currently lives in `person_entities.json`. Design a SQLite table (or tables) that captures the same data with proper typing, indexing, and relational integrity.

**Explore:** Read the current `PersonEntity` model/class to understand all fields. Look at how `person_entities.json` is structured. Understand which fields are queried vs. stored.

### 2. Build Migration Script
A standalone script that:
- Reads the current JSON file
- Creates the SQLite table(s)
- Migrates all records
- Verifies record-by-record that no data was lost
- Keeps the JSON file as a backup (does not delete it)

### 3. Update PersonEntityManager
The service that reads/writes PersonEntity data needs to use SQLite instead of JSON. This is the core change and the scariest part — many services depend on this.

**Explore:** Find every file that imports or uses PersonEntityManager (or directly reads/writes `person_entities.json`). Map all consumers before changing anything.

### 4. Keep JSON Export
Maintain a `to_json()` or `export_json()` method for backup purposes. The JSON format should remain available as an export, just not as the primary store.

## Files to Explore

- `person_entities.json` (or wherever it lives in `data/`)
- `api/services/person_entity_manager.py` (or equivalent)
- Every file that imports PersonEntityManager — use grep for this
- `api/routes/crm.py` — Major consumer of PersonEntity
- `api/services/entity_resolution.py` (or equivalent)
- The data model/class definition for PersonEntity

## Boundaries

- Do NOT change the PersonEntity data model (fields, types, relationships)
- Do NOT change how other services consume PersonEntity data (keep the same interface)
- Do NOT delete the JSON file after migration (keep it as backup)
- Do NOT refactor CRM routes or other consumers beyond what's needed for the SQLite switch
- Match existing patterns for SQLite usage in the codebase

## Verification

1. Migration script runs without errors on the current JSON file
2. Record count matches: JSON records == SQLite rows
3. Spot-check 10+ records for field-level data integrity
4. All existing tests pass: `./scripts/test.sh`
5. CRM page loads and displays people correctly
6. Person search works via API and Telegram
7. Entity resolution still links records correctly
8. Server starts cleanly: `./scripts/server.sh restart`

## Rollback

The JSON file is preserved. If the migration fails:
1. Revert the code changes (git checkout)
2. The JSON file is still there and untouched
3. System returns to pre-migration state immediately
