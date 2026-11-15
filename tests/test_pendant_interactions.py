"""Pendant conversations reconciled into in_person interactions.

Drives api/services/pendant_interactions.py and
scripts/sync_pendant_interactions.py with a synthetic feed and a temp
interactions database.
"""
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.services import pendant_feed, pendant_interactions as pi
from api.services.interaction_store import InteractionStore, get_last_real_interactions
from config.settings import settings

pytestmark = pytest.mark.unit

DAY = "2025-11-08"
DAY_START = datetime(2025, 11, 8, 14, 0, tzinfo=timezone.utc)
RULES = pi.Rules(margin=0.05, min_speech_seconds=60, note_path_template="Pendant/Raw/{day}.md")
OWNER = "person-owner"
P, Q, R = "person-alex", "person-blair", "person-casey"


class People:
    """Person store double: a merge map plus existing (optionally hidden) people."""

    def __init__(self, ids=(P, Q, R, OWNER), merged=None, hidden=()):
        self.ids = set(ids)
        self.merged = dict(merged or {})
        self.hidden = set(hidden)

    def get_canonical_id(self, pid):
        seen = set()
        while pid in self.merged and pid not in seen:
            seen.add(pid)
            pid = self.merged[pid]
        return pid

    def get_by_id(self, pid):
        canon = self.get_canonical_id(pid)
        if canon not in self.ids:
            return None
        return SimpleNamespace(id=canon, hidden=canon in self.hidden)


def ms(dt):
    return int(dt.timestamp() * 1000)


def speaker(entity_id, labeled_by="user", speech_s=120.0, score=None, wearer=False, label_kind="person"):
    return {
        "entity_id": entity_id, "name": "Synthetic Name", "labeled_by": labeled_by,
        "label_kind": label_kind, "speech_s": speech_s, "score": score, "wearer": wearer,
    }


def conv(offset_min=0, minutes=15, speakers=(), day=DAY, kind="conversation", event=None, notes=()):
    start = DAY_START + timedelta(minutes=offset_min)
    return {
        "device": "dev-a", "start_ms": ms(start), "end_ms": ms(start + timedelta(minutes=minutes)),
        "day": day, "kind": kind, "calendar_event": event, "meeting_notes": list(notes),
        "speakers": [speaker(OWNER, wearer=True), *speakers],
        "title": "Synthetic secret topic", "overview": "Synthetic overview text",
    }


def event(offset_min=0, attendees=(P,)):
    start = DAY_START + timedelta(minutes=offset_min)
    return {
        "id": "evt-synthetic", "start_ms": ms(start), "end_ms": ms(start + timedelta(minutes=30)),
        "inherited": False,
        "attendees": [{"entity_id": a, "wearer": False} for a in attendees],
    }


class Feed:
    """Fake feed: conversations per request, or an exception to raise."""

    def __init__(self, conversations=(), threshold=0.55, fail=None):
        self.conversations = list(conversations)
        self.threshold = threshold
        self.fail = fail
        self.calls = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        if self.fail and self.fail(start, end):
            raise pendant_feed.PendantFeedError("unreachable (ConnectionRefusedError)")
        days = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
        return {
            "from": start.isoformat(), "to": end.isoformat(), "scoring": "mean",
            "match_threshold": self.threshold,
            "conversations": [c for c in self.conversations if c["day"] in days],
        }


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "interactions.db")
    InteractionStore(path, strict=False)
    return path


def add_row(db, source_type, person_id, ts, source_id, provider=None, title="Synthetic"):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_link, source_id, "
            "created_at, provider) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)",
            (f"row-{source_id}", person_id, ts, source_type, title, source_id,
             datetime.now(timezone.utc).isoformat(), provider),
        )


