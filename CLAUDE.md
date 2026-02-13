# Instructions for AI Coding Agents

Critical instructions for AI agents (Claude, Cursor, Copilot, etc.) working on this codebase.

---

## Project Overview

LifeOS is a self-hosted AI assistant that indexes personal data (notes, emails, messages) for semantic search and synthesis.

**Key Concepts:**
- **Two-tier data model**: SourceEntity (raw observations) → PersonEntity (canonical records)
- **Hybrid search**: Vector (ChromaDB) + keyword (BM25/FTS5)
- **Entity resolution**: Links emails/phones to canonical people

**Tech Stack:**
- FastAPI backend (port 8000)
- ChromaDB vector store (port 8001)
- Ollama for query routing
- Claude API for synthesis

**Documentation:**
- [README.md](README.md) - Quick start and architecture
- [docs/architecture/](docs/architecture/) - Technical details
- [docs/getting-started/](docs/getting-started/) - Setup guides

---

# Development Workflow

1. **Edit code**
2. **Restart server**: `./scripts/server.sh restart`
3. **Test manually** or run tests: `./scripts/test.sh`
4. **Deploy**: `./scripts/deploy.sh "Your commit message"`

Use the below guidelines when executing tasks or pursuing goals that have more than basic complexity. These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

These guidelines bias toward caution over speed. For trivial tasks (simple typo fixes, obvious one-liners), use judgment — not every change needs the full rigor.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## Common Mistakes to Avoid

1. **Running uvicorn directly** → Use `./scripts/server.sh start` 
2. **Forgetting to restart server after code changes** → Use `./scripts/server.sh restart`
3. **Committing without testing** → Use `./scripts/deploy.sh`
4. **Starting server on localhost only** → Must use 0.0.0.0 for Tailscale
5. **Overfitting to specific test cases** → All fixes must take into account the potential effects on the full system, never single-file patches to pass one query

---

## Key Files


| File | Purpose |
|------|---------|
| `api/main.py` | FastAPI application entry point |
| `api/services/task_manager.py` | Task management service (Obsidian Tasks integration) |
| `api/routes/tasks.py` | Task CRUD API endpoints |
| `config/settings.py` | Environment configuration |
| `config/people_dictionary.json` | Known people and aliases (restart required after edits) |
| `README.md` | Architecture documentation including hybrid search system |


| Script | Purpose |
|--------|---------|
| `./scripts/server.sh` | Start/stop/restart server |
| `./scripts/deploy.sh` | Test → restart → commit → push |
| `./scripts/test.sh` | Run test suites |
| `./scripts/service.sh` | launchd service management |

## Dependency Management

**Single source of truth**: `requirements.txt`
**Virtual environment**: `~/.venvs/lifeos` (external due to macOS TCC)

### Adding a new dependency

1. Add to `requirements.txt`
2. Install: `~/.venvs/lifeos/bin/pip install -r requirements.txt`
3. Restart server: `./scripts/server.sh restart`

### Why external venv?

macOS TCC security scanning causes 30+ second delays when launchd loads
agents from `~/Documents/`. The external venv at `~/.venvs/` is exempt.

### Testing

```bash
./scripts/test.sh              # Unit tests (fast, ~30s)
./scripts/test.sh smoke        # Unit + critical browser (used by deploy)
./scripts/test.sh all          # Full test suite
```

## Server Management — CRITICAL

**NEVER run uvicorn or start the server directly.** Always use the provided scripts:

```bash
./scripts/server.sh start    # Start server
./scripts/server.sh stop     # Stop server
./scripts/server.sh restart  # Restart after code changes
./scripts/server.sh status   # Check if running
```
Always restart the server after modifying Python files
The server does NOT auto-reload. Changes won't take effect until restart.

### Why This Matters

Running `uvicorn api.main:app` directly causes **ghost server processes**:

1. The script binds to `0.0.0.0:8000` (all interfaces including Tailscale)
2. Direct uvicorn often binds only to `127.0.0.1:8000` (localhost)
3. This creates TWO servers on different interfaces
4. User sees different behavior via localhost vs Tailscale/network
5. Code changes appear to "not work" because the wrong server handles requests

---

## Common Tasks

### Check Service Health

```bash
# API endpoint health (tests all endpoints)
curl http://localhost:8000/health/full | jq

# External service status (ChromaDB, Ollama, etc.)
curl http://localhost:8000/health/services | jq
```

The `/health/services` endpoint shows:
- Per-service status (healthy/degraded/unavailable)
- Degradation events (fallback usage) from last 24h
- Critical issues requiring immediate attention

### Search for a Person

```bash
curl "http://localhost:8000/api/crm/people?q=Name" | jq '.people[0]'
```

### Run a Search Query

```bash
curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "search terms", "top_k": 10}' | jq
```

