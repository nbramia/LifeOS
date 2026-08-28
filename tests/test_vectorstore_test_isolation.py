"""Regression coverage for the #828 conftest guard
(``_guard_live_vectorstore_collections``) and its interaction with the
existing #288 vault-indexer isolation (``_isolate_vault_indexer_stores``).

Mirrors ``tests/test_session_store_test_isolation.py``'s approach for #652:
nothing here exercises the guard fixture explicitly (it's autouse), so
these tests are already running under it. What's asserted is the thing
#828 was actually about — a bare ``VectorStore(collection_name=...)``
pointed at a real production collection name must fail loudly instead of
silently opening a connection to the live server.
"""
from __future__ import annotations

import pytest


@pytest.mark.unit
def test_bare_vectorstore_against_production_collection_fails_loudly():
    """A test that (accidentally or otherwise) constructs a ``VectorStore``
    pointed at the real ``lifeos_vault`` collection must be stopped before
    it ever opens a connection — this is the exact shape of bug #828 found:
    stray `/tmp/tmpXXXXXXXX/vault/...` rows in the live collection."""
    from api.services.vectorstore import VectorStore

    with pytest.raises(pytest.fail.Exception, match="uses_live_vectorstore"):
        VectorStore(collection_name="lifeos_vault")


@pytest.mark.unit
@pytest.mark.parametrize(
    "collection_name",
    ["lifeos_vault", "lifeos_people", "lifeos_slack", "lifeos_calendar"],
)
def test_every_known_production_collection_is_guarded(collection_name):
    """All four real collection names (vault default, plus the person/slack/
    calendar indexers' explicit ones) must trip the guard, not just the
    default."""
    from api.services.vectorstore import VectorStore

    with pytest.raises(pytest.fail.Exception):
        VectorStore(collection_name=collection_name)


@pytest.mark.unit
def test_isolated_collection_name_is_not_guarded():
    """A test-scoped collection name (the pattern
    ``_isolate_vault_indexer_stores`` and ``test_vectorstore.py`` both use)
    must pass straight through — the guard only blocks the known production
    names, not test isolation itself.

    ``chromadb.HttpClient`` and the (heavy, GPU-loading) embedding service
    are both mocked so this stays a true ``unit`` test rather than
    incidentally becoming a ``slow`` one.
    """
    from unittest.mock import MagicMock, patch

    from api.services.vectorstore import VectorStore

    with patch("chromadb.HttpClient") as mock_client, \
            patch("api.services.embeddings.get_embedding_service", return_value=MagicMock()):
        mock_client.return_value.get_or_create_collection.return_value = object()
        store = VectorStore(collection_name="lifeos_vault_test_828")
        assert store.collection_name == "lifeos_vault_test_828"


@pytest.mark.unit
def test_bare_indexer_service_never_reaches_a_production_collection_name(tmp_path):
    """Regression guard for the #288 fixture's actual job: a bare
    ``IndexerService()`` construction (the shape ``test_indexer.py``,
    ``test_integration.py``, and ``test_people.py`` all use) must resolve
    to a throwaway collection, never the live ``lifeos_vault`` — this is
    what #828's stray rows imply happened at some point in the past. If
    ``_isolate_vault_indexer_stores`` ever regressed, the guard fixture
    above would fail this test loudly rather than let it silently open a
    real connection.

    ``chromadb.HttpClient`` and the embedding service are mocked so this
    exercises the isolation/guard wiring without becoming a ``slow`` test.
    """
    from unittest.mock import MagicMock, patch

    from api.services.indexer import IndexerService

    with patch("chromadb.HttpClient") as mock_client, \
            patch("api.services.embeddings.get_embedding_service", return_value=MagicMock()):
        mock_client.return_value.get_or_create_collection.return_value = object()
        indexer = IndexerService(vault_path=str(tmp_path / "vault"), db_path=str(tmp_path / "db"))
        try:
            assert indexer.vector_store.collection_name != "lifeos_vault"
        finally:
            indexer.stop()


@pytest.mark.unit
@pytest.mark.uses_live_vectorstore
def test_marker_opts_a_test_out_of_the_guard():
    """The opt-in marker used by test_admin.py / test_search_api.py must
    actually suppress the guard, exercising the composition those files
    rely on (this test's own marker, above)."""
    from unittest.mock import MagicMock, patch

    from api.services.vectorstore import VectorStore

    with patch("chromadb.HttpClient") as mock_client, \
            patch("api.services.embeddings.get_embedding_service", return_value=MagicMock()):
        mock_client.return_value.get_or_create_collection.return_value = object()
        # Would raise pytest.fail.Exception without the marker above.
        store = VectorStore(collection_name="lifeos_vault")
        assert store.collection_name == "lifeos_vault"
