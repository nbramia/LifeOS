"""
Tests for adaptive search scope in the finances, calendar, email, and Slack tools.

Regression context: same bug class as tests/test_agent_tools_message_history.py.
Each of these tools silently clamped its scope — transactions to 30 days,
calendar keyword search to a fixed ±180 days, email to 5 results per account,
Slack to 10 — and then reported a bare "No X found." that never named what was
searched. The orchestrator could not tell "the data isn't there" from "I looked
in the wrong place", so it invented a backend fault for data that was present.

These tests pin the fix: widen when the caller stated no scope, honor an
explicit scope exactly, disclose truncation, and make every empty result name
the scope it searched instead of implying a fault.
"""
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import api.services.agent_tools as at
from api.services.agent_tools import (
    _CALENDAR_LADDER_DAYS,
    _CALENDAR_MAX_DAYS,
    _positive_int,
    _TXN_LADDER_DAYS,
    _TXN_ROW_CAP,
    _exhausted_note,
    _ladder_note,
    _parse_ymd,
    _tool_search_calendar,
    _tool_search_email,
    _tool_search_finances,
    _tool_search_slack,
    TOOL_DEFINITIONS,
    _TOOL_HANDLERS,
)

pytestmark = pytest.mark.unit

# Phrases that would tell the orchestrator the backend broke. An empty result is
# a fact about the data, so none of these may appear in one.
FAULT_WORDS = ("sync issue", "permission", "failed", "error", "unavailable")


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


def _ymd_days_ago(n: int) -> str:
    return _days_ago(n).strftime("%Y-%m-%d")


def _counts_outside_range(text: str, n: int) -> bool:
    """Does `text` attribute exactly n results to being outside the date range?

    Matches the fragment and its count with an optional verb ("3 outside",
    "3 fall outside"), so a harmless copy edit to the sentence can't fail a test
    that is really about the attribution being right.
    """
    return re.search(rf"\b{n} (?:\w+ )?outside that range", text) is not None


def _account(value: str = "personal"):
    """Stand-in for a GoogleAccount enum member; only `.value` is read."""
    return SimpleNamespace(value=value)


@pytest.fixture
def accounts(monkeypatch):
    """One configured Google account. Tests may append more in place."""
    configured = [_account("personal")]
    monkeypatch.setattr(at, "get_configured_accounts", lambda: list(configured))
    return configured


# ---------------------------------------------------------------------------
# search_finances — action="transactions"
# ---------------------------------------------------------------------------

def _txn(days_ago: int, merchant: str, amount: float, category: str = "Shopping") -> dict:
    return {
        "date": _ymd_days_ago(days_ago),
        "merchant": merchant,
        "category": category,
        "amount": amount,
    }


@pytest.fixture
def fake_monarch(monkeypatch):
    """Stub the Monarch client, recording the bounds of every attempt.

    Records each (start_date, end_date) pair so the widening ladder — and the
    both-bounds-or-neither rule the real API enforces — are observable.
    """
    state = SimpleNamespace(txns=[], calls=[])

    class FakeMonarchClient:
        async def get_transactions(
            self, start_date=None, end_date=None, search="", category=None, limit=None
        ):
            state.calls.append(
                {
                    "start": start_date, "end": end_date, "search": search,
                    "category": category, "limit": limit,
                }
            )
            hits = list(state.txns)
            if start_date:
                hits = [t for t in hits if t["date"] >= start_date]
            if end_date:
                hits = [t for t in hits if t["date"] <= end_date]
            if search:
                hits = [t for t in hits if search.lower() in t["merchant"].lower()]
            if category:
                hits = [t for t in hits if t["category"] == category]
            # Newest-first, matching the real client (orderBy="date", verified
            # descending), so the row cap drops the oldest rows.
            hits.sort(key=lambda t: t["date"], reverse=True)
            return hits[:limit] if limit else hits

    monkeypatch.setattr("api.services.monarch.get_monarch_client", lambda: FakeMonarchClient())
    return state


async def _transactions(**inp) -> str:
    return await _tool_search_finances({"action": "transactions", **inp})


class TestFinancesWidening:
    async def test_finds_transaction_just_outside_the_old_30d_clamp(self, fake_monarch):
        """The original bug: a 45-day-old charge with no start_date supplied."""
        fake_monarch.txns = [_txn(45, "Plum Grove Diner", -42.10)]
        out = await _transactions()
        assert "Plum Grove Diner" in out

    async def test_stops_at_the_first_rung_that_hits(self, fake_monarch):
        fake_monarch.txns = [_txn(10, "Nimbus Cleaners", -18.00)]
        await _transactions()
        assert len(fake_monarch.calls) == 1

    async def test_walks_all_rungs_to_reach_old_history(self, fake_monarch):
        fake_monarch.txns = [_txn(800, "Orbital Hardware Co", -310.55)]
        out = await _transactions()
        assert "Orbital Hardware Co" in out
        # 90d, 365d, then all history (both bounds dropped)
        assert len(fake_monarch.calls) == 3
        assert fake_monarch.calls[-1]["start"] is None

    async def test_ladder_rungs_match_the_declared_ladder(self, fake_monarch):
        fake_monarch.txns = [_txn(800, "Orbital Hardware Co", -310.55)]
        await _transactions()
        assert len(fake_monarch.calls) == len(_TXN_LADDER_DAYS)

    async def test_reports_which_windows_were_empty(self, fake_monarch):
        fake_monarch.txns = [_txn(800, "Orbital Hardware Co", -310.55)]
        out = await _transactions()
        assert "nothing in last 90d, last 365d" in out
        assert "widened to all history" in out

    async def test_no_widening_note_when_first_rung_hits(self, fake_monarch):
        fake_monarch.txns = [_txn(5, "Nimbus Cleaners", -18.00)]
        assert "widened" not in await _transactions()

    async def test_search_term_reaches_all_history(self, fake_monarch):
        fake_monarch.txns = [
            _txn(700, "Wexler Piano Tuning", -95.00),
            _txn(2, "Nimbus Cleaners", -18.00),
        ]
        out = await _transactions(search="Wexler")
        assert "Wexler Piano Tuning" in out
        assert "Nimbus Cleaners" not in out


class TestFinancesMonarchBounds:
    """Monarch rejects a one-sided range, so every attempt must send both or neither."""

    async def test_every_ladder_attempt_sends_both_bounds_or_neither(self, fake_monarch):
        await _transactions()
        assert fake_monarch.calls  # ladder actually ran
        for call in fake_monarch.calls:
            assert (call["start"] is None) == (call["end"] is None), call

    async def test_dated_rungs_fill_in_the_missing_end_bound(self, fake_monarch):
        await _transactions()
        today = datetime.now().strftime("%Y-%m-%d")
        for call in fake_monarch.calls[:-1]:
            assert call["end"] == today

    async def test_all_history_rung_drops_both_bounds(self, fake_monarch):
        await _transactions()
        assert fake_monarch.calls[-1]["start"] is None
        assert fake_monarch.calls[-1]["end"] is None

    async def test_end_date_alone_still_ladders_with_both_bounds(self, fake_monarch):
        """end_date is not a scope statement, so widening still applies."""
        end = _ymd_days_ago(1)
        fake_monarch.txns = [_txn(800, "Orbital Hardware Co", -310.55)]
        out = await _transactions(end_date=end)
        assert "Orbital Hardware Co" in out
        assert len(fake_monarch.calls) == 3
        last = fake_monarch.calls[-1]
        assert (last["start"], last["end"]) == ("1900-01-01", end)
        assert (last["search"], last["category"]) == ("", None)


