@AGENTS.md

# Claude Code Configuration

## Workflow

- Use plan mode for non-trivial tasks (3+ files, architectural decisions, unclear requirements).
- After modifying docs, verify compliance with [docs/AGENTS.md](docs/AGENTS.md) standards.
- After modifying code, restart server on Mac Mini before testing.

## Remote Development

LifeOS runs on a **Mac Mini** (server at `100.95.233.70` via Tailscale). Code is edited on a **MacBook Pro** — the filesystem is synced, so edits are visible on both machines immediately.

| Task | Where | Command |
|------|-------|---------|
| Edit code | MacBook (local) | Normal file editing |
| Run tests | Mac Mini (SSH) | `ssh nathanramia@100.95.233.70 "cd ~/Documents/Code/LifeOS && ./scripts/test.sh"` |
| Restart server | Mac Mini (SSH) | `ssh nathanramia@100.95.233.70 "cd ~/Documents/Code/LifeOS && ./scripts/server.sh restart"` |
| Install deps | Mac Mini (SSH) | `ssh nathanramia@100.95.233.70 "~/.venvs/lifeos/bin/pip install -r ~/Documents/Code/LifeOS/requirements.txt"` |
| Git operations | MacBook (local) | Normal git commands |

## Quick Reference

**Read these first:**
- [AGENTS.md](AGENTS.md) — Full project reference (principles, key files, common tasks)
- [docs/AGENTS.md](docs/AGENTS.md) — Documentation standards

**Key commands:**
```bash
./scripts/server.sh restart           # After code changes
./scripts/test.sh                     # Run unit tests
./scripts/deploy.sh "message"         # Test → restart → commit → push
curl http://localhost:8000/health/full | jq   # Health check
```

## Critical Invariants

- **Never run uvicorn directly** — always use `./scripts/server.sh`
- **Never create a venv locally** — it only exists on the Mac Mini at `~/.venvs/lifeos`
- **Never run pytest locally** — dependencies aren't installed on the MacBook
- **Always restart server after Python changes** — no auto-reload
- **SSH uses Tailscale IP** — `100.95.233.70`, not `.local` hostnames

## Common Mistakes

1. **Putting implementation details in product specs** → Product specs describe WHAT (consumer view). Implementation goes in `specs/technical/`.
2. **Adding "Next Steps" or task lists to specs** → Specs describe target state. Tasks go in `plans/` or GitHub issues.
3. **Modifying an ADR** → ADRs are immutable. Create a new one that supersedes.
4. **Missing Related Documents section** → Every doc must have one, with bidirectional links.
5. **Using real personal data in examples** → Always use obviously synthetic data.
6. **Creating monolithic docs** → Split by concern. Target line counts are in `docs/AGENTS.md`.
7. **Leaving stale plans in `docs/plans/`** → Completed or superseded plans must be moved to `docs/plans/archive/`.
8. **Over-documenting routine changes** → Not every change needs a docs update. Write what helps the next reader understand current state.
9. **Deleting or modifying a failing test to unblock a commit** → See AGENTS.md § "Tests Are Sacred" for the full decision framework.
10. **Committing without running the test suite** → Run tests on Mac Mini before every commit. No exceptions.
