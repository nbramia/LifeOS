---
name: sync-health
description: Comprehensive sync + vault + LLM + service health checkup. Surfaces what's stale, what's broken, what's about to break.
argument-hint: [--brief|--verbose]
---

# Sync Health Checkup

Arguments: `$ARGUMENTS`

Parameters (all optional):
- **--brief** — one-line per section, skip the per-source detail tables
- **--verbose** — include the long-tail listings (per-source last-run dump, full error log tail)
- *(default)* — sectioned report with WARN/FAIL flags only on actual problems

## Why this exists

Built after the 2026-05-27 incident (issue #199) and the 2026-05-28 VRAM-exhaustion freeze. Both could have been caught hours earlier with a structured checkup that reads the right tables and sysfs paths instead of eyeballing logs. The data blocks below collect every signal those two debugs taught us to look at; the analysis section weights them.

## Data Collection

All paths assume the checkup is invoked from the repo root. Paths use `data/` not absolute paths so the checkup also works in worktrees.

### sync_runs — last run per source

!`if [ -f data/sync_health.db ]; then sqlite3 data/sync_health.db "SELECT source, status, started_at, completed_at, COALESCE(records_processed,0) as processed, COALESCE(records_created,0) as created, COALESCE(interactions_created,0) as ic, COALESCE(source_entities_created,0) as sec, COALESCE(error_message,'') as err FROM sync_runs WHERE id IN (SELECT MAX(id) FROM sync_runs GROUP BY source) ORDER BY source;" -separator $'\t' 2>/dev/null || echo "DB_ERROR"; else echo "NO_SYNC_HEALTH_DB"; fi`

### sync_runs — orphan running rows (process died without completing)

!`if [ -f data/sync_health.db ]; then sqlite3 data/sync_health.db "SELECT id, source, started_at, CAST((julianday('now') - julianday(started_at)) * 24 AS INTEGER) AS age_hours FROM sync_runs WHERE status='running' ORDER BY started_at;" -separator $'\t' 2>/dev/null; fi`

### sync_errors — top 20 most recent

!`if [ -f data/sync_health.db ]; then sqlite3 data/sync_health.db "SELECT source, timestamp, COALESCE(error_type,''), substr(COALESCE(error_message,''),1,200) FROM sync_errors ORDER BY timestamp DESC LIMIT 20;" -separator $'\t' 2>/dev/null; fi`

### Source-entity drift (interactions flowing but source_entities stale)

!`~/.venvs/lifeos/bin/python -c "
import sys; sys.path.insert(0, '.')
try:
    from api.services.sync_health import detect_silent_source_entity_drift
    warnings = detect_silent_source_entity_drift()
    if not warnings:
        print('NO_DRIFT')
    else:
        for w in warnings:
            print(f\"{w['source']}\t{w.get('last_interaction','?')}\t{w.get('last_source_entity','none')}\t{w.get('gap_days','?')}\")
except Exception as e:
    print(f'DETECT_ERROR: {e}')
" 2>/dev/null`

### Vault index — file count vs BM25 chunks vs ChromaDB vectors

!`~/.venvs/lifeos/bin/python -c "
import sys, os; sys.path.insert(0, '.')
from config.settings import settings
from pathlib import Path
try:
    vault = Path(os.path.expanduser(str(settings.vault_path)))
    md_count = len(list(vault.rglob('*.md'))) if vault.exists() else 0
except Exception as e:
    md_count = f'ERROR: {e}'
try:
    from api.services.bm25_index import BM25Index, get_bm25_db_path
    bm25_count = BM25Index().count()
    bm25_path = get_bm25_db_path()
except Exception as e:
    bm25_count = f'ERROR: {e}'
    bm25_path = '?'
try:
    from api.services.vectorstore import VectorStore
    chroma_count = VectorStore()._collection.count()
except Exception as e:
    chroma_count = f'ERROR: {e}'
print(f'md_files={md_count}')
print(f'bm25_chunks={bm25_count}')
print(f'bm25_db={bm25_path}')
print(f'chroma_vectors={chroma_count}')
" 2>/dev/null`

### Vault index — sample of doc_ids (check for stale macOS paths or vault-migration leftovers)

!`if [ -f data/bm25_index.db ]; then sqlite3 data/bm25_index.db "SELECT doc_id FROM chunks_fts ORDER BY RANDOM() LIMIT 5;" 2>/dev/null; fi`

### LLM runtimes — what's running, what's holding VRAM

!`echo "lifeos-llm.service: $(systemctl is-active lifeos-llm.service 2>/dev/null || echo 'absent')"; echo "ollama.service:     $(systemctl is-active ollama.service 2>/dev/null || echo 'absent')"; echo "lifeos-llm enabled: $(systemctl is-enabled lifeos-llm.service 2>/dev/null || echo 'absent')"; echo "ollama enabled:     $(systemctl is-enabled ollama.service 2>/dev/null || echo 'absent')"`

### VRAM usage (AMDGPU sysfs — primary source) + rocm-smi attribution

!`~/.venvs/lifeos/bin/python -c "
import subprocess
from pathlib import Path
GB = 1024**3
for d in sorted(Path('/sys/class/drm').glob('card*/device')):
    u_p = d / 'mem_info_vram_used'
    t_p = d / 'mem_info_vram_total'
    if u_p.exists() and t_p.exists():
        try:
            u = int(u_p.read_text().strip())
            t = int(t_p.read_text().strip())
            if t > 0:
                print(f'VRAM: {u/GB:.1f} / {t/GB:.1f} GB ({u*100//t}%)')
        except Exception:
            pass
        break
print('---')
try:
    out = subprocess.run(['rocm-smi', '--showpids'], capture_output=True, text=True, timeout=5).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit() and len(parts) >= 4:
            try:
                vram = int(parts[3]) / GB
                rows.append((vram, parts[0], parts[1]))
            except (ValueError, IndexError):
                pass
    for vram, pid, name in sorted(rows, reverse=True)[:8]:
        print(f'  PID {pid} ({name}): {vram:.1f} GB VRAM')
except Exception as e:
    print(f'(rocm-smi unavailable: {e})')
" 2>/dev/null`

### LifeOS services — uptime + status

!`for svc in lifeos-api lifeos-chromadb lifeos-llm lifeos-mcp-http lifeos-agent-worker; do active=$(systemctl is-active $svc.service 2>/dev/null); since=$(systemctl show $svc.service --property=ActiveEnterTimestamp 2>/dev/null | cut -d= -f2-); echo "$svc: $active (since $since)"; done`

### Server stacking — multiple LISTENERS on :8000

!`pids=$(ss -lntpH 2>/dev/null | grep ':8000 ' | grep -oP 'pid=\K[0-9]+' | sort -u); count=$(echo "$pids" | grep -c .); echo "listener_pids_on_8000=$count"; if [ "$count" -gt 1 ]; then echo "DUPLICATE_LISTENERS: $pids"; fi; echo "(client connections to :8000, for context: $(lsof -ti :8000 2>/dev/null | sort -u | wc -l) total fds)"`

### Watchdog timer state

!`~/.venvs/lifeos/bin/python -c "
import subprocess
for t in ['lifeos-watchdog', 'lifeos-server-watchdog', 'lifeos-gpu-watchdog', 'lifeos-sync']:
    try:
        out = subprocess.run(
            ['systemctl', 'list-timers', f'{t}.timer', '--no-pager'],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
        # The output is a header row, data row(s), then footer. Find the data row.
        data = None
        for line in out:
            if t in line:
                data = line
                break
        if not data:
            print(f'{t}: (timer not installed)')
            continue
        # Columns: NEXT (3 cols) | LEFT | LAST (3 cols) | PASSED | UNIT | ACTIVATES
        # Just dump the line with our prefix; humans can read it.
        print(f'{t}: {data.strip()}')
    except Exception as e:
        print(f'{t}: (error: {e})')
" 2>/dev/null`

### Watchdog log tails (last 5 entries each)

!`for f in logs/server-watchdog.log logs/gpu-watchdog.log logs/chromadb-watchdog.log; do if [ -f "$f" ]; then echo "=== $f ==="; tail -5 "$f" 2>/dev/null; fi; done`

### Monarch session status

!`if [ -f data/monarch_session.pickle ]; then
  ~/.venvs/lifeos/bin/python -c "
import sys; sys.path.insert(0, '.')
try:
    from api.services.monarch import get_session_status
    s = get_session_status()
    print(f\"status={s['status']}  age_days={s.get('age_days','?')}  msg={s['message']}\")
except Exception as e:
    print(f'MONARCH_ERROR: {e}')
" 2>/dev/null
else
  echo "NO_MONARCH_SESSION"
fi`

### Storage — sizes of the big stores

!`du -sh data/chromadb data/bm25_index.db data/interactions.db data/crm.db data/sync_health.db data/backups 2>/dev/null`

### Backup recency (most recent backup of each DB)

!`if [ -d data/backups ]; then /bin/ls -lt data/backups/ 2>/dev/null | head -10; fi`

### Latest sync log tail (for errors during last run)

!`latest=$(/bin/ls -t logs/sync_*.log 2>/dev/null | head -1); if [ -n "$latest" ]; then echo "=== $latest ==="; grep -iE "error|failed|timeout|exception|saturat|oom" "$latest" 2>/dev/null | tail -10 || echo "(no errors found)"; fi`

### Pending tonight's nightly — next scheduled sync run

!`systemctl list-timers lifeos-sync --no-pager 2>/dev/null | head -3`

### Configuration sanity — backend + key env vars

!`~/.venvs/lifeos/bin/python -c "
import sys; sys.path.insert(0, '.')
from config.settings import settings
print(f'llm_backend={settings.llm_backend}')
print(f'local_llm_url={settings.local_llm_url}')
print(f'anthropic_model={settings.anthropic_model}')
print(f'vault_path={settings.vault_path}')
print(f'embedding_model={settings.embedding_model}')
print(f'agent_worker_autostart={settings.agent_worker_autostart}')
print(f'local_llm_autostart={settings.local_llm_autostart}')
" 2>/dev/null`

## Instructions

You are producing a **sync health checkup** — a structured report that surfaces problems instead of a narrative of what's running. The data above is your only ground truth; do **not** speculate beyond it. If a block returned an error or was empty, say so in the corresponding section; don't paper over it.

### Step 1: Triage

Look for blocking errors in the data:
- `NO_SYNC_HEALTH_DB` → say so plainly; the rest of the sync section is moot but the LLM / vault / service sections still work.
- `DB_ERROR` → database is locked or corrupt; flag and continue.
- All Python blocks returning `ERROR:` → the venv is broken or `sys.path` couldn't resolve; flag and continue.

### Step 2: Compute the overall verdict

Walk the data and assign one of:
- **🟢 Healthy** — every check inside expected ranges; no recent errors of consequence.
- **🟡 Degraded** — at least one WARN-level finding (stale source, watchdog hasn't fired recently, Monarch expiring_soon, VRAM > 60%).
- **🔴 Failing** — at least one FAIL-level finding (orphan running rows older than 24h, drift detected, vault BM25/Chroma severely under-populated, VRAM > 80%, server stacking, Monarch expired, recent CRITICAL errors).

### Step 3: Write the report

Use the section headers below. For each section, lead with a single-character status (✅ ⚠️ 🔴) then a one-line summary, then the numbers. Skip the detail tables under `--brief`. Include the full per-source dumps and the long-tail error log under `--verbose`.

---

#### Overall

> **🟡 Degraded — 2 stale sources, Monarch session 31d old (expired), tonight's nightly will likely fail on `slack` again unless action taken.**

One sentence. Pull from Step 2.

#### Sync Runs

Per-source status from the `sync_runs` block.
- ✅ if last status was `success` and `completed_at` is within the source's expected frequency (daily / weekly / monthly).
- ⚠️ if status is `success` but last run is stale (older than the frequency threshold).
- 🔴 if status is `failed`, or there's an orphan `running` row older than 8h.

Default output: only sources with ⚠️ or 🔴, plus a footer like `(<N> other sources healthy)`. Under `--verbose` show all sources.

For each problem source include: `source | status | last_completed | stats | error_excerpt`.

#### Source-Entity Drift

If the drift detector returned `NO_DRIFT` → ✅ "no silent regressions detected".
Otherwise → 🔴 with the per-source `(last_interaction, last_source_entity, gap_days)` table. This is the silent-regression pattern from issue #199 §2 — interactions still flowing but source_entities stopped weeks ago.

#### Vault Index

Three numbers must roughly align:
- `md_files` — files on disk
- `chroma_vectors` — vector store population
- `bm25_chunks` — BM25 population

Expected: `chroma_vectors ≈ bm25_chunks ≈ 5–15× md_files` (chunks per file vary). Severely under-populated (`chroma_vectors < md_files`) → 🔴. Within order of magnitude → ✅.

Also inspect the doc_id sample. If any row references a path that doesn't match the current vault path (e.g. `/Users/` on Linux, `/home/` on macOS), flag a 🔴 for stale BM25 entries with the suggested fix: `python scripts/sync_vault_reindex.py --execute --force --clear-bm25`.

#### LLM Runtime

Read the systemd state + VRAM usage:
- 🔴 if BOTH lifeos-llm and ollama are active — that's the dual-stack pattern from 2026-05-28. Strongly suggest stopping one.
- 🔴 if VRAM > 80% — alert is imminent / already firing.
- ⚠️ if VRAM 60–80% — heading toward trouble.
- ⚠️ if ollama is `enabled` (not just active) — it'll come back on next boot. After PR #217, ollama should be `disabled`.
- ✅ if lifeos-llm is active, ollama is inactive+disabled, VRAM under 50%.

Include the top VRAM consumers in the section body for any state other than ✅.

#### Services

For each LifeOS service: ✅ if active, 🔴 if it's expected-active and `inactive` or `failed`, ⚠️ if `activating` (in startup).

Expected-active services depend on settings:
- `lifeos-api`, `lifeos-chromadb` — always expected active
- `lifeos-llm` — expected active iff `local_llm_autostart=True`
- `lifeos-mcp-http` — expected active iff there's a non-empty `LIFEOS_MCP_BEARER_TOKEN`
- `lifeos-agent-worker` — expected active iff `agent_worker_autostart=True`

#### Server Stacking

✅ if `listener_pids_on_8000=1`. 🔴 if `DUPLICATE_LISTENERS` present — the issue #199 §5 pattern that PR #214 added the pkill fix for. Suggest `./scripts/server.sh restart`. The "client connections" count is informational only; agent-worker and mcp-http legitimately keep open FDs to the API.

#### Watchdogs

For each timer in (`lifeos-watchdog`, `lifeos-server-watchdog`, `lifeos-gpu-watchdog`, `lifeos-sync`):
- ✅ if the timer last fired within `2 × its scheduled interval`
- ⚠️ if it hasn't fired in `2–5 × its interval`
- 🔴 if it hasn't fired in `> 5 × its interval` OR if `systemctl list-timers` returned nothing for it (timer not installed)

#### Monarch

Echo the status from `get_session_status()`. `🔴 expired/missing → re-auth needed` (point at the procedure in `MEMORY.md`). `⚠️ expiring_soon` → mention how many days until next monthly sync (1st of month). `✅ ok` → just the age.

#### Storage

Just the sizes from `du`, plus the most recent backup of each DB and its age (from the `data/backups` listing). ⚠️ if any DB backup is older than 7 days.

#### Tonight's Nightly Readiness

Combine the prior signals: will the nightly run cleanly?
- 🔴 if VRAM is already > 60% — embedding phase will likely fall back to CPU or OOM.
- 🔴 if vault_reindex has been failing repeatedly (3+ consecutive `failed` rows in `sync_runs`).
- 🔴 if there's an orphan `running` row from a previous sync that the reaper missed (shouldn't happen post-PR #214, but check anyway).
- ⚠️ if Monarch is expired and the 1st of the month is within 7 days.
- ⚠️ if any sync source has `enabled` but `last_status=failed` for the last 3 runs — the dependency cascade will skip downstream sources.
- ✅ otherwise.

End with the timer's "next fire" timestamp so the user knows when to expect it.

### Step 4: Action Items

End the report with a numbered list of concrete commands to run, ordered by severity. Examples:
1. `~/.venvs/lifeos/bin/python -c "import asyncio; ..."` — re-auth Monarch
2. `./scripts/server.sh restart` — clear stacked uvicorn PIDs
3. `~/.venvs/lifeos/bin/python scripts/sync_vault_reindex.py --execute --force --clear-bm25` — drop stale doc_ids
4. `sudo systemctl disable ollama` — keep it off across reboots

Only include items the data actually supports. Don't add advice that wasn't in the findings.

## Scoping notes

- `--brief` should cap output at ~30 lines. Skip detail tables, only show the section headers + verdict line + numbers.
- `--verbose` adds: full `sync_runs` table, full `sync_errors` tail, full storage / backup listing, full configuration dump.
- This skill is **read-only**. Never run mutations. Suggest commands, don't execute them.
