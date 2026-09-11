"""Regression tests for #952: xdist workers sharing one lazily-created
``data/crm.db``.

``get_crm_db_path()`` (``api/utils/db_paths.py``) resolves from
``settings.chroma_path``, which defaults to the *relative* ``./data/chromadb``
-- resolved against the pytest process's cwd. Every xdist worker shares that
cwd, so a plain test that constructs a default-path store (e.g.
``RelationshipStore()``) races every other worker's default-path store over
the same file: ``RelationshipStore._init_db()`` connects (creating a
zero-page file) and only then issues ``CREATE TABLE IF NOT EXISTS`` -- a
second worker connecting in that window sees an empty database and raises
``sqlite3.OperationalError: no such table: relationships``.

A deterministic red-then-green reproduction of the race itself is not
practical (it requires two real xdist worker processes racing a narrow
connect-then-create-table window). Instead, these tests pin the ownership
property the fix establishes: the default CRM db path this process resolves
to is not the literal shared, cwd-relative ``data/crm.db`` -- it is a path
owned by this test/worker. Reverting the ``conftest.py`` fixture under test
makes ``test_get_crm_db_path_is_isolated_from_shared_default`` fail (it
resolves to the literal relative path); this file's own tests never
monkeypatch ``get_crm_db_path`` themselves, so the isolation exercised here
comes entirely from the autouse conftest fixture.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _unpatched_default_crm_db_path() -> Path:
    """The crm.db path a fresh, un-monkeypatched ``Settings()`` would resolve
    to -- i.e. the one every xdist worker shares absent this fix, whatever
    the un-isolated default value of ``chroma_path`` itself happens to be."""
    from config.settings import Settings

    return Path(Settings().chroma_path).parent / "crm.db"


def test_get_crm_db_path_is_isolated_from_shared_default(tmp_path_factory):
    """The default CRM db path must not be the shared path every xdist
    worker would otherwise resolve to from an un-isolated ``Settings()``."""
    from api.utils.db_paths import get_crm_db_path

    resolved = Path(get_crm_db_path())

    assert resolved != _unpatched_default_crm_db_path()
    # It must be owned by *this test's* pytest-managed temp tree (unique per
    # xdist worker), not merely different by coincidence.
    assert resolved.is_relative_to(tmp_path_factory.getbasetemp())


def test_relationship_store_default_path_matches_get_crm_db_path():
    """``RelationshipStore()`` (no explicit ``db_path``) resolves through
    ``get_crm_db_path()`` -- confirming the isolation fixture actually
    reaches the store construction path most call sites use, not just the
    bare path-resolution function."""
    from api.services.relationship import RelationshipStore
    from api.utils.db_paths import get_crm_db_path

    store = RelationshipStore()
    assert store.db_path == get_crm_db_path()
    assert Path(store.db_path) != _unpatched_default_crm_db_path()