class TestFinancesEndDateAnchor:
    """With a past end_date the ladder must count back from it, not from today.

    Counting from today would build an inverted start>end window that can only
    return zero rows — which the ladder note would then misreport as "nothing in
    the last 90d" when the window was never valid.
    """

    async def test_rungs_count_back_from_the_end_date(self, fake_monarch):
        end = _ymd_days_ago(400)
        await _transactions(end_date=end)
        anchor = datetime.strptime(end, "%Y-%m-%d")
        assert fake_monarch.calls[0]["start"] == (anchor - timedelta(days=90)).strftime("%Y-%m-%d")
        assert fake_monarch.calls[1]["start"] == (anchor - timedelta(days=365)).strftime("%Y-%m-%d")

    async def test_no_window_is_inverted(self, fake_monarch):
        await _transactions(end_date=_ymd_days_ago(400))
        for call in fake_monarch.calls:
            if call["start"] and call["end"]:
                assert call["start"] <= call["end"], call

    async def test_windows_are_labelled_with_the_anchor(self, fake_monarch):
        end = _ymd_days_ago(400)
        fake_monarch.txns = [_txn(900, "Orbital Hardware Co", -310.55)]
        out = await _transactions(end_date=end)
        assert "Orbital Hardware Co" in out
        assert f"nothing in last 90d before {end}, last 365d before {end}" in out
        assert f"widened to all history before {end}" in out

    async def test_empty_result_names_the_anchored_windows(self, fake_monarch):
        end = _ymd_days_ago(400)
        out = await _transactions(end_date=end)
        assert f"last 90d before {end}, then last 365d before {end}" in out
        assert f"then all history before {end}" in out


class TestFinancesRowCap:
    """A widened window can hold more rows than Monarch will return in one page."""

    async def test_row_cap_is_passed_on_every_attempt(self, fake_monarch):
        await _transactions()
        assert fake_monarch.calls
        for call in fake_monarch.calls:
            assert call["limit"] == _TXN_ROW_CAP

    @staticmethod
    def _overflowing_window(extra: int = 10) -> list:
        """More rows than the cap, all inside the first ladder rung."""
        return [
            _txn(i % 80 + 2, f"Merchant {i:04d}", -1.00)
            for i in range(_TXN_ROW_CAP + extra)
        ]

    async def test_cap_is_disclosed_when_hit(self, fake_monarch):
        fake_monarch.txns = self._overflowing_window()
        out = await _transactions()
        assert f"Fetched the {_TXN_ROW_CAP} most recent rows" in out
        assert "may be incomplete" in out
        assert "older ones were dropped" in out

    async def test_no_cap_note_below_the_cap(self, fake_monarch):
        fake_monarch.txns = [_txn(i + 1, f"Merchant {i:04d}", -1.00) for i in range(5)]
        out = await _transactions()
        assert "most recent rows" not in out

    async def test_cap_drops_the_oldest_rows_not_the_newest(self, fake_monarch):
        """The note claims the oldest go first, which only holds if rows arrive newest-first."""
        fake_monarch.txns = [
            _txn(1, "Newest Marker Shop", -1.00),
            *self._overflowing_window(),
        ]
        out = await _transactions()
        assert "Newest Marker Shop" in out
        assert f"Fetched the {_TXN_ROW_CAP} most recent rows" in out
        assert "may be incomplete" in out


class TestFinancesExplicitScope:
    async def test_explicit_start_date_disables_widening(self, fake_monarch):
        fake_monarch.txns = [_txn(200, "Orbital Hardware Co", -310.55)]
        start = _ymd_days_ago(10)
        out = await _transactions(start_date=start)
        assert len(fake_monarch.calls) == 1
        assert fake_monarch.calls[0]["start"] == start
        assert "Orbital Hardware Co" not in out

    async def test_empty_explicit_window_suggests_dropping_the_date(self, fake_monarch):
        out = await _transactions(start_date=_ymd_days_ago(3))
        assert "Retry without start_date" in out

    async def test_explicit_window_result_carries_no_widening_note(self, fake_monarch):
        fake_monarch.txns = [_txn(2, "Nimbus Cleaners", -18.00)]
        out = await _transactions(start_date=_ymd_days_ago(10))
        assert "widened" not in out


class TestFinancesUnparseableDates:
    async def test_unparseable_start_date_never_reaches_monarch(self, fake_monarch):
        await _transactions(start_date="last month")
        for call in fake_monarch.calls:
            assert call["start"] != "last month"

    async def test_unparseable_start_date_does_not_disable_widening(self, fake_monarch):
        """A garbage date must not be honored as an intentional scope."""
        fake_monarch.txns = [_txn(200, "Orbital Hardware Co", -310.55)]
        out = await _transactions(start_date="last month")
        assert "Orbital Hardware Co" in out
        assert len(fake_monarch.calls) == 2

    async def test_unparseable_date_is_disclosed_on_a_hit(self, fake_monarch):
        fake_monarch.txns = [_txn(5, "Nimbus Cleaners", -18.00)]
        out = await _transactions(start_date="last month")
        assert "Ignored unparseable start_date='last month'" in out
        assert "NOT scoped" in out

    async def test_unparseable_date_is_disclosed_on_an_empty_result(self, fake_monarch):
        out = await _transactions(end_date="soon")
        assert "Ignored unparseable end_date='soon'" in out

    async def test_a_valid_date_is_not_reported_as_ignored(self, fake_monarch):
        fake_monarch.txns = [_txn(5, "Nimbus Cleaners", -18.00)]
        out = await _transactions(start_date=_ymd_days_ago(10))
        assert "Ignored unparseable" not in out


class TestFinancesHonestEmpty:
    async def test_empty_result_names_the_windows_searched(self, fake_monarch):
        out = await _transactions()
        assert "last 90d, then last 365d, then all history" in out

    async def test_empty_result_does_not_suggest_a_backend_fault(self, fake_monarch):
        """The misdiagnosis this whole change exists to prevent."""
        out = (await _transactions()).lower()
        for word in FAULT_WORDS:
            assert word not in out

    async def test_unmatched_search_suggests_dropping_it(self, fake_monarch):
        fake_monarch.txns = [_txn(5, "Nimbus Cleaners", -18.00)]
        out = await _transactions(search="zzqqx")
        assert "retry without the search term" in out.lower()

    async def test_empty_result_names_the_category_filter(self, fake_monarch):
        out = await _transactions(category="Veterinary")
        assert "'Veterinary'" in out
        for word in FAULT_WORDS:
            assert word not in out.lower()


# ---------------------------------------------------------------------------
# search_calendar
# ---------------------------------------------------------------------------

def _event(days_ago: int, title: str, **extra):
    return SimpleNamespace(
        title=title,
        start_time=_days_ago(days_ago),
        source_account=extra.get("source_account", "personal"),
        attendees=extra.get("attendees", []),
        location=extra.get("location", ""),
    )


