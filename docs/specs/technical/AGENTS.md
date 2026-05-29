This directory contains technical specifications — how the system is built from an engineering perspective.

## Contents

- `agent-viz.md` — Implementation of the `/agents` page (SSE feed, JSONL ingest, D3 graph, status inference)
- `agent-worker.md` — Agent worker internals (session lifecycle, executor split, MCP transport, budget enforcement)
- `architecture.md` — Top-level code structure, module boundaries, request flow
- `claude-code-orchestration.md` — Implementation of the Claude Code orchestration surface (skill discovery, MCP server wiring)
- `data-and-sync.md` — Seven-phase nightly sync pipeline; per-phase responsibilities and failure modes
- `frontend.md` — Vanilla HTML/JS architecture (no build step), shared components, page conventions
- `observability.md` — Performance tracing (spans, SQLite), health checks, alerting tiers
- `scheduler.md` — Scheduler internals (markdown source of truth + index cache, round-trip, watcher reindex, firing/dispatch)
- `search-indexing.md` — Hybrid search internals (vector + BM25 fusion, embedding pipeline integration)
- `security-privacy.md` — Auth, network exposure, data-at-rest, privacy invariants

## Key Principles

- Technical specs describe **HOW** (engineering view) — schemas, route-handler structure, library internals, performance constraints. If you find yourself describing what the user sees or how the API is *meant* to be used, that belongs in `product/`.
- Privacy and security implementation details live here, not in guides. Guides are how-tos; specs are the *what* and *why* of the implementation.
- Every technical spec must include frontmatter: `Status`, `Last Updated`, `Owner`.
- Cross-link bidirectionally with the matching `product/` spec when one exists (e.g., `product/agent-worker.md` ↔ `technical/agent-worker.md`).

## Related Documents

- [Documentation Strategy](../../AGENTS.md) — Rules governing all documentation
- [Product Specs](../product/) — Consumer-facing counterpart to these technical specs
- [Standards](../standards/) — Coding and testing conventions referenced from implementation specs
