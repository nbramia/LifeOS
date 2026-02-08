# First Run Guide

Post-installation guide for your first use of LifeOS.

---

## Prerequisites

Before continuing, ensure you have:
- [x] Completed [Installation](INSTALLATION.md)
- [x] Configured [Environment](CONFIGURATION.md)
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
uv run python scripts/run_all_syncs.py --dry-run

# Execute sync
uv run python scripts/run_all_syncs.py --execute --force
```

**Note**: First sync may take 30+ minutes depending on data volume.

---

## Step 5: MCP Integration (Optional)

If using Claude Code, add LifeOS as an MCP server:

```bash
# Add MCP server
claude mcp add lifeos -s user -- python /path/to/LifeOS/mcp_server.py
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
- `lifeos_reminder_create` / `lifeos_reminder_list` - Reminders

See [API & MCP Reference](../architecture/API-MCP-REFERENCE.md) for full tool list.

---

## Step 6: Set Up Automated Services

For production use, configure launchd services for:
- API server auto-start on boot
- Nightly data sync at 3 AM

See [Launchd Setup Guide](../guides/LAUNCHD-SETUP.md).

---

## Verification Checklist

Run this checklist to ensure everything is working:

| Check | Command | Expected |
|-------|---------|----------|
| Server health | `curl localhost:8000/health/full \| jq` | All services "healthy" |
| ChromaDB | `curl localhost:8001/api/v1/heartbeat` | `{"nanosecond heartbeat":...}` |
| Ollama | `curl localhost:11434/api/tags \| jq` | Lists models |
| Search works | Search via UI | Returns results |
| Index populated | `curl localhost:8000/api/search -d '{"query":"test"}'` | Non-empty results |
| Tasks API | `curl localhost:8000/api/tasks` | `{"tasks":[],"total":0}` |
| Reminders API | `curl localhost:8000/api/reminders` | `{"reminders":[...]}` |

---

## Next Steps

1. **Configure integrations**:
   - [Google OAuth](../guides/GOOGLE-OAUTH.md) for Calendar/Gmail/Drive
   - [Slack Integration](../guides/SLACK-INTEGRATION.md) for Slack messages

2. **Set up services**:
   - [Launchd Setup](../guides/LAUNCHD-SETUP.md) for auto-start

3. **Learn the API**:
   - [API Reference](../architecture/API-MCP-REFERENCE.md)

---

## Common Issues

| Issue | Solution |
|-------|----------|
| Search returns no results | Run reindex: `curl -X POST localhost:8000/api/admin/reindex/sync` |
| Slow first query | Ollama loading model - wait 30s |
| MCP tools not working | Check server is running and MCP added correctly |

See [Troubleshooting](../reference/TROUBLESHOOTING.md) for more.
