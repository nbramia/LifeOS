This directory contains Architecture Decision Records (ADRs) — immutable records of significant design decisions with context and rationale.

## Contents

- `001-python-fastapi.md` — Python 3.11+ with FastAPI as the backend framework
- `002-chromadb-vector-store.md` — ChromaDB in client-server mode for semantic search
- `003-two-tier-data-model.md` — SourceEntity (raw) + PersonEntity (canonical) separation
- `004-hybrid-search.md` — Vector (ChromaDB) + keyword (BM25/FTS5) with RRF fusion
- `005-external-venv-macos-tcc.md` — Virtual environment at ~/.venvs to avoid TCC scanning
- `006-ollama-query-routing.md` — Local Ollama + Qwen 2.5 for query classification
- `007-linux-migration.md` — Linux migration and local LLM orchestration (supersedes 005)

## Key Principles

- ADRs are **append-only**. Never modify an accepted ADR; create a new one to supersede it.
- Naming: `NNN-kebab-case.md`. Sequential numbering, never reuse numbers.
- Every ADR must include frontmatter: Decision, Date, Status, Last Updated.
- Target length: 200–600 lines (max 800).

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
