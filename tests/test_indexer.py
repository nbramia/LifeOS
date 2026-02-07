"""
Tests for the indexer service.
P1.1 Acceptance Criteria:
- Indexer starts and watches vault folder without errors
- New file creation triggers indexing within 5 seconds
- File modification triggers re-indexing within 5 seconds
- File deletion removes chunks from ChromaDB
- Indexer recovers gracefully from restart (no duplicate chunks)

NOTE: Imports are deferred to avoid loading heavy dependencies (ChromaDB,
sentence-transformers) during pytest collection, which would slow down unit tests.
"""
import pytest

# These tests require ChromaDB and file watching (slow)
pytestmark = pytest.mark.slow

# Standard library imports are fine at module level (lightweight)
import tempfile
import time
import os
from pathlib import Path
from datetime import datetime


class TestIndexerService:
    """Test the main indexer service."""

    @pytest.fixture
    def temp_vault(self):
        """Create temporary vault directory with test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir) / "vault"
            vault_path.mkdir()

            # Create some test files
            (vault_path / "test1.md").write_text("""---
tags: [test]
type: note
---

# Test Note 1

This is a test note about budget planning.
""")
            (vault_path / "test2.md").write_text("""---
tags: [meeting]
type: meeting
people: [Alex]
---

# Meeting Notes

## Discussion
We talked about Q1 targets.

## Action Items
- [ ] Review budget
""")
            yield vault_path

    @pytest.fixture
    def temp_db(self):
        """Create temporary database directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def indexer(self, temp_vault, temp_db):
        """Create indexer instance."""
        from api.services.indexer import IndexerService

        indexer = IndexerService(
            vault_path=str(temp_vault),
            db_path=str(temp_db)
        )
        yield indexer
        indexer.stop()

    def test_indexes_markdown_files(self, indexer, temp_vault):
        """Should index all markdown files in vault."""
        # Run initial index
        indexed = indexer.index_all()

        assert indexed >= 2  # We created 2 test files

    def test_extracts_metadata_from_frontmatter(self, indexer, temp_vault):
        """Should extract metadata from YAML frontmatter."""
        indexer.index_all()

        # Search for content
        results = indexer.vector_store.search("budget", top_k=5)
        assert len(results) >= 1

        # Check metadata
        result = results[0]
        assert "file_path" in result
        assert result["file_name"].endswith(".md")

    def test_chunks_granola_notes_by_headers(self, indexer, temp_vault):
        """Granola/meeting notes should be chunked by headers."""
        # Create a Granola-style note
        granola_note = temp_vault / "Granola" / "meeting.md"
        granola_note.parent.mkdir(exist_ok=True)
        granola_note.write_text("""---
granola_id: abc123
type: meeting
---

# Team Standup

## Attendees
- John
- Alex

## Updates
Everyone shared their progress.

## Action Items
- [ ] John: Send report
""")

        indexer.index_all()

        # Search for action items specifically
        results = indexer.vector_store.search("send report", top_k=5)
        assert len(results) >= 1

    def test_infers_note_type_from_folder(self, indexer, temp_vault):
        """Should infer note_type from folder path."""
        # Create files in different folders
        (temp_vault / "Personal").mkdir()
        (temp_vault / "Work" / "ML").mkdir(parents=True)

        (temp_vault / "Personal" / "journal.md").write_text("""# My Journal

Today was a good day.
""")
        (temp_vault / "Work" / "ML" / "meeting.md").write_text("""# Work Meeting

Discussed strategy.
""")

        indexer.index_all()

        # Search and check note_type
        personal_results = indexer.vector_store.search(
            "journal good day",
            filters={"note_type": "Personal"}
        )
        assert len(personal_results) >= 1

    def test_handles_file_without_frontmatter(self, indexer, temp_vault):
        """Should handle files without YAML frontmatter."""
        (temp_vault / "plain.md").write_text("""# Simple Note

No frontmatter here, just content.
""")

        indexed = indexer.index_all()
        assert indexed >= 1

        results = indexer.vector_store.search("simple note content")
        assert len(results) >= 1

    def test_skips_non_markdown_files(self, indexer, temp_vault):
        """Should skip non-markdown files."""
        (temp_vault / "image.png").write_bytes(b"fake image data")
        (temp_vault / "data.json").write_text('{"key": "value"}')

        indexed = indexer.index_all()
        # Should only index .md files
        assert indexed == 2  # Original test files only

    def test_recovers_from_restart_no_duplicates(self, indexer, temp_vault, temp_db):
        """Re-indexing should not create duplicates."""
        # First index
        indexer.index_all()
        count1 = indexer.vector_store.get_document_count()

        # Create new indexer (simulating restart)
        indexer2 = IndexerService(
            vault_path=str(temp_vault),
            db_path=str(temp_db)
        )

        # Re-index
        indexer2.index_all()
        count2 = indexer2.vector_store.get_document_count()

        # Should have same count (no duplicates)
        assert count1 == count2
        indexer2.stop()


class TestFileWatcher:
    """Test file watching functionality."""

    @pytest.fixture
    def temp_vault(self):
        """Create temporary vault directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir) / "vault"
            vault_path.mkdir()
            yield vault_path

    @pytest.fixture
    def temp_db(self):
        """Create temporary database directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def indexer(self, temp_vault, temp_db):
        """Create indexer with file watching."""
        indexer = IndexerService(
            vault_path=str(temp_vault),
            db_path=str(temp_db)
        )
        yield indexer
        indexer.stop()

    def test_detects_new_file_creation(self, indexer, temp_vault):
        """New file should be indexed automatically."""
        # Start watching
        indexer.start_watching()
        time.sleep(0.5)  # Let watcher initialize

        # Create new file
        new_file = temp_vault / "new_note.md"
        new_file.write_text("""# Brand New Note

This is freshly created content about testing.
""")

        # Wait for indexing (should be within 5 seconds)
        time.sleep(3)

        # Search for the new content
        results = indexer.vector_store.search("freshly created testing")
        assert len(results) >= 1
        assert "new_note.md" in results[0]["file_name"]

    def test_detects_file_modification(self, indexer, temp_vault):
        """Modified file should be re-indexed."""
        # Create initial file
        test_file = temp_vault / "modify_test.md"
        test_file.write_text("# Original\n\nOriginal content.")

        # Index it
        indexer.index_file(str(test_file))

        # Start watching
        indexer.start_watching()
        time.sleep(0.5)

        # Modify the file
        test_file.write_text("# Modified\n\nCompletely different updated content.")

        # Wait for re-indexing
        time.sleep(3)

        # Search should find new content
        results = indexer.vector_store.search("different updated content")
        assert len(results) >= 1

    def test_detects_file_deletion(self, indexer, temp_vault):
        """Deleted file should be removed from index."""
        # Create and index file
        test_file = temp_vault / "to_delete.md"
        test_file.write_text("# To Delete\n\nThis will be deleted.")
        indexer.index_file(str(test_file))

        # Verify it's indexed
        initial_results = indexer.vector_store.search("will be deleted")
        assert len(initial_results) >= 1

        # Start watching
        indexer.start_watching()
        time.sleep(1)  # Give watcher more time to start

        # Delete the file
        test_file.unlink()

        # Wait for deletion to process (debounce + processing)
        time.sleep(4)

        # Search should not find the content
        results = indexer.vector_store.search("will be deleted")
        deleted_results = [r for r in results if "to_delete.md" in r["file_name"]]
        assert len(deleted_results) == 0
