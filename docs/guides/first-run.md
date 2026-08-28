# First Run Guide

> **Status:** Complete
> **Last Updated:** 2026-07-09
> **Audience:** New users

Post-installation guide for your first use of LifeOS.

This guide covers the **minimal path** — indexing your vault and confirming search and
chat work. The optional integrations (Google, Slack, Monarch, Telegram, Apple Data
Agent, local LLM, voice, agent worker) that populate CRM and relationship features are
described in [Installation → Start Here](installation.md#start-here-minimal-vs-full-setup).

---

## Prerequisites

Before continuing, ensure you have:
- [x] Completed [Installation](installation.md)
- [x] Configured [Environment](configuration.md)
- [x] Server running: `./scripts/server.sh status`
- [x] ChromaDB running: `./scripts/chromadb.sh status`

---

## Step 1: Initial Vault Index

Index your Obsidian vault for semantic search:

```bash
# Check current index status
curl http://localhost:8000/api/admin/health | jq

# Trigger full reindex (runs in background)
curl -X POST http://localhost:8000/api/admin/reindex

# Or trigger blocking reindex (waits for completion)
curl -X POST http://localhost:8000/api/admin/reindex/sync
```

First index may take several minutes depending on vault size. Monitor progress in logs:
```bash
tail -f logs/lifeos-api.log
```

---

## Step 2: Verify Search

Test that search is working:

```bash
# Search your vault
curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "test query", "top_k": 5}' | jq

# Ask a question (uses RAG)
curl -X POST http://localhost:8000/api/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "What did I write about recently?"}'
```

---

## Step 3: Web UI Walkthrough

Open http://localhost:8000 in your browser.

### Chat Interface

1. Type a question in the input box
2. Press Enter or click Send
3. View sources in the expandable section
4. See routing indicator (semantic/keyword/hybrid)

### CRM Interface

Navigate to http://localhost:8000/crm.html

1. **People List**: Browse all indexed people
2. **Search**: Filter by name, company, or email
3. **Person Detail**: Click a person to see timeline
4. **Network Graph**: Visualize relationships

---

## Step 4: Run Initial Sync (Optional)

If you've configured Google OAuth or Slack, run the initial data sync:

```bash
# Dry run (shows what would sync)
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --dry-run

# Execute sync
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --execute --force
```

**Note**: First sync may take 30+ minutes depending on data volume.

### After First Sync: Set Your Identity

After sync completes, run the guided setup script to pick yourself out of
the indexed people, name your partner, configure family, and set your work
email domain(s):

```bash
~/.venvs/lifeos/bin/python scripts/setup_identity.py
```

It searches the same people list the CRM UI does, writes what you answer
into `.env` and `config/*.json` (backing up any existing files first), and
reports what it wrote and which names it couldn't match. Safe to re-run at
any time. Or set `LIFEOS_MY_PERSON_ID` by hand:

```bash
curl "http://localhost:8000/api/crm/people?q=YourName" | jq '.people[0].id'
```

Then restart: `./scripts/server.sh restart`

This enables relationship tracking, communication gap analysis, and other CRM features that need to know which person is "you."

### Pull Full History (Optional)

**This is the nightly job, not a deep pass.** It deliberately looks back
only a narrow window (30 days for Gmail/Calendar) to stay fast — a fresh
install won't get older history from just this. Run the one-time backfill
to pull each source's full history:

```bash
~/.venvs/lifeos/bin/python scripts/first_backfill.py --execute
```

See [data-and-sync.md](../specs/technical/data-and-sync.md#first-backfill-entry-point) for what it covers and how long it can take.

---

## Step 5: MCP Integration (Optional)

If using Claude Code, add LifeOS as an MCP server:

```bash
# Add MCP server (point at your LifeOS checkout)
claude mcp add lifeos -s user -- ~/.venvs/lifeos/bin/python ~/LifeOS/mcp_server.py
```

Verify tools are available:
```bash
claude mcp list
```

Available tools include:
- `lifeos_ask` - Query with synthesis
- `lifeos_search` - Raw search results
- `lifeos_meeting_prep` - Meeting briefings
- `lifeos_people_search` - CRM search
- `lifeos_task_create` / `lifeos_task_list` - Task management
- `lifeos_schedule_create` / `lifeos_schedule_list` - Schedules (triggers + actions)

See [MCP Tools](../specs/product/mcp-tools.md) for full tool list.

---

## Step 6: Set Up Automated Services

For production use, configure system services for:
- API server auto-start on boot
- Nightly data sync at 3 AM

- **Linux**: `sudo ./scripts/setup-systemd.sh`
- **macOS**: See [Launchd Setup Guide](launchd-setup.md)

---

## Verification Checklist

Run this checklist to ensure everything is working:

| Check | Command | Expected |
|-------|---------|----------|
| Server health | `curl localhost:8000/health/full \| jq` | All services "healthy" |
| ChromaDB | `curl localhost:8001/api/v2/heartbeat` | `{"nanosecond heartbeat":...}` |
| Local LLM (only if `LIFEOS_LLM_BACKEND=local`) | `curl $LIFEOS_LOCAL_LLM_URL/v1/models \| jq` | Lists the loaded model |
| Search works | Search via UI | Returns results |
| Index populated | `curl localhost:8000/api/search -d '{"query":"test"}'` | Non-empty results |
| Tasks API | `curl localhost:8000/api/tasks` | `{"tasks":[],"total":0}` |
| Scheduler API | `curl localhost:8000/api/scheduler` | `{"schedules":[...]}` |

---

## Next Steps

1. **Configure integrations**:
   - [Google OAuth](google-oauth.md) for Calendar/Gmail/Drive
   - [Slack Integration](slack-integration.md) for Slack messages

2. **Set up services**:
   - Linux: `sudo ./scripts/setup-systemd.sh`
   - macOS: [Launchd Setup](launchd-setup.md) for auto-start

3. **Learn the API**:
   - [API Reference](../specs/product/api-reference.md)
   - [MCP Tools](../specs/product/mcp-tools.md)

---

## Common Issues

| Issue | Solution |
|-------|----------|
| Search returns no results | Run reindex: `curl -X POST localhost:8000/api/admin/reindex/sync` |
| Slow first query | If using `LIFEOS_LLM_BACKEND=local`, llama-server loads the model on first request — wait ~30s. If using the Anthropic backend, first call is uncached — subsequent calls within ~5 min hit Anthropic's prompt cache. |
| MCP tools not working | Check server is running and MCP added correctly |

See [Troubleshooting](troubleshooting.md) for more.

## Related Documents

- [Installation](installation.md) -- Initial installation walkthrough
- [Configuration](configuration.md) -- Environment variables and config files
- [Setup](setup.md) -- Interactive Claude Code setup guide
- [Data & Sync Architecture](../specs/technical/data-and-sync.md) -- Nightly sync schedule and the first-backfill entry point referenced in Step 4
