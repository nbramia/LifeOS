"""
Tests for the journal trend views: the strip, the unexplored wheel,
felt-vs-recorded connection, and the scalar stack.

All fixtures use invented dates, names, and values — never the real
journal, the real CRM, or the real interactions database. View C's tests
build a temp interactions database and a fake entity resolver instead of
touching `data/`.
"""
import json
import sqlite3
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import journal_trends
from api.routes.journal_trends import (
    ConnectionResolution,
    _derive_taxonomy_labels,
    _grid_span,
    _interaction_counts_by_day,
    _numeric,
    _parse_bool_field,
)
from api.services.entity_resolver import ResolutionResult
from api.services.interaction_store import Interaction, InteractionStore
from api.services.person_entity import PersonEntity

pytestmark = pytest.mark.unit


def _write_entry(vault_dir, date_str: str, frontmatter: dict, body: str = "Body text.") -> None:
    journal_dir = vault_dir / "Personal" / "Journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body)
    (journal_dir / f"{date_str}.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def client_and_vault(tmp_path, monkeypatch):
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 6, 30)

    monkeypatch.setattr(journal_trends, "date", _FixedDate)
    vault = tmp_path / "vault"
    monkeypatch.setattr(journal_trends.settings, "vault_path", vault)
    monkeypatch.setattr(journal_trends, "get_gsheet_sync_db_path", lambda: str(tmp_path / "no-such-gsheet.db"))
    monkeypatch.setattr(journal_trends, "get_interaction_db_path", lambda: str(tmp_path / "no-such-interactions.db"))
    app = FastAPI()
    app.include_router(journal_trends.router)
    return TestClient(app), vault, tmp_path


# ---------------------------------------------------------------------------
# Shared helper: _grid_span
# ---------------------------------------------------------------------------

class TestGridSpan:
    def test_all_time_with_entries_starts_at_earliest(self):
        entries = [(date(2026, 1, 27), {}), (date(2026, 6, 22), {})]
        start, end = _grid_span("all-time", date(2026, 6, 30), entries)
        assert start == date(2026, 1, 27)
        assert end == date(2026, 6, 30)

    def test_all_time_with_no_entries_has_no_start(self):
        start, end = _grid_span("all-time", date(2026, 6, 30), [])
        assert start is None
        assert end == date(2026, 6, 30)

    def test_short_window_keeps_fixed_bounds_even_with_no_entries(self):
        # A "week" window's grid must not shrink just because it's empty.
        start, end = _grid_span("week", date(2026, 6, 30), [])
        assert start == date(2026, 6, 24)
        assert end == date(2026, 6, 30)


# ---------------------------------------------------------------------------
# View A: the strip
# ---------------------------------------------------------------------------

class TestStripEndpoint:
    def test_empty_vault_all_time_has_no_days(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/strip?window=all-time")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_entries"] == 0
        assert body["emotion_entries"] == 0
        assert body["days"] == []
        assert body["start_date"] is None

    def test_short_window_with_zero_entries_still_renders_gap_grid(self, client_and_vault):
        # "week" has fixed bounds regardless of data, so the grid still
        # exists — it's just all gaps.
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/strip?window=week")
        body = resp.json()
        assert len(body["days"]) == 7
        assert all(not d["has_entry"] for d in body["days"])

    def test_single_entry_n1(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        resp = client.get("/api/journal/strip?window=day")
        body = resp.json()
        assert len(body["days"]) == 1
        assert body["days"][0] == {"date": "2026-06-30", "has_entry": True, "primary_emotion": "Happy"}
        assert body["emotion_entries"] == 1

    def test_entry_with_no_feeling_is_a_distinct_state(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "mood": 5})
        resp = client.get("/api/journal/strip?window=day")
        body = resp.json()
        assert body["days"][0]["has_entry"] is True
        assert body["days"][0]["primary_emotion"] is None
        assert body["emotion_entries"] == 0

    def test_gap_spanning_weeks_renders_every_day_between(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "feeling": "Sad"})
        _write_entry(vault, "2026-06-22", {"date": "2026-06-22", "feeling": "Happy"})
        resp = client.get("/api/journal/strip?window=all-time")
        body = resp.json()
        assert body["start_date"] == "2026-01-27"
        assert body["end_date"] == "2026-06-30"  # "today", not the last entry
        by_date = {d["date"]: d for d in body["days"]}
        assert by_date["2026-01-27"]["primary_emotion"] == "Sad"
        assert by_date["2026-06-22"]["primary_emotion"] == "Happy"
        # A day squarely in the middle of the gap has no entry at all.
        assert by_date["2026-03-15"] == {"date": "2026-03-15", "has_entry": False, "primary_emotion": None}
        assert body["total_entries"] == 2
        assert body["emotion_entries"] == 2
        # Every calendar day from 2026-01-27 through 2026-06-30 is present.
        assert len(body["days"]) == (date(2026, 6, 30) - date(2026, 1, 27)).days + 1


