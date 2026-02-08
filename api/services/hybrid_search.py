"""
Hybrid Search for LifeOS.

Combines vector similarity search with BM25 keyword search using
Reciprocal Rank Fusion (RRF).

## Pipeline

1. **Name Expansion**: Nicknames → canonical ("Al" → "Alex")
2. **Dual Search**: Vector (semantic) + BM25 (keywords, OR semantics)
3. **RRF Fusion**: score = Σ 1/(60 + rank)
4. **Boosting**: Recency (0-50%) + Filename match (2x)
5. **Ranking**: Sort by hybrid_score

## Key Design Decisions

- **OR semantics**: AND fails when no chunk has all terms
- **2x filename boost**: Person files rank first for person queries
- **Possessive handling**: "Alex's" → "alex" for ALIAS_MAP lookup

## Usage

    from api.services.hybrid_search import get_hybrid_search
    results = get_hybrid_search().search("Alex's phone number", top_k=10)
"""
import re
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from config.settings import settings
from api.services.query_classifier import classify_query

# Lazy imports to avoid slow ChromaDB initialization at import time
if TYPE_CHECKING:
    from api.services.vectorstore import VectorStore
    from api.services.bm25_index import BM25Index

logger = logging.getLogger(__name__)


def find_protected_indices(
    query: str,
    results: list[dict],
    max_protected: int = 3
) -> list[int]:
    """
    Find indices of results to protect from reranking.

    For factual queries, protects top results that contain query keywords.
    For semantic queries, returns empty list (no protection).

    Args:
        query: Search query string
        results: Hybrid search results
        max_protected: Maximum number of results to protect

    Returns:
        List of indices to protect (may be empty)
    """
    query_type = classify_query(query)

    if query_type == "semantic":
        return []

    # Factual query: find results containing query keywords
    query_lower = query.lower()

    # Extract significant keywords (skip common words)
    stop_words = {"what", "is", "the", "a", "an", "of", "for", "to", "s"}
    keywords = []
    for word in query_lower.split():
        clean = re.sub(r"[''`]s?$", "", word)  # Remove possessive
        clean = re.sub(r"[^a-z0-9]", "", clean)  # Remove punctuation
        if clean and clean not in stop_words and len(clean) >= 2:
            keywords.append(clean)

    if not keywords:
        return []

    protected = []
    for i, result in enumerate(results):
        if len(protected) >= max_protected:
            break

        content = result.get("content", "").lower()

        # Check if content contains any significant keyword (whole words only)
        content_words = set(re.findall(r'\b\w+\b', content))
        matches = sum(1 for kw in keywords if kw in content_words)
        if matches >= 1:  # At least one keyword match
            protected.append(i)

    return protected


def expand_person_names(query: str) -> str:
    """
    Expand nicknames/aliases in query to canonical names.

    Users use nicknames ("Al") but files use canonical names ("Alex.md").
    Expanding before search ensures both vector and BM25 find matches.

    Handles possessives: "Al's" → "Alex's", "als" → "Alex's"
    Configured via config/people_dictionary.json and ALIAS_MAP.

    Args:
        query: Original search query

    Returns:
        Query with person names expanded

    Example:
        >>> expand_person_names("What is Al's phone?")
        "What is Alex's phone?"
    """
    from api.services.people import ALIAS_MAP

    # Tokenize query, preserving case for non-name words
    words = query.split()
    expanded = []

    for word in words:
        # Clean word for lookup (remove possessives, punctuation)
        clean = re.sub(r"[''`]s?$", "", word.lower())
        clean = re.sub(r"[^a-z]", "", clean)

        # Check for match (minimum 2 chars to avoid expanding common words)
        canonical = None
        is_possessive = False

        if len(clean) >= 2 and clean in ALIAS_MAP:
            canonical = ALIAS_MAP[clean]
            # Check if original was possessive
            if word.lower().endswith("'s") or word.lower().endswith("'s"):
                is_possessive = True
        elif len(clean) >= 3 and clean.endswith("s") and clean[:-1] in ALIAS_MAP:
            # Handle "tays" -> "tay" + "s" (possessive without apostrophe)
            # Require 3+ chars to avoid "is" -> "i" + "s"
            canonical = ALIAS_MAP[clean[:-1]]
            is_possessive = True

        if canonical:
            if is_possessive:
                expanded.append(f"{canonical}'s")
            else:
                expanded.append(canonical)
        else:
            expanded.append(word)

    return " ".join(expanded)


