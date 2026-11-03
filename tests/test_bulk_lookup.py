"""Looking a person up in bulk-content lists on request, over a real keyword index."""
import logging
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import crm
from api.services import bulk_lookup
from api.services.bm25_index import BM25Index
from api.services.bulk_content import bulk_path_matcher
from api.services.bulk_lookup import find_list_matches
from api.services.person_entity import PersonEntity, PersonEntityStore
from config.settings import settings

pytestmark = pytest.mark.unit

BULK_FOLDER = "Imports/Lists"


class Lists:
    def __init__(self, tmp_path):
        self.vault = tmp_path / "vault"
        self.vault.mkdir()
        self.index = BM25Index(db_path=str(tmp_path / "bm25.db"))

    def add(self, name, body, chunk=0, folder=BULK_FOLDER):
        path = f"{self.vault.resolve().as_posix()}/{folder}/{name}.md"
        self.index.add_document(f"{path}_{chunk}", f"This chunk is from {name}.md.\n\n{body}", f"{name}.md")
        return path


@pytest.fixture
def lists(tmp_path, monkeypatch):
    built = Lists(tmp_path)
    monkeypatch.setattr(settings, "vault_path", built.vault)
    monkeypatch.setattr(settings, "vault_no_people_paths_raw", BULK_FOLDER)
    return built


def person(name="Robin Gamma", emails=(), phones=()):
    return PersonEntity(canonical_name=name, emails=list(emails), phone_numbers=list(phones))


def find(lists, who):
    return find_list_matches(who, lists.index, bulk_path_matcher())


class TestMatching:
    def test_email_finds_the_list_name_and_row_in_any_case(self, lists):
        lists.add("Donors 2020", "| Robin | Gamma | ROBIN.GAMMA@Example.com | $100 |")
        matches, truncated = find(lists, person(name="", emails=["robin.gamma@example.com"]))
        assert [(m.list_name, m.match_type, m.row) for m in matches] == [
            ("Donors 2020", "email", "Robin | Gamma | ROBIN.GAMMA@Example.com | $100"),
        ]
        assert truncated is False

    @pytest.mark.parametrize("row", [
        "| a.robin.gamma@example.com |", "| robin.gamma@example.com.au |", "| robin gamma example com |",
    ])
    def test_a_longer_address_holding_the_same_tokens_is_not_a_match(self, lists, row):
        lists.add("Donors", row)
        assert find(lists, person(name="", emails=["robin.gamma@example.com"]))[0] == []

    @pytest.mark.parametrize("written", [
        "(212) 555-0173", "212-555-0173", "212.555.0173", "2125550173", "+12125550173", "+1 (212) 555-0173",
    ])
    def test_written_forms_of_a_us_number_match(self, lists, written):
        lists.add("Donors", f"| Robin | {written} | $5 |")
        assert [m.match_type for m in find(lists, person(name="", phones=["+12125550173"]))[0]] == ["phone"]

    @pytest.mark.parametrize("near_miss", ["9212-555-0173", "212-555-01734", "212-555-0174"])
    def test_a_different_number_in_the_same_chunk_is_not_a_row(self, lists, near_miss):
        lists.add("Donors", f"| Quillon | {near_miss} | | Robin | 212-555-0173 | $5 |")
        matches, _ = find(lists, person(name="", phones=["+12125550173"]))
        assert [m.row for m in matches] == ["Robin | 212-555-0173 | $5"]

    def test_a_number_outside_the_us_matches_whole_and_a_short_one_not_at_all(self, lists):
        lists.add("Donors", "| Robin | +442079460958 | 555 | 312-345-6789 |")
        assert len(find(lists, person(name="", phones=["+44 20 7946 0958"]))[0]) == 1
        assert find(lists, person(name="", phones=["+33123456789"]))[0] == []
        assert find(lists, person(name="", phones=["555"]))[0] == []

    @pytest.mark.parametrize("written", ["Robin Gamma", "robin gamma", "Gamma, Robin", "Robin | Gamma"])
    def test_written_forms_match_as_a_name(self, lists, written):
        lists.add("Donors", f"| x | {written} | $5 |")
        assert [m.match_type for m in find(lists, person())[0]] == ["name"]

    @pytest.mark.parametrize("written", ["Robin Gammas", "Robina Gamma", "Robin Q. Gamma", "Robin | | Gamma", "Gamma Robin"])
    def test_a_different_name_is_not_a_match(self, lists, written):
        lists.add("Donors", f"| x | {written} | $5 |")
        assert find(lists, person())[0] == []

    @pytest.mark.parametrize("name", ["Robin", "+12125550173", "Robin 2"])
    def test_a_name_that_cannot_identify_anyone_is_not_looked_up(self, lists, name):
        lists.add("Donors", f"| {name} | $5 |")
        assert find(lists, person(name=name))[0] == []

    def test_only_rows_of_bulk_notes_count(self, lists):
        lists.add("Plan", "| Robin Gamma | x |", folder="Notes")
        lists.add("Robin Gamma pledges", "| someone else | $5 |")
        path = lists.add("Donors", "| x |")
        lists.index.add_document(f"{path}::summary", "Summary\n\nRobin Gamma leads this list", "Donors.md")
        assert find(lists, person())[0] == []

    def test_no_file_is_opened(self, lists):
        lists.add("Donors", "| Robin Gamma |")
        with patch("builtins.open", side_effect=AssertionError("read a file")):
            assert len(find(lists, person())[0]) == 1


