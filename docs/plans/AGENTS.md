This directory contains active execution plans — ephemeral working notes describing how and when to build what specs describe.

> **Gitignored content:** the actual plan files in this directory are personal and gitignored. Only this `AGENTS.md` and `CLAUDE.md` are tracked, so any agent or contributor landing here knows the conventions even when the personal content isn't visible.

## Contents

The directory typically holds:

- `backlog.md` — running operator backlog of small tasks, bugs, and improvements (personal, gitignored).
- `archive/` — completed or superseded plans (also gitignored; preserved locally for reference).
- Dated planning notes — point-in-time snapshots, gap analyses, issue-drafting context. Date-prefixed in the filename.

For trackable cross-contributor work, prefer **GitHub issues** over plan files. Plan files are for personal/local planning that doesn't need to be in the repo history.

## Key Principles

- Plans are **ephemeral**. They become git history (or, in this repo, never enter the public repo) when complete. Move finished plans to `archive/` with a `**Completed:** YYYY-MM-DD` line in the frontmatter; supersede stale plans with a pointer to the successor.
- Plans answer "how do we get from current state to the spec?" They are **not** specs themselves — keep target-state design out of plan files; that belongs in [`../specs/`](../specs/).
- Every plan file should include frontmatter: `Status` (`Draft` | `Active` | `Completed` | `Superseded`), `Last Updated`, and `Target Date` (or `Ongoing`).
- Never put planning content (roadmaps, task lists, checklists) in `specs/` or `adr/`.

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
- [ADR/](../adr/) — Where architectural decisions live (permanent, immutable; not plans)
