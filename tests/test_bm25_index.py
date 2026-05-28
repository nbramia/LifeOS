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
