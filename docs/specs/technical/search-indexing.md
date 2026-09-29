# Search & Indexing Pipeline

> **Status:** Complete
> **Owner:** Search Pipeline
> **Last Updated:** 2026-02-19

How LifeOS searches across vault content using hybrid vector + keyword search.

---

## Pipeline Overview

```
Query → Name Expansion → [Vector Search + BM25 Search] → RRF Fusion → Boosting → Dedup → Reranking → Results
```

---

## Components

### 1. Name Expansion

Nicknames are expanded to canonical names (e.g., "Al" → "Alex") before search. This ensures queries about people match documents regardless of which name variant was used.

### 2. Dual Search

- **Vector Search**: Semantic similarity via ChromaDB embeddings
- **BM25 Search**: Keyword matching via SQLite FTS5; every query term is quoted so no input character is FTS5 syntax. A strict all-terms query runs first; any remaining slots up to the limit are filled with any-term (stop words removed) matches not already returned, unless the strict query already filled the limit; results carry `match_mode` (`and` rows first, then `or`)

Both searches run in parallel and their results are combined.

### 3. RRF Fusion

Reciprocal Rank Fusion combines results from both search backends:

```
score = Σ 1/(60 + rank)
```

This normalizes rankings across different scoring scales.

### 4. Boosting

Post-fusion boosting adjusts scores based on:
- **Recency**: 0-50% boost for newer documents
- **Filename match**: 2x boost when the query matches a filename

### 5. Deduplication

`deduplicate_overlapping_chunks()` removes near-duplicate results from the same source file. With 20% chunk overlap, adjacent chunks from the same document may both appear in results. The deduplicator keeps only the highest-scored chunk when adjacent chunks from the same file are present.

### 6. Reranking

A cross-encoder reranker (`api/services/reranker.py`) re-scores top results for relevance. Unlike bi-encoder similarity (used in vector search), the cross-encoder processes query and document together, catching nuances like negation and specificity.

- **Model**: `cross-encoder/ms-marco-MiniLM-L6-v2` (configurable via `LIFEOS_RERANKER_MODEL`)
- **Toggle**: Enabled by default, configurable via `LIFEOS_RERANKER_ENABLED`
- **Protected indices**: For factual queries (as determined by `query_classifier.py`), high-signal results containing query keywords are protected from being displaced by reranking. Semantic queries have no protected results.
- **Candidate pool**: Fetches `rerank_candidates` (default 50) results from the hybrid pipeline, then re-ranks and returns `top_k`

### Query Classification Impact

The query classifier (`api/services/query_classifier.py`) determines whether a query is factual or semantic. This affects the pipeline:
- **Factual queries**: Up to 3 top results containing query keywords are protected from reranking displacement
- **Semantic queries**: All results are eligible for reranking reorder

---

### Vault Tags

`api/services/vault_tagger.py` derives per-note facets (document type, domain, primary and secondary topics, project, actionability, has-decision, sensitivity) into `data/vault_tags.db` via `api/services/vault_tag_store.py`, a database separate from `bm25_index.db`. Rows are keyed by file path and current while the content hash and the taxonomy `vocab_version` match; modification time never triggers re-tagging. Closed-set facets come from one Jev call per note, gated by `LIFEOS_JEV_VAULT_TAGGING`, a folder allowlist, and an on-box sensitivity rule; raw probabilities are stored so thresholds can change without re-inference. Tags are never written to frontmatter. See [configuration.md](../../guides/configuration.md) for the privacy contract.

**Tag phrase in chunk context.** Each chunk's contextual prefix (`generate_chunk_context`) carries a tag phrase when the file's tag row has `doc_type_conf` of 0.6 or above: `Classified as <doc_type> in the <domain> domain about <topic>`, plus ` for project <project>` when `project_conf` is 0.6 or above and the project is not `none`. Underscores and `/` in tag values become spaces (`work/hiring` reads `work hiring`), parts without a value are omitted, and no people names appear. The prefix is embedded and keyword-indexed with the chunk, so a note is reachable by what it is about even when its body never uses the word. The indexer looks the row up by vault-relative POSIX path, the key `sync_vault_tag.py` writes; a missing store or row leaves the prefix unchanged. A change to a file's phrase tuple re-embeds it on the next reindex — see [data-and-sync.md](data-and-sync.md).

## Key Files

| File | Purpose |
|------|---------|
| `api/services/hybrid_search.py` | Main search logic |
| `api/services/vectorstore.py` | ChromaDB wrapper |
| `api/services/bm25_index.py` | BM25 index |
| `api/services/query_classifier.py` | Factual vs semantic detection |
| `api/services/reranker.py` | Cross-encoder re-ranking service |
| `api/services/query_router.py` | LLM-based source routing + person name extraction |
| `api/services/vault_tagger.py`, `api/services/vault_tag_store.py` | Per-note facet tagging and its rebuildable store |

## Related Documents

- [Data & Sync](data-and-sync.md) -- Data ingestion and vector store indexing
- [Architecture](architecture.md) -- System architecture and code structure
- [ADR-004: Hybrid Search](../../adr/004-hybrid-search.md) -- Why both vector and keyword search
- [ADR-012: Embedding Pipeline](../../adr/012-embedding-pipeline.md) -- Encoder model choice, GPU/CPU fallback, OOM protection
