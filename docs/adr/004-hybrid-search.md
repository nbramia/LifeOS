# ADR-004: Hybrid Search (Vector + BM25 with RRF Fusion)

> **Decision:** Combine ChromaDB vector search with SQLite FTS5 BM25 keyword search, fused via Reciprocal Rank Fusion.
> **Date:** 2026-02-19
> **Status:** Accepted
> **Last Updated:** 2026-02-19

## Context

LifeOS serves personal data queries that span a wide spectrum of intent and specificity. Users ask semantic questions ("meetings about the product roadmap"), exact-match questions ("email from john@acme.com on January 15th"), and hybrid questions that require both ("what did Sarah say about the budget?"). A single search technology cannot serve all three well.

Vector search using embedding models excels at understanding intent and meaning — it knows that "strategy" and "roadmap" are conceptually related. But it struggles with exact matches: querying "john@acme.com" relies on the embedding model recognizing email address patterns, which is unreliable. Keyword search (BM25) excels at exact and partial term matching but misses conceptual similarity entirely — "meetings about strategy" would not match documents using "planning" or "roadmap."

Personal data queries are particularly demanding because they frequently combine proper nouns (names, email addresses, dates) with semantic intent. The search system must handle both dimensions simultaneously, which requires retrieving candidates from multiple sources and combining them intelligently.

## Decision

Dual-index search with fusion:

1. **Vector search**: ChromaDB with all-MiniLM-L6-v2 embeddings for semantic similarity.
2. **Keyword search**: SQLite FTS5 with BM25 scoring for exact and partial term matching.
3. **Fusion**: Reciprocal Rank Fusion (RRF) combines ranked results from both sources into a single ranking, with optional query-type-aware boosting.
4. **Reranking**: A final reranking pass scores fused results against the original query.

Both indexes are populated during the same ingestion pipeline. The BM25 index lives in SQLite alongside ChromaDB's metadata store.

## Rationale

- **Complementary strengths**: Vector search excels at "what is this about?" while BM25 excels at "does this contain this exact term?" Together they cover the full query spectrum.
- **RRF is simple and effective**: Reciprocal Rank Fusion (`score = 1 / (k + rank)` with k=60) is parameter-free beyond the constant k, well-studied in IR literature, and produces strong results without complex learned fusion models. It does not require training data or per-query weight tuning.
- **SQLite FTS5 is lightweight**: No additional service dependency. FTS5 is built into SQLite, which is already used for metadata. The BM25 index adds disk usage but no operational complexity.
- **Name expansion**: The search pipeline expands person names using the known-people dictionary before querying, improving recall for queries like "messages from Mike" when the contact is stored as "Michael."

## Alternatives Considered

### Vector-Only Search

Using only ChromaDB for all queries would simplify the architecture to a single search path. Semantic queries would work well, and the system would be easier to maintain with one index. However, vector search fundamentally struggles with exact-match queries. Querying "john@acme.com" produces unpredictable results because embedding models were not trained to treat email addresses as atomic identifiers. Similarly, date queries ("January 15th") depend on the embedding model's incidental handling of date formats. For LifeOS, where users frequently search for specific people by name or email, this recall gap is unacceptable.

### Keyword-Only Search (BM25)

BM25 keyword search is well-understood, fast, and excellent for exact-match queries. It would handle "email from john@acme.com" perfectly. However, it completely misses semantic similarity — "meetings about strategy" would not match documents containing "planning session" or "roadmap discussion." For a personal AI assistant where users often ask conceptual questions about their data, keyword-only search would produce frustrating gaps. The value of semantic understanding for personal data queries outweighs BM25's simplicity advantage.

### Elasticsearch

Elasticsearch provides both keyword and vector search in a single system, with sophisticated query DSL, analyzers, and built-in hybrid search capabilities. It would eliminate the need for a separate ChromaDB instance and SQLite FTS5 index. However, Elasticsearch requires a JVM, consumes significant memory (1-2GB minimum), and adds substantial operational complexity for a single-user system. The benefit of consolidation does not justify adding a heavyweight Java dependency to a Python/SQLite stack. For LifeOS's scale (~100K documents, single user), the lighter combination of ChromaDB + FTS5 is proportionate.

### Reranking-Only (Single Source + Reranker)

A reranking approach would retrieve candidates from a single source (e.g., vector search) and then rerank them using a cross-encoder model. This can improve precision significantly but cannot fix recall — if the initial retrieval misses a relevant document because it's an exact-match query, no reranker can recover it. LifeOS uses reranking as a final stage after fusion, not as a substitute for dual-source retrieval. The combination of broad recall (from two retrieval sources) plus precision (from reranking) produces better results than either approach alone.

## Consequences

**Positive:**
- Excellent recall across both semantic and exact-match queries.
- Lightweight — no additional services beyond SQLite (already present) and ChromaDB (already required).
- RRF fusion is simple to implement and maintain.
- Name expansion via the people dictionary improves people-related query recall.

**Negative:**
- Two indexes to maintain — ingestion and reindexing take roughly twice as long.
- RRF weights may need per-query-type tuning for edge cases (currently uses fixed k=60).
- Debugging search results requires inspecting both vector and BM25 contributions.

**Risks:**
- As the corpus grows, maintaining two indexes doubles the indexing time and storage requirements. If sync operations become slow, the dual-index approach may need optimization (incremental indexing, batch updates).
- The fixed RRF constant (k=60) was chosen empirically. Different query types may benefit from different fusion weights, which would require a more sophisticated query-aware fusion strategy.
- FTS5's default tokenizer may not handle all data formats well (e.g., CamelCase identifiers, hyphenated names, international characters). Custom tokenizer configuration may be needed as the data diversity increases.

## Related Documents

**Design Context:**
- [ADR-002: ChromaDB](002-chromadb-vector-store.md) — The vector database powering semantic search
- [ADR-006: Ollama Query Routing](006-ollama-query-routing.md) — How queries are classified before reaching the search pipeline

**Specifications:**
- [Search Indexing](../specs/technical/search-indexing.md) — Detailed hybrid search implementation
- [Architecture](../specs/technical/architecture.md) — How search fits into the overall system
- [Data Model](../specs/product/data-model.md) — What gets indexed (SourceEntity and PersonEntity data)

**Operational:**
- [AGENTS.md](../../AGENTS.md) — Search API commands and vault reindex trigger