### Trigger Vault Reindex

```bash
curl -X POST http://localhost:8000/api/admin/reindex
```

### Run Manual Sync

```bash
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --dry-run  # Preview
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --execute --force  # Execute
```

### Debug Sync Issues

```bash
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status
tail -50 logs/lifeos-api-error.log
```

### FDA Sync (Phone/iMessage)

Phone calls, FaceTime, and iMessage require **Full Disk Access** to read CallHistoryDB
and chat.db. The launchd service doesn't have FDA, but Terminal.app does.

**How it works:**
- `run_sync_with_fda.sh` runs via cron at **2:50 AM** (10 min before main sync)
- Opens Terminal.app which has FDA permission
- Runs `run_fda_syncs.py` to sync phone + iMessage with health tracking
- Main sync at 3:00 AM detects these were recently synced and skips them

**Cron entry:**
```
50 2 * * * /Users/nathanramia/Documents/Code/LifeOS/scripts/run_sync_with_fda.sh
```

**Manual run (from Terminal.app):**
```bash
.venv/bin/python scripts/run_fda_syncs.py
```

### Monarch Money (Financial Data)

Monarch Money provides live financial data (accounts, transactions, budgets) via API.
Auth uses a cached session token at `data/monarch_session.pickle`.

**Monthly sync**: Runs on the 1st of each month via `run_all_syncs.py` (phase 5).
Writes `Personal/Finance/Monarch/YYYY-MM.md` to the vault. Use `--force` to run any day.

**Live queries**: 4 API endpoints at `/api/monarch/*` auto-exposed as MCP tools.
Also available to the chat agent via the `search_finances` tool.

**Re-authenticate when token expires** (login fails with 401/525):
```bash
~/.venvs/lifeos/bin/python -c "
import asyncio
from monarchmoney import MonarchMoney
mm = MonarchMoney()
asyncio.run(mm.interactive_login())
mm.save_session('data/monarch_session.pickle')
print('Session saved!')
"
```
Monarch requires MFA — a code will be sent via email/SMS. Sessions last months.

**Env vars** (in `.env`):
- `MONARCH_EMAIL` — Monarch account email
- `MONARCH_PASSWORD` — Monarch account password

**Package**: `monarchmoneycommunity>=1.3.0` (community fork with correct `api.monarch.com` domain).
Import path is still `from monarchmoney import MonarchMoney`.

### Manage Tasks

```bash
# Create a task
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"description": "Review Q4 report", "context": "Work", "tags": ["review"]}' | jq

# List open tasks
curl "http://localhost:8000/api/tasks?status=todo" | jq

# Complete a task
curl -X PUT http://localhost:8000/api/tasks/{id}/complete | jq
```

### Web Search & Compound Queries

The chat supports three query categories:

1. **General knowledge** - Claude answers directly (no data fetch):
   - "What's the capital of France?"
   - "How do I sort a list in Python?"

2. **Web search** - External info via Claude's web_search tool:
   - "What's the weather in NYC?"
   - "When does trash get picked up in 22043?"

3. **Compound queries** - Info gathering + action:
   - "Look up the trash schedule and remind me the night before"
   - "How do I reset AirPods? Add a task to do this later."

---

## Observability & Alerting

### How Alerts Work

| Severity | When Sent | Examples |
|----------|-----------|----------|
| **CRITICAL** | Immediately (rate-limited) | ChromaDB down, embedding model failed, vault inaccessible |
| **WARNING** | Batched nightly (7 AM ET) | Ollama unavailable, backup failed, >5 degradation events |
| **INFO** | Log only | Telegram retry, config defaults used |

**Rate limiting for CRITICAL alerts:**
- Only sent on state transition (healthy → failed), not repeated failures
- 5-minute cooldown between alerts for the same service (handles flapping)

### Alert Configuration

Set in `.env`:
- `LIFEOS_ALERT_EMAIL` - Email address for alerts
- `telegram_bot_token` + `telegram_chat_id` - Telegram backup channel

### Tracked Services

| Service | Severity | Fallback |
|---------|----------|----------|
| `chromadb` | CRITICAL | None (core functionality) |
| `embedding_model` | CRITICAL | None (core functionality) |
| `vault_filesystem` | CRITICAL | None (core functionality) |
| `ollama` | WARNING | Haiku LLM → pattern matching |
| `bm25_index` | WARNING | Vector-only search |
| `google_calendar` | WARNING | Cached data |
| `google_gmail` | WARNING | Cached data |
| `backup_storage` | WARNING | Skips backup |
| `telegram` | INFO | Email-only alerts |

### Degradation Tracking

When a service fails and a fallback is used, this is recorded as a "degradation event".
These are collected and reported in the nightly health check if there are 5+ in 24 hours.

**Note:** Services are tracked on-use, not by polling. Status updates when a service is actually called.







