# ADR-002: ChromaDB for Vector Storage

> **Decision:** Use ChromaDB in client-server mode as the vector database for semantic search.
> **Date:** 2026-02-19
> **Status:** Accepted
> **Last Updated:** 2026-02-19

## Context

LifeOS needs a vector database for semantic search over personal documents including Obsidian notes, emails, calendar events, messages, and more. The vector store is a core infrastructure component — it stores embeddings for every indexed document and handles similarity queries that power the search pipeline.

Requirements are shaped by LifeOS's design principles. **Local-first**: all data must stay on the user's machine — no cloud vector databases. **Server mode**: the vector store should run as a separate process so the API server can restart independently without losing index state or triggering expensive re-indexing. **Scale**: must handle ~100K+ documents with acceptable query latency (<500ms). **Python-native**: first-class Python client to minimize integration friction with the FastAPI backend.

The choice of vector database also affects operational complexity. LifeOS runs on a Mac Mini as a background service managed by launchd. Any vector store must be reliable enough to run unattended, with minimal configuration and reasonable recovery from crashes or ungraceful shutdowns.

## Decision

ChromaDB running in client-server mode on port 8001. The embedding model (all-MiniLM-L6-v2) runs in-process within the FastAPI server. ChromaDB stores pre-computed embeddings and handles similarity search.

## Rationale

- **Local-first**: ChromaDB runs entirely on the local machine with no cloud dependency, satisfying the core privacy requirement.
- **Simple Python API**: Native Python client with a clean collection-based interface. Adding, querying, and deleting documents is straightforward.
- **Server mode**: Client-server separation means the API can restart without rebuilding the vector index. ChromaDB persists data to disk independently.
- **Adequate performance**: For the target scale (~100K documents, single user), ChromaDB provides sub-200ms query latency, which is well within requirements.
- **Low operational overhead**: Single binary, minimal configuration, SQLite-backed metadata storage.

## Alternatives Considered

### Pinecone

Pinecone is a fully managed cloud vector database with excellent query performance and scaling capabilities. However, it fundamentally violates LifeOS's local-first privacy requirement — all vectors and metadata would be stored on Pinecone's servers. Beyond privacy, it introduces an external dependency and recurring cost ($70+/month for production usage) for a single-user system that runs on local hardware. Pinecone is the right choice for multi-tenant SaaS applications, not for a self-hosted personal assistant.

### Weaviate

Weaviate is a capable open-source vector database with a rich feature set including hybrid search, multi-tenancy, and GraphQL APIs. It can run self-hosted and satisfies the local-first requirement. However, its operational footprint is significantly heavier than ChromaDB — it requires more memory, has more complex configuration (schema definitions, module configuration), and its Docker-based deployment model adds friction for a launchd-managed macOS service. For a single-user system indexing ~100K documents, Weaviate's enterprise features (multi-tenancy, horizontal scaling) add complexity without corresponding benefit.

### Qdrant

Qdrant is a strong open-source alternative with excellent performance characteristics and a Rust-based engine. At the time of evaluation, its Python client was less mature than ChromaDB's, with fewer examples and less community support for the specific integration patterns LifeOS uses (collection-per-source, metadata filtering). Qdrant would be a viable migration target if ChromaDB's stability issues worsen — its Rust core offers better reliability guarantees than ChromaDB's Python/SQLite backend.

### pgvector (PostgreSQL extension)

pgvector adds vector similarity search to PostgreSQL. While this would consolidate the data layer (vectors + metadata in one database), it requires adding PostgreSQL as a dependency. LifeOS currently uses only SQLite for relational data, and adding a full PostgreSQL server for vector search alone is disproportionate to the need. PostgreSQL also requires more memory, more configuration, and more operational attention than ChromaDB's self-contained deployment.

### FAISS (Facebook AI Similarity Search)

FAISS provides excellent vector search performance and is widely used in research. However, it operates as an in-process library with no built-in server mode or persistence management. Every API server restart would require reloading the entire index from disk, adding seconds to startup time. FAISS also lacks built-in metadata storage and filtering — these would need to be implemented separately. For a service that restarts frequently during development and must persist state across process lifecycles, FAISS's in-process model is a poor fit.

## Consequences

**Positive:**
- All personal data stays on the local machine — no cloud vector database dependency.
- Simple API reduces integration and maintenance effort.
- Server mode provides process isolation and independent persistence.
- Lightweight enough to run alongside the API server on a Mac Mini.

**Negative:**
- Less battle-tested at scale than Pinecone or Weaviate. Occasional stability issues under heavy write loads required adding a watchdog process (`LifeOS watchdog`).
- Smaller community means fewer resources for troubleshooting edge cases.
- Migration to another vector store would require re-embedding all documents.

**Risks:**
- ChromaDB's stability under heavy write loads (bulk sync operations) has required a watchdog process. If stability degrades further, migration to Qdrant or another store may be necessary, which would require re-embedding the entire corpus.
- ChromaDB's development pace means API changes between versions can require code updates. Pinning the ChromaDB version and testing upgrades before deploying is essential.
- The SQLite-backed metadata store has a theoretical size limit, though this is unlikely to be reached at the current scale (~100K documents).

## Related Documents

**Design Context:**
- [ADR-004: Hybrid Search](004-hybrid-search.md) — How ChromaDB is used alongside BM25 for search
- [ADR-001: Python/FastAPI](001-python-fastapi.md) — The Python stack that ChromaDB integrates with

**Specifications:**
- [Data and Sync](../specs/technical/data-and-sync.md) — Sync pipeline that populates ChromaDB
- [Search Indexing](../specs/technical/search-indexing.md) — How vector search fits into the hybrid search pipeline
- [Architecture](../specs/technical/architecture.md) — System architecture including ChromaDB's role

**Operational:**
- [Troubleshooting](../guides/troubleshooting.md) — ChromaDB debugging and watchdog management
- [AGENTS.md](../../AGENTS.md) — Watchdog cron entry and health check commands