@pytest.fixture
def fake_calendar(monkeypatch, accounts):
    """Stub CalendarService, recording the days_back of every search attempt."""
    state = SimpleNamespace(events=[], searches=[], range_calls=[], upcoming_calls=[])

    class FakeCalendarService:
        def __init__(self, account):
            self.account = account

        def search_events(self, query=None, attendee=None, days_back=30,
                          days_forward=30, calendar_id="primary"):
            state.searches.append({"query": query, "days_back": days_back,
                                   "days_forward": days_forward})
            now = datetime.now(timezone.utc)
            lo = now - timedelta(days=days_back)
            hi = now + timedelta(days=days_forward)
            return [
                e for e in state.events
                if lo <= e.start_time <= hi
                and (not query or query.lower() in e.title.lower())
            ]

        def get_events_in_range(self, start, end):
            state.range_calls.append((start, end))
            return []

        def get_upcoming_events(self, days=7, max_results=15):
            state.upcoming_calls.append({"days": days, "max_results": max_results})
            return []

    monkeypatch.setattr("api.services.calendar.CalendarService", FakeCalendarService)
    return state


class TestCalendarWidening:
    async def test_finds_event_beyond_the_old_fixed_180d_window(self, fake_calendar):
        """The clamp that hid it: search_events was pinned to ±180 days."""
        fake_calendar.events = [_event(250, "Quarterly Review with Team Aster")]
        out = await _tool_search_calendar({"query": "Aster"})
        assert "Team Aster" in out

    async def test_stops_at_the_first_rung_that_hits(self, fake_calendar):
        fake_calendar.events = [_event(20, "Coffee with Rowan Placeholder")]
        await _tool_search_calendar({"query": "Rowan"})
        assert len(fake_calendar.searches) == 1
        assert fake_calendar.searches[0]["days_back"] == _CALENDAR_LADDER_DAYS[0]

    async def test_walks_all_rungs_to_reach_old_events(self, fake_calendar):
        fake_calendar.events = [_event(1000, "Housewarming at Fig Lane")]
        out = await _tool_search_calendar({"query": "Housewarming"})
        assert "Fig Lane" in out
        assert [s["days_back"] for s in fake_calendar.searches] == list(_CALENDAR_LADDER_DAYS)

    async def test_window_is_symmetric_on_every_rung(self, fake_calendar):
        await _tool_search_calendar({"query": "nothing-matches-this"})
        for search in fake_calendar.searches:
            assert search["days_back"] == search["days_forward"]

    async def test_reports_which_windows_were_empty(self, fake_calendar):
        fake_calendar.events = [_event(1000, "Housewarming at Fig Lane")]
        out = await _tool_search_calendar({"query": "Housewarming"})
        assert "nothing in ±180d, ±365d" in out
        assert "widened to ±1095d" in out

    async def test_no_widening_note_when_first_rung_hits(self, fake_calendar):
        fake_calendar.events = [_event(20, "Coffee with Rowan Placeholder")]
        out = await _tool_search_calendar({"query": "Rowan"})
        assert "widened" not in out


class TestCalendarExplicitScope:
    async def test_explicit_days_range_is_honored_exactly(self, fake_calendar):
        fake_calendar.events = [_event(300, "Housewarming at Fig Lane")]
        out = await _tool_search_calendar({"query": "Housewarming", "days_range": 7})
        assert len(fake_calendar.searches) == 1
        attempt = fake_calendar.searches[0]
        assert (attempt["query"], attempt["days_back"], attempt["days_forward"]) == (
            "Housewarming", 7, 7,
        )
        assert "Fig Lane" not in out

    async def test_explicit_days_range_result_carries_no_widening_note(self, fake_calendar):
        fake_calendar.events = [_event(3, "Coffee with Rowan Placeholder")]
        out = await _tool_search_calendar({"query": "Rowan", "days_range": 30})
        assert "widened" not in out

    async def test_empty_explicit_range_names_the_span_it_searched(self, fake_calendar):
        out = await _tool_search_calendar({"query": "Housewarming", "days_range": 7})
        assert "Searched ±7d." in out


class TestCalendarUnchangedBranches:
    async def test_date_ref_branch_does_not_ladder(self, fake_calendar):
        out = await _tool_search_calendar({"date_ref": "2026-03-14"})
        assert len(fake_calendar.range_calls) == 1
        assert fake_calendar.searches == []
        assert out == "No calendar events found."

    async def test_date_ref_uses_days_range_as_a_forward_span(self, fake_calendar):
        await _tool_search_calendar({"date_ref": "2026-03-14", "days_range": 5})
        start, end = fake_calendar.range_calls[0]
        assert (end - start).days == 5

    async def test_upcoming_branch_does_not_ladder(self, fake_calendar):
        out = await _tool_search_calendar({})
        assert fake_calendar.upcoming_calls == [{"days": 7, "max_results": 15}]
        assert fake_calendar.searches == []
        assert out == "No calendar events found."


class TestCalendarHonestEmpty:
    async def test_empty_result_names_the_windows_searched(self, fake_calendar):
        out = await _tool_search_calendar({"query": "Housewarming"})
        assert "Searched ±180d, then ±365d, then ±1095d." in out

    async def test_empty_result_names_the_query(self, fake_calendar):
        out = await _tool_search_calendar({"query": "Housewarming"})
        assert "'Housewarming'" in out

    async def test_empty_result_does_not_suggest_a_backend_fault(self, fake_calendar):
        out = (await _tool_search_calendar({"query": "Housewarming"})).lower()
        for word in FAULT_WORDS:
            assert word not in out

    async def test_events_from_every_configured_account_are_merged(
        self, fake_calendar, accounts
    ):
        accounts.append(_account("work"))
        fake_calendar.events = [_event(20, "Coffee with Rowan Placeholder")]
        out = await _tool_search_calendar({"query": "Rowan"})
        assert "Rowan Placeholder" in out
        # One rung, queried once per account.
        assert len(fake_calendar.searches) == 2


# ---------------------------------------------------------------------------
# search_email
# ---------------------------------------------------------------------------

def _email(days_ago: int, subject: str, sender: str = "rowan@example.invalid"):
    return SimpleNamespace(
        sender=sender,
        to="me@example.invalid",
        subject=subject,
        date=_days_ago(days_ago),
        source_account="personal",
        body=f"Synthetic body for {subject}.",
        snippet="",
    )


@pytest.fixture
def fake_gmail(monkeypatch, accounts):
    """Stub GmailService, recording the kwargs of every search call.

    Applies after/before the way Gmail would (server-side) so date scoping is
    exercised, and caps at max_results so the truncation signal is real.
    """
    state = SimpleNamespace(messages=[], per_account={}, broken=set(), calls=[])

    class FakeGmailService:
        def __init__(self, account):
            self.account = account

        def search(self, keywords=None, from_email=None, to_email=None, after=None,
                   before=None, max_results=20, include_body=False):
            state.calls.append({
                "account": self.account.value, "keywords": keywords,
                "from_email": from_email, "to_email": to_email, "after": after,
                "before": before, "max_results": max_results,
                "include_body": include_body,
            })
            if self.account.value in state.broken:
                raise RuntimeError("synthetic account outage")
            msgs = state.per_account.get(self.account.value, state.messages)
            if after:
                msgs = [m for m in msgs if m.date.date() >= after.date()]
            if before:
                msgs = [m for m in msgs if m.date.date() <= before.date()]
            return list(msgs)[:max_results]

    monkeypatch.setattr("api.services.gmail.GmailService", FakeGmailService)
    return state


