# Linux Migration — Remaining Steps

> **Status:** Ready for execution
> **Date:** 2026-03-04
> **Context:** Phases 1-4 complete. Code is migrated, server runs on Linux, all LLM calls use local model. These are the remaining manual/operational steps.

---

## Current State

| Item | Status |
|------|--------|
| LifeOS API server | Running on Linux (`100.68.0.120:8000`) |
| Local LLM (GPT-OSS-120B) | Running at `localhost:8080` (~50 t/s) |
| ChromaDB | Running at `localhost:8001` (2.5GB data) |
| Vault (`~/Notes 2025`) | Synced (737MB) |
| All databases | Present (crm.db 571MB, bm25_index 272MB, imessage 81MB, etc.) |
| Data backups | Present (6.2GB) |
| Health check | All 12 services healthy |
| Tests | 1698 passed, 5 data-path failures (need reindex) |
| Systemd units | Created but **not installed** |
| SSH key (Linux) | `~/.ssh/id_ed25519` exists, `authorized_keys` empty |

---

## A. Linux Server Steps (do on `100.68.0.120`)

### A1. Install systemd services

This makes LifeOS start on boot and sets up the nightly sync timer.

```bash
cd ~/Documents/Code/LifeOS
sudo ./scripts/setup-systemd.sh
```

Verify:
```bash
systemctl status lifeos-api
systemctl status lifeos-chromadb
systemctl list-timers lifeos-*
```

### A2. Vault reindex

The indexed data still references macOS paths (`/Users/nathanramia/Notes 2025`). A full reindex will fix this and resolve the 5 failing tests.

```bash
curl -X POST http://localhost:8000/api/admin/reindex
```

This takes a while (indexes all vault files with LLM summaries). Check progress:
```bash
tail -f logs/lifeos-api.log
```

### A3. Test Google OAuth

The OAuth token files from macOS may or may not work. Test with a real query:

```bash
# Test calendar
curl "http://localhost:8000/api/calendar/events?days=7" | python3 -m json.tool | head -20

# Test Gmail
curl "http://localhost:8000/api/gmail/search?q=test&max_results=3" | python3 -m json.tool | head -20
```

If you get auth errors, delete the stale tokens and re-auth:
```bash
rm config/token-*.json
# Then open http://100.68.0.120:8000 in a browser and trigger a query
# that needs Google (e.g., "what's on my calendar today?")
# The OAuth flow will open in the browser
```

### A4. Set up SSH for Mac Mini → Linux

The Mac Mini needs to rsync to this machine. Add the Mac Mini's public key to authorized_keys:

```bash
# Option 1: If you can SSH from Mac Mini to Linux already (via password):
# Run this ON THE MAC MINI:
ssh-copy-id nathanramia@100.68.0.120

# Option 2: Manually copy the key. On Mac Mini, run:
cat ~/.ssh/id_ed25519.pub
# Then on Linux, append that output to:
nano ~/.ssh/authorized_keys
# (paste the key, save)

# Option 3: If Mac Mini doesn't have an SSH key yet:
# On Mac Mini:
ssh-keygen -t ed25519 -C "macmini"
ssh-copy-id nathanramia@100.68.0.120
```

Test from Mac Mini:
```bash
ssh nathanramia@100.68.0.120 "echo 'SSH works'"
```

### A5. Set up Syncthing (optional, for vault sync)

If you want the vault to stay in sync between machines without rsync:
```bash
sudo systemctl enable --now syncthing@nathanramia
# Then open http://localhost:8384 to configure
# Share ~/Notes 2025 between Mac Mini and Linux
```

---

## B. Mac Mini Steps (do on `100.95.233.70`)

### B1. Pull latest code

```bash
cd ~/Documents/Code/LifeOS
git pull origin linux_migration
```

Or if using Syncthing, wait for it to sync. Verify the new scripts exist:
```bash
ls scripts/apple_data_agent.sh scripts/apple_data_export.py scripts/apple_data_import.py
```

### B2. Stop the LifeOS server

The Mac Mini should no longer run the API server — Linux handles that now.

```bash
cd ~/Documents/Code/LifeOS

# Stop the running server
./scripts/server.sh stop

# Disable the launchd service (prevents auto-start on boot/login)
./scripts/service.sh stop

# Verify it's not running
./scripts/server.sh status
# Should say: "not running"
```

### B3. Stop the ChromaDB watchdog cron

```bash
crontab -l > /tmp/old_crontab.bak   # backup first
crontab -l | grep -v "LifeOS\|lifeos\|watchdog\|run_sync\|run_all_syncs\|fda" > /tmp/new_crontab
```

