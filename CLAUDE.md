@AGENTS.md

# Claude Code Configuration

## Workflow

- Use plan mode for non-trivial tasks (3+ files, architectural decisions, unclear requirements).
- After modifying docs, verify compliance with [docs/AGENTS.md](docs/AGENTS.md) standards.
- After modifying code, restart server before testing.
- When creating new directories or modules, check if an AGENTS.md + CLAUDE.md pair is appropriate.

## Skills

Repo skills are surfaced automatically as slash commands (their descriptions come from `.claude/skills/`); `/implement <task>` is the primary entry point — full lifecycle: plan → implement → PR → adversarial review → merge.

## Quick Reference

**Read these first:**
- [AGENTS.md](AGENTS.md) — Full project reference (principles, invariants, key files, commands)
- [docs/AGENTS.md](docs/AGENTS.md) — Documentation standards

## Common Mistakes

The operational and documentation mistake lists live in AGENTS.md (§ Common Mistakes to Avoid, § Documentation Rules) and § Tests Are Sacred — imported above. The docs-hygiene items below aren't restated there:

1. **Creating monolithic docs** → Split by concern. Target line counts are in `docs/AGENTS.md`.
2. **Creating a `backlog.md` (or `todo.md`, `ideas.md`)** → Backlog lives in GitHub issues. Plan files are only for time-bounded execution notes for a specific in-flight effort.
3. **Over-documenting routine changes** → Not every change needs a docs update. Write what helps the next reader understand current state.
