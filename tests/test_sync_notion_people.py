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

def page(pid, name="Ada Lovelace", dex="", email=None, phone=None, linkedin=None, checkin=None):
    return {
        "id": pid,
        "properties": {
            "Full Name": {"title": [{"plain_text": name}] if name else []},
            "Dex_ID": {"rich_text": [{"plain_text": dex}] if dex else []},
            "Email": {"email": email},
            "Phone": {"phone_number": phone},
            "LinkedIn": {"url": linkedin},
            "Last Check-In": {"date": {"start": checkin} if checkin else None},
        },
    }


def person(pid, name="Ada Lovelace", emails=(), phones=(), linkedin=None, last_seen=None):
    return {
        "id": pid,
        "display_name": name,
        "canonical_name": name.lower(),
        "emails": list(emails),
        "phone_numbers": list(phones),
        "linkedin_url": linkedin,
        "last_seen": last_seen,
    }


class FakeClient:
    """Stands in for NotionClient; applies updates to its pages like Notion."""

    def __init__(self, pages):
        self.pages = pages
        self.updates = []

    schema_error = None

    def close(self):
        pass

    def check_schema(self):
        if self.schema_error:
            raise sync.NotionError(self.schema_error)

    def query_rows(self, limit=None):
        return self.pages[:limit] if limit else list(self.pages)

    def update_page(self, page_id, properties):
        self.updates.append((page_id, properties))
        target = next(p for p in self.pages if p["id"] == page_id)
        for key, value in properties.items():
            target["properties"][key] = value
        return {}


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
    return {"map_path": tmp_path / "map.json", "dexmap_path": tmp_path / "dexmap.json"}


def run(client, people, tmp_path, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("tz", UTC)
    kw.setdefault("today", date(2026, 6, 1))
    return sync.run_sync(client, people, **{**paths(tmp_path), **kw})


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
                "Email": {"email": "ada@example.com"},
                "Phone": {"phone_number": "+12125550173"},
                "LinkedIn": {"url": "https://www.linkedin.com/in/ada/"},
                "Last Check-In": {"date": {"start": "2026-03-01"}},
            },
        }
    )
    assert row == sync.NotionRow(
        "p1", "Ada Lovelace", "dex-1", "ada@example.com", "+12125550173",
        "https://www.linkedin.com/in/ada/", "2026-03-01",
    )


def test_parse_row_datetime_checkin_uses_first_ten_chars():
    row = sync.parse_row(page("p", checkin="2026-03-01T10:00:00.000-05:00"))
    assert row.last_checkin == "2026-03-01"


def test_parse_row_empty_and_missing_values():
    row = sync.parse_row(page("p", name="", dex=""))
    assert (row.name, row.dex_id, row.email, row.phone, row.linkedin, row.last_checkin) == (
        "", "", None, None, None, None,
    )
    bare = sync.parse_row({"id": "q", "properties": {}})
    assert bare.page_id == "q" and bare.name == "" and bare.email is None


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


def test_resolve_name_mismatch_rejected_for_every_source():
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
    assert ok("Adrienne Szarmack", "Adrienne (Szarmack) Smith")   # parenthesised word as surname
    assert ok("Adrienne Smith", "Adrienne (Szarmack) Smith")      # parentheses removed
    assert not ok("Adrienne Jones", "Adrienne (Szarmack) Smith")  # still strict
    assert not ok("Jane Smith", "John (Jane) Jones")


def test_name_check_parenthesised_nickname_that_is_not_a_surname():
    ok = sync.names_compatible
    assert ok("Robert Lee", "Bob (Robert) Lee")   # parenthesised word replacing the first name
    assert ok("Bob Lee", "Bob (Robert) Lee")      # parentheses removed
    assert not ok("Robert Kim", "Bob (Robert) Lee")
    assert not ok("Robert Lee", "Bob Lee")        # no parentheses: unchanged


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
    assert [pid for pid, _ in client.updates] == ["p2"]
    assert stats["unmatched"] == 1
    assert "points at LifeOS A but the seed map sends its Dex_ID to A2" in caplog.text


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
# Change rules
# ---------------------------------------------------------------------------

def changes_for(row_page, p, **kw):
    return sync.compute_changes(sync.parse_row(row_page), p, now=NOW, tz=UTC, **kw)


def test_fills_empty_email_and_phone_with_first_values():
    p = person("A", emails=["first@example.com", "second@example.com"],
               phones=["+12125550173", "+13105550111"])
    change, conflicts = changes_for(page("p1"), p)
    assert change.properties == {
        "Email": {"email": "first@example.com"},
        "Phone": {"phone_number": "+12125550173"},
    }
    assert conflicts == []


