# Archive Index

**Status:** Complete
**Last Updated:** 2026-05-27

This directory holds **superseded documents, audit notes, and historical investigation working files** that the operator wants to keep locally for reference but aren't part of the live documentation surface.

> **Gitignored content:** the actual archive files in this directory are personal/historical and gitignored. Only this `INDEX.md` is tracked, so anyone landing here knows what the directory is for even when the contents aren't visible.

## What lives here

Archive content typically falls into one of three categories:

| Category | Examples | What to do when you find one |
|----------|----------|-------------------------------|
| **Durable insight** — architectural finding still true, problem analysis still useful | An audit doc that surfaced a real constraint that still applies | If a live spec doesn't already capture the insight, promote it: extract the durable conclusion into a spec or ADR, then leave the archive file alone. |
| **Superseded** — the issue was fixed or the finding obsolete | Pre-Linux-migration deployment notes; old audit findings now reflected in the live specs and ADRs | Leave in archive; no live-doc action needed. |
| **Ephemeral investigation** — working notes, draft analysis, exploratory writeups | "What if we did X?" thought experiments, point-in-time gap analyses | Leave in archive; no live-doc action needed. |

## How to add to the archive

When a live doc is superseded, deleted, or extensively rewritten:

1. Move the old version to `docs/archive/` with a date-prefix on the filename if it's a point-in-time snapshot (`audit-frontend-2026-02.md`) or a thematic prefix if it's an investigation (`audit-vision.md`).
2. Add a one-line note to the top of the moved file: `Superseded by [link]` or `Investigation closed YYYY-MM-DD`.
3. If you're moving an investigation that turned into a live spec, link the live spec from the archive file's intro so future readers can find the current source of truth.

Don't link **to** archive files from live docs unless you're explicitly promoting a durable insight — the goal is for live docs to stand on their own.

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
- [Plans/](../plans/) — Ephemeral working notes (planning side; this directory is the historical/audit side)
- [ADR/](../adr/) — Where decisions land permanently (different from archive — ADRs are immutable but active)
