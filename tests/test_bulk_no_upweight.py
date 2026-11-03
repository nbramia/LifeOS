"""Being named in bulk content changes nothing about a person, through the real indexer and stores.

The same note placed outside a bulk folder is the control: it must change what the bulk
note must not, or this test would pass whatever the code did.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.services.indexer import IndexerService
from api.services.person_entity import PersonEntity
from config.settings import settings

pytestmark = pytest.mark.unit

BULK_FOLDER = "Imports/Lists"
LIST_NOTE = """---
people: [Quillon Example]
tags: [people/quillon-example]
---
# Pledges

| Name | Email | Phone | Pledge |
|---|---|---|---|
| Quillon Example | quillon@example.org | (212) 555-0173 | $5,000 pledge |
| Robin Rival | robin@example.org | (212) 555-0174 | $9 |

**Quillon Example** spoke at length. [[Quillon Example]] pledged again.
"""


@pytest.fixture
def crm(tmp_path, monkeypatch):
    """The real people, interaction, fact and source-entity stores in a throwaway folder."""
    from api.services import entity_resolver, interaction_store, person_entity, person_facts, source_entity
    from api.services.entity_resolver import EntityResolver
    from api.services.interaction_store import InteractionStore
    from api.services.link_override import LinkOverrideStore
    from api.services.person_entity import PersonEntityStore
    from api.services.person_facts import PersonFactStore
    from api.services.source_entity import SourceEntityStore

    crm_db = str(tmp_path / "crm.db")
    interactions_db = str(tmp_path / "interactions.db")
    store = PersonEntityStore(crm_db)
    interactions = InteractionStore(interactions_db, strict=False)
    resolver = EntityResolver(store)
    sources = SourceEntityStore(crm_db)
    overrides = LinkOverrideStore(tmp_path / "crm.db")
    monkeypatch.setattr(interaction_store, "TEMP_PREFIXES", ())
    monkeypatch.setattr(interaction_store, "_interaction_store", interactions)
    monkeypatch.setattr(interaction_store, "get_interaction_db_path", lambda: interactions_db)
    monkeypatch.setattr(person_entity, "_entity_store", store)
    monkeypatch.setattr(entity_resolver, "_entity_resolver", resolver)
    monkeypatch.setattr(entity_resolver, "get_link_override_store", lambda: overrides)
    monkeypatch.setattr(person_facts, "_fact_store", PersonFactStore(crm_db))
    monkeypatch.setattr(source_entity, "_source_entity_store", sources, raising=False)
    monkeypatch.setattr(settings, "vault_no_people_paths_raw", BULK_FOLDER)
    monkeypatch.setattr(settings, "my_person_id", "", raising=False)

    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(settings, "vault_path", vault)
    indexer = IndexerService.__new__(IndexerService)
    indexer.vault_path = vault
    indexer._entity_resolver = resolver
    indexer._interaction_store = interactions
    indexer._source_entity_store = sources
    indexer._tag_store = MagicMock()
    indexer._tag_store.get.return_value = None
    indexer._tag_store_failed = False
    indexer.vector_store = MagicMock()
    indexer.bm25_index = MagicMock()

    named = store.add(PersonEntity(
        canonical_name="Quillon Example", emails=["quillon@example.org"], phone_numbers=["+12125550173"],
        category="personal",
    ))
    rival = store.add(PersonEntity(canonical_name="Quillon Rival", category="personal"))
    interactions.add_if_not_exists(_message("rival-1", rival.id))
    interactions.add_if_not_exists(_message("named-1", named.id, month=6))
    return store, interactions, indexer, vault, named, rival


def _message(row_id, person_id, month=9):
    from api.services.interaction_store import Interaction

    return Interaction(
        id=row_id, person_id=person_id, timestamp=datetime(2026, month, 1, tzinfo=timezone.utc),
        source_type="imessage", title="synthetic message", source_id=row_id,
    )


def _standing(crm):
    """Everything the task forbids a list from raising, recomputed the way the sync does."""
    from api.services.person_context import build_person_context
    from api.services.person_stats import refresh_person_stats, sync_last_seen
    from api.services.relationship_metrics import compute_all_dunbar_circles, compute_strength_for_person

    store, interactions, _, _, named, rival = crm
    refresh_person_stats([named.id, rival.id])
    sync_last_seen([named.id, rival.id])
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for person_id in (named.id, rival.id):
        person = store.get_by_id(person_id)
        person.relationship_strength = compute_strength_for_person(person, now=now)
        store.update(person)
    compute_all_dunbar_circles(store)

    person = store.get_by_id(named.id)
    context = build_person_context("what's new with Quillon Example?")
    return {
        "strength": person.relationship_strength,
        "circle": person.dunbar_circle,
        "last_seen": person.last_seen,
        "mentions": person.mention_count,
        "total_interactions": sum(
            count for count in interactions.get_interaction_counts(named.id).values()
        ),
        "sources": sorted(person.sources),
        "related_notes": list(person.related_notes),
        "rank": [p.id for p in store.search("Quillon", limit=10)],
        "people": store.count(),
        "block": context.block if context else None,
    }


def _index_list_note(crm, folder):
    _, _, indexer, vault, _, _ = crm
    path = vault / folder / "Pledges 2026-10-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LIST_NOTE)
    with patch("api.services.person_stats.refresh_person_stats"):
        indexer.index_file(str(path), skip_summaries=True)
    return path


CHANGED_BY_AN_ORDINARY_NOTE = ("last_seen", "mentions", "total_interactions", "sources", "related_notes")


class TestBulkNoteRaisesNobody:
    def test_nothing_about_anyone_changes(self, crm):
        before = _standing(crm)
        _index_list_note(crm, BULK_FOLDER)
        assert _standing(crm) == before

    def test_the_same_note_outside_a_bulk_folder_does_change_them(self, crm):
        before = _standing(crm)
        _index_list_note(crm, "Notes")
        after = _standing(crm)
        for field in CHANGED_BY_AN_ORDINARY_NOTE:
            assert after[field] != before[field], field
        assert after["rank"] != before["rank"]

    def test_no_source_entity_or_new_person_comes_from_a_row(self, crm):
        from api.services import source_entity

        store, _, _, _, _, _ = crm
        people_before = store.count()
        path = _index_list_note(crm, BULK_FOLDER)
        sources = source_entity._source_entity_store
        assert sources.get_by_source("vault", f"{path.resolve()}:Quillon Example") is None
        assert sources.get_by_source("vault", f"{path.resolve()}:Robin Rival") is None
        assert store.count() == people_before
        assert store.get_by_name("Robin Rival") is None

    def test_no_row_text_reaches_the_person_block_on_either_rendering(self, crm):
        from api.services.person_context import build_offbox_person_context, build_person_context

        _index_list_note(crm, BULK_FOLDER)
        for build in (build_person_context, build_offbox_person_context):
            context = build("what's new with Quillon Example?")
            assert context is not None
            for text in ("$5,000", "Pledges", "pledge", "robin@example.org", "Robin Rival", "555-0174"):
                assert text not in context.block