Review what you're removing:
```bash
diff /tmp/old_crontab.bak /tmp/new_crontab
```

Then install the new crontab with just the Apple Data Agent:
```bash
# Add the single Apple Data Agent entry
echo '50 2 * * * /Users/nathanramia/Documents/Code/LifeOS/scripts/apple_data_agent.sh' >> /tmp/new_crontab
crontab /tmp/new_crontab
crontab -l   # verify
```

Expected crontab after this:
```
50 2 * * * /Users/nathanramia/Documents/Code/LifeOS/scripts/apple_data_agent.sh
```

That's it — one cron job. It handles FDA syncs, export, and rsync to Linux.

### B4. Set up SSH key to Linux

```bash
# Check if you already have a key
ls ~/.ssh/id_ed25519.pub

# If not, generate one:
ssh-keygen -t ed25519 -C "macmini"

# Copy to Linux server
ssh-copy-id nathanramia@100.68.0.120

# Test
ssh nathanramia@100.68.0.120 "echo 'SSH from Mac Mini works'"
```

### B5. Test the Apple Data Agent manually

```bash
cd ~/Documents/Code/LifeOS

# Dry run (no export, just check what would happen)
~/.venvs/lifeos/bin/python scripts/apple_data_export.py --dry-run

# Full run (export + rsync)
./scripts/apple_data_agent.sh
```

Check the log:
```bash
ls -t logs/apple_agent_*.log | head -1 | xargs cat
```

Verify data arrived on Linux:
```bash
ssh nathanramia@100.68.0.120 "ls -la ~/Documents/Code/LifeOS/data/apple-imports/"
```

---

## C. Verification Checklist

Run these after completing A and B:

| Check | Command (on Linux) | Expected |
|-------|-------------------|----------|
| Server healthy | `curl http://100.68.0.120:8000/health/full \| jq .summary` | "All 12 services healthy" |
| Systemd services | `systemctl is-active lifeos-api lifeos-chromadb` | Both "active" |
| Nightly timer | `systemctl list-timers lifeos-sync.timer` | Shows next trigger ~3:30 AM |
| Vault indexed | `curl -X POST http://localhost:8000/api/search -d '{"query":"test"}'` | Returns results with Linux paths |
| Google OAuth | `curl "http://localhost:8000/api/calendar/events?days=7"` | Returns events (not auth error) |
| Mac Mini server stopped | `ssh nathanramia@100.95.233.70 "curl -s http://localhost:8000/health 2>&1"` | Connection refused |
| Mac Mini cron correct | `ssh nathanramia@100.95.233.70 "crontab -l"` | Single apple_data_agent.sh entry |
| Apple imports exist | `ls data/apple-imports/manifest.json` | File exists after first agent run |
| Tests pass | `~/.venvs/lifeos/bin/python -m pytest tests/ -q -m "not browser and not slow"` | 1700+ passed, 0 data-path failures |

---

## D. Documentation Updates (Phase 6, low priority)

These can be done anytime after cutover is verified:

1. **CLAUDE.md** — Update Remote Development section:
   - Change Tailscale IP from `100.95.233.70` to `100.68.0.120`
   - Change "Mac Mini" to "Linux server" for server operations
   - Update SSH commands
   - Keep "Edit code on MacBook" (still true via Syncthing)

2. **AGENTS.md** — Update:
   - Tech stack table (add local LLM, note Linux)
   - Server Management section (add systemd alongside launchd)
   - macOS Permissions section (note this is now Mac Mini agent only)
   - Common Mistakes (update SSH IP)

3. **Create ADR** — `docs/adr/007-linux-migration.md`:
   - Decision: Move server from Mac Mini to Linux workstation
   - Context: 96GB VRAM enables local LLM, faster processing
   - Consequences: Mac Mini becomes Apple Data Agent

4. **Archive macOS-only docs** — Move any pure-macOS operational docs to archive

---

## E. Nice-to-Haves (not blocking)

- **ROCm PyTorch update**: Currently running PyTorch ROCm 6.4, but ROCm 7.2 is installed. May want to rebuild PyTorch for 7.2 for better gfx1151 support (current HSA_OVERRIDE works fine though).
- **Remove `HSA_OVERRIDE_GFX_VERSION`**: If PyTorch ROCm 7.2 natively supports gfx1151, this env var can be removed.
- **Syncthing for vault**: More reliable than rsync for continuous sync of ~/Notes 2025.
- **Telegram bot update**: If the Telegram bot webhook points to the Mac Mini IP, update it to the Linux IP.
