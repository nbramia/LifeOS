"""Tests for the vault-root sanity check added to `vault_search` in
`GET /health/full` (#762): the existing request/response probe only
confirms search returns *something* — it can't tell a moved/deleted vault
whose index still holds entries from the old location, because search keeps
"working" against stale content. These tests cover the pure comparison
logic (`sample_paths_match_vault_root`) directly, plus a wiring check that
`full_health_check()` downgrades an already-passing `vault_search` row to
"degraded" when a sample of indexed paths falls outside the configured
vault root.
"""
import pytest

from api.services.vectorstore import VectorStore, sample_paths_match_vault_root

pytestmark = pytest.mark.unit


class _StubCollection:
    """Stands in for a ChromaDB collection's `.get()` — enough to test
    `VectorStore.sample_file_paths()`'s dedup logic without a real
    ChromaDB server. Understands the `{"note_type": {"$nin": [...]}}` /
    `{"$in": [...]}` shape of `where` clause this code actually issues, so
    the filtering behavior is exercised, not just dedup."""

    def __init__(self, metadatas):
        self._metadatas = metadatas

    def get(self, limit=None, where=None, include=None):
        metadatas = self._metadatas
        if where:
            metadatas = [m for m in metadatas if self._matches(m, where)]
        return {"metadatas": metadatas[: limit or len(metadatas)]}

    @staticmethod
    def _matches(meta, where):
        for field, condition in where.items():
            value = (meta or {}).get(field)
            if isinstance(condition, dict):
                if "$nin" in condition and value in condition["$nin"]:
                    return False
                if "$in" in condition and value not in condition["$in"]:
                    return False
            elif value != condition:
                return False
        return True


def _store_with_collection(collection) -> VectorStore:
    # Bypass VectorStore.__init__ (it opens a real ChromaDB HTTP connection
    # and loads the embedding model) — only `_collection` is needed here.
    store = object.__new__(VectorStore)
    store._collection = collection
    return store


class TestSampleFilePaths:
    def test_dedupes_chunks_from_the_same_file(self):
        """Each file is indexed as several chunks sharing one file_path — a
        raw limit-sized fetch could otherwise return the same file 5 times
        over instead of sampling 5 distinct files (Codex review finding)."""
        metadatas = (
            [{"file_path": "/vault/a.md"}] * 3
            + [{"file_path": "/vault/b.md"}] * 3
            + [{"file_path": "/vault/c.md"}] * 3
        )
        store = _store_with_collection(_StubCollection(metadatas))

        sample = store.sample_file_paths(limit=2)

        assert sample == ["/vault/a.md", "/vault/b.md"]

    def test_skips_rows_missing_file_path(self):
        metadatas = [{}, {"other": "x"}, {"file_path": "/vault/a.md"}]
        store = _store_with_collection(_StubCollection(metadatas))

        sample = store.sample_file_paths(limit=5)

        assert sample == ["/vault/a.md"]

    def test_calendar_only_rows_yield_an_empty_sample(self):
        """On a real vault, calendar-event rows (indexed under a relative
        pseudo-path like "calendar/<event-id>") dominate the front of
        insertion order. Before #762 follow-up, a fetch limited to the first
        few rows could return nothing but these, and every one would be
        flagged as "outside the vault root" — a false degraded. They must be
        excluded from the sample entirely, not just tolerated."""
        metadatas = [
            {"note_type": "calendar_event", "file_path": "calendar/evt-1"},
            {"note_type": "calendar_event", "file_path": "calendar/evt-2"},
            {"note_type": "slack_message", "file_path": "slack/msg-1"},
        ]
        store = _store_with_collection(_StubCollection(metadatas))

        sample = store.sample_file_paths(limit=5)

        assert sample == []

    def test_mixed_calendar_and_vault_rows_samples_only_vault_paths(self):
        metadatas = [
            {"note_type": "calendar_event", "file_path": "calendar/evt-1"},
            {"note_type": "Personal", "file_path": "/vault/notes/a.md"},
            {"note_type": "slack_message", "file_path": "slack/msg-1"},
            {"note_type": "Work", "file_path": "/vault/notes/b.md"},
        ]
        store = _store_with_collection(_StubCollection(metadatas))

        sample = store.sample_file_paths(limit=5)

        assert sample == ["/vault/notes/a.md", "/vault/notes/b.md"]

    def test_non_absolute_path_excluded_even_without_a_known_non_vault_note_type(self):
        """Defense in depth: any row storing a relative path is excluded
        regardless of its note_type, so a future non-vault source that isn't
        (yet) added to the exclusion list still can't pollute the sample."""
        metadatas = [
            {"note_type": "some_future_source", "file_path": "future/doc-1"},
            {"note_type": "Personal", "file_path": "/vault/notes/a.md"},
        ]
        store = _store_with_collection(_StubCollection(metadatas))

        sample = store.sample_file_paths(limit=5)

        assert sample == ["/vault/notes/a.md"]

    def test_passes_a_note_type_exclusion_where_filter_to_the_collection(self):
        """Pins the filter shape `sample_file_paths()` relies on ChromaDB to
        honor — a regression here would silently turn the where-filter into
        a no-op fetch."""
        captured = {}

        class _RecordingCollection(_StubCollection):
            def get(self, limit=None, where=None, include=None):
                captured["where"] = where
                return super().get(limit=limit, where=where, include=include)

        store = _store_with_collection(_RecordingCollection([]))

        store.sample_file_paths(limit=5)

        assert captured["where"] == {
            "note_type": {"$nin": ["calendar_event", "slack_message"]}
        }


