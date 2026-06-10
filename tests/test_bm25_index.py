"""Unit tests for the BM25 keyword index — delete behaviour matters most.

The historical regression was: ``delete_document(path)`` only clears the row
whose doc_id equals ``path`` exactly, but chunks are keyed
``{path}_{chunk_idx}`` and the summary is ``{path}::summary`` — so the
"clean before re-add" path silently leaked all chunks whenever a file's
chunk count shrank or its underlying path changed (e.g. vault migration
from macOS to Linux left two parallel sets of rows).
"""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def bm25(tmp_path):
    from api.services.bm25_index import BM25Index
    return BM25Index(db_path=str(tmp_path / "bm25_test.db"))


def _seed_file_chunks(bm25, path, chunk_count, with_summary=True):
    for i in range(chunk_count):
        bm25.add_document(
            doc_id=f"{path}_{i}",
            content=f"chunk {i} content for {path}",
            file_name=path.split("/")[-1],
        )
    if with_summary:
        bm25.add_document(
            doc_id=f"{path}::summary",
            content=f"summary of {path}",
            file_name=path.split("/")[-1],
        )


class TestDeleteByPath:
    def test_clears_all_chunks_for_a_file(self, bm25):
        _seed_file_chunks(bm25, "/notes/foo.md", chunk_count=5)
        assert bm25.count() == 6  # 5 chunks + 1 summary

        removed = bm25.delete_by_path("/notes/foo.md")
        assert removed == 6
        assert bm25.count() == 0

    def test_only_touches_the_named_path(self, bm25):
        _seed_file_chunks(bm25, "/notes/foo.md", chunk_count=3)
        _seed_file_chunks(bm25, "/notes/bar.md", chunk_count=2)

        removed = bm25.delete_by_path("/notes/foo.md")
        assert removed == 4
        # bar's 2 chunks + 1 summary survive
        assert bm25.count() == 3

    def test_path_prefix_collision_is_safe(self, bm25):
        """``/a/foo.md`` deletion must not clobber ``/a/foo.md.bak``.

        GLOB ``{path}_*`` is the underscore-suffix shape we use for numbered
        chunks; ``foo.md.bak_0`` starts with ``foo.md.b`` not ``foo.md_``, so
        the safety property here is just that we don't accidentally substring-
        match the wrong file.
        """
        _seed_file_chunks(bm25, "/a/foo.md", chunk_count=2)
        _seed_file_chunks(bm25, "/a/foo.md.bak", chunk_count=2)

        removed = bm25.delete_by_path("/a/foo.md")
        # 2 chunks + summary for /a/foo.md only
        assert removed == 3
        # /a/foo.md.bak still has 2 chunks + summary
        assert bm25.count() == 3

    def test_removes_stale_path_alongside_current_path(self, bm25):
        """The vault migration scenario: old macOS path and new Linux path coexist."""
        _seed_file_chunks(bm25, "/Users/x/Notes/q.md", chunk_count=3)
        _seed_file_chunks(bm25, "/home/x/Notes/q.md", chunk_count=3)

        # Drop the legacy macOS rows only.
        removed = bm25.delete_by_path("/Users/x/Notes/q.md")
        assert removed == 4
        # Linux rows survive.
        assert bm25.count() == 4

    def test_returns_zero_when_nothing_to_delete(self, bm25):
        assert bm25.delete_by_path("/nonexistent.md") == 0


class TestDocDates:
    """The sidecar date table backs recency ranking + date filtering for the
    keyword half of hybrid search."""

    def test_search_returns_modified_date(self, bm25):
        bm25.add_document(
            "doc1", "quarterly budget review", "Budget.md",
            modified_date="2026-06-05",
        )
        results = bm25.search("budget")
        assert len(results) == 1
        assert results[0]["modified_date"] == "2026-06-05"

    def test_search_returns_empty_string_when_undated(self, bm25):
        bm25.add_document("doc1", "quarterly budget review", "Budget.md")
        results = bm25.search("budget")
        assert results[0]["modified_date"] == ""

    def test_date_updates_on_readd(self, bm25):
        bm25.add_document("doc1", "budget", "B.md", modified_date="2026-01-01")
        bm25.add_document("doc1", "budget", "B.md", modified_date="2026-06-01")
        assert bm25.search("budget")[0]["modified_date"] == "2026-06-01"

    def test_readd_without_date_clears_stale_date(self, bm25):
        bm25.add_document("doc1", "budget", "B.md", modified_date="2026-01-01")
        bm25.add_document("doc1", "budget", "B.md")  # no date this time
        assert bm25.search("budget")[0]["modified_date"] == ""

    def test_delete_by_path_clears_dates(self, bm25):
        bm25.add_document(
            "/notes/q.md_0", "budget", "q.md", modified_date="2026-06-01"
        )
        bm25.delete_by_path("/notes/q.md")
        # Re-add undated; the old date must not linger via the sidecar table.
        bm25.add_document("/notes/q.md_0", "budget", "q.md")
        assert bm25.search("budget")[0]["modified_date"] == ""

    def test_bulk_add_persists_dates(self, bm25):
        bm25.bulk_add([
            {"doc_id": "d1", "content": "budget alpha", "file_name": "a.md",
             "modified_date": "2026-03-03"},
            {"doc_id": "d2", "content": "budget beta", "file_name": "b.md"},
        ])
        by_id = {r["doc_id"]: r for r in bm25.search("budget")}
        assert by_id["d1"]["modified_date"] == "2026-03-03"
        assert by_id["d2"]["modified_date"] == ""

    def test_clear_removes_dates(self, bm25):
        bm25.add_document("d1", "budget", "a.md", modified_date="2026-03-03")
        bm25.clear()
        bm25.add_document("d1", "budget", "a.md")
        assert bm25.search("budget")[0]["modified_date"] == ""