# ---------------------------------------------------------------------------
# View B: the unexplored wheel
# ---------------------------------------------------------------------------

def _make_gsheet_db(path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE synced_rows (id INTEGER PRIMARY KEY, sheet_id TEXT, row_hash TEXT, "
        "entry_date TEXT, raw_data TEXT)"
    )
    for i, row in enumerate(rows):
        conn.execute(
            "INSERT INTO synced_rows (sheet_id, row_hash, entry_date, raw_data) VALUES (?, ?, ?, ?)",
            ("sheet1", f"h{i}", "2026-06-01", json.dumps(row)),
        )
    conn.commit()
    conn.close()


class TestDeriveTaxonomyLabels:
    def test_missing_file_returns_empty(self, tmp_path):
        assert _derive_taxonomy_labels(str(tmp_path / "nope.db")) == []

    def test_present_file_no_table_returns_empty(self, tmp_path):
        db_path = tmp_path / "gsheet.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE unrelated (x TEXT)")
        conn.commit()
        conn.close()
        assert _derive_taxonomy_labels(str(db_path)) == []

    def test_table_present_but_empty_returns_empty(self, tmp_path):
        db_path = tmp_path / "gsheet.db"
        _make_gsheet_db(db_path, [])
        assert _derive_taxonomy_labels(str(db_path)) == []

    def test_extracts_branch_names_from_feelings_columns(self, tmp_path):
        db_path = tmp_path / "gsheet.db"
        _make_gsheet_db(db_path, [
            {"Timestamp": "1/27/2026 10:00:00", "Angry feelings": "Frustrated", "Bad feelings": "Wobbly"},
        ])
        labels = _derive_taxonomy_labels(str(db_path))
        assert labels == ["Angry", "Bad"]

    def test_ignores_non_feelings_columns(self, tmp_path):
        db_path = tmp_path / "gsheet.db"
        _make_gsheet_db(db_path, [{"Timestamp": "x", "Mood": "5", "Sleep hours": "7"}])
        assert _derive_taxonomy_labels(str(db_path)) == []

    def test_implausible_branch_name_is_skipped(self, tmp_path):
        # A column whose stripped branch name fails the shape policy (too
        # many words) must not become a taxonomy label — same disclosure
        # policy as the wheel, applied here to form structure too.
        db_path = tmp_path / "gsheet.db"
        _make_gsheet_db(db_path, [
            {"This is a whole sentence not a branch name feelings": "x", "Sad feelings": "Lonely"},
        ])
        labels = _derive_taxonomy_labels(str(db_path))
        assert labels == ["Sad"]

    def test_dedupes_across_rows(self, tmp_path):
        db_path = tmp_path / "gsheet.db"
        _make_gsheet_db(db_path, [
            {"Angry feelings": "Frustrated"},
            {"Angry feelings": "Mad"},
        ])
        assert _derive_taxonomy_labels(str(db_path)) == ["Angry"]


