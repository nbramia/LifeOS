"""Social links, labeled contact info, migration, matching, and export reading."""
import csv
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.contact_export_import import import_contact_export, map_columns, read_row_values
from api.services.entity_resolver import EntityResolver
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.social_links import (
    InvalidContactDetail,
    build_social_link,
    contact_entry_id,
    find_account_for_url,
    normalize_label,
    parse_social_value,
    social_link_id,
)
from config.social_platforms import SOCIAL_PLATFORMS

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return PersonEntityStore(str(tmp_path / "crm.db"))


def _person(store, name="Test Person One", **fields):
    return store.add(PersonEntity(canonical_name=name, **fields))


class TestProfileParsing:
    def test_required_platforms_are_registered(self):
        assert {p.key for p in SOCIAL_PLATFORMS} == {
            "linkedin", "x", "facebook", "instagram", "telegram", "tiktok", "youtube", "discord",
        }

    @pytest.mark.parametrize("platform,spellings,expected_key", [
        ("linkedin", ["https://www.linkedin.com/in/Test-User/", "http://uk.linkedin.com/in/test-user?trk=x",
                      "linkedin.com/in/test-user/details/skills", "test-user"], "test-user"),
        ("x", ["https://twitter.com/TestUser", "https://x.com/testuser/", "@TestUser"], "testuser"),
        ("facebook", ["https://www.facebook.com/test.user", "m.facebook.com/Test.User/", "test.user"], "test.user"),
        ("facebook", ["https://www.facebook.com/profile.php?id=123456789", "https://facebook.com/123456789",
                      "https://www.facebook.com/people/Test-User/123456789/", "123456789"], "id:123456789"),
        ("instagram", ["https://instagram.com/Test_User/", "@test_user"], "test_user"),
        ("telegram", ["https://t.me/TestUser", "https://telegram.me/s/testuser", "@testuser"], "testuser"),
        ("tiktok", ["https://www.tiktok.com/@TestUser/video/1", "@testuser"], "testuser"),
        ("youtube", ["https://www.youtube.com/@TestChannel", "@testchannel"], "testchannel"),
        ("youtube", ["https://youtube.com/channel/UCabcDEF"], "channel:UCabcDEF"),
        ("discord", ["https://discord.com/users/123456789012345678", "123456789012345678"], "id:123456789012345678"),
        ("discord", ["Test_User", "@test_user"], "test_user"),
    ])
    def test_spellings_of_one_account_share_a_key(self, platform, spellings, expected_key):
        assert {parse_social_value(platform, s).match_key for s in spellings} == {expected_key}

    def test_doubled_export_url_resolves_to_the_inner_profile(self):
        doubled = "https://www.facebook.com/https://www.facebook.com/test.user"
        assert parse_social_value("facebook", doubled).match_key == "test.user"

    def test_non_ascii_linkedin_slug_encoded_and_plain_match(self):
        assert (parse_social_value("linkedin", "https://linkedin.com/in/test-cfa®").match_key
                == parse_social_value("linkedin", "https://linkedin.com/in/test-cfa%C2%AE").match_key)

    @pytest.mark.parametrize("platform,value", [
        ("linkedin", "https://www.facebook.com/test.user"),
        ("facebook", "https://www.facebook.com/groups/123456"),
        ("x", "https://x.com/i/status/1"),
        ("instagram", "https://instagram.com/p/abc123"),
        ("linkedin", "javascript:alert(1)"),
        ("telegram", "abc"),
        ("nosuch", "someone"),
        ("x", ""),
    ])
    def test_invalid_values_are_rejected(self, platform, value):
        with pytest.raises(InvalidContactDetail):
            parse_social_value(platform, value)

    def test_url_detection_identifies_the_platform(self):
        platform, account = find_account_for_url("https://www.facebook.com/test.user")
        assert (platform, account.match_key) == ("facebook", "test.user")
        assert find_account_for_url("https://example.com/test.user") is None
        assert find_account_for_url("Test Person") is None

    def test_labels_are_free_text_and_bounded(self):
        assert normalize_label("  my   finsta ") == "my finsta"
        assert normalize_label("   ") is None
        with pytest.raises(InvalidContactDetail):
            normalize_label("x" * 65)


