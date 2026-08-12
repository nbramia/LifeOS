"""
Tests for the journal emotion-wheel aggregation API (#212).

All fixtures use invented dates, names, and emotion values — never the real
journal, which is deeply personal data. Covers the real-data irregularities
this feature has to handle: a singular `<slug>_feeling` key (observed for
more than one branch in the real vault, starting with "Bad"), chains that
terminate before depth 3, a multi-word value ("Not sure") used as both a
chain value and as the basis for a lookup key, and the same value appearing
at multiple chain positions.
"""
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import journal as journal_route
from api.routes.journal import (
    EmotionNode,
    build_wheel,
    iter_journal_chains,
    parse_emotion_chain,
    window_bounds,
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


# -- parse_emotion_chain: the frontmatter-chain walker --

class TestParseEmotionChain:
    def test_singular_key_for_irregular_value(self):
        # Mirrors the real vault's singular-key branch: "Bad" uses
        # `<slug>_feeling`, not `<slug>_feelings`.
        fm = {"feeling": "Bad", "bad_feeling": "Wobbly", "wobbly_feeling": "Fizzy"}
        assert parse_emotion_chain(fm) == ["Bad", "Wobbly", "Fizzy"]

    def test_plural_key_is_the_common_case(self):
        fm = {"feeling": "Happy", "happy_feelings": "Cozy", "cozy_feelings": "Sunny"}
        assert parse_emotion_chain(fm) == ["Happy", "Cozy", "Sunny"]

    def test_chain_terminates_early(self):
        # No restless_feelings key at all -> chain stops at depth 2.
        fm = {"feeling": "Angry", "angry_feelings": "Restless"}
        assert parse_emotion_chain(fm) == ["Angry", "Restless"]

    def test_level1_only_no_children(self):
        fm = {"feeling": "Not sure"}
        assert parse_emotion_chain(fm) == ["Not sure"]

    def test_multiword_value_used_as_lookup_key(self):
        # "Not sure" appears mid-chain and must slug to "not_sure_feelings"
        # to find its own child — a naive f"{value.lower()}_feelings" without
        # replacing the space would miss this.
        fm = {
            "feeling": "Sad",
            "sad_feelings": "Not sure",
            "not_sure_feelings": "Muted",
        }
        assert parse_emotion_chain(fm) == ["Sad", "Not sure", "Muted"]

    def test_not_sure_as_leaf_under_a_different_branch(self):
        fm = {"feeling": "Bad", "bad_feeling": "Achy", "achy_feelings": "Not sure"}
        assert parse_emotion_chain(fm) == ["Bad", "Achy", "Not sure"]

    def test_missing_feeling_key_yields_empty_chain(self):
        assert parse_emotion_chain({"mood": 6, "sleep": 7}) == []

    def test_non_string_feeling_value_yields_empty_chain(self):
        assert parse_emotion_chain({"feeling": 42}) == []

    def test_cyclic_data_does_not_infinite_loop(self):
        # Defensive case: A -> B -> A would loop forever without the seen-set guard.
        fm = {"feeling": "A", "a_feelings": "B", "b_feelings": "A"}
        chain = parse_emotion_chain(fm)
        assert chain == ["A", "B"]


# -- window_bounds --

class TestWindowBounds:
    def test_all_time_has_no_start(self):
        start, end = window_bounds("all-time", date(2026, 6, 30))
        assert start is None
        assert end == date(2026, 6, 30)

    def test_day_window_is_today_only(self):
        start, end = window_bounds("day", date(2026, 6, 30))
        assert start == end == date(2026, 6, 30)

    def test_week_window_is_seven_trailing_days(self):
        start, end = window_bounds("week", date(2026, 6, 30))
        assert start == date(2026, 6, 24)
        assert end == date(2026, 6, 30)

    def test_unknown_window_falls_back_to_month(self):
        start, _ = window_bounds("fortnight", date(2026, 6, 30))
        assert start == date(2026, 6, 1)


# -- build_wheel: chains -> nested count tree --

class TestBuildWheel:
    def test_counts_and_nesting(self):
        chains = [
            ["Happy", "Cozy", "Sunny"],
            ["Happy", "Cozy", "Sunny"],
            ["Happy", "Giddy"],
            ["Bad", "Wobbly", "Fizzy"],
        ]
        wheel = build_wheel(chains)
        by_value = {n.value: n for n in wheel}
        assert by_value["Happy"].count == 3
        assert by_value["Bad"].count == 1

        happy_children = {c.value: c for c in by_value["Happy"].children}
        assert happy_children["Cozy"].count == 2
        assert happy_children["Cozy"].children[0].value == "Sunny"
        assert happy_children["Cozy"].children[0].count == 2
        assert happy_children["Giddy"].count == 1
        assert happy_children["Giddy"].children == []

    def test_same_value_at_different_positions_stays_distinct(self):
        # "Not sure" as a bare level-1 value, and "Not sure" as a leaf under
        # Bad -> Achy, must not be merged into one node.
        chains = [
            ["Not sure"],
            ["Bad", "Achy", "Not sure"],
        ]
        wheel = build_wheel(chains)
        by_value = {n.value: n for n in wheel}
        assert by_value["Not sure"].count == 1
        assert by_value["Not sure"].children == []

        bad_node = by_value["Bad"]
        achy_node = bad_node.children[0]
        assert achy_node.value == "Achy"
        leaf = achy_node.children[0]
        assert leaf.value == "Not sure"
        assert leaf.count == 1

    def test_empty_chains_produce_empty_wheel(self):
        assert build_wheel([]) == []

    def test_sorted_by_count_desc_then_alpha(self):
        chains = [["Sad"], ["Angry"], ["Bad"], ["Bad"]]
        wheel = build_wheel(chains)
        assert [n.value for n in wheel] == ["Bad", "Angry", "Sad"]


# -- iter_journal_chains: reads synthetic vault fixtures --

class TestIterJournalChains:
    def test_bad_feeling_singular_case_from_disk(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-06-01", {
            "date": "2026-06-01", "feeling": "Bad", "bad_feeling": "Wobbly",
            "wobbly_feeling": "Fizzy",
        })
        chains = list(iter_journal_chains(vault, "all-time", today=date(2026, 6, 30)))
        assert chains == [["Bad", "Wobbly", "Fizzy"]]

    def test_missing_frontmatter_is_skipped(self, tmp_path):
        vault = tmp_path / "vault"
        journal_dir = vault / "Personal" / "Journal"
        journal_dir.mkdir(parents=True)
        (journal_dir / "2026-06-01.md").write_text("Just prose, no frontmatter block.")
        chains = list(iter_journal_chains(vault, "all-time", today=date(2026, 6, 30)))
        assert chains == []

    def test_entry_without_feeling_key_is_skipped(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-06-01", {"date": "2026-06-01", "mood": 5})
        chains = list(iter_journal_chains(vault, "all-time", today=date(2026, 6, 30)))
        assert chains == []

    def test_empty_window_returns_no_entries(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-01-01", {"date": "2026-01-01", "feeling": "Happy"})
        # "day" window on 2026-06-30 only covers 2026-06-30 itself.
        chains = list(iter_journal_chains(vault, "day", today=date(2026, 6, 30)))
        assert chains == []

    def test_single_entry_window(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        chains = list(iter_journal_chains(vault, "day", today=date(2026, 6, 30)))
        assert chains == [["Happy"]]

    def test_window_filters_out_of_range_entries(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        _write_entry(vault, "2024-01-01", {"date": "2024-01-01", "feeling": "Sad"})
        week_chains = list(iter_journal_chains(vault, "week", today=date(2026, 6, 30)))
        assert week_chains == [["Happy"]]
        all_time_chains = list(iter_journal_chains(vault, "all-time", today=date(2026, 6, 30)))
        assert sorted(c[0] for c in all_time_chains) == ["Happy", "Sad"]

    def test_missing_journal_dir_yields_nothing(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        chains = list(iter_journal_chains(vault, "all-time", today=date(2026, 6, 30)))
        assert chains == []

    def test_falls_back_to_filename_when_date_field_missing(self, tmp_path):
        vault = tmp_path / "vault"
        _write_entry(vault, "2026-06-30", {"feeling": "Happy"})  # no `date:` field
        chains = list(iter_journal_chains(vault, "day", today=date(2026, 6, 30)))
        assert chains == [["Happy"]]


# -- endpoint wiring --

@pytest.fixture
def client_and_vault(tmp_path, monkeypatch):
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 6, 30)

    monkeypatch.setattr(journal_route, "date", _FixedDate)
    vault = tmp_path / "vault"
    monkeypatch.setattr(journal_route.settings, "vault_path", vault)
    app = FastAPI()
    app.include_router(journal_route.router)
    return TestClient(app), vault


class TestJournalEmotionsEndpoint:
    def test_empty_vault_returns_zero_entries(self, client_and_vault):
        client, _ = client_and_vault
        resp = client.get("/api/journal/emotions?window=all-time")
        assert resp.status_code == 200
        body = resp.json()
        assert body["entry_count"] == 0
        assert body["wheel"] == []

    def test_default_window_is_all_time(self, client_and_vault):
        client, _ = client_and_vault
        resp = client.get("/api/journal/emotions")
        assert resp.status_code == 200
        assert resp.json()["window"] == "all-time"
        assert resp.json()["start_date"] is None

    def test_aggregates_across_synthetic_entries(self, client_and_vault):
        client, vault = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy", "happy_feelings": "Cozy"})
        _write_entry(vault, "2026-06-29", {"date": "2026-06-29", "feeling": "Bad", "bad_feeling": "Wobbly"})
        _write_entry(vault, "2026-06-28", {"date": "2026-06-28", "feeling": "Happy", "happy_feelings": "Cozy"})

        resp = client.get("/api/journal/emotions?window=week")
        assert resp.status_code == 200
        body = resp.json()
        assert body["entry_count"] == 3
        happy = next(n for n in body["wheel"] if n["value"] == "Happy")
        assert happy["count"] == 2
        assert happy["children"][0]["value"] == "Cozy"
        assert happy["children"][0]["count"] == 2
        bad = next(n for n in body["wheel"] if n["value"] == "Bad")
        assert bad["children"][0]["value"] == "Wobbly"

    def test_single_entry_window(self, client_and_vault):
        client, vault = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        resp = client.get("/api/journal/emotions?window=day")
        body = resp.json()
        assert body["entry_count"] == 1
        assert body["wheel"] == [{"value": "Happy", "count": 1, "children": []}]

    def test_unknown_window_falls_back_gracefully(self, client_and_vault):
        client, vault = client_and_vault
        _write_entry(vault, "2026-06-30", {"date": "2026-06-30", "feeling": "Happy"})
        resp = client.get("/api/journal/emotions?window=fortnight")
        assert resp.status_code == 200
        assert resp.json()["entry_count"] == 1


def test_emotion_node_model_is_recursive():
    node = EmotionNode(value="Happy", count=1, children=[EmotionNode(value="Cozy", count=1)])
    assert node.children[0].value == "Cozy"
    assert node.children[0].children == []