class TestTaxonomyEndpoint:
    def test_degrades_to_used_only_when_gsheet_db_missing(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["taxonomy_source"] == "used-only"
        assert body["branches"] == []
        assert body["extra_used"] == [{"label": "Happy", "used": True, "count": 1}]

    def test_form_source_marks_used_and_unused_branches(self, client_and_vault, tmp_path, monkeypatch):
        client, vault, _ = client_and_vault
        gsheet_path = tmp_path / "gsheet.db"
        _make_gsheet_db(gsheet_path, [{"Angry feelings": "Frustrated", "Sad feelings": "Lonely"}])
        monkeypatch.setattr(journal_trends, "get_gsheet_sync_db_path", lambda: str(gsheet_path))

        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Angry"})
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["taxonomy_source"] == "form"
        by_label = {b["label"]: b for b in body["branches"]}
        assert by_label["Angry"]["used"] is True
        assert by_label["Angry"]["count"] == 1
        assert by_label["Sad"]["used"] is False
        assert by_label["Sad"]["count"] == 0
        assert body["extra_used"] == []

    def test_value_not_matching_any_branch_lands_in_extra_used(self, client_and_vault, tmp_path, monkeypatch):
        client, vault, _ = client_and_vault
        gsheet_path = tmp_path / "gsheet.db"
        _make_gsheet_db(gsheet_path, [{"Angry feelings": "Frustrated"}])
        monkeypatch.setattr(journal_trends, "get_gsheet_sync_db_path", lambda: str(gsheet_path))

        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Not sure"})
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["branches"] == [{"label": "Angry", "used": False, "count": 0}]
        assert body["extra_used"] == [{"label": "Not sure", "used": True, "count": 1}]

    def test_empty_window_reports_zero_entries(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["total_entries"] == 0
        assert body["emotion_entries"] == 0


# ---------------------------------------------------------------------------
# View C: felt vs. recorded connection
# ---------------------------------------------------------------------------

class _FakeResolver:
    """Resolves exactly the names it's told to, nothing else — a stand-in
    for `EntityResolver` so these tests never touch the real person store."""

    def __init__(self, mapping: dict):
        self._mapping = mapping

    def resolve_by_name(self, name, create_if_missing=False):
        return self._mapping.get(name)


def _make_interactions_db(path, rows: list[tuple]) -> str:
    """rows: list of (person_id, iso_timestamp)."""
    store = InteractionStore(db_path=str(path), strict=False)
    for i, (person_id, ts) in enumerate(rows):
        store.add(
            Interaction(
                id=f"int-{i}",
                person_id=person_id,
                timestamp=datetime.fromisoformat(ts),
                source_type="imessage",
                title="synthetic",
            )
        )
    return str(path)


class TestParseBoolField:
    @pytest.mark.parametrize("value", [True, "yes", "Yes", "true", "TRUE", 1])
    def test_truthy_values(self, value):
        assert _parse_bool_field(value) is True

    @pytest.mark.parametrize("value", [False, "no", "No", "false", 0])
    def test_falsy_values(self, value):
        assert _parse_bool_field(value) is False

    @pytest.mark.parametrize("value", ["maybe", "sort of", 2, None, 3.5, [], {}])
    def test_unparseable_values_return_none(self, value):
        assert _parse_bool_field(value) is None


class TestInteractionCountsByDay:
    def test_missing_db_returns_empty(self, tmp_path):
        counts = _interaction_counts_by_day("person-1", date(2026, 1, 1), date(2026, 1, 31), str(tmp_path / "nope.db"))
        assert counts == {}

    def test_counts_grouped_by_day_for_one_person(self, tmp_path):
        db_path = _make_interactions_db(tmp_path / "interactions.db", [
            ("person-1", "2026-01-27T09:00:00+00:00"),
            ("person-1", "2026-01-27T15:00:00+00:00"),
            ("person-1", "2026-01-28T09:00:00+00:00"),
            ("person-2", "2026-01-27T09:00:00+00:00"),  # different person, excluded
        ])
        counts = _interaction_counts_by_day("person-1", date(2026, 1, 1), date(2026, 1, 31), db_path)
        assert counts == {"2026-01-27": 2, "2026-01-28": 1}


class TestConnectionsEndpoint:
    def test_no_connection_fields_in_window(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        resp = client.get("/api/journal/connections?window=day")
        body = resp.json()
        assert body["fields"] == []
        assert body["total_entries"] == 1

    def test_unresolvable_name_reports_unresolved_with_no_counts(self, client_and_vault, monkeypatch):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "connection_ghost": True})
        monkeypatch.setattr(journal_trends, "get_entity_resolver", lambda: _FakeResolver({}))

        resp = client.get("/api/journal/connections?window=day")
        body = resp.json()
        field = body["fields"][0]
        assert field["field"] == "connection_ghost"
        assert field["resolution"]["status"] == "unresolved"
        assert field["resolution"]["canonical_name"] is None
        assert field["days"] == [{"date": "2026-06-30", "self_reported": True, "interaction_count": None}]

    def test_ambiguous_resolution_is_disclosed_and_still_counted(self, client_and_vault, tmp_path, monkeypatch):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "connection_sam": True})

        person = PersonEntity(id="person-amb", canonical_name="Sam Ambiguous")
        result = ResolutionResult(entity=person, is_new=False, confidence=0.42, match_type="fuzzy_ambiguous", disambiguation_applied=True)
        monkeypatch.setattr(journal_trends, "get_entity_resolver", lambda: _FakeResolver({"Sam": result}))

        db_path = _make_interactions_db(tmp_path / "interactions.db", [("person-amb", "2026-06-30T09:00:00+00:00")])
        monkeypatch.setattr(journal_trends, "get_interaction_db_path", lambda: db_path)

        resp = client.get("/api/journal/connections?window=day")
        field = resp.json()["fields"][0]
        assert field["resolution"]["status"] == "ambiguous"
        assert field["resolution"]["canonical_name"] == "Sam Ambiguous"
        assert "cautiously" in field["resolution"]["note"]
        assert field["days"][0]["interaction_count"] == 1

    def test_low_confidence_resolution_is_disclosed(self, client_and_vault, monkeypatch):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "connection_pat": False})

        person = PersonEntity(id="person-low", canonical_name="Pat Uncertain")
        result = ResolutionResult(entity=person, is_new=False, confidence=0.3, match_type="structured", disambiguation_applied=False)
        monkeypatch.setattr(journal_trends, "get_entity_resolver", lambda: _FakeResolver({"Pat": result}))

        resp = client.get("/api/journal/connections?window=day")
        field = resp.json()["fields"][0]
        assert field["resolution"]["status"] == "low_confidence"

    def test_resolved_field_matches_self_report_against_interaction_counts(self, client_and_vault, tmp_path, monkeypatch):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "connection_taylor": True})
        _write_entry(vault, "2026-01-28", {"date": "2026-01-28", "connection_taylor": False})

        person = PersonEntity(id="person-taylor", canonical_name="Taylor Walker")
        result = ResolutionResult(entity=person, is_new=False, confidence=0.95, match_type="name_exact")
        monkeypatch.setattr(journal_trends, "get_entity_resolver", lambda: _FakeResolver({"Taylor": result}))

        db_path = _make_interactions_db(tmp_path / "interactions.db", [
            ("person-taylor", "2026-01-27T09:00:00+00:00"),
            # The "reverse direction" case: not self-reported as connecting,
            # but a real interaction happened that day.
            ("person-taylor", "2026-01-28T09:00:00+00:00"),
        ])
        monkeypatch.setattr(journal_trends, "get_interaction_db_path", lambda: db_path)

        resp = client.get("/api/journal/connections?window=all-time")
        field = resp.json()["fields"][0]
        assert field["resolution"]["status"] == "resolved"
        by_date = {d["date"]: d for d in field["days"]}
        assert by_date["2026-01-27"] == {"date": "2026-01-27", "self_reported": True, "interaction_count": 1}
        assert by_date["2026-01-28"] == {"date": "2026-01-28", "self_reported": False, "interaction_count": 1}

    def test_unparseable_self_report_is_skipped_and_counted(self, client_and_vault, monkeypatch):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "connection_avery": "maybe"})
        monkeypatch.setattr(journal_trends, "get_entity_resolver", lambda: _FakeResolver({}))

        resp = client.get("/api/journal/connections?window=day")
        field = resp.json()["fields"][0]
        assert field["days"] == []
        assert field["unparseable_entries"] == 1

    def test_multiple_connection_fields_discovered_dynamically(self, client_and_vault, monkeypatch):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {
            "date": "2026-06-30",
            "connection_river": True,
            "connection_sage": False,
        })
        monkeypatch.setattr(journal_trends, "get_entity_resolver", lambda: _FakeResolver({}))

        resp = client.get("/api/journal/connections?window=day")
        field_names = {f["field"] for f in resp.json()["fields"]}
        assert field_names == {"connection_river", "connection_sage"}


