# LifeOS — Agent Reference

> **Audience:** All AI coding agents (Claude Code, Cursor, Copilot, etc.)
> **Status:** Complete
> **Last Updated:** 2026-11-01

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
- **Agent worker**: Autonomous executor for engine-assigned tasks (or work delegated from chat/Telegram). Runs long, multi-step sessions with the full MCP tool catalog, reports progress through the originating channel, and routes to local Gemma, cloud Claude via Managed Agents, or a configured remote provider (`#cloud`, `#local`, etc. sub-tags; `LIFEOS_AGENT_DEFAULT_ROUTE`). See [specs/product/agent-worker.md](docs/specs/product/agent-worker.md).
- **HTTP client surfaces**: Web chat, Telegram, whisper-relay (voice), and the Hermes gateway (an external front door persona bots can route Telegram turns through) share a stable HTTP contract — see [specs/technical/client-surfaces.md](docs/specs/technical/client-surfaces.md) before changing chat or conversation APIs.

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | FastAPI (port 8000) |
| Vector DB | ChromaDB (port 8001) |
| Keyword Search | SQLite FTS5 (BM25) |
| Intent classifier | Claude Haiku via Anthropic API (default); pattern-matching fallback when the API is unavailable |
| LLM (orchestration + synthesis) | Claude via Anthropic API (default), a local OpenAI-compatible llama-server (`LIFEOS_LLM_BACKEND=local`), or any other OpenAI-compatible remote provider such as Fireworks (`LIFEOS_LLM_BACKEND=remote`, `LIFEOS_REMOTE_LLM_*` — no Anthropic key required; see [ADR-024](docs/adr/024-remote-llm-backend.md)). Base model set by `LIFEOS_ANTHROPIC_MODEL` (defaults to `haiku` — a family name resolved to the newest Claude Haiku at request time; a full model id pins a release). Per-query escalation (Anthropic backend) sits on top: user-directed ("escalate to opus", "use codex") and automatic on refusal+pushback, via `LIFEOS_AGENT_ESCALATION_MODEL` / `LIFEOS_AGENT_ESCALATION_LADDER`. **Automatic escalation only climbs to non-API engines** (`claude_code`, `codex`, `local`); reaching an API model takes an explicit request from the operator. Specialist calls (relationship insights, fact extraction) fall back Anthropic → local → remote on a keyless install, never erroring — see [ADR-025](docs/adr/025-specialist-call-fallback.md) |
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
| How do names and first-name note mentions become people? | [specs/product/entity-resolution.md](docs/specs/product/entity-resolution.md) |
| What does the code structure look like? | [specs/technical/architecture.md](docs/specs/technical/architecture.md) |
| How does hybrid search work internally? | [specs/technical/search-indexing.md](docs/specs/technical/search-indexing.md) |
| How is perf traced and monitored? | [specs/technical/observability.md](docs/specs/technical/observability.md) |
| What does the agent worker do? | [specs/product/agent-worker.md](docs/specs/product/agent-worker.md) |
| How does the agent worker work internally? | [specs/technical/agent-worker.md](docs/specs/technical/agent-worker.md) |
| How does the task store work internally (id-addressed writes, notes body, conflict files)? | [specs/technical/task-management.md](docs/specs/technical/task-management.md) |
| What can I do with tasks (statuses, API, chat)? | [specs/product/task-management.md](docs/specs/product/task-management.md) |
| How do I set up the agent worker? | [guides/agent-worker-setup.md](docs/guides/agent-worker-setup.md) |
| How does the doctor self-repair bot work? | [guides/doctor-bot.md](docs/guides/doctor-bot.md) |
| How do Claude/ChatGPT apps authenticate to MCP, and which tools can they use? | [specs/technical/mcp-oauth.md](docs/specs/technical/mcp-oauth.md) |
| How do I connect the Claude or ChatGPT app to LifeOS? | [guides/mcp-connected-apps.md](docs/guides/mcp-connected-apps.md) |
| What file, command and log access can an MCP client get on the host? | [guides/host-tools.md](docs/guides/host-tools.md) |
| How do agents hand work to the human? | [guides/human-queue.md](docs/guides/human-queue.md) |
| How do I pause/resume household internet access via eero? | [guides/home-eero.md](docs/guides/home-eero.md) |
| How do schedules (triggers + actions) work? | [guides/scheduler.md](docs/guides/scheduler.md) |
| How do I import Apple Health/Fitness data? | [guides/apple-health.md](docs/guides/apple-health.md) |
| How does the day-context job feed Dailies suggestions to Home? | [guides/day-context.md](docs/guides/day-context.md) |
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
| How do I land a PR? | [§ Development Workflow](#development-workflow) |
| What lifecycle applies to a change? | [Development lifecycle standard](docs/specs/standards/development-lifecycle.md) |

---

## Development Principles

Bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Acting

State assumptions. If a request is unclear or has several readings, say so and ask rather than pick silently. Push back when a simpler approach exists.

### 2. Simplicity First

Write the minimum code that solves the problem: no speculative features, single-use abstractions, unrequested configurability, or handling for impossible cases.

### 3. Surgical Changes

Touch only what the task needs and match the existing style. Mention unrelated dead code rather than deleting it, and remove only what your own change made unused.

### 4. Goal-Driven Execution

Turn the task into a checkable goal, write tests that pass only when it is met, and loop until verified.

### 5. Tests Are Sacred

Never skip, weaken, or delete a test to make code pass, and never assume a flaky test is "just flaky" without investigating. When an existing test fails after your change, assume your code is wrong. Change the test only for intentionally changed behavior, and say why. Consider the effects of a fix on the full system, not just one test.

### 6. Privacy Is Non-Negotiable

- LifeOS handles deeply personal data: emails, messages, photos, finances, therapy notes.
- Never log, expose, or transmit personal data beyond what the system requires.
- LLM inference runs locally by default and on a keyless install; a remote or cloud provider is used only when the operator explicitly configures one (`LIFEOS_REMOTE_LLM_*`, `LIFEOS_LLM_BACKEND`, or an Anthropic key) — see [ADR-024](docs/adr/024-remote-llm-backend.md) and, for Pebble specifically, [guides/pebble-capture.md](docs/guides/pebble-capture.md). Sending data off-box is always an explicit operator choice, never a silent default.
- Use obviously synthetic data in all documentation and test fixtures.
- Security-sensitive implementation details belong in code, not docs.
- When in doubt about whether something is a privacy concern, treat it as one.

---

## Boundaries

| Tier | Action |
|------|--------|
| **Always** | Run focused tests for what you changed; the hosted gate runs the retained lanes |
| **Always** | Restart the server after **deploying** Python changes, with `./scripts/server.sh` (isolated test runs never restart production) |
| **Always** | Use obviously synthetic data in tests and docs |
| **Ask first** | Database schema changes (new or altered tables) |
| **Ask first** | Adding new third-party dependencies |
| **Ask first** | Public API changes (new or modified endpoints) |
| **Ask first** | Changes to sync pipeline phases or cron jobs |
| **Ask first** | Changes to MCP tool definitions |
| **Ask first** | Breaking changes to HTTP client contract — see [client-surfaces.md](docs/specs/technical/client-surfaces.md) |
| **Never** | Commit secrets, credentials, or API keys |
| **Never** | Force push to main |
| **Never** | Skip pre-commit hooks (`--no-verify`) |
| **Never** | Log, print, or expose real personal data |
| **Never** | Run uvicorn directly (use `./scripts/server.sh`) |

---

## Development Workflow

**Branch naming:** `<type>/<short-description>` where type is one of: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`. Lowercase, hyphen-separated.

1. **Build** on a branch off `origin/main` in its own worktree, with `/implement` for coding work and `/draft-issue` to file work. Both are user-scope skills, and so is the `wt-sweep --apply` command that removes merged worktrees.
2. **Test** with focused runs (`pytest <files>`, `./scripts/test.sh`). The [Development lifecycle](docs/specs/standards/development-lifecycle.md) sets what review and evidence a change's risk requires.
3. **Open a PR.** It gets a full advisory run, `candidate-verification-shadow`.
4. **Land** with `./scripts/merge-pr.sh <pr>`, from any checkout, on the MacBook or the Linux server. `main` requires a check that is only published on a candidate merge commit, so `gh pr merge` and `ship` cannot land here, and admin enforcement rules out `--admin`.
   - `REJECTED` with a lease reason means `main` or the branch moved during verification. Run it again.
   - Never cancel a candidate verification run. Its publisher records a failed check on that exact candidate.
5. **Deploy:** a host with autodeploy enabled pulls `main` and restarts within about 10 minutes. See [guides/operations.md](docs/guides/operations.md).

---

## Key Files

| File | Purpose |
|------|---------|
| `api/main.py` | FastAPI application entry point |
| `api/services/llm_client.py` | Unified LLM wrapper (OpenAI-compatible API with tool format translation) |
| `api/services/task_manager.py` | Task management service (Obsidian Tasks integration) |
| `api/routes/tasks.py` | Task CRUD API endpoints |
| `config/settings.py` | Environment configuration |
| `config/people_dictionary.json` | Known people and aliases (deployed server restart required after edits) |
| `web/` | Vanilla HTML/JS frontend served as static files (`index.html`, `home.html`, `crm.html`; SPA at `/chat`) |
| `README.md` | Architecture overview with diagrams |
| `api/services/perf_trace.py` | Request-level performance tracing (spans, SQLite) |
| `api/routes/perf.py` | Performance trace query API |
| `api/services/agent_worker/` | Autonomous worker for engine-assigned tasks (local Gemma or cloud Claude via Managed Agents) |
| `mcp_server.py` | MCP server — stdio for Claude Code + HTTP transport for Managed Agents (84 tools: 73 from `CURATED_ENDPOINTS` plus 11 `lifeos_agent_*` tools registered separately by `_register_inter_agent_tools()` from `INTER_AGENT_TOOL_SCHEMAS`; plus the 5 host tools from `api/services/host_tools.py` when `LIFEOS_HOST_TOOLS_ENABLED` is set) |
| `tests/test_perf_benchmark.py` | Benchmark suite for query performance and quality |

Scripts are in [guides/scripts.md](docs/guides/scripts.md). The ones you need most: `./scripts/merge-pr.sh`, `./scripts/server.sh`, `./scripts/test.sh`, `./scripts/remote-test.sh`.

---

## Dependency Management

`requirements.txt` is the single source of truth. The virtual environment is `~/.venvs/lifeos`, outside the repo (see [ADR-005](docs/adr/005-external-venv-macos-tcc.md)). To add a dependency, add it to `requirements.txt`, then run `~/.venvs/lifeos/bin/pip install -r requirements.txt`.

### Testing

```bash
./scripts/test.sh              # Unit tests (fast-unit lane, serial by default)
./scripts/test.sh smoke        # Unit + critical browser
./scripts/test.sh all          # Full test suite
```

- **No local venv (the MacBook):** run `./scripts/remote-test.sh`, which rsyncs your uncommitted tree to an isolated directory on the host named by `LIFEOS_REMOTE_HOST` and runs the same `test.sh auto` scope there. There is no need to commit or push so tests can run. Don't run `./scripts/setup-hooks.sh` on a checkout with no venv: ordinary code pushes would then need pytest.
- **Parallelism:** `LIFEOS_TEST_PARALLEL_WORKERS=<n>` adds `-n <n> --dist loadscope`. The default is serial.
- **A `browser`-marked file co-scheduled with an unrelated `async def` test can fail nondeterministically.** pytest-playwright's sync-API driver parks an event loop that can break a later async test in the same process, and under `--dist loadscope` which scopes share a worker shifts with the selection. Don't bisect by shuffling files under xdist. Run them in one process, browser file first: `pytest tests/some_x_ui_browser.py tests/the_suspect_test.py -q`. `tests/conftest.py` has the mechanism and reproducers.
- **Browser tests:** the web SPA is served at `/chat`, not `/`. `scripts/test_lane_registry.py` defines lane membership from markers: `browser` alone is `browser-free`, `browser` with `requires_server` is `browser-server`.
- **Pre-push hook:** an ordinary push runs Ruff on changed Python files, a pytest collection check, and the fixture privacy audit. Docs-only pushes, deletions, and the candidate publisher's own pushes skip them. A failure shows the end of the log it writes under `${TMPDIR:-/tmp}/lifeos-prepush/`.
- **The merge gate is hosted:** the App-bound required check `candidate-verification` is what blocks a bad merge. It runs `fast-unit`, plus `browser-free` when a `web/` file changed, and nothing for a docs-only candidate, across a matrix of parts. It never runs `browser-server`, so run `./scripts/test.sh browser` when you change server-backed UI. See [candidate-verification-ci.md](docs/guides/candidate-verification-ci.md), including its rollback to blocking local verification.
- **Push over HTTPS:** `./scripts/setup-hooks.sh` points `origin`'s push URL at HTTPS, because GitHub closes an idle SSH session after about 6 minutes.

---

## Server Management — CRITICAL

**NEVER run uvicorn or start the server directly.** A direct uvicorn often binds only to localhost while the script binds `0.0.0.0:8000`, which leaves two servers answering on different interfaces.

```bash
./scripts/server.sh start    # Start server (background)
./scripts/server.sh stop     # Stop server
./scripts/server.sh restart  # Restart after deploying code changes (never for isolated test runs)
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

---

## Common Tasks

```bash
curl http://localhost:8000/health/full | jq                        # Health of all endpoints
curl http://localhost:8000/health/services | jq                    # External services
curl -X POST http://localhost:8000/api/admin/reindex               # Vault reindex
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --dry-run      # Preview a manual sync
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status       # Sync status
tail -50 logs/server.log                                           # Linux systemd (macOS launchd: logs/lifeos-api-error.log)
```

`gws` is the Google Workspace CLI, for raw Drive, Gmail, Sheets, and Calendar calls the `lifeos_*` tools don't wrap (`gws schema drive.files.list` shows the parameters of any call). Prefer `lifeos_*` tools for search and synthesis.

Operational procedures live in [guides/operations.md](docs/guides/operations.md): the Apple Data Agent, Monarch Money re-auth, perf tracing, and alerting severities. Perf and monitoring design: [specs/technical/observability.md](docs/specs/technical/observability.md).

---

## Documentation Rules (Quick Reference)

Full standards in [docs/AGENTS.md](docs/AGENTS.md). After changing docs, check them against it. Key rules:

- **Product specs** describe WHAT (consumer view). Implementation details go in `specs/technical/`.
- **ADRs are immutable.** To change a decision, create a new ADR that supersedes.
- **Every doc** must have a Related Documents section.
- **No task lists in specs, and no `backlog.md`.** Specs describe target state; tasks go in GitHub issues. Plan files in `docs/plans/` are for one in-flight effort, and move to `docs/plans/archive/` when done.
- **Split docs by concern.** Target line counts are in `docs/AGENTS.md`. Write what helps the next reader, not a note on every routine change.
- **Synthetic data only** in all examples and test fixtures.
- **Current behavior only, everywhere.** Docs, code comments, docstrings, and test docstrings describe the system as it is — no temporal before/after comparisons, no review-round or reviewer-finding narration, and no issue/PR numbers cited as history. Git history holds that narrative.
- **PR descriptions are short summaries.** No verification matrix, evidence section, or risk table required for work outside the named risk triggers — see [Development Lifecycle](docs/specs/standards/development-lifecycle.md#risk-and-review).

Repo skills in `.claude/skills/` are LifeOS-specific health checks, housekeeping, and reporting. When creating a new directory or module, check if an AGENTS.md is appropriate.

---

## Related Documents

- [README.md](README.md) — Architecture overview with diagrams
- [docs/AGENTS.md](docs/AGENTS.md) — Documentation strategy and standards
- [Development lifecycle](docs/specs/standards/development-lifecycle.md) — Shared risk, review, and verification contract
