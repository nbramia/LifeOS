"""Vault indexing must not create CRM people unless LIFEOS_VAULT_CREATE_PEOPLE is on.

Name extraction picks up bold headings and product names ("Eval", "Calendar") as if
they were people, so by default notes may only link to people who already exist.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.services.indexer import IndexerService
from config.settings import settings

pytestmark = pytest.mark.unit


class FakeResolver:
    """Knows one person. Records every resolve call; creates people only when asked to."""

    def __init__(self):
        self.calls = []
        self.store = MagicMock()
        self.known = SimpleNamespace(
            id="person-known", display_name="Alice Known", related_notes=[], last_seen=None, sources=[],
        )

    def resolve(self, name, context_path=None, create_if_missing=False):
        self.calls.append((name, create_if_missing))
        if name == "Alice Known":
            return SimpleNamespace(entity=self.known, confidence=1.0, match_type="exact")
        if create_if_missing:
            created = SimpleNamespace(
                id=f"person-{name.lower()}", display_name=name, related_notes=[], last_seen=None, sources=[],
            )
            return SimpleNamespace(entity=created, confidence=0.5, match_type="created")
        return None


def _indexer(resolver):
    indexer = IndexerService.__new__(IndexerService)
    indexer._entity_resolver = resolver
    indexer._interaction_store = MagicMock()
    indexer._interaction_store.add_if_not_exists.return_value = (None, True)
    indexer._source_entity_store = MagicMock()
    return indexer


NOTE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_setting_is_off_by_default():
    field = type(settings).model_fields["vault_create_people"]
    assert field.default is False


def test_unknown_names_are_not_created_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_create_people", False)
    resolver = FakeResolver()
    indexer = _indexer(resolver)
    note = tmp_path / "Plan.md"
    note.write_text("**Eval** and Alice Known")

    affected = indexer._sync_people_to_v2(note, ["Alice Known", "Eval", "Calendar"], NOTE)

    assert affected == {"person-known"}
    assert all(flag is False for _, flag in resolver.calls)
    assert indexer._interaction_store.add_if_not_exists.call_count == 1


def test_known_people_still_link_to_the_note(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_create_people", False)
    resolver = FakeResolver()
    indexer = _indexer(resolver)
    note = tmp_path / "Meeting.md"
    note.write_text("Alice Known attended")

    indexer._sync_people_to_v2(note, ["Alice Known"], NOTE)

    assert str(note.resolve()) in resolver.known.related_notes
    assert "vault" in resolver.known.sources
    resolver.store.update.assert_called_once_with(resolver.known)
    indexer._source_entity_store.add_or_update.assert_called_once()


def test_creation_can_be_turned_back_on(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_create_people", True)
    resolver = FakeResolver()
    indexer = _indexer(resolver)
    note = tmp_path / "Plan.md"
    note.write_text("Eval")

    affected = indexer._sync_people_to_v2(note, ["Eval"], NOTE)

    assert affected == {"person-eval"}
    assert resolver.calls == [("Eval", True)]


def test_a_result_without_an_entity_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_create_people", False)
    resolver = FakeResolver()
    resolver.resolve = MagicMock(return_value=SimpleNamespace(entity=None, confidence=0.0, match_type="none"))
    indexer = _indexer(resolver)
    note = tmp_path / "Plan.md"
    note.write_text("x")

    affected = indexer._sync_people_to_v2(note, ["Nobody"], NOTE)

    assert affected == set()
    indexer._interaction_store.add_if_not_exists.assert_not_called()
