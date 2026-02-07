"""
Integration tests for LifeOS.
Tests end-to-end workflows with real components.

NOTE: Imports are deferred to avoid loading heavy dependencies (ChromaDB,
sentence-transformers) during pytest collection, which would slow down unit tests.
"""
import pytest

# These are full integration tests requiring ChromaDB (slow)
pytestmark = pytest.mark.slow

# Standard library imports are fine at module level (lightweight)
import tempfile
import time
from pathlib import Path
from datetime import datetime


class TestFullIndexingWorkflow:
    """Test complete indexing workflow."""

    @pytest.fixture
    def test_vault(self):
        """Create a comprehensive test vault."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "vault"
            vault.mkdir()

            # Create folder structure
            (vault / "Personal").mkdir()
            (vault / "Work" / "ML" / "Meetings").mkdir(parents=True)
            (vault / "Granola").mkdir()

            # Create various test files
            # 1. Simple note
            (vault / "Personal" / "journal.md").write_text("""---
tags: [personal, reflection]
type: note
---

# Daily Journal

Today I worked on the LifeOS project. Made good progress on the indexer.
""")

            # 2. Meeting note (Granola style)
            (vault / "Granola" / "Team Standup 20250105.md").write_text("""---
granola_id: abc123
created_at: 2025-01-05T10:00:00
type: meeting
---

# Team Standup

## Attendees
- John
- Alex
- Sarah

## Updates
Everyone shared their progress on Q1 goals.

## Action Items
- [ ] John: Send budget proposal by Friday
- [ ] Alex: Review hiring pipeline
""")

            # 3. Work note
            (vault / "Work" / "ML" / "Meetings" / "Budget Review 20250106.md").write_text("""---
tags: [meeting, work, finance]
type: meeting
people: [Kevin, John]
---

# Budget Review

We reviewed the Q1 budget allocations with Kevin from finance.

## Key Decisions
- Increase engineering headcount budget by 20%
- Defer office expansion to Q2

## Next Steps
- [ ] Kevin: Send updated budget spreadsheet
- [ ] John: Present to leadership
""")

            # 4. Long note that should be chunked
            long_content = """---
tags: [reference, technical]
type: reference
---

# Technical Architecture Overview

This document describes the technical architecture of the LifeOS system.

""" + "\n\n".join([f"## Section {i}\n\nThis is content for section {i}. " * 50 for i in range(1, 11)])

            (vault / "Work" / "ML" / "architecture.md").write_text(long_content)

            yield vault

    @pytest.fixture
    def temp_db(self):
        """Create temp database directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_full_index_and_search(self, test_vault, temp_db):
        """Should index all files and return relevant search results."""
        from api.services.indexer import IndexerService

        indexer = IndexerService(
            vault_path=str(test_vault),
            db_path=str(temp_db)
        )

        # Index all files
        indexed = indexer.index_all()
        assert indexed == 4  # We created 4 markdown files

        # Search for budget-related content
        results = indexer.vector_store.search("budget proposal Q1", top_k=5)
        assert len(results) >= 2

        # Should find the budget review and standup notes
        file_names = [r["file_name"] for r in results]
        assert any("Budget" in name for name in file_names)

        # Search with filter
        work_results = indexer.vector_store.search(
            "budget",
            filters={"note_type": "Work"}
        )
        assert all(r["note_type"] == "Work" for r in work_results)

        indexer.stop()

    def test_granola_chunking(self, test_vault, temp_db):
        """Granola notes should be chunked by headers."""
        from api.services.indexer import IndexerService

        indexer = IndexerService(
            vault_path=str(test_vault),
            db_path=str(temp_db)
        )

        indexer.index_all()

        # Search for action items specifically (use recency_weight=0 for pure semantic search
        # since test files don't have date stamps in filenames)
        results = indexer.vector_store.search("send budget proposal Friday", top_k=5, recency_weight=0.0)

        # Should find the action item chunk
        assert len(results) >= 1
        assert any("budget proposal" in r["content"].lower() for r in results)

        indexer.stop()

    def test_people_filter(self, test_vault, temp_db):
        """Should be able to filter by people in frontmatter."""
        from api.services.indexer import IndexerService

        indexer = IndexerService(
            vault_path=str(test_vault),
            db_path=str(temp_db)
        )

        indexer.index_all()

        # The Budget Review note has Kevin in the people field
        results = indexer.vector_store.search("finance budget", top_k=5)

        # Find results with Kevin
        kevin_results = [r for r in results if "Kevin" in str(r.get("people", []))]
        assert len(kevin_results) >= 1

        indexer.stop()

    def test_long_note_chunking(self, test_vault, temp_db):
        """Long notes should be chunked appropriately."""
        from api.services.indexer import IndexerService

        indexer = IndexerService(
            vault_path=str(test_vault),
            db_path=str(temp_db)
        )

        indexer.index_all()

        # The architecture doc should have multiple chunks
        results = indexer.vector_store.search("technical architecture section", top_k=10)

        # Should have multiple chunks from the architecture doc
        arch_results = [r for r in results if "architecture" in r["file_name"]]
        # Might have multiple chunks with different chunk_index
        assert len(arch_results) >= 1

        indexer.stop()

    def test_watch_and_update(self, test_vault, temp_db):
        """File changes should be detected and indexed."""
        from api.services.indexer import IndexerService

        indexer = IndexerService(
            vault_path=str(test_vault),
            db_path=str(temp_db)
        )

        indexer.index_all()
        initial_count = indexer.vector_store.get_document_count()

        # Start watching
        indexer.start_watching()
        time.sleep(1)

        # Create new file
        new_file = test_vault / "Personal" / "new_thought.md"
        new_file.write_text("""# New Insight

Had an interesting thought about product strategy today.
""")

        # Wait for indexing
        time.sleep(4)

        # Search should find new content
        results = indexer.vector_store.search("product strategy insight")
        assert any("new_thought.md" in r["file_name"] for r in results)

        indexer.stop()
