"""
ChromaDB vector store service for LifeOS.

Connects to ChromaDB server via HTTP for thread-safe concurrent access.

NOTE: Heavy dependencies (chromadb, embeddings) are imported lazily
to speed up pytest collection for unit tests.
"""
from typing import Optional
from datetime import date, datetime
from pathlib import Path
import json
import math
import os

from config.settings import settings

# note_type values written by non-vault sources (api/services/calendar_indexer.py,
# api/services/slack_indexer.py). These index real content under a relative
# pseudo-path (e.g. "calendar/<event-id>"), not a vault document, so they must
# never be mistaken for a vault-root sample.
_NON_VAULT_NOTE_TYPES = ["calendar_event", "slack_message"]

# Chroma has no metadata operator for "list contains", so each human tag is
# also stored as a boolean key ``tag:<lowercased tag>`` a ``where`` can match.
TAG_KEY_PREFIX = "tag:"

# A ``file_path $in`` list is split into batches of this many paths so no
# single query carries an oversized parameter list.
FILE_PATH_BATCH = 2000
# ``modified_day`` value of a chunk whose ``modified_date`` cannot be parsed;
# date windows keep such chunks, matching the undated-passes contract.
UNDATED_DAY = -1_000_000


def is_people_tag(tag: str) -> bool:
    """``people/<slug>`` tags name a person; they are resolved to the ``people``
    metadata and are not part of the tag vocabulary."""
    return tag.strip().lstrip("#").casefold().startswith("people/")


def tag_key(tag: str) -> str:
    """Metadata key that marks a chunk as carrying ``tag``."""
    return TAG_KEY_PREFIX + tag.strip().lstrip("#").lower()


def modified_day(date_str: Optional[str]) -> int:
    """Days since 1970-01-01 of a ``YYYY-MM-DD`` (or ISO timestamp) string;
    ``UNDATED_DAY`` when it does not parse."""
    try:
        return (datetime.strptime(str(date_str or "")[:10], "%Y-%m-%d").date() - date(1970, 1, 1)).days
    except ValueError:
        return UNDATED_DAY


def _date_where(date_from: Optional[str], date_to: Optional[str]) -> Optional[dict]:
    """Chroma ``where`` admitting chunks inside the inclusive window, plus
    undated chunks. None when no bound is given (or a bound does not parse)."""
    bounds = []
    if date_from and modified_day(date_from) != UNDATED_DAY:
        bounds.append({"modified_day": {"$gte": modified_day(date_from)}})
    if date_to and modified_day(date_to) != UNDATED_DAY:
        bounds.append({"modified_day": {"$lte": modified_day(date_to)}})
    if not bounds:
        return None
    window = bounds[0] if len(bounds) == 1 else {"$and": bounds}
    return {"$or": [window, {"modified_day": UNDATED_DAY}]}


def _all_of(conds: list[dict]) -> Optional[dict]:
    """One Chroma ``where`` requiring every clause (None when there are none)."""
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {"$and": conds}