class TestRows:
    def test_a_row_stops_at_the_next_row_in_a_flattened_table(self, lists):
        lists.add("Donors", "| Quillon | $9 | | Robin | robin@example.com | $5 | | Mirel | $7 |")
        assert [m.row for m in find(lists, person(name="", emails=["robin@example.com"]))[0]] == [
            "Robin | robin@example.com | $5",
        ]

    def test_a_line_break_ends_the_row(self, lists):
        lists.add("Donors", "a | above\nRobin | robin@example.com\nbelow | z")
        assert [m.row for m in find(lists, person(name="", emails=["robin@example.com"]))[0]] == [
            "Robin | robin@example.com",
        ]

    def test_plain_text_is_bounded_on_both_sides(self, lists):
        lists.add("Donors", "w " * 400 + "robin@example.com" + " y" * 400)
        row = find(lists, person(name="", emails=["robin@example.com"]))[0][0].row
        left, right = row.split("robin@example.com")
        assert bulk_lookup.ROW_REACH // 2 - 1 <= left.count("w") <= bulk_lookup.ROW_REACH // 2
        assert bulk_lookup.ROW_REACH // 2 - 1 <= right.count("y") <= bulk_lookup.ROW_REACH // 2

    @pytest.mark.parametrize("first,second,kept", [
        ("| Robin Gamma | robin@example.com | $5 | more", "| Robin Gamma | robin@example.com | $5 |",
         "Robin Gamma | robin@example.com | $5 | more"),
        ("| Robin Gamma | robin@example.com | $5 |", "| Robin Gamma | robin@example.com | $5 | more",
         "Robin Gamma | robin@example.com | $5"),
    ])
    def test_a_row_is_not_repeated_across_kinds_or_overlapping_chunks(self, lists, first, second, kept):
        lists.add("Donors", first, chunk=0)
        lists.add("Donors", second, chunk=1)
        matches, _ = find(lists, person(emails=["robin@example.com"]))
        assert [(m.match_type, m.row) for m in matches] == [("email", kept)]

    def test_the_same_row_in_two_lists_is_two_matches_strongest_kind_first(self, lists):
        lists.add("Names", "| Robin Gamma | x |")
        lists.add("Phones", "| y | 212-555-0173 |")
        lists.add("Emails", "| z | robin@example.com |")
        matches, _ = find(lists, person(emails=["robin@example.com"], phones=["+12125550173"]))
        assert [m.match_type for m in matches] == ["email", "phone", "name"]

    def test_matches_and_chunks_are_bounded_and_say_so(self, lists, monkeypatch):
        for i in range(4):
            lists.add(f"List {i}", "| Robin Gamma |")
        monkeypatch.setattr(bulk_lookup, "MAX_MATCHES", 2)
        matches, truncated = find(lists, person())
        assert (len(matches), truncated) == (2, True)
        monkeypatch.setattr(bulk_lookup, "MAX_MATCHES", 4)
        assert find(lists, person())[1] is False
        monkeypatch.setattr(bulk_lookup, "MAX_CHUNKS_PER_LOOKUP", 3)
        matches, truncated = find(lists, person())
        assert (len(matches), truncated) == (3, True)

    def test_phrases_match_adjacent_tokens_in_the_text_only(self, lists):
        lists.add("Adjacent", "robin gamma")
        lists.add("Apart", "robin x gamma")
        lists.index.add_document("/robin gamma.md_0", "other", "robin gamma.md", people=["robin gamma"])
        chunks = lists.index.chunks_with_phrases(["@@", "robin gamma"], lambda doc_id: True, 10)
        assert [c["file_name"] for c in chunks] == ["Adjacent.md"]