def rows(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return {
            r["source_id"]: dict(r)
            for r in conn.execute("SELECT * FROM interactions WHERE source_type = 'in_person' ORDER BY source_id")
        }


def run(db, feed, people=None, start=date(2025, 11, 8), end=date(2025, 11, 8), dry_run=False):
    return pi.reconcile(
        start, end, db_path=db, person_store=people or People(), rules=RULES,
        owner_id=OWNER, fetch=feed, dry_run=dry_run,
    )


def sid(pid, day=DAY):
    return f"pendant:{day}:{pid}"


# ---------------------------------------------------------------- rows


def test_confirmed_speaker_without_event_writes_one_row(db):
    result = run(db, Feed([conv(speakers=[speaker(P)])]))
    out = rows(db)
    assert list(out) == [sid(P)]
    row = out[sid(P)]
    assert row["person_id"] == P
    assert row["provider"] == "pendant"
    assert row["timestamp"] == DAY_START.isoformat()
    assert row["title"] == "In person · 1 conversation · 15 min"
    assert row["snippet"] is None
    assert row["attendee_count"] == 1
    assert row["source_link"].startswith("obsidian://open?vault=")
    assert "Pendant%2FRaw%2F2025-11-08" in row["source_link"]
    assert (result.created, result.updated, result.deleted, result.errors) == (1, 0, 0, 0)


def test_three_conversations_give_one_row_timed_at_the_last(db):
    feed = Feed([
        conv(0, 10, [speaker(P)]),
        conv(120, 20, [speaker(P)]),
        conv(300, 17, [speaker(P)]),
    ])
    run(db, feed)
    row = rows(db)[sid(P)]
    assert row["title"] == "In person · 3 conversations · 47 min"
    assert row["timestamp"] == (DAY_START + timedelta(minutes=300)).isoformat()


def test_attendee_count_is_largest_counted_conversation(db):
    run(db, Feed([conv(0, 10, [speaker(P), speaker(Q), speaker(R)]), conv(60, 10, [speaker(P)])]))
    out = rows(db)
    assert out[sid(P)]["attendee_count"] == 3
    assert out[sid(Q)]["attendee_count"] == 3


# ---------------------------------------------------------------- double counting


def test_calendar_row_at_event_start_drops_the_person(db):
    # Stored with a local offset, as calendar rows are: same instant as the event start.
    local = (DAY_START + timedelta(minutes=1)).astimezone(timezone(timedelta(hours=-5)))
    add_row(db, "calendar", P, local.isoformat(), "evt-synthetic:alex@example.com")
    result = run(db, Feed([conv(speakers=[speaker(P), speaker(Q)], event=event(attendees=(P,)))]))
    out = rows(db)
    assert sid(P) not in out
    assert result.counters["dropped_calendar"] == 1
    # Q was not invited, so the calendar never recorded Q.
    assert out[sid(Q)]["attendee_count"] == 2


def test_calendar_rule_witness_rows_without_a_matching_calendar_row(db):
    add_row(db, "calendar", P, (DAY_START + timedelta(minutes=10)).isoformat(), "evt-other:alex@example.com")
    add_row(db, "calendar", Q, DAY_START.isoformat(), "evt-synthetic:blair@example.com")
    run(db, Feed([conv(speakers=[speaker(P)], event=event())]))
    assert sid(P) in rows(db)


def test_phone_row_during_conversation_drops_the_person(db):
    add_row(db, "phone", P, (DAY_START + timedelta(minutes=19)).isoformat(), "call-1")
    result = run(db, Feed([conv(minutes=15, speakers=[speaker(P)])]))
    assert sid(P) not in rows(db)
    assert result.counters["dropped_call"] == 1


def test_phone_rule_witness_call_outside_the_span(db):
    add_row(db, "phone", P, (DAY_START + timedelta(minutes=21)).isoformat(), "call-1")
    add_row(db, "phone", P, (DAY_START - timedelta(minutes=6)).isoformat(), "call-2")
    add_row(db, "phone", Q, (DAY_START + timedelta(minutes=5)).isoformat(), "call-3")
    run(db, Feed([conv(minutes=15, speakers=[speaker(P)])]))
    assert sid(P) in rows(db)


def test_phone_row_just_before_the_conversation_drops_the_person(db):
    add_row(db, "phone", P, (DAY_START - timedelta(minutes=4)).isoformat(), "call-1")
    run(db, Feed([conv(minutes=15, speakers=[speaker(P)])]))
    assert sid(P) not in rows(db)


def test_meeting_row_from_a_linked_note_drops_the_person(db):
    add_row(db, "meeting", P, "2025-11-08T00:00:00+00:00",
            "/synthetic/vault/Granola/2025-11-08 Sync.md", provider="granola")
    result = run(db, Feed([conv(speakers=[speaker(P, labeled_by="meeting")],
                                notes=["Granola/2025-11-08 Sync.md"])]))
    assert sid(P) not in rows(db)
    assert result.counters["dropped_meeting"] == 1


def test_meeting_rule_witness_other_note_or_other_person(db):
    add_row(db, "meeting", P, "2025-11-08T00:00:00+00:00",
            "/synthetic/vault/Granola/2025-11-08 Other.md", provider="granola")
    add_row(db, "meeting", Q, "2025-11-08T00:00:00+00:00",
            "/synthetic/vault/Granola/2025-11-08 Sync.md", provider="granola")
    run(db, Feed([conv(speakers=[speaker(P)], notes=["Granola/2025-11-08 Sync.md"])]))
    assert sid(P) in rows(db)


def test_a_dropped_conversation_leaves_the_persons_other_conversations(db):
    add_row(db, "phone", P, DAY_START.isoformat(), "call-1")
    run(db, Feed([conv(0, 15, [speaker(P)]), conv(240, 5, [speaker(P)])]))
    row = rows(db)[sid(P)]
    assert row["title"] == "In person · 1 conversation · 5 min"


# ---------------------------------------------------------------- who counts


def test_auto_match_needs_score_floor_and_day_seconds(db):
    feed = Feed([
        conv(0, 10, [speaker(P, "match", speech_s=300, score=0.59)]),  # below 0.55 + 0.05
        conv(30, 10, [speaker(Q, "match", speech_s=40, score=0.9)]),   # under 60 s that day
        conv(60, 10, [speaker(R, "match", speech_s=40, score=0.6)]),   # floor exactly, 80 s over two
        conv(90, 10, [speaker(R, "match", speech_s=40, score=0.8)]),
    ])
    result = run(db, feed)
    assert list(rows(db)) == [sid(R)]
    assert rows(db)[sid(R)]["title"] == "In person · 2 conversations · 20 min"
    assert result.counters["match_below_floor"] == 1
    assert result.counters["match_below_seconds"] == 1


def test_confirmed_speaker_counts_where_the_auto_match_would_not(db):
    run(db, Feed([conv(0, 10, [speaker(P, "user", speech_s=5)]),
                  conv(30, 10, [speaker(Q, "match", speech_s=5, score=0.4)])]))
    assert list(rows(db)) == [sid(P)]


def test_never_counted_speakers_and_media(db):
    feed = Feed([
        conv(0, 10, [
            speaker(None, "user"),
            speaker(P, "user", label_kind="stranger"),
            speaker(P, "user", label_kind="noise"),
            speaker(P, "dismissed", label_kind="person"),
            speaker(Q, "user", wearer=True),
            speaker(OWNER, "user"),
            speaker("person-unknown", "user"),
        ]),
        conv(60, 30, [speaker(R)], kind="media"),
    ])
    result = run(db, feed)
    assert rows(db) == {}
    assert result.counters["media_skipped"] == 1


def test_empty_label_kind_is_a_person_label(db):
    run(db, Feed([conv(0, 10, [speaker(P, "user", label_kind=None)]),
                  conv(30, 10, [speaker(Q, "match", speech_s=90, score=0.8, label_kind=None)])]))
    assert set(rows(db)) == {sid(P), sid(Q)}


def test_hidden_person_gets_no_row(db):
    run(db, Feed([conv(speakers=[speaker(P)])]), people=People(hidden={P}))
    assert rows(db) == {}


def test_merged_id_resolves_to_survivor(db):
    people = People(ids=(P, Q, OWNER), merged={"person-legacy": P})
    run(db, Feed([conv(speakers=[speaker("person-legacy")])]), people=people)
    assert list(rows(db)) == [sid(P)]
    assert rows(db)[sid(P)]["person_id"] == P


def test_merged_owner_id_counts_as_wearer(db):
    people = People(ids=(P, OWNER), merged={"person-owner-old": OWNER})
    run(db, Feed([conv(speakers=[speaker("person-owner-old")])]), people=people)
    assert rows(db) == {}


def test_dedup_rows_under_a_merged_id_cover_the_survivor(db):
    people = People(ids=(P, OWNER), merged={"person-legacy": P})
    add_row(db, "phone", "person-legacy", DAY_START.isoformat(), "call-1")
    run(db, Feed([conv(speakers=[speaker(P)])]), people=people)
    assert rows(db) == {}


# ---------------------------------------------------------------- reconcile


def test_relabel_moves_the_row(db):
    run(db, Feed([conv(speakers=[speaker(P)])]))
    assert list(rows(db)) == [sid(P)]
    result = run(db, Feed([conv(speakers=[speaker(R)])]))
    assert list(rows(db)) == [sid(R)]
    assert (result.created, result.deleted) == (1, 1)
    assert result.affected_person_ids == {P, R}


def test_rerun_is_idempotent(db):
    feed = Feed([conv(0, 10, [speaker(P), speaker(Q)]), conv(60, 10, [speaker(P)])])
    run(db, feed)
    first = rows(db)
    result = run(db, feed)
    assert rows(db) == first
    assert (result.created, result.updated, result.deleted) == (0, 0, 0)


def test_changed_day_updates_in_place(db):
    run(db, Feed([conv(0, 10, [speaker(P)])]))
    row_id = rows(db)[sid(P)]["id"]
    result = run(db, Feed([conv(0, 10, [speaker(P)]), conv(60, 10, [speaker(P)])]))
    row = rows(db)[sid(P)]
    assert row["id"] == row_id
    assert row["title"] == "In person · 2 conversations · 20 min"
    assert result.updated == 1


def test_deletes_only_pendant_in_person_rows_inside_the_window(db):
    add_row(db, "in_person", P, "2025-11-01T12:00:00+00:00", sid(P, "2025-11-01"), provider="pendant")
    add_row(db, "in_person", Q, DAY_START.isoformat(), "other-device:2025-11-08", provider="other")
    add_row(db, "in_person", R, DAY_START.isoformat(), sid(R), provider="pendant")
    add_row(db, "calendar", P, DAY_START.isoformat(), "evt-x:alex@example.com")
    result = run(db, Feed([]))
    out = rows(db)
    assert sid(R) not in out
    assert sid(P, "2025-11-01") in out
    assert "other-device:2025-11-08" in out
    assert result.deleted == 1


def test_feed_failure_leaves_rows_untouched(db):
    add_row(db, "in_person", P, DAY_START.isoformat(), sid(P), provider="pendant", title="In person · 1 conversation · 5 min")
    before = rows(db)
    result = run(db, Feed([conv(speakers=[speaker(R)])], fail=lambda s, e: True))
    assert rows(db) == before
    assert result.errors == 1
    assert result.days_reconciled == 0


def test_failed_chunk_only_skips_its_days(db):
    late_day = "2025-12-15"
    add_row(db, "in_person", Q, "2025-12-15T12:00:00+00:00", sid(Q, late_day), provider="pendant")
    feed = Feed(
        [conv(speakers=[speaker(P)]), conv(speakers=[speaker(R)], day=late_day)],
        fail=lambda s, e: s > date(2025, 12, 1),
    )
    result = run(db, feed, start=date(2025, 11, 8), end=date(2025, 12, 20))
    assert feed.calls == [(date(2025, 11, 8), date(2025, 12, 8)), (date(2025, 12, 9), date(2025, 12, 20))]
    out = rows(db)
    assert sid(P) in out
    assert sid(Q, late_day) in out and sid(R, late_day) not in out
    assert result.errors == 1


def test_bad_json_is_a_failure(db):
    add_row(db, "in_person", P, DAY_START.isoformat(), sid(P), provider="pendant")
    result = run(db, lambda s, e: {"conversations": "nope"})
    assert result.errors == 1 and sid(P) in rows(db)


def test_dry_run_writes_nothing(db):
    result = run(db, Feed([conv(speakers=[speaker(P)])]), dry_run=True)
    assert rows(db) == {}
    assert result.created == 1 and result.rows == 1 and result.people == 1


def test_rows_carry_no_conversation_content(db):
    run(db, Feed([conv(0, 10, [speaker(P), speaker(Q)], event=event(30), notes=["Granola/Secret.md"])]))
    for row in rows(db).values():
        assert re.fullmatch(r"In person · \d+ conversations? · \d+ min", row["title"])
        assert row["snippet"] is None
        blob = json.dumps(row)
        for text in ("Synthetic secret topic", "Synthetic overview", "Synthetic Name", "Secret", "evt-synthetic"):
            assert text not in blob


def test_in_person_row_sets_last_real_interaction(db):
    run(db, Feed([conv(30, 10, [speaker(P)])]))
    with sqlite3.connect(db) as conn:
        last = get_last_real_interactions(conn, [P], now=datetime(2025, 11, 9, tzinfo=timezone.utc))
    assert last[P] == DAY_START + timedelta(minutes=30)


def test_day_chunks_cap_at_31_days():
    chunks = pi.day_chunks(date(2025, 1, 1), date(2025, 3, 5))
    assert chunks[0] == (date(2025, 1, 1), date(2025, 1, 31))
    assert all((e - s).days < 31 for s, e in chunks)
    assert chunks[-1][1] == date(2025, 3, 5)
    assert pi.window_days(14, date(2025, 11, 14)) == (date(2025, 11, 1), date(2025, 11, 14))


# ---------------------------------------------------------------- script


@pytest.fixture
def script(db, monkeypatch):
    import scripts.sync_pendant_interactions as mod

    monkeypatch.setattr(mod, "get_interaction_store", lambda: SimpleNamespace(db_path=db))
    monkeypatch.setattr("api.services.person_entity.get_person_entity_store", lambda: People())
    monkeypatch.setattr(settings, "my_person_id", OWNER)
    monkeypatch.setattr(settings, "pendant_feed_token", "synthetic-token")
    refreshed = []
    monkeypatch.setattr("api.services.person_stats.refresh_person_stats", lambda ids: refreshed.append(ids))
    mod.refreshed = refreshed
    return mod


def stats_line(out):
    return json.loads(re.search(r"^SYNC_STATS:(\{.*\})$", out, re.M).group(1))


def test_unconfigured_feed_prints_skip(script, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(settings, "pendant_feed_token", "")
    monkeypatch.setattr(settings, "pendant_feed_token_file", str(tmp_path / "absent"))
    assert script.main([]) == 0
    assert re.search(r"^SYNC_SKIPPED:", capsys.readouterr().out, re.M)


def test_script_writes_and_refreshes_people(script, monkeypatch, db, capsys):
    monkeypatch.setattr(pi, "fetch_interactions", Feed([conv(speakers=[speaker(P)])]))
    assert script.main(["--days", "3"], today=date(2025, 11, 9)) == 0
    assert list(rows(db)) == [sid(P)]
    assert script.refreshed == [[P]]
    stats = stats_line(capsys.readouterr().out)
    assert stats == {"processed": 1, "created": 1, "updated": 0, "deleted": 0, "errors": 0, "interactions_created": 1}


def test_script_unreachable_feed_reports_error(script, monkeypatch, db, capsys):
    add_row(db, "in_person", P, DAY_START.isoformat(), sid(P), provider="pendant")
    monkeypatch.setattr(pi, "fetch_interactions", Feed(fail=lambda s, e: True))
    assert script.main([], today=date(2025, 11, 9)) == 1
    assert sid(P) in rows(db)
    assert stats_line(capsys.readouterr().out)["errors"] == 1
    assert script.refreshed == []


def test_script_dry_run_prints_counts_only(script, monkeypatch, db, capsys):
    monkeypatch.setattr(pi, "fetch_interactions", Feed([conv(speakers=[speaker(P)])]))
    assert script.main(["--dry-run", "--days", "2"], today=date(2025, 11, 9)) == 0
    out = capsys.readouterr().out
    assert rows(db) == {} and script.refreshed == []
    summary = json.loads(re.search(r"^PENDANT_INTERACTIONS:(\{.*\})$", out, re.M).group(1))
    assert summary["would_create"] == 1 and summary["people"] == 1
    assert "Synthetic Name" not in out and P not in out


def test_script_backfill_starts_at_the_feeds_first_day(script, monkeypatch, db):
    feed = Feed([conv(speakers=[speaker(P)])])
    monkeypatch.setattr(pi, "fetch_interactions", feed)
    monkeypatch.setattr(pi, "fetch_first_day", lambda: date(2025, 11, 8))
    assert script.main(["--backfill"], today=date(2026, 1, 20)) == 0
    assert feed.calls[0][0] == date(2025, 11, 8) and feed.calls[-1][1] == date(2026, 1, 20)
    assert sid(P) in rows(db)


def test_first_day_reads_the_day_list(monkeypatch):
    monkeypatch.setattr(pendant_feed, "get_json", lambda path, params=None, timeout=10: {
        "days": [{"date": "2025-11-10"}, {"date": "2025-11-08"}],
    })
    assert pi.fetch_first_day() == date(2025, 11, 8)


# ---------------------------------------------------------------- registration


def test_registered_in_phase_two_after_calendar_and_phone():
    from api.services.sync_health import SYNC_SOURCES
    from scripts.run_all_syncs import SYNC_ORDER, SYNC_SCRIPTS

    order = SYNC_ORDER.index("pendant_interactions")
    for upstream in ("calendar_personal", "calendar_work", "apple_import"):
        assert SYNC_ORDER.index(upstream) < order
    assert order < SYNC_ORDER.index("person_stats_full")
    assert SYNC_SCRIPTS["pendant_interactions"][0] == "scripts/sync_pendant_interactions.py"
    assert SYNC_SOURCES["pendant_interactions"]["phase"] == 2


def test_unconfigured_feed_disables_the_source(monkeypatch, tmp_path):
    from api.services.sync_health import _is_source_disabled
    from scripts.run_all_syncs import get_disabled_work_sources

    monkeypatch.setattr(settings, "pendant_feed_token", "")
    monkeypatch.setattr(settings, "pendant_feed_token_file", str(tmp_path / "absent"))
    assert "pendant_interactions" in get_disabled_work_sources()
    assert _is_source_disabled("pendant_interactions")
    monkeypatch.setattr(settings, "pendant_feed_token", "synthetic-token")
    assert "pendant_interactions" not in get_disabled_work_sources()
    assert not _is_source_disabled("pendant_interactions")