class TestEmailDateFilters:
    async def test_after_and_before_reach_gmail_as_datetimes(self, fake_gmail):
        fake_gmail.messages = [_email(40, "Nursery paint samples")]
        await _tool_search_email({"keywords": "nursery", "after": "2026-01-01",
                                  "before": "2026-02-01"})
        call = fake_gmail.calls[0]
        assert call["after"] == datetime(2026, 1, 1)
        assert call["before"] == datetime(2026, 2, 1)

    async def test_no_dates_means_no_bounds_reach_gmail(self, fake_gmail):
        """Unscoped must mean all of history, not a silent recent-only window."""
        await _tool_search_email({"keywords": "nursery"})
        assert fake_gmail.calls[0]["after"] is None
        assert fake_gmail.calls[0]["before"] is None

    async def test_old_email_is_reachable_with_no_dates(self, fake_gmail):
        fake_gmail.messages = [_email(400, "Nursery paint samples")]
        out = await _tool_search_email({"keywords": "nursery"})
        assert "Nursery paint samples" in out

    async def test_dates_actually_scope_the_result(self, fake_gmail):
        fake_gmail.messages = [_email(400, "Old thread"), _email(2, "Recent thread")]
        out = await _tool_search_email({"after": _ymd_days_ago(30)})
        assert "Recent thread" in out
        assert "Old thread" not in out


class TestEmailCapAndTruncation:
    async def test_default_max_results_is_fifteen(self, fake_gmail):
        await _tool_search_email({"keywords": "nursery"})
        assert fake_gmail.calls[0]["max_results"] == 15

    async def test_explicit_max_results_is_passed_through(self, fake_gmail):
        await _tool_search_email({"keywords": "nursery", "max_results": 50})
        assert fake_gmail.calls[0]["max_results"] == 50

    async def test_truncation_is_disclosed_when_the_cap_is_hit(self, fake_gmail):
        fake_gmail.messages = [_email(i + 1, f"Thread {i}") for i in range(20)]
        out = await _tool_search_email({"keywords": "thread"})
        assert "Capped at 15 per account" in out
        assert "Raise max_results" in out

    async def test_no_truncation_note_below_the_cap(self, fake_gmail):
        fake_gmail.messages = [_email(1, "Only thread")]
        out = await _tool_search_email({"keywords": "thread"})
        assert "Capped at" not in out

    async def test_truncation_on_one_of_several_accounts_is_disclosed(self, fake_gmail, accounts):
        accounts.append(_account("work"))
        fake_gmail.per_account = {
            "personal": [_email(1, "Only personal thread")],
            "work": [_email(i + 1, f"Work thread {i}") for i in range(15)],
        }
        out = await _tool_search_email({"keywords": "thread"})
        assert "Capped at 15 per account" in out

    async def test_a_broken_account_does_not_fake_truncation(self, fake_gmail, accounts):
        accounts.append(_account("work"))
        fake_gmail.broken = {"work"}
        fake_gmail.per_account = {"personal": [_email(1, "Only personal thread")]}
        out = await _tool_search_email({"keywords": "thread"})
        assert "Only personal thread" in out
        assert "Capped at" not in out


class TestEmailUnparseableDates:
    async def test_unparseable_after_is_dropped_before_reaching_gmail(self, fake_gmail):
        fake_gmail.messages = [_email(3, "Nursery paint samples")]
        out = await _tool_search_email({"keywords": "nursery", "after": "last tuesday"})
        assert fake_gmail.calls[0]["after"] is None
        assert "Nursery paint samples" in out

    async def test_unparseable_date_is_disclosed_on_a_hit(self, fake_gmail):
        fake_gmail.messages = [_email(3, "Nursery paint samples")]
        out = await _tool_search_email({"keywords": "nursery", "after": "last tuesday"})
        assert "Ignored unparseable after='last tuesday'" in out
        assert "NOT scoped" in out

    async def test_unparseable_date_is_disclosed_on_an_empty_result(self, fake_gmail):
        out = await _tool_search_email({"keywords": "nursery", "before": "sometime"})
        assert "Ignored unparseable before='sometime'" in out

    async def test_both_unparseable_dates_are_named(self, fake_gmail):
        fake_gmail.messages = [_email(3, "Nursery paint samples")]
        out = await _tool_search_email({"keywords": "nursery", "after": "??",
                                        "before": "2026-13-45"})
        assert "after='??'" in out
        assert "before='2026-13-45'" in out

    async def test_a_valid_date_is_not_reported_as_ignored(self, fake_gmail):
        fake_gmail.messages = [_email(3, "Nursery paint samples")]
        out = await _tool_search_email({"keywords": "nursery", "after": "2026-01-01"})
        assert "Ignored unparseable" not in out


class TestEmailHonestEmpty:
    async def test_empty_result_names_the_filters_applied(self, fake_gmail):
        out = await _tool_search_email({
            "keywords": "nursery", "from_email": "rowan@example.invalid",
            "to_email": "me@example.invalid",
        })
        assert "keywords='nursery'" in out
        assert "from='rowan@example.invalid'" in out
        assert "to='me@example.invalid'" in out

    async def test_empty_result_states_when_no_date_filter_applied(self, fake_gmail):
        """Without this the model can't tell an unscoped miss from a scoped one."""
        out = await _tool_search_email({"keywords": "nursery"})
        assert "no date filter" in out

    async def test_empty_result_names_the_date_span(self, fake_gmail):
        out = await _tool_search_email({"keywords": "nursery", "after": "2026-01-01",
                                        "before": "2026-02-01"})
        assert "dates 2026-01-01" in out
        assert "2026-02-01" in out
        assert "no date filter" not in out

    async def test_empty_result_names_a_one_sided_span(self, fake_gmail):
        out = await _tool_search_email({"keywords": "nursery", "after": "2026-01-01"})
        assert "dates 2026-01-01" in out

    async def test_empty_result_does_not_suggest_a_backend_fault(self, fake_gmail):
        out = (await _tool_search_email({"keywords": "nursery"})).lower()
        for word in FAULT_WORDS:
            assert word not in out

    async def test_empty_result_after_an_account_outage_still_blames_no_backend(
        self, fake_gmail
    ):
        """Even a genuine per-account failure must not be guessed at in the text."""
        fake_gmail.broken = {"personal"}
        out = (await _tool_search_email({"keywords": "nursery"})).lower()
        for word in FAULT_WORDS:
            assert word not in out


# ---------------------------------------------------------------------------
# search_slack
# ---------------------------------------------------------------------------

def _slack_msg(days_ago: int, content: str, *, channel="#synthetic-standup",
               user="Rowan Placeholder", timestamp=None) -> dict:
    return {
        "channel_name": channel,
        "user_name": user,
        "timestamp": _days_ago(days_ago).isoformat() if timestamp is None else timestamp,
        "content": content,
    }


