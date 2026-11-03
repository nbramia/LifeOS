"""PersonEntity.past_organizations: storage, API responses, hand edits, search, and the index profile."""
import importlib
import sqlite3
from types import SimpleNamespace

import pytest

from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.person_indexer import _descriptive_lines, generate_person_document

pytestmark = pytest.mark.unit

PAST = [
    {"name": "Old Firm", "source": "linkedin_profile", "since": "2018", "until": "2019-04"},
    {"name": "Older Org", "source": "manual", "since": None, "until": None},
]


def _person(**fields):
    return PersonEntity(id="p1", canonical_name="Robin Example", past_organizations=PAST, **fields)


def _summary():
    return SimpleNamespace(
        relationship_strength=50, total_interactions_90d=0, channels=[], active_channels=[], primary_channel=None,
        contact_on_record=False, days_since_contact=0, has_facts=False,
    )


@pytest.fixture
def store(tmp_path):
    return PersonEntityStore(db_path=str(tmp_path / "crm.db"))


class TestStorage:
    def test_a_new_person_keeps_the_list_and_an_update_replaces_it(self, store):
        person = store.add(PersonEntity(canonical_name="Jane Doe", emails=["jane@example.com"], past_organizations=PAST))
        loaded = store.get_by_id(person.id)
        assert [e["name"] for e in loaded.past_organizations] == ["Old Firm", "Older Org"]
        loaded.past_organizations = [PAST[0]]
        store.update(loaded)
        assert [e["name"] for e in store.get_by_id(person.id).past_organizations] == ["Old Firm"]

    def test_a_person_has_none_by_default_and_a_dict_without_the_field_loads(self, store):
        person = store.add(PersonEntity(canonical_name="Jane Doe", emails=["jane@example.com"]))
        assert store.get_by_id(person.id).past_organizations == []
        data = person.to_dict()
        data.pop("past_organizations")
        assert PersonEntity.from_dict(data).past_organizations == []

    def test_a_database_without_the_column_gains_it_and_keeps_its_people(self, tmp_path):
        path = tmp_path / "old.db"
        PersonEntityStore(db_path=str(path))
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE person_entities DROP COLUMN past_organizations")
        conn.execute("INSERT INTO person_entities (id, canonical_name, display_name, emails) VALUES ('p1', 'Jane Doe', 'Jane Doe', '[]')")
        conn.commit()
        conn.close()
        assert PersonEntityStore(db_path=str(path)).get_by_id("p1").past_organizations == []


class TestResponses:
    def test_the_people_response_hides_the_current_company(self):
        from api.routes.people import _entity_to_response

        response = _entity_to_response(_person(company="Old Firm"), include_channels=False)
        assert [o["name"] for o in response.past_organizations] == ["Older Org"]
        assert response.company == "Old Firm"

    @pytest.mark.parametrize("module", ["api.routes.crm", "api.routes.crm_models._utils"])
    def test_the_crm_detail_response_and_search(self, module, tmp_path, monkeypatch):
        from api.services.source_entity import SourceEntityStore

        mod = importlib.import_module(module)
        sources = SourceEntityStore(db_path=str(tmp_path / "crm.db"))
        monkeypatch.setattr(mod, "get_source_entity_store", lambda: sources)
        convert = getattr(mod, "_person_to_detail_response", None) or mod.person_to_detail_response
        response = convert(_person(company="Old Firm"), include_related=False)
        assert [o["name"] for o in response.past_organizations] == ["Older Org"]
        assert mod._search_matches("older org", _person(company="Now Org"))
        assert mod._search_matches("old firm", _person(company="Now Org"))
        assert not mod._search_matches("unrelated", _person(company="Now Org"))

    def test_the_response_models_keep_every_existing_field(self):
        from api.routes.crm_models.models import PersonDetailResponse

        assert {"company", "position", "linkedin_url", "past_organizations"} <= set(PersonDetailResponse.model_fields)


