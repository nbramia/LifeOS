# Testing Standards

**Status:** Complete
**Last Updated:** 2026-09-09
**Audience:** All developers and AI agents

Testing patterns and conventions for the LifeOS codebase.

These are rules, not suggestions. PRs that violate testing standards should be rejected.

---

## Test Organization

All tests live in the `tests/` directory. Files follow the `test_<module>.py` naming convention. There is no nested directory structure -- all test files are at the top level (except `tests/e2e/`, `tests/archive/`, and `tests/fixtures/`).

| Pattern | Example |
|---------|---------|
| Service tests | `test_task_manager.py` |
| API route tests | `test_tasks_api.py` |
| E2E / browser tests | `test_e2e_flow.py`, `test_ui_browser.py` |
| Benchmark tests | `test_perf_benchmark.py` |

## Test Levels

| Level | Command | What it runs |
|-------|---------|-------------|
| Unit | `./scripts/test.sh` | Fast lane: tests without browser, server, integration, or slow markers |
| Smoke | `./scripts/test.sh smoke` | Unit + critical browser test (used by deploy) |
| All | `./scripts/test.sh all` | Every non-archived test, once, after verifying the lane inventory is complete and disjoint |
| Health | `./scripts/test.sh health` | Quick server health check |

`scripts/test_lane_registry.py` is the single test-lane registry;
`scripts/test-lanes.sh` is its shell runner and inventory command. It assigns
each test to exactly one execution lane using marker precedence, so `all`,
pre-push, and future CI collect the same IDs:

| Lane | Marker selection | Prerequisite |
|------|------------------|--------------|
| `fast-unit` | `unit`, excluding `browser`, `requires_server`, `integration`, and `slow` | none |
| `slow` | `slow`, excluding browser/server/integration | isolated heavy dependencies as needed |
| `browser-free` | `browser` without `requires_server` | Playwright |
| `browser-server` | `browser` with `requires_server` | Playwright and a running API server |
| `server` | `requires_server`, excluding browser | running API server |
| `integration` | `integration`, excluding browser and `requires_server` | none |

Browser, server, and integration lanes each run in separate processes. This
prevents pytest-playwright's sync event loop from being co-scheduled with
unrelated async tests. `./scripts/test.sh all` collects the full non-archived
inventory and every lane first; it fails before execution if any test is
omitted or appears in more than one lane.

`./scripts/test-lanes.sh inventory [test-path ...]` runs one collection and
prints a JSON receipt with every lane's IDs/count, marker expression,
prerequisites, and intentional exclusions. `prepush-parity` uses the same
single collection to compare the current fast-unit plus server-free-browser
gate against the former `unit and not slow` selection.

`./scripts/test.sh auto` is an iteration planner, not a shortcut around
markers. A changed test file is collected against every lane and each lane
that has selected IDs runs. A present test file that selects zero IDs fails;
deleted test paths and unknown test infrastructure conservatively fall back to
the fast-unit lane. Docs-only diffs are the intentional no-test case.

## Lifecycle Verification Contract

The lifecycle contract in
[development-lifecycle.md](development-lifecycle.md) is authoritative for
which evidence is current and which lanes a change requires. Before commit,
obtain current evidence for every selected lane; a focused iteration command
does not replace a required broad gate. Broad gates remain blocking until the
repository records an approved evidence-reuse policy, and a valid reusable
receipt may be consumed only for its exact source candidate and lane.

Lifecycle implementers, reviewers, documentation checks, and pull-request
standards checks run focused checks only. One verification phase owns the
authoritative broad command or ordered command plan and records its result once
for the exact source candidate; merge readiness consumes that record rather
than rerunning the broad plan. Infrastructure retries require an explicit
reason and preserve the original result.

Production restarts belong to deployment. Tests that use an isolated source,
temporary data, or a server-free fixture never restart or stop a production
server; server-dependent tests must use the runner's owned instance contract.

