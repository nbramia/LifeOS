# Scripts Reference

> **Status:** Complete
> **Last Updated:** 2026-08-27
> **Audience:** Operators

Reference for all LifeOS scripts with usage examples.

---

## Server Management

### server.sh

Manage the LifeOS API server.

```bash
./scripts/server.sh start      # Start server (background)
./scripts/server.sh stop       # Stop server
./scripts/server.sh restart    # Restart (after code changes)
./scripts/server.sh status     # Check if running
./scripts/server.sh wait       # Wait for server to become healthy
./scripts/server.sh preflight  # Check prerequisites before first start
./scripts/server.sh foreground # Start server in foreground (for systemd)
```

**Important**: Always use this script. Never run `uvicorn` directly.

---

### chromadb.sh

Manage the ChromaDB vector database server.

```bash
./scripts/chromadb.sh start    # Start ChromaDB
./scripts/chromadb.sh stop     # Stop ChromaDB
./scripts/chromadb.sh restart  # Restart
./scripts/chromadb.sh status   # Check status
```

ChromaDB runs on port 8001.

---

## Deployment

### deploy.sh

Test, restart, commit, and push in one command.

```bash
./scripts/deploy.sh "Your commit message"
./scripts/deploy.sh --skip-tests "Your commit message"  # Skip tests
./scripts/deploy.sh --no-push "Your commit message"     # Commit but don't push
```

Workflow:
1. Runs `./scripts/test.sh smoke` (unless `--skip-tests`)
2. Restarts server
3. Creates git commit
4. Pushes to remote (unless `--no-push`)

---

## Testing

### test.sh

Run test suites.

```bash
./scripts/test.sh          # Unit tests only (~30s)
./scripts/test.sh smoke    # Unit + critical browser tests
./scripts/test.sh all      # Full test suite (slower)
```

---

## Sync Scripts

### run_all_syncs.py

Orchestrate all data sync operations. On Linux the nightly run is driven by the `lifeos-sync.timer` systemd timer (installed by `setup-systemd.sh`) — not a raw crontab. Check its schedule with `systemctl list-timers lifeos-sync.timer`. On macOS the equivalent is the `com.lifeos.crm-sync` launchd agent. The commands below run it manually.

```bash
# Check sync status
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status

# Dry run (show what would run)
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --dry-run

# Execute full sync
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --execute --force

# Run specific source only
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --source gmail --force
```

**Phases**:
1. Data Collection (Gmail, Calendar, Contacts, etc.)
2. Entity Processing (link Slack/iMessage to people)
3. Relationship Building (discover connections)
4. Vector Store Indexing (reindex vault)
5. Content Sync (Google Docs/Sheets to vault)
6. Entity Cleanup (auto-hide obvious non-human entities)
7. Consistency Verification (orphan checks, stats reconciliation)

---

### Individual Sync Scripts

All sync scripts follow the pattern:
- Dry run by default (shows what would change)
- Use `--execute` to apply changes

#### Data Collection

| Script | Purpose |
|--------|---------|
| `sync_gmail_calendar_interactions.py` | Sync Gmail and Calendar |
| `sync_linkedin.py` | Import LinkedIn connections |
| `sync_contacts_csv.py` | Import Apple Contacts (CSV) |
| `sync_apple_contacts.py` | Sync Apple Contacts via direct API |
| `sync_phone_calls.py` | Sync phone call history |
| `sync_imessage_interactions.py` | Sync iMessage |
| `sync_slack.py` | Sync Slack users and DMs |
| `sync_monarch_money.py` | Sync Monarch Money financial data |

Example:
```bash
~/.venvs/lifeos/bin/python scripts/sync_gmail_calendar_interactions.py --execute
```

#### Entity Processing

| Script | Purpose |
|--------|---------|
| `link_slack_entities.py` | Link Slack users to people by email |
| `link_imessage_entities.py` | Link iMessage handles by phone |
| `link_source_entities.py` | Retroactive linking for unlinked entities |
| `sync_photos.py` | Sync Photos face recognition |

#### Relationship Building

| Script | Purpose |
|--------|---------|
| `sync_relationship_discovery.py` | Discover relationships from interactions |
| `sync_strengths.py` | Recalculate relationship strengths |
| `sync_person_stats.py` | Verify/repair interaction counts |
| `push_birthdays_to_contacts.py` | Push birthdays to Apple Contacts |

#### Vector Store