@pytest.fixture
def fake_slack(monkeypatch):
    """Stub the Slack indexer, recording the top_k of every search."""
    state = SimpleNamespace(results=[], calls=[], enabled=True)

    class FakeIndexer:
        def search(self, query, top_k=20, **kwargs):
            state.calls.append({"query": query, "top_k": top_k})
            return list(state.results)[:top_k]

    monkeypatch.setattr("api.services.slack_integration.is_slack_enabled", lambda: state.enabled)
    monkeypatch.setattr("api.services.slack_indexer.get_slack_indexer", lambda: FakeIndexer())
    return state


class TestSlackTopK:
    def test_default_top_k_is_twenty(self, fake_slack):
        _tool_search_slack({"query": "release plan"})
        assert fake_slack.calls[0]["top_k"] == 20

    def test_explicit_top_k_is_passed_through(self, fake_slack):
        _tool_search_slack({"query": "release plan", "top_k": 75})
        assert fake_slack.calls[0]["top_k"] == 75

    def test_truncation_is_disclosed_when_top_k_is_hit(self, fake_slack):
        fake_slack.results = [_slack_msg(i + 1, f"note {i}") for i in range(25)]
        out = _tool_search_slack({"query": "note"})
        assert "Ranked top 20 only" in out
        assert "Raise top_k" in out

    def test_no_truncation_note_below_top_k(self, fake_slack):
        fake_slack.results = [_slack_msg(1, "note zero")]
        out = _tool_search_slack({"query": "note"})
        assert "Ranked top" not in out

    def test_not_configured_is_reported_plainly(self, fake_slack):
        fake_slack.enabled = False
        assert _tool_search_slack({"query": "release plan"}) == "Slack is not configured."


class TestSlackDateFilters:
    def test_dates_post_filter_the_ranked_page(self, fake_slack):
        fake_slack.results = [
            _slack_msg(400, "ancient note"),
            _slack_msg(10, "recent note"),
        ]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)})
        assert "recent note" in out
        assert "ancient note" not in out

    def test_before_bound_includes_the_whole_end_day(self, fake_slack):
        """before is a date, so a message later that same day must still match."""
        same_day = datetime.now(timezone.utc).replace(hour=22, minute=30)
        fake_slack.results = [_slack_msg(0, "late note", timestamp=same_day.isoformat())]
        out = _tool_search_slack({
            "query": "note", "before": same_day.strftime("%Y-%m-%d"),
        })
        assert "late note" in out

    def test_undateable_message_is_excluded_when_dates_are_given(self, fake_slack):
        """A message we can't date can't honestly be claimed to be in range."""
        fake_slack.results = [
            _slack_msg(1, "undateable note", timestamp="not-a-timestamp"),
            _slack_msg(1, "dateable note"),
        ]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)})
        assert "dateable note" in out
        assert "undateable note" not in out

    def test_undateable_message_is_kept_when_no_dates_are_given(self, fake_slack):
        fake_slack.results = [_slack_msg(1, "undateable note", timestamp="not-a-timestamp")]
        out = _tool_search_slack({"query": "note"})
        assert "undateable note" in out

    def test_undateable_exclusion_is_disclosed_alongside_hits(self, fake_slack):
        """Dropping a match silently would understate what the range might hold."""
        fake_slack.results = [
            _slack_msg(1, "dateable note"),
            _slack_msg(1, "undateable note", timestamp="not-a-timestamp"),
        ]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)})
        assert "dateable note" in out
        assert "1 matching message" in out
        assert "no readable timestamp" in out
        assert "in range" in out

    def test_no_undateable_note_when_every_match_could_be_dated(self, fake_slack):
        fake_slack.results = [_slack_msg(1, "dateable note")]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)})
        assert "no readable timestamp" not in out

    def test_unparseable_date_is_ignored_and_disclosed(self, fake_slack):
        fake_slack.results = [_slack_msg(400, "ancient note")]
        out = _tool_search_slack({"query": "note", "after": "last week"})
        assert "ancient note" in out, "an ignored filter must not silently scope results"
        assert "Ignored unparseable after='last week'" in out
        assert "NOT date-scoped" in out


class TestSlackHonestEmpty:
    def test_nothing_indexed_matches_is_named_as_such(self, fake_slack):
        out = _tool_search_slack({"query": "release plan"})
        assert "'release plan'" in out
        assert "nothing indexed matches" in out

    def test_all_matches_outside_the_range_is_a_distinct_branch(self, fake_slack):
        """Only here does a bigger top_k help — the query did match, the dates cut it."""
        fake_slack.results = [_slack_msg(400 + i, f"ancient note {i}") for i in range(3)]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)})
        assert "of 3 top-ranked matches" in out
        assert _counts_outside_range(out, 3)
        assert "raising top_k" in out

    def test_out_of_range_and_undateable_are_counted_separately(self, fake_slack):
        """"Outside the range" is established; "couldn't be dated" is not the same fact."""
        fake_slack.results = [
            _slack_msg(400, "ancient note"),
            _slack_msg(1, "undateable note", timestamp="not-a-timestamp"),
        ]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)})
        assert "of 2 top-ranked matches" in out
        assert _counts_outside_range(out, 1)
        assert "1 with no readable timestamp" in out

    def test_all_undateable_does_not_claim_they_were_out_of_range(self, fake_slack):
        fake_slack.results = [
            _slack_msg(1, f"undateable note {i}", timestamp="not-a-timestamp")
            for i in range(2)
        ]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)})
        assert "2 with no readable timestamp" in out
        assert "outside that range" not in out

    def test_the_two_empty_branches_are_not_the_same_text(self, fake_slack):
        after = _ymd_days_ago(30)
        nothing_indexed = _tool_search_slack({"query": "note", "after": after})
        fake_slack.results = [_slack_msg(400, "ancient note")]
        out_of_range = _tool_search_slack({"query": "note", "after": after})
        assert nothing_indexed != out_of_range
        assert _counts_outside_range(out_of_range, 1)
        assert "outside that range" not in nothing_indexed
        assert "nothing indexed matches" in nothing_indexed
        assert "nothing indexed matches" not in out_of_range

    def test_out_of_range_empty_names_the_span(self, fake_slack):
        fake_slack.results = [_slack_msg(400, "ancient note")]
        out = _tool_search_slack({
            "query": "note", "after": "2026-01-01", "before": "2026-02-01",
        })
        assert "between 2026-01-01" in out
        assert "2026-02-01" in out

    def test_out_of_range_empty_does_not_suggest_a_backend_fault(self, fake_slack):
        fake_slack.results = [_slack_msg(400, "ancient note")]
        out = _tool_search_slack({"query": "note", "after": _ymd_days_ago(30)}).lower()
        for word in FAULT_WORDS:
            assert word not in out

    def test_nothing_indexed_empty_does_not_suggest_a_backend_fault(self, fake_slack):
        """The misdiagnosis guard, with no exceptions.

        This branch used to deny a fault in the words "not that the search
        failed", which forced a carve-out in this invariant. The wording was
        changed to "not a sign the search broke" so the guard could stay literal
        and apply to every empty-result path identically — a blanket ban is a
        stronger contract than one with a permitted phrase inside it.
        """
        out = _tool_search_slack({"query": "release plan"}).lower()
        for word in FAULT_WORDS:
            assert word not in out, f"empty result implies a fault: {word!r}"

    def test_empty_result_states_the_coverage_limit_rather_than_a_fault(self, fake_slack):
        out = _tool_search_slack({"query": "release plan"})
        assert "have been indexed" in out


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class TestLadderNote:
    def test_empty_attempts_has_no_note(self):
        assert _ladder_note([]) == ""

    def test_single_attempt_has_no_note(self):
        """One rung means nothing was widened, so nothing to disclose."""
        assert _ladder_note(["last 90d"]) == ""

    def test_two_attempts_name_the_empty_one(self):
        assert _ladder_note(["last 90d", "last 365d"]) == (
            " (nothing in last 90d; widened to last 365d)"
        )

    def test_three_attempts_list_every_empty_rung(self):
        note = _ladder_note(["last 90d", "last 365d", "all history"])
        assert note == " (nothing in last 90d, last 365d; widened to all history)"

    def test_note_never_implies_a_fault(self):
        note = _ladder_note(["±180d", "±365d", "±1095d"]).lower()
        for word in FAULT_WORDS:
            assert word not in note