### Development metrics

[`scripts/development_metrics.py`](../../../scripts/development_metrics.py) is
the one shared local recorder for lifecycle phase timing and evidence-reuse
observations; a caller records a phase through its `record` subcommand
(candidate-verification CLIs and canonical lifecycle plugin adapters alike)
rather than hand-rolling a second JSONL format or timing implementation, and
reads `summary` for aggregate durations, percentile, and per-phase status
(`unknown` for a phase never observed — never imputed from commit or
wall-clock timestamps). Records are private and contain no timestamps, paths,
or environment values; only an opaque run/candidate/task id and the
caller-supplied evidence reference identify a phase. `scripts/remote-test.sh`
records the transfer/execution phases of a remote run this way, gated on
`METRICS_CANDIDATE` and writing to `LIFEOS_DEV_METRICS_PATH` (default
`~/.cache/lifeos/remote-test-metrics/metrics.jsonl`); it is best-effort only
and never changes the script's real exit status.

`scripts/remote-test.sh` never rsyncs `.git` itself — a linked worktree's
`.git` is a file pointing at the source host's administrative directory,
which does not exist on the execution host, and even an ordinary checkout's
`.git` carries credentials/hooks/config the transfer must not export.
`scripts/_remote_git_bundle.py` resolves HEAD and (if present) `origin/main`
locally via plain git commands and bundles just that history; the execution
host materializes a fresh, self-contained repository from the bundle (a
throwaway `LifeOS Remote Test` identity, no source credentials) before the
working-tree content — including uncommitted/untracked state — is rsynced
on top, so `git status`/`auto`'s diff match the source exactly.

## Remote Testing Workflow

Tests run on the server, not locally on the development machine. The virtual environment only exists on the server.

```bash
# Run unit tests
ssh <user>@<server-ip> "cd ~/Code/LifeOS && ./scripts/test.sh"

# Run a specific test file
ssh <user>@<server-ip> "cd ~/Code/LifeOS && \
  ~/.venvs/lifeos/bin/python -m pytest tests/test_task_manager.py -v --tb=short"

# Run smoke tests (unit + critical browser)
ssh <user>@<server-ip> "cd ~/Code/LifeOS && ./scripts/test.sh smoke"
```

## Test Naming

- Test functions: `test_<description>` with descriptive names (e.g., `test_create_basic_task`, `test_complete_task_not_found`).
- Test classes: `Test<Feature>` grouping related tests (e.g., `TestCreate`, `TestStatusTransitions`, `TestTasksAPI`).
- Docstrings on every test function describing what is being verified.

```python
class TestCreate:
    """Tests for create method."""

    def test_create_basic_task(self, task_manager):
        """Test creating a basic task."""
        task = task_manager.create("Test task")
        assert task.id
        assert task.status == "todo"
```

## Markers

Custom markers are registered in `conftest.py`:

| Marker | Purpose |
|--------|---------|
| `@pytest.mark.unit` | Fast unit tests |
| `@pytest.mark.slow` | Tests needing ChromaDB, embeddings, or file watchers |
| `@pytest.mark.integration` | Cross-service tests; add `requires_server` only when they need the API server |
| `@pytest.mark.browser` | Playwright browser tests |
| `@pytest.mark.requires_server` | Tests requiring the API server |
| `@pytest.mark.requires_db` | Tests needing direct database access |

Apply `pytestmark = pytest.mark.unit` at the module level for unit test files.

### Browser tests and `requires_server`

`browser` and `requires_server` are independent. A browser test that points at a running `lifeos-api` carries both; one that serves `web/` itself on an ephemeral port and intercepts every `/api/` call carries only `browser`.

That distinction is load-bearing: the `browser-free` lane is the set the
pre-push hook runs, so it is the only gate that catches a `web/` JS regression
before it reaches `main`. Pushes must not depend on a shared server that other
agents restart, so a browser test that needs one is excluded there and runs
under `./scripts/test.sh browser` instead. Playwright is a required
pre-push prerequisite: when it is absent, the hook fails with its install
command rather than reporting a green check without browser coverage.