class VectorStore:
    """ChromaDB-backed vector store for document chunks."""

    def __init__(
        self,
        collection_name: str = "lifeos_vault",
        server_url: str = None
    ):
        """
        Initialize vector store.

        Args:
            collection_name: Name of the collection
            server_url: ChromaDB server URL (default: from settings)
        """
        import chromadb
        from chromadb.config import Settings

        self.collection_name = collection_name
        self.server_url = server_url or settings.chroma_url

        # Connect to ChromaDB server via HTTP
        try:
            self._client = chromadb.HttpClient(
                host=self._parse_host(self.server_url),
                port=self._parse_port(self.server_url),
                settings=Settings(anonymized_telemetry=False)
            )

            # Get or create collection
            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"}
            )

            # Mark ChromaDB as healthy on successful connection
            from api.services.service_health import mark_service_healthy
            mark_service_healthy("chromadb")
        except Exception as e:
            # Mark ChromaDB as failed
            from api.services.service_health import mark_service_failed, Severity
            mark_service_failed("chromadb", str(e), Severity.CRITICAL)
            raise

        # Get embedding service (lazy import)
        from api.services.embeddings import get_embedding_service
        self._embedding_service = get_embedding_service()

    def _parse_host(self, url: str) -> str:
        """Extract host from URL."""
        return url.replace("http://", "").replace("https://", "").split(":")[0]

    def _parse_port(self, url: str) -> int:
        """Extract port from URL."""
        parts = url.replace("http://", "").replace("https://", "").split(":")
        return int(parts[1]) if len(parts) > 1 else 8000

    def add_document(
        self,
        chunks: list[dict],
        metadata: dict
    ) -> None:
        """
        Add document chunks to the store.

        Args:
            chunks: List of chunk dicts with 'content' and 'chunk_index'
            metadata: Document metadata (file_path, file_name, etc.)
        """
        if not chunks:
            return

        ids = []
        embeddings = []
        documents = []
        metadatas = []

        # Generate embeddings for all chunks
        contents = [c["content"] for c in chunks]
        chunk_embeddings = self._embedding_service.embed_texts(contents)

        for i, chunk in enumerate(chunks):
            # Create unique ID: file_path + chunk_index
            chunk_id = f"{metadata['file_path']}::{chunk['chunk_index']}"
            ids.append(chunk_id)

            embeddings.append(chunk_embeddings[i])
            documents.append(chunk["content"])

            # Prepare metadata - ChromaDB needs flat values
            chunk_meta = {
                "file_path": metadata["file_path"],
                "file_name": metadata["file_name"],
                "modified_date": metadata.get("modified_date", ""),
                "modified_day": modified_day(metadata.get("modified_date")),
                "note_type": metadata.get("note_type", ""),
                "chunk_index": chunk["chunk_index"],
                # Store lists as JSON strings
                "people": json.dumps(metadata.get("people", [])),
                "tags": json.dumps(metadata.get("tags", []))
            }
            # Copy any extra chunk-level metadata (e.g., channel_id, timestamp for Slack)
            for key, value in chunk.items():
                if key not in ("content", "chunk_index") and key not in chunk_meta:
                    # ChromaDB only accepts str, int, float, bool
                    if isinstance(value, (str, int, float, bool)):
                        chunk_meta[key] = value
                    elif value is None:
                        chunk_meta[key] = ""
            for tag in metadata.get("tags") or []:
                if not is_people_tag(str(tag)):
                    chunk_meta.setdefault(tag_key(str(tag)), True)
            metadatas.append(chunk_meta)

        # Add to collection
        self._collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas
        )

    def _calculate_recency_score(self, modified_date: str, note_type: str = "") -> float:
        """
        Calculate recency score with heavy bias toward recent documents.

        Returns score between 0 and 1, where:
        - Documents from last 30 days: 0.9-1.0
        - Documents from last 90 days: 0.7-0.9
        - Documents from last year: 0.4-0.7
        - Documents older than 1 year: 0.0-0.4 (exponential decay)
        - ML folder content: Always boosted (current job)
        - Undated files: Neutral score (0.5)
        """
        # ML folder = current job, always highly relevant
        if note_type == "ML":
            return 0.95

        # No date in filename = undated, give neutral score
        if not modified_date:
            return 0.5

        try:
            # Parse date (supports various formats)
            date = None
            for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y%m%d"]:
                try:
                    date = datetime.strptime(modified_date[:10], fmt)
                    break
                except ValueError:
                    continue

            if not date:
                return 0.5  # Couldn't parse date, neutral score

            days_old = (datetime.now() - date).days

            if days_old <= 0:
                return 1.0
            elif days_old <= 30:
                return 0.9 + (0.1 * (1 - days_old / 30))
            elif days_old <= 90:
                return 0.7 + (0.2 * (1 - (days_old - 30) / 60))
            elif days_old <= 365:
                return 0.4 + (0.3 * (1 - (days_old - 90) / 275))
            else:
                # Exponential decay for older documents
                years_old = days_old / 365
                return max(0.05, 0.4 * math.exp(-0.5 * (years_old - 1)))

        except Exception:
            return 0.5

    def search(
        self,
        query: str,
        top_k: int = 20,
        filters: Optional[dict] = None,
        recency_weight: float = 0.6,
        file_paths: Optional[list[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> list[dict]:
        """
        Search for similar chunks with heavy recency bias.

        Args:
            query: Search query text
            top_k: Number of results to return
            filters: Optional metadata filters
            recency_weight: Weight for recency vs semantic similarity (0.6 = 60% recency)
            file_paths: When given, only chunks of these files are candidates
                (pre-filter inside the vector query); an empty list matches
                nothing. Large lists run as batched queries whose candidates
                are merged by distance.
            date_from / date_to: Inclusive ``YYYY-MM-DD`` window, applied as a
                ``modified_day`` clause inside the vector query; chunks
                without a parseable date pass.

        Returns:
            List of result dicts with content, metadata, and score
        """
        # Generate query embedding
        query_embedding = self._embedding_service.embed_text(query)

        # Build where clause for filters
        conds = [{k: v} for k, v in (filters or {}).items() if v is not None]
        window = _date_where(date_from, date_to)
        if window:
            conds.append(window)

        # Fetch more results to re-rank with recency bias
        fetch_count = min(top_k * 5, 100)

        # Query collection
        if file_paths is None:
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=fetch_count,
                where=_all_of(conds),
                include=["documents", "metadatas", "distances"]
            )
        else:
            results = self._query_restricted(query_embedding, fetch_count, conds, file_paths)

        # Format and score results
        formatted = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                metadata = results["metadatas"][0][i]
                semantic_score = 1 - results["distances"][0][i]

                # Calculate recency score
                recency_score = self._calculate_recency_score(
                    metadata.get("modified_date", ""),
                    metadata.get("note_type", "")
                )

                # Combined score: heavily weighted toward recency
                combined_score = (
                    (1 - recency_weight) * semantic_score +
                    recency_weight * recency_score
                )

                result = {
                    "content": results["documents"][0][i],
                    "score": combined_score,
                    "semantic_score": semantic_score,
                    "recency_score": recency_score,
                    **metadata,
                    # Set AFTER **metadata: add_document() permits arbitrary
                    # extra chunk keys through, so a chunk with its own "id"
                    # field must not overwrite the real stored Chroma id.
                    "id": doc_id,
                }
                # Parse JSON fields
                if "people" in result and isinstance(result["people"], str):
                    try:
                        result["people"] = json.loads(result["people"])
                    except json.JSONDecodeError:
                        result["people"] = []
                if "tags" in result and isinstance(result["tags"], str):
                    try:
                        result["tags"] = json.loads(result["tags"])
                    except json.JSONDecodeError:
                        result["tags"] = []
                formatted.append(result)

        # Re-rank by combined score
        formatted.sort(key=lambda x: x["score"], reverse=True)

        return formatted[:top_k]

    def _query_restricted(
        self,
        query_embedding,
        fetch_count: int,
        conds: list[dict],
        file_paths: list[str],
    ) -> dict:
        """Nearest-neighbour query limited to chunks of ``file_paths``.

        The path restriction is a ``file_path $in`` clause inside the vector
        query (a pre-filter). Lists longer than ``FILE_PATH_BATCH`` run as one
        query per batch; each batch returns its own nearest ``fetch_count``
        and the union is cut back to the overall nearest ``fetch_count``, so
        the merged result equals a single unbounded query.
        """
        empty = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        rows = []
        for i in range(0, len(file_paths), FILE_PATH_BATCH):
            batch = file_paths[i:i + FILE_PATH_BATCH]
            batch_conds = [*conds, {"file_path": {"$in": batch}}]
            res = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=fetch_count,
                where=_all_of(batch_conds),
                include=["documents", "metadatas", "distances"],
            )
            if res["ids"] and res["ids"][0]:
                rows.extend(zip(
                    res["distances"][0], res["ids"][0],
                    res["documents"][0], res["metadatas"][0],
                ))
        if not rows:
            return empty
        rows.sort(key=lambda r: r[0])
        rows = rows[:fetch_count]
        return {
            "ids": [[r[1] for r in rows]],
            "documents": [[r[2] for r in rows]],
            "metadatas": [[r[3] for r in rows]],
            "distances": [[r[0] for r in rows]],
        }

    def backfill_search_keys(self, batch_size: int = 500) -> int:
        """Write the search metadata keys chunks are missing.

        Scans every chunk. A chunk gets the ``tag:<name>`` boolean keys for its
        non-empty JSON ``tags`` when it carries none, and ``modified_day`` when
        it lacks it. Only metadata is written (``update`` with the full
        existing metadata plus the new keys); documents and embeddings are
        untouched, and an already-complete chunk is not written. Returns the
        number of chunks updated; a second run returns 0.
        """
        updated = 0
        offset = 0
        while True:
            page = self._collection.get(include=["metadatas"], limit=batch_size, offset=offset)
            ids = page["ids"]
            if not ids:
                return updated
            offset += len(ids)
            write_ids, write_metas = [], []
            for chunk_id, meta in zip(ids, page["metadatas"]):
                meta = meta or {}
                add: dict = {}
                if "modified_day" not in meta:
                    add["modified_day"] = modified_day(meta.get("modified_date"))
                if not any(k.startswith(TAG_KEY_PREFIX) for k in meta):
                    try:
                        tags = json.loads(meta.get("tags") or "[]")
                    except (TypeError, ValueError):
                        tags = []
                    add.update({tag_key(str(t)): True for t in tags if str(t).strip() and not is_people_tag(str(t))})
                if add:
                    write_ids.append(chunk_id)
                    write_metas.append({**meta, **add})
            if write_ids:
                self._collection.update(ids=write_ids, metadatas=write_metas)
                updated += len(write_ids)

    def file_paths_with_people(self, file_paths, names: list[str]) -> set[str]:
        """Subset of ``file_paths`` having a chunk whose ``people`` list holds one
        of ``names`` as a whole value (case-insensitive, no stemming)."""
        wanted = {n.strip().casefold() for n in names if n and n.strip()}
        paths = sorted(file_paths)
        found: set[str] = set()
        for i in range(0, len(paths), FILE_PATH_BATCH):
            batch = paths[i:i + FILE_PATH_BATCH]
            res = self._collection.get(
                where={"file_path": {"$in": batch}}, include=["metadatas"]
            )
            for meta in res["metadatas"] or []:
                try:
                    people = json.loads((meta or {}).get("people") or "[]")
                except (TypeError, ValueError):
                    continue
                if any(str(p).strip().casefold() in wanted for p in people):
                    found.add(meta["file_path"])
        return found

    def file_paths_matching(self, where: Optional[dict] = None) -> set[str]:
        """Distinct file paths of chunks matching a Chroma ``where`` (all when None).

        Reads chunk ids only (``{path}::{chunk}``), never documents or vectors."""
        res = self._collection.get(where=where, include=[])
        return {i.rpartition("::")[0] if "::" in i else i for i in res["ids"]}

    def delete_document(self, file_path: str) -> None:
        """
        Delete all chunks for a document.

        Args:
            file_path: Path of the document to delete
        """
        # Find all chunks with this file_path
        results = self._collection.get(
            where={"file_path": file_path},
            include=[]
        )

        if results["ids"]:
            self._collection.delete(ids=results["ids"])

    def update_document(
        self,
        chunks: list[dict],
        metadata: dict
    ) -> None:
        """
        Update a document by deleting old chunks and adding new ones.

        Args:
            chunks: New chunks
            metadata: Updated metadata
        """
        # Delete existing chunks
        self.delete_document(metadata["file_path"])
        # Add new chunks
        self.add_document(chunks, metadata)

    def get_document_count(self) -> int:
        """Get total number of chunks in the store."""
        return self._collection.count()

    def get_all_file_paths(self) -> set[str]:
        """Get set of all indexed file paths."""
        results = self._collection.get(include=["metadatas"])
        paths = set()
        if results["metadatas"]:
            for meta in results["metadatas"]:
                if meta and "file_path" in meta:
                    paths.add(meta["file_path"])
        return paths

    def sample_file_paths(self, limit: int = 5) -> list[str]:
        """Return up to `limit` distinct indexed *vault* file paths, without
        scanning the whole collection. Used by the vault_search
        health check's vault-root sanity check — `get_all_file_paths()`
        above is the right tool when every path is actually needed, but is
        too expensive to call on every health-check request against a large
        vault.

        The collection also holds non-vault sources (calendar events, Slack
        messages) indexed under relative pseudo-paths, not real vault
        documents. On a real vault these can dominate the front of insertion
        order (e.g. thousands of calendar-event rows), so a plain unfiltered
        fetch can return nothing but non-vault rows — every one of them
        trivially "outside the vault root" and a false `degraded`. Push the
        exclusion down to ChromaDB via `where` so the
        fetch still targets real vault rows regardless of how many non-vault
        rows precede them, instead of over-fetching further and further to
        try to skip past them client-side.

        Each file is indexed as several chunks (one row per chunk, all
        sharing one `file_path`), so a raw `limit`-sized fetch risks
        returning the same one or two files repeatedly. Over-fetch a bit
        and dedupe to `file_path` so the sample actually spans up to
        `limit` distinct files. At the health check's default `limit=50`
        this fetches at most 250 rows — still a small, bounded read
        regardless of collection size (250 rows out of 45k+ on a real vault
        is well under 1%), not a scan.
        """
        results = self._collection.get(
            limit=limit * 5,
            where={"note_type": {"$nin": _NON_VAULT_NOTE_TYPES}},
            include=["metadatas"],
        )
        paths = []
        seen = set()
        for meta in results.get("metadatas") or []:
            if not meta or "file_path" not in meta:
                continue
            path = meta["file_path"]
            # Defense in depth: vault documents always store an absolute,
            # resolved path (indexer.py: `str(path.resolve())`), while every
            # non-vault source uses a relative pseudo-path or doc id. This
            # catches any future non-vault source we haven't added to
            # `_NON_VAULT_NOTE_TYPES` above, without needing to keep the two
            # lists in lockstep.
            if not os.path.isabs(path):
                continue
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
            if len(paths) >= limit:
                break
        return paths


def sample_paths_match_vault_root(
    paths: "list[str] | set[str]", vault_root: Path
) -> "tuple[bool, list[str]]":
    """Classify a sample of indexed file paths against the configured vault
    root. `file_path` metadata is always stored as `str(path.resolve())`
    (see indexer.py), so a resolved-prefix check is sufficient — no need to
    touch the filesystem for paths that don't currently exist on disk (`Path.is_relative_to`
    does no I/O; only `vault_root.resolve()` below might, and that's on the
    live configured root, not on the — possibly vanished — indexed paths).

    Returns `(all_match, mismatched_paths)`. An empty `paths` sample trivially
    matches (nothing to contradict "healthy") — the caller is responsible for
    deciding whether an empty sample itself is worth reporting.
    """
    root = vault_root.resolve()
    mismatched = []
    for p in paths:
        try:
            if not Path(p).is_relative_to(root):
                mismatched.append(p)
        except (TypeError, ValueError):
            # Not a usable path string at all — treat as a mismatch rather
            # than silently skipping it, since that's exactly the kind of
            # drift this check exists to surface.
            mismatched.append(p)
    return (not mismatched, mismatched)


# Singleton instance
_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """
    Get or create the VectorStore singleton.

    Returns:
        VectorStore instance
    """
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