class TestSamplePathsMatchVaultRoot:
    def test_all_paths_under_root_match(self, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()
        paths = [str(root / "notes" / "a.md"), str(root / "b.md")]

        all_match, mismatched = sample_paths_match_vault_root(paths, root)

        assert all_match is True
        assert mismatched == []

    def test_path_outside_root_is_a_mismatch(self, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()
        old_location = tmp_path / "old_vault"
        paths = [str(root / "a.md"), str(old_location / "b.md")]

        all_match, mismatched = sample_paths_match_vault_root(paths, root)

        assert all_match is False
        assert mismatched == [str(old_location / "b.md")]

    def test_empty_sample_trivially_matches(self, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()

        all_match, mismatched = sample_paths_match_vault_root([], root)

        assert all_match is True
        assert mismatched == []

    def test_sibling_directory_with_shared_prefix_is_not_a_false_match(self, tmp_path):
        """A naive string-prefix check (`str(p).startswith(str(root))`) would
        wrongly treat /vault2/x.md as under /vault — is_relative_to must not
        make that mistake."""
        root = tmp_path / "vault"
        root.mkdir()
        sibling = tmp_path / "vault2"
        paths = [str(sibling / "x.md")]

        all_match, mismatched = sample_paths_match_vault_root(paths, root)

        assert all_match is False
        assert mismatched == [str(sibling / "x.md")]

    def test_malformed_path_is_reported_not_silently_dropped(self, tmp_path):
        root = tmp_path / "vault"
        root.mkdir()

        all_match, mismatched = sample_paths_match_vault_root([None], root)

        assert all_match is False
        assert mismatched == [None]


class TestCheckVaultRootSanity:
    """Direct tests of `_check_vault_root_sanity` (api/main.py), the helper
    `full_health_check()` calls after the existing vault_search
    request/response probe. Exercised in isolation — with a synthetic
    `checks["vault_search"]` dict and a stubbed vector store — so these don't
    depend on a live ChromaDB or a running LifeOS server."""

    def test_downgrades_ok_to_degraded_on_mismatch(self, monkeypatch, tmp_path):
        import api.main as main
        from api.services import vectorstore as vs

        old_location = tmp_path / "old_vault_location"
        configured_root = tmp_path / "vault"

        class _StubStore:
            def sample_file_paths(self, limit=5):
                return [str(old_location / "note.md")]

        monkeypatch.setattr(vs, "get_vector_store", lambda: _StubStore())

        check = {"status": "ok", "latency_ms": 5, "detail": "1 results"}
        main._check_vault_root_sanity(check, configured_root)

        assert check["status"] == "degraded"
        # The original request/response detail is preserved unchanged — the
        # mismatch is reported in a separate field, not a replacement.
        assert check["detail"] == "1 results"
        assert "1/1" in check["vault_root_check"]
        # Neither the mismatched path nor the configured vault root itself
        # may appear in the response — /health/full is unauthenticated, and
        # either one can reveal personal folder/file names or the operator's
        # home directory layout.
        assert "old_vault_location" not in check["vault_root_check"]
        assert "note.md" not in check["vault_root_check"]
        assert str(configured_root.resolve()) not in check["vault_root_check"]

    def test_empty_sample_is_no_signal_not_degraded(self, monkeypatch, tmp_path):
        """A sample with zero vault documents (e.g. the collection is
        currently all calendar/Slack content) must not be treated as
        evidence of a moved vault — absence of vault chunks in a bounded
        sample isn't proof the vault moved (#762 follow-up)."""
        import api.main as main
        from api.services import vectorstore as vs

        class _StubStore:
            def sample_file_paths(self, limit=5):
                return []

        monkeypatch.setattr(vs, "get_vector_store", lambda: _StubStore())

        check = {"status": "ok", "detail": "1 results"}
        main._check_vault_root_sanity(check, tmp_path / "vault")

        assert check["status"] == "ok"
        assert check["vault_root_check"] == "no vault documents sampled"

    def test_partial_mismatch_stays_ok_with_a_counts_note(self, monkeypatch, tmp_path):
        """The failure this check exists to catch is a vault that MOVED —
        in that case NO sampled vault path is under the root. A sample with
        some matches and some mismatches (e.g. debris from a test run that
        indexed documents straight into this collection, or a stray path
        from an old sync) isn't that failure and must not degrade — but the
        partial mismatch is still surfaced as a counts-only note (#762
        second follow-up)."""
        import api.main as main
        from api.services import vectorstore as vs

        vault_root = tmp_path / "vault"
        debris_root = tmp_path / "debris"
        under_root = [str(vault_root / f"note{i}.md") for i in range(20)]
        debris = [str(debris_root / f"stray{i}.md") for i in range(30)]

        class _StubStore:
            def sample_file_paths(self, limit=50):
                return under_root + debris

        monkeypatch.setattr(vs, "get_vector_store", lambda: _StubStore())

        check = {"status": "ok", "detail": "1 results"}
        main._check_vault_root_sanity(check, vault_root)

        assert check["status"] == "ok"
        assert check["vault_root_check"] == "20/50 sampled vault paths under root"

    def test_all_mismatch_at_scale_still_degrades(self, monkeypatch, tmp_path):
        """The real detection must survive the new partial-mismatch
        tolerance: zero matches out of a larger sample is still the
        moved-vault signal."""
        import api.main as main
        from api.services import vectorstore as vs

        vault_root = tmp_path / "vault"
        old_location = tmp_path / "old_vault_location"
        mismatched = [str(old_location / f"note{i}.md") for i in range(50)]

        class _StubStore:
            def sample_file_paths(self, limit=50):
                return mismatched

        monkeypatch.setattr(vs, "get_vector_store", lambda: _StubStore())

        check = {"status": "ok", "detail": "1 results"}
        main._check_vault_root_sanity(check, vault_root)

        assert check["status"] == "degraded"
        assert (
            check["vault_root_check"]
            == "50/50 sampled indexed path(s) fall outside the configured vault root"
        )

    def test_leaves_ok_unchanged_when_paths_match(self, monkeypatch, tmp_path):
        import api.main as main
        from api.services import vectorstore as vs

        vault_root = tmp_path / "vault"

        class _StubStore:
            def sample_file_paths(self, limit=5):
                return [str(vault_root / "note.md")]

        monkeypatch.setattr(vs, "get_vector_store", lambda: _StubStore())

        check = {"status": "ok", "latency_ms": 5, "detail": "1 results"}
        main._check_vault_root_sanity(check, vault_root)

        assert check == {"status": "ok", "latency_ms": 5, "detail": "1 results"}

    def test_skips_entirely_when_base_check_did_not_pass(self, monkeypatch, tmp_path):
        """A failing (or missing) base check is left completely untouched —
        this is an additional signal on top of a pass, not a replacement for
        it, and must never turn a failure into a pass or vice versa."""
        import api.main as main
        from api.services import vectorstore as vs

        called = []

        def _boom():
            called.append(True)
            raise AssertionError("should not be reached when base check failed")

        monkeypatch.setattr(vs, "get_vector_store", _boom)

        check = {"status": "error", "error": "HTTP 500: boom"}
        main._check_vault_root_sanity(check, tmp_path / "vault")

        assert check == {"status": "error", "error": "HTTP 500: boom"}
        assert called == []

        main._check_vault_root_sanity(None, tmp_path / "vault")  # no-op, must not raise

    def test_vector_store_error_does_not_crash_or_change_status(self, monkeypatch, tmp_path):
        """The sanity check is additive only — if the vector store itself
        can't be reached for this probe, the primary vault_search result
        (already "ok" from the request/response check) is left alone rather
        than being dragged down by a problem visible elsewhere
        (chromadb_server)."""
        import api.main as main
        from api.services import vectorstore as vs

        def _boom():
            raise RuntimeError("chromadb unreachable")

        monkeypatch.setattr(vs, "get_vector_store", _boom)

        check = {"status": "ok", "detail": "1 results"}
        main._check_vault_root_sanity(check, tmp_path / "vault")

        assert check == {"status": "ok", "detail": "1 results"}


async def test_full_health_check_wires_in_the_vault_root_sanity_check(monkeypatch):
    """Wiring check: `full_health_check()` actually calls the helper on the
    `vault_search` row it just produced (rather than the helper existing but
    never being invoked)."""
    import api.main as main

    called_with = {}

    def _spy(vault_search_check, vault_path):
        called_with["check"] = vault_search_check
        called_with["vault_path"] = vault_path

    monkeypatch.setattr(main, "_check_vault_root_sanity", _spy)

    result = await main.full_health_check()

    assert called_with.get("vault_path") == main.settings.vault_path
    assert called_with.get("check") is result["checks"].get("vault_search")