def _legacy_database(path, rows):
    """Create a person_entities table as it exists before the structured columns."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE person_entities (
        id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '',
        emails TEXT NOT NULL DEFAULT '[]', company TEXT, position TEXT, linkedin_url TEXT,
        category TEXT NOT NULL DEFAULT 'unknown', vault_contexts TEXT NOT NULL DEFAULT '[]',
        sources TEXT NOT NULL DEFAULT '[]', first_seen TEXT, last_seen TEXT,
        meeting_count INTEGER NOT NULL DEFAULT 0, email_count INTEGER NOT NULL DEFAULT 0,
        mention_count INTEGER NOT NULL DEFAULT 0, message_count INTEGER NOT NULL DEFAULT 0,
        slack_message_count INTEGER NOT NULL DEFAULT 0, photo_count INTEGER NOT NULL DEFAULT 0,
        related_notes TEXT NOT NULL DEFAULT '[]', aliases TEXT NOT NULL DEFAULT '[]',
        phone_numbers TEXT NOT NULL DEFAULT '[]', phone_primary TEXT,
        confidence_score REAL NOT NULL DEFAULT 1.0, tags TEXT NOT NULL DEFAULT '[]',
        notes TEXT NOT NULL DEFAULT '', source_entity_count INTEGER NOT NULL DEFAULT 0, birthday TEXT,
        hidden INTEGER NOT NULL DEFAULT 0, hidden_at TEXT, hidden_reason TEXT NOT NULL DEFAULT '',
        relationship_strength REAL, is_peripheral_contact INTEGER NOT NULL DEFAULT 0, dunbar_circle INTEGER)""")
    for person_id, name, emails, phones, linkedin in rows:
        conn.execute(
            "INSERT INTO person_entities (id, canonical_name, display_name, emails, phone_numbers, linkedin_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, name, name, json.dumps(emails), json.dumps(phones), linkedin),
        )
    conn.commit()
    conn.close()


