"""Likely-present suggestions: ranking signals, exclusions, opt-in message reading.

Uses a temp interactions database, a person-store double and a fake remote
client; nothing touches real data or the network.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import crm
from api.services import likely_present as lp
from api.services.interaction_store import InteractionStore
from api.services.sqlite_connect import connect_closing
from config.settings import settings

pytestmark = pytest.mark.unit

START = datetime(2025, 11, 12, 18, 0, tzinfo=timezone.utc)  # a Wednesday evening
END = START + timedelta(hours=1)
OWNER = "person-owner"
ALEX, BLAIR, CASEY, DREW = "person-alex", "person-blair", "person-casey", "person-drew"
HIDDEN, SERVICE = "person-hidden", "person-service"
SECRET = "SYNTHETIC-SECRET-BODY-4242"


class People:
    def __init__(self, strengths=None, hidden=(HIDDEN, SERVICE), merged=None):
        names = {
            OWNER: "Owner Person", ALEX: "Alex Rivera", BLAIR: "Blair Quinn",
            CASEY: "Casey Lin", DREW: "Drew Park", HIDDEN: "Hidden Person",
            SERVICE: "Service Sender",
        }
        strengths = strengths or {}
        self.entities = {
            pid: SimpleNamespace(
                id=pid, canonical_name=name, hidden=pid in hidden,
                relationship_strength=strengths.get(pid, 0.0),
            )
            for pid, name in names.items()
        }
        self.merged = dict(merged or {})

    def get_canonical_id(self, pid):
        while pid in self.merged:
            pid = self.merged[pid]
        return pid

    def get_by_ids(self, ids, exclude_hidden=False):
        out = {}
        for i in ids:
            e = self.entities.get(self.get_canonical_id(i))
            if e is not None and not (exclude_hidden and e.hidden):
                out[i] = e
        return out

    def get_all(self, include_hidden=False, include_merged=False):
        return [e for e in self.entities.values() if include_hidden or not e.hidden]


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    db = tmp_path / "interactions.db"
    InteractionStore(str(db), strict=False)
    monkeypatch.setattr(lp, "get_interaction_db_path", lambda: str(db))
    monkeypatch.setattr(settings, "my_person_id", OWNER)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "likely_present_read_messages", False)
    monkeypatch.setattr(settings, "likely_present_llm_timeout", 8.0)
    monkeypatch.setattr(settings, "remote_llm_base_url", "")
    people = People()
    monkeypatch.setattr("api.services.person_entity.get_person_entity_store", lambda *a, **k: people)
    events: list = []
    real_events_near = lp._events_near
    monkeypatch.setattr(lp, "_events_near", lambda start, end: list(events))   # no real calendar
    lp._index_cache.clear()
    yield SimpleNamespace(db=str(db), people=people, events=events, real_events_near=real_events_near)
    lp._index_cache.clear()
    time.tzset()


def add(env, pid, source_type, when, snippet=None, attendees=None, n=[0]):
    n[0] += 1
    with connect_closing(env.db) as conn:
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_id, attendee_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"i{n[0]}", pid, when.isoformat(), source_type, f"title {SECRET}", snippet, f"s{n[0]}", attendees),
        )


def run(**kw):
    return lp.likely_present(START, END, **kw)


def by_id(result):
    return {p["person_id"]: p for p in result}


def test_recent_message_names_channel_and_gap(env):
    add(env, ALEX, "imessage", START - timedelta(minutes=20))
    got = by_id(run())
    assert [r for r in got[ALEX]["reasons"] if "iMessage" in r and "20 min before" in r]
    assert 0 < got[ALEX]["score"] <= 1


def test_message_signal_decays_with_gap(env):
    add(env, ALEX, "imessage", START - timedelta(minutes=10))
    add(env, BLAIR, "imessage", START - timedelta(hours=5))
    got = by_id(run())
    assert got[ALEX]["score"] > got[BLAIR]["score"]
    add(env, CASEY, "imessage", START - timedelta(hours=7))
    assert CASEY not in by_id(run())


def test_calendar_invitee_outranks_strength_only(env):
    env.people.entities[BLAIR].relationship_strength = 90.0
    add(env, ALEX, "calendar", START + timedelta(minutes=15), attendees=2)
    result = run()
    order = [p["person_id"] for p in result]
    assert order.index(ALEX) < order.index(BLAIR)
    assert any("invited to an event at 18:15" in r for r in by_id(result)[ALEX]["reasons"])


def test_mass_meeting_calendar_rows_ignored(env):
    add(env, ALEX, "calendar", START, attendees=500)
    got = by_id(run(limit=10))
    assert not any(r.startswith("invited") for r in got.get(ALEX, {}).get("reasons", []))


def test_recurring_in_person_days(env):
    for day in (3, 4, 5):
        add(env, ALEX, "in_person", datetime(2025, 11, day, 18, 30, tzinfo=timezone.utc))
    add(env, BLAIR, "in_person", datetime(2025, 11, 3, 18, 30, tzinfo=timezone.utc))
    add(env, CASEY, "in_person", datetime(2025, 11, 8, 18, 30, tzinfo=timezone.utc))  # weekend: other day type
    got = by_id(run(limit=10))
    assert any("weekday evenings" in r for r in got[ALEX]["reasons"])
    assert not got.get(BLAIR, {}).get("reasons")
    assert CASEY not in got


def test_recurring_ignores_other_time_band(env):
    for day in (3, 4, 5):
        add(env, ALEX, "in_person", datetime(2025, 11, day, 8, 0, tzinfo=timezone.utc))
    assert ALEX not in by_id(run(limit=10))


def test_recurring_ignores_rows_after_the_window(env):
    for day in (19, 20, 21):  # later weekday evenings, none before start
        add(env, ALEX, "in_person", datetime(2025, 11, day, 18, 30, tzinfo=timezone.utc))
    assert ALEX not in by_id(run(limit=10))


def test_recurring_ignores_rows_older_than_the_lookback(env):
    for weeks in (30, 31, 32):  # same weekday evening, over 180 days back
        add(env, ALEX, "in_person", datetime(2025, 11, 12, 18, 30, tzinfo=timezone.utc) - timedelta(weeks=weeks))
    assert ALEX not in by_id(run(limit=10))


def test_recurring_uses_configured_timezone_for_bands(env, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    start = datetime(2025, 11, 12, 23, 0, tzinfo=timezone.utc)  # 18:00 in New York, "night" in UTC
    for day in (4, 5, 6):  # 19:00 New York the evening before
        add(env, ALEX, "in_person", datetime(2025, 11, day, 0, 0, tzinfo=timezone.utc))
        add(env, BLAIR, "in_person", datetime(2025, 11, day, 4, 30, tzinfo=timezone.utc))  # 23:30 New York
    got = by_id(lp.likely_present(start, start + timedelta(hours=1), limit=10))
    assert any("weekday evenings" in r for r in got[ALEX]["reasons"])
    assert BLAIR not in got


def test_calendar_label_uses_configured_timezone(env, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    start = datetime(2025, 11, 12, 23, 0, tzinfo=timezone.utc)
    add(env, ALEX, "calendar", datetime(2025, 11, 12, 23, 15, tzinfo=timezone.utc), attendees=2)
    got = by_id(lp.likely_present(start, start + timedelta(hours=1), limit=10))
    assert "invited to an event at 18:15" in got[ALEX]["reasons"]


def test_owner_hidden_and_merged_owner_never_returned(env):
    env.people.merged["person-owner-dup"] = OWNER
    for pid in (OWNER, HIDDEN, SERVICE, "person-owner-dup", "person-ghost"):
        add(env, pid, "imessage", START - timedelta(minutes=5))
    add(env, ALEX, "imessage", START - timedelta(minutes=5))
    ids = {p["person_id"] for p in run(limit=10)}
    assert ALEX in ids
    assert not ids & {OWNER, HIDDEN, SERVICE, "person-owner-dup", "person-ghost"}


def test_strength_breaks_ties(env):
    env.people.entities[ALEX].relationship_strength = 80.0
    env.people.entities[BLAIR].relationship_strength = 20.0
    add(env, ALEX, "imessage", START - timedelta(minutes=30))
    add(env, BLAIR, "imessage", START - timedelta(minutes=30))
    order = [p["person_id"] for p in run()]
    assert order.index(ALEX) < order.index(BLAIR)


def test_limit_and_ordering(env):
    for i, pid in enumerate((ALEX, BLAIR, CASEY, DREW)):
        add(env, pid, "imessage", START - timedelta(minutes=10 + 40 * i))
    result = run(limit=3)
    assert len(result) == 3
    scores = [p["score"] for p in result]
    assert scores == sorted(scores, reverse=True)


def test_response_has_no_row_text(env):
    add(env, ALEX, "imessage", START - timedelta(minutes=20), snippet=SECRET)
    add(env, BLAIR, "calendar", START, attendees=2)
    add(env, CASEY, "gmail", START - timedelta(hours=1), snippet=SECRET)
    blob = json.dumps(run(limit=10))
    assert SECRET not in blob and "title" not in blob


# --- HTTP surface ------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(crm.router)
    return TestClient(app)


def _q(start=START, end=END, **extra):
    return {"start": start.isoformat(), "end": end.isoformat(), **extra}


def test_endpoint_shape(env, client):
    add(env, ALEX, "imessage", START - timedelta(minutes=25))
    r = client.get("/api/crm/people/likely-present", params=_q())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"start", "end", "people"}
    assert set(body["people"][0]) == {"person_id", "name", "score", "reasons"}
    assert body["people"][0]["name"] == "Alex Rivera"
    assert SECRET not in r.text


@pytest.mark.parametrize("params", [
    {"start": "2025-11-12T19:00:00+00:00", "end": "2025-11-12T18:00:00+00:00"},
    {"start": "2025-11-12T00:00:00+00:00", "end": "2025-11-12T12:00:01+00:00"},
    {"start": "not-a-date", "end": "2025-11-12T18:00:00+00:00"},
    {"start": "2025-11-12T18:00:00+00:00", "end": "nope"},
])
def test_endpoint_rejects_bad_windows(env, client, params):
    assert client.get("/api/crm/people/likely-present", params=params).status_code == 400


def test_endpoint_limit_bounds(env, client):
    assert client.get("/api/crm/people/likely-present", params=_q(limit=11)).status_code == 422
    assert client.get("/api/crm/people/likely-present", params=_q(limit=0)).status_code == 422


def test_twelve_hour_window_allowed(env, client):
    r = client.get("/api/crm/people/likely-present", params=_q(end=START + timedelta(hours=12)))
    assert r.status_code == 200


# --- Message reading ---------------------------------------------------------

class FakeRemote:
    def __init__(self, verdicts=None, error=None, delay=0.0):
        self.verdicts = verdicts or {}
        self.error = error
        self.delay = delay
        self.prompts = []

    def create(self, messages, **kwargs):
        text = messages[0]["content"]
        self.prompts.append(text)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        verdict = "no"
        for marker, v in self.verdicts.items():
            if marker in text:
                verdict = v
        return SimpleNamespace(text=json.dumps({"verdict": verdict}))


@pytest.fixture
def reading(monkeypatch, env):
    monkeypatch.setattr(settings, "likely_present_read_messages", True)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example/v1")
    monkeypatch.setattr(settings, "remote_llm_model", "synthetic-model")
    monkeypatch.setattr(settings, "remote_llm_api_key", "synthetic-key")
    monkeypatch.setattr("api.services.imessage.get_imessage_store", lambda *a, **k: SimpleNamespace(
        query_messages=lambda **kw: []))
    fake = FakeRemote()
    constructed = []

    def factory(timeout):
        constructed.append(timeout)
        return fake

    monkeypatch.setattr(lp, "_remote_client", factory)
    fake.constructed = constructed
    return fake


def _two_equal_candidates(env):
    add(env, ALEX, "whatsapp", START - timedelta(minutes=30), snippet=f"see you at 6 {SECRET}-alex")
    add(env, BLAIR, "whatsapp", START - timedelta(minutes=30), snippet=f"lol ok {SECRET}-blair")


def test_plans_verdict_boosts_and_adds_reason(env, reading):
    reading.verdicts = {"-alex": "yes"}
    _two_equal_candidates(env)
    result = run()
    assert [p["person_id"] for p in result][0] == ALEX
    assert "plans to meet in messages" in by_id(result)[ALEX]["reasons"]
    assert "plans to meet in messages" not in by_id(result)[BLAIR]["reasons"]
    assert SECRET not in json.dumps(result)


def test_maybe_verdict_boosts_less_than_yes(env, reading):
    reading.verdicts = {"-alex": "yes", "-blair": "maybe"}
    _two_equal_candidates(env)
    got = by_id(run())
    assert got[ALEX]["score"] > got[BLAIR]["score"] > 0
    assert "plans to meet in messages" in got[BLAIR]["reasons"]


def test_imessage_text_is_read_through_existing_store(env, reading, monkeypatch):
    add(env, ALEX, "imessage", START - timedelta(minutes=30))
    rec = SimpleNamespace(text="synthetic imessage-alex", timestamp=START - timedelta(minutes=30), is_from_me=False)
    monkeypatch.setattr("api.services.imessage.get_imessage_store", lambda *a, **k: SimpleNamespace(
        query_messages=lambda **kw: [rec]))
    reading.verdicts = {"imessage-alex": "yes"}
    assert "plans to meet in messages" in by_id(run())[ALEX]["reasons"]


def test_disabled_sends_nothing(env, reading, monkeypatch):
    monkeypatch.setattr(settings, "likely_present_read_messages", False)
    _two_equal_candidates(env)
    run()
    assert reading.constructed == [] and reading.prompts == []


def test_no_remote_provider_sends_nothing(env, reading, monkeypatch):
    monkeypatch.setattr(settings, "remote_llm_base_url", "")
    _two_equal_candidates(env)
    run()
    assert reading.constructed == [] and reading.prompts == []


def test_remote_error_falls_back_to_metadata(env, reading):
    _two_equal_candidates(env)
    baseline = by_id(run())
    reading.error = RuntimeError("provider down")
    got = by_id(run())
    assert {k: v["score"] for k, v in got.items()} == {k: v["score"] for k, v in baseline.items()}
    assert all("plans to meet in messages" not in p["reasons"] for p in got.values())


def test_garbage_verdict_is_ignored(env, reading, monkeypatch):
    _two_equal_candidates(env)
    monkeypatch.setattr(reading, "create", lambda messages, **kw: SimpleNamespace(text="definitely!"))
    assert all("plans to meet in messages" not in p["reasons"] for p in run())


def test_remote_timeout_answers_within_budget(env, reading, monkeypatch):
    monkeypatch.setattr(settings, "likely_present_llm_timeout", 0.3)
    reading.delay = 3.0
    reading.verdicts = {"-alex": "yes"}
    _two_equal_candidates(env)
    t0 = time.monotonic()
    result = run()
    assert time.monotonic() - t0 < 1.5
    assert all("plans to meet in messages" not in p["reasons"] for p in result)
    assert len(result) == 2


def test_prompt_times_use_configured_timezone(env, reading, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    add(env, ALEX, "whatsapp", START - timedelta(minutes=30), snippet="synthetic")
    run()
    assert "2025-11-12 13:00 to 14:00" in reading.prompts[0]  # 18:00 UTC is 13:00 in New York


def test_remote_call_gets_the_configured_budget(env, reading):
    _two_equal_candidates(env)
    run()
    assert reading.constructed and set(reading.constructed) == {8.0}


def test_default_client_targets_the_configured_remote_provider(monkeypatch):
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example/v1")
    monkeypatch.setattr(settings, "remote_llm_model", "synthetic-model")
    monkeypatch.setattr(settings, "remote_llm_api_key", "synthetic-key")
    client = lp._remote_client(4.0)
    assert type(client).__name__ == "LocalLLMClient"
    assert client.base_url == "https://remote.example"
    assert client.model == "synthetic-model"
    assert client.timeout == 4.0


# --- Timing ------------------------------------------------------------------

def test_metadata_only_is_fast_over_50k_rows(env):
    types = ("imessage", "whatsapp", "gmail", "calendar", "phone", "in_person", "slack")
    base = START - timedelta(days=200)
    rows = []
    for i in range(50_000):
        when = base + timedelta(minutes=7 * i)
        rows.append((f"b{i}", f"person-bulk-{i % 800}", when.isoformat(), types[i % len(types)], "t", None, f"b{i}", None))
    with connect_closing(env.db) as conn:
        conn.executemany(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_id, attendee_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    add(env, ALEX, "imessage", START - timedelta(minutes=20))
    run()  # warm
    t0 = time.perf_counter()
    result = run()
    elapsed = time.perf_counter() - t0
    assert result and result[0]["person_id"] == ALEX
    assert elapsed < 0.3, f"{elapsed:.3f}s"


# --- People named in calendar event titles -----------------------------------

def event(env, title, start, minutes=60, attendees=0):
    env.events.append(lp._Event(title, start, start + timedelta(minutes=minutes), attendees))


def person(env, pid, name, strength=0.0, emails=("someone@example.com",)):
    env.people.entities[pid] = SimpleNamespace(id=pid, canonical_name=name, hidden=False,
                                               relationship_strength=strength, emails=list(emails))


def test_full_name_in_an_event_with_no_invitees(env):
    event(env, "Dinner with Alex Rivera", START - timedelta(hours=2, minutes=30), minutes=90)
    got = by_id(run())
    assert got[ALEX]["reasons"][0] == "named in an event at 15:30"
    assert got[ALEX]["score"] > 0


def test_title_text_never_reaches_the_result(env):
    event(env, f"Alex Rivera {SECRET}", START)
    assert SECRET not in json.dumps(run())


def named(result, pid):
    """Whether a title named `pid` (strong contacts may appear as close contacts anyway)."""
    p = by_id(result).get(pid)
    return bool(p and [r for r in p["reasons"] if r.startswith("named in")])


def test_first_name_only_for_one_close_contact(env):
    env.people.entities[ALEX].relationship_strength = 40.0
    env.people.entities[ALEX].emails = ["alex@example.com"]
    event(env, "Coffee w/ Alex", START)
    assert "named in an event at 18:00" in by_id(run())[ALEX]["reasons"]
    # lowercase is a word, not a name
    env.events.clear()
    event(env, "coffee w/ alex", START)
    assert not named(run(), ALEX)
    # two close contacts share it: nobody
    env.events.clear()
    lp._index_cache.clear()
    person(env, "person-alex2", "Alex Moreno", strength=35.0)
    event(env, "Coffee w/ Alex", START)
    got = run()
    assert not named(got, ALEX) and not named(got, "person-alex2")
    # the full name still settles it
    env.events.clear()
    event(env, "Coffee w/ Alex Moreno", START)
    assert named(run(), "person-alex2")


def test_first_name_of_a_group_or_a_common_word_is_not_enough(env):
    person(env, "group-all", "All Staff Example Co", strength=60.0, emails=())   # a group: nobody to reach
    person(env, "person-will", "Will Tanaka", strength=60.0)
    person(env, "person-team", "Team Okafor", strength=60.0)
    event(env, "All Staff", START)
    event(env, "Team offsite", START)
    event(env, "Will Tanaka & co", START)
    got = run()
    assert not named(got, "group-all") and not named(got, "person-team") and named(got, "person-will")


def test_a_shared_first_name_goes_to_a_much_closer_contact(env):
    person(env, "person-jo", "Joan Ramos", strength=95.0)
    person(env, "person-jo-dentist", "Joan Dentist Office", strength=30.0)
    event(env, "Joan recital rehearsal", START)
    got = run()
    assert named(got, "person-jo") and not named(got, "person-jo-dentist")
    assert lp._first_name_owner([(40.0, "a"), (35.0, "b")]) is None
    assert lp._first_name_owner([(40.0, "a"), (20.0, "b")]) == "a"


def test_first_name_of_a_distant_contact_is_not_enough(env):
    event(env, "Call Blair", START)
    assert BLAIR not in by_id(run())


def test_ordinary_titles_name_nobody(env):
    for pid in (ALEX, BLAIR, CASEY, DREW):
        env.people.entities[pid].relationship_strength = 40.0
    for t in ("Lunch", "Project Phoenix review", "Gym", "Focus time", "Park run", "Drive to airport"):
        event(env, t, START)
    got = run()
    assert not [p for p in got for r in p["reasons"] if r.startswith("named in")]


def test_title_events_keep_the_time_window_and_skip_mass_meetings(env):
    event(env, "Alex Rivera office hours", START + timedelta(hours=3))          # long after the window
    event(env, "Blair Quinn breakfast", START - timedelta(hours=5), minutes=30)  # long before
    event(env, "All hands with Casey Lin", START, attendees=400)               # a mass meeting
    got = by_id(run())
    assert not ({ALEX, BLAIR, CASEY} & set(got))


def test_named_during_a_long_event_counts_as_overlap(env):
    event(env, "Dinner with Alex Rivera", START - timedelta(hours=2), minutes=180)
    near = lp.CALENDAR_OVERLAP_WEIGHT
    got = by_id(run())
    assert got[ALEX]["reasons"] == ["named in an event at 16:00"] and got[ALEX]["score"] >= near - 1e-9


def test_invited_and_named_keeps_one_calendar_reason(env):
    add(env, ALEX, "calendar", START + timedelta(minutes=15), attendees=2)
    event(env, "Alex Rivera / Owner", START + timedelta(minutes=15), attendees=2)
    reasons = by_id(run())[ALEX]["reasons"]
    assert len([r for r in reasons if "event" in r]) == 1


def test_the_owner_is_never_named(env):
    event(env, "Owner Person and Alex Rivera", START)
    got = by_id(run())
    assert OWNER not in got and ALEX in got


def test_title_people_matches_aliases_and_possessives():
    index = ({("alex", "rivera"): {"a"}, ("sam", "lee"): {"s1", "s2"}}, {"drew": "d", "kim": None})
    assert lp.title_people("Alex Rivera's birthday", index) == ["a"]
    assert lp.title_people("Sam Lee sync", index) == []              # two people share the full name
    assert lp.title_people("Drew + Alex Rivera", index) == ["d", "a"]
    assert lp.title_people("Drewery visit", index) == []             # whole words only
    assert lp.title_people("Kim's recital", index) == []             # an ambiguous first name


def test_a_failing_calendar_leaves_the_other_signals(env, monkeypatch):
    monkeypatch.setattr(lp, "_events_near", env.real_events_near)
    import api.services.google_auth as ga
    monkeypatch.setattr(ga, "get_configured_accounts", lambda: ["personal"])

    def boom(account, day):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(lp, "_fetch_day", boom)
    lp._events_cache.clear()
    add(env, ALEX, "imessage", START - timedelta(minutes=20))
    got = by_id(lp.likely_present(START, END))
    assert ALEX in got and not [r for r in got[ALEX]["reasons"] if r.startswith("named in")]
