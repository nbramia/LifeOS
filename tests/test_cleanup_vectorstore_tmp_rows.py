"""Tests for the #828 one-off cleanup script
(`scripts/cleanup_vectorstore_tmp_rows.py`).

The acceptance criterion this backs is that the selector "must not touch
any row whose path is under the configured vault root or any non-vault
source" — so most of this file is about what the selector must *not*
match, using the same fake-collection pattern as
`test_vault_root_check.py::_StubCollection`.
"""
from pathlib import Path

import pytest

from scripts.cleanup_vectorstore_tmp_rows import find_stray_rows, is_stray_tmp_row

pytestmark = pytest.mark.unit


class _PagingStubCollection:
    """Fakes just enough of a ChromaDB collection's `.get()` to exercise
    `find_stray_rows`'s pagination — a list of (id, file_path) rows, served
    back a `limit`-sized page at a time starting at `offset`."""

    def __init__(self, rows: list[tuple[str, str]]):
        self._rows = rows

    def get(self, limit=None, offset=0, include=None):
        page = self._rows[offset:offset + limit]
        return {
            "ids": [row_id for row_id, _ in page],
            "metadatas": [{"file_path": path} for _, path in page],
        }


class TestIsStrayTmpRow:
    def test_matches_a_tmp_vault_path(self):
        assert is_stray_tmp_row("/tmp/tmp8f2k3a9x/vault/Work/ML/meeting.md")

    def test_does_not_match_a_real_vault_path(self):
        assert not is_stray_tmp_row("/home/nathan/LifeOS Vault/Work/ML/meeting.md")

    def test_does_not_match_a_relative_calendar_pseudo_path(self):
        assert not is_stray_tmp_row("calendar/abc123")

    def test_does_not_match_a_relative_slack_pseudo_path(self):
        assert not is_stray_tmp_row("slack/C123/1700000000.000100")

    def test_does_not_match_a_path_merely_mentioning_tmp(self):
        """Prefix check, not substring — a real note path that happens to
        contain "/tmp/" elsewhere (e.g. a vault subfolder literally named
        "tmp") must not be treated as debris."""
        assert not is_stray_tmp_row("/home/nathan/LifeOS Vault/tmp/notes.md")

    def test_empty_path_does_not_match(self):
        assert not is_stray_tmp_row("")

    def test_vault_root_check_wins_even_under_tmp(self):
        """Belt-and-suspenders: if the *configured* vault root itself lives
        under /tmp (an unusual but possible setup), a real document there
        must never be treated as stray."""
        vault_root = Path("/tmp/my-real-vault")
        assert not is_stray_tmp_row("/tmp/my-real-vault/Personal/journal.md", vault_root=vault_root)

    def test_a_sibling_tmp_dir_still_matches_with_vault_root_set(self):
        vault_root = Path("/tmp/my-real-vault")
        assert is_stray_tmp_row("/tmp/tmp8f2k3a9x/vault/note.md", vault_root=vault_root)


class TestFindStrayRows:
    def test_finds_only_the_tmp_rows_across_a_mixed_collection(self):
        rows = [
            ("id1", "/home/nathan/LifeOS Vault/Personal/journal.md"),
            ("id2", "/tmp/tmp8f2k3a9x/vault/Work/ML/meeting.md"),
            ("id3", "calendar/abc123"),
            ("id4", "/tmp/tmpzz11xy00/vault/Personal/journal.md"),
            ("id5", "slack/C123/1700000000.000100"),
        ]
        collection = _PagingStubCollection(rows)

        stray = find_stray_rows(collection, page_size=2)

        assert stray == [
            ("id2", "/tmp/tmp8f2k3a9x/vault/Work/ML/meeting.md"),
            ("id4", "/tmp/tmpzz11xy00/vault/Personal/journal.md"),
        ]

    def test_empty_collection_returns_no_rows(self):
        collection = _PagingStubCollection([])
        assert find_stray_rows(collection) == []

    def test_pages_past_a_full_first_page(self):
        """The exact bug this pagination guards against: a naive `.get()`
        with no limit would risk "too many SQL variables" on a large live
        collection, so `find_stray_rows` must keep paging past a full page
        instead of stopping after the first `page_size` rows."""
        rows = [(f"id{i}", "/home/nathan/LifeOS Vault/note.md") for i in range(5)]
        rows.append(("id_stray", "/tmp/tmp8f2k3a9x/vault/note.md"))
        collection = _PagingStubCollection(rows)

        stray = find_stray_rows(collection, page_size=2)

        assert stray == [("id_stray", "/tmp/tmp8f2k3a9x/vault/note.md")]