@pytest.fixture
def route(lists, tmp_path):
    store = PersonEntityStore(str(tmp_path / "crm.db"))
    app = FastAPI()
    app.include_router(crm.router)
    with patch.object(crm, "get_person_entity_store", return_value=store), patch.object(crm, "get_bm25_index", return_value=lists.index):
        yield TestClient(app), store


class TestEndpoint:
    def test_returns_rows_and_is_not_cached(self, lists, route):
        client, store = route
        lists.add("Names", "| Robin Gamma | $7 |")
        response = client.get(f"/api/crm/people/{store.add(person()).id}/list-matches")
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "matches": [{"list_name": "Names", "row": "Robin Gamma | $7", "match_type": "name"}],
            "truncated": False,
        }

    def test_reports_when_more_rows_may_exist(self, lists, route, monkeypatch):
        client, store = route
        monkeypatch.setattr(bulk_lookup, "MAX_MATCHES", 1)
        lists.add("A", "| Robin Gamma |")
        lists.add("B", "| Robin Gamma |")
        assert client.get(f"/api/crm/people/{store.add(person()).id}/list-matches").json()["truncated"] is True

    def test_unknown_person_is_a_404(self, route):
        assert route[0].get("/api/crm/people/nobody/list-matches").status_code == 404

    def test_hidden_person_returns_nothing(self, lists, route):
        client, store = route
        lists.add("Names", "| Robin Gamma | $7 |")
        added = store.add(person())
        added.hidden = True
        store.update(added)
        assert client.get(f"/api/crm/people/{added.id}/list-matches").json() == {"matches": [], "truncated": False}

    def test_merged_away_id_returns_nothing_even_when_its_row_is_gone(self, lists, route):
        client, store = route
        lists.add("Names", "| Robin Gamma | $7 |")
        survivor = store.add(person())
        merged = store.add(person(name="Robin Gamma Old"))
        store._merged_ids[merged.id] = survivor.id
        store._merged_ids["gone-id"] = survivor.id
        for person_id in (merged.id, "gone-id"):
            assert client.get(f"/api/crm/people/{person_id}/list-matches").json() == {"matches": [], "truncated": False}
        assert client.get(f"/api/crm/people/{survivor.id}/list-matches").json()["matches"]

    def test_no_bulk_folder_says_so_rather_than_finding_nothing(self, route, monkeypatch):
        client, store = route
        added = store.add(person())
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", "")
        response = client.get(f"/api/crm/people/{added.id}/list-matches")
        assert response.status_code == 409
        assert "matches" not in response.json()

    @pytest.mark.parametrize("raw,enabled", [(BULK_FOLDER, True), ("", False)])
    def test_config_says_whether_the_lookup_is_available(self, route, monkeypatch, raw, enabled):
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", raw)
        assert route[0].get("/api/crm/config").json()["list_lookup_enabled"] is enabled

    def test_no_row_text_reaches_the_logs(self, lists, route, caplog):
        client, store = route
        lists.add("Sensitive List", "| Robin Gamma | robin@example.com | $5,000 pledge |")
        added = store.add(person(emails=["robin@example.com"]))
        with caplog.at_level(logging.DEBUG):
            assert client.get(f"/api/crm/people/{added.id}/list-matches").json()["matches"]
        for text in ("robin@example.com", "$5,000", "Sensitive List", "Robin Gamma"):
            assert text not in caplog.text


class TestStaysOnRequest:
    @pytest.mark.parametrize("module", [
        "api/services/person_context.py", "api/services/agent_tools.py", "api/services/briefings.py",
        "api/services/meeting_prep.py", "api/services/hybrid_search.py", "api/routes/chat.py",
        "api/routes/hermes_proxy.py", "mcp_server.py",
    ])
    def test_nothing_a_chat_turn_runs_reaches_the_lookup(self, module):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / module).read_text()
        assert "bulk_lookup" not in source and "find_list_matches" not in source and "list-matches" not in source
