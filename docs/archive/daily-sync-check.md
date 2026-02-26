# Daily Sync Status Check Instructions

Run these checks each morning to report on the nightly sync status.

## Step 1: Check if sync ran and completed

```bash
tail -50 logs/crm-sync.log | grep -E "SYNC RUN COMPLETE|Succeeded:|Failed:|Starting sync run"
```

Look for:
- "Starting sync run" with today's date (or last night around 3am)
- "SYNC RUN COMPLETE"
- "Succeeded: X" and "Failed: Y"

## Step 2: Get per-source results

```bash
grep "$(date -v-1d '+%Y-%m-%d')\|$(date '+%Y-%m-%d')" logs/crm-sync.log | grep "Sync completed for"
```

This shows each source's results: `{'processed': X, 'created': Y, 'updated': Z, 'errors': N}`

## Step 3: Check for errors

```bash
tail -20 logs/crm-sync-error.log
tail -20 logs/lifeos-api-error.log
```

If empty or only old entries, no errors.

## Step 4: Check sync health warnings

```bash
tail -5 logs/crm-sync.log | grep "health:"
```

Reports stale or never-run sources.

## Step 5: Get interaction totals

```bash
sqlite3 data/interactions.db "SELECT source_type, COUNT(*) FROM interactions GROUP BY source_type ORDER BY COUNT(*) DESC;"
```

---

## Output Format

Provide a summary like this:

```
## Nightly Sync Status: [DATE]

**Overall: ✅ Success** (or ❌ Failed)

| Metric | Value |
|--------|-------|
| Started | [TIME] |
| Completed | [TIME] |
| Duration | [X hours] |
| Sources | [X/Y] succeeded |
| Failures | [N] |

**Sources with new data:**
- [source]: [created] created, [updated] updated

**Errors:** None (or list any)

**Health warnings:** [list any stale sources]
```

If the sync failed or didn't run:
1. Report what's missing
2. Check `logs/crm-sync-error.log` for details
3. Suggest running manually: `./scripts/run_all_syncs.py --execute`
