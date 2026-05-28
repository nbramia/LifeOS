This directory contains time-bounded execution notes — working files for a specific in-flight effort.

> **Gitignored content:** the actual plan files in this directory are personal and gitignored. Only this `AGENTS.md` and `CLAUDE.md` are tracked, so any agent or contributor landing here knows the conventions even when the personal content isn't visible.

> **Backlog lives in GitHub issues — never in `docs/plans/`.** Do not create `backlog.md`, `todo.md`, `ideas.md`, or any running list of deferred work. File a GitHub issue instead. Plan files exist only for the slice of work that's actively being executed and needs scratch space outside the issue tracker.

## Contents

The directory typically holds:

- `archive/` — completed or superseded plans (also gitignored; preserved locally for reference).
- Dated planning notes — point-in-time snapshots, gap analyses, issue-drafting context for a specific effort. Date-prefixed in the filename (e.g., `2026-05-11-mvp-gap-analysis.md`).

The directory does **not** hold: running backlogs, idea lists, todo files, or anything that should live as a GitHub issue.

## Key Principles

- **Backlog → GitHub issues, never plan files.** If you're tempted to write a list of "things to do later", that's an issue, not a plan.
- Plans are **ephemeral**. They become git history (or, in this repo, never enter the public repo) when complete. Move finished plans to `archive/` with a `**Completed:** YYYY-MM-DD` line in the frontmatter; supersede stale plans with a pointer to the successor.
- Plans answer "how do we get from current state to the spec?" They are **not** specs themselves — keep target-state design out of plan files; that belongs in [`../specs/`](../specs/).
- Every plan file should include frontmatter: `Status` (`Draft` | `Active` | `Completed` | `Superseded`), `Last Updated`, and `Target Date` (or `Ongoing`).
- Never put planning content (roadmaps, task lists, checklists) in `specs/` or `adr/`.

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
- [ADR/](../adr/) — Where architectural decisions live (permanent, immutable; not plans)
