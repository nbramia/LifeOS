"""
Tests for the get_message_history orchestrator tool's adaptive windowing.

Regression context: the tool used to clamp silently to the last 30 days when the
caller passed no dates. A message ~7 weeks old was therefore unreachable, and
because the failure text ("No messages found.") never named the window searched
— while person_info reported interaction counts over 90 days — the orchestrator
concluded the backend had a sync or permissions fault instead of widening.

These tests pin the fix: widen 90d -> 1y -> all history, report the window,
honor explicit dates, and cap the payload without misattributing messages.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import api.services.agent_tools as at
from api.services.agent_tools import (
    _MSG_HISTORY_CHAR_BUDGET,
    _split_to_budget,
    _tool_get_message_history,
    _trim_section,
    TOOL_DEFINITIONS,
    _TOOL_HANDLERS,
)

pytestmark = pytest.mark.unit

ENTITY = "11111111-2222-3333-4444-555555555555"


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


@pytest.fixture
def fake_sources(monkeypatch):
    """Stub both message sources so windowing is tested without real data.

    Records the start_date of every attempt so the widening ladder is visible.
    `state.ambiguous` defaults to False (a confident resolution); set it to
    True in a test to simulate entity_resolver reporting `fuzzy_ambiguous`.
    """
    state = SimpleNamespace(imessages=[], whatsapp=[], attempts=[], ambiguous=False)

    def fake_resolve(entity_id):
        if entity_id == ENTITY:
            return entity_id, state.ambiguous
        return None, False

    def fake_query(entity_id, search_term=None, start_date=None, end_date=None, limit=100):
        state.attempts.append(start_date)
        hits = list(state.imessages)
        if start_date:
            bound = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            hits = [m for m in hits if m[0] >= bound]
        if search_term:
            hits = [m for m in hits if search_term.lower() in m[1].lower()]
        hits = hits[-limit:]
        formatted = "\n".join(f"### {ts:%Y-%m-%d}\n- **00:00** <- {text}" for ts, text in hits)
        date_range = None
        if hits:
            date_range = {
                "start": min(h[0] for h in hits).isoformat(),
                "end": max(h[0] for h in hits).isoformat(),
            }
        return {"messages": hits, "formatted": formatted, "count": len(hits), "date_range": date_range}

    class FakeStore:
        def get_for_person(self, person_id, days_back, source_type, limit):
            bound = datetime.now(timezone.utc) - timedelta(days=days_back)
            return [
                SimpleNamespace(timestamp=ts, snippet=text)
                for ts, text in state.whatsapp
                if ts >= bound
            ][:limit]

    monkeypatch.setattr("api.services.imessage.resolve_entity_id_confidence", fake_resolve)
    monkeypatch.setattr("api.services.imessage.query_person_messages", fake_query)
    monkeypatch.setattr(
        "api.services.interaction_store.get_interaction_store", lambda: FakeStore()
    )
    monkeypatch.setattr(
        at, "_format_whatsapp_interactions",
        lambda items: "\n".join(f"### {i.timestamp:%Y-%m-%d}\n- **00:00** <- {i.snippet}" for i in items),
    )
    return state


def test_tool_is_registered():
    assert "get_message_history" in _TOOL_HANDLERS
    assert any(t["name"] == "get_message_history" for t in TOOL_DEFINITIONS)


def test_unresolvable_entity_is_reported():
    assert "Could not resolve" in _tool_get_message_history({"entity_id": "nope"})


class TestWidening:
    def test_finds_message_older_than_the_first_rung(self, fake_sources):
        """The original bug: a 49-day-old message with no dates supplied."""
        fake_sources.imessages = [(_days_ago(49), "Meet Robin Alex Doe - mom and baby happy")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "Robin Alex Doe" in out

    def test_stops_at_the_first_rung_that_hits(self, fake_sources):
        fake_sources.imessages = [(_days_ago(3), "hello")]
        _tool_get_message_history({"entity_id": ENTITY})
        assert len(fake_sources.attempts) == 1

    def test_walks_all_rungs_to_reach_old_history(self, fake_sources):
        fake_sources.imessages = [(_days_ago(900), "ancient")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "ancient" in out
        # 90d, 365d, then all-history (None)
        assert len(fake_sources.attempts) == 3
        assert fake_sources.attempts[-1] is None

    def test_reports_which_windows_were_empty(self, fake_sources):
        fake_sources.imessages = [(_days_ago(900), "ancient")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "nothing in last 90d, last 365d" in out
        assert "widened to all history" in out

    def test_no_widening_note_when_first_rung_hits(self, fake_sources):
        fake_sources.imessages = [(_days_ago(3), "hello")]
        assert "widened" not in _tool_get_message_history({"entity_id": ENTITY})

    def test_widens_for_whatsapp_only_contact(self, fake_sources):
        """Ladder must break on either source, not iMessage alone."""
        fake_sources.whatsapp = [(_days_ago(200), "wa only")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "wa only" in out

    def test_search_term_reaches_all_history(self, fake_sources):
        fake_sources.imessages = [(_days_ago(800), "the wedding was lovely"), (_days_ago(2), "hi")]
        out = _tool_get_message_history({"entity_id": ENTITY, "search_term": "wedding"})
        assert "wedding was lovely" in out


class TestExplicitDates:
    def test_explicit_start_date_disables_widening(self, fake_sources):
        fake_sources.imessages = [(_days_ago(200), "older")]
        start = _days_ago(10).strftime("%Y-%m-%d")
        out = _tool_get_message_history({"entity_id": ENTITY, "start_date": start})
        assert fake_sources.attempts == [start]
        assert "older" not in out

    def test_empty_explicit_window_suggests_dropping_dates(self, fake_sources):
        start = _days_ago(3).strftime("%Y-%m-%d")
        out = _tool_get_message_history({"entity_id": ENTITY, "start_date": start})
        assert "Retry without start_date" in out


class TestHonestEmpty:
    def test_empty_result_names_the_windows_searched(self, fake_sources):
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "last 90d, then last 365d, then all history" in out

    def test_empty_result_does_not_suggest_a_backend_fault(self, fake_sources):
        """The misdiagnosis this whole change exists to prevent."""
        out = _tool_get_message_history({"entity_id": ENTITY}).lower()
        for word in ("sync issue", "permission", "failed", "error", "unavailable"):
            assert word not in out

    def test_unmatched_search_term_suggests_dropping_it(self, fake_sources):
        fake_sources.imessages = [(_days_ago(5), "hello")]
        out = _tool_get_message_history({"entity_id": ENTITY, "search_term": "zzqqx"})
        assert "without search_term" in out


class TestAmbiguousResolution:
    """resolve_entity_id_confidence's second element (#346): when
    entity_resolver reports `fuzzy_ambiguous` (two-plus candidates scored close
    enough together that the top pick isn't reliably right), the tool must
    refuse the query outright rather than returning what may be the wrong
    person's private messages with a warning attached — a warning read after
    the fact doesn't undo content already in the model's context.
    """

    def test_ambiguous_match_is_refused_not_disclosed(self, fake_sources):
        fake_sources.ambiguous = True
        fake_sources.imessages = [(_days_ago(3), "hello")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "matched more than one person" in out
        assert "hello" not in out

    def test_ambiguous_match_never_queries_either_source(self, fake_sources):
        """No message content may reach the tool's output at all — assert the
        underlying sources were never even queried, not just that the reply
        omits them."""
        fake_sources.ambiguous = True
        fake_sources.imessages = [(_days_ago(3), "hello")]
        fake_sources.whatsapp = [(_days_ago(3), "wa hello")]
        _tool_get_message_history({"entity_id": ENTITY})
        assert fake_sources.attempts == []

    def test_ambiguous_match_names_the_term_and_the_remedy(self, fake_sources):
        fake_sources.ambiguous = True
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert ENTITY in out
        assert "person_info" in out
        assert "UUID" in out

    def test_confident_match_is_not_refused(self, fake_sources):
        fake_sources.ambiguous = False
        fake_sources.imessages = [(_days_ago(3), "hello")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "matched more than one person" not in out
        assert "hello" in out


class TestBudget:
    def test_small_result_is_not_trimmed(self, fake_sources):
        fake_sources.imessages = [(_days_ago(5), "short")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "trimmed" not in out

    def test_large_result_is_capped(self, fake_sources):
        fake_sources.imessages = [(_days_ago(5), "x" * 200) for _ in range(1000)]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert len(out) <= _MSG_HISTORY_CHAR_BUDGET + 600
        assert "trimmed" in out

    def test_trim_keeps_the_most_recent_messages(self, fake_sources):
        fake_sources.imessages = [(_days_ago(400), "OLDEST " + "x" * 200)] + [
            (_days_ago(2), "y" * 200) for _ in range(500)
        ] + [(_days_ago(1), "NEWEST-MARKER")]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "NEWEST-MARKER" in out
        assert "OLDEST" not in out

    def test_both_source_labels_survive_trimming(self, fake_sources):
        fake_sources.imessages = [(_days_ago(5), "x" * 200) for _ in range(500)]
        fake_sources.whatsapp = [(_days_ago(5), "y" * 200) for _ in range(500)]
        out = _tool_get_message_history({"entity_id": ENTITY})
        assert "## iMessage" in out and "## WhatsApp" in out
        assert len(out) <= _MSG_HISTORY_CHAR_BUDGET + 600

    def test_trimmed_sections_start_at_a_date_header(self, fake_sources):
        """A message shown without its date header could be misdated."""
        fake_sources.imessages = [(_days_ago(5), "x" * 200) for _ in range(500)]
        fake_sources.whatsapp = [(_days_ago(5), "y" * 200) for _ in range(500)]
        out = _tool_get_message_history({"entity_id": ENTITY})
        for label in ("## iMessage", "## WhatsApp"):
            body = out.split(label, 1)[1].split("\n\n", 1)[1]
            assert body.lstrip().startswith("### "), f"{label} section starts mid-message"


class TestBudgetHelpers:
    def test_trim_section_returns_short_input_unchanged(self):
        assert _trim_section("### 2020-01-01\n- hi", 1000) == "### 2020-01-01\n- hi"

    def test_trim_section_aligns_to_header(self):
        text = "\n".join(f"### 2020-01-{d:02d}\n- **00:00** {'z' * 50}" for d in range(1, 28))
        assert _trim_section(text, 300).startswith("### ")

    def test_quiet_source_donates_unused_budget(self):
        big = "### 2020-01-01\n" + "a" * (_MSG_HISTORY_CHAR_BUDGET * 2)
        small = "### 2020-01-01\n- tiny"
        kept_big, kept_small, trimmed = _split_to_budget(big, small)
        assert trimmed
        assert kept_small == small
        # More than an even split, since the quiet source gave back its share.
        assert len(kept_big) > _MSG_HISTORY_CHAR_BUDGET // 2

    def test_no_trim_when_combined_fits(self):
        a, b, trimmed = _split_to_budget("short a", "short b")
        assert (a, b, trimmed) == ("short a", "short b", False)
