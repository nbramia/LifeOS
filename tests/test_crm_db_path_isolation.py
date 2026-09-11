"""A default-constructed ``RelationshipStore``'s database path is isolated
per test.

``RelationshipStore.__init__`` falls back to ``get_crm_db_path()`` when no
``db_path`` is given, and ``_init_db()`` connects to that path (creating the
file) before issuing ``CREATE TABLE IF NOT EXISTS``. Two processes each
constructing a default-path store around the same moment can race that
window: the second connection sees an empty database and raises
``sqlite3.OperationalError: no such table: relationships``.

A deterministic reproduction of that race across real parallel worker
processes is impractical to drive from a single test. These tests instead
pin the ownership property that prevents it: the path a default-constructed
``RelationshipStore`` resolves to is never the path a fresh, unpatched
``get_crm_db_path()`` produces -- the path every process that skips
isolation would share -- and it lives under this test's own pytest-managed
temp tree.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _shared_default_crm_db_path() -> Path:
    """The path a default-constructed store shares with every other process
    that also skips isolation.

    Built from a fresh ``Settings`` rather than from ``get_crm_db_path()``,
    which the isolation under test redirects -- reading it through the
    redirected helper would compare the isolated path against itself and
    assert nothing.
    """
    from config.settings import Settings

    return Path(Settings().chroma_path).parent / "crm.db"


def test_relationship_store_default_path_is_isolated(tmp_path_factory):
    """A default-constructed ``RelationshipStore`` must not resolve to the
    shared path every other default-constructed store would share."""
    from api.services.relationship import RelationshipStore

    resolved = Path(RelationshipStore().db_path)

    assert resolved != _shared_default_crm_db_path()
    assert resolved.is_relative_to(tmp_path_factory.getbasetemp())


def test_relationship_store_singleton_default_path_is_isolated(tmp_path_factory):
    """``get_relationship_store()``'s lazily constructed singleton resolves
    the same isolated path as a direct ``RelationshipStore()`` construction."""
    from api.services.relationship import get_relationship_store, reset_relationship_store

    reset_relationship_store()
    resolved = Path(get_relationship_store().db_path)
    reset_relationship_store()

    assert resolved != _shared_default_crm_db_path()
    assert resolved.is_relative_to(tmp_path_factory.getbasetemp())