def test_never_overwrites_filled_email_or_phone():
    p = person("A", emails=["known@example.com"], phones=["+12125550173"])
    change, conflicts = changes_for(page("p1", email="Known@Example.com", phone="212-555-0173"), p)
    assert change is None and conflicts == []


def test_email_conflict_when_notion_holds_unknown_address():
    p = person("A", emails=["known@example.com"])
    change, conflicts = changes_for(page("p1", email="other@example.com"), p)
    assert change is None
    assert len(conflicts) == 1 and conflicts[0].startswith("Email")


def test_phone_conflict_when_last_ten_digits_unknown():
    p = person("A", phones=["+12125550173"])
    change, conflicts = changes_for(page("p1", phone="+1 310 555 0111"), p)
    assert change is None
    assert len(conflicts) == 1 and conflicts[0].startswith("Phone")
    # same last ten digits with a different country prefix style is not a conflict
    assert changes_for(page("p1", phone="0012125550173"), p)[1] == []


def test_conflict_reported_when_lifeos_has_nothing():
    change, conflicts = changes_for(page("p1", email="x@example.com", phone="+12125550173"), person("A"))
    assert change is None and len(conflicts) == 2


def test_checkin_set_when_empty_or_older():
    p = person("A", last_seen="2026-05-20T15:00:00+00:00")
    empty, _ = changes_for(page("p1"), p)
    older, _ = changes_for(page("p1", checkin="2026-05-01"), p)
    for change in (empty, older):
        assert change.properties == {"Last Check-In": {"date": {"start": "2026-05-20"}}}
        assert change.fields == ["Last Check-In"]


def test_checkin_not_set_when_equal_or_newer_or_future():
    p = person("A", last_seen="2026-05-20T15:00:00+00:00")
    assert changes_for(page("p1", checkin="2026-05-20"), p)[0] is None
    assert changes_for(page("p1", checkin="2026-05-21T09:00:00.000+00:00"), p)[0] is None
    future = person("A", last_seen="2026-07-01T00:00:00+00:00")
    assert changes_for(page("p1"), future)[0] is None


def test_checkin_converts_to_the_local_date():
    p = person("A", last_seen="2026-03-02T03:00:00Z")
    west = timezone(timedelta(hours=-8))
    east = timezone(timedelta(hours=9))
    row = sync.parse_row(page("p1"))
    assert sync.compute_changes(row, p, now=NOW, tz=west)[0].properties["Last Check-In"] == {
        "date": {"start": "2026-03-01"}
    }
    assert sync.compute_changes(row, p, now=NOW, tz=east)[0].properties["Last Check-In"] == {
        "date": {"start": "2026-03-02"}
    }


def test_checkin_naive_timestamp_is_read_as_utc_and_garbage_is_ignored():
    west = timezone(timedelta(hours=-8))
    assert sync.last_seen_local_date("2026-03-02T03:00:00", now=NOW, tz=west) == date(2026, 3, 1)
    assert sync.last_seen_local_date("not a date", now=NOW, tz=UTC) is None
    assert sync.last_seen_local_date(None, now=NOW, tz=UTC) is None


def test_only_the_three_properties_are_ever_written():
    p = person("A", emails=["a@example.com"], phones=["+12125550173"],
               linkedin="https://linkedin.com/in/ada", last_seen="2026-05-20T00:00:00Z")
    change, _ = changes_for(page("p1"), p)
    assert set(change.properties) <= {"Email", "Phone", "Last Check-In"}


# ---------------------------------------------------------------------------
# Cap, abort, dry run, execute, idempotence
# ---------------------------------------------------------------------------

def many(n):
    names = ["Zed Zulu", "Amy Able", "Bob Baker", "Cy Cole", "Di Dunn", "Ed Eng"]
    pages, people = [], []
    for i in range(n):
        name = names[i]
        pages.append(page(f"p{i}", name=name, dex=f"d{i}"))
        people.append(person(f"L{i}", name=name, emails=[f"u{i}@example.com"]))
    dexmap = {f"d{i}": f"L{i}" for i in range(n)}
    return pages, people, dexmap


def write_dexmap(tmp_path, dexmap):
    (tmp_path / "dexmap.json").write_text(json.dumps({"version": 1, "dex_to_lifeos": dexmap}))


