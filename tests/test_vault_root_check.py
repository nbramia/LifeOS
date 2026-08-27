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
    ChromaDB server."""

    def __init__(self, metadatas):
        self._metadatas = metadatas

    def get(self, limit=None, include=None):
        return {"metadatas": self._metadatas[: limit or len(self._metadatas)]}


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

        class _StubStore:
            def sample_file_paths(self, limit=5):
                return [str(old_location / "note.md")]

        monkeypatch.setattr(vs, "get_vector_store", lambda: _StubStore())

        check = {"status": "ok", "latency_ms": 5, "detail": "1 results"}
        main._check_vault_root_sanity(check, tmp_path / "vault")

        assert check["status"] == "degraded"
        # The original request/response detail is preserved unchanged — the
        # mismatch is reported in a separate field, not a replacement.
        assert check["detail"] == "1 results"
        assert "1/1" in check["vault_root_check"]
        # The mismatched path itself must never appear in the response —
        # /health/full is unauthenticated, and a real indexed path can
        # reveal personal folder/file names.
        assert "old_vault_location" not in check["vault_root_check"]
        assert "note.md" not in check["vault_root_check"]

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
