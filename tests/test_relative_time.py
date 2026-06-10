"""Tests for the deterministic relative-time resolver.

The resolver is a pure function of the injected ``now`` — these tests pin a
fixed reference date (Wednesday, 2026-06-10) and assert exact ranges so the
date math can't silently drift.
"""
from datetime import date, datetime

import pytest

from api.utils.date_parser import resolve_relative_time

pytestmark = pytest.mark.unit

# 2026-06-10 is a Wednesday (weekday() == 2).
NOW = date(2026, 6, 10)


@pytest.mark.parametrize("phrase,expected", [
    ("what did I do today?", ("2026-06-10", "2026-06-10")),
    ("anything from yesterday", ("2026-06-09", "2026-06-09")),
    ("notes this week", ("2026-06-08", "2026-06-10")),       # Mon..today
    ("emails last week", ("2026-06-01", "2026-06-07")),       # prev Mon..Sun
    ("spending this month", ("2026-06-01", "2026-06-10")),
    ("what happened last month", ("2026-05-01", "2026-05-31")),
    ("summary this year", ("2026-01-01", "2026-06-10")),
    ("recap of last year", ("2025-01-01", "2025-12-31")),
    ("past 7 days", ("2026-06-03", "2026-06-10")),
    ("last 2 weeks", ("2026-05-27", "2026-06-10")),
    ("previous 3 months", ("2026-03-12", "2026-06-10")),     # 90 days
    ("recent updates", ("2026-03-12", "2026-06-10")),        # 90-day window
    ("anything lately?", ("2026-03-12", "2026-06-10")),
    ("what happened recently", ("2026-03-12", "2026-06-10")),
])
def test_recognized_phrases(phrase, expected):
    assert resolve_relative_time(phrase, NOW) == expected


@pytest.mark.parametrize("phrase", [
    "what is the quarterly budget",
    "Taylor's phone number",
    "summarize the product roadmap",
    "",
])
def test_unrecognized_returns_none(phrase):
    assert resolve_relative_time(phrase, NOW) is None


def test_bounded_phrase_wins_over_vague_recent():
    """'last week' should produce the precise week range, not the 90-day 'recent'
    window, even when both words appear."""
    assert resolve_relative_time("any recent notes from last week", NOW) == (
        "2026-06-01",
        "2026-06-07",
    )


def test_accepts_datetime_as_now():
    """A tz-aware/naive datetime is accepted and reduced to its date."""
    now = datetime(2026, 6, 10, 15, 30, 0)
    assert resolve_relative_time("today", now) == ("2026-06-10", "2026-06-10")


def test_is_pure_does_not_read_clock():
    """Same inputs → same output regardless of wall-clock time."""
    assert resolve_relative_time("today", date(2020, 1, 1)) == ("2020-01-01", "2020-01-01")


@pytest.mark.parametrize("phrase", ["recent updates", "anything lately", "recently"])
def test_include_vague_false_drops_vague_terms(phrase):
    """With vague terms excluded, 'recent'/'lately' resolve to None so the route
    never hard-filters on them (the recency boost handles ordering instead)."""
    assert resolve_relative_time(phrase, NOW, include_vague=False) is None


def test_include_vague_false_keeps_bounded_terms():
    """Bounded phrases are still resolved when vague terms are excluded."""
    assert resolve_relative_time("notes from last week", NOW, include_vague=False) == (
        "2026-06-01",
        "2026-06-07",
    )


def test_resolve_effective_dates_ignores_vague_recent():
    """The route-facing wrapper must not auto-apply a hard filter for vague
    'recent' (regression for over-aggressive auto-filtering)."""
    from api.utils.date_parser import resolve_effective_dates
    assert resolve_effective_dates("my most recent invoice") == (None, None)


def test_resolve_effective_dates_explicit_params_win():
    from api.utils.date_parser import resolve_effective_dates
    assert resolve_effective_dates("last week", "2026-01-01", None) == ("2026-01-01", None)