class TestExhaustedNote:
    def test_no_attempts_is_a_bare_statement(self):
        assert _exhausted_note("transactions", []) == "No transactions found."

    def test_attempts_are_listed_in_order(self):
        assert _exhausted_note("transactions", ["last 90d", "all history"]) == (
            "No transactions found. Searched last 90d, then all history."
        )

    def test_single_attempt_is_named(self):
        assert _exhausted_note("calendar events", ["±7d"]) == (
            "No calendar events found. Searched ±7d."
        )

    def test_hint_is_appended(self):
        out = _exhausted_note("transactions", ["last 90d"], "Try dropping the search.")
        assert out.endswith(" Try dropping the search.")

    def test_hint_without_attempts(self):
        assert _exhausted_note("transactions", [], "Try again.") == (
            "No transactions found. Try again."
        )

    def test_note_never_implies_a_fault(self):
        out = _exhausted_note("transactions", ["last 90d", "all history"]).lower()
        for word in FAULT_WORDS:
            assert word not in out


class TestParseYmd:
    def test_parses_a_well_formed_date(self):
        assert _parse_ymd("2026-03-14") == datetime(2026, 3, 14)

    @pytest.mark.parametrize("raw", [None, "", "last tuesday", "03/14/2026",
                                     "2026-13-01", "2026-02-30", "2026-03-14T10:00:00"])
    def test_unusable_input_yields_none(self, raw):
        assert _parse_ymd(raw) is None

    @pytest.mark.parametrize("raw", [20260314, 3.14, ["2026-03-14"], object()])
    def test_non_string_input_yields_none_instead_of_raising(self, raw):
        """A garbage type must cost the filter, not the whole search."""
        assert _parse_ymd(raw) is None

    def test_returns_a_naive_datetime(self):
        """Callers attach tzinfo themselves; a surprise tz would shift bounds."""
        assert _parse_ymd("2026-03-14").tzinfo is None


# ---------------------------------------------------------------------------
# Tool schema — the model can only use parameters that are advertised
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    @pytest.mark.parametrize(
        "name", ["search_finances", "search_calendar", "search_email", "search_slack"]
    )
    def test_tool_is_registered(self, name):
        assert name in _TOOL_HANDLERS
        assert any(t["name"] == name for t in TOOL_DEFINITIONS)

    @staticmethod
    def _schema(name: str) -> dict:
        return next(t for t in TOOL_DEFINITIONS if t["name"] == name)["input_schema"]

    @pytest.mark.parametrize("name", ["search_email", "search_slack"])
    def test_date_params_are_advertised(self, name):
        props = self._schema(name)["properties"]
        assert "after" in props
        assert "before" in props

    @pytest.mark.parametrize(
        "name,param",
        [("search_finances", "start_date"), ("search_calendar", "days_range")],
    )
    def test_widening_is_documented_on_the_scope_param(self, name, param):
        desc = self._schema(name)["properties"][param]["description"].lower()
        assert "widen" in desc
        assert "disables widening" in desc

    def test_email_documents_the_new_default_cap(self, name="search_email"):
        desc = self._schema(name)["properties"]["max_results"]["description"]
        assert "15" in desc

    def test_slack_documents_the_new_default_top_k(self):
        desc = self._schema("search_slack")["properties"]["top_k"]["description"]
        assert "20" in desc


# ---------------------------------------------------------------------------
# Argument hardening — the model fills these in, so garbled values arrive
# ---------------------------------------------------------------------------

class TestCalendarDaysRangeValidation:
    """An invalid days_range must not reach CalendarService.

    It used to: `timedelta(days="30")` raises inside search_events, the
    per-account handler swallowed it, and the tool reported "No calendar events
    found. Searched ±30d" — a scoped-looking empty for a search that never
    validly ran. Invalid values are now treated as *unstated* so the ladder
    applies, and the drop is disclosed.
    """

    async def test_numeric_string_is_coerced_and_honored(self, fake_calendar):
        await _tool_search_calendar({"query": "standup", "days_range": "30"})
        assert [s["days_back"] for s in fake_calendar.searches] == [30]

    async def test_numeric_string_is_not_reported_as_ignored(self, fake_calendar):
        out = await _tool_search_calendar({"query": "standup", "days_range": "30"})
        assert "Ignored days_range" not in out

    async def test_float_is_truncated_and_honored(self, fake_calendar):
        out = await _tool_search_calendar({"query": "standup", "days_range": 3.9})
        assert [s["days_back"] for s in fake_calendar.searches] == [3]
        assert "Ignored days_range" not in out

    @pytest.mark.parametrize("bad", [-30, 0, "soon", "", [], {}, "3.9"])
    async def test_invalid_range_falls_back_to_the_ladder(self, fake_calendar, bad):
        """Ignored means widened, not clamped to some nearby number."""
        out = await _tool_search_calendar({"query": "standup", "days_range": bad})
        assert [s["days_back"] for s in fake_calendar.searches] == list(
            _CALENDAR_LADDER_DAYS
        )
        assert "Ignored days_range" in out

    async def test_ignored_range_still_reaches_old_events(self, fake_calendar):
        """The payoff: falling back to the ladder must actually find the data."""
        fake_calendar.events = [_event(300, "Housewarming at Fig Lane")]
        out = await _tool_search_calendar({"query": "Housewarming", "days_range": 0})
        assert "Fig Lane" in out

    async def test_ignored_range_is_disclosed_on_a_hit_too(self, fake_calendar):
        """A note only on empty results would let a scoped-looking hit mislead."""
        fake_calendar.events = [_event(20, "Coffee with Rowan Placeholder")]
        out = await _tool_search_calendar({"query": "Rowan", "days_range": -30})
        assert "Rowan Placeholder" in out
        assert "Ignored days_range=-30" in out
        assert "NOT scoped" in out

    async def test_negative_range_is_not_clamped_to_one_day(self, fake_calendar):
        """Clamping would invent a ±1d scope the caller never asked for."""
        await _tool_search_calendar({"query": "standup", "days_range": -30})
        assert 1 not in [s["days_back"] for s in fake_calendar.searches]

    async def test_absent_range_is_not_an_error(self, fake_calendar):
        """Omission is the normal case and must not be reported as ignored."""
        out = await _tool_search_calendar({"query": "standup"})
        assert "Ignored days_range" not in out

    async def test_invalid_range_on_the_date_ref_branch_is_also_disclosed(
        self, fake_calendar
    ):
        """date_ref uses days_range as a forward span, so a bad value matters there too."""
        out = await _tool_search_calendar({"date_ref": "2026-03-14", "days_range": 0})
        start, end = fake_calendar.range_calls[0]
        assert (end - start).days == 1  # falls back to the documented default
        assert "Ignored days_range=0" in out

    async def test_none_range_is_treated_as_absent(self, fake_calendar):
        out = await _tool_search_calendar({"query": "standup", "days_range": None})
        assert [s["days_back"] for s in fake_calendar.searches] == list(
            _CALENDAR_LADDER_DAYS
        )
        assert "Ignored days_range" not in out


