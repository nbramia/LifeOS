"""
Tests for ChromaDB vector store integration.

Requires ChromaDB server running on localhost:8001.
Uses a separate test collection to avoid polluting production data.

NOTE: Imports are deferred to avoid loading heavy dependencies (ChromaDB,
sentence-transformers) during pytest collection, which would slow down unit tests.
"""
import pytest

# These tests require ChromaDB server (slow)
pytestmark = pytest.mark.slow

# Standard library imports are fine at module level (lightweight)
from datetime import datetime


class TestVectorStore:
    """Test ChromaDB vector store operations."""

    @pytest.fixture
    def vector_store(self):
        """Create vector store instance with test collection."""
        from api.services.vectorstore import VectorStore

        # Use a test collection name to avoid polluting production data
        store = VectorStore(collection_name="lifeos_vault_test")
        # Clear any existing test data
        try:
            store._client.delete_collection("lifeos_vault_test")
            store._collection = store._client.get_or_create_collection(
                name="lifeos_vault_test",
                metadata={"hnsw:space": "cosine"}
            )
        except Exception:
            pass
        yield store
        # Cleanup: delete test collection
        try:
            store._client.delete_collection("lifeos_vault_test")
        except Exception:
            pass

    def test_add_document_chunks(self, vector_store):
        """Should add document chunks to the store."""
        chunks = [
            {
                "content": "This is chunk one about budget planning.",
                "chunk_index": 0
            },
            {
                "content": "This is chunk two about team meetings.",
                "chunk_index": 1
            }
        ]
        metadata = {
            "file_path": "/vault/test.md",
            "file_name": "test.md",
            "modified_date": datetime.now().isoformat(),
            "note_type": "Personal",
            "people": ["John"],
            "tags": ["test"]
        }

        vector_store.add_document(chunks, metadata)

        # Verify chunks were added
        count = vector_store.get_document_count()
        assert count == 2

    def test_search_returns_relevant_results(self, vector_store):
        """Should return relevant chunks for search query."""
        # Add some documents
        doc1_chunks = [{"content": "Meeting about Q1 budget review.", "chunk_index": 0}]
        doc2_chunks = [{"content": "Recipe for chocolate cake.", "chunk_index": 0}]

        vector_store.add_document(doc1_chunks, {
            "file_path": "/vault/budget.md",
            "file_name": "budget.md",
            "modified_date": datetime.now().isoformat(),
            "note_type": "Work",
            "people": [],
            "tags": ["finance"]
        })

        vector_store.add_document(doc2_chunks, {
            "file_path": "/vault/recipe.md",
            "file_name": "recipe.md",
            "modified_date": datetime.now().isoformat(),
            "note_type": "Personal",
            "people": [],
            "tags": ["cooking"]
        })

        # Search for budget-related content
        results = vector_store.search("quarterly budget planning", top_k=2)

        assert len(results) >= 1
        # Budget doc should be most relevant
        assert "budget" in results[0]["file_name"].lower()

    def test_search_with_filters(self, vector_store):
        """Should filter results by metadata."""
        # Add work and personal docs
        vector_store.add_document(
            [{"content": "Work meeting notes.", "chunk_index": 0}],
            {
                "file_path": "/vault/work.md",
                "file_name": "work.md",
                "modified_date": datetime.now().isoformat(),
                "note_type": "Work",
                "people": ["Alex"],
                "tags": ["meeting"]
            }
        )

        vector_store.add_document(
            [{"content": "Personal journal entry.", "chunk_index": 0}],
            {
                "file_path": "/vault/personal.md",
                "file_name": "personal.md",
                "modified_date": datetime.now().isoformat(),
                "note_type": "Personal",
                "people": [],
                "tags": ["journal"]
            }
        )

        # Filter by note_type
        results = vector_store.search(
            "notes",
            top_k=10,
            filters={"note_type": "Work"}
        )

        assert len(results) >= 1
        assert all(r["note_type"] == "Work" for r in results)

    def test_delete_document(self, vector_store):
        """Should delete all chunks for a document."""
        chunks = [
            {"content": "Chunk 1", "chunk_index": 0},
            {"content": "Chunk 2", "chunk_index": 1},
        ]

        vector_store.add_document(chunks, {
            "file_path": "/vault/todelete.md",
            "file_name": "todelete.md",
            "modified_date": datetime.now().isoformat(),
            "note_type": "Personal",
            "people": [],
            "tags": []
        })

        # Verify added
        assert vector_store.get_document_count() == 2

        # Delete
        vector_store.delete_document("/vault/todelete.md")

        # Verify deleted
        assert vector_store.get_document_count() == 0

    def test_update_document(self, vector_store):
        """Should update document by deleting old chunks and adding new."""
        # Add initial version
        vector_store.add_document(
            [{"content": "Original content.", "chunk_index": 0}],
            {
                "file_path": "/vault/update.md",
                "file_name": "update.md",
                "modified_date": datetime.now().isoformat(),
                "note_type": "Personal",
                "people": [],
                "tags": []
            }
        )

        # Update with new content
        vector_store.update_document(
            [{"content": "Updated content with more info.", "chunk_index": 0}],
            {
                "file_path": "/vault/update.md",
                "file_name": "update.md",
                "modified_date": datetime.now().isoformat(),
                "note_type": "Personal",
                "people": [],
                "tags": []
            }
        )

        # Search should find new content
        results = vector_store.search("Updated content", top_k=1)
        assert len(results) == 1
        assert "Updated" in results[0]["content"]

    def test_metadata_preserved(self, vector_store):
        """All metadata fields should be preserved and returned."""
        chunks = [{"content": "Test content.", "chunk_index": 0}]
        metadata = {
            "file_path": "/vault/meta.md",
            "file_name": "meta.md",
            "modified_date": "2025-01-05T10:00:00",
            "note_type": "Work",
            "people": ["Alex", "Sarah"],
            "tags": ["meeting", "budget"]
        }

        vector_store.add_document(chunks, metadata)
        results = vector_store.search("Test", top_k=1)

        assert len(results) == 1
        result = results[0]
        assert result["file_path"] == "/vault/meta.md"
        assert result["file_name"] == "meta.md"
        assert result["note_type"] == "Work"
        # People and tags stored as string, need to handle
        assert "Alex" in str(result.get("people", ""))

    def test_no_duplicates_on_reindex(self, vector_store):
        """Re-indexing same document shouldn't create duplicates."""
        chunks = [{"content": "Unique content.", "chunk_index": 0}]
        metadata = {
            "file_path": "/vault/nodupe.md",
            "file_name": "nodupe.md",
            "modified_date": datetime.now().isoformat(),
            "note_type": "Personal",
            "people": [],
            "tags": []
        }

        # Add same document twice using update
        vector_store.update_document(chunks, metadata)
        vector_store.update_document(chunks, metadata)

        # Should only have 1 chunk
        assert vector_store.get_document_count() == 1
