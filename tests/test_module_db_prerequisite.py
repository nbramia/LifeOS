"""Tests for the database prerequisite a test module establishes for itself.

``require_db`` gates tests that read the runtime interactions database. Its
probe must report unavailable for the preconditions a test module cannot
satisfy -- a running server holding the write lock, an unresolvable path --
and available once the module has created the table it reads.
"""
import inspect
import sqlite3

import pytest

from config.settings import settings
from tests.conftest import (
    establish_interaction_schema,
    interaction_db_readable,
    runtime_databases_populated,
)


@pytest.fixture
def candidate_data_dir(tmp_path, monkeypatch):
    """Point both default runtime database paths at an empty directory."""
    from api.services.person_entity import PersonEntityStore

    monkeypatch.setattr(settings, "chroma_path", tmp_path / "chromadb")
    monkeypatch.setattr(PersonEntityStore, "CRM_DB_PATH", tmp_path / "crm.db")
    return tmp_path


@pytest.mark.unit
class TestInteractionSchemaPrerequisite:
    """The schema a module needs is established by the module itself."""

    def test_probe_reports_unavailable_before_the_schema_exists(self, candidate_data_dir):
        assert interaction_db_readable() is False

    def test_probe_reports_available_once_the_module_establishes_it(self, candidate_data_dir):
        establish_interaction_schema()

        assert interaction_db_readable() is True
        assert (candidate_data_dir / "interactions.db").exists()

    def test_establishing_an_existing_schema_keeps_its_rows(self, candidate_data_dir):
        establish_interaction_schema()
        db_path = str(candidate_data_dir / "interactions.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO interactions (id, person_id, timestamp, source_type, title)"
                " VALUES ('i-1', 'p-1', '2026-01-01T00:00:00+00:00', 'vault', 'Synthetic note')"
            )
            conn.commit()
        finally:
            conn.close()

        establish_interaction_schema()

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1
        finally:
            conn.close()

    def test_establishing_the_schema_never_raises_on_an_unusable_path(self, tmp_path, monkeypatch):
        # A file where the data directory must be: the path cannot resolve, so
        # the probe stays the one place that decides to skip.
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(settings, "chroma_path", blocked / "chromadb")

        establish_interaction_schema()

        assert interaction_db_readable() is False


@pytest.mark.unit
class TestRequireDbRealPrecondition:
    """The probe still reports unavailable for a genuinely locked database."""

    def test_probe_reports_unavailable_while_a_writer_holds_the_lock(self, candidate_data_dir):
        establish_interaction_schema()
        db_path = str(candidate_data_dir / "interactions.db")
        writer = sqlite3.connect(db_path)
        try:
            # Rollback-journal mode makes an exclusive write transaction block
            # readers, which is how a running server's lock presents.
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute(
                "INSERT INTO interactions (id, person_id, timestamp, source_type, title)"
                " VALUES ('i-2', 'p-2', '2026-01-01T00:00:00+00:00', 'vault', 'Synthetic note')"
            )

            assert interaction_db_readable() is False
        finally:
            writer.rollback()
            writer.close()

        assert interaction_db_readable() is True


@pytest.mark.unit
class TestPopulatedDatabasePrecondition:
    """Records, not a schema, are what the data-integrity checks require."""

    def test_an_empty_schema_is_not_populated(self, candidate_data_dir):
        establish_interaction_schema()

        assert runtime_databases_populated() is False

    def test_a_missing_database_is_not_populated(self, candidate_data_dir):
        assert runtime_databases_populated() is False

    def test_records_in_both_databases_are_populated(self, candidate_data_dir):
        from api.services.person_entity import PersonEntity, PersonEntityStore

        crm_path = candidate_data_dir / "crm.db"
        PersonEntityStore(str(crm_path)).add(
            PersonEntity(id="p-3", canonical_name="Jordan Sample")
        )

        establish_interaction_schema()
        conn = sqlite3.connect(str(candidate_data_dir / "interactions.db"))
        try:
            conn.execute(
                "INSERT INTO interactions (id, person_id, timestamp, source_type, title)"
                " VALUES ('i-3', 'p-3', '2026-01-01T00:00:00+00:00', 'vault', 'Synthetic note')"
            )
            conn.commit()
        finally:
            conn.close()

        assert runtime_databases_populated() is True

    def test_the_probe_does_not_create_a_database_it_reads(self, candidate_data_dir):
        assert runtime_databases_populated() is False
        assert not (candidate_data_dir / "crm.db").exists()
        assert not (candidate_data_dir / "interactions.db").exists()


@pytest.mark.unit
class TestFixtureComposition:
    """The gates compose so both preconditions apply to the same test."""

    def test_the_populated_gate_keeps_the_lock_gate_in_its_closure(self):
        # `_isolate_integration_persistent_stores` recognizes a test that reads
        # the developer's own databases by finding `require_db` in its fixture
        # closure, and leaves the default store paths alone for it.
        from tests import conftest

        assert "require_db" in inspect.signature(conftest.require_populated_db).parameters
        assert "interaction_schema" in inspect.signature(conftest.require_db).parameters