class TestCalendarAccountFailureDisclosure:
    """A failing account must not be rendered as an empty calendar.

    An expired token was logged and the tool still said "No calendar events
    found" — a genuine backend fault dressed as absence, which is exactly the
    misdiagnosis this change exists to prevent.
    """

    @pytest.fixture
    def broken_calendar(self, monkeypatch, accounts):
        state = SimpleNamespace(events_by_account={}, raising=set())

        class FlakyCalendarService:
            def __init__(self, account):
                self.account = account

            def search_events(self, query=None, days_back=30, days_forward=30, **kw):
                if self.account.value in state.raising:
                    raise RuntimeError("credentials expired")
                return list(state.events_by_account.get(self.account.value, []))

            def get_events_in_range(self, start, end):
                return []

            def get_upcoming_events(self, days=7, max_results=15):
                return []

        monkeypatch.setattr(
            "api.services.calendar.CalendarService", FlakyCalendarService
        )
        return state

    async def test_total_failure_is_disclosed_not_reported_as_empty(
        self, broken_calendar, accounts
    ):
        broken_calendar.raising = {"personal"}
        out = await _tool_search_calendar({"query": "standup"})
        assert "personal" in out
        assert "NOT an empty calendar" in out

    async def test_partial_failure_still_discloses_alongside_results(
        self, broken_calendar, accounts
    ):
        """The dangerous case: real events returned, one account silently missing."""
        accounts.append(_account("work"))
        broken_calendar.raising = {"work"}
        broken_calendar.events_by_account["personal"] = [
            SimpleNamespace(
                title="Synthetic standup",
                start_time=datetime.now(timezone.utc),
                attendees=[],
                location="",
                source_account="personal",
            )
        ]
        out = await _tool_search_calendar({"query": "standup"})
        assert "Synthetic standup" in out
        assert "Could not reach work" in out

    async def test_healthy_accounts_produce_no_failure_note(
        self, broken_calendar, accounts
    ):
        broken_calendar.raising = set()
        out = await _tool_search_calendar({"query": "standup"})
        assert "Could not reach" not in out

    async def test_only_the_failing_account_is_named(self, broken_calendar, accounts):
        accounts.append(_account("work"))
        broken_calendar.raising = {"work"}
        out = await _tool_search_calendar({"query": "standup"})
        reach = out.split("Could not reach", 1)[1]
        assert "work" in reach
        assert "personal" not in reach

    async def test_a_real_fault_is_named_rather_than_hidden(
        self, broken_calendar, accounts
    ):
        """The complement of the no-fault-words rule.

        An honest empty must never imply a fault — but a genuine failure must not
        be scrubbed into one either, or we are back to reporting absence for a
        calendar we could not read.
        """
        broken_calendar.raising = {"personal"}
        out = await _tool_search_calendar({"query": "standup"})
        assert "errored" in out
        assert any(word in out.lower() for word in FAULT_WORDS)


class TestCapNormalization:
    """max_results/top_k double as the truncation yardstick.

    A None or 0 from the model would both confuse the service layer and silently
    disable the truncation disclosure that compares against them.
    """

    @pytest.mark.parametrize(
        "raw,expected", [(None, 15), (0, 1), (-5, 1), ("25", 25), (99999, 100)]
    )
    async def test_email_cap_is_normalised_before_the_service_call(
        self, fake_gmail, raw, expected
    ):
        await _tool_search_email({"keywords": "invoice", "max_results": raw})
        assert fake_gmail.calls[0]["max_results"] == expected

    @pytest.mark.parametrize(
        "raw,expected", [(None, 20), (0, 1), (-5, 1), ("25", 25), (99999, 200)]
    )
    def test_slack_top_k_is_normalised_before_the_service_call(
        self, fake_slack, raw, expected
    ):
        _tool_search_slack({"query": "release plan", "top_k": raw})
        assert fake_slack.calls[0]["top_k"] == expected

    def test_zero_top_k_still_searches_rather_than_faking_an_empty(self, fake_slack):
        """A 0 cap must not manufacture "nothing matches" without looking."""
        fake_slack.results = [_slack_msg(1, "release plan notes")]
        out = _tool_search_slack({"query": "release plan", "top_k": 0})
        assert fake_slack.calls[0]["top_k"] >= 1
        assert "release plan notes" in out

    def test_normalised_top_k_still_drives_the_truncation_note(self, fake_slack):
        """The defect: a 0 cap made len(results) == top_k unreachable, so the
        "there may be more" disclosure silently never fired."""
        fake_slack.results = [_slack_msg(i + 1, f"note {i}") for i in range(5)]
        out = _tool_search_slack({"query": "note", "top_k": 0})
        assert "Ranked top 1 only" in out

    async def test_normalised_email_cap_still_drives_the_truncation_note(
        self, fake_gmail
    ):
        fake_gmail.messages = [_email(i + 1, f"Thread {i}") for i in range(5)]
        out = await _tool_search_email({"keywords": "thread", "max_results": 0})
        assert "Capped at 1 per account" in out

    async def test_email_cap_above_the_maximum_is_the_yardstick_used(self, fake_gmail):
        """Truncation must compare against the capped value, not the raw request."""
        fake_gmail.messages = [_email(i + 1, f"Thread {i}") for i in range(150)]
        out = await _tool_search_email({"keywords": "thread", "max_results": 99999})
        assert "Capped at 100 per account" in out


class TestPositiveIntHelper:
    @pytest.mark.parametrize("raw", [None, "abc", "", [], {}, object(), "3.9", "1e3"])
    def test_unusable_input_falls_back_to_the_default(self, raw):
        """int() rejects a decimal *string* even though it truncates a real float."""
        assert _positive_int(raw, 15, 100) == 15

    @pytest.mark.parametrize("raw", [0, -1, -999, False])
    def test_non_positive_is_raised_to_one(self, raw):
        assert _positive_int(raw, 15, 100) == 1

    def test_value_above_maximum_is_capped(self):
        assert _positive_int(10**9, 15, 100) == 100

    def test_numeric_string_is_accepted(self):
        assert _positive_int("42", 15, 100) == 42

    def test_float_is_truncated_toward_zero(self):
        assert _positive_int(7.9, 15, 100) == 7

    def test_value_inside_bounds_is_unchanged(self):
        assert _positive_int(50, 15, 100) == 50


