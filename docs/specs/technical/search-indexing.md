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

**Operator labels as evidence.** For every non-restricted note, whether or not Jev is called: a frontmatter `type:` mapped by the taxonomy's `type_doc_types` sets `doc_type` with confidence 1.0 and `doc_type_source` `frontmatter`, and Jev is not asked the document-type question (other facets are still asked); otherwise the source is `jev`, or `code` for a record Jev never saw (rows written before the column existed read as `jev`). A human tag (frontmatter or inline) mapped by `tag_topics` always appears among the stored topics: as a secondary topic, or as the primary topic with confidence 1.0 when no primary exists or the classifier named only the topic's parent domain. `topics_json` records the mapped topics under `from_tags`. A `topic` facet matches a note's primary or secondary topics. `people/<slug>` tags are candidate names for the note's `people` metadata (the slug's hyphens and underscores read as spaces, then alias resolution runs as for body text); they never become `tag:` metadata keys or `tags` metadata.

**Tag phrase in chunk context.** Each chunk's contextual prefix (`generate_chunk_context`) carries a tag phrase when the file's tag row has `doc_type_conf` of 0.6 or above: `Classified as <doc_type> in the <domain> domain about <topic>`, plus ` for project <project>` when `project_conf` is 0.6 or above and the project is not `none`. Underscores and `/` in tag values become spaces (`work/hiring` reads `work hiring`), parts without a value are omitted, and no people names appear. The prefix is embedded and keyword-indexed with the chunk, so a note is reachable by what it is about even when its body never uses the word. The indexer looks the row up by vault-relative POSIX path, the key `sync_vault_tag.py` writes; a missing store or row leaves the prefix unchanged. A change to a file's phrase tuple re-embeds it on the next reindex — see [data-and-sync.md](data-and-sync.md).

### Facets

`HybridSearch.search(..., facets=SearchFacets(...))` (`api/services/search_facets.py`) narrows a search by `folder`, `note_type`, `people`, `tags` (human tags), and the machine facets `doc_type`, `domain`, `topic`, `project`. A list matches any of its items; different facets must all match. The `/api/search` `filters`, the `search_vault` orchestrator tool, and the `lifeos_search` MCP tool expose the same fields.

Facets resolve to a set of allowed absolute file paths that both arms use as a pre-filter before ranking:

| Facet | Source |
|-------|--------|
| `doc_type`, `domain`, `topic`, `project` | Vault tag store. It keys vault-relative paths; they are joined to the resolved (symlink-free) `settings.vault_path`, the same resolution the indexer keys files by, to match the absolute paths the indexes use. A missing or empty store matches nothing. A `topic` matches a note whose primary or secondary topic it is, or whose stored topic distribution ranks it within the top `TOPIC_MATCH_TOP_K` or gives it probability of at least `TOPIC_MATCH_MIN_P` (`vault_tag_store.py`; chosen by `scripts/jev_eval/calibrate_topic_match.py` against notes carrying a mapped human tag). A parent `topic` matches its `parent/child` topics the same way. Only matching widens; the stored primary topic is unchanged. The distribution is mirrored on every `upsert` into the `vault_tag_topics` side table (`file_path`, `topic`, `p`, `rank`; only topics with p > 0) so a query never parses JSON; over 9,000 synthetic rows a topic query takes about 16 ms. |
| `note_type` | Alias for a vault folder (`Work`, `Personal`, `LifeOS`, `Granola`, `ML` = `Work/ML`, `Other` = the rest), resolved by path; other values match stored chunk metadata |
| `tags` | Vector-store chunk metadata: each tag is also stored as a boolean key `tag:<lowercased tag>` (the JSON `tags` string remains). Every nightly vault reindex runs a metadata-only scan of all chunks (`VectorStore.backfill_search_keys`, no re-embedding) that writes the `tag:` keys onto chunks whose JSON `tags` are non-empty and the `modified_day` key onto chunks lacking it; a complete chunk is not written, so completion is never inferred from a marker and a replaced or partially migrated collection is repaired. The run reports `search_keys_backfilled`; on a synthetic 48,000-chunk collection the first pass writes every chunk in about 5 s and a no-op pass takes about 1.5 s. |
| `people` | Whole person values, compared case-insensitively without stemming. The BM25 `people` column (space-joined, stemmed) only narrows candidate files; each candidate is confirmed against the vector store's per-chunk `people` lists, so `Robert` never matches `Roberts` and `Stone Blake` never matches `Avery Stone` plus `Blake Reed`. |
| `folder` | Vault-relative directory, matched on a path-segment boundary (`Work/Meetings` never matches `Work/Meetings-old`); a value that escapes the vault matches nothing. Applied as a prefix test on the other facets' sets, or as a prefix scan of the BM25 catalog when it is the only facet. |

The vector arm restricts with a Chroma `where` on `file_path` (`$in`). Allowed sets longer than 2,000 paths run as batched queries whose candidates are merged by distance, so the result equals one unbounded query without an oversized parameter list. On a synthetic 48,000-chunk, 9,600-file collection (1,536 dimensions) a restricted vector query costs about 40 ms up to 2,000 files, about 70 ms for 4,000 and about 190 ms for the whole set, against 4 ms unrestricted. The BM25 arm restricts inside the FTS query with a per-row path check, applied before `LIMIT`, so a selective restriction still fills the candidate list.

A `date_from`/`date_to` window (inclusive) constrains candidates before either arm's limit. Each chunk carries an integer `modified_day` (days since 1970-01-01, from `modified_date`; `-1000000` when the date does not parse). The vector arm adds a `modified_day` range clause (`$gte`/`$lte`, `$and`ed with any other `where`) inside the query, OR `modified_day = -1000000` so undated chunks pass; chunks not yet backfilled carry no key and are excluded until the reindex writes it. The BM25 arm checks `doc_dates` inside the FTS query, and undated chunks pass there too. On a synthetic 48,000-chunk collection (32 dimensions) fetching 50 candidates costs about 2 ms unwindowed, about 50 ms with half the chunks in the window and about 12 ms with 2%. The `/api/search` `filters.date_from/to` are intersected with the top-level `date_from/to` and behave identically.

A facet set that matches zero files returns `[]` immediately; nothing is recorded as a degradation. With no facets the arms are called exactly as without this feature. With `SearchFacets(..., boost=True)` nothing is filtered; results whose file matches the facets have their fused score multiplied by `LIFEOS_SEARCH_FACET_BOOST` (default 1.2). The `search_attribution` span records the names of the facets used, never their values.

## Key Files

| File | Purpose |
|------|---------|
| `api/services/hybrid_search.py` | Main search logic |
| `api/services/vectorstore.py` | ChromaDB wrapper |
| `api/services/bm25_index.py` | BM25 index |
| `api/services/search_facets.py` | Facet model and facet-to-allowed-paths resolution |
| `api/services/query_classifier.py` | Factual vs semantic detection |
| `api/services/reranker.py` | Cross-encoder re-ranking service |
| `api/services/query_router.py` | LLM-based source routing + person name extraction |
| `api/services/vault_tagger.py`, `api/services/vault_tag_store.py` | Per-note facet tagging and its rebuildable store |

## Related Documents

- [Data & Sync](data-and-sync.md) -- Data ingestion and vector store indexing
- [Architecture](architecture.md) -- System architecture and code structure
- [ADR-004: Hybrid Search](../../adr/004-hybrid-search.md) -- Why both vector and keyword search
- [ADR-012: Embedding Pipeline](../../adr/012-embedding-pipeline.md) -- Encoder model choice, GPU/CPU fallback, OOM protection
