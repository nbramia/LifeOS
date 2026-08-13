"""
Tests for the journal trend views: the strip, the unexplored wheel, and
the scalar stack.

All fixtures use invented dates, names, and values — never the real
journal, the real CRM, or the real interactions database. A fourth view
(felt-vs-recorded connection) was removed after operator feedback that it
wasn't useful — see docs/specs/product/journal-analytics.md's Removed
section — which is why there's no longer a fake entity resolver or temp
interactions database here.
"""
import json
import sqlite3
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import journal_trends
from api.routes.journal_trends import (
    _PUBLISHED_TAXONOMY_MAP,
    _derive_branch_groups,
    _derive_taxonomy_labels,
    _find_grouping_conflicts,
    _grid_span,
    _group_for_label,
    _numeric,
)

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
    """The strip always spans full history — earliest valid entry through
    today — and takes no `window` parameter at all. See CHANGE 1 (squares)
    and the follow-up operator feedback (auto-scale to full history,
    independent of the window selector) this module was revised for."""

    def test_empty_vault_has_no_days(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/strip")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_entries"] == 0
        assert body["emotion_entries"] == 0
        assert body["days"] == []
        assert body["start_date"] is None
        assert "window" not in body  # no window concept applies to this view at all

    def test_a_window_query_param_has_no_effect(self, client_and_vault):
        # The endpoint takes no `window` parameter; passing one anyway
        # (e.g. a stale bookmark, or a caller assuming this view works
        # like the other three) must not silently change the answer —
        # confirming there's no half-wired parameter left behind.
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "feeling": "Sad"})
        without = client.get("/api/journal/strip").json()
        with_window = client.get("/api/journal/strip?window=day").json()
        assert without == with_window

    def test_single_entry_n1(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        resp = client.get("/api/journal/strip")
        body = resp.json()
        assert len(body["days"]) == 1
        assert body["days"][0] == {"date": "2026-06-30", "has_entry": True, "primary_emotion": "Happy"}
        assert body["emotion_entries"] == 1

    def test_entry_with_no_feeling_is_a_distinct_state(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "mood": 5})
        resp = client.get("/api/journal/strip")
        body = resp.json()
        assert body["days"][0]["has_entry"] is True
        assert body["days"][0]["primary_emotion"] is None
        assert body["emotion_entries"] == 0

    def test_not_sure_as_root_is_an_entry_with_no_feeling(self, client_and_vault):
        # "Not sure" is excluded from the wheel as a non-answer; the strip
        # has to agree, or it would show a sixth colour in its key claiming
        # to be an emotion. The day still counts as an entry — only the
        # emotion is absent.
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Not sure"})
        body = client.get("/api/journal/strip").json()
        assert body["days"][0]["has_entry"] is True
        assert body["days"][0]["primary_emotion"] is None
        assert body["total_entries"] == 1
        assert body["emotion_entries"] == 0

    def test_not_sure_root_is_matched_case_insensitively(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "not SURE"})
        body = client.get("/api/journal/strip").json()
        assert body["days"][0]["primary_emotion"] is None
        assert body["emotion_entries"] == 0

    def test_leftmost_is_earliest_entry_rightmost_is_today(self, client_and_vault):
        # The exact contract from operator feedback: furthest left is the
        # earliest recorded observation, furthest right is today — fixed
        # in this fixture at 2026-06-30 — regardless of the gap between
        # them.
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "feeling": "Sad"})
        _write_entry(vault, "2026-06-22", {"date": "2026-06-22", "feeling": "Happy"})
        resp = client.get("/api/journal/strip")
        body = resp.json()
        assert body["start_date"] == "2026-01-27"
        assert body["end_date"] == "2026-06-30"  # "today", not the last entry
        assert body["days"][0]["date"] == "2026-01-27"
        assert body["days"][-1]["date"] == "2026-06-30"
        by_date = {d["date"]: d for d in body["days"]}
        assert by_date["2026-01-27"]["primary_emotion"] == "Sad"
        assert by_date["2026-06-22"]["primary_emotion"] == "Happy"
        # A day squarely in the middle of the gap has no entry at all.
        assert by_date["2026-03-15"] == {"date": "2026-03-15", "has_entry": False, "primary_emotion": None}
        assert body["total_entries"] == 2
        assert body["emotion_entries"] == 2
        # Every calendar day from 2026-01-27 through 2026-06-30 is present.
        assert len(body["days"]) == (date(2026, 6, 30) - date(2026, 1, 27)).days + 1

    def test_malformed_earlier_file_does_not_move_the_left_edge(self, client_and_vault):
        # A file that doesn't parse as a trustworthy journal entry (here:
        # frontmatter `date:` disagreeing with the filename) must not set
        # the left edge to a date with no real data behind it, even though
        # it's the earliest file on disk.
        client, vault, _ = client_and_vault
        journal_dir = vault / "Personal" / "Journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "2020-01-01.md").write_text(
            "---\ndate: 2019-01-01\nfeeling: Bad\n---\nMismatched date.", encoding="utf-8"
        )
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "feeling": "Sad"})
        resp = client.get("/api/journal/strip")
        body = resp.json()
        assert body["start_date"] == "2026-01-27"


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
        # "Happy" is a known primary, so it groups under itself even with
        # no form taxonomy available to derive anything from.
        assert body["extra_used"] == [{"label": "Happy", "used": True, "count": 1, "group": "Happy"}]

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
        assert by_label["Angry"]["group"] == "Angry"  # primaries group under themselves
        assert by_label["Sad"]["used"] is False
        assert by_label["Sad"]["count"] == 0
        assert by_label["Sad"]["group"] == "Sad"
        assert body["extra_used"] == []

    def test_value_not_matching_any_branch_lands_in_extra_used(self, client_and_vault, tmp_path, monkeypatch):
        client, vault, _ = client_and_vault
        gsheet_path = tmp_path / "gsheet.db"
        _make_gsheet_db(gsheet_path, [{"Angry feelings": "Frustrated"}])
        monkeypatch.setattr(journal_trends, "get_gsheet_sync_db_path", lambda: str(gsheet_path))

        # "Meh" is a stray value that doesn't match any derived branch and
        # isn't a known primary, so it lands in extra_used, unplaced.
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Meh"})
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["branches"] == [{"label": "Angry", "used": False, "count": 0, "group": "Angry"}]
        assert body["extra_used"] == [{"label": "Meh", "used": True, "count": 1, "group": "Unplaced"}]

    def test_empty_window_reports_zero_entries(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["total_entries"] == 0
        assert body["emotion_entries"] == 0

    def test_group_order_is_primaries_then_unplaced(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["group_order"] == ["Angry", "Bad", "Disgusted", "Fearful", "Happy", "Sad", "Surprised", "Unplaced"]


class TestNotSureExcludedFromTaxonomy:
    """"Not sure" must disappear entirely — from branches, extra_used, and
    every count — whether it appears as a root `feeling:` value or as a
    leaf deep in another branch's chain. See CHANGE 3 of the operator
    feedback this module was revised for."""

    def test_not_sure_as_root_produces_no_branch_or_extra_used_entry(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Not sure"})
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        assert body["branches"] == []
        assert body["extra_used"] == []
        # The entry itself still counts as emotion-bearing — "Not sure" is
        # a real answer to "did you log a feeling", just not a taxonomy
        # branch this view groups or displays.
        assert body["emotion_entries"] == 1

    def test_not_sure_as_leaf_is_never_counted(self, client_and_vault, tmp_path, monkeypatch):
        client, vault, _ = client_and_vault
        gsheet_path = tmp_path / "gsheet.db"
        _make_gsheet_db(gsheet_path, [{"Sad feelings": "Muted"}])
        monkeypatch.setattr(journal_trends, "get_gsheet_sync_db_path", lambda: str(gsheet_path))
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Sad", "sad_feelings": "Not sure"})
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        by_label = {b["label"]: b for b in body["branches"]}
        assert by_label["Sad"]["used"] is True
        assert by_label["Sad"]["count"] == 1
        assert body["extra_used"] == []  # the "Not sure" leaf is dropped, not surfaced anywhere

    def test_not_sure_as_a_derived_form_column_is_filtered(self, client_and_vault, tmp_path, monkeypatch):
        # Defensive: even if the sheet somehow had a "Not sure feelings"
        # column, it must never become a displayable taxonomy branch.
        client, vault, _ = client_and_vault
        gsheet_path = tmp_path / "gsheet.db"
        _make_gsheet_db(gsheet_path, [{"Not sure feelings": "Whatever", "Sad feelings": "Muted"}])
        monkeypatch.setattr(journal_trends, "get_gsheet_sync_db_path", lambda: str(gsheet_path))
        resp = client.get("/api/journal/taxonomy?window=day")
        body = resp.json()
        labels = {b["label"] for b in body["branches"]}
        assert "Not sure" not in labels
        assert "Sad" in labels


class TestDeriveBranchGroups:
    """Unit coverage for the grouping logic itself, independent of the
    taxonomy endpoint's use of it."""

    def test_majority_vote_assigns_the_more_frequent_root(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-01-01", {"date": "2026-01-01", "feeling": "Sad", "sad_feelings": "Muted"})
        _write_entry(vault, "2026-01-02", {"date": "2026-01-02", "feeling": "Sad", "sad_feelings": "Muted"})
        _write_entry(vault, "2026-01-03", {"date": "2026-01-03", "feeling": "Bad", "bad_feeling": "Muted"})
        groups = _derive_branch_groups(vault, date(2026, 6, 30))
        assert groups["muted"] == "Sad"  # 2 votes for Sad outweigh 1 for Bad

    def test_tie_breaks_alphabetically(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-01-01", {"date": "2026-01-01", "feeling": "Bad", "bad_feeling": "Foggy"})
        _write_entry(vault, "2026-01-02", {"date": "2026-01-02", "feeling": "Angry", "angry_feelings": "Foggy"})
        groups = _derive_branch_groups(vault, date(2026, 6, 30))
        assert groups["foggy"] == "Angry"  # one vote each; "Angry" < "Bad" alphabetically

    def test_not_sure_root_casts_no_votes(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-01-01", {"date": "2026-01-01", "feeling": "Not sure", "not_sure_feelings": "Adrift"})
        groups = _derive_branch_groups(vault, date(2026, 6, 30))
        assert "adrift" not in groups

    def test_not_sure_as_a_chain_value_is_never_recorded(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-01-01", {"date": "2026-01-01", "feeling": "Sad", "sad_feelings": "Not sure"})
        groups = _derive_branch_groups(vault, date(2026, 6, 30))
        assert "not sure" not in groups

    def test_root_outside_known_primaries_casts_no_votes(self, tmp_path):
        # A hand-edited entry whose root isn't one of the seven known
        # primaries at all — must not become an eighth, invented primary.
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-01-01", {"date": "2026-01-01", "feeling": "Weird", "weird_feelings": "Odd"})
        groups = _derive_branch_groups(vault, date(2026, 6, 30))
        assert "odd" not in groups

    def test_not_window_scoped(self, tmp_path):
        # Grouping is a structural property of the form, derived from
        # every entry ever written — not something that should change
        # depending on which window the taxonomy endpoint happens to be
        # showing. Passing a `today` far enough in the future to be
        # "all-time"-inclusive of an old entry must still find its votes.
        vault = tmp_path / "vault"
        _write_entry(vault, "2020-01-01", {"date": "2020-01-01", "feeling": "Happy", "happy_feelings": "Cozy"})
        groups = _derive_branch_groups(vault, date(2026, 6, 30))
        assert groups["cozy"] == "Happy"


class TestPublishedTaxonomyFallback:
    """Grouping by observed evidence alone is self-defeating for this
    specific view: a branch is only ever observed if it was selected, but
    the unexplored wheel exists to show branches that were *never*
    selected. Verified against one real vault: leaving grouping purely
    evidence-based left 25 of 47 branches (53%) unplaced. This fallback
    (`_PUBLISHED_TAXONOMY_MAP`) is what closes that gap — see its
    module-level comment and `_group_for_label`'s docstring for the full
    precedence order."""

    @pytest.mark.parametrize("label,expected_primary", [
        # The exact branches a real vault left unplaced before this
        # fallback existed (a representative subset of the reported 25).
        ("Accepted", "Happy"),
        ("Aggressive", "Angry"),
        ("Anxious", "Fearful"),
        ("Awful", "Disgusted"),
        ("Bitter", "Angry"),
        ("Confused", "Surprised"),
        ("Critical", "Angry"),
        ("Depressed", "Sad"),
        ("Despairing", "Sad"),
        ("Distant", "Angry"),
        ("Excited", "Surprised"),
        ("Humiliated", "Angry"),
        ("Hurt", "Sad"),
        ("Insecure", "Fearful"),
        ("Interested", "Happy"),
        # "Playful" specifically: the branch whose form-order position
        # (index 29, immediately before "Happy" at 30) is what disproved
        # the original column-order-encodes-depth assumption.
        ("Playful", "Happy"),
    ])
    def test_known_unplaced_branches_now_resolve_via_the_published_map(self, label, expected_primary):
        # No observed evidence at all (empty branch_groups) — purely
        # exercising the published-map fallback.
        assert _group_for_label(label, {}) == expected_primary

    def test_a_branch_the_map_does_not_cover_still_lands_unplaced(self):
        # The map is a fallback, not a guarantee of full coverage — an
        # honest gap beats a wrong guess here too.
        assert _group_for_label("SomeBrandNewFormWord", {}) == "Unplaced"

    def test_derived_evidence_still_wins_over_the_published_map(self):
        # If this vault's real chains ever contradict the published map
        # for some branch, the observed evidence takes precedence — the
        # map is only consulted for branches with zero observed evidence.
        branch_groups = {"accepted": "Sad"}  # hypothetical contradicting real evidence
        assert _group_for_label("Accepted", branch_groups) == "Sad"

    def test_known_primaries_are_never_shadowed_by_the_map(self):
        # A branch that is itself a known primary groups under itself,
        # checked before either the derived votes or the published map —
        # confirms the map has no entries that could override this.
        for primary in journal_trends._KNOWN_PRIMARIES:
            assert _group_for_label(primary, {}) == primary


class TestGroupingConflictDetection:
    """`_find_grouping_conflicts` is the safety net that keeps the
    published map from silently overriding — or being silently
    contradicted by — real observed evidence without anyone noticing."""

    def test_no_conflict_when_derived_agrees_with_published(self):
        branch_groups = {"accepted": "Happy"}  # matches _PUBLISHED_TAXONOMY_MAP
        assert _find_grouping_conflicts(branch_groups) == []

    def test_flags_a_real_disagreement(self):
        branch_groups = {"accepted": "Sad"}  # published map says "Happy"
        conflicts = _find_grouping_conflicts(branch_groups)
        assert conflicts == [("accepted", "Sad", "Happy")]

    def test_a_label_the_published_map_does_not_cover_is_never_a_conflict(self):
        branch_groups = {"some_unmapped_word": "Bad"}
        assert _find_grouping_conflicts(branch_groups) == []

    def test_the_published_map_itself_has_no_internal_contradictions(self):
        # Sanity check on the map's own construction: every key maps to
        # exactly one of the seven known primaries, never something else.
        primaries = set(journal_trends._KNOWN_PRIMARIES)
        for label, primary in _PUBLISHED_TAXONOMY_MAP.items():
            assert primary in primaries, f"{label!r} maps to unknown primary {primary!r}"


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


def _corr(body, a, b):
    """Pull one pair's ScalarCorrelation dict out of a scalars response."""
    return next(c for c in body["correlations"] if c["pair"] == [a, b])


class TestScalarsEndpoint:
    def test_returns_all_six_pairs(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/scalars?window=day")
        body = resp.json()
        pairs = {tuple(c["pair"]) for c in body["correlations"]}
        assert pairs == {
            ("mood", "stress"), ("mood", "sleep"), ("mood", "body"),
            ("stress", "sleep"), ("stress", "body"), ("sleep", "body"),
        }

    def test_empty_window(self, client_and_vault):
        client, _, _ = client_and_vault
        resp = client.get("/api/journal/scalars?window=day")
        body = resp.json()
        assert body["total_entries"] == 0
        assert all(s["points"] == [] for s in body["series"])
        assert all(c["n"] == 0 and c["r"] is None for c in body["correlations"])

    def test_single_entry_n1_correlation_is_undefined(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "mood": 6, "stress": 4, "sleep": 7, "body": 5})
        resp = client.get("/api/journal/scalars?window=day")
        body = resp.json()
        mood_stress = _corr(body, "mood", "stress")
        assert mood_stress["n"] == 1
        assert mood_stress["r"] is None
        assert "n=1" in mood_stress["caveat"]
        mood_series = next(s for s in body["series"] if s["field"] == "mood")
        assert mood_series["points"] == [{"date": "2026-06-30", "value": 6.0}]

    def test_two_points_perfectly_correlated(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "mood": 5, "stress": 5})
        _write_entry(vault, "2026-01-28", {"date": "2026-01-28", "mood": 7, "stress": 8})
        resp = client.get("/api/journal/scalars?window=all-time")
        body = resp.json()
        mood_stress = _corr(body, "mood", "stress")
        assert mood_stress["n"] == 2
        assert mood_stress["r"] == pytest.approx(1.0)
        # The pair-specific caveat is now just the "co-movement" note — the
        # scale-direction disclaimer moved to the frontend's single shared
        # banner above the whole grid (see journal-trends.html), since
        # it's identical for all six pairs and repeating it verbatim six
        # times in the API response would be redundant, not informative.
        assert "co-movement" in mood_stress["caveat"]

    def test_zero_variance_makes_correlation_undefined(self, client_and_vault):
        client, vault, _ = client_and_vault
        _write_entry(vault, "2026-01-27", {"date": "2026-01-27", "mood": 5, "stress": 5})
        _write_entry(vault, "2026-01-28", {"date": "2026-01-28", "mood": 5, "stress": 6})
        _write_entry(vault, "2026-01-29", {"date": "2026-01-29", "mood": 5, "stress": 7})
        resp = client.get("/api/journal/scalars?window=all-time")
        body = resp.json()
        mood_stress = _corr(body, "mood", "stress")
        assert mood_stress["r"] is None
        assert "variance" in mood_stress["caveat"]

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
        assert _corr(body, "mood", "stress")["n"] == 1  # only one day has both

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
        assert body["extra_used"] == [{"label": "Unrecognized", "used": True, "count": 1, "group": "Unplaced"}]
        assert "private" not in resp.text