Prefer the self-contained pattern for new frontend tests — it also means the test exercises the checkout under test rather than whatever a running server has deployed.

The pre-push hook writes each suite's output to a per-branch, per-process log under `${TMPDIR:-/tmp}/lifeos-prepush/` and prints the resolved path before each run, so concurrent pushes from different worktrees never overwrite each other's log; logs past 4 days old are pruned automatically (`find -mtime +3` starts deleting at the 4-day mark, not 3). A branch name with non-ASCII characters or longer than 80 characters collapses to a less distinguishable slug in the filename, but the log path still stays unique (it's the process id, not the slug, that guarantees that). If the configured directory is unusable, the hook degrades instead of blocking the push: it tries a fixed fallback path, then a throwaway `lifeos-prepush.XXXXXX` directory, then (only if that `mktemp` call itself fails) a bare `mktemp -d`, which lands in a `tmp.XXXXXXXXXX`-style directory, then (as a true last resort) `/tmp` directly — printing which it used and why. Only a directory the hook actually created/manages this way is chmod'd to 0700 or pruned; any directory it doesn't own this way — the last resort, or a symlinked directory at any rung — still receives the log but is never tightened or swept, and gets a `lifeos-prepush-` filename prefix on its logs so they stay attributable.

Because the pre-push hook always runs the unit and browser suites as separate invocations, a bug that only shows up when a `browser`-marked file and an unrelated test are *co-scheduled in one invocation* can appear or vanish with the file set under `--dist loadscope` — see AGENTS.md § Testing for the mechanism and how to bisect it deterministically.

## Fixture Patterns

Shared fixtures are in `tests/conftest.py`. Key fixtures:

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `test_vault_path` | session | Temporary vault directory with standard folders |
| `test_data_path` | session | Temporary data directory for ChromaDB/SQLite |
| `mock_settings` | function | Patched `config.settings.settings` with temp paths |
| `server_available` | session | Checks if API server is running |
| `reset_singletons_after_test` | function (autouse) | Resets service singletons to prevent test pollution |

Test-specific fixtures use `tmp_path` for isolated file system state:

```python
@pytest.fixture
def task_manager(tmp_vault, tmp_index):
    """Create a TaskManager with temporary paths."""
    return TaskManager(vault_path=tmp_vault, index_path=tmp_index)
```

## Mocking Patterns

API route tests mock the service singleton via `unittest.mock.patch`:

```python
@pytest.fixture
def mock_task_manager(self):
    with patch("api.routes.tasks.get_task_manager") as mock:
        manager = mock.return_value
        manager.create.return_value = sample_task
        manager.get.return_value = sample_task
        yield manager
```

Route tests use FastAPI's `TestClient`:

```python
@pytest.fixture
def client(self):
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)
```

Service tests use real objects with temporary paths -- no mocking of the service under test.

## Pre-existing Test Failures

These tests fail on clean `main` and are NOT caused by new changes:

- `test_mcp_server.py::TestAPIOpenAPISync::test_openapi_endpoints_match_curated` -- curated endpoint path format mismatch
- `test_p91_data_integrity.py` -- stale person IDs in interactions database

Do not spend time fixing these unless explicitly asked.

## When Tests Fail After Your Changes

The default assumption is that your code is wrong, not the test. Before modifying any test, answer all three:

1. **What was this test originally meant to verify?** Read the test name, docstring, and assertions carefully.
2. **Why is it failing now — what specific change caused the failure?** Trace the failure to your diff.
3. **Is the correct fix to (a) fix your code, (b) update the test to match intentionally changed behavior, or (c) remove the test because the behavior no longer exists?**

Option (a) is the default. Options (b) and (c) require explicit justification in the commit message.

**Never:**
- Delete a failing test to make the suite pass.
- Mark a test as skipped/xfail to unblock a commit.
- Rewrite a test you don't fully understand.
- Weaken assertions (e.g., changing `assertEqual` to `assertIn`) without justification.

## HTTP client contract tests

When changing chat or conversation APIs, run the tests in [Client Surfaces](../technical/client-surfaces.md#before-changing-chat-or-conversation-apis) and update them if shapes change.

| Test file | Endpoints |
|-----------|-----------|
| `tests/test_chat_api.py` | ask/stream, handoff |
| `tests/test_conversations_api.py` | conversation list/detail |
| `tests/test_agent_proxy.py` | agent/ask/stream, agent/status |
| `tests/test_hermes_proxy.py` | hermes/ask/stream, hermes/status, `lifeos_context` envelope, turn persistence |
| `tests/test_voice_proxy.py` | voice/turn/stream and related voice proxy routes |

## Benchmark Tests

`test_perf_benchmark.py` runs queries against a **live server**, collects perf traces, and validates answer quality. It is not part of the unit test suite.

```bash
ssh <user>@<server-ip> "cd ~/Code/LifeOS && \
  ~/.venvs/lifeos/bin/python -m pytest tests/test_perf_benchmark.py -v -s"
```

Test queries and expected results are defined in `BENCHMARK_QUERIES` within the test file. Personal data can be overridden via `tests/fixtures/benchmark_config.json` (gitignored).

## Golden/Snapshot Fixtures

Several modules cache a config-derived value in a module-level constant at
import time (e.g. `agent_system_prompt._STATIC_PROMPT`, which interpolates
`settings.user_name` once, on first import, and is never recomputed). A
golden/snapshot fixture that captures one of these must pin every
config-derived input it depends on to an explicit, synthetic value chosen
before capture -- never whatever a live machine's real `.env` happens to
contain. Two enforced reasons:

1. **Determinism across test ordering.** Because the constant is cached at
   first import, whichever test triggers that import first (an accident of
   `pytest -n N --dist loadscope` work distribution, not something a test
   controls) decides the value for the rest of that worker process. Pinning
   removes the dependency on import order entirely.
2. **Privacy.** This is an open-source repo; a fixture captured against a
   real `.env` bakes the operator's real personal data into a committed
   file. `tests/test_fixtures_no_personal_data.py` scans committed fixtures
   for known identity-sensitive settings values whenever a real `.env` is
   actually reachable, but is a backstop, not a substitute for pinning at
   capture time.

`tests/fixtures/agent_system_prompt_golden_591.py` is the reference example:
its module docstring documents exactly which inputs are pinned, why each one
matters, and the recapture recipe (pin env vars before importing anything
from this repo, then capture against the pre-change code). Follow that
pattern for any new golden/snapshot fixture. See #598 for the underlying
defect this guards against -- `api/main.py`'s `load_dotenv()` now loads an
explicit repo-root path rather than searching upward, which is the primary
fix, but pinning at capture time remains the standard for any fixture that
touches config-derived output.

## Singleton Reset

The `reset_singletons_after_test` autouse fixture (in `conftest.py`) calls `tests/reset_singletons.py` after every test to clear cached service instances. This prevents state leakage between tests. ML singletons (embedding model) are only reset once at session end to avoid expensive reloads.

## Coverage

There is no enforced coverage threshold. The project relies on targeted tests for each service and route rather than coverage metrics.

---

## Related Documents

- [specs/technical/architecture.md](../technical/architecture.md) -- system architecture and code structure
- [Client Surfaces](../technical/client-surfaces.md) -- HTTP consumers and breaking-change policy
- [AGENTS.md](../../../AGENTS.md) -- development workflow and agent instructions
- [Python Conventions](python-conventions.md) -- coding style and module patterns
- [Development Lifecycle](development-lifecycle.md) -- risk-based implementation, review, and verification contract
