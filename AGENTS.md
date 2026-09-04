# LifeOS — Agent Reference

> **Audience:** All AI coding agents (Claude Code, Cursor, Copilot, etc.)
> **Status:** Complete
> **Last Updated:** 2026-09-04

LifeOS is a self-hosted personal AI assistant with two halves:

1. **A context layer** — indexes personal data (notes, emails, messages, photos, calendar, contacts, financial data) into a unified semantically-searchable corpus with cross-source entity resolution.
2. **An agentic layer** — an agent worker plus chat/Telegram/MCP/**voice (whisper-relay)** surfaces that autonomously complete multi-step tasks against that context (drafting, scheduling, researching, prepping) and report progress as they work.

Runs on Linux or macOS. Optionally, a Mac can act as an Apple Data Agent for iMessage, phone calls, and contacts.

---

## Key Concepts

- **Two-tier data model**: SourceEntity (raw observations from each data source) → PersonEntity (canonical, merged records per person). See [ADR-003](docs/adr/003-two-tier-data-model.md).
- **Hybrid search**: Vector similarity (ChromaDB) + keyword matching (BM25/FTS5), fused via Reciprocal Rank Fusion. See [ADR-004](docs/adr/004-hybrid-search.md).
- **Entity resolution**: Links emails, phones, and names across sources to canonical people using fuzzy matching with scoring.
- **Sync phases**: Seven-phase nightly pipeline — Collection → Entity Processing → Relationship Building → Indexing → Content Sync → Entity Cleanup → Consistency Verification.
- **Agentic chat**: Orchestrator LLM autonomously calls 15+ tools (search, calendar, email, tasks, etc.) across multiple rounds to answer queries.
- **Agent worker**: Autonomous executor for `#agent`-tagged tasks (or work delegated from chat/Telegram). Runs long, multi-step sessions with the full MCP tool catalog, reports progress through the originating channel, and routes to local Gemma, cloud Claude via Managed Agents, or a configured remote provider (`#cloud`, `#local`, etc. sub-tags; `LIFEOS_AGENT_DEFAULT_ROUTE`). See [specs/product/agent-worker.md](docs/specs/product/agent-worker.md).
- **HTTP client surfaces**: Web chat, Telegram, whisper-relay (voice), and the Hermes gateway (an external front door persona bots can route Telegram turns through) share a stable HTTP contract — see [specs/technical/client-surfaces.md](docs/specs/technical/client-surfaces.md) before changing chat or conversation APIs.

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | FastAPI (port 8000) |
| Vector DB | ChromaDB (port 8001) |
| Keyword Search | SQLite FTS5 (BM25) |
| Intent classifier | Claude Haiku via Anthropic API (default); pattern-matching fallback when the API is unavailable |
| LLM (orchestration + synthesis) | Claude via Anthropic API (default), a local OpenAI-compatible llama-server (`LIFEOS_LLM_BACKEND=local`), or any other OpenAI-compatible remote provider such as Fireworks (`LIFEOS_LLM_BACKEND=remote`, `LIFEOS_REMOTE_LLM_*` — no Anthropic key required; see [ADR-024](docs/adr/024-remote-llm-backend.md)). Base model set by `LIFEOS_ANTHROPIC_MODEL` (defaults to `claude-haiku-4-5`). Per-query escalation (Anthropic backend) sits on top: user-directed ("escalate to opus", "use codex") and automatic on refusal+pushback, via `LIFEOS_AGENT_ESCALATION_MODEL` / `LIFEOS_AGENT_ESCALATION_LADDER`. **Automatic escalation only climbs to non-API engines** (`claude_code`, `codex`, `local`); reaching an API model takes an explicit request from the operator. Specialist calls (relationship insights, fact extraction) fall back Anthropic → local → remote on a keyless install, never erroring — see [ADR-025](docs/adr/025-specialist-call-fallback.md) |
| LLM Client | `api/services/llm_client.py` — unified wrapper with Anthropic↔OpenAI tool format translation |
| Embeddings | sentence-transformers (GPU via ROCm/CUDA) |
| Frontend | Vanilla HTML/JS (no build step) |
| Job Queue | SQLite-backed background workers |
| Scheduler | Markdown source of truth (`LifeOS/Scheduler/Inbox.md`) + rebuildable index cache; 60s cron tick |
| Service Management | systemd (Linux) / launchd (macOS) |

## Documentation Structure

| Category | Path | Purpose |
|----------|------|---------|
| **WHY** (Decisions) | `docs/adr/` | Immutable architecture decision records |
| **WHAT** (Product) | `docs/specs/product/` | Consumer-facing specifications |
| **HOW** (Design) | `docs/specs/technical/` | Engineering specifications |
| **HOW** (Standards) | `docs/specs/standards/` | Coding and testing conventions |
| **HOW** (Operations) | `docs/guides/` | Setup, config, and operational guides |
| **WHEN** (Plans) | `docs/plans/` | Ephemeral roadmap and backlog |
| **WHY** (Vision) | `docs/vision/` | Project philosophy and guiding principles |
| **History** | `docs/archive/` | Superseded documents |

### Navigation — "What question → which doc"

| Question | Document |
|----------|----------|
| How is data modeled? | [specs/product/data-model.md](docs/specs/product/data-model.md) |
| What API endpoints exist? | [specs/product/api-reference.md](docs/specs/product/api-reference.md) |
| What must not break for external chat clients? | [specs/technical/client-surfaces.md](docs/specs/technical/client-surfaces.md) |
| How does the sync pipeline work? | [specs/technical/data-and-sync.md](docs/specs/technical/data-and-sync.md) |
| What does the code structure look like? | [specs/technical/architecture.md](docs/specs/technical/architecture.md) |
| How does hybrid search work internally? | [specs/technical/search-indexing.md](docs/specs/technical/search-indexing.md) |
| How is perf traced and monitored? | [specs/technical/observability.md](docs/specs/technical/observability.md) |
| What does `#agent` do? | [specs/product/agent-worker.md](docs/specs/product/agent-worker.md) |
| How does the agent worker work internally? | [specs/technical/agent-worker.md](docs/specs/technical/agent-worker.md) |
| How does the task store work internally (id-addressed writes, notes body, conflict files)? | [specs/technical/task-management.md](docs/specs/technical/task-management.md) |
| What can I do with tasks (statuses, API, chat)? | [specs/product/task-management.md](docs/specs/product/task-management.md) |
| How do I set up the agent worker? | [guides/agent-worker-setup.md](docs/guides/agent-worker-setup.md) |
| How does the doctor self-repair bot work? | [guides/doctor-bot.md](docs/guides/doctor-bot.md) |
| How do agents hand work to the human? | [guides/human-queue.md](docs/guides/human-queue.md) |
| How do schedules (triggers + actions) work? | [guides/scheduler.md](docs/guides/scheduler.md) |
| How do I import Apple Health/Fitness data? | [guides/apple-health.md](docs/guides/apple-health.md) |
| How do I configure a capture device (e.g. the Pebble Index ring) to feed the journal persona? | [guides/journal-ring-ingest.md](docs/guides/journal-ring-ingest.md) |
| How do I run the Apple Data Agent / re-auth Monarch / check alerting? | [guides/operations.md](docs/guides/operations.md) |
| What does `/agents` show? | [specs/product/agent-viz.md](docs/specs/product/agent-viz.md) |
| How does the `/agents` page work internally? | [specs/technical/agent-viz.md](docs/specs/technical/agent-viz.md) |
| What does the CRM do (overview)? | [specs/product/crm-ui.md](docs/specs/product/crm-ui.md) |
| How does the CRM people view work? | [specs/product/crm-people.md](docs/specs/product/crm-people.md) |
| How does the CRM interaction timeline work? | [specs/product/crm-interactions.md](docs/specs/product/crm-interactions.md) |
| How does the CRM graph work? | [specs/product/crm-graph.md](docs/specs/product/crm-graph.md) |
| How do the CRM dashboards (Family/Me/Birthdays/Relationship) work? | [specs/product/crm-analytics.md](docs/specs/product/crm-analytics.md) |
| How do I set up the project? | [guides/installation.md](docs/guides/installation.md) |
| What scripts are available? | [guides/scripts.md](docs/guides/scripts.md) |
| Why does LifeOS exist? What guides decisions? | [vision/philosophy.md](docs/vision/philosophy.md) |
| Why was X chosen over Y? | `docs/adr/` (specific ADR) |
| How do we review PRs? | `/implement <pr> just review`, `/pr-check`, `/merge-pr` — from the user-scope `implement-lifecycle` plugin |

---

## Development Principles

These apply to all agents. Bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Acting

**Don't assume. Don't hide confusion. Surface tradeoffs.**

- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
- Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For non-trivial features, follow this cycle:

1. Define requirements (what does "done" look like?)
2. Write tests that pass if and only if requirements are met
3. Plan the implementation approach
4. Implement — deliver production code and tests together
5. Adversarial review — compare implementation against requirements; identify gaps, missed edge cases, deviations from spec
6. Close gaps and re-verify

Steps 5–6 repeat until no meaningful gaps remain.

For smaller tasks, a brief inline plan suffices:
1. [Step] → verify: [check]
2. [Step] → verify: [check]

### 5. Tests Are Sacred

- Never skip or weaken tests to make code pass.
- All fixes must consider effects on the full system, not just one test.
- Pre-existing test failures are documented — don't "fix" them without understanding context.

**When an existing test fails after your changes:**

The default assumption is that your code is wrong, not the test. Before modifying any test, answer:
1. What was this test originally meant to verify?
2. Why is it failing — what specific change caused it?
3. Is the correct fix to (a) fix your code, (b) update the test for intentionally changed behavior, or (c) remove the test because the behavior no longer exists?

Option (a) is the default. Options (b) and (c) require explicit justification.

**Never:**
- Delete a failing test to make the suite pass.
- Mark a test as skipped/xfail to unblock a commit.
- Rewrite a test you don't fully understand.
- Assume a flaky test is "just flaky" without investigating.

### 6. Privacy Is Non-Negotiable

- LifeOS handles deeply personal data: emails, messages, photos, finances, therapy notes.
- Never log, expose, or transmit personal data beyond what the system requires.
- All data stays local. LLM inference runs locally — no data leaves the machine.
- Use obviously synthetic data in all documentation and test fixtures.
- Security-sensitive implementation details belong in code, not docs.
- When in doubt about whether something is a privacy concern, treat it as one.

---

## Boundaries

Quick-reference guardrails for all contributors. These complement the Development Principles above with a scannable list.

| Tier | Action |
|------|--------|
| **Always** | Run full test suite before commit (on the server — see § Testing) |
| **Always** | Restart server after **deploying** Python changes (test-only runs in isolated copies don't need it) |
| **Always** | Use `./scripts/server.sh` for server management |
| **Always** | Use obviously synthetic data in tests and docs |
| **Ask first** | Database schema changes (new or altered tables) |
| **Ask first** | Adding new third-party dependencies |
| **Ask first** | Public API changes (new or modified endpoints) |
| **Ask first** | Changes to sync pipeline phases or cron jobs |
| **Ask first** | Changes to MCP tool definitions |
| **Ask first** | Breaking changes to HTTP client contract — see [client-surfaces.md](docs/specs/technical/client-surfaces.md) |
| **Never** | Commit secrets, credentials, or API keys |
| **Never** | Force push to main (sole exception: `merge-pr`'s post-merge timestamp normalization) |
| **Never** | Skip pre-commit hooks (`--no-verify`) |
| **Never** | Log, print, or expose real personal data |
| **Never** | Run uvicorn directly (use `./scripts/server.sh`) |

---

## Development Workflow

**Branch naming:** `<type>/<short-description>` where type is one of: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`. Lowercase, hyphen-separated.

1. **Edit code**
2. **Restart server**: `./scripts/server.sh restart` (or `sudo systemctl restart lifeos-api`)
3. **Test manually** or run tests: `./scripts/test.sh`
4. **Deploy**: `./scripts/deploy.sh "Your commit message"`

`deploy.sh` is the direct-to-main hotfix path. PR-based work (e.g. `/implement`) uses feature branches + `merge-pr` and never calls `deploy.sh`.

---

## Key Files

| File | Purpose |
|------|---------|
| `api/main.py` | FastAPI application entry point |
| `api/services/llm_client.py` | Unified LLM wrapper (OpenAI-compatible API with tool format translation) |
| `api/services/task_manager.py` | Task management service (Obsidian Tasks integration) |
| `api/routes/tasks.py` | Task CRUD API endpoints |
| `config/settings.py` | Environment configuration |
| `config/people_dictionary.json` | Known people and aliases (restart required after edits) |
| `web/` | Vanilla HTML/JS frontend served as static files (`index.html`, `home.html`, `crm.html`; SPA at `/chat`) |
| `README.md` | Architecture overview with diagrams |
| `api/services/perf_trace.py` | Request-level performance tracing (spans, SQLite) |
| `api/routes/perf.py` | Performance trace query API |
| `api/services/agent_worker/` | Autonomous worker for `#agent`-tagged tasks (local Gemma or cloud Claude via Managed Agents) |
| `mcp_server.py` | MCP server — stdio for Claude Code + HTTP transport for Managed Agents (67 tools: 59 from `CURATED_ENDPOINTS` plus 8 `lifeos_agent_*` tools registered separately by `_register_inter_agent_tools()` from `INTER_AGENT_TOOL_SCHEMAS`) |
| `tests/test_perf_benchmark.py` | Benchmark suite for query performance and quality |

| Script | Purpose |
|--------|---------|
| `./scripts/server.sh` | Start/stop/restart/foreground server |
| `./scripts/deploy.sh` | Test → restart → commit → push |
| `./scripts/test.sh` | Run test suites |
| `./scripts/service.sh` | systemd (Linux) / launchd (macOS) service management |
| `./scripts/setup-systemd.sh` | Install systemd units and enable services (Linux, run with sudo) |
| `./scripts/run_sync_wrapper.sh` | Pre-flight checks for nightly sync |
| `./scripts/apple_data_export.py` | Export Apple data (contacts, iMessage, phone) — macOS only |
| `./scripts/apple_data_import.py` | Import Apple data on Linux server |
| `./scripts/apple_data_agent.sh` | macOS cron wrapper: FDA sync → export → rsync to server |

---

## Dependency Management

**Single source of truth**: `requirements.txt`
**Virtual environment**: `~/.venvs/lifeos` (external — see [ADR-005](docs/adr/005-external-venv-macos-tcc.md))

### Adding a new dependency

1. Add to `requirements.txt`
2. Install: `~/.venvs/lifeos/bin/pip install -r requirements.txt`
3. Restart server: `./scripts/server.sh restart`

### Testing

```bash
./scripts/test.sh              # Unit tests (fast, ~30s)
./scripts/test.sh smoke        # Unit + critical browser (used by deploy)
./scripts/test.sh all          # Full test suite
```

- **No local venv (the MacBook)** — `./scripts/test.sh` only runs where a venv exists (the server). From the Mac, run **`./scripts/remote-test.sh`**: it rsyncs your uncommitted working tree to an isolated branch-keyed dir on the test-runner host and runs the same `test.sh auto` scope there. Set `LIFEOS_REMOTE_HOST` to that host's ssh target first (there is no default) — put it in your shell profile so it persists. Get a green run **before** committing — no push required, and committing/pushing "so tests can run" is neither necessary nor allowed.
- **Parallelism:** unit tests run under pytest-xdist (`-n auto --dist loadscope`) — reproduce flakes in that mode, not sequentially.
- **Browser tests:** the web SPA is served at `/chat` (not `/`); `test.sh browser` covers only `test_ui_browser.py`, `test_e2e_flow.py`, and `test_voice_mic_block_ui_browser.py` — run new browser test files directly with pytest.
- **What the pre-push hook skips:** a docs-only push and a deletion-only push skip the suites; everything else runs them in full. The docs-only judgement is `test.sh`'s `decide_plan`, called by the hook rather than reimplemented — so dependency manifests (`requirements*.txt`, `constraints*.txt`) still run tests despite the `.txt` extension. The hook never runs a narrowed subset: a prior failure changes test *order* (`--ff`), never which tests are selected.
- **Server-free browser tests:** most browser tests point at a running `lifeos-api` and carry `requires_server` on top of `browser`. A browser test that serves `web/` itself on an ephemeral port and stubs every `/api/` call (see `tests/test_voice_mic_block_ui_browser.py`) omits that marker, so `browser and not requires_server` selects it. The pre-push hook runs that set alongside `unit and not slow` — it's the only gate that catches a `web/` JS regression before it reaches main, so prefer the self-contained pattern for new frontend tests.
- **Push over HTTPS, not SSH:** `./scripts/setup-hooks.sh` points `origin`'s push URL at HTTPS (fetch stays SSH) because GitHub closes an idle SSH session after ~6 minutes while this ~9-minute gate is still running, which otherwise fails the push even after every test passed. If you push over SSH anyway, `scripts/pre-push` prints a one-line warning naming the HTTPS URL and keeps going.

---

## Server Management — CRITICAL

**NEVER run uvicorn or start the server directly.** Always use the provided scripts:

```bash
./scripts/server.sh start    # Start server (background)
./scripts/server.sh stop     # Stop server
./scripts/server.sh restart  # Restart after code changes
./scripts/server.sh status   # Check if running
```

On Linux, services are managed by systemd (installed via `sudo ./scripts/setup-systemd.sh`):

```bash
sudo systemctl restart lifeos-api        # Restart API server
sudo systemctl status lifeos-api         # Check status
sudo systemctl status lifeos-chromadb    # Check ChromaDB
sudo systemctl status lifeos-llm         # Check local LLM (llama-server)
systemctl list-timers lifeos-*           # Check sync/watchdog timers
```

Always restart the server after modifying Python files. The server does NOT auto-reload.

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
curl http://localhost:8000/health/full | jq          # All endpoints
curl http://localhost:8000/health/services | jq      # External services
```

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
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --dry-run     # Preview
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --execute --force  # Execute
```

### Debug Sync Issues

```bash
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status
tail -50 logs/server.log    # Linux systemd (macOS launchd: logs/lifeos-api-error.log)
```

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

### Google Workspace CLI (`gws`)

`gws` is the Google Workspace CLI — direct, typed access to Drive, Gmail, Sheets, and Calendar via the user's authenticated account. Useful for raw API calls the `lifeos_*` tools don't wrap (creating a Sheet, downloading a Drive file by id). Available to any agent with shell access (Claude Code, Codex).

```bash
gws drive files list --params '{"pageSize": 10}'
gws gmail users messages list --params '{"userId": "me"}'
gws sheets spreadsheets get --params '{"spreadsheetId": "..."}'
gws schema drive.files.list          # discover params for any call
```

Prefer `lifeos_*` tools for search/synthesis; reach for `gws` for direct Google API calls.

---

## Operations Pointers

Operational procedures live in [guides/operations.md](docs/guides/operations.md): the Apple Data Agent (macOS export + FDA wrapper), Monarch Money re-auth, perf-tracing quick commands, and alerting severities. Perf/monitoring design: [specs/technical/observability.md](docs/specs/technical/observability.md).

---

## Common Mistakes to Avoid

1. **Running uvicorn directly** → Use `./scripts/server.sh start`
2. **Forgetting to restart server after code changes** → Use `./scripts/server.sh restart`
3. **Committing without testing** → Use `./scripts/deploy.sh`
4. **Starting server on localhost only** → Must use 0.0.0.0 for Tailscale
5. **Overfitting to specific test cases** → Consider effects on the full system
6. **Breaking external chat clients** → See [client-surfaces.md](docs/specs/technical/client-surfaces.md) before editing chat/conversation routes or web SSE handling

---

## Documentation Rules (Quick Reference)

Full standards in [docs/AGENTS.md](docs/AGENTS.md). Key rules:

- **Product specs** describe WHAT (consumer view). Implementation details go in `specs/technical/`.
- **ADRs are immutable.** To change a decision, create a new ADR that supersedes.
- **Every doc** must have a Related Documents section with bidirectional links.
- **No task lists in specs.** Specs describe target state. Tasks go in `docs/plans/` or GitHub issues.
- **Synthetic data only** in all examples and test fixtures.
- **Completed plans** must be moved to `docs/plans/archive/`.
- **Current behavior only, everywhere.** Docs, code comments, docstrings, and test docstrings describe the system as it is — no "before/now", review rounds, findings, or issue/PR numbers cited as history. Git history holds that narrative.

---

## Related Documents

- [README.md](README.md) — Architecture overview with diagrams
- [docs/AGENTS.md](docs/AGENTS.md) — Documentation strategy and standards
- [CLAUDE.md](CLAUDE.md) — Claude Code-specific configuration