class TestHandEdit:
    def test_a_patch_replaces_the_list_and_marks_every_entry_manual(self, store, tmp_path, monkeypatch):
        from api.routes import crm
        from api.services.source_entity import SourceEntityStore

        sources = SourceEntityStore(db_path=str(tmp_path / "crm.db"))
        monkeypatch.setattr(crm, "get_source_entity_store", lambda: sources)
        monkeypatch.setattr(crm, "get_person_entity_store", lambda: store)
        person = store.add(PersonEntity(canonical_name="Jane Doe", emails=["jane@example.com"], past_organizations=[PAST[0]]))

        response = crm.update_person(person.id, crm.PersonUpdateRequest(past_organizations=[
            {"name": "Hand Org", "until": "2020", "source": "linkedin_profile"}, {"name": "Freelance"}]))

        assert [(o["name"], o["source"]) for o in response.past_organizations] == [("Hand Org", "manual")]
        assert [(o["name"], o["source"]) for o in store.get_by_id(person.id).past_organizations] == [("Hand Org", "manual")]
        crm.update_person(person.id, crm.PersonUpdateRequest(notes="untouched list"))
        assert len(store.get_by_id(person.id).past_organizations) == 1
        crm.update_person(person.id, crm.PersonUpdateRequest(past_organizations=[]))
        assert store.get_by_id(person.id).past_organizations == []


class TestBadHandEdit:
    @pytest.mark.parametrize("bad", [
        {"company": "Wrong Key Org", "end_year": 2020},
        {"name": "", "until": "2020"},
        {"name": "   "},
        {"name": 5},
        {"name": "Two", "until": "March 2021"},
        {"name": "Two", "since": "2021/03"},
        {"name": "Two", "until": 2021},
    ])
    def test_a_bad_entry_is_refused_and_the_stored_list_is_kept(self, store, tmp_path, monkeypatch, bad):
        from fastapi import HTTPException

        from api.routes import crm

        monkeypatch.setattr(crm, "get_person_entity_store", lambda: store)
        kept = [{"name": "Hand Org", "source": "manual", "since": "2015", "until": "2017"}]
        person = store.add(PersonEntity(canonical_name="Jane Doe", emails=["jane@example.com"], past_organizations=kept, notes="before"))

        with pytest.raises(HTTPException) as refused:
            crm.update_person(person.id, crm.PersonUpdateRequest(
                notes="changed", past_organizations=[{"name": "Good Org", "until": "2020"}, bad]))

        assert refused.value.status_code == 422 and "past_organizations[1]" in refused.value.detail
        stored = store.get_by_id(person.id)
        assert stored.past_organizations == kept and stored.notes == "before"

    def test_the_dates_a_valid_edit_may_use(self, store, tmp_path, monkeypatch):
        from api.routes import crm
        from api.services.source_entity import SourceEntityStore

        monkeypatch.setattr(crm, "get_source_entity_store", lambda: SourceEntityStore(db_path=str(tmp_path / "crm.db")))
        monkeypatch.setattr(crm, "get_person_entity_store", lambda: store)
        person = store.add(PersonEntity(canonical_name="Jane Doe", emails=["jane@example.com"]))
        crm.update_person(person.id, crm.PersonUpdateRequest(past_organizations=[
            {"name": "A", "since": "2010", "until": "2012-06"}, {"name": "B", "since": None, "until": ""}, {"name": "C", "until": "2014-02-03"}]))
        assert sorted(e["name"] for e in store.get_by_id(person.id).past_organizations) == ["A", "B", "C"]


class TestIndexProfile:
    def test_the_profile_lists_them_after_the_current_role(self):
        lines = generate_person_document(_person(company="Now Org", position="Lead"), _summary()).splitlines()
        assert lines.index("Previously worked at Old Firm (until 2019), Older Org") == lines.index("Works at Now Org as Lead") + 1

    def test_the_current_company_is_not_listed_as_past(self):
        doc = generate_person_document(_person(company="old firm inc"), _summary())
        assert [ln for ln in doc.splitlines() if ln.startswith("Previously worked at")] == ["Previously worked at Older Org"]

    def test_no_line_without_any_and_search_reads_the_line(self):
        assert "Previously worked at" not in generate_person_document(PersonEntity(id="p1", canonical_name="R E"), _summary())
        assert "Previously worked at Old Firm" in _descriptive_lines(generate_person_document(_person(company="Now"), _summary()))