def reciprocal_rank_fusion(
    vector_results: list[str],
    bm25_results: list[str],
    k: int = 60
) -> list[tuple[str, float]]:
    """
    Merge two ranked lists using Reciprocal Rank Fusion (RRF).

    Formula: score(doc) = Σ 1/(k + rank) for each list containing doc

    Documents found by BOTH methods score higher. k=60 is the standard
    constant from academic literature (Cormack et al., 2009).

    Args:
        vector_results: Doc IDs ranked by vector similarity
        bm25_results: Doc IDs ranked by BM25 relevance
        k: Ranking constant (default 60)

    Returns:
        List of (doc_id, rrf_score) tuples sorted by score descending
    """
    scores = defaultdict(float)

    # Score from vector results
    seen_vector = set()
    for rank, doc_id in enumerate(vector_results):
        if doc_id not in seen_vector:
            scores[doc_id] += 1 / (k + rank + 1)
            seen_vector.add(doc_id)

    # Score from BM25 results
    seen_bm25 = set()
    for rank, doc_id in enumerate(bm25_results):
        if doc_id not in seen_bm25:
            scores[doc_id] += 1 / (k + rank + 1)
            seen_bm25.add(doc_id)

    # Sort by score descending
    sorted_results = sorted(scores.items(), key=lambda x: -x[1])

    return sorted_results


def calculate_recency_boost(date_str: Optional[str], max_boost: float = 0.5) -> float:
    """
    Calculate recency boost based on document date.

    Args:
        date_str: ISO format date string
        max_boost: Maximum boost value (default 0.5)

    Returns:
        Boost value between 0.0 and max_boost
    """
    if not date_str:
        return 0.0

    try:
        # Parse various date formats
        if "T" in date_str:
            doc_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        else:
            doc_date = datetime.fromisoformat(date_str)

        # Make naive datetime for comparison
        if doc_date.tzinfo:
            doc_date = doc_date.replace(tzinfo=None)

        now = datetime.now()
        days_old = (now - doc_date).days

        # Decay function: newer = higher boost
        # 0 days old = max_boost, 365 days old = ~0
        if days_old <= 0:
            return max_boost
        elif days_old >= 365:
            return 0.0
        else:
            # Exponential decay
            return max_boost * (1 - (days_old / 365) ** 0.5)

    except (ValueError, TypeError):
        return 0.0


def deduplicate_overlapping_chunks(results: list[dict]) -> list[dict]:
    """
    Remove overlapping chunks from same file, keeping highest scored.

    With 20% chunk overlap, adjacent chunks may both appear in results.
    This deduplicates by keeping only the highest-scored chunk when
    adjacent chunks from the same file are present.

    Args:
        results: Search results sorted by score (descending)

    Returns:
        Deduplicated results with adjacent chunks removed
    """
    if not results:
        return results

    seen_file_chunks: dict[str, set[int]] = {}  # file_path -> set of chunk_indices
    deduplicated = []

    for result in results:
        file_path = result.get("file_path", "") or result.get("metadata", {}).get("file_path", "")

        # Extract chunk index from result
        chunk_idx = result.get("metadata", {}).get("chunk_index", -1)
        if chunk_idx == -1:
            # Try to extract from ID (format: /path/file.md::N or /path/file.md_N)
            doc_id = result.get("id", "")
            if "::" in doc_id:
                try:
                    chunk_idx = int(doc_id.split("::")[-1])
                except ValueError:
                    chunk_idx = -1
            elif "_" in doc_id:
                try:
                    chunk_idx = int(doc_id.rsplit("_", 1)[-1])
                except ValueError:
                    chunk_idx = -1

        if not file_path:
            # No file path, can't deduplicate, include it
            deduplicated.append(result)
            continue

        if file_path not in seen_file_chunks:
            seen_file_chunks[file_path] = set()

        if chunk_idx == -1:
            # Unknown chunk index, include it
            deduplicated.append(result)
            seen_file_chunks[file_path].add(-1)
            continue

        # Check if adjacent chunk already included
        adjacent = {chunk_idx - 1, chunk_idx, chunk_idx + 1}
        if not adjacent.intersection(seen_file_chunks[file_path]):
            deduplicated.append(result)
            seen_file_chunks[file_path].add(chunk_idx)
        # else: skip this overlapping chunk (lower score since results are sorted)

    return deduplicated


