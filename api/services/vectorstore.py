"""
ChromaDB vector store service for LifeOS.

Connects to ChromaDB server via HTTP for thread-safe concurrent access.

NOTE: Heavy dependencies (chromadb, embeddings) are imported lazily
to speed up pytest collection for unit tests.
"""
from typing import Optional, Any, TYPE_CHECKING
from datetime import datetime
import json
import math

from config.settings import settings

if TYPE_CHECKING:
    import chromadb


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
        recency_weight: float = 0.6
    ) -> list[dict]:
        """
        Search for similar chunks with heavy recency bias.

        Args:
            query: Search query text
            top_k: Number of results to return
            filters: Optional metadata filters
            recency_weight: Weight for recency vs semantic similarity (0.6 = 60% recency)

        Returns:
            List of result dicts with content, metadata, and score
        """
        # Generate query embedding
        query_embedding = self._embedding_service.embed_text(query)

        # Build where clause for filters
        where = None
        if filters:
            where = {}
            for key, value in filters.items():
                if value is not None:
                    where[key] = value

        # Fetch more results to re-rank with recency bias
        fetch_count = min(top_k * 5, 100)

        # Query collection
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=fetch_count,
            where=where if where else None,
            include=["documents", "metadatas", "distances"]
        )

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
                    **metadata
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
