"""Regression coverage for the conftest guard
(``_guard_live_vectorstore_collections``) and its interaction with the
existing vault-indexer isolation (``_isolate_vault_indexer_stores``).

Mirrors ``tests/test_session_store_test_isolation.py``'s approach:
nothing here exercises the guard fixture explicitly (it's autouse), so
these tests are already running under it. What's asserted: a bare
``VectorStore(collection_name=...)``
pointed at a real production collection name must fail loudly instead of
silently opening a connection to the live server.
"""
from __future__ import annotations

import pytest


@pytest.mark.unit
def test_bare_vectorstore_against_production_collection_fails_loudly():
    """A test that (accidentally or otherwise) constructs a ``VectorStore``
    pointed at the real ``lifeos_vault`` collection must be stopped before
    it ever opens a connection — otherwise it leaves stray
    `/tmp/tmpXXXXXXXX/vault/...` rows in the live collection."""
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
    """Regression guard for the isolation fixture's actual job: a bare
    ``IndexerService()`` construction (the shape ``test_indexer.py``,
    ``test_integration.py``, and ``test_people.py`` all use) must resolve
    to a throwaway collection, never the live ``lifeos_vault`` — a bare
    construction reaching production is exactly how stray rows end up
    there. If ``_isolate_vault_indexer_stores`` ever regressed, the guard
    fixture above would fail this test loudly rather than let it silently
    open a real connection.

    ``chromadb.HttpClient`` and the embedding service are mocked so this
    exercises the isolation/guard wiring without becoming a ``slow`` test.
    """
    from unittest.mock import MagicMock, patch

    from api.services.indexer import IndexerService

    with patch("chromadb.HttpClient") as mock_client, \
            patch("api.services.embeddings.get_embedding_service", return_value=MagicMock()):
        mock_client.return_value.get_or_create_collection.return_value = object()
        indexer = IndexerService(vault_path=str(tmp_path / "vault"), db_path=str(tmp_path / "db"))
        assert indexer.vector_store.collection_name != "lifeos_vault"


@pytest.mark.unit
def test_vectorstore_singletons_are_reset_before_every_test():
    """Every process-wide singleton that can hold a live
    VectorStore/HybridSearch/*Indexer instance must be `None` at the start
    of any test, regardless of what ran before it in this worker process
    (xdist reuses one process across many tests). This is what
    `_reset_vectorstore_singletons` in conftest.py exists to guarantee --
    without it, a test that legitimately populated one of these with a
    real instance would leak it to whatever test happens to run next in
    the same worker, bypassing the construction-time guard entirely
    (the guard only sees `VectorStore.__init__`, not singleton reuse)."""
    import api.main as main_mod
    import api.routes.ask as ask_route_mod
    import api.routes.search as search_route_mod
    import api.services.calendar_indexer as calendar_indexer_mod
    import api.services.slack_indexer as slack_indexer_mod
    import api.services.vectorstore as vectorstore_mod

    assert vectorstore_mod._vector_store is None
    assert search_route_mod._vector_store is None
    assert search_route_mod._hybrid_search is None
    assert ask_route_mod._hybrid_search is None
    assert slack_indexer_mod._slack_indexer is None
    assert calendar_indexer_mod._calendar_indexer is None
    assert main_mod._calendar_indexer is None


@pytest.mark.unit
@pytest.mark.uses_live_vectorstore
def test_marker_opts_a_test_out_of_the_guard():
    """The opt-in marker exists precisely so a future test that legitimately
    needs the live store can bypass the guard -- no current test needs it
    (test_admin.py and test_search_api.py were both converted to mock the
    store instead), so this test exercises the marker directly."""
    from unittest.mock import MagicMock, patch

    from api.services.vectorstore import VectorStore

    with patch("chromadb.HttpClient") as mock_client, \
            patch("api.services.embeddings.get_embedding_service", return_value=MagicMock()):
        mock_client.return_value.get_or_create_collection.return_value = object()
        # Would raise pytest.fail.Exception without the marker above.
        store = VectorStore(collection_name="lifeos_vault")
        assert store.collection_name == "lifeos_vault"


@pytest.mark.unit
def test_fast_unit_test_cannot_open_a_chroma_server_connection(tmp_path):
    """A throwaway collection name still opens a real connection to the server
    on the default port, which on the maintainer's host holds the live index."""
    import chromadb

    from api.services.vectorstore import VectorStore

    with pytest.raises(pytest.fail.Exception, match="ChromaDB server connection"):
        chromadb.HttpClient(host="localhost", port=1)
    with pytest.raises(pytest.fail.Exception, match="ChromaDB server connection"):
        VectorStore(collection_name="synthetic_throwaway_collection")


@pytest.mark.unit
def test_bare_indexer_service_cannot_connect_to_a_server(tmp_path):
    from api.services.indexer import IndexerService

    with pytest.raises(pytest.fail.Exception, match="ChromaDB server connection"):
        IndexerService(vault_path=str(tmp_path / "vault"), db_path=str(tmp_path / "db"))


@pytest.mark.unit
def test_in_process_chroma_still_works_under_the_refusal():
    import chromadb

    client = chromadb.EphemeralClient()
    assert client.get_max_batch_size() > 0


@pytest.mark.unit
@pytest.mark.uses_live_vectorstore
def test_opt_out_marker_leaves_the_client_alone():
    import chromadb

    assert chromadb.HttpClient.__name__ == "HttpClient"


@pytest.mark.unit
@pytest.mark.parametrize(
    "marker_names, in_lane",
    [
        ({"unit"}, True),
        ({"unit", "slow"}, False),
        ({"unit", "integration"}, False),
        ({"unit", "requires_server"}, False),
        ({"slow"}, False),
        (set(), False),
    ],
)
def test_refusal_applies_to_the_fast_unit_lane_only(marker_names, in_lane):
    from tests.conftest import runs_in_fast_unit_lane

    assert runs_in_fast_unit_lane(marker_names) is in_lane
