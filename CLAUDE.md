@AGENTS.md

# Claude Code Configuration

## Workflow

- Use plan mode for non-trivial tasks (3+ files, architectural decisions, unclear requirements).
- After modifying docs, verify compliance with [docs/AGENTS.md](docs/AGENTS.md) standards.
- After modifying code, restart server before testing.
- When creating new directories or modules, check if an AGENTS.md + CLAUDE.md pair is appropriate.

## Skills

Available as slash commands. See `.claude/skills/` for full details.

| Skill | Purpose |
|-------|---------|
| `/implement <task>` | Full lifecycle: plan → implement → PR → adversarial review → merge |
| `/review-pr <number>` | Adversarial PR review with specialist subagents |
| `/address-review <number>` | Address PR review feedback with independent verification |
| `/pr-check [number]` | Validate PR against standards before requesting review |
| `/merge-pr <number>` | Merge PR, update linked issues |
| `/tune <feedback>` | Tune orchestrator prompts based on bad response feedback |
| `/draft-issue <description>` | Create a GitHub issue from a description or investigation |
| `/standup [hours]` | Personal daily summary — shipped, in progress, needs attention |
| `/catchup [period]` | Cross-PR net-effects briefing for a time period |
| `/stale` | Find stale PRs, orphan branches, stale issues |
| `/mine-for-ideas <repo>` | Analyze an open-source repo for patterns LifeOS could adopt |

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
- **Always restart server after Python changes** — no auto-reload
- **Venv lives on the server** at `~/.venvs/lifeos` — don't create one elsewhere

## Common Mistakes

1. **Putting implementation details in product specs** → Product specs describe WHAT (consumer view). Implementation goes in `specs/technical/`.
2. **Adding "Next Steps" or task lists to specs** → Specs describe target state. Backlog items go in **GitHub issues**; time-bounded execution notes go in `plans/`.
3. **Modifying an ADR** → ADRs are immutable. Create a new one that supersedes.
4. **Missing Related Documents section** → Every doc must have one, with bidirectional links.
5. **Using real personal data in examples** → Always use obviously synthetic data.
6. **Creating monolithic docs** → Split by concern. Target line counts are in `docs/AGENTS.md`.
7. **Leaving stale plans in `docs/plans/`** → Completed or superseded plans must be moved to `docs/plans/archive/`.
8. **Creating a `backlog.md` (or `todo.md`, `ideas.md`)** → Backlog lives in GitHub issues. Plan files are only for time-bounded execution notes for a specific in-flight effort.
9. **Over-documenting routine changes** → Not every change needs a docs update. Write what helps the next reader understand current state.
10. **Deleting or modifying a failing test to unblock a commit** → See AGENTS.md § "Tests Are Sacred" for the full decision framework.
11. **Committing without running the test suite** → Run `./scripts/test.sh` before every commit. No exceptions.