| Script | Purpose |
|--------|---------|
| `sync_vault_reindex.py` | Reindex vault to ChromaDB + BM25 |
| `sync_crm_to_vectorstore.py` | Index CRM people for search |

#### Content Sync

| Script | Purpose |
|--------|---------|
| `sync_google_docs.py` | Sync Google Docs to vault |
| `sync_google_sheets.py` | Sync Google Sheets to vault |

#### Post-Sync Cleanup

| Script | Purpose |
|--------|---------|
| `sync_entity_cleanup.py` | Auto-hide obvious non-human entities (noreply@, newsletters) |

---

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `backup.sh` | Backup data directory |
| `chromadb-watchdog.sh` | ChromaDB health check and auto-restart |
| `clear-caches.sh` | Clear embedding and search caches |
| `network-watchdog.sh` | WiFi link health check and self-heal (re-activate → bounce radio → reload driver → restart NetworkManager) |
| `auto-deploy.sh` | Poll `origin/main`; on a fast-forward advance, pull and restart the code services that changed. Pull-based, guarded (main branch + clean tree + `--ff-only`), opt-in via `LIFEOS_AUTODEPLOY_ENABLED`. Run by `lifeos-autodeploy.timer`. |
| `cleanup-worktrees.sh` | Idempotent git-worktree pruning plus targeted removal of a stale worktree/branch; safe to call pre-flight before `git worktree add`. |
| `migrate_reminders_to_scheduler.py` | One-shot, idempotent migration of the legacy `~/.lifeos/reminders.json` store into the Scheduler's `Inbox.md` source of truth. Non-destructive (keeps the JSON as backup). |
| `install_codex_skills.py` | Convert portable LifeOS skills from `.claude/skills/` into Codex's `SKILL.md` format into `~/.codex/skills/`. Re-run after editing source skills. |
| `register_persona_bot.py` | Safely register a new persona Telegram bot: appends its token/chat-id env vars to `.env` (append-only — a symlinked `.env` stays a symlink) and adds it to `config/telegram_bots.local.json` (seeded from the template on first use). Does not register the bot with @BotFather or restart the service — see [personas.md](personas.md#create-your-own-persona). |
| `create-lifeos-app.sh` | Create the `LifeOS.app` Full Disk Access wrapper bundle in `/Applications` (macOS only), the FDA container cron/launchd route through for protected databases. |
| `preflight.sh` | Pre-flight checks (called by server.sh) |
| `run_sync_wrapper.sh` | NVMe wake + pre-flight for nightly sync |
| `run_sync_with_fda.sh` | FDA wrapper for phone/iMessage sync (macOS) |
| `run_fda_syncs.py` | Python FDA sync orchestrator (macOS) |
| `apple_data_export.py` | Export Apple data (iMessage, Contacts, Photos) on macOS |
| `apple_data_import.py` | Import Apple data exports on the server |
| `apple_data_agent.sh` | Apple Data Agent orchestrator (runs on macOS, rsyncs to server) |

---

## Service Management

### service.sh

Manage system services (systemd on Linux, launchd on macOS).

```bash
./scripts/service.sh install    # Install and start the service (auto-start on boot)
./scripts/service.sh uninstall  # Stop and remove the service
./scripts/service.sh start      # Start the service
./scripts/service.sh stop       # Stop the service
./scripts/service.sh restart    # Restart the service
./scripts/service.sh status     # Check service status and health
./scripts/service.sh logs       # Tail the service logs
```

---

### setup-systemd.sh

Configure systemd services on Linux. Reads toggles from `.env`, substitutes templates from `config/systemd/`, then enables and starts the units. Re-run after changing any autostart toggle in `.env`.

```bash
sudo ./scripts/setup-systemd.sh
```

Installs and enables:

- **Services** — `lifeos-api`, `lifeos-chromadb`, `lifeos-llm` (local LLM; enabled only when `LIFEOS_LOCAL_LLM_AUTOSTART=true`), `lifeos-mcp-http` (enabled only when `LIFEOS_MCP_BEARER_TOKEN` is set), `lifeos-agent-worker` (enabled only when `LIFEOS_AGENT_WORKER_AUTOSTART=true`).
- **Timers** — `lifeos-watchdog`, `lifeos-server-watchdog`, `lifeos-gpu-watchdog`, `lifeos-network-watchdog`, `lifeos-sync` (nightly unified sync), and `lifeos-autodeploy` (enabled only when `LIFEOS_AUTODEPLOY_ENABLED=true`).
- **Supporting config** — a logrotate rule (`/etc/logrotate.d/lifeos`), a passwordless-`systemctl` sudoers rule (`/etc/sudoers.d/lifeos`) so `server.sh` and the sync scripts can manage units without a password, and an 8 GB swap file as an OOM safety net (created only if no swap is already active).

---

### setup-launchd.sh

Configure launchd services from templates (macOS only). Interactive: prompts for the vault path (or accepts it as an optional argument), generates plist files from `config/launchd/` templates, and installs them to `~/Library/LaunchAgents/`. ChromaDB is intentionally skipped — use a cron watchdog for it (see [launchd-setup.md](launchd-setup.md)).

```bash
./scripts/setup-launchd.sh                 # prompts for vault path
./scripts/setup-launchd.sh ~/Notes --yes   # pass vault path, skip confirmation
```

---

### setup-tailscale.sh

Expose LifeOS on the tailnet HTTPS front (port 443) via `tailscale serve`, so `/chat` voice works (the mic needs a secure context). Reverse-proxies to the local API; whisper-relay stays on localhost. Reads `LIFEOS_PORT` / `TAILNET_HTTPS_URL` from the environment.

```bash
./scripts/setup-tailscale.sh
```

---

### install-systemd-tailscale.sh

Generate a `--user` systemd unit (`lifeos-tailscale.service`) that runs `setup-tailscale.sh` once the API is healthy, so the Tailscale Serve proxy survives reboot.

```bash
./scripts/install-systemd-tailscale.sh
systemctl --user enable --now lifeos-tailscale.service
```

---

## Authentication

### google_auth.py

Authenticate with Google OAuth.

```bash
# Personal account
~/.venvs/lifeos/bin/python scripts/google_auth.py --account personal

# Work account
~/.venvs/lifeos/bin/python scripts/google_auth.py --account work
```

Opens browser for Google sign-in, saves token to configured path.

---

## Maintenance

### Manual API Triggers

These operations can also be triggered via API:

```bash
# Reindex vault (background)
curl -X POST http://localhost:8000/api/admin/reindex

# Reindex vault (blocking)
curl -X POST http://localhost:8000/api/admin/reindex/sync

# Trigger calendar sync
curl -X POST http://localhost:8000/api/admin/calendar/sync

# Trigger relationship discovery
curl -X POST http://localhost:8000/api/crm/relationships/discover

# Update relationship strengths
curl -X POST http://localhost:8000/api/crm/strengths/update
```

---

## Script Patterns

### Common Flags

| Flag | Description |
|------|-------------|
| `--execute` | Apply changes (default is dry run) |
| `--force` | Skip "already ran today" checks |
| `--dry-run` | Show what would happen without changes |
| `--source X` | Run only specific source |

### Environment

Scripts expect:
- `~/.venvs/lifeos` on the server
- `.env` file with configuration
- ChromaDB running for vector operations
- Server running for API-based operations

### Logs

Sync logs go to:
- stdout/stderr during execution
- `logs/crm-sync.log` when run via systemd/launchd
- `~/Notes/LifeOS/sync_errors.md` for error summaries

---

## Examples

### Daily Workflow

```bash
# Morning: check status
./scripts/server.sh status
curl http://localhost:8000/health/full | jq

# After code changes
./scripts/server.sh restart
./scripts/test.sh

# Ready to commit
./scripts/deploy.sh "Add new feature"
```

### Debug a Sync Issue

```bash
# Check what ran
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status

# Run specific source with debug output
~/.venvs/lifeos/bin/python scripts/sync_gmail_calendar_interactions.py --execute 2>&1 | tee debug.log

# Check for errors
cat ~/Notes/LifeOS/sync_errors.md
```

### Force Full Reindex

```bash
# Stop server
./scripts/server.sh stop

# Clear and rebuild (optional, destructive)
rm -rf data/chromadb/lifeos_*
rm data/chromadb/bm25_index.db

# Start server
./scripts/server.sh start

# Trigger full reindex
curl -X POST http://localhost:8000/api/admin/reindex/sync
```

## Related Documents

- [Launchd Setup](launchd-setup.md) -- Automated service management (macOS)
- [Troubleshooting](troubleshooting.md) -- Common issues and solutions
- [Personas](personas.md) -- `register_persona_bot.py`'s "Create your own persona" workflow
