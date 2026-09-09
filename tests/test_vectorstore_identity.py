"""Fast, no-live-server regression coverage for VectorStore.search()'s
result identity.

tests/test_vectorstore.py exercises VectorStore against a real ChromaDB
server (marked ``slow``) — this file instead fakes the two collaborators
``search()`` actually calls (``self._collection.query(...)`` and
``self._embedding_service.embed_text(...)``), bypassing ``__init__``
entirely, so it runs as a fast unit test while still exercising the real
production ``search()`` method body (not a hybrid_search-level mock of
VectorStore itself, which would never catch a regression here).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class _FakeCollection:
    """Mimics the one Chroma collection method VectorStore.search() calls."""

    def __init__(self, doc_id: str, document: str, metadata: dict, distance: float = 0.1):
        self._doc_id = doc_id
        self._document = document
        self._metadata = metadata
        self._distance = distance

    def query(self, query_embeddings, n_results, where, include):
        return {
            "ids": [[self._doc_id]],
            "documents": [[self._document]],
            "metadatas": [[self._metadata]],
            "distances": [[self._distance]],
        }


class _FakeEmbeddingService:
    def embed_text(self, text: str):
        return [0.0] * 4  # the fake collection ignores the embedding's content


def _bare_vector_store(collection: _FakeCollection):
    """A VectorStore with only the two attributes search() reads, built
    without running __init__ (which would try to connect to a real
    ChromaDB HTTP server)."""
    from api.services.vectorstore import VectorStore

    store = VectorStore.__new__(VectorStore)
    store._collection = collection
    store._embedding_service = _FakeEmbeddingService()
    return store


def test_search_returns_the_actual_stored_chroma_id():
    """search() must put the real Chroma document id on its result dict —
    callers (hybrid_search.py) key fusion matching off ``result.get("id")``."""
    doc_id = "/vault/Budget Review.md::0"
    store = _bare_vector_store(_FakeCollection(
        doc_id=doc_id,
        document="Q4 budget planning meeting",
        metadata={"file_path": "/vault/Budget Review.md", "file_name": "Budget Review.md", "chunk_index": 0},
    ))

    results = store.search("budget")

    assert len(results) == 1
    assert results[0]["id"] == doc_id


def test_extra_chunk_metadata_named_id_cannot_overwrite_the_actual_stored_identity():
    """add_document() copies arbitrary extra scalar chunk-level metadata
    through verbatim (e.g. channel_id/timestamp for Slack) — if a chunk
    happened to carry a field literally named "id", spreading that
    metadata into the result dict AFTER setting the real Chroma id would
    silently replace the authoritative identity with whatever unrelated
    value that metadata field held."""
    doc_id = "/vault/Budget Review.md::0"
    store = _bare_vector_store(_FakeCollection(
        doc_id=doc_id,
        document="Q4 budget planning meeting",
        metadata={
            "file_path": "/vault/Budget Review.md",
            "file_name": "Budget Review.md",
            "chunk_index": 0,
            "id": "not-the-real-id",  # an extra metadata field that collides with the reserved key
        },
    ))

    results = store.search("budget")

    assert results[0]["id"] == doc_id, "the actual stored Chroma id must win over any same-named metadata field"
