This directory contains Architecture Decision Records (ADRs) — immutable records of significant design decisions with context and rationale.

## Contents

- `001-python-fastapi.md` — Python 3.11+ with FastAPI as the backend framework
- `002-chromadb-vector-store.md` — ChromaDB in client-server mode for semantic search
- `003-two-tier-data-model.md` — SourceEntity (raw) + PersonEntity (canonical) separation
- `004-hybrid-search.md` — Vector (ChromaDB) + keyword (BM25/FTS5) with RRF fusion
- `005-external-venv-macos-tcc.md` — Virtual environment at ~/.venvs to avoid TCC scanning **(Superseded by 007)**
- `006-ollama-query-routing.md` — Local Ollama + Qwen 2.5 for query classification
- `007-linux-migration.md` — Linux migration and local LLM orchestration (supersedes 005)

## Key Principles

- ADRs are **append-only**. Never modify an accepted ADR. The only acceptable in-place edit is adding a `**Superseded By:** ADR-NNN` or `**Amended by:** ADR-NNN` pointer in the frontmatter.
- Naming: `NNN-kebab-case.md`. Sequential numbering, never reuse numbers.
- Every ADR must include frontmatter: `**Status:** Complete | Partial | Draft`, `**Last Updated:** YYYY-MM-DD`, `**Decision:** Accepted | Superseded | Deprecated`, and (if applicable) `**Superseded By:** ADR-NNN` / `**Amended by:** ADR-NNN`.
- Required sections: Context, Decision, Rationale, **Alternatives Considered** (each alternative + explicit "Rejected because"), **Consequences** split into **Positive** and **Negative**, Related Documents (4-bucket structure: Design Context / Specifications / Operational / Code References).
- Target length: 200–500 lines (max 800). Split into multiple ADRs if a single decision is too sprawling.
- See [docs/AGENTS.md § ADR Template](../AGENTS.md#adr-template) for the full template.

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