def test_apply_cap_orders_by_name_and_defers_the_rest():
    changes = [sync.Change(f"id{i}", n, f"L{i}", {}, []) for i, n in enumerate(["b", "A", "c"])]
    applied, deferred = sync.apply_cap(changes, 2)
    assert [c.name for c in applied] == ["A", "b"] and [c.name for c in deferred] == ["c"]
    assert sync.apply_cap(changes, 0) == ([], sorted(changes, key=lambda c: c.name.casefold()))


def test_run_applies_max_writes_cap_and_reports_deferred(tmp_path):
    pages, people, dexmap = many(5)
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    stats = run(client, people, tmp_path, execute=True, max_writes=2)
    assert stats["changes"] == 5 and stats["writes"] == 2 and stats["deferred"] == 3
    # name order: Amy Able (p1), Bob Baker (p2)
    assert [pid for pid, _ in client.updates] == ["p1", "p2"]


def test_abort_when_under_half_match_writes_nothing(tmp_path):
    pages, people, dexmap = many(3)
    # only one of the three rows can be matched
    write_dexmap(tmp_path, {"d0": "L0"})
    client = FakeClient(pages)
    with pytest.raises(sync.MappingAbort):
        run(client, people, tmp_path, execute=True)
    assert client.updates == []
    assert not (tmp_path / "map.json").exists()


def test_exactly_half_matched_does_not_abort(tmp_path):
    pages, people, _ = many(2)
    write_dexmap(tmp_path, {"d0": "L0"})
    stats = run(FakeClient(pages), people, tmp_path, execute=False)
    assert stats["matched"] == 1 and stats["unmatched"] == 1


def test_empty_database_is_not_an_abort(tmp_path):
    stats = run(FakeClient([]), [], tmp_path)
    assert stats["rows"] == 0 and stats["changes"] == 0


def test_dry_run_makes_no_updates_and_writes_no_map(tmp_path):
    pages, people, dexmap = many(3)
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    stats = run(client, people, tmp_path, execute=False)
    assert client.updates == []
    assert not (tmp_path / "map.json").exists()
    assert stats["mode"] == "dry-run" and stats["changes"] == 3 and stats["writes"] == 0


def test_execute_sends_expected_payloads_and_stores_new_mappings(tmp_path):
    pages = [page("p1", dex="d1")]
    people = [person("L1", emails=["ada@example.com"], phones=["+12125550173"],
                     last_seen="2026-05-20T15:00:00Z")]
    write_dexmap(tmp_path, {"d1": "L1"})
    client = FakeClient(pages)
    stats = run(client, people, tmp_path, execute=True)
    assert client.updates == [
        ("p1", {
            "Email": {"email": "ada@example.com"},
            "Phone": {"phone_number": "+12125550173"},
            "Last Check-In": {"date": {"start": "2026-05-20"}},
        })
    ]
    assert stats["writes"] == 1 and stats["new_mappings"] == 1
    stored = json.loads((tmp_path / "map.json").read_text())
    assert stored == {
        "version": 1,
        "pages": {"p1": {"lifeos_id": "L1", "dex_id": "d1", "matched_by": "dex_map",
                         "added": "2026-06-01"}},
    }
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_stored_map_is_used_and_kept_on_later_runs(tmp_path):
    pages = [page("p1")]  # no identifiers at all
    people = [person("L1", emails=["ada@example.com"])]
    sync.save_map_atomic(tmp_path / "map.json", {
        "p1": {"lifeos_id": "L1", "dex_id": "", "matched_by": "dex_map", "added": "2026-01-01"}
    })
    stats = run(FakeClient(pages), people, tmp_path, execute=True)
    assert stats["matched"] == 1 and stats["new_mappings"] == 0
    assert json.loads((tmp_path / "map.json").read_text())["pages"]["p1"]["added"] == "2026-01-01"


def test_second_run_finds_nothing(tmp_path):
    pages, people, dexmap = many(3)
    for p in people:
        p["phone_numbers"] = ["+12125550173"]
        p["last_seen"] = "2026-05-20T10:00:00Z"
    write_dexmap(tmp_path, dexmap)
    client = FakeClient(pages)
    first = run(client, people, tmp_path, execute=True)
    second = run(client, people, tmp_path, execute=True)
    assert first["writes"] == 3
    assert second["changes"] == 0 and second["writes"] == 0 and second["conflicts"] == 0


def test_unmatched_and_conflicts_are_counted_and_logged(tmp_path, caplog):
    pages = [
        page("p1", name="Ada Lovelace", dex="d1", email="other@example.com"),
        page("p2", name="Nobody Known"),
    ]
    people = [person("L1", emails=["ada@example.com"])]
    write_dexmap(tmp_path, {"d1": "L1"})
    with caplog.at_level(logging.INFO, logger="notion_people_sync"):
        stats = run(FakeClient(pages), people, tmp_path)
    assert stats["conflicts"] == 1 and stats["unmatched"] == 1
    text = caplog.text
    assert "CONFLICT Ada Lovelace" in text and "UNMATCHED Nobody Known" in text