class HybridSearch:
    """
    Main search orchestrator combining vector and BM25 search.

    Implements: name expansion → dual search → RRF fusion → boosting → ranking.
    Falls back to vector-only if BM25 is unavailable.

    Results contain: id, content, file_path, file_name, hybrid_score, rrf_score.
    """

    def __init__(
        self,
        vector_store: Optional["VectorStore"] = None,
        bm25_index: Optional["BM25Index"] = None
    ):
        """
        Initialize hybrid search.

        Args:
            vector_store: Vector store instance (default creates new)
            bm25_index: BM25 index instance (default uses singleton)
        """
        self.vector_store = vector_store
        self.bm25_index = bm25_index

    def _get_vector_store(self):
        """Lazy-load vector store."""
        if self.vector_store is None:
            from api.services.vectorstore import VectorStore
            self.vector_store = VectorStore()
        return self.vector_store

    def _get_bm25_index(self):
        """Get BM25 index, returns None if unavailable."""
        if self.bm25_index is not None:
            return self.bm25_index

        try:
            from api.services.bm25_index import get_bm25_index
            return get_bm25_index()
        except Exception as e:
            logger.warning(f"BM25 index unavailable: {e}")
            return None

    def search(
        self,
        query: str,
        top_k: int = 20,
        apply_recency_boost: bool = True,
        use_reranker: bool | None = None,
        rerank_candidates: int = 50
    ) -> list[dict]:
        """
        Perform hybrid search combining vector and BM25 with optional cross-encoder re-ranking.

        Pipeline: name expansion → vector + BM25 → RRF fusion → boosting →
                  [cross-encoder re-ranking] → ranking.

        Filename boost (2x) applied when person name in query matches filename,
        ensuring person-specific files rank first for person queries.

        When use_reranker=True:
        1. Retrieve rerank_candidates results via hybrid search
        2. Re-rank candidates using cross-encoder model
        3. Return top_k after re-ranking

        Args:
            query: Search query string
            top_k: Maximum results to return
            apply_recency_boost: Apply recency boosting (default True)
            use_reranker: Apply cross-encoder re-ranking (default True)
            rerank_candidates: Number of candidates to fetch for re-ranking (default 50)

        Returns:
            List of dicts with id, content, file_path, file_name, hybrid_score
        """
        # Use settings default if not explicitly specified
        if use_reranker is None:
            use_reranker = settings.reranker_enabled

        # Determine how many candidates to fetch
        fetch_k = rerank_candidates if use_reranker else top_k
        # Expand person names (nicknames -> canonical names)
        expanded_query = expand_person_names(query)
        if expanded_query != query:
            logger.debug(f"Expanded query: '{query}' -> '{expanded_query}'")

        # Get vector results (use expanded query for better semantic matching)
        vector_store = self._get_vector_store()
        vector_results = vector_store.search(query=expanded_query, top_k=fetch_k)

        # Extract doc IDs and create lookup
        vector_doc_ids = []
        results_by_id = {}

        for result in vector_results:
            doc_id = result.get("id")
            if doc_id:
                vector_doc_ids.append(doc_id)
                results_by_id[doc_id] = result

        # Get BM25 results (use expanded query for name resolution)
        bm25_index = self._get_bm25_index()
        bm25_doc_ids = []
        bm25_results_by_id = {}

        if bm25_index:
            try:
                bm25_results = bm25_index.search(expanded_query, limit=fetch_k)
                bm25_doc_ids = [r["doc_id"] for r in bm25_results]
                # Store BM25 results for later lookup
                bm25_results_by_id = {r["doc_id"]: r for r in bm25_results}
            except Exception as e:
                logger.warning(f"BM25 search failed: {e}")

        # If no BM25 results, return vector results directly
        if not bm25_doc_ids:
            logger.debug("No BM25 results, using vector-only search")
            # Track degradation if BM25 index was expected but unavailable
            if bm25_index is None:
                from api.services.service_health import record_degradation
                record_degradation("bm25_index", "hybrid_search", "vector_only", "BM25 index unavailable")
            return vector_results[:top_k]

        # Apply RRF fusion
        fused = reciprocal_rank_fusion(vector_doc_ids, bm25_doc_ids)

        # Extract person names from expanded query for filename boosting
        from api.services.people import ALIAS_MAP
        query_person_names = set()
        for word in expanded_query.lower().split():
            # Remove possessives and punctuation
            clean = re.sub(r"[''`]s$", "", word)  # Remove 's first
            clean = re.sub(r"[^a-z]", "", clean)  # Then remove other non-alpha
            if clean in ALIAS_MAP:
                query_person_names.add(ALIAS_MAP[clean].lower())

        # Build final results with recency boost
        final_results = []

        for doc_id, rrf_score in fused[:fetch_k]:
            # Get full result data
            if doc_id in results_by_id:
                result = results_by_id[doc_id].copy()
            elif doc_id in bm25_results_by_id:
                # BM25-only result - use the content from BM25
                bm25_result = bm25_results_by_id[doc_id]
                # Extract file path from doc_id (format: /path/to/file.md_chunkN)
                file_path = doc_id.rsplit("_", 1)[0] if "_" in doc_id else doc_id
                result = {
                    "id": doc_id,
                    "content": bm25_result.get("content", ""),
                    "file_path": file_path,
                    "file_name": bm25_result.get("file_name", ""),
                    "people": bm25_result.get("people", []),
                    "metadata": {
                        "file_name": bm25_result.get("file_name", ""),
                        "file_path": file_path,
                        "source": file_path,
                    }
                }
            else:
                # Unknown result, create minimal
                result = {"id": doc_id, "content": "", "metadata": {}}

            # Apply recency boost
            if apply_recency_boost:
                date_str = result.get("metadata", {}).get("date")
                recency_boost = calculate_recency_boost(date_str)
                final_score = rrf_score * (1 + recency_boost)
            else:
                final_score = rrf_score

            # Apply filename boost if person name appears in filename
            # This helps when user asks about "Taylor's passport" and Taylor.md exists
            if query_person_names:
                file_name = result.get("file_name", "") or result.get("metadata", {}).get("file_name", "")
                file_name_lower = file_name.lower()
                for person_name in query_person_names:
                    if person_name in file_name_lower:
                        # Significant boost (2x) for exact person name match in filename
                        final_score *= 2.0
                        logger.debug(f"Filename boost applied: {file_name} contains {person_name}")
                        break

            result["hybrid_score"] = final_score
            result["rrf_score"] = rrf_score
            final_results.append(result)

        # Re-sort by final score and limit
        final_results.sort(key=lambda x: -x.get("hybrid_score", 0))

        # Deduplicate overlapping chunks (important with 20% overlap)
        final_results = deduplicate_overlapping_chunks(final_results)

        # Apply cross-encoder re-ranking if enabled and we have enough candidates
        if use_reranker and len(final_results) > top_k:
            try:
                from api.services.reranker import get_reranker

                # Find protected indices for factual queries
                protected = find_protected_indices(
                    expanded_query,
                    final_results,
                    max_protected=3
                )

                reranker = get_reranker()
                final_results = reranker.rerank(
                    query=expanded_query,
                    results=final_results,
                    top_k=top_k,
                    content_key="content",
                    protected_indices=protected if protected else None
                )

                if protected:
                    logger.debug(f"Protected {len(protected)} results from reranking")
                logger.debug(f"Re-ranked {len(final_results)} results with cross-encoder")
            except Exception as e:
                logger.warning(f"Re-ranking failed, using hybrid scores: {e}")
                final_results = final_results[:top_k]
        else:
            final_results = final_results[:top_k]

        return final_results


# Singleton instance
_hybrid_search_instance: Optional[HybridSearch] = None


def get_hybrid_search() -> HybridSearch:
    """Get the singleton HybridSearch instance."""
    global _hybrid_search_instance
    if _hybrid_search_instance is None:
        _hybrid_search_instance = HybridSearch()
    return _hybrid_search_instance