class TestMigration:
    LINKEDIN = "https://linkedin.com/in/test-person-one"

    def _migrated(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        _legacy_database(path, [
            ("p1", "Test Person One", ["one@example.com", "One.Alt@example.com"], ["+15550100001"], self.LINKEDIN),
            ("p2", "Test Person Two", [], [], "not a recognizable profile"),
            ("p3", "Test Person Three", ["three@example.com"], [], None),
        ])
        return PersonEntityStore(path), path

    def test_existing_values_move_into_the_new_structure(self, tmp_path):
        store, _ = self._migrated(tmp_path)
        person = store.get_by_id("p1")
        assert person.linkedin_url == self.LINKEDIN
        assert [(item["platform"], item["value"], item["match_key"]) for item in person.social_links] == [
            ("linkedin", self.LINKEDIN, "test-person-one"),
        ]
        assert person.emails == ["one@example.com", "One.Alt@example.com"]
        assert [e["value"] for e in person.email_entries] == person.emails
        assert person.phone_entries == [{"value": "+15550100001", "label": None}]

    def test_unparseable_linkedin_value_is_kept(self, tmp_path):
        store, _ = self._migrated(tmp_path)
        person = store.get_by_id("p2")
        assert person.linkedin_url == "not a recognizable profile"
        assert person.social_links[0]["value"] == "not a recognizable profile"
        assert person.social_links[0]["match_key"] is None

    def test_migration_is_idempotent_and_keeps_later_edits(self, tmp_path):
        store, path = self._migrated(tmp_path)
        person = store.get_by_id("p1")
        person.label_contact_entry("email", "one@example.com", "work")
        store.update(person)

        again = PersonEntityStore(path).get_by_id("p1")

        assert len(again.social_links) == 1
        assert again.email_entries[0]["label"] == "work"

    def test_startup_after_migration_needs_no_write_lock(self, tmp_path):
        _, path = self._migrated(tmp_path)
        statements = []
        conn = sqlite3.connect(path)
        conn.set_trace_callback(statements.append)
        PersonEntityStore._backfill_contact_details(conn)
        conn.close()
        assert not any("BEGIN" in statement for statement in statements)

    def test_migrated_linkedin_is_matchable_without_a_rewrite(self, tmp_path):
        store, _ = self._migrated(tmp_path)
        assert store.get_by_social_link("linkedin", "https://www.linkedin.com/in/Test-Person-One/").id == "p1"


class TestLinkedinFieldStaysInStep:
    def test_direct_assignment_adds_a_primary_link(self, store):
        person = _person(store)
        person.linkedin_url = "https://linkedin.com/in/test-primary"
        saved = store.update(person)
        assert saved.social_links[0]["match_key"] == "test-primary"

    def test_clearing_the_field_removes_the_account(self, store):
        person = _person(store, linkedin_url="https://linkedin.com/in/test-wrong-match")
        person.linkedin_url = None
        store.update(person)
        reloaded = store.get_by_id(person.id)
        assert reloaded.linkedin_url is None
        assert reloaded.social_links == []
        assert store.get_by_social_link("linkedin", "test-wrong-match") is None

    def test_clearing_promotes_the_next_account(self, store):
        person = _person(store, linkedin_url="https://linkedin.com/in/test-first")
        person.add_social_link(build_social_link("linkedin", "test-second"))
        store.update(person)
        person.linkedin_url = None
        store.update(person)
        assert store.get_by_id(person.id).linkedin_url == "https://www.linkedin.com/in/test-second"

    def test_several_accounts_on_one_platform(self, store):
        person = _person(store)
        person.add_social_link(build_social_link("instagram", "test_main", "personal"))
        person.add_social_link(build_social_link("instagram", "test_finsta", "finsta"))
        store.update(person)
        reloaded = store.get_by_id(person.id)
        assert [(item["handle"], item["label"]) for item in reloaded.social_links] == [
            ("test_main", "personal"), ("test_finsta", "finsta"),
        ]


class TestMatching:
    def test_facebook_url_matches_like_linkedin(self, store):
        person = _person(store)
        person.add_social_link(build_social_link("facebook", "https://www.facebook.com/profile.php?id=123456789"))
        store.update(person)
        found = EntityResolver(store).resolve_by_social_link("facebook", "https://m.facebook.com/123456789/")
        assert found.id == person.id

    def test_search_finds_a_person_by_profile_url(self, store):
        person = _person(store)
        person.add_social_link(build_social_link("tiktok", "@test_user"))
        store.update(person)
        assert [p.id for p in store.search("https://www.tiktok.com/@Test_User")] == [person.id]

    def test_crm_people_search_matches_a_pasted_profile_url(self):
        from api.routes.crm import _search_matches

        person = PersonEntity(canonical_name="Unrelated Name",
                              social_links=[build_social_link("facebook", "test.user")])
        assert _search_matches("https://www.facebook.com/Test.User/", person)
        assert not _search_matches("https://www.facebook.com/someone.else", person)

    def test_account_held_by_two_people_is_not_a_match(self, store):
        shared = "https://linkedin.com/in/test-shared"
        _person(store, "Test Person One", linkedin_url=shared)
        _person(store, "Test Person Two", linkedin_url=shared)
        assert store.get_by_social_link("linkedin", shared) is None

    def test_hidden_people_do_not_match(self, store):
        person = _person(store, linkedin_url="https://linkedin.com/in/test-hidden")
        store.hide_person(person.id)
        assert store.get_by_social_link("linkedin", "test-hidden") is None

    def test_linkedin_sync_matches_url_before_name_and_email(self, store):
        person = _person(store, "Test Maiden Name", linkedin_url="https://linkedin.com/in/test-stable")
        result = EntityResolver(store).resolve_from_linkedin(
            first_name="Test", last_name="Married", email=None, company="Example Co",
            position="Analyst", linkedin_url="https://www.linkedin.com/in/test-stable/",
        )
        assert result.entity.id == person.id
        assert result.match_type == "linkedin_url_exact"
        assert result.entity.company == "Example Co"

    def test_sync_cannot_restore_a_removed_account(self, store):
        person = _person(store, linkedin_url="https://linkedin.com/in/test-removed")
        person.remove_social_link(social_link_id(person.social_links[0]))
        store.update(person)

        reloaded = store.get_by_id(person.id)
        reloaded.linkedin_url = "https://linkedin.com/in/test-removed"
        store.update(reloaded)

        final = store.get_by_id(person.id)
        assert final.social_links == []
        assert not final.linkedin_url


class TestMerge:
    def test_merge_unions_accounts_and_fills_labels(self):
        primary = PersonEntity(canonical_name="Test One", emails=["same@example.com"],
                               social_links=[build_social_link("x", "@test_one")])
        other = PersonEntity(
            canonical_name="Test Uno", emails=["same@example.com"],
            email_entries=[{"value": "same@example.com", "label": "work"}],
            social_links=[build_social_link("x", "https://x.com/test_one", "main"),
                          build_social_link("linkedin", "test-one")],
        )
        merged = primary.merge(other)
        assert [(item["platform"], item["label"]) for item in merged.social_links] == [("x", "main"), ("linkedin", None)]
        assert merged.email_entries[0]["label"] == "work"
        assert merged.linkedin_url == "https://www.linkedin.com/in/test-one"

    def test_primary_label_wins_over_duplicate(self):
        primary = PersonEntity(canonical_name="Test One", social_links=[build_social_link("x", "@test_one", "mine")])
        other = PersonEntity(canonical_name="Test Uno", social_links=[build_social_link("x", "@test_one", "theirs")])
        assert primary.merge(other).social_links[0]["label"] == "mine"


@pytest.fixture
def client(store, monkeypatch):
    import api.routes.crm as crm

    monkeypatch.setattr(crm, "get_person_entity_store", lambda: store)
    monkeypatch.setattr(crm, "get_source_entity_store", lambda: type("S", (), {"get_for_person": lambda *a, **k: []})())
    app = FastAPI()
    app.include_router(crm.router)
    return TestClient(app)


class TestContactDetailsApi:
    def test_platform_list_comes_from_the_registry(self, client):
        platforms = client.get("/api/crm/social-platforms").json()
        assert [p["key"] for p in platforms] == [p.key for p in SOCIAL_PLATFORMS]

    def test_add_edit_remove_social_link(self, client, store):
        person = _person(store)
        url = f"/api/crm/people/{person.id}/social-links"

        added = client.post(url, json={"platform": "instagram", "value": "@test_main", "label": "personal"})
        assert added.status_code == 201
        link = added.json()["social_links"][0]
        assert (link["url"], link["label"]) == ("https://www.instagram.com/test_main", "personal")

        relabeled = client.patch(f"{url}/{link['id']}", json={"label": "finsta"}).json()["social_links"][0]
        assert (relabeled["label"], relabeled["id"]) == ("finsta", link["id"])

        cleared = client.patch(f"{url}/{link['id']}", json={"label": ""}).json()["social_links"][0]
        assert cleared["label"] is None

        changed = client.patch(f"{url}/{link['id']}", json={"value": "@test_other"}).json()["social_links"][0]
        assert changed["handle"] == "test_other"
        assert store.get_by_social_link("instagram", "test_main") is None

        assert client.delete(f"{url}/{changed['id']}").json()["social_links"] == []
        assert store.get_by_id(person.id).social_links == []

    def test_linkedin_edits_keep_the_legacy_field(self, client, store):
        person = _person(store)
        url = f"/api/crm/people/{person.id}/social-links"
        data = client.post(url, json={"platform": "linkedin", "value": "https://linkedin.com/in/test-person"}).json()
        assert data["linkedin_url"] == "https://linkedin.com/in/test-person"
        assert client.delete(f"{url}/{data['social_links'][0]['id']}").json()["linkedin_url"] is None

    def test_bad_social_values_return_422(self, client, store):
        person = _person(store)
        url = f"/api/crm/people/{person.id}/social-links"
        assert client.post(url, json={"platform": "nosuch", "value": "x"}).status_code == 422
        assert client.post(url, json={"platform": "x", "value": "https://facebook.com/a"}).status_code == 422
        assert client.post(url, json={"platform": "x", "value": "a", "label": "y" * 65}).status_code == 422

    def test_duplicates_and_another_contacts_account_conflict(self, client, store):
        one = _person(store)
        two = _person(store, "Test Person Two")
        url = f"/api/crm/people/{one.id}/social-links"
        assert client.post(url, json={"platform": "x", "value": "@test_one"}).status_code == 201
        assert client.post(url, json={"platform": "x", "value": "https://x.com/Test_One"}).status_code == 409
        other = f"/api/crm/people/{two.id}/social-links"
        assert client.post(other, json={"platform": "x", "value": "@test_one"}).status_code == 409

    def test_removed_account_can_be_added_back_by_hand(self, client, store):
        person = _person(store)
        url = f"/api/crm/people/{person.id}/social-links"
        link = client.post(url, json={"platform": "x", "value": "@test_one"}).json()["social_links"][0]
        client.delete(f"{url}/{link['id']}")
        assert client.post(url, json={"platform": "x", "value": "@test_one"}).status_code == 201
        assert store.get_by_id(person.id).dismissed_social_keys == []

    def test_email_add_label_edit_remove(self, client, store):
        person = _person(store, emails=["one@example.com"])
        url = f"/api/crm/people/{person.id}/emails"
        entry_id = contact_entry_id("email", "one@example.com")

        labeled = client.patch(f"{url}/{entry_id}", json={"label": "work"}).json()
        assert labeled["labeled_emails"] == [{"id": entry_id, "value": "one@example.com", "label": "work"}]

        added = client.post(url, json={"value": "Two@Example.com", "label": "school"}).json()
        assert added["emails"] == ["one@example.com", "two@example.com"]
        assert store.get_by_email("two@example.com").id == person.id

        edited = client.patch(f"{url}/{entry_id}", json={"value": "new@example.com"}).json()
        assert edited["labeled_emails"][0] == {
            "id": contact_entry_id("email", "new@example.com"), "value": "new@example.com", "label": "work",
        }
        assert store.get_by_email("one@example.com") is None

        new_id = edited["labeled_emails"][0]["id"]
        assert client.delete(f"{url}/{new_id}").json()["emails"] == ["two@example.com"]

    def test_phone_edit_keeps_primary_and_label(self, client, store):
        person = _person(store, phone_numbers=["+15550100001"], phone_primary="+15550100001")
        url = f"/api/crm/people/{person.id}/phones"
        entry_id = contact_entry_id("phone", "+15550100001")

        data = client.patch(f"{url}/{entry_id}", json={"value": "(555) 010-0002", "label": "mobile"}).json()
        assert data["phone_primary"] == "+15550100002"
        assert data["labeled_phones"][0]["label"] == "mobile"

        removed = client.delete(f"{url}/{data['labeled_phones'][0]['id']}").json()
        assert (removed["phone_numbers"], removed["phone_primary"]) == ([], None)

    def test_contact_conflicts_and_validation(self, client, store):
        _person(store, "Test Person Two", emails=["taken@example.com"], phone_numbers=["+15550100009"])
        person = _person(store)
        assert client.post(f"/api/crm/people/{person.id}/emails", json={"value": "taken@example.com"}).status_code == 409
        assert client.post(f"/api/crm/people/{person.id}/phones", json={"value": "+15550100009"}).status_code == 409
        assert client.post(f"/api/crm/people/{person.id}/emails", json={"value": "nope"}).status_code == 422
        assert client.post(f"/api/crm/people/{person.id}/phones", json={"value": "nope"}).status_code == 422

    def test_person_detail_includes_links_and_labels(self, client, store):
        person = _person(store, emails=["one@example.com"])
        client.patch(
            f"/api/crm/people/{person.id}/emails/{contact_entry_id('email', 'one@example.com')}",
            json={"label": "work"},
        )
        client.post(f"/api/crm/people/{person.id}/social-links", json={"platform": "discord", "value": "test_user"})
        from api.routes.crm import _person_to_detail_response

        detail = _person_to_detail_response(store.get_by_id(person.id), include_related=False)
        assert detail.labeled_emails[0].label == "work"
        assert (detail.social_links[0].platform_label, detail.social_links[0].url) == ("Discord", None)

    def test_missing_person_and_entry_return_404(self, client, store):
        person = _person(store)
        assert client.post("/api/crm/people/missing/emails", json={"value": "a@example.com"}).status_code == 404
        assert client.delete(f"/api/crm/people/{person.id}/social-links/missing").status_code == 404
        assert client.delete(f"/api/crm/people/{person.id}/phones/missing").status_code == 404


def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


class TestExportColumns:
    def test_notion_clay_columns(self):
        columns = map_columns(["Name", "Primary Email", "Secondary Email", "Primary Phone",
                               "Linkedin", "Facebook", "Twitter", "Last Contacted", "Contact URL", "Website"])
        assert [(c.header, c.kind, c.label) for c in columns.values] == [
            ("Linkedin", "linkedin", None), ("Facebook", "facebook", None), ("Twitter", "x", None),
            ("Primary Email", "email", None), ("Secondary Email", "email", None), ("Primary Phone", "phone", None),
        ]

    def test_notion_template_twitter_x_column(self):
        assert map_columns(["Twitter/X"]).values[0].kind == "x"

    def test_labels_in_headers(self):
        columns = map_columns(["Work Email", "Email (School)", "Mobile", "Home Phone", "Instagram (finsta)", "Instagram URL"])
        assert {(c.header, c.label) for c in columns.values} == {
            ("Work Email", "work"), ("Email (School)", "school"), ("Mobile", "mobile"),
            ("Home Phone", "home"), ("Instagram (finsta)", "finsta"), ("Instagram URL", None),
        }

    def test_paired_label_columns(self):
        headers = ["E-mail 1 - Type", "E-mail 1 - Value", "Phone 1 - Label", "Phone 1 - Value", "Phone 2 - Value"]
        columns = map_columns(headers)
        row = dict(zip(headers, ["* Work", "one@example.com", "Mobile", "+15550100001", "+15550100002"]))
        assert {(v.value, v.label) for v in read_row_values(row, columns)} == {
            ("one@example.com", "Work"), ("+15550100001", "Mobile"), ("+15550100002", None),
        }

    def test_labels_and_multiple_values_inside_a_cell(self):
        headers = ["Emails", "Phones", "Instagram"]
        columns = map_columns(headers)
        row = dict(zip(headers, [
            "work: one@example.com, two@example.com (school)", "+15550100001 ::: +15550100002",
            "finsta: https://instagram.com/test_finsta",
        ]))
        assert {(v.kind, v.value, v.label) for v in read_row_values(row, columns)} == {
            ("email", "one@example.com", "work"), ("email", "two@example.com", "school"),
            ("phone", "+15550100001", None), ("phone", "+15550100002", None),
            ("instagram", "https://instagram.com/test_finsta", "finsta"),
        }

    def test_metadata_columns_are_ignored(self):
        assert map_columns(["Email Domain", "Last Phone Call", "Phone Calls", "Email Opt Out"]).values == []


class TestExportImport:
    HEADERS = ["Full Name", "Work Email", "Phone", "LinkedIn", "Facebook", "Instagram (finsta)"]

    def _run(self, store, tmp_path, rows, execute=True, headers=None):
        path = _write_csv(tmp_path / "export.csv", headers or self.HEADERS, rows)
        return import_contact_export(path, "notion", store, execute=execute)

    def test_enriches_a_person_matched_by_email(self, store, tmp_path):
        person = _person(store, emails=["one@example.com"])
        stats = self._run(store, tmp_path, [[
            "Different Name", "one@example.com", "+15550100001",
            "https://linkedin.com/in/test-one", "https://www.facebook.com/test.one", "test_finsta",
        ]])
        saved = store.get_by_id(person.id)
        assert stats["matched_by_email"] == 1
        assert saved.email_entries[0]["label"] == "work"
        assert saved.phone_numbers == ["+15550100001"]
        assert saved.linkedin_url == "https://linkedin.com/in/test-one"
        assert {(item["platform"], item["label"], item["source"]) for item in saved.social_links} == {
            ("linkedin", None, "notion"), ("facebook", None, "notion"), ("instagram", "finsta", "notion"),
        }

    def test_matches_by_facebook_url(self, store, tmp_path):
        person = _person(store)
        person.add_social_link(build_social_link("facebook", "test.one"))
        store.update(person)
        stats = self._run(store, tmp_path, [["Other Name", "", "+15550100001", "", "https://facebook.com/test.one", ""]])
        assert stats["matched_by_social"] == 1
        assert store.get_by_id(person.id).phone_numbers == ["+15550100001"]

    def test_exact_full_name_is_the_last_resort(self, store, tmp_path):
        person = _person(store, "Test Person One")
        stats = self._run(store, tmp_path, [["Test Person One", "", "", "test-one", "", ""]])
        assert stats["matched_by_name"] == 1
        assert store.get_by_id(person.id).social_links[0]["match_key"] == "test-one"

    def test_single_word_name_never_matches(self, store, tmp_path):
        _person(store, "Mononym")
        stats = self._run(store, tmp_path, [["Mononym", "", "", "test-one", "", ""]])
        assert stats["rows_unmatched"] == 1

    def test_rows_pointing_at_two_people_are_left_alone(self, store, tmp_path):
        one = _person(store, "Test Person One", emails=["one@example.com"])
        _person(store, "Test Person Two", phone_numbers=["+15550100002"])
        stats = self._run(store, tmp_path, [["X Y", "one@example.com", "+15550100002", "test-new", "", ""]])
        assert stats["rows_conflicting"] == 1
        assert store.get_by_id(one.id).social_links == []

    def test_unmatched_rows_do_not_create_people(self, store, tmp_path):
        stats = self._run(store, tmp_path, [["Nobody Known", "new@example.com", "", "", "", ""]])
        assert stats["rows_unmatched"] == 1
        assert store.get_all() == []

    def test_dry_run_writes_nothing(self, store, tmp_path):
        person = _person(store, emails=["one@example.com"])
        stats = self._run(store, tmp_path, [["X Y", "one@example.com", "", "test-one", "", ""]], execute=False)
        assert stats["people_updated"] == 1
        assert store.get_by_id(person.id).social_links == []

    def test_rerun_changes_nothing(self, store, tmp_path):
        _person(store, emails=["one@example.com"])
        row = [["X Y", "one@example.com", "+15550100001", "test-one", "", "test_finsta"]]
        self._run(store, tmp_path, row)
        assert "people_updated" not in self._run(store, tmp_path, row)

    def test_user_labels_and_removals_survive_import(self, store, tmp_path):
        person = _person(store, emails=["one@example.com"])
        person.label_contact_entry("email", "one@example.com", "mine")
        removed = build_social_link("linkedin", "test-removed")
        person.add_social_link(removed)
        person.remove_social_link(social_link_id(removed))
        store.update(person)
        self._run(store, tmp_path, [["X Y", "one@example.com", "", "test-removed", "", ""]])
        saved = store.get_by_id(person.id)
        assert saved.email_entries[0]["label"] == "mine"
        assert saved.social_links == []

    def test_invalid_values_are_counted_not_stored(self, store, tmp_path):
        person = _person(store, emails=["one@example.com"])
        stats = self._run(store, tmp_path, [["X Y", "one@example.com", "", "https://example.com/nope", "", ""]])
        assert stats["invalid_values"] == 1
        assert store.get_by_id(person.id).social_links == []

    def test_blocked_addresses_are_not_added(self, store, tmp_path):
        hidden = _person(store, "Test Hidden Person", emails=["blocked@example.com"])
        store.hide_person(hidden.id)
        person = _person(store, linkedin_url="https://linkedin.com/in/test-one")
        stats = self._run(store, tmp_path, [["X Y", "blocked@example.com", "", "test-one", "", ""]])
        assert stats["matched_by_social"] == 1
        assert stats["blocked_values"] == 1
        assert not store.get_by_id(person.id).has_email("blocked@example.com")


class TestMergeScriptAndUndo:
    def test_merge_then_undo_moves_accounts_both_ways(self, tmp_path):
        from unittest.mock import patch

        from scripts.merge_people import merge_people
        from scripts.undo_merge import undo_merge
        from tests.test_merge_crash_safety import _create_crm_db, _create_interactions_db

        crm_path = str(tmp_path / "crm.db")
        int_path = str(tmp_path / "interactions.db")
        _create_crm_db(crm_path)
        store = PersonEntityStore(crm_path)
        _create_interactions_db(int_path)
        merged_ids = tmp_path / "merged_person_ids.json"
        merged_ids.write_text("{}")
        primary = _person(store, "Test Keeper", emails=["keeper@example.com"])
        primary.label_contact_entry("email", "keeper@example.com", "work")
        store.update(primary)
        secondary = _person(store, "Test Duplicate", linkedin_url="https://linkedin.com/in/test-duplicate")

        with patch("scripts.merge_people.MERGE_LOG_FILE", tmp_path / "merge_log.json"), \
             patch("scripts.merge_people.MERGED_IDS_FILE", merged_ids), \
             patch("scripts.merge_people.get_person_entity_store", return_value=store), \
             patch("scripts.merge_people.get_crm_db_path", return_value=crm_path), \
             patch("scripts.merge_people.get_interaction_db_path", return_value=int_path), \
             patch("api.services.person_stats.refresh_person_stats"), \
             patch("api.services.relationship_metrics.update_strength_for_person", return_value=0.5):
            merge_people(primary.id, secondary.id, dry_run=False)

        store._load_merged_ids()
        merged = store.get_by_id(primary.id)
        assert merged.linkedin_url == "https://linkedin.com/in/test-duplicate"
        assert merged.email_entries[0]["label"] == "work"
        assert store.get_by_social_link("linkedin", "test-duplicate").id == primary.id

        with patch("scripts.undo_merge.get_crm_db_path", return_value=crm_path), \
             patch("scripts.undo_merge.get_interaction_db_path", return_value=int_path), \
             patch("scripts.merge_people.MERGED_IDS_FILE", merged_ids), \
             patch("api.services.person_stats.refresh_person_stats"):
            undo_merge(secondary.id, dry_run=False)

        store._load_merged_ids()
        assert store.get_by_id(primary.id).social_links == []
        assert not store.get_by_id(primary.id).linkedin_url
        assert store.get_by_social_link("linkedin", "test-duplicate").id == secondary.id