def test_one_failed_update_does_not_stop_the_run(tmp_path):
    pages, people, dexmap = many(3)
    write_dexmap(tmp_path, dexmap)

    class Flaky(FakeClient):
        def update_page(self, page_id, properties):
            if page_id == "p1":
                raise sync.NotionError("HTTP 400", 400)
            return super().update_page(page_id, properties)

    stats = run(Flaky(pages), people, tmp_path, execute=True)
    assert stats["writes"] == 2 and stats["failed"] == 1


def test_auth_failure_during_update_aborts(tmp_path):
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)

    class Unauthorized(FakeClient):
        def update_page(self, page_id, properties):
            raise sync.NotionError("HTTP 401", 401)

    with pytest.raises(sync.NotionError):
        run(Unauthorized(pages), people, tmp_path, execute=True)


# ---------------------------------------------------------------------------
# NotionClient over httpx.MockTransport
# ---------------------------------------------------------------------------

def make_client(handler, sleeps=None, **kw):
    sleeps = [] if sleeps is None else sleeps
    kw.setdefault("pace", 0)
    return sync.NotionClient(
        TOKEN, "db123", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )


def test_query_follows_pagination():
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
    assert seen[0] == ("POST", "/v1/databases/db123/query", {"page_size": 100})
    assert seen[1][2] == {"page_size": 100, "start_cursor": "c1"}


def test_query_limit_truncates():
    def handler(request):
        return httpx.Response(200, json={"results": [{"id": str(i)} for i in range(100)],
                                         "has_more": True, "next_cursor": "more"})

    assert len(make_client(handler).query_rows(limit=5)) == 5


def test_requests_carry_auth_and_version_headers_and_patch_shape():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={})

    make_client(handler).update_page("page-1", {"Email": {"email": "a@example.com"}})
    req = captured[0]
    assert req.method == "PATCH" and req.url.path == "/v1/pages/page-1"
    assert json.loads(req.content) == {"properties": {"Email": {"email": "a@example.com"}}}
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.headers["Notion-Version"] == "2022-06-28"


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


@pytest.mark.parametrize("status,fragment", [
    (401, "rejected the token"),
    (403, "not shared with the integration"),
    (404, "not shared with the integration"),
])
def test_auth_and_sharing_errors_are_clear_and_not_retried(status, fragment):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"message": "nope"})

    with pytest.raises(sync.NotionError) as err:
        make_client(handler).query_rows()
    assert fragment in str(err.value) and err.value.status == status and len(calls) == 1


def test_requests_are_paced():
    sleeps = []

    def handler(request):
        return httpx.Response(200, json={})

    client = make_client(handler, sleeps, pace=0.35)
    client.update_page("a", {})
    client.update_page("b", {})
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 0.35


# ---------------------------------------------------------------------------
# Token secrecy
# ---------------------------------------------------------------------------

def test_error_messages_never_contain_the_token():
    def echo(request):
        return httpx.Response(400, text=f"bad header Bearer {TOKEN}")

    with pytest.raises(sync.NotionError) as err:
        make_client(echo).update_page("p", {})
    assert TOKEN not in str(err.value) and "***" in str(err.value)

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


def _main_with(monkeypatch, tmp_path, client, people, *extra):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    monkeypatch.setattr(sync, "NotionClient", lambda token, db: client)
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: people)
    return sync.main(["--no-file-log", "--map", str(tmp_path / "map.json"),
                      "--dexmap", str(tmp_path / "dexmap.json"), *extra])


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
    assert rc == 2 and stats["writes"] == 1 and stats["rows"] == 3
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
    assert rc == 1 and stats["failed"] == 2 and stats["status"] == "write_failures"


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


def test_non_json_200_is_a_clear_notion_error():
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(sync.NotionError) as err:
        make_client(handler).query_rows()
    assert "not JSON" in str(err.value)


def test_main_non_json_200_exits_2(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="oops"))
    real = sync.NotionClient
    monkeypatch.setattr(
        sync, "NotionClient",
        lambda token, db: real(token, db, transport=transport, pace=0),
    )
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: [])
    rc = sync.main(["--no-file-log", "--map", str(tmp_path / "m.json"),
                    "--dexmap", str(tmp_path / "d.json")])
    assert rc == 2 and _stats_line(capsys.readouterr().out)["exit_code"] == 2


