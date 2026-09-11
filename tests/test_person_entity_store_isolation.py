"""A default-constructed ``PersonEntityStore``'s database path is isolated
per test.

``PersonEntityStore.CRM_DB_PATH`` is a ``Path(__file__)``-derived class
attribute, not something resolved through ``settings``. ``__init__`` falls
back to it when no ``db_path`` is given, and ``_init_db()`` connects to that
path (creating the file) before issuing ``CREATE TABLE IF NOT EXISTS`` -- a
second process connecting to the same file inside that window sees an empty
database and raises
``sqlite3.OperationalError: no such table: person_entities``.

A deterministic reproduction of that race across real parallel worker
processes is impractical to drive from a single test. These tests instead
pin the ownership property that prevents it: the path a default-constructed
``PersonEntityStore`` resolves to is never the repo-root path every process
that skips isolation would share, and it lives under this test's own
pytest-managed temp tree.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _shared_default_crm_db_path() -> Path:
    """The path a default-constructed store shares with every other process
    that also skips isolation.

    Computed from this test file's own known position in the repo tree,
    independent of ``PersonEntityStore.CRM_DB_PATH`` -- reading the class
    attribute here would compare the isolated path against itself and
    assert nothing.
    """
    return Path(__file__).parent.parent / "data" / "crm.db"


def test_person_entity_store_default_path_is_isolated(tmp_path_factory):
    """A default-constructed ``PersonEntityStore`` must not resolve to the
    shared repo-root path every other default-constructed store would
    share."""
    from api.services.person_entity import PersonEntityStore

    resolved = Path(PersonEntityStore().db_path)

    assert resolved != _shared_default_crm_db_path()
    assert resolved.is_relative_to(tmp_path_factory.getbasetemp())


def test_person_entity_store_singleton_default_path_is_isolated(tmp_path_factory):
    """``get_person_entity_store()``'s lazily constructed singleton resolves
    the same isolated path as a direct ``PersonEntityStore()`` construction."""
    import api.services.person_entity as person_entity_mod

    person_entity_mod._entity_store = None
    resolved = Path(person_entity_mod.get_person_entity_store().db_path)
    person_entity_mod._entity_store = None

    assert resolved != _shared_default_crm_db_path()
    assert resolved.is_relative_to(tmp_path_factory.getbasetemp())
