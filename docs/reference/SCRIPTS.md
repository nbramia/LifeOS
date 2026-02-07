# Scripts Reference

Reference for all LifeOS scripts with usage examples.

---

## Server Management

### server.sh

Manage the LifeOS API server.

```bash
./scripts/server.sh start    # Start server
./scripts/server.sh stop     # Stop server
./scripts/server.sh restart  # Restart (after code changes)
./scripts/server.sh status   # Check if running
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
```

Workflow:
1. Runs `./scripts/test.sh smoke`
2. Restarts server
3. Creates git commit
4. Pushes to remote

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

Orchestrate all data sync operations.

```bash
# Check sync status
uv run python scripts/run_all_syncs.py --status

# Dry run (show what would run)
uv run python scripts/run_all_syncs.py --dry-run

# Execute full sync
uv run python scripts/run_all_syncs.py --execute --force

# Run specific source only
uv run python scripts/run_all_syncs.py --source gmail --force
```

**Phases**:
1. Data Collection (Gmail, Calendar, Contacts, etc.)
2. Entity Processing (link Slack/iMessage to people)
3. Relationship Building (discover connections)
4. Vector Store Indexing (reindex vault)
5. Content Sync (Google Docs/Sheets to vault)

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
| `sync_contacts_csv.py` | Import Apple Contacts |
| `sync_phone_calls.py` | Sync phone call history |
| `sync_whatsapp.py` | Sync WhatsApp messages |
| `sync_imessage_interactions.py` | Sync iMessage |
| `sync_slack.py` | Sync Slack users and DMs |

Example:
```bash
uv run python scripts/sync_gmail_calendar_interactions.py --execute
```

#### Entity Processing

| Script | Purpose |
|--------|---------|
| `link_slack_entities.py` | Link Slack users to people by email |
| `link_imessage_entities.py` | Link iMessage handles by phone |
| `sync_photos.py` | Sync Photos face recognition |

#### Relationship Building

| Script | Purpose |
|--------|---------|
| `sync_relationship_discovery.py` | Discover relationships from interactions |
| `sync_strengths.py` | Recalculate relationship strengths |
| `sync_person_stats.py` | Verify/repair interaction counts |

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

---

## Service Management

### service.sh

Manage launchd services.

```bash
./scripts/service.sh status   # Show all LifeOS services
./scripts/service.sh start    # Start all services
./scripts/service.sh stop     # Stop all services
./scripts/service.sh restart  # Restart all services
```

---

### setup-launchd.sh

Configure launchd services from templates.

```bash
./scripts/setup-launchd.sh
```

Interactive script that:
1. Prompts for vault path
2. Generates plist files from templates
3. Installs to `~/Library/LaunchAgents/`

---

## Authentication

### google_auth.py

Authenticate with Google OAuth.

```bash
# Personal account
uv run python scripts/google_auth.py --account personal

# Work account
uv run python scripts/google_auth.py --account work
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
- Virtual environment active or `uv run` prefix
- `.env` file with configuration
- ChromaDB running for vector operations
- Server running for API-based operations

### Logs

Sync logs go to:
- stdout/stderr during execution
- `logs/crm-sync.log` when run via launchd
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
uv run python scripts/run_all_syncs.py --status

# Run specific source with debug output
uv run python scripts/sync_gmail_calendar_interactions.py --execute 2>&1 | tee debug.log

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