def test_retry_on_409_conflict():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(409, json={}) if len(calls) == 1 else httpx.Response(200, json={})

    make_client(handler).update_page("p", {})
    assert len(calls) == 2


SCHEMA = {
    "Full Name": "title", "Dex_ID": "rich_text", "Email": "email",
    "Phone": "phone_number", "LinkedIn": "url", "Last Check-In": "date", "Other": "select",
}


def _schema_client(props):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"properties": {k: {"type": v} for k, v in props.items()}})

    return make_client(handler), seen


def test_check_schema_accepts_expected_types():
    client, seen = _schema_client(SCHEMA)
    client.check_schema()
    assert seen == [("GET", "/v1/databases/db123")]


def test_check_schema_linkedin_is_optional():
    props = {k: v for k, v in SCHEMA.items() if k != "LinkedIn"}
    _schema_client(props)[0].check_schema()


@pytest.mark.parametrize("name", ["Full Name", "Dex_ID", "Email", "Phone", "Last Check-In"])
def test_check_schema_missing_required_property(name):
    props = {k: v for k, v in SCHEMA.items() if k != name}
    with pytest.raises(sync.NotionError) as err:
        _schema_client(props)[0].check_schema()
    assert f"no property '{name}'" in str(err.value)


def test_check_schema_wrong_type_names_property_and_types():
    props = dict(SCHEMA, Email="rich_text", LinkedIn="rich_text")
    with pytest.raises(sync.NotionError) as err:
        _schema_client(props)[0].check_schema()
    assert "'Email' has type rich_text, expected email" in str(err.value)
    props = dict(SCHEMA, **{"Last Check-In": "created_time"})
    with pytest.raises(sync.NotionError) as err:
        _schema_client(props)[0].check_schema()
    assert "'Last Check-In' has type created_time, expected date" in str(err.value)


def test_run_sync_stops_before_querying_when_schema_is_wrong(tmp_path):
    class Bad(FakeClient):
        def query_rows(self, limit=None):
            raise AssertionError("must not query")

    bad = Bad([])
    bad.schema_error = "schema wrong"
    with pytest.raises(sync.NotionError):
        run(bad, [], tmp_path)


def test_fetch_people_refuses_a_truncated_list():
    for body in ({"people": [{"id": "A"}], "has_more": True},
                 {"people": [{"id": "A"}], "total": 5}):
        transport = httpx.MockTransport(lambda request, b=body: httpx.Response(200, json=b))
        with pytest.raises(sync.LifeOSUnavailable) as err:
            sync.fetch_people("http://127.0.0.1:8000", transport=transport)
        assert "truncated" in str(err.value)
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"people": [{"id": "A"}], "total": 1}))
    assert sync.fetch_people("http://127.0.0.1:8000", transport=ok) == [{"id": "A"}]


def test_main_dry_run_prints_stats_line(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    monkeypatch.setattr(settings, "notion_people_database_id", "dbX")
    pages, people, dexmap = many(2)
    write_dexmap(tmp_path, dexmap)
    fake = FakeClient(pages)
    monkeypatch.setattr(sync, "NotionClient", lambda token, db: fake)
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: people)
    rc = sync.main(["--no-file-log", "--map", str(tmp_path / "map.json"),
                    "--dexmap", str(tmp_path / "dexmap.json")])
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("SYNC_STATS:"))
    stats = json.loads(line[len("SYNC_STATS:"):])
    assert rc == 0 and stats["mode"] == "dry-run" and stats["changes"] == 2
    assert fake.updates == [] and not (tmp_path / "map.json").exists()


def test_main_exit_status_when_mapping_breaks(monkeypatch, capsys, tmp_path):
    from config.settings import settings

    monkeypatch.setattr(settings, "notion_token", TOKEN)
    pages, people, _ = many(3)
    fake = FakeClient(pages)
    monkeypatch.setattr(sync, "NotionClient", lambda token, db: fake)
    monkeypatch.setattr(sync, "fetch_people", lambda api_base, transport=None: people)
    rc = sync.main(["--execute", "--no-file-log", "--map", str(tmp_path / "map.json"),
                    "--dexmap", str(tmp_path / "missing.json")])
    assert rc == sync.EXIT_MAPPING_ABORT and fake.updates == []


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


# ---------------------------------------------------------------------------
# Nightly wrapper
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
def test_nightly_wrapper_runs_execute_with_cap_and_propagates_status(tmp_path):
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
    assert "sync_notion_people.py --execute --max-writes 50" in log and "done" in log
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