# ---------------------------------------------------------------------------
# View D: the scalar stack
# ---------------------------------------------------------------------------

class TestNumeric:
    def test_int_and_float_pass_through(self):
        assert _numeric(5) == 5.0
        assert _numeric(5.5) == 5.5

    def test_bool_is_excluded_even_though_it_is_an_int_subclass(self):
        assert _numeric(True) is None
        assert _numeric(False) is None

    def test_non_numeric_is_none(self):
        assert _numeric("high") is None
        assert _numeric(None) is None


class TestScalarsEndpoint:
    def test_empty_window(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/scalars?window=day")
        body = resp.json()
        assert body["total_entries"] == 0
        assert all(s["points"] == [] for s in body["series"])
        assert body["correlation"]["n"] == 0
        assert body["correlation"]["r"] is None

    def test_single_entry_n1_correlation_is_undefined(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "mood": 6, "stress": 4, "sleep": 7, "body": 5})
        resp = client.get("/api/journal/scalars?window=day")
        body = resp.json()
        assert body["correlation"]["n"] == 1
        assert body["correlation"]["r"] is None
        assert "n=1" in body["correlation"]["caveat"]
        mood_series = next(s for s in body["series"] if s["field"] == "mood")
        assert mood_series["points"] == [{"date": "2026-06-30", "value": 6.0}]

    def test_two_points_perfectly_correlated(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "mood": 5, "stress": 5})
        _write_entry(vault, "2026-01-28", {"date": "2026-01-28", "mood": 7, "stress": 8})
        resp = client.get("/api/journal/scalars?window=all-time")
        body = resp.json()
        assert body["correlation"]["n"] == 2
        assert body["correlation"]["r"] == pytest.approx(1.0)
        assert "caveat" in body["correlation"]
        assert "scale" in body["correlation"]["caveat"] or "reverse-coded" in body["correlation"]["caveat"]

    def test_zero_variance_makes_correlation_undefined(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "mood": 5, "stress": 5})
        _write_entry(vault, "2026-01-28", {"date": "2026-01-28", "mood": 5, "stress": 6})
        _write_entry(vault, "2026-01-29", {"date": "2026-01-29", "mood": 5, "stress": 7})
        resp = client.get("/api/journal/scalars?window=all-time")
        body = resp.json()
        assert body["correlation"]["r"] is None
        assert "variance" in body["correlation"]["caveat"]

    def test_missing_field_on_some_entries_is_excluded_not_zeroed(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-29", {"date": "2026-06-29", "mood": 6})  # no stress
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "mood": 7, "stress": 3})
        resp = client.get("/api/journal/scalars?window=week")
        body = resp.json()
        mood_series = next(s for s in body["series"] if s["field"] == "mood")
        stress_series = next(s for s in body["series"] if s["field"] == "stress")
        assert len(mood_series["points"]) == 2
        assert len(stress_series["points"]) == 1
        assert body["correlation"]["n"] == 1  # only one day has both

    def test_grid_aligned_with_strip_for_all_time(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "mood": 5})
        resp_scalars = client.get("/api/journal/scalars?window=all-time")
        resp_strip = client.get("/api/journal/strip?window=all-time")
        assert resp_scalars.json()["start_date"] == resp_strip.json()["start_date"] == "2026-01-27"
        assert resp_scalars.json()["end_date"] == resp_strip.json()["end_date"]


# ---------------------------------------------------------------------------
# Cross-view: existing disclosure policy still collapses implausible values
# ---------------------------------------------------------------------------

class TestDisclosurePolicyStillApplies:
    def test_free_text_feeling_is_unrecognized_in_strip(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {
            "date": "2026-06-30",
            "feeling": "a whole sentence about something private that happened today",
        })
        resp = client.get("/api/journal/strip?window=day")
        body = resp.json()
        assert body["days"][0]["primary_emotion"] == "Unrecognized"
        assert "private" not in resp.text

    def test_free_text_feeling_is_unrecognized_in_taxonomy_extra_used(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {
            "date": "2026-06-30",
            "feeling": "a whole sentence about something private that happened today",
        })
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["extra_used"] == [{"label": "Unrecognized", "used": True, "count": 1}]
        assert "private" not in resp.text


def test_connection_resolution_model_defaults():
    r = ConnectionResolution(status="unresolved", note="x")
    assert r.canonical_name is None
    assert r.confidence is None
