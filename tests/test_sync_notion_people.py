"""Tests for scripts/sync_notion_people.py (LifeOS -> Notion People sync).

No network and no real token: the Notion client is either a fake or a real
NotionClient on an httpx.MockTransport.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from scripts import sync_notion_people as sync

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
TOKEN = "secret_FAKE_TOKEN_0123456789"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Builders and fakes
# ---------------------------------------------------------------------------

def _rich(text):
    return {"rich_text": [{"plain_text": text}] if text else []}


def page(pid, name="Ada Lovelace", dex="", email=None, phone=None, linkedin=None, checkin=None,
         lifeos_id="", company="", title="", instagram=None, twitter=None, website=None):
    return {
        "id": pid,
        "properties": {
            "Full Name": {"title": [{"plain_text": name}] if name else []},
            "Dex_ID": _rich(dex),
            "LifeOS_ID": _rich(lifeos_id),
            "Email": {"email": email},
            "Phone": {"phone_number": phone},
            "Company": _rich(company),
            "Title": _rich(title),
            "LinkedIn": {"url": linkedin},
            "Instagram": {"url": instagram},
            "Twitter/X": {"url": twitter},
            "Website": {"url": website},
            "Last Check-In": {"date": {"start": checkin} if checkin else None},
        },
    }


def person(pid, name="Ada Lovelace", emails=(), phones=(), linkedin=None, last_seen=None,
           company=None, position=None, social=(), **extra):
    data = {
        "id": pid,
        "display_name": name,
        "canonical_name": name.lower(),
        "emails": list(emails),
        "phone_numbers": list(phones),
        "linkedin_url": linkedin,
        "last_seen": last_seen,
        "company": company,
        "position": position,
        "social_links": list(social),
        "category": "personal",
        "relationship_strength": 0,
    }
    data.update(extra)
    return data


def link(platform, url):
    return {"platform": platform, "url": url, "handle": "h", "value": url}


class FakeClient:
    """Stands in for NotionClient; applies writes to its pages like Notion."""

    schema_error = None
    create_error = None
    template_appears = True

    def __init__(self, pages, available=None):
        self.pages = pages
        self.available = available
        self.updates = []
        self.creates = []
        self.waits = []
        self.order = []

    def close(self):
        pass

    def check_schema(self):
        if self.schema_error:
            raise sync.NotionError(self.schema_error)
        return self.available

    def query_rows(self, limit=None):
        return self.pages[:limit] if limit else list(self.pages)

    def _apply(self, target, properties):
        for key, value in properties.items():
            if "rich_text" in value:
                texts = [t["text"]["content"] for t in value["rich_text"]]
                target["properties"][key] = {"rich_text": [{"plain_text": t} for t in texts]}
            else:
                target["properties"][key] = value

    def update_page(self, page_id, properties):
        self.order.append(("update", page_id))
        self.updates.append((page_id, properties))
        self._apply(next(p for p in self.pages if p["id"] == page_id), properties)
        return {}

    def create_page(self, name, lifeos_id, template_id="", timezone_name=None):
        self.order.append(("create", name))
        if self.create_error:
            raise self.create_error
        self.creates.append((name, lifeos_id, template_id, timezone_name))
        new = page(f"new-{len(self.creates)}", name=name, lifeos_id=lifeos_id)
        self.pages.append(new)
        return {"id": new["id"]}

    def wait_for_template(self, page_id):
        self.order.append(("wait", page_id))
        self.waits.append(page_id)
        return self.template_appears


@pytest.fixture(autouse=True)
def _reset_logger():
    yield
    for handler in list(sync.logger.handlers):
        handler.close()
        sync.logger.removeHandler(handler)
    sync.logger.propagate = True


def _stats_line(out):
    line = [l for l in out.splitlines() if l.startswith("SYNC_STATS:")][-1]
    return json.loads(line[len("SYNC_STATS:"):])


def paths(tmp_path):
    return {"map_path": tmp_path / "map.json", "dexmap_path": tmp_path / "dexmap.json",
            "ledger_path": tmp_path / "ledger.jsonl"}


def run(client, people, tmp_path, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("tz", UTC)
    kw.setdefault("today", date(2026, 6, 1))
    kw.setdefault("run_id", "run1")
    return sync.run_sync(client, people, **{**paths(tmp_path), **kw})


def write_dexmap(tmp_path, dexmap):
    (tmp_path / "dexmap.json").write_text(json.dumps({"version": 1, "dex_to_lifeos": dexmap}))


def ledger_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def read(client, pid, prop):
    return next(p for p in client.pages if p["id"] == pid)["properties"][prop]


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

def test_parse_row_every_property_shape():
    row = sync.parse_row(
        {
            "id": "p1",
            "properties": {
                "Full Name": {"title": [{"plain_text": "Ada "}, {"plain_text": "Lovelace"}]},
                "Dex_ID": {"rich_text": [{"plain_text": "dex"}, {"plain_text": "-1"}]},
                "LifeOS_ID": {"rich_text": [{"plain_text": "abc123"}]},
                "Email": {"email": "ada@example.com"},
                "Phone": {"phone_number": "+12125550173"},
                "Company": {"rich_text": [{"plain_text": "Analytical Engines"}]},
                "Title": {"rich_text": [{"plain_text": "Founder"}]},
                "LinkedIn": {"url": "https://www.linkedin.com/in/ada/"},
                "Instagram": {"url": "https://instagram.com/ada"},
                "Twitter/X": {"url": "https://x.com/ada"},
                "Website": {"url": "https://ada.example.com"},
                "Last Check-In": {"date": {"start": "2026-03-01"}},
            },
        }
    )
    assert row == sync.NotionRow(
        "p1", "Ada Lovelace", "dex-1", "abc123", "ada@example.com", "+12125550173",
        "Analytical Engines", "Founder", "https://www.linkedin.com/in/ada/",
        "https://instagram.com/ada", "https://x.com/ada", "https://ada.example.com", "2026-03-01",
    )


def test_parse_row_datetime_checkin_uses_first_ten_chars():
    row = sync.parse_row(page("p", checkin="2026-03-01T10:00:00.000-05:00"))
    assert row.last_checkin == "2026-03-01"


def test_parse_row_empty_and_missing_values():
    row = sync.parse_row(page("p", name="", dex=""))
    assert (row.name, row.dex_id, row.lifeos_id, row.email, row.phone, row.company, row.title,
            row.linkedin, row.instagram, row.twitter, row.website, row.last_checkin) == (
        "", "", "", None, None, "", "", None, None, None, None, None,
    )
    bare = sync.parse_row({"id": "q", "properties": {}})
    assert bare.page_id == "q" and bare.name == "" and bare.email is None


def test_url_normalisation():
    n = sync.normalize_url
    assert n("HTTPS://www.Example.com/Path/?utm=1#x") == "example.com/path"
    assert n("https://x.com/ada") == n("http://www.twitter.com/ada/")
    assert n("") is None and n(None) is None
    assert sync.https_url("http://example.com/a?b=1") == "https://example.com/a"
    assert sync.https_url("example.com") == "https://example.com"


def test_linkedin_normalisation():
    n = sync.normalize_linkedin
    assert n("HTTPS://www.LinkedIn.com/in/Ada-L/?utm=1#x") == "linkedin.com/in/ada-l"
    assert n("linkedin.com/in/ada/") == "linkedin.com/in/ada"
    assert n("https://uk.linkedin.com/in/ada") == "linkedin.com/in/ada"
    assert n("https://linkedin.com/company/acme") is None
    assert n("") is None and n(None) is None


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------

def resolve(row_page, people, stored=None, dexmap=None):
    return sync.resolve_row(
        sync.parse_row(row_page), sync.build_index(people), stored or {}, dexmap or {}
    )


def test_lifeos_id_wins_over_every_other_source_and_needs_no_name_check():
    a, b = person("A", name="Ada Lovelace"), person("B", name="Grace Hopper", emails=["g@example.com"])
    res = resolve(
        page("p1", name="Somebody Else", lifeos_id="A", dex="d1", email="g@example.com"), [a, b],
        stored={"p1": {"lifeos_id": "B"}}, dexmap={"d1": "B"},
    )
    assert res.person["id"] == "A" and res.source == "lifeos_id" and res.matched_by == "lifeos_id"


def test_lifeos_id_without_a_person_is_an_orphan_not_a_fallthrough():
    people = [person("B", emails=["ada@example.com"])]
    res = resolve(page("p1", lifeos_id="GONE", email="ada@example.com"), people,
                  stored={"p1": {"lifeos_id": "B"}})
    assert res.person is None and res.orphan and "orphan" in res.reason


def test_resolve_uses_stored_map_first_when_seed_agrees_or_is_silent():
    a, b = person("A"), person("B")
    for dexmap in ({"d1": "A"}, {}):
        res = resolve(
            page("p1", dex="d1"), [a, b],
            stored={"p1": {"lifeos_id": "A", "matched_by": "identifiers"}},
            dexmap=dexmap,
        )
        assert res.person["id"] == "A" and res.source == "map" and res.matched_by == "identifiers"


def test_resolve_dexmap_when_not_stored():
    res = resolve(page("p1", dex="d1"), [person("A"), person("B")], dexmap={"d1": "B"})
    assert res.person["id"] == "B" and res.matched_by == "dex_map" and res.source == "dexmap"


def test_resolve_stale_stored_mapping_falls_through():
    res = resolve(
        page("p1", dex="d1"), [person("B")],
        stored={"p1": {"lifeos_id": "GONE"}}, dexmap={"d1": "B"},
    )
    assert res.person["id"] == "B" and res.source == "dexmap"


def test_resolve_identifiers_unique_by_each_identifier():
    people = [
        person("A", emails=["ada@example.com"], phones=["+12125550173"],
               linkedin="https://linkedin.com/in/ada"),
        person("B", name="Grace Hopper", emails=["grace@example.com"]),
    ]
    by_email = resolve(page("p", email="ADA@example.com"), people)
    by_phone = resolve(page("p", phone="(212) 555-0173"), people)
    by_link = resolve(page("p", linkedin="http://www.linkedin.com/in/ada/?x=1"), people)
    for res in (by_email, by_phone, by_link):
        assert res.person["id"] == "A"
        assert res.matched_by == "identifiers" and res.source == "identifiers"


def test_resolve_identifiers_use_the_social_links_linkedin_too():
    people = [person("A", social=[link("linkedin", "https://www.linkedin.com/in/ada")])]
    assert resolve(page("p", linkedin="linkedin.com/in/ada"), people).person["id"] == "A"


def test_resolve_identifiers_ambiguous():
    people = [
        person("A", emails=["shared@example.com"]),
        person("B", emails=["shared@example.com"]),
    ]
    res = resolve(page("p", email="shared@example.com"), people)
    assert res.person is None and "ambiguous" in res.reason


def test_resolve_identifiers_disagreeing_people_are_ambiguous():
    people = [person("A", emails=["a@example.com"]), person("B", phones=["+12125550173"])]
    res = resolve(page("p", email="a@example.com", phone="2125550173"), people)
    assert res.person is None and "ambiguous" in res.reason


def test_resolve_no_match_and_no_identifiers():
    people = [person("A", emails=["a@example.com"])]
    none = resolve(page("p", email="zzz@example.com"), people)
    assert none.person is None and "no LifeOS person" in none.reason
    bare = resolve(page("p"), people)
    assert bare.person is None and "no mapping" in bare.reason


def test_resolve_short_phone_is_not_matched():
    people = [person("A", phones=["+12125550173"])]
    assert resolve(page("p", phone="5550173"), people).person is None


def test_resolve_name_mismatch_rejected_for_map_seed_and_identifiers():
    people = [person("A", name="Grace Hopper", emails=["g@example.com"])]
    expected = "name mismatch"
    assert expected in resolve(page("p", name="Ada Lovelace", email="g@example.com"), people).reason
    assert expected in resolve(page("p", name="Ada Lovelace", dex="d"), people, dexmap={"d": "A"}).reason
    assert expected in resolve(
        page("p", name="Ada Lovelace"), people, stored={"p": {"lifeos_id": "A"}}
    ).reason


def test_name_check_requires_first_and_last_token_ignoring_case_and_punctuation():
    ok = sync.names_compatible
    assert ok("ADA  Lovelace", "Ada Lovelace")
    assert ok("Ada Augusta Lovelace", "Ada Lovelace")      # middle names do not matter
    assert ok("Pat O'Brien", "pat obrien")
    assert not ok("Ada Lovelace", "Grace Hopper")
    assert not ok("Jane Smith", "John Smith")              # shared last name only
    assert not ok("Chris Smith", "Chris Johnson")          # shared first name only
    assert not ok("", "Grace Hopper")


def test_name_check_cuts_credentials_and_accepts_canonical_name():
    ok = sync.names_compatible
    assert ok("Haley Hernandez, CFP", "Haley Hernandez")
    assert ok("Haley Hernandez", "Haley Hernandez, CFP")
    assert ok("Ada Lovelace", "Countess A.", "Ada Lovelace")  # either LifeOS name is enough


def test_name_check_parenthesised_maiden_name_matches_either_way():
    ok = sync.names_compatible
    assert ok("Adrienne Szarmack", "Adrienne (Szarmack) Smith")
    assert ok("Adrienne Smith", "Adrienne (Szarmack) Smith")
    assert not ok("Adrienne Jones", "Adrienne (Szarmack) Smith")
    assert not ok("Jane Smith", "John (Jane) Jones")


def test_name_check_parenthesised_nickname_that_is_not_a_surname():
    ok = sync.names_compatible
    assert ok("Robert Lee", "Bob (Robert) Lee")
    assert ok("Bob Lee", "Bob (Robert) Lee")
    assert not ok("Robert Kim", "Bob (Robert) Lee")
    assert not ok("Robert Lee", "Bob Lee")


def test_name_check_parenthesised_canonical_name_and_unparenthesised_unchanged():
    ok = sync.names_compatible
    assert ok("Adrienne Szarmack", "Adrienne Smith", "adrienne (szarmack) smith")
    assert sync.name_candidates("Ada Lovelace") == [["ada", "lovelace"]]
    assert sync.name_candidates("Ada Lovelace, CFP") == [["ada", "lovelace"]]
    assert sync.name_candidates("Adrienne (Szarmack) Smith") == [
        ["adrienne", "smith"], ["adrienne", "szarmack"], ["szarmack", "smith"],
    ]
    assert sync.name_candidates(None) == []


def test_shared_household_phone_does_not_match_a_different_first_name():
    people = [person("J", name="John Smith", phones=["+12125550173"], last_seen="2026-05-20T00:00:00Z")]
    jane = sync.parse_row(page("p1", name="Jane Smith", phone="212-555-0173"))
    plan = sync.build_plan([jane], people, {}, {}, now=NOW, tz=UTC)
    assert plan.matched == 0 and plan.changes == [] and plan.new_mappings == {}
    assert "name mismatch" in plan.unmatched[0][1]


def test_seed_entry_pointing_at_a_different_person_is_rejected():
    people = [person("X", name="Chris Johnson")]
    res = resolve(page("p1", name="Chris Smith", dex="d1"), people, dexmap={"d1": "X"})
    assert res.person is None and "name mismatch" in res.reason


def test_credentials_in_the_notion_name_still_match():
    people = [person("H", name="Haley Hernandez", emails=["h@example.com"])]
    res = resolve(page("p1", name="Haley Hernandez, CFP", email="h@example.com"), people)
    assert res.person["id"] == "H"


def test_stored_mapping_disagreeing_with_seed_map_skips_row(tmp_path, caplog):
    pages = [page("p1", name="Ada Lovelace", dex="d1"), page("p2", name="Grace Hopper", dex="d2")]
    people = [person("A", emails=["a@example.com"]),
              person("A2", name="Ada Lovelace", emails=["a2@example.com"]),
              person("G", name="Grace Hopper", emails=["g@example.com"])]
    write_dexmap(tmp_path, {"d1": "A2", "d2": "G"})
    sync.save_map_atomic(tmp_path / "map.json", {"p1": {"lifeos_id": "A", "matched_by": "dex_map"}})
    client = FakeClient(pages)
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(client, people, tmp_path, execute=True)
    assert {pid for pid, _ in client.updates} == {"p2"}  # p1 is skipped entirely
    assert stats["unmatched"] == 1
    assert "points at LifeOS A but the seed map sends its Dex_ID to A2" in caplog.text


# ---------------------------------------------------------------------------
# Plan: orphans, duplicates, claims
# ---------------------------------------------------------------------------

def plan_for(pages, people, **kw):
    rows = [sync.parse_row(p) for p in pages]
    return sync.build_plan(rows, people, kw.pop("stored", {}), kw.pop("dexmap", {}),
                           now=NOW, tz=UTC, **kw)


def test_orphan_row_is_reported_and_nothing_is_written_for_it():
    people = [person("A", emails=["a@example.com"], company="Acme")]
    plan = plan_for([page("p1", lifeos_id="GONE", email="a@example.com"),
                     page("p2", name="Ada Lovelace", lifeos_id="A")], people)
    assert [r.page_id for r in plan.orphans] == ["p1"]
    assert [c.page_id for c in plan.changes] == ["p2"]
    assert plan.new_mappings.keys() == {"p2"}


def test_duplicate_lifeos_id_reports_both_rows_and_writes_neither():
    people = [person("A", emails=["a@example.com"], message_count=3)]
    plan = plan_for([page("p1", lifeos_id="A"), page("p2", name="Ada B Lovelace", lifeos_id="A")], people)
    assert sorted(r.page_id for r in plan.duplicates) == ["p1", "p2"]
    assert plan.changes == [] and plan.matched == 0
    assert plan.creates == []  # the id exists on a row, so no second page is made


def test_two_rows_resolving_to_one_person_only_the_first_links():
    people = [person("A", emails=["a@example.com"])]
    plan = plan_for([page("p1", name="Ada Lovelace", email="a@example.com"),
                     page("p2", name="Ada Lovelace", email="A@example.com")], people)
    assert plan.matched == 1
    assert "already linked" in plan.unmatched[0][1]


def test_row_with_lifeos_id_claims_the_person_before_identifier_rows():
    people = [person("A", emails=["a@example.com"])]
    plan = plan_for([page("p1", name="Ada Lovelace", email="a@example.com"),
                     page("p2", name="Zed Other", lifeos_id="A")], people)
    assert [c.page_id for c in plan.changes if c.id_value] == []  # p2 has an id, p1 is skipped
    assert plan.matched == 1 and plan.unmatched[0][0].page_id == "p1"


# ---------------------------------------------------------------------------
# Change rules
# ---------------------------------------------------------------------------

def changes_for(row_page, p, **kw):
    return sync.compute_changes(sync.parse_row(row_page), p, now=NOW, tz=UTC, **kw)


def props_of(change):
    return {} if change is None else change.properties()


def test_id_write_only_when_the_cell_is_empty():
    p = person("A")
    change, _ = changes_for(page("p1"), p)
    assert change.id_value == "A" and change.fields == []
    assert change.properties() == {"LifeOS_ID": {"rich_text": [{"text": {"content": "A"}}]}}
    assert changes_for(page("p1", lifeos_id="A"), p)[0] is None


def test_email_set_when_empty_and_replaced_when_unknown():
    p = person("A", emails=["first@example.com", "second@example.com"])
    empty, _ = changes_for(page("p1", lifeos_id="A"), p)
    wrong, _ = changes_for(page("p1", lifeos_id="A", email="stale@example.com"), p)
    for change in (empty, wrong):
        assert change.properties() == {"Email": {"email": "first@example.com"}}
    assert wrong.fields[0].old == "stale@example.com"


def test_email_known_to_lifeos_is_left_alone_even_if_not_first():
    p = person("A", emails=["first@example.com", "second@example.com"])
    assert changes_for(page("p1", lifeos_id="A", email="Second@Example.com"), p)[0] is None


def test_phone_set_replaced_and_no_churn_on_last_ten_digits():
    p = person("A", phones=["+12125550173", "+13105550111"])
    assert props_of(changes_for(page("p1", lifeos_id="A"), p)[0]) == {
        "Phone": {"phone_number": "+12125550173"}}
    assert props_of(changes_for(page("p1", lifeos_id="A", phone="+1 650 555 0000"), p)[0]) == {
        "Phone": {"phone_number": "+12125550173"}}
    assert changes_for(page("p1", lifeos_id="A", phone="(310) 555-0111"), p)[0] is None
    assert changes_for(page("p1", lifeos_id="A", phone="0012125550173"), p)[0] is None


def test_lifeos_without_a_value_never_blanks_notion():
    row = page("p1", lifeos_id="A", email="x@example.com", phone="+12125550173", company="Acme",
               title="CEO", linkedin="https://linkedin.com/in/x", instagram="https://instagram.com/x",
               checkin="2026-05-01")
    assert changes_for(row, person("A"))[0] is None


def test_company_and_title_overwrite_after_trim_and_casefold():
    p = person("A", company=" Acme Corp ", position="Chief Engineer")
    change, _ = changes_for(page("p1", lifeos_id="A", company="Old Co", title="Engineer"), p)
    assert change.properties() == {
        "Company": {"rich_text": [{"text": {"content": "Acme Corp"}}]},
        "Title": {"rich_text": [{"text": {"content": "Chief Engineer"}}]},
    }
    assert changes_for(page("p1", lifeos_id="A", company="acme corp", title="CHIEF ENGINEER "), p)[0] is None
    filled, _ = changes_for(page("p1", lifeos_id="A"), p)
    assert set(filled.properties()) == {"Company", "Title"}


def test_linkedin_overwrites_with_https_and_does_not_churn_on_equivalent_urls():
    p = person("A", linkedin="http://www.linkedin.com/in/ada/?trk=x")
    change, _ = changes_for(page("p1", lifeos_id="A", linkedin="https://linkedin.com/in/someone-else"), p)
    assert change.properties() == {"LinkedIn": {"url": "https://www.linkedin.com/in/ada"}}
    assert changes_for(page("p1", lifeos_id="A", linkedin="https://linkedin.com/in/ada"), p)[0] is None


def test_linkedin_falls_back_to_the_social_links_entry():
    p = person("A", social=[link("linkedin", "https://www.linkedin.com/in/ada")])
    assert props_of(changes_for(page("p1", lifeos_id="A"), p)[0]) == {
        "LinkedIn": {"url": "https://www.linkedin.com/in/ada"}}


def test_social_platforms_map_to_their_properties():
    p = person("A", social=[
        link("instagram", "https://www.instagram.com/ada"),
        link("twitter", "https://twitter.com/ada"),
        link("facebook", "https://facebook.com/ada"),
        link("website", "http://ada.example.com/"),
    ])
    props = props_of(changes_for(page("p1", lifeos_id="A"), p)[0])
    assert props == {
        "Instagram": {"url": "https://www.instagram.com/ada"},
        "Twitter/X": {"url": "https://twitter.com/ada"},
        "Website": {"url": "https://ada.example.com"},
    }
    x = person("A", social=[link("x", "https://x.com/ada")])
    assert props_of(changes_for(page("p1", lifeos_id="A"), x)[0]) == {"Twitter/X": {"url": "https://x.com/ada"}}
    # the same account under the other host is not a change
    assert changes_for(page("p1", lifeos_id="A", twitter="https://twitter.com/ada"), x)[0] is None
    for name in ("web", "personal", "personal_website"):
        site = person("A", social=[link(name, "https://ada.example.com")])
        assert set(props_of(changes_for(page("p1", lifeos_id="A"), site)[0])) == {"Website"}


def test_missing_optional_schema_properties_are_skipped():
    p = person("A", social=[link("instagram", "https://instagram.com/ada")],
               linkedin="https://linkedin.com/in/ada", company="Acme")
    change, _ = changes_for(page("p1", lifeos_id="A"), p, available={"Instagram"})
    assert set(change.properties()) == {"Instagram", "Company"}


def test_checkin_set_when_empty_or_older():
    p = person("A", last_seen="2026-05-20T15:00:00+00:00")
    empty, _ = changes_for(page("p1", lifeos_id="A"), p)
    older, _ = changes_for(page("p1", lifeos_id="A", checkin="2026-05-01"), p)
    for change in (empty, older):
        assert change.properties() == {"Last Check-In": {"date": {"start": "2026-05-20"}}}


def test_checkin_never_moves_backwards_and_skips_the_future():
    p = person("A", last_seen="2026-05-20T15:00:00+00:00")
    assert changes_for(page("p1", lifeos_id="A", checkin="2026-05-20"), p)[0] is None
    assert changes_for(page("p1", lifeos_id="A", checkin="2026-05-21T09:00:00.000+00:00"), p)[0] is None
    future = person("A", last_seen="2026-07-01T00:00:00+00:00")
    assert changes_for(page("p1", lifeos_id="A"), future)[0] is None


def test_checkin_converts_to_the_local_date():
    p = person("A", last_seen="2026-03-02T03:00:00Z")
    west = timezone(timedelta(hours=-8))
    east = timezone(timedelta(hours=9))
    row = sync.parse_row(page("p1", lifeos_id="A"))
    assert sync.compute_changes(row, p, now=NOW, tz=west)[0].properties()["Last Check-In"] == {
        "date": {"start": "2026-03-01"}}
    assert sync.compute_changes(row, p, now=NOW, tz=east)[0].properties()["Last Check-In"] == {
        "date": {"start": "2026-03-02"}}


def test_checkin_naive_timestamp_is_read_as_utc_and_garbage_is_ignored():
    west = timezone(timedelta(hours=-8))
    assert sync.last_seen_local_date("2026-03-02T03:00:00", now=NOW, tz=west) == date(2026, 3, 1)
    assert sync.last_seen_local_date("not a date", now=NOW, tz=UTC) is None
    assert sync.last_seen_local_date(None, now=NOW, tz=UTC) is None


def test_unreadable_notion_checkin_is_reported_and_left_alone():
    p = person("A", last_seen="2026-05-20T15:00:00+00:00")
    change, conflicts = changes_for(page("p1", lifeos_id="A", checkin="soon"), p)
    assert change is None and conflicts and conflicts[0].startswith("Last Check-In")


def test_one_patch_per_page_holds_every_changed_property():
    p = person("A", emails=["a@example.com"], phones=["+12125550173"], company="Acme",
               last_seen="2026-05-20T00:00:00Z")
    change, _ = changes_for(page("p1"), p)
    assert set(change.properties()) == {"LifeOS_ID", "Email", "Phone", "Company", "Last Check-In"}
    assert set(change.properties(with_id=False)) == {"Email", "Phone", "Company", "Last Check-In"}
    assert set(change.properties(with_fields=False)) == {"LifeOS_ID"}


def test_only_the_synced_properties_are_ever_written():
    p = person("A", emails=["a@example.com"], birthday="1990-01-01", tags=["x"],
               notes="n", relationships=[{"x": 1}])
    change, _ = changes_for(page("p1"), p)
    assert set(change.properties()) <= {"LifeOS_ID", "Email", "Phone", "Company", "Title", "LinkedIn",
                                        "Instagram", "Twitter/X", "Website", "Last Check-In"}


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

def mk_change(name, fields=1, id_value=None):
    return sync.Change(f"id-{name}", name, f"L-{name}",
                       [sync.FieldChange("Email", None, "x", {"email": "x"})] * fields, id_value)


def test_apply_caps_orders_by_name_and_counts_each_kind_separately():
    changes = [mk_change("b", 1, "Lb"), mk_change("A", 1, "LA"), mk_change("c", 0, "Lc"),
               mk_change("d", 1, None)]
    applied, deferred_updates, deferred_ids = sync.apply_caps(changes, max_writes=2, max_id_writes=3)
    assert [(a.change.name, a.with_fields, a.with_id) for a in applied] == [
        ("A", True, True), ("b", True, True), ("c", False, True)]
    assert deferred_updates == 1 and deferred_ids == 0


def test_update_cap_does_not_use_up_the_id_cap_and_vice_versa():
    changes = [mk_change("a", 1, "La"), mk_change("b", 1, "Lb"), mk_change("c", 1, "Lc")]
    applied, du, di = sync.apply_caps(changes, max_writes=1, max_id_writes=3)
    assert [(a.with_fields, a.with_id) for a in applied] == [(True, True), (False, True), (False, True)]
    assert du == 2 and di == 0
    applied, du, di = sync.apply_caps(changes, max_writes=3, max_id_writes=1)
    assert [(a.with_fields, a.with_id) for a in applied] == [(True, True), (True, False), (True, False)]
    assert du == 0 and di == 2
    applied, du, di = sync.apply_caps(changes, max_writes=0, max_id_writes=0)
    assert applied == [] and du == 3 and di == 3


def many(n, with_company=True):
    names = ["Zed Zulu", "Amy Able", "Bob Baker", "Cy Cole", "Di Dunn", "Ed Eng"]
    pages, people = [], []
    for i in range(n):
        name = names[i]
        pages.append(page(f"p{i}", name=name, dex=f"d{i}"))
        people.append(person(f"L{i}", name=name, emails=[f"u{i}@example.com"]))
    dexmap = {f"d{i}": f"L{i}" for i in range(n)}
    return pages, people, dexmap


def test_run_caps_field_updates_and_id_writes_separately(tmp_path):
    pages, people, dexmap = many(5)
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    stats = run(client, people, tmp_path, execute=True, max_writes=2, max_id_writes=4)
    assert stats["changes"] == 5 and stats["writes"] == 2 and stats["deferred"] == 3
    assert stats["id_writes"] == 4 and stats["deferred_id_writes"] == 1
    # name order: Amy Able (p1), Bob Baker (p2), Cy Cole (p3), Di Dunn (p4), Zed Zulu (p0)
    # Zed is over both caps; the id write is always a separate request from the fields
    assert [(pid, set(props)) for pid, props in client.updates] == [
        ("p1", {"LifeOS_ID"}), ("p1", {"Email"}), ("p2", {"LifeOS_ID"}), ("p2", {"Email"}),
        ("p3", {"LifeOS_ID"}), ("p4", {"LifeOS_ID"}),
    ]


# ---------------------------------------------------------------------------
# Run: abort, dry run, execute, ledger, idempotence
# ---------------------------------------------------------------------------

def test_abort_when_under_half_match_writes_nothing(tmp_path):
    pages, people, dexmap = many(3)
    write_dexmap(tmp_path, {"d0": "L0"})
    client = FakeClient(pages)
    with pytest.raises(sync.MappingAbort):
        run(client, people, tmp_path, execute=True)
    assert client.updates == [] and client.creates == []
    assert not (tmp_path / "map.json").exists()


def test_orphans_and_duplicates_count_as_unmatched_for_the_abort(tmp_path):
    pages = [page("p1", lifeos_id="GONE"), page("p2", name="B C", lifeos_id="GONE2"),
             page("p3", name="Ada Lovelace", lifeos_id="A")]
    with pytest.raises(sync.MappingAbort):
        run(FakeClient(pages), [person("A")], tmp_path)


def test_exactly_half_matched_does_not_abort(tmp_path):
    pages, people, _ = many(2)
    write_dexmap(tmp_path, {"d0": "L0"})
    stats = run(FakeClient(pages), people, tmp_path, execute=False)
    assert stats["matched"] == 1 and stats["unmatched"] == 1


def test_empty_database_is_not_an_abort_and_creates_nothing(tmp_path, caplog):
    people = [person("A", message_count=2)]
    client = FakeClient([])
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(client, people, tmp_path, execute=True)
    assert stats["rows"] == 0 and client.creates == [] and "refusing to create" in caplog.text


def test_dry_run_sends_no_writes_and_writes_no_files(tmp_path):
    pages, people, dexmap = many(3)
    people.append(person("N", name="New Person", message_count=2))
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    stats = run(client, people, tmp_path, execute=False, template_id="tpl")
    assert client.updates == [] and client.creates == [] and client.waits == []
    assert not (tmp_path / "map.json").exists() and not (tmp_path / "ledger.jsonl").exists()
    assert stats["mode"] == "dry-run" and stats["changes"] == 3 and stats["writes"] == 0
    assert stats["planned_id_writes"] == 3 and stats["planned_creates"] == 1
    assert stats["id_writes"] == 0 and stats["creates"] == 0 and stats["ledger_path"] is None


def test_dry_run_output_lists_changes_ids_creates_orphans(tmp_path, caplog):
    pages = [page("p1", lifeos_id="A", email="old@example.com"), page("p2", name="Gone Person", lifeos_id="X")]
    people = [person("A", emails=["new@example.com"]),
              person("N", name="New Person", message_count=2)]
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        run(FakeClient(pages), people, tmp_path)
    text = caplog.text
    assert "WOULD UPDATE Ada Lovelace (p1): Email: old@example.com -> new@example.com" in text
    assert "Planned LifeOS_ID writes: 0" in text
    assert "Planned new pages: 1: New Person" in text
    assert "UNMATCHED Gone Person (p2): orphan" in text


def test_long_values_are_truncated_in_the_log(tmp_path, caplog):
    pages = [page("p1", lifeos_id="A")]
    people = [person("A", company="C" * 200)]
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        run(FakeClient(pages), people, tmp_path)
    assert "C" * 200 not in caplog.text and "…" in caplog.text


def test_execute_sends_expected_payload_and_stores_new_mappings(tmp_path):
    pages = [page("p1", dex="d1")]
    people = [person("L1", emails=["ada@example.com"], phones=["+12125550173"], company="Acme",
                     position="Founder", last_seen="2026-05-20T15:00:00Z",
                     social=[link("instagram", "https://instagram.com/ada")])]
    write_dexmap(tmp_path, {"d1": "L1"})
    client = FakeClient(pages)
    stats = run(client, people, tmp_path, execute=True)
    assert client.updates == [
        ("p1", {"LifeOS_ID": {"rich_text": [{"text": {"content": "L1"}}]}}),
        ("p1", {
            "Email": {"email": "ada@example.com"},
            "Phone": {"phone_number": "+12125550173"},
            "Company": {"rich_text": [{"text": {"content": "Acme"}}]},
            "Title": {"rich_text": [{"text": {"content": "Founder"}}]},
            "Instagram": {"url": "https://instagram.com/ada"},
            "Last Check-In": {"date": {"start": "2026-05-20"}},
        })
    ]
    assert stats["writes"] == 1 and stats["id_writes"] == 1 and stats["new_mappings"] == 1
    stored = json.loads((tmp_path / "map.json").read_text())
    assert stored == {
        "version": 1,
        "pages": {"p1": {"lifeos_id": "L1", "dex_id": "d1", "matched_by": "dex_map",
                         "added": "2026-06-01"}},
    }
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_ledger_records_every_change_with_old_and_new_values(tmp_path):
    pages = [page("p1", lifeos_id="L1", email="old@example.com", company="Old Co"),
             page("p2", name="Grace Hopper", dex="d2")]
    people = [person("L1", emails=["new@example.com"], company="Acme"),
              person("L2", name="Grace Hopper")]
    write_dexmap(tmp_path, {"d2": "L2"})
    stats = run(FakeClient(pages), people, tmp_path, execute=True)
    lines = ledger_lines(tmp_path)
    assert stats["ledger_path"] == str(tmp_path / "ledger.jsonl")
    assert all(l["run_id"] == "run1" and l["ts"] for l in lines)
    results = [l for l in lines if l["phase"] == "result"]
    lines = [l for l in lines if l["phase"] == "intent"]
    assert len(results) == 3 and all(l["status"] == "ok" for l in results)
    by_field = {(l["page_id"], l["field"]): l for l in lines}
    assert by_field[("p1", "Email")] == {**by_field[("p1", "Email")], "action": "update",
                                         "lifeos_id": "L1", "old": "old@example.com",
                                         "new": "new@example.com"}
    assert by_field[("p1", "Company")]["old"] == "Old Co" and by_field[("p1", "Company")]["new"] == "Acme"
    assert by_field[("p2", "LifeOS_ID")]["action"] == "id_write"
    assert by_field[("p2", "LifeOS_ID")]["old"] is None and by_field[("p2", "LifeOS_ID")]["new"] == "L2"
    assert len(lines) == 3


def test_ledger_is_appended_across_runs(tmp_path):
    for run_id in ("r1", "r2"):
        pages = [page("p1", lifeos_id="L1", email="old@example.com")]
        run(FakeClient(pages), [person("L1", emails=["new@example.com"])], tmp_path,
            execute=True, run_id=run_id)
    assert [l["run_id"] for l in ledger_lines(tmp_path) if l["phase"] == "intent"] == ["r1", "r2"]


def test_failed_patch_is_ledgered_as_failed(tmp_path):
    class Rejecting(FakeClient):
        def update_page(self, page_id, properties):
            raise sync.NotionError("HTTP 400", 400)

    stats = run(Rejecting([page("p1", lifeos_id="L1")]), [person("L1", emails=["a@example.com"])],
                tmp_path, execute=True)
    lines = ledger_lines(tmp_path)
    assert stats["failed"] == 1
    assert {l["status"] for l in lines if l["phase"] == "result"} == {"failed"}
    assert [l["phase"] for l in lines] == ["intent", "result"]


def test_unwritable_ledger_stops_before_any_write(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory")
    client = FakeClient([page("p1", lifeos_id="L1")])
    with pytest.raises(sync.LedgerError):
        sync.run_sync(client, [person("L1", emails=["a@example.com"])], execute=True,
                      map_path=tmp_path / "map.json", dexmap_path=tmp_path / "d.json",
                      ledger_path=blocker / "ledger.jsonl", now=NOW, tz=UTC)
    assert client.updates == []


def test_stored_map_is_used_and_kept_on_later_runs(tmp_path):
    pages = [page("p1")]  # no identifiers at all
    people = [person("L1", emails=["ada@example.com"])]
    sync.save_map_atomic(tmp_path / "map.json", {
        "p1": {"lifeos_id": "L1", "dex_id": "", "matched_by": "dex_map", "added": "2026-01-01"}
    })
    stats = run(FakeClient(pages), people, tmp_path, execute=True)
    assert stats["matched"] == 1 and stats["new_mappings"] == 0
    assert json.loads((tmp_path / "map.json").read_text())["pages"]["p1"]["added"] == "2026-01-01"


def test_second_run_finds_nothing_including_creates(tmp_path):
    pages, people, dexmap = many(3)
    for p in people:
        p["phone_numbers"] = ["+12125550173"]
        p["last_seen"] = "2026-05-20T10:00:00Z"
        p["company"] = "Acme"
        p["social_links"] = [link("instagram", "https://instagram.com/x")]
    people.append(person("N", name="New Person", emails=["n@example.com"], position="Pilot",
                         last_seen="2026-05-02T00:00:00Z", message_count=2))
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    first = run(client, people, tmp_path, execute=True)
    second = run(client, people, tmp_path, execute=True)
    assert first["writes"] == 3 and first["id_writes"] == 3 and first["creates"] == 1
    assert second["changes"] == 0 and second["writes"] == 0 and second["id_writes"] == 0
    assert second["creates"] == 0 and second["planned_creates"] == 0 and second["conflicts"] == 0
    assert len(client.creates) == 1


def test_unmatched_are_counted_and_logged(tmp_path, caplog):
    pages = [page("p1", name="Ada Lovelace", dex="d1"), page("p2", name="Nobody Known")]
    people = [person("L1", emails=["ada@example.com"])]
    write_dexmap(tmp_path, {"d1": "L1"})
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        stats = run(FakeClient(pages), people, tmp_path)
    assert stats["unmatched"] == 1 and stats["orphans"] == 0
    assert "UNMATCHED Nobody Known" in caplog.text


def test_one_failed_update_does_not_stop_the_run(tmp_path):
    pages, people, dexmap = many(3)
    write_dexmap(tmp_path, dexmap)

    class Flaky(FakeClient):
        def update_page(self, page_id, properties):
            if page_id == "p1":
                raise sync.NotionError("HTTP 400", 400)
            return super().update_page(page_id, properties)

    stats = run(Flaky(pages), people, tmp_path, execute=True)
    assert stats["writes"] == 2 and stats["failed"] == 2 and stats["exit_code"] == 1


def test_auth_failure_during_update_aborts(tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)

    class Unauthorized(FakeClient):
        def update_page(self, page_id, properties):
            raise sync.NotionError("HTTP 401", 401)

    with pytest.raises(sync.NotionError):
        run(Unauthorized(pages), people, tmp_path, execute=True)


def test_corrupt_stored_map_blocks_execute_and_is_left_alone(tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    (tmp_path / "map.json").write_text("{not json")
    client = FakeClient(pages)
    with pytest.raises(sync.MapFileError):
        run(client, people, tmp_path, execute=True)
    assert client.updates == []
    assert (tmp_path / "map.json").read_text() == "{not json"


def test_corrupt_stored_map_only_warns_in_dry_run(tmp_path, caplog):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    (tmp_path / "map.json").write_text("[1, 2]")
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(FakeClient(pages), people, tmp_path, execute=False)
    assert stats["changes"] == 2 and "unreadable" in caplog.text


# ---------------------------------------------------------------------------
# New pages
# ---------------------------------------------------------------------------

def test_qualifies_for_page():
    q = sync.qualifies_for_page
    assert not q(person("A", last_seen="2026-01-01T00:00:00Z"))  # a bare last_seen is not enough
    assert q(person("A", message_count=3))
    assert q(person("A", email_count=1))
    assert q(person("A", meeting_count=2))
    assert q(person("A", dunbar_circle=1)) and q(person("A", dunbar_circle=5))
    assert not q(person("A", dunbar_circle=6)) and not q(person("A", dunbar_circle=0))
    assert not q(person("A"))
    assert not q(person("A", message_count=0, email_count=None, meeting_count=0, dunbar_circle=None))
    assert not q(person("A", category="self", last_seen="2026-01-01T00:00:00Z", message_count=9))
    assert not q(person("A", name="ann@example.com", message_count=4))   # a name with "@"
    assert not q(person("A", name="  ", message_count=4))               # an empty name
    assert q(person("A", name="Ru", message_count=4))                   # single-word names are real


def new_people():
    return [
        person("N1", name="Low Strength", last_seen="2026-05-01T00:00:00Z", email_count=1,
               relationship_strength=0.2, emails=["low@example.com"]),
        person("N2", name="High Strength", message_count=4, relationship_strength=0.9,
               company="Acme", position="Pilot"),
        person("N3", name="Mid Strength", dunbar_circle=3, relationship_strength=0.5),
        person("S", name="Me Myself", category="self", message_count=99),
        person("Q", name="Quiet Person"),
    ]


def test_creates_are_ordered_by_relationship_strength_and_skip_unqualified(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    stats = run(client, new_people() + [person("E", name="Somebody Else")], tmp_path, execute=True)
    assert [c[0] for c in client.creates] == ["High Strength", "Mid Strength", "Low Strength"]
    assert stats["creates"] == 3 and stats["planned_creates"] == 3


def test_create_flow_request_order_template_wait_then_properties(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    run(client, [person("E", name="Somebody Else")] + [new_people()[1]], tmp_path, execute=True,
        template_id="tpl-1", timezone_name="America/New_York")
    assert client.creates == [("High Strength", "N2", "tpl-1", "America/New_York")]
    assert client.order[1:] == [("create", "High Strength"), ("wait", "new-1"), ("update", "new-1")]
    new_id, props = client.updates[-1]
    assert new_id == "new-1"
    assert props == {"Company": {"rich_text": [{"text": {"content": "Acme"}}]},
                     "Title": {"rich_text": [{"text": {"content": "Pilot"}}]}}
    assert read(client, "new-1", "LifeOS_ID") == {"rich_text": [{"plain_text": "N2"}]}


def test_created_pages_are_stored_in_the_map_and_the_ledger(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    run(client, [person("E", name="Somebody Else"), new_people()[1]], tmp_path, execute=True,
        template_id="tpl-1")
    stored = json.loads((tmp_path / "map.json").read_text())["pages"]
    assert stored["new-1"]["lifeos_id"] == "N2" and stored["new-1"]["matched_by"] == "created"
    lines = [l for l in ledger_lines(tmp_path) if l["page_id"] == "new-1"]
    assert {l["field"] for l in lines} == {"page", "Company", "Title"}
    assert all(l["action"] == "create" and l["lifeos_id"] == "N2" for l in lines)
    assert next(l for l in lines if l["field"] == "page")["new"] == "High Strength"


def test_template_that_never_appears_is_counted_and_properties_still_written(tmp_path, caplog):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    client.template_appears = False
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(client, [person("E", name="Somebody Else"), new_people()[1]], tmp_path,
                    execute=True, template_id="tpl-1")
    assert stats["template_pending"] == 1 and stats["creates"] == 1
    assert client.updates and client.updates[-1][0] == "new-1"
    assert "has not appeared" in caplog.text


def test_empty_template_setting_creates_without_template_and_without_waiting(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    run(client, [person("E", name="Somebody Else"), new_people()[1]], tmp_path, execute=True,
        template_id="")
    assert client.creates[0][2] == "" and client.waits == []
    assert client.order[1:] == [("create", "High Strength"), ("update", "new-1")]


def test_forbidden_creation_stops_creates_but_updates_still_run(tmp_path, caplog):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    client.create_error = sync.NotionError("HTTP 403 [restricted_resource: block limit]", 403,
                                           "restricted_resource")
    with caplog.at_level(logging.ERROR, logger="notion_people_sync"):
        stats = run(client, people + new_people(), tmp_path, execute=True)
    assert stats["writes"] == 2 and stats["creates"] == 0
    assert [o for o in client.order if o[0] == "create"] == [("create", "High Strength")]  # stopped after the first
    assert stats["creates_stopped"].startswith("403") and "restricted_resource" in caplog.text
    assert stats["status"] == "creates_stopped" and stats["exit_code"] == 1  # the nightly shows it


def test_other_non_retryable_4xx_also_stops_creating(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    client.create_error = sync.NotionError("HTTP 400 [validation_error: bad template]", 400)
    stats = run(client, [person("E", name="Somebody Else")] + new_people(), tmp_path, execute=True)
    assert len([o for o in client.order if o[0] == "create"]) == 1
    assert stats["creates_stopped"].startswith("400")


def test_401_while_creating_aborts(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    client.create_error = sync.NotionError("HTTP 401", 401)
    with pytest.raises(sync.NotionError):
        run(client, [person("E", name="Somebody Else")] + new_people(), tmp_path, execute=True)


def test_creates_cap_and_deferred(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    stats = run(client, [person("E", name="Somebody Else")] + new_people(), tmp_path, execute=True,
                max_creates=2)
    assert [c[0] for c in client.creates] == ["High Strength", "Mid Strength"]
    assert stats["creates"] == 2 and stats["deferred_creates"] == 1 and stats["planned_creates"] == 2


def test_dedupe_person_whose_id_is_already_a_lifeos_id_is_not_created(tmp_path):
    existing = [page("p1", name="High Strength", lifeos_id="N2"),
                page("p2", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    run(client, [person("E", name="Somebody Else")] + new_people(), tmp_path, execute=True)
    assert "High Strength" not in [c[0] for c in client.creates]


def test_dedupe_person_in_the_stored_map_is_not_created(tmp_path):
    existing = [page("p2", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    sync.save_map_atomic(tmp_path / "map.json",
                         {"not-visible-yet": {"lifeos_id": "N2", "matched_by": "created"}})
    client = FakeClient(existing)
    run(client, [person("E", name="Somebody Else")] + new_people(), tmp_path, execute=True)
    assert "High Strength" not in [c[0] for c in client.creates]


def test_name_only_link_writes_nothing_and_blocks_a_duplicate_page(tmp_path, caplog):
    # The row has no identifiers and no mapping; only its name resembles one LifeOS person.
    existing = [page("p1", name="High Strength"), page("p2", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    client = FakeClient(existing)
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        stats = run(client, [person("E", name="Somebody Else")] + new_people(), tmp_path, execute=True)
    assert "High Strength" not in [c[0] for c in client.creates]
    assert not any(pid == "p1" for pid, _ in client.updates)           # no field change, no LifeOS_ID
    assert stats["possible_matches"] == 1 and stats["matched"] == 1 and stats["unmatched"] == 1
    assert "POSSIBLE MATCH, review by hand" in caplog.text
    assert "p1" not in json.loads((tmp_path / "map.json").read_text())["pages"]


def test_rows_sharing_the_name_of_a_person_block_their_creation(tmp_path):
    existing = [page("p1", name="High Strength"), page("p3", name="High Strength"),
                page("p2", name="Somebody Else", dex="d1"), page("p4", name="Other Match", dex="d2")]
    write_dexmap(tmp_path, {"d1": "E", "d2": "O"})
    client = FakeClient(existing)
    run(client, [person("E", name="Somebody Else"), person("O", name="Other Match"), new_people()[1]],
        tmp_path, execute=True)
    assert "High Strength" not in [c[0] for c in client.creates]
    assert not any(pid in ("p1", "p3") for pid, _ in client.updates)


def test_limit_skips_page_creation(tmp_path, caplog):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        stats = run(client, people + new_people(), tmp_path, execute=True, limit=2)
    assert client.creates == [] and stats["planned_creates"] == 0
    assert "page creation skipped" in caplog.text


# ---------------------------------------------------------------------------
# NotionClient over httpx.MockTransport
# ---------------------------------------------------------------------------

def make_client(handler, sleeps=None, **kw):
    sleeps = [] if sleeps is None else sleeps
    kw.setdefault("pace", 0)
    return sync.NotionClient(
        TOKEN, "ds123", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )


def test_query_follows_pagination_on_the_data_source_endpoint():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        if "start_cursor" not in body:
            return httpx.Response(200, json={"results": [{"id": "a"}], "has_more": True,
                                             "next_cursor": "c1"})
        return httpx.Response(200, json={"results": [{"id": "b"}], "has_more": False,
                                         "next_cursor": None})

    rows = make_client(handler).query_rows()
    assert [r["id"] for r in rows] == ["a", "b"]
    assert seen[0] == ("POST", "/v1/data_sources/ds123/query", {"page_size": 100})
    assert seen[1][2] == {"page_size": 100, "start_cursor": "c1"}


def test_query_limit_truncates():
    def handler(request):
        return httpx.Response(200, json={"results": [{"id": str(i)} for i in range(100)],
                                         "has_more": True, "next_cursor": "more"})

    assert len(make_client(handler).query_rows(limit=5)) == 5


def test_requests_carry_auth_and_the_configured_version_header_and_patch_shape():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={})

    make_client(handler).update_page("page-1", {"Email": {"email": "a@example.com"}})
    make_client(handler, version="2026-01-01").update_page("page-1", {})
    req = captured[0]
    assert req.method == "PATCH" and req.url.path == "/v1/pages/page-1"
    assert json.loads(req.content) == {"properties": {"Email": {"email": "a@example.com"}}}
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.headers["Notion-Version"] == "2025-09-03"
    assert captured[1].headers["Notion-Version"] == "2026-01-01"


def test_create_page_request_body_with_template():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "new-page"})

    make_client(handler).create_page("Ada Lovelace", "L1", "tpl-1", "Europe/London")
    req = captured[0]
    assert req.method == "POST" and req.url.path == "/v1/pages"
    body = json.loads(req.content)
    assert body == {
        "parent": {"type": "data_source_id", "data_source_id": "ds123"},
        "properties": {
            "Full Name": {"title": [{"text": {"content": "Ada Lovelace"}}]},
            "LifeOS_ID": {"rich_text": [{"text": {"content": "L1"}}]},
        },
        "template": {"type": "template_id", "template_id": "tpl-1", "timezone": "Europe/London"},
    }
    assert "children" not in body and "content" not in body


def test_create_page_without_template_has_no_template_key():
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    make_client(handler).create_page("Ada", "L1", "", None)
    assert "template" not in captured[0]
    make_client(handler).create_page("Ada", "L1", "tpl", None)
    assert captured[1]["template"] == {"type": "template_id", "template_id": "tpl"}


def test_create_page_is_not_retried_after_a_server_error_but_is_after_429():
    calls = []

    def boom(request):
        calls.append(1)
        return httpx.Response(502, json={})

    with pytest.raises(sync.NotionError):
        make_client(boom).create_page("Ada", "L1")
    assert len(calls) == 1

    seq = []

    def limited(request):
        seq.append(1)
        return httpx.Response(429, headers={"Retry-After": "1"}, json={}) if len(seq) == 1 \
            else httpx.Response(200, json={"id": "x"})

    assert make_client(limited).create_page("Ada", "L1") == {"id": "x"}
    assert len(seq) == 2


def test_wait_for_template_polls_children_until_content_exists():
    polls = []
    sleeps = []

    def handler(request):
        polls.append(request.url.path)
        return httpx.Response(200, json={"results": [] if len(polls) < 3 else [{"id": "blk"}]})

    assert make_client(handler, sleeps).wait_for_template("pg1") is True
    assert polls == ["/v1/blocks/pg1/children"] * 3 and sleeps == [2.0, 2.0]


def test_wait_for_template_gives_up_after_the_timeout():
    polls = []
    sleeps = []

    def handler(request):
        polls.append(1)
        return httpx.Response(200, json={"results": []})

    assert make_client(handler, sleeps).wait_for_template("pg1", interval=2.0, timeout=6.0) is False
    assert len(polls) == 4 and sleeps == [2.0, 2.0, 2.0]


def test_wait_for_template_read_failure_is_not_fatal():
    def handler(request):
        return httpx.Response(403, json={"code": "restricted_resource", "message": "no read"})

    assert make_client(handler).wait_for_template("pg1") is False


def test_retry_on_429_honours_retry_after():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json={"results": [], "has_more": False})

    assert make_client(handler, sleeps).query_rows() == []
    assert len(calls) == 2 and 3.0 in sleeps


def test_retry_on_5xx_then_gives_up_with_bounded_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={})

    with pytest.raises(sync.NotionError) as err:
        make_client(handler, max_retries=2).update_page("p", {})
    assert len(calls) == 3 and err.value.status == 503


def test_retry_on_transport_error():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={})

    make_client(handler).update_page("p", {})
    assert len(calls) == 2


def test_retry_on_409_conflict():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(409, json={}) if len(calls) == 1 else httpx.Response(200, json={})

    make_client(handler).update_page("p", {})
    assert len(calls) == 2


@pytest.mark.parametrize("status,fragment", [
    (401, "rejected the token"),
    (403, "not shared with the integration"),
    (404, "not shared with the integration"),
])
def test_auth_and_sharing_errors_are_clear_and_not_retried(status, fragment):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"code": "some_code", "message": "nope"})

    with pytest.raises(sync.NotionError) as err:
        make_client(handler).query_rows()
    assert fragment in str(err.value) and err.value.status == status and len(calls) == 1
    assert "some_code" in str(err.value) and "nope" in str(err.value)  # Notion's own message is kept


def test_other_4xx_errors_carry_the_notion_message():
    def handler(request):
        return httpx.Response(400, json={"code": "validation_error", "message": "template is wrong"})

    with pytest.raises(sync.NotionError) as err:
        make_client(handler).update_page("p", {})
    assert err.value.code == "validation_error" and "template is wrong" in str(err.value)


def test_requests_are_paced():
    sleeps = []

    def handler(request):
        return httpx.Response(200, json={})

    client = make_client(handler, sleeps, pace=0.35)
    client.update_page("a", {})
    client.update_page("b", {})
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 0.35


def test_non_json_200_is_a_clear_notion_error():
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(sync.NotionError) as err:
        make_client(handler).query_rows()
    assert "not JSON" in str(err.value)


# ---------------------------------------------------------------------------
# Schema preflight
# ---------------------------------------------------------------------------

SCHEMA = {
    "Full Name": "title", "LifeOS_ID": "rich_text", "Email": "email", "Phone": "phone_number",
    "Last Check-In": "date", "Company": "rich_text", "Title": "rich_text",
    "LinkedIn": "url", "Instagram": "url", "Twitter/X": "url", "Website": "url",
    "Dex_ID": "rich_text", "Other": "select",
}


def _schema_client(props):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"properties": {k: {"type": v} for k, v in props.items()}})

    return make_client(handler), seen


def test_check_schema_accepts_expected_types_and_returns_optional_names():
    client, seen = _schema_client(SCHEMA)
    present = client.check_schema()
    assert seen == [("GET", "/v1/data_sources/ds123")]
    assert present == {"LinkedIn", "Instagram", "Twitter/X", "Website", "Dex_ID"}


def test_check_schema_optional_properties_may_be_absent():
    props = {k: v for k, v in SCHEMA.items() if k in sync.REQUIRED_PROPERTIES}
    assert _schema_client(props)[0].check_schema() == set()


@pytest.mark.parametrize("name", ["Full Name", "LifeOS_ID", "Email", "Phone", "Last Check-In",
                                  "Company", "Title"])
def test_check_schema_missing_required_property(name):
    props = {k: v for k, v in SCHEMA.items() if k != name}
    with pytest.raises(sync.NotionError) as err:
        _schema_client(props)[0].check_schema()
    assert f"no property '{name}'" in str(err.value)


def test_check_schema_wrong_type_names_property_and_types():
    for prop, found, expected in (("Email", "rich_text", "email"), ("Company", "title", "rich_text"),
                                  ("Last Check-In", "created_time", "date")):
        with pytest.raises(sync.NotionError) as err:
            _schema_client(dict(SCHEMA, **{prop: found}))[0].check_schema()
        assert f"'{prop}' has type {found}, expected {expected}" in str(err.value)


@pytest.mark.parametrize("name", ["LinkedIn", "Instagram", "Twitter/X", "Website", "Dex_ID"])
def test_check_schema_optional_property_with_the_wrong_type_stops_the_run(name):
    with pytest.raises(sync.NotionError) as err:
        _schema_client(dict(SCHEMA, **{name: "select"}))[0].check_schema()
    assert f"'{name}' has type select" in str(err.value)


def test_run_sync_stops_before_querying_when_schema_is_wrong(tmp_path):
    class Bad(FakeClient):
        def query_rows(self, limit=None):
            raise AssertionError("must not query")

    bad = Bad([])
    bad.schema_error = "schema wrong"
    with pytest.raises(sync.NotionError):
        run(bad, [], tmp_path)


def test_run_sync_skips_fields_missing_from_the_schema_and_logs_it(tmp_path, caplog):
    pages = [page("p1", lifeos_id="L1")]
    people = [person("L1", social=[link("instagram", "https://instagram.com/a"),
                                   link("twitter", "https://twitter.com/a")])]
    client = FakeClient(pages, available={"Instagram"})
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        run(client, people, tmp_path, execute=True)
    assert client.updates == [("p1", {"Instagram": {"url": "https://instagram.com/a"}})]
    assert "'Twitter/X' is not in the Notion schema" in caplog.text


# ---------------------------------------------------------------------------
# Token secrecy
# ---------------------------------------------------------------------------

def test_error_messages_never_contain_the_token():
    def echo(request):
        return httpx.Response(400, text=f"bad header Bearer {TOKEN}")

    with pytest.raises(sync.NotionError) as err:
        make_client(echo).update_page("p", {})
    assert TOKEN not in str(err.value) and "***" in str(err.value)

    def echo_json(request):
        return httpx.Response(400, json={"code": "x", "message": f"token {TOKEN} invalid"})

    with pytest.raises(sync.NotionError) as err3:
        make_client(echo_json).update_page("p", {})
    assert TOKEN not in str(err3.value)

    def boom(request):
        raise httpx.ConnectError(f"cannot reach, auth {TOKEN}")

    with pytest.raises(sync.NotionError) as err2:
        make_client(boom, max_retries=0).update_page("p", {})
    assert TOKEN not in str(err2.value)


def test_redact_helper():
    assert sync.redact(f"a {TOKEN} b", TOKEN) == "a *** b"
    assert sync.redact("plain", "") == "plain"


def test_log_output_never_contains_the_token(tmp_path):
    log_file = tmp_path / "logs" / "sync.log"
    sync.setup_logging(log_file, TOKEN)
    sync.logger.error("request failed: %s", f"Authorization: Bearer {TOKEN}")
    sync.logger.info(f"plain message {TOKEN}")
    for handler in sync.logger.handlers:
        handler.flush()
    text = log_file.read_text()
    assert "request failed" in text and TOKEN not in text


def test_ledger_and_stats_never_contain_the_token(tmp_path):
    pages = [page("p1", lifeos_id="L1")]
    stats = run(FakeClient(pages), [person("L1", emails=["a@example.com"])], tmp_path, execute=True)
    assert TOKEN not in json.dumps(stats) and TOKEN not in (tmp_path / "ledger.jsonl").read_text()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def _main_with(monkeypatch, tmp_path, client, people, *extra):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    monkeypatch.setattr(sync, "NotionClient", lambda token, ds, **kw: client)
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: people)
    return sync.main(["--no-file-log", "--map", str(tmp_path / "map.json"),
                      "--dexmap", str(tmp_path / "dexmap.json"),
                      "--ledger", str(tmp_path / "ledger.jsonl"), *extra])


def test_main_without_token_fails_cleanly(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", "")
    rc = sync.main(["--no-file-log", "--map", str(tmp_path / "m.json")])
    out = capsys.readouterr().out
    assert rc == sync.EXIT_CONFIG
    assert "LIFEOS_NOTION_TOKEN is not set" in out and "Traceback" not in out
    stats = _stats_line(out)
    assert stats["status"] == "config_error" and stats["exit_code"] == 2


def test_main_reports_unreachable_api_without_leaking_token(monkeypatch, capsys):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)

    def unreachable(api_base, transport=None):
        raise sync.LifeOSUnavailable(f"Could not read people from the LifeOS API at {api_base}")

    monkeypatch.setattr(sync, "fetch_people", unreachable)
    rc = sync.main(["--no-file-log"])
    out = capsys.readouterr().out
    assert rc == sync.EXIT_CONFIG and "Could not read people" in out and TOKEN not in out
    assert _stats_line(out)["exit_code"] == 2


def test_main_dry_run_prints_stats_with_the_new_keys(monkeypatch, capsys, tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    fake = FakeClient(pages)
    rc = _main_with(monkeypatch, tmp_path, fake, people)
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 0 and stats["mode"] == "dry-run" and stats["changes"] == 2
    for key in ("id_writes", "creates", "template_pending", "orphans", "duplicates", "ledger_path"):
        assert key in stats
    assert stats["ledger_path"] is None
    assert fake.updates == [] and not (tmp_path / "map.json").exists()
    assert not (tmp_path / "ledger.jsonl").exists()


def test_main_execute_reports_ledger_path_and_uses_settings(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    fake = FakeClient(pages)
    monkeypatch.setattr(settings, "notion_person_template_id", "tpl-9")
    people.append(person("N", name="New Person", message_count=2))
    rc = _main_with(monkeypatch, tmp_path, fake, people, "--execute")
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 0 and stats["ledger_path"] == str(tmp_path / "ledger.jsonl")
    assert stats["id_writes"] == 2 and stats["creates"] == 1
    assert fake.creates[0][2] == "tpl-9"


def test_main_passes_caps_from_the_command_line(monkeypatch, capsys, tmp_path):
    pages, people, dexmap = many(3)
    write_dexmap(tmp_path, dexmap)
    fake = FakeClient(pages)
    rc = _main_with(monkeypatch, tmp_path, fake, people, "--execute", "--max-writes", "1",
                    "--max-id-writes", "2", "--max-creates", "0")
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 0 and stats["writes"] == 1 and stats["id_writes"] == 2
    assert stats["max_writes"] == 1 and stats["max_id_writes"] == 2 and stats["max_creates"] == 0


def test_main_corrupt_map_in_execute_exits_2_and_prints_stats(monkeypatch, capsys, tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    (tmp_path / "map.json").write_text("garbage")
    fake = FakeClient(pages)
    rc = _main_with(monkeypatch, tmp_path, fake, people, "--execute")
    out = capsys.readouterr().out
    assert rc == 2 and fake.updates == [] and "unreadable" in out
    assert _stats_line(out)["status"] == "config_error"
    assert (tmp_path / "map.json").read_text() == "garbage"


def test_main_midway_401_reports_writes_already_done(monkeypatch, capsys, tmp_path):
    pages, people, dexmap = many(3)
    write_dexmap(tmp_path, dexmap)

    class Revoked(FakeClient):
        def update_page(self, page_id, properties):
            if len(self.updates) == 1:
                raise sync.NotionError("Notion rejected the token (HTTP 401)", 401)
            return super().update_page(page_id, properties)

    rc = _main_with(monkeypatch, tmp_path, Revoked(pages), people, "--execute")
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 2 and stats["writes"] == 0 and stats["id_writes"] == 1 and stats["rows"] == 3
    assert stats["exit_code"] == 2 and stats["status"] == "config_error"


def test_main_schema_mismatch_exits_2_with_stats(monkeypatch, capsys, tmp_path):
    fake = FakeClient([])
    fake.schema_error = "Notion property 'Email' has type rich_text, expected email"
    rc = _main_with(monkeypatch, tmp_path, fake, [])
    out = capsys.readouterr().out
    assert rc == 2 and "'Email' has type rich_text" in out and _stats_line(out)["exit_code"] == 2


def test_main_write_failures_exit_1_with_stats(monkeypatch, capsys, tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)

    class Rejecting(FakeClient):
        def update_page(self, page_id, properties):
            raise sync.NotionError("HTTP 400", 400)

    rc = _main_with(monkeypatch, tmp_path, Rejecting(pages), people, "--execute")
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 1 and stats["failed"] == 4 and stats["status"] == "write_failures"


def test_main_unwritable_ledger_exits_2(monkeypatch, capsys, tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    fake = FakeClient(pages)
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    monkeypatch.setattr(sync, "NotionClient", lambda token, ds, **kw: fake)
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: people)
    rc = sync.main(["--execute", "--no-file-log", "--map", str(tmp_path / "map.json"),
                    "--dexmap", str(tmp_path / "dexmap.json"),
                    "--ledger", str(blocker / "ledger.jsonl")])
    assert rc == 2 and fake.updates == []
    assert _stats_line(capsys.readouterr().out)["status"] == "config_error"


def test_main_unexpected_value_error_exits_2_with_stats(monkeypatch, capsys, tmp_path):
    class Broken(FakeClient):
        def query_rows(self, limit=None):
            raise ValueError("bad body")

    rc = _main_with(monkeypatch, tmp_path, Broken([]), [])
    out = capsys.readouterr().out
    assert rc == 2 and _stats_line(out)["status"] == "error" and "Traceback" not in out


def test_main_mapping_abort_prints_stats(monkeypatch, capsys, tmp_path):
    pages, people, _ = many(3)
    rc = _main_with(monkeypatch, tmp_path, FakeClient(pages), people)
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 3 and stats["status"] == "mapping_abort" and stats["rows"] == 3


def test_main_non_json_200_exits_2(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="oops"))
    real = sync.NotionClient
    monkeypatch.setattr(
        sync, "NotionClient",
        lambda token, ds, **kw: real(token, ds, transport=transport, pace=0, **kw),
    )
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: [])
    rc = sync.main(["--no-file-log", "--map", str(tmp_path / "m.json"),
                    "--dexmap", str(tmp_path / "d.json")])
    assert rc == 2 and _stats_line(capsys.readouterr().out)["exit_code"] == 2


def test_full_run_over_a_mock_transport_dry_run_sends_no_writes(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    methods = []

    def handler(request):
        methods.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"properties": {k: {"type": v} for k, v in SCHEMA.items()}})
        return httpx.Response(200, json={"results": [page("p1", name="Ada Lovelace", lifeos_id="L1")],
                                         "has_more": False})

    transport = httpx.MockTransport(handler)
    real = sync.NotionClient
    monkeypatch.setattr(sync, "NotionClient",
                        lambda token, ds, **kw: real(token, ds, transport=transport, pace=0, **kw))
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: [
        person("L1", emails=["a@example.com"]), person("N", name="New Person", message_count=2)])
    rc = sync.main(["--no-file-log", "--map", str(tmp_path / "m.json"),
                    "--dexmap", str(tmp_path / "d.json"), "--ledger", str(tmp_path / "l.jsonl")])
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 0 and stats["planned_creates"] == 1 and stats["changes"] == 1
    assert methods == [("GET", "/v1/data_sources/c841f807-b4d6-824f-8105-87105c118db7"),
                       ("POST", "/v1/data_sources/c841f807-b4d6-824f-8105-87105c118db7/query")]


# ---------------------------------------------------------------------------
# LifeOS API
# ---------------------------------------------------------------------------

def test_fetch_people_reads_the_list():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"people": [{"id": "A"}]})

    people = sync.fetch_people("http://127.0.0.1:8000/", transport=httpx.MockTransport(handler))
    assert people == [{"id": "A"}]
    assert seen[0].url.path == "/api/crm/people" and seen[0].url.params["limit"] == "5000"


def test_fetch_people_unreachable_is_a_clear_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(sync.LifeOSUnavailable) as err:
        sync.fetch_people("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    assert "LifeOS API" in str(err.value)


def test_fetch_people_refuses_a_truncated_list():
    for body in ({"people": [{"id": "A"}], "has_more": True},
                 {"people": [{"id": "A"}], "total": 5}):
        transport = httpx.MockTransport(lambda request, b=body: httpx.Response(200, json=b))
        with pytest.raises(sync.LifeOSUnavailable) as err:
            sync.fetch_people("http://127.0.0.1:8000", transport=transport)
        assert "truncated" in str(err.value)
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"people": [{"id": "A"}], "total": 1}))
    assert sync.fetch_people("http://127.0.0.1:8000", transport=ok) == [{"id": "A"}]


# ---------------------------------------------------------------------------
# Company and Title: value-quality gate and the "Notion is richer" guard
# ---------------------------------------------------------------------------

JUNK = ["--", "'--", "—", '"--"', "12345", "alice@example.com", "x" * 101, "CEO | Speaker | Author"]


@pytest.mark.parametrize("value", JUNK)
@pytest.mark.parametrize("key,prop", [("company", "Company"), ("position", "Title")])
def test_quality_gate_rejects_junk_for_company_and_title(key, prop, value):
    for kwargs in ({"company": "Old Co", "title": "Old Title"}, {}):
        skips = []
        change, _ = sync.compute_changes(
            sync.parse_row(page("p1", lifeos_id="A", **kwargs)), person("A", **{key: value}),
            now=NOW, tz=UTC, skips=skips)
        assert change is None and len(skips) == 1 and "not written" in skips[0]


def test_quality_gate_accepts_a_bare_at_used_as_the_word_at():
    p = person("A", position="Partnerships @ The Carlyle Group")
    change, _ = changes_for(page("p1", lifeos_id="A", title="Partner"), p)
    assert change.properties() == {
        "Title": {"rich_text": [{"text": {"content": "Partnerships @ The Carlyle Group"}}]}}


def test_email_pattern_is_rejected_but_only_as_a_whole_address():
    assert sync.reject_reason("name@example.com") == "looks like an email address"
    assert sync.reject_reason("Contact name@example.com today") == "looks like an email address"
    assert sync.reject_reason("Head @ Acme") is None
    assert sync.reject_reason("--") == "no letters"
    assert sync.reject_reason("a" * 100) is None and sync.reject_reason("a" * 101)


@pytest.mark.parametrize("lifeos,notion", [
    ("Acme & Co.", "acme and co"),
    ("VP, Sales", "VP Sales"),
    ("“Acme”", "Acme"),
    ("Acme   Corp", "acme corp"),
    ("R&D Lead", "R and D lead"),
])
def test_formatting_only_differences_never_write(lifeos, notion):
    assert changes_for(page("p1", lifeos_id="A", company=notion), person("A", company=lifeos))[0] is None
    assert changes_for(page("p1", lifeos_id="A", title=notion), person("A", position=lifeos))[0] is None


def test_notion_value_with_more_detail_is_kept():
    skips = []
    row = sync.parse_row(page("p1", lifeos_id="A", title="Business Development at iCapital Network"))
    change, _ = sync.compute_changes(row, person("A", position="Business Development"),
                                     now=NOW, tz=UTC, skips=skips)
    assert change is None and "richer" in skips[0]
    # a word subset counts too
    row = sync.parse_row(page("p1", lifeos_id="A", title="Director of Sales and Marketing"))
    assert sync.compute_changes(row, person("A", position="Sales Marketing"), now=NOW, tz=UTC)[0] is None
    # the other direction (LifeOS holds more) is written
    row = sync.parse_row(page("p1", lifeos_id="A", title="Engineer"))
    change, _ = sync.compute_changes(row, person("A", position="Chief Engineer at Acme"), now=NOW, tz=UTC)
    assert change.fields[0].new == "Chief Engineer at Acme"
    assert sync.notion_is_richer("a b", "a b c") and not sync.notion_is_richer("a b", "a b")


def test_a_genuinely_different_value_still_overwrites():
    change, _ = changes_for(page("p1", lifeos_id="A", company="Old Co"), person("A", company="New Co"))
    assert change.fields[0].old == "Old Co" and change.fields[0].new == "New Co"


def test_skipped_values_are_counted_and_logged_in_the_run(tmp_path, caplog):
    pages = [page("p1", lifeos_id="L1", title="Director of Sales")]
    people = [person("L1", position="Sales", company="--")]
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        stats = run(FakeClient(pages), people, tmp_path, execute=True)
    assert stats["skipped_values"] == 2 and "SKIPPED Ada Lovelace" in caplog.text
    assert stats["writes"] == 0


# ---------------------------------------------------------------------------
# --fill-only-fields
# ---------------------------------------------------------------------------

def test_fill_only_fields_fill_empty_cells_but_never_overwrite():
    p = person("A", emails=["new@example.com"], phones=["+12125550173"], company="Acme", position="Founder",
               social=[link("instagram", "https://instagram.com/ada")])
    fo = frozenset({"Email", "Phone", "Company", "Title", "Instagram"})
    empty, _ = sync.compute_changes(sync.parse_row(page("p1", lifeos_id="A")), p, now=NOW, tz=UTC,
                                    fill_only=fo)
    assert set(empty.properties()) == {"Email", "Phone", "Company", "Title", "Instagram"}
    filled = sync.parse_row(page("p1", lifeos_id="A", email="old@example.com", phone="+16505550000",
                                 company="Old", title="Old T", instagram="https://instagram.com/other"))
    assert sync.compute_changes(filled, p, now=NOW, tz=UTC, fill_only=fo)[0] is None
    # without fill-only the same row is overwritten
    assert sync.compute_changes(filled, p, now=NOW, tz=UTC)[0] is not None


def test_fill_only_check_in_never_replaces_an_older_date():
    p = person("A", last_seen="2026-05-20T00:00:00Z")
    row = sync.parse_row(page("p1", lifeos_id="A", checkin="2026-01-01"))
    assert sync.compute_changes(row, p, now=NOW, tz=UTC, fill_only=frozenset({"Last Check-In"}))[0] is None


def test_parse_fill_only_validates_names():
    assert sync.parse_fill_only("") == frozenset() and sync.parse_fill_only(None) == frozenset()
    assert sync.parse_fill_only(" email , PHONE,Twitter/X") == frozenset({"Email", "Phone", "Twitter/X"})
    with pytest.raises(sync.ConfigError) as err:
        sync.parse_fill_only("Email, Nickname")
    assert "Nickname" in str(err.value) and "Allowed" in str(err.value)
    with pytest.raises(sync.ConfigError):
        sync.parse_fill_only("LifeOS_ID")


def test_main_unknown_fill_only_name_exits_2_before_any_request(monkeypatch, capsys, tmp_path):
    fake = FakeClient([])
    fake.schema_error = "must not be reached"
    rc = _main_with(monkeypatch, tmp_path, fake, [], "--fill-only-fields", "Bogus")
    out = capsys.readouterr().out
    assert rc == 2 and "Bogus" in out and "must not be reached" not in out
    assert _stats_line(out)["status"] == "config_error"


def test_main_fill_only_comes_from_the_option_or_the_setting(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    pages = [page("p1", lifeos_id="L1", email="old@example.com")]
    people = [person("L1", emails=["new@example.com"])]
    monkeypatch.setattr(settings, "notion_fill_only_fields", "Email")
    fake = FakeClient(pages)
    rc = _main_with(monkeypatch, tmp_path, fake, people, "--execute")
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 0 and stats["fill_only"] == ["Email"] and fake.updates == []
    fake2 = FakeClient([page("p1", lifeos_id="L1", email="old@example.com")])
    rc = _main_with(monkeypatch, tmp_path, fake2, people, "--execute", "--fill-only-fields", "Phone")
    stats = _stats_line(capsys.readouterr().out)
    assert stats["fill_only"] == ["Phone"] and fake2.updates  # the option overrides the setting


def test_settings_fill_only_default_is_empty():
    from config.settings import Settings

    assert Settings(_env_file=None).notion_fill_only_fields == ""


# ---------------------------------------------------------------------------
# Create filter
# ---------------------------------------------------------------------------

def test_outlook_only_contacts_and_odd_names_are_not_created(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [
        person("E", name="Somebody Else"),
        person("O", name="Some Organisation", last_seen="2026-05-01T00:00:00Z"),  # Outlook-only
        person("AT", name="ann@example.com", message_count=3),
        person("RU", name="Ru", message_count=3, relationship_strength=0.1),
        person("REAL", name="Real Contact", email_count=2, relationship_strength=0.4),
    ]
    client = FakeClient(existing)
    stats = run(client, people, tmp_path, execute=True)
    assert [c[0] for c in client.creates] == ["Real Contact", "Ru"]
    assert stats["creates"] == 2


# ---------------------------------------------------------------------------
# Name-only links are report-only
# ---------------------------------------------------------------------------

def test_row_whose_identifiers_match_another_person_is_never_name_linked():
    people = [person("A", name="Ada Lovelace", emails=["a@example.com"]),
              person("B", name="Bea Other", message_count=3, emails=["b@example.com"])]
    plan = plan_for([page("p1", name="Bea Other", email="a@example.com")], people)
    # The email resolves to Ada, the name check rejects it, and the name alone never links.
    assert plan.possible_matches == [] and plan.changes == [] and plan.new_mappings == {}
    assert plan.unmatched[0][1].startswith("name mismatch")
    assert plan.creates == []  # the row's name still blocks a second page for Bea


def test_name_only_candidate_is_reported_in_the_plan_and_never_changed():
    people = [person("N", name="Bob (Robert) Lee", message_count=2, company="Acme", emails=["r@example.com"])]
    plan = plan_for([page("p1", name="Robert Lee")], people)
    assert [(r.page_id, p["id"]) for r, p in plan.possible_matches] == [("p1", "N")]
    assert plan.changes == [] and plan.new_mappings == {} and plan.creates == []
    assert plan.unmatched[0][1].startswith("possible match, review by hand")


# ---------------------------------------------------------------------------
# Duplicate pages
# ---------------------------------------------------------------------------

def test_two_people_with_the_same_name_create_one_page(tmp_path, caplog):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"),
              person("T1", name="Twin Person", message_count=2, relationship_strength=0.9),
              person("T2", name="Twin Person", message_count=1, relationship_strength=0.1)]
    client = FakeClient(existing)
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(client, people, tmp_path, execute=True)
    assert [(c[0], c[1]) for c in client.creates] == [("Twin Person", "T1")]
    assert stats["duplicate_name_skips"] == 1 and "SKIPPED CREATE Twin Person" in caplog.text


def test_orphan_row_with_a_merged_person_does_not_get_a_second_page(tmp_path):
    existing = [page("p1", name="Merged Person", lifeos_id="GONE"),
                page("p2", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"), person("M", name="Merged Person", message_count=5)]
    client = FakeClient(existing)
    stats = run(client, people, tmp_path, execute=True)
    assert client.creates == [] and stats["orphans"] == 1 and stats["duplicate_name_skips"] == 1


def test_a_second_lifeos_record_for_the_same_human_is_not_a_second_page(tmp_path):
    existing = [page("p1", name="Ada Lovelace", lifeos_id="A")]
    people = [person("A", name="Ada Lovelace"),
              person("A2", name="Ada Lovelace", message_count=4)]
    client = FakeClient(existing)
    stats = run(client, people, tmp_path, execute=True)
    assert client.creates == [] and stats["duplicate_name_skips"] == 1


def test_name_with_credentials_or_unmatched_row_still_blocks_a_duplicate(tmp_path):
    existing = [page("p1", name="Haley Hernandez, CFP"), page("p2", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"), person("H", name="Haley Hernandez", message_count=3)]
    client = FakeClient(existing)
    run(client, people, tmp_path, execute=True)
    assert client.creates == []


def _pending_setup(tmp_path):
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"), new_people()[1]]
    return people


def test_create_with_unknown_outcome_stays_pending_and_is_never_repeated(tmp_path, caplog):
    people = _pending_setup(tmp_path)
    existing = [page("p1", name="Somebody Else", dex="d1")]

    first = FakeClient(list(existing))
    first.create_error = sync.NotionError("Notion request failed: ReadTimeout", None)
    stats = run(first, people, tmp_path, execute=True)
    assert stats["status"] == "creates_stopped" and stats["pending_creates"] == 1
    stored = json.loads((tmp_path / "map.json").read_text())["pages"]
    assert stored["pending:N2"]["pending"] is True and stored["pending:N2"]["lifeos_id"] == "N2"
    assert any(l["phase"] == "result" and l["status"] == "unknown" for l in ledger_lines(tmp_path))

    # The query does not show the page yet: it is not created again, and is reported.
    second = FakeClient(list(existing))
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(second, people, tmp_path, execute=True)
    assert second.creates == [] and stats["planned_creates"] == 0 and stats["pending_creates"] == 1
    assert "PENDING create for LifeOS N2" in caplog.text

    # Once a row with that LifeOS_ID shows up, the entry is resolved.
    third = FakeClient(list(existing) + [page("late", name="High Strength", lifeos_id="N2")])
    stats = run(third, people, tmp_path, execute=True)
    assert third.creates == [] and stats["pending_creates"] == 0
    assert "pending:N2" not in json.loads((tmp_path / "map.json").read_text())["pages"]


def test_5xx_on_create_is_also_unknown_but_a_4xx_is_not_pending(tmp_path):
    people = _pending_setup(tmp_path)
    existing = [page("p1", name="Somebody Else", dex="d1")]
    server = FakeClient(list(existing))
    server.create_error = sync.NotionError("HTTP 502", 502)
    assert run(server, people, tmp_path, execute=True)["pending_creates"] == 1

    clean = tmp_path / "clean"
    clean.mkdir()
    write_dexmap(clean, {"d1": "E"})
    client = FakeClient(list(existing))
    client.create_error = sync.NotionError("HTTP 400 [validation_error]", 400)
    stats = run(client, people, clean, execute=True)
    assert stats["pending_creates"] == 0
    assert "pending:N2" not in json.loads((clean / "map.json").read_text())["pages"]


def test_dry_run_reports_pending_entries_without_changing_the_map(tmp_path):
    people = _pending_setup(tmp_path)
    sync.save_map_atomic(tmp_path / "map.json", {"pending:N2": {"lifeos_id": "N2", "pending": True}})
    client = FakeClient([page("p1", name="Somebody Else", dex="d1")])
    stats = run(client, people, tmp_path, execute=False)
    assert stats["pending_creates"] == 1 and stats["planned_creates"] == 0
    assert "pending:N2" in json.loads((tmp_path / "map.json").read_text())["pages"]


# ---------------------------------------------------------------------------
# Ledger ordering and full old values
# ---------------------------------------------------------------------------

def test_intent_line_is_on_disk_before_each_patch(tmp_path):
    snapshots = []

    class Checking(FakeClient):
        def update_page(self, page_id, properties):
            lines = ledger_lines(tmp_path)
            snapshots.append((page_id, [l["phase"] for l in lines if l["page_id"] == page_id],
                              [l["field"] for l in lines if l["phase"] == "intent"
                               and l["page_id"] == page_id]))
            return super().update_page(page_id, properties)

    pages = [page("p1", lifeos_id="L1", email="old@example.com", company="Old Co")]
    run(Checking(pages), [person("L1", emails=["new@example.com"], company="Acme")], tmp_path, execute=True)
    page_id, phases, fields = snapshots[0]
    assert phases == ["intent", "intent"] and set(fields) == {"Email", "Company"}  # no result yet
    assert [l["phase"] for l in ledger_lines(tmp_path)] == ["intent", "intent", "result", "result"]


def test_intent_line_is_on_disk_before_the_create_request(tmp_path):
    seen = []

    class Checking(FakeClient):
        def create_page(self, name, lifeos_id, template_id="", timezone_name=None):
            seen.append([(l["phase"], l["action"], l["new"]) for l in ledger_lines(tmp_path)
                         if l["action"] == "create"])
            return super().create_page(name, lifeos_id, template_id, timezone_name)

    write_dexmap(tmp_path, {"d1": "E"})
    run(Checking([page("p1", name="Somebody Else", dex="d1")]),
        [person("E", name="Somebody Else"), new_people()[1]], tmp_path, execute=True)
    assert seen == [[("intent", "create", "High Strength")]]


def test_ledger_failure_before_a_request_stops_that_request_and_the_run(monkeypatch, capsys, tmp_path):
    original = sync.Ledger.record

    def failing(self, phase, action, *args, **kwargs):
        if phase == "intent" and action == "update":
            raise sync.LedgerError("disk full")
        return original(self, phase, action, *args, **kwargs)

    monkeypatch.setattr(sync.Ledger, "record", failing)
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    fake = FakeClient(pages)
    rc = _main_with(monkeypatch, tmp_path, fake, people, "--execute")
    stats = _stats_line(capsys.readouterr().out)
    assert rc == 2 and stats["status"] == "config_error"
    assert [(pid, set(props)) for pid, props in fake.updates] == [("p1", {"LifeOS_ID"})]  # no field write


def test_map_save_failure_right_after_a_create_still_leaves_the_create_in_the_ledger(monkeypatch, tmp_path):
    original = sync.save_map_atomic

    def flaky(path, pages):
        if any(k.startswith("new-") for k in pages):
            raise OSError("disk full")
        return original(path, pages)

    monkeypatch.setattr(sync, "save_map_atomic", flaky)
    write_dexmap(tmp_path, {"d1": "E"})
    with pytest.raises(OSError):
        run(FakeClient([page("p1", name="Somebody Else", dex="d1")]),
            [person("E", name="Somebody Else"), new_people()[1]], tmp_path, execute=True)
    done = [l for l in ledger_lines(tmp_path)
            if l["phase"] == "result" and l["action"] == "create" and l["status"] == "ok"]
    assert done and done[0]["page_id"] == "new-1" and done[0]["lifeos_id"] == "N2"


def test_old_values_are_stored_in_full(tmp_path):
    pg = page("p1", lifeos_id="L1", company="Old Co", checkin="2026-01-01")
    full_text = [{"plain_text": "Old Co", "annotations": {"bold": True}, "href": "https://old.example.com"}]
    full_date = {"start": "2026-01-01", "end": "2026-01-05", "time_zone": "America/New_York"}
    pg["properties"]["Company"] = {"rich_text": full_text}
    pg["properties"]["Last Check-In"] = {"date": full_date}
    run(FakeClient([pg]), [person("L1", company="Acme", last_seen="2026-05-20T00:00:00Z")], tmp_path,
        execute=True)
    intents = {l["field"]: l for l in ledger_lines(tmp_path) if l["phase"] == "intent"}
    assert intents["Company"]["old"] == "Old Co" and intents["Company"]["old_full"] == full_text
    assert intents["Last Check-In"]["old"] == "2026-01-01" and intents["Last Check-In"]["old_full"] == full_date
    assert intents["Company"]["new"] == "Acme"


# ---------------------------------------------------------------------------
# Email and Phone displacement
# ---------------------------------------------------------------------------

def test_overwrites_of_unknown_email_and_phone_are_called_out(tmp_path, caplog):
    pages = [page("p1", name="Ada Lovelace", lifeos_id="L1", email="stranger@example.com",
                  phone="+16505550000"),
             page("p2", name="Grace Hopper", lifeos_id="L2"),
             page("p3", name="Alan Turing", lifeos_id="L3", email="known@example.com")]
    people = [person("L1", emails=["ada@example.com"], phones=["+12125550173"]),
              person("L2", name="Grace Hopper", emails=["grace@example.com"]),
              person("L3", name="Alan Turing", emails=["known@example.com"])]
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(FakeClient(pages), people, tmp_path)  # a dry run reports them too
    assert stats["displaced_values"] == 2
    assert "DISPLACES a value LifeOS has never seen: Ada Lovelace (p1): Email: stranger@example.com" in caplog.text
    assert "Grace Hopper" not in "".join(l for l in caplog.text.splitlines() if "DISPLACES" in l)


def test_fill_only_email_and_phone_are_protected_from_displacement(tmp_path):
    pages = [page("p1", lifeos_id="L1", email="stranger@example.com", phone="+16505550000")]
    people = [person("L1", emails=["ada@example.com"], phones=["+12125550173"])]
    client = FakeClient(pages)
    stats = run(client, people, tmp_path, execute=True, fill_only=frozenset({"Email", "Phone"}))
    assert client.updates == [] and stats["displaced_values"] == 0


# ---------------------------------------------------------------------------
# Smaller request-safety fixes
# ---------------------------------------------------------------------------

def test_rich_text_over_2000_characters_is_truncated_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        value = sync.rich_text("x" * 2500)
    assert len(value["rich_text"][0]["text"]["content"]) == 2000 and "truncated" in caplog.text
    assert len(sync.rich_text("short")["rich_text"][0]["text"]["content"]) == 5


def test_id_write_is_its_own_request_and_survives_a_bad_field_value(tmp_path):
    class RejectsEmail(FakeClient):
        def update_page(self, page_id, properties):
            if "Email" in properties:
                raise sync.NotionError("HTTP 400 [validation_error]", 400)
            return super().update_page(page_id, properties)

    pages = [page("p1", dex="d1")]
    write_dexmap(tmp_path, {"d1": "L1"})
    client = RejectsEmail(pages)
    stats = run(client, [person("L1", emails=["a@example.com"])], tmp_path, execute=True)
    assert stats["id_writes"] == 1 and stats["failed"] == 1 and stats["writes"] == 0
    assert read(client, "p1", "LifeOS_ID") == {"rich_text": [{"plain_text": "L1"}]}


def test_bare_handle_is_not_a_url_and_nothing_is_written():
    assert sync.https_url("@annlong") is None
    assert sync.https_url("annlong") is None
    assert sync.https_url("ann@example.com") is None
    assert sync.https_url("instagram.com/ann") == "https://instagram.com/ann"
    skips = []
    row = sync.parse_row(page("p1", lifeos_id="A"))
    change, _ = sync.compute_changes(row, person("A", social=[link("instagram", "@annlong")]),
                                     now=NOW, tz=UTC, skips=skips)
    assert change is None and "not a URL" in skips[0]


def test_percent_encoded_urls_compare_equal_to_their_decoded_form():
    assert sync.normalize_url("https://linkedin.com/in/jos%C3%A9") == sync.normalize_url(
        "https://www.linkedin.com/in/josé/")
    p = person("A", linkedin="https://www.linkedin.com/in/jos%C3%A9")
    assert changes_for(page("p1", lifeos_id="A", linkedin="https://linkedin.com/in/josé"), p)[0] is None


def test_403_on_a_patch_stops_all_further_updates(tmp_path):
    attempts = []

    class Forbidden(FakeClient):
        def update_page(self, page_id, properties):
            attempts.append(page_id)
            raise sync.NotionError("HTTP 403 [restricted_resource]", 403, "restricted_resource")

    pages, people, dexmap = many(3)
    write_dexmap(tmp_path, dexmap)
    stats = run(Forbidden(pages), people, tmp_path, execute=True)
    assert len(attempts) == 1 and stats["failed"] == 1
    assert stats["updates_stopped"].startswith("403")
    assert stats["status"] == "write_failures" and stats["exit_code"] == 1


def test_template_poll_timing_comes_from_constants():
    import inspect

    assert sync.TEMPLATE_POLL_INTERVAL == 2.0 and sync.TEMPLATE_POLL_TIMEOUT == 20.0
    params = inspect.signature(sync.NotionClient.wait_for_template).parameters
    assert params["interval"].default == sync.TEMPLATE_POLL_INTERVAL
    assert params["timeout"].default == sync.TEMPLATE_POLL_TIMEOUT


def test_stats_line_has_the_review_keys(monkeypatch, capsys, tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    _main_with(monkeypatch, tmp_path, FakeClient(pages), people)
    stats = _stats_line(capsys.readouterr().out)
    for key in ("pending_creates", "displaced_values", "possible_matches", "skipped_values",
                "duplicate_name_skips", "updates_stopped", "fill_only"):
        assert key in stats


# ---------------------------------------------------------------------------
# Pending creates that never resolve
# ---------------------------------------------------------------------------

def test_pending_entry_makes_the_run_exit_1_and_counts_the_runs(tmp_path, caplog):
    people = _pending_setup(tmp_path)
    existing = [page("p1", name="Somebody Else", dex="d1")]
    first = FakeClient(list(existing))
    first.create_error = sync.NotionError("HTTP 502", 502)
    run(first, people, tmp_path, execute=True)
    entry = json.loads((tmp_path / "map.json").read_text())["pages"]["pending:N2"]
    assert entry["runs"] == 1

    seen = []
    for n in (2, 3, 4):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
            stats = run(FakeClient(list(existing)), people, tmp_path, execute=True)
        assert stats["status"] == "pending_creates" and stats["exit_code"] == 1
        assert stats["pending_creates"] == 1
        seen.append(caplog.text)
        assert json.loads((tmp_path / "map.json").read_text())["pages"]["pending:N2"]["runs"] == n
    assert "pending for 2 runs" in seen[0] and "Check Notion" not in seen[0]
    assert "pending for 3 runs" in seen[1] and "Check Notion" in seen[1]
    assert "pending for 4 runs" in seen[2] and "Check Notion" in seen[2]


def test_dry_run_with_a_pending_entry_exits_1_but_keeps_the_counter(tmp_path):
    people = _pending_setup(tmp_path)
    sync.save_map_atomic(tmp_path / "map.json",
                         {"pending:N2": {"lifeos_id": "N2", "pending": True, "runs": 2}})
    stats = run(FakeClient([page("p1", name="Somebody Else", dex="d1")]), people, tmp_path)
    assert stats["status"] == "pending_creates" and stats["exit_code"] == 1
    assert json.loads((tmp_path / "map.json").read_text())["pages"]["pending:N2"]["runs"] == 2


def test_a_resolved_pending_entry_ends_the_non_zero_status(tmp_path):
    people = _pending_setup(tmp_path)
    sync.save_map_atomic(tmp_path / "map.json",
                         {"pending:N2": {"lifeos_id": "N2", "pending": True, "runs": 3}})
    client = FakeClient([page("p1", name="Somebody Else", dex="d1"),
                         page("late", name="High Strength", lifeos_id="N2")])
    stats = run(client, people, tmp_path, execute=True)
    assert stats["status"] == "ok" and stats["exit_code"] == 0 and stats["pending_creates"] == 0


def test_main_exit_status_is_1_for_a_pending_create(monkeypatch, capsys, tmp_path):
    people = _pending_setup(tmp_path)
    sync.save_map_atomic(tmp_path / "map.json", {"pending:N2": {"lifeos_id": "N2", "pending": True}})
    rc = _main_with(monkeypatch, tmp_path, FakeClient([page("p1", name="Somebody Else", dex="d1")]), people)
    assert rc == 1 and _stats_line(capsys.readouterr().out)["status"] == "pending_creates"


# ---------------------------------------------------------------------------
# Possible duplicates by surname and first initial
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row_name,lifeos_name", [
    ("Matt Eastman", "Matthew Eastman"),
    ("Rogers, Leah", "Leah Rogers"),
    ("Dr. Ann Smith", "Ann Smith"),
    ("Ann Smith, CFP", "Ann Smith"),
    ("Prof. Rogers, Leah", "Leah Rogers"),
])
def test_possible_duplicates_by_surname_and_initial_are_not_created(tmp_path, caplog, row_name, lifeos_name):
    existing = [page("p1", name=row_name), page("p2", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"), person("X", name=lifeos_name, message_count=4)]
    client = FakeClient(existing)
    with caplog.at_level(logging.WARNING, logger="notion_people_sync"):
        stats = run(client, people, tmp_path, execute=True)
    assert client.creates == []
    # an exact-name hit is reported as a possible match, a near hit as a possible duplicate
    assert "review by hand" in caplog.text


def test_exact_name_matches_count_as_duplicates_and_near_matches_as_possible_duplicates(tmp_path):
    existing = [page("p1", name="Matt Eastman"), page("p2", name="Twin Person", lifeos_id="GONE"),
                page("p3", name="Somebody Else", dex="d1"), page("p4", name="Other One", dex="d2"),
                page("p5", name="Third Row", dex="d3")]
    write_dexmap(tmp_path, {"d1": "E", "d2": "O", "d3": "R"})
    people = [person("E", name="Somebody Else"), person("O", name="Other One"), person("R", name="Third Row"),
              person("X", name="Matthew Eastman", message_count=4),
              person("T", name="Twin Person", message_count=4)]
    stats = run(FakeClient(existing), people, tmp_path)
    assert stats["possible_duplicate_skips"] == 1 and stats["duplicate_name_skips"] == 1


def test_different_first_initial_with_the_same_surname_is_still_created(tmp_path):
    existing = [page("p1", name="Bob Smith"), page("p2", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"), person("X", name="Ann Smith", message_count=4)]
    client = FakeClient(existing)
    stats = run(client, people, tmp_path, execute=True)
    assert [c[0] for c in client.creates] == ["Ann Smith"] and stats["possible_duplicate_skips"] == 0


def test_two_planned_creates_with_the_same_surname_and_initial_make_only_one_page(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"),
              person("X1", name="Matt Eastman", message_count=4, relationship_strength=0.9),
              person("X2", name="Matthew Eastman", message_count=2, relationship_strength=0.1)]
    client = FakeClient(existing)
    stats = run(client, people, tmp_path, execute=True)
    assert [c[1] for c in client.creates] == ["X1"] and stats["possible_duplicate_skips"] == 1


def test_loose_name_keys():
    k = sync.loose_name_keys
    assert k("Matt Eastman") == k("Matthew Eastman") == {"eastman|m"}
    assert k("Rogers, Leah") == k("Leah Rogers") == {"rogers|l"}
    assert k("Dr. Ann Smith") == k("Mrs Ann Smith") == k("Ann Smith, MBA") == {"smith|a"}
    assert k("Ru") == set() and k("") == set() and k(None) == set()
    assert k("Adrienne (Szarmack) Smith") == {"smith|a", "szarmack|a", "smith|s"}


# ---------------------------------------------------------------------------
# Bulk senders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("address", [
    "noreply@example.com", "No-Reply@example.com", "donotreply@example.com", "do-not-reply@example.com",
    "notifications@example.com", "notification@example.com", "newsletter@example.com", "news@example.com",
    "marketing@example.com", "mailer@example.com", "bounce@example.com",
    "team@em1234.cloudflare.com", "team@mail.vendor.com", "team@email.vendor.com", "x@mg.vendor.io",
    "x@bounce2.vendor.com", "x@notify.vendor.com",
])
def test_bulk_sender_addresses(address):
    assert sync.is_bulk_sender_email(address)


@pytest.mark.parametrize("address", [
    "ann@gmail.com", "ann@mail.com", "ann@email.com", "news.reader@example.com", "mailer.ann@example.com",
    "ann@em.com", "ann@example.com", "ann@mgmt.example.com", "ann@notifyme.example.com", "no-address",
])
def test_ordinary_addresses_are_not_bulk_senders(address):
    assert not sync.is_bulk_sender_email(address)


def test_a_person_with_only_bulk_sender_emails_does_not_qualify():
    q = sync.qualifies_for_page
    assert not q(person("V", name="Cloudflare", emails=["team@em1234.cloudflare.com"], dunbar_circle=4))
    assert not q(person("V", name="TexAu Desktop Team", emails=["noreply@texau.com", "news@texau.com"],
                        dunbar_circle=4, message_count=2))
    # one ordinary address, or none at all, leaves the person unaffected
    assert q(person("V", emails=["noreply@texau.com", "ann@gmail.com"], message_count=2))
    assert q(person("V", emails=["ann@gmail.com"], message_count=2))
    assert q(person("V", emails=[], message_count=2))
    assert not q(person("V", emails=["ann@gmail.com"]))  # still needs real interaction


def test_vendor_contact_is_not_created_in_a_run(tmp_path):
    existing = [page("p1", name="Somebody Else", dex="d1")]
    write_dexmap(tmp_path, {"d1": "E"})
    people = [person("E", name="Somebody Else"),
              person("V", name="Cloudflare", emails=["team@em1234.cloudflare.com"], dunbar_circle=4),
              person("R", name="Real Person", emails=["real@gmail.com"], message_count=3)]
    client = FakeClient(existing)
    run(client, people, tmp_path, execute=True)
    assert [c[0] for c in client.creates] == ["Real Person"]


# ---------------------------------------------------------------------------
# Every ledger intent line is fsynced before its request
# ---------------------------------------------------------------------------

def test_every_ledger_line_is_fsynced_before_the_request(monkeypatch, tmp_path):
    calls = []
    real_fsync = os.fsync

    def counting(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(sync.os, "fsync", counting)
    checks = []

    class Checking(FakeClient):
        def update_page(self, page_id, properties):
            checks.append((len(ledger_lines(tmp_path)), len(calls)))
            return super().update_page(page_id, properties)

        def create_page(self, name, lifeos_id, template_id="", timezone_name=None):
            checks.append((len(ledger_lines(tmp_path)), len(calls)))
            return super().create_page(name, lifeos_id, template_id, timezone_name)

    write_dexmap(tmp_path, {"d1": "L1"})
    pages = [page("p1", dex="d1", email="old@example.com")]
    people = [person("L1", emails=["new@example.com"]), new_people()[1]]
    run(Checking(pages), people, tmp_path, execute=True)
    assert len(checks) >= 3
    for lines_on_disk, fsyncs in checks:
        assert lines_on_disk > 0 and fsyncs >= lines_on_disk  # one fsync per line, none skipped


# ---------------------------------------------------------------------------
# Nightly wrapper and units
# ---------------------------------------------------------------------------

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _wrapper_env(tmp_path, token):
    env = {k: v for k, v in os.environ.items() if k != "LIFEOS_NOTION_TOKEN"}
    env["LIFEOS_NOTION_TOKEN"] = token
    env["LIFEOS_LOG_DIR"] = str(tmp_path / "logs")
    env["LIFEOS_VENV_PYTHON"] = sys.executable
    return env


@needs_bash
def test_nightly_wrapper_skips_cleanly_without_token(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "notion_people_sync_nightly.sh")],
        env=_wrapper_env(tmp_path, ""), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "logs" / "notion-people-sync.log").read_text()
    assert "LIFEOS_NOTION_TOKEN not set; skipping" in log
    assert "start" not in log


@needs_bash
def test_nightly_wrapper_broken_python_is_an_error_not_a_skip(tmp_path):
    env = _wrapper_env(tmp_path, "")
    env["LIFEOS_VENV_PYTHON"] = str(tmp_path / "no-such-python")
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "notion_people_sync_nightly.sh")],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    log = (tmp_path / "logs" / "notion-people-sync.log").read_text()
    assert "ERROR: could not check LIFEOS_NOTION_TOKEN" in log and "skipping" not in log


@needs_bash
def test_nightly_wrapper_unexpected_check_output_is_an_error(tmp_path):
    stub = tmp_path / "noisy-python"
    stub.write_text('#!/usr/bin/env bash\necho "maybe"\n')
    stub.chmod(0o755)
    env = _wrapper_env(tmp_path, "")
    env["LIFEOS_VENV_PYTHON"] = str(stub)
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "notion_people_sync_nightly.sh")],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "skipping" not in (tmp_path / "logs" / "notion-people-sync.log").read_text()


@needs_bash
def test_nightly_wrapper_runs_execute_with_caps_and_propagates_status(tmp_path):
    stub = tmp_path / "stub-python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-c" ]; then echo 1; exit 0; fi\n'
        'echo "ARGS: $*"\n'
        'exit "${STUB_RC:-0}"\n'
    )
    stub.chmod(0o755)
    env = _wrapper_env(tmp_path, TOKEN)
    env["LIFEOS_VENV_PYTHON"] = str(stub)
    script = str(REPO / "scripts" / "notion_people_sync_nightly.sh")

    ok = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0
    log = (tmp_path / "logs" / "notion-people-sync.log").read_text()
    assert "sync_notion_people.py --execute --max-writes 100 --max-creates 25" in log and "done" in log
    assert TOKEN not in log

    env["STUB_RC"] = "3"
    bad = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=60)
    assert bad.returncode == 3
    assert "ERROR: sync exited 3" in (tmp_path / "logs" / "notion-people-sync.log").read_text()


def test_systemd_units_exist_and_schedule_0415_persistent():
    timer = (REPO / "config" / "systemd" / "lifeos-notion-people-sync.timer").read_text()
    service = (REPO / "config" / "systemd" / "lifeos-notion-people-sync.service").read_text()
    assert "OnCalendar=*-*-* 04:15:00" in timer and "Persistent=true" in timer
    assert "Type=oneshot" in service
    assert "scripts/notion_people_sync_nightly.sh" in service


def test_settings_defaults_for_the_data_source_template_and_version():
    from config.settings import Settings

    s = Settings(_env_file=None)
    assert s.notion_people_data_source_id == "c841f807-b4d6-824f-8105-87105c118db7"
    assert s.notion_person_template_id == "4141f807-b4d6-8093-8836-ce6c9bbd4417"
    assert s.notion_version == "2025-09-03"