class TestFinancesCategoryRowCapInteraction:
    """`category` is filtered client-side, downstream of the row cap.

    api/services/monarch.py sends limit/search/dates to the API but applies the
    category filter to the returned page. So a window dense enough to fill the
    cap yields a filtered handful drawn from only the newest N rows — and every
    wider ladder rung re-fetches that same newest page, since wider windows can
    only add older rows the cap already excluded. Before this was handled, a
    category with no recent activity produced "There are no transactions on
    record in category 'X'" over data that was present: the original
    misdiagnosis, reintroduced through the cap.
    """

    def _dense_recent(self, n: int = _TXN_ROW_CAP + 100) -> list:
        """More rows than the cap, all inside the first ladder rung."""
        return [_txn(i % 80 + 1, f"Merchant {i:04d}", -1.00) for i in range(n)]

    async def test_category_is_not_pushed_down_to_the_client(self, fake_monarch):
        """Filtering must happen where the cap is visible, or it can't be disclosed."""
        fake_monarch.txns = self._dense_recent()
        await _transactions(category="Veterinary")
        assert fake_monarch.calls
        assert all(call["category"] is None for call in fake_monarch.calls)

    async def test_capped_miss_is_not_reported_as_confirmed_absence(self, fake_monarch):
        fake_monarch.txns = self._dense_recent() + [
            _txn(800, "Cedar Veterinary Clinic", -240.00, category="Veterinary")
        ]
        out = await _transactions(category="Veterinary")
        assert "There are no transactions on record" not in out
        assert "NOT a confirmed absence" in out

    async def test_capped_miss_names_the_cap_and_the_category(self, fake_monarch):
        fake_monarch.txns = self._dense_recent()
        out = await _transactions(category="Veterinary")
        assert f"{_TXN_ROW_CAP} most recent rows" in out
        assert "'Veterinary'" in out

    async def test_capped_miss_does_not_claim_windows_it_never_reached(self, fake_monarch):
        """Once a rung fills the cap, wider rungs are pointless — and claiming
        them would say all history was searched when it never got past one page.
        """
        fake_monarch.txns = self._dense_recent()
        out = await _transactions(category="Veterinary")
        assert len(fake_monarch.calls) == 1
        assert "all history" not in out

    async def test_capped_miss_avoids_fault_language(self, fake_monarch):
        fake_monarch.txns = self._dense_recent()
        out = (await _transactions(category="Veterinary")).lower()
        for word in FAULT_WORDS:
            assert word not in out, f"capped miss implies a fault: {word!r}"

    async def test_uncapped_absence_is_still_stated_confidently(self, fake_monarch):
        """The honest-absence path must survive: a sparse window proves absence."""
        fake_monarch.txns = [_txn(5, "Nimbus Cleaners", -18.00)]
        out = await _transactions(category="Veterinary")
        assert "There are no transactions on record" in out
        assert "NOT a confirmed absence" not in out

    async def test_category_hit_inside_the_cap_is_returned(self, fake_monarch):
        fake_monarch.txns = [
            _txn(5, "Cedar Veterinary Clinic", -240.00, category="Veterinary"),
            _txn(6, "Nimbus Cleaners", -18.00),
        ]
        out = await _transactions(category="Veterinary")
        assert "Cedar Veterinary Clinic" in out
        assert "Nimbus Cleaners" not in out

    async def test_category_match_is_case_insensitive(self, fake_monarch):
        fake_monarch.txns = [
            _txn(5, "Cedar Veterinary Clinic", -240.00, category="Veterinary")
        ]
        out = await _transactions(category="veterinary")
        assert "Cedar Veterinary Clinic" in out


class TestCalendarDaysRangeUpperBound:
    """A huge days_range must not be blamed on the account.

    search_events does `now - timedelta(days=days_back)`, which raises
    OverflowError past a few million days. That lands in the per-account handler
    and — now that the handler speaks up instead of staying silent — was reported
    as an account needing re-authorisation. A bad argument dressed as expired
    credentials is this same misdiagnosis in a new costume.
    """

    @pytest.mark.parametrize("huge", [_CALENDAR_MAX_DAYS + 1, 10**9, 4 * 10**8])
    async def test_absurd_range_is_rejected_not_sent(self, fake_calendar, huge):
        out = await _tool_search_calendar({"query": "standup", "days_range": huge})
        assert [s["days_back"] for s in fake_calendar.searches] == list(
            _CALENDAR_LADDER_DAYS
        )
        assert "Ignored days_range" in out

    async def test_absurd_range_is_not_blamed_on_the_account(self, fake_calendar):
        out = await _tool_search_calendar({"query": "standup", "days_range": 10**9})
        assert "Could not reach" not in out
        assert "re-authorising" not in out

    async def test_the_bound_itself_is_still_accepted(self, fake_calendar):
        await _tool_search_calendar(
            {"query": "standup", "days_range": _CALENDAR_MAX_DAYS}
        )
        assert [s["days_back"] for s in fake_calendar.searches] == [_CALENDAR_MAX_DAYS]

    async def test_bound_does_not_overflow_timedelta(self):
        """The ceiling must be safely below where timedelta gives out."""
        assert datetime.now(timezone.utc) - timedelta(days=_CALENDAR_MAX_DAYS)


class TestCalendarTotalAccountFailure:
    """With every account down, nothing was searched — so nothing was established.

    `CalendarService._fetch_events` used to resolve its lazy `service` property
    inside its own `try/except Exception -> return []`, so an expired token came
    back as an empty list. The per-account disclosure could never fire for the
    case it exists for, and the widening ladder then reported "Searched ±180d,
    then ±365d, then ±1095d. Nothing on the calendar matches ..." over a broken
    connection — three windows it claimed to search and never did.
    """

    @pytest.fixture
    def flaky_calendar(self, monkeypatch, accounts):
        state = SimpleNamespace(raising=set(), events={}, attempts=0)

        class FlakyCalendarService:
            def __init__(self, account):
                self.name = account.value

            def search_events(self, query=None, days_back=30, days_forward=30, **kw):
                state.attempts += 1
                if self.name in state.raising:
                    raise RuntimeError("invalid_grant: token expired")
                return list(state.events.get(self.name, []))

            def get_events_in_range(self, start, end):
                return []

            def get_upcoming_events(self, days=7, max_results=15):
                return []

        monkeypatch.setattr(
            "api.services.calendar.CalendarService", FlakyCalendarService
        )
        return state

    def _event(self, title="Synthetic standup"):
        return SimpleNamespace(
            title=title,
            start_time=datetime.now(timezone.utc),
            attendees=[],
            location="",
            source_account="personal",
        )

    async def test_total_failure_leads_with_the_fault(self, flaky_calendar):
        flaky_calendar.raising = {"personal"}
        out = await _tool_search_calendar({"query": "standup"})
        assert out.startswith("Could not search the calendar")

    async def test_total_failure_does_not_claim_an_absence(self, flaky_calendar):
        """A denial with a footnote still reads as an answer."""
        flaky_calendar.raising = {"personal"}
        out = await _tool_search_calendar({"query": "standup"})
        assert "Nothing on the calendar matches" not in out
        assert "NOT an empty calendar" in out

    async def test_total_failure_stops_the_ladder(self, flaky_calendar):
        """Widening cannot help a broken connection, and claiming it lies."""
        flaky_calendar.raising = {"personal"}
        await _tool_search_calendar({"query": "standup"})
        assert flaky_calendar.attempts == 1

