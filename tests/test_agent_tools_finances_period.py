"""
Tests for period labelling in the finance summary actions (cashflow, budgets).

Regression context: same bug class as tests/test_agent_tools_scope_widening.py,
but the harm is inverted. Those tools denied data that was present; these two
answered confidently with a figure covering a window nobody asked for. Both
actions defaulted `start_date` to the 1st of the current month and then printed
Income / Expenses / Net Savings / Savings Rate with no period anywhere in the
output — on the 2nd of a month, a two-day total with a savings rate beside it
and nothing to cue the reader that the period was partial. The category
breakdown was cut to the top ten silently, so the 11th category read as zero
spend, and an empty budget month reported "No budgets found." as though none
were configured.

These tests pin the fix: every figure carries its period, the breakdown
discloses its own cut, an empty budget result names the period it searched
instead of asserting absence, and an unparseable date is dropped and disclosed.
"""
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from api.services.agent_tools import (
    _CASHFLOW_CATEGORY_CAP,
    _tool_search_finances,
)

pytestmark = pytest.mark.unit

# Phrases that would tell the orchestrator the backend broke. An empty result is
# a fact about the data, so none of these may appear in one.
FAULT_WORDS = ("sync issue", "permission", "failed", "error", "unavailable")

# Claims an empty budget result must never make: a bounded period being empty
# says nothing about whether budgets exist at all.
_CONFIGURED_CLAIM = re.compile(
    r"no budgets (?:are|were|have been)?\s*(?:configured|set up|set)", re.I
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _first_of_this_month() -> str:
    return datetime.now().replace(day=1).strftime("%Y-%m-%d")


@pytest.fixture
def fake_monarch(monkeypatch):
    """Stub the Monarch client, recording the bounds of every summary call.

    Raises on a one-sided range exactly as monarchmoney does ("You must specify
    both a startDate and endDate, not just one of them"), so any code path that
    sends a bare start_date fails loudly here instead of in production.
    """
    state = SimpleNamespace(
        summary={"total_income": 0.0, "total_expenses": 0.0, "savings": 0.0, "savings_rate": 0.0},
        categories=[],
        budgets=[],
        calls=[],
    )

    def _record(method, start_date, end_date):
        if (start_date is None) != (end_date is None):
            raise AssertionError(
                f"{method} got a one-sided range ({start_date!r}, {end_date!r}); "
                "Monarch rejects that outright"
            )
        state.calls.append({"method": method, "start": start_date, "end": end_date})

    class FakeMonarchClient:
        async def get_cashflow_summary(self, start_date=None, end_date=None):
            _record("cashflow_summary", start_date, end_date)
            return dict(state.summary)

        async def get_cashflow_by_category(self, start_date=None, end_date=None):
            _record("cashflow_by_category", start_date, end_date)
            return [dict(c) for c in state.categories]

        async def get_budgets(self, start_date=None, end_date=None):
            _record("budgets", start_date, end_date)
            return [dict(b) for b in state.budgets]

    monkeypatch.setattr("api.services.monarch.get_monarch_client", lambda: FakeMonarchClient())
    return state


async def _cashflow(**inp) -> str:
    return await _tool_search_finances({"action": "cashflow", **inp})


async def _budgets(**inp) -> str:
    return await _tool_search_finances({"action": "budgets", **inp})


def _category(name: str, amount: float) -> dict:
    return {"category": name, "amount": amount}


def _categories(n: int) -> list[dict]:
    """n synthetic categories, largest first (the real client sorts this way)."""
    return [_category(f"Category {i:02d}", 1000.0 - i) for i in range(n)]


def _budget(category: str, budgeted: float, actual: float) -> dict:
    return {
        "category": category,
        "budgeted": budgeted,
        "actual": actual,
        "remaining": budgeted - actual,
    }


class TestCashflowPeriodLabel:
    """The core fix: no figure is printed without the window it covers."""

    async def test_defaulted_period_is_stated(self, fake_monarch):
        out = await _cashflow()
        assert f"**Period**: {_first_of_this_month()} to {_today()}" in out

    async def test_defaulted_period_says_it_is_partial(self, fake_monarch):
        """A month-to-date total is the confidently-wrong-number case."""
        out = await _cashflow()
        assert "month-to-date by default" in out
        assert "part of the month" in out

    async def test_explicit_period_is_stated(self, fake_monarch):
        out = await _cashflow(start_date="2026-03-01", end_date="2026-03-31")
        assert "**Period**: 2026-03-01 to 2026-03-31" in out

    async def test_explicit_period_is_not_labelled_a_default(self, fake_monarch):
        out = await _cashflow(start_date="2026-03-01", end_date="2026-03-31")
        assert "default" not in out

    async def test_period_precedes_the_figures(self, fake_monarch):
        """Scope after the numbers is scope the reader has already skipped."""
        out = await _cashflow()
        assert out.index("**Period**") < out.index("**Savings Rate**")

    async def test_explicit_period_is_sent_to_monarch_verbatim(self, fake_monarch):
        await _cashflow(start_date="2026-03-01", end_date="2026-03-31")
        assert fake_monarch.calls
        for call in fake_monarch.calls:
            assert (call["start"], call["end"]) == ("2026-03-01", "2026-03-31")

    async def test_missing_end_bound_is_filled_in(self, fake_monarch):
        """Monarch rejects a one-sided range, so the stated end must be real."""
        await _cashflow(start_date="2026-03-01")
        for call in fake_monarch.calls:
            assert (call["start"], call["end"]) == ("2026-03-01", _today())

    async def test_summary_and_breakdown_cover_the_same_window(self, fake_monarch):
        """A label over two different windows would be a label over neither."""
        fake_monarch.categories = _categories(3)
        await _cashflow()
        bounds = {(c["start"], c["end"]) for c in fake_monarch.calls}
        assert len(fake_monarch.calls) == 2
        assert len(bounds) == 1


class TestSummaryEndDateAnchor:
    """A defaulted start counts back from end_date, not from today.

    Pairing this month's 1st with a past end_date builds an inverted window that
    can only return zeros — which then prints as a real $0.00 month under a
    period label that reads backwards.
    """

    async def test_defaulted_start_anchors_to_the_end_date(self, fake_monarch):
        out = await _cashflow(end_date="2026-03-31")
        assert "**Period**: 2026-03-01 to 2026-03-31" in out

    async def test_anchored_default_is_disclosed(self, fake_monarch):
        out = await _cashflow(end_date="2026-03-31")
        assert "start defaulted to the 1st of that month" in out

    async def test_window_is_never_inverted(self, fake_monarch):
        await _cashflow(end_date="2026-03-31")
        assert fake_monarch.calls
        for call in fake_monarch.calls:
            assert call["start"] <= call["end"], call

    async def test_budgets_anchor_the_same_way(self, fake_monarch):
        fake_monarch.budgets = [_budget("Groceries", 600.0, 120.0)]
        out = await _budgets(end_date="2026-03-31")
        assert "2026-03-01 to 2026-03-31" in out


class TestCashflowBreakdownTruncation:
    async def test_truncation_is_disclosed_when_the_cap_binds(self, fake_monarch):
        fake_monarch.categories = _categories(_CASHFLOW_CATEGORY_CAP + 3)
        out = await _cashflow()
        assert "and 3 more categories" in out
        assert f"largest {_CASHFLOW_CATEGORY_CAP} of {_CASHFLOW_CATEGORY_CAP + 3}" in out

    async def test_truncated_note_discloses_without_claiming_amounts(self, fake_monarch):
        """A category outside the cut must not read as zero spend.

        An earlier version said "the rest are not zero", which the code cannot
        establish — an omitted category may legitimately be 0. The note now
        discloses the omission by count and asserts nothing about the amounts.
        """
        fake_monarch.categories = _categories(_CASHFLOW_CATEGORY_CAP + 1)
        out = await _cashflow()
        assert "1 more categories not shown" in out
        assert "not zero" not in out

    async def test_no_note_when_exactly_at_the_cap(self, fake_monarch):
        """At the cap nothing was cut, so there is nothing to disclose."""
        fake_monarch.categories = _categories(_CASHFLOW_CATEGORY_CAP)
        out = await _cashflow()
        assert "more categories" not in out
        assert "truncated" not in out

    async def test_no_note_below_the_cap(self, fake_monarch):
        fake_monarch.categories = _categories(3)
        out = await _cashflow()
        assert "more categories" not in out
        assert "truncated" not in out

    async def test_kept_categories_are_the_largest(self, fake_monarch):
        fake_monarch.categories = _categories(_CASHFLOW_CATEGORY_CAP + 2)
        out = await _cashflow()
        assert "Category 00" in out
        assert f"Category {_CASHFLOW_CATEGORY_CAP + 1:02d}" not in out


class TestCashflowHonestEmpty:
    async def test_empty_breakdown_names_the_period(self, fake_monarch):
        out = await _cashflow()
        assert "No spending in this period." in out
        assert f"{_first_of_this_month()} to {_today()}" in out

    async def test_unclassified_expenses_are_not_called_no_spending(self, fake_monarch):
        """Saying "no spending" here would contradict the Expenses line above it."""
        fake_monarch.summary = {
            "total_income": 5000.0, "total_expenses": 1200.0, "savings_rate": 0.76,
        }
        fake_monarch.categories = []
        out = await _cashflow()
        assert "No spending in this period." not in out
        assert "unavailable, not" in out

    async def test_empty_breakdown_does_not_suggest_a_backend_fault(self, fake_monarch):
        out = (await _cashflow()).lower()
        for word in FAULT_WORDS:
            assert word not in out


class TestBudgetsHonestEmpty:
    async def test_empty_result_names_the_defaulted_period(self, fake_monarch):
        out = await _budgets()
        assert f"Searched {_first_of_this_month()} to {_today()}" in out

    async def test_empty_result_names_an_explicit_period(self, fake_monarch):
        out = await _budgets(start_date="2026-03-01", end_date="2026-03-31")
        assert "Searched 2026-03-01 to 2026-03-31" in out

    async def test_empty_result_does_not_claim_none_are_configured(self, fake_monarch):
        """The period searched is always bounded, so absence is never established."""
        out = await _budgets()
        assert not _CONFIGURED_CLAIM.search(out), out

    async def test_empty_result_separates_the_period_from_the_setup(self, fake_monarch):
        out = await _budgets()
        assert "hasn't populated yet" in out

    async def test_empty_result_does_not_suggest_a_backend_fault(self, fake_monarch):
        out = (await _budgets()).lower()
        for word in FAULT_WORDS:
            assert word not in out

    async def test_empty_explicit_period_does_not_suggest_a_backend_fault(self, fake_monarch):
        out = (await _budgets(start_date="2026-03-01", end_date="2026-03-31")).lower()
        for word in FAULT_WORDS:
            assert word not in out


class TestBudgetsPopulated:
    async def test_period_is_stated_above_the_table(self, fake_monarch):
        fake_monarch.budgets = [_budget("Groceries", 600.0, 120.0)]
        out = await _budgets()
        assert f"Budgets for {_first_of_this_month()} to {_today()}" in out
        assert out.index("Budgets for") < out.index("| Category |")

    async def test_rows_keep_all_four_columns(self, fake_monarch):
        fake_monarch.budgets = [_budget("Groceries", 600.0, 120.0)]
        out = await _budgets()
        assert "| Groceries | $600.00 | $120.00 | $480.00 |" in out


class TestSummaryUnparseableDatesAreRefused:
    """An unreadable date on an aggregate is refused, not dropped.

    The transactions branch drops one and says so, which is survivable there
    because every row it prints carries its own date — a wrong period shows up in
    the output. An aggregate has no such cue: the number IS the whole answer, and
    a note beside a real total is easily reported without it. So cashflow and
    budgets make no API call at all when an explicitly supplied date can't be
    read.
    """

    async def test_unparseable_start_is_refused(self, fake_monarch):
        out = await _cashflow(start_date="last month")
        assert "could not read start_date='last month'" in out
        assert not fake_monarch.calls, "no figures may be fetched for a guessed period"

    async def test_unparseable_end_is_refused(self, fake_monarch):
        out = await _cashflow(start_date="2026-03-01", end_date="soon")
        assert "could not read end_date='soon'" in out
        assert not fake_monarch.calls

    async def test_both_unparseable_dates_are_named(self, fake_monarch):
        out = await _cashflow(start_date="q1", end_date="q2")
        assert "start_date='q1'" in out
        assert "end_date='q2'" in out

    async def test_refusal_is_named_as_a_bad_argument_not_an_empty_period(
        self, fake_monarch
    ):
        out = await _cashflow(start_date="last month")
        assert "bad date argument" in out
        assert "NOT an empty period" in out

    async def test_refusal_says_why_a_note_would_not_have_been_enough(
        self, fake_monarch
    ):
        """The reason this differs from transactions: an aggregate has no cue."""
        out = await _cashflow(start_date="last month")
        assert "reads exactly like the one that was asked for" in out

    async def test_refusal_prints_no_figures(self, fake_monarch):
        fake_monarch.summary = {
            "total_income": 5000.0, "total_expenses": 3200.0, "savings_rate": 0.36,
        }
        out = await _cashflow(start_date="last month")
        assert "$" not in out
        assert "Savings Rate" not in out

    async def test_refusal_asks_for_the_expected_format(self, fake_monarch):
        out = await _cashflow(start_date="last month")
        assert "YYYY-MM-DD" in out

    async def test_no_refusal_when_dates_are_valid(self, fake_monarch):
        out = await _cashflow(start_date="2026-03-01", end_date="2026-03-31")
        assert "could not read" not in out
        assert fake_monarch.calls

    async def test_no_refusal_when_dates_are_absent(self, fake_monarch):
        out = await _cashflow()
        assert "could not read" not in out
        assert fake_monarch.calls

    async def test_budgets_refuse_the_same_way(self, fake_monarch):
        fake_monarch.budgets = [_budget("Groceries", 600.0, 120.0)]
        out = await _budgets(start_date="whenever")
        assert "could not read start_date='whenever'" in out
        assert not fake_monarch.calls
        assert "| Groceries |" not in out

    async def test_budgets_refuse_an_unparseable_end(self, fake_monarch):
        out = await _budgets(end_date="soon")
        assert "could not read end_date='soon'" in out
        assert not fake_monarch.calls

    async def test_refusal_carries_no_backend_fault_language(self, fake_monarch):
        """A malformed argument is not a broken backend."""
        for out in (
            await _cashflow(start_date="q1", end_date="q2"),
            await _budgets(start_date="q1", end_date="q2"),
        ):
            lowered = out.lower()
            for word in FAULT_WORDS:
                assert word not in lowered, f"bad-date refusal implies a fault: {word!r}"

    async def test_transactions_still_drop_and_disclose(self, fake_monarch, monkeypatch):
        """The deliberate asymmetry: rows carry their own dates, so a note works.

        Refusing there would remove a working answer; the period is visible on
        every line printed.
        """
        async def _get_transactions(start_date=None, end_date=None, search="", limit=None):
            return [{
                "date": "2026-03-04", "merchant": "Synthetic Grocer",
                "category": "Groceries", "amount": -42.0,
            }]

        class FakeClient:
            get_transactions = staticmethod(_get_transactions)

        monkeypatch.setattr(
            "api.services.monarch.get_monarch_client", lambda: FakeClient()
        )
        out = await _tool_search_finances(
            {"action": "transactions", "start_date": "last month"}
        )
        assert "Ignored unparseable start_date='last month'" in out
        assert "Synthetic Grocer" in out


class TestSummaryFigures:
    """The figures themselves are unchanged — the period is what was missing."""

    async def test_figures_are_rendered_from_the_summary(self, fake_monarch):
        fake_monarch.summary = {
            "total_income": 5000.0,
            "total_expenses": 3200.0,
            "savings": 1800.0,
            "savings_rate": 0.36,
        }
        out = await _cashflow()
        assert "**Income**: $5,000.00" in out
        assert "**Expenses**: $3,200.00" in out
        assert "**Net Savings**: $1,800.00" in out
        assert "**Savings Rate**: 36.0%" in out


class TestSummaryDefaultBoundary:
    """The month-to-date default stays; only its silence was the defect."""

    async def test_default_start_is_the_first_of_the_month(self, fake_monarch):
        await _cashflow()
        first = _first_of_this_month()
        assert first.endswith("-01")
        for call in fake_monarch.calls:
            assert call["start"] == first

    async def test_default_end_is_today(self, fake_monarch):
        await _budgets()
        assert fake_monarch.calls[0]["end"] == _today()

    async def test_default_window_is_at_most_one_month(self, fake_monarch):
        await _cashflow()
        call = fake_monarch.calls[0]
        span = datetime.strptime(call["end"], "%Y-%m-%d") - datetime.strptime(
            call["start"], "%Y-%m-%d"
        )
        assert timedelta(0) <= span < timedelta(days=31)


class TestImpossibleWindowIsRefused:
    """A start after the end cannot contain anything, so it must not be queried.

    Anchoring a defaulted start to end_date fixes the defaulted case, but a
    caller-supplied future start_date — or bounds passed the wrong way round —
    still describes an impossible window. Querying it returns zeros that print as
    a real $0.00 period at 0%, which is the confidently-wrong-number failure this
    whole change exists to remove.
    """

    async def test_future_start_date_is_refused(self, fake_monarch):
        future = (datetime.now() + timedelta(days=9)).strftime("%Y-%m-%d")
        out = await _cashflow(start_date=future)
        assert "start is after the end" in out
        assert not fake_monarch.calls, "an impossible window must not be queried"

    async def test_reversed_bounds_are_refused(self, fake_monarch):
        out = await _cashflow(start_date="2026-09-01", end_date="2026-08-01")
        assert "start is after the end" in out
        assert not fake_monarch.calls

    async def test_budgets_refuses_the_same_window(self, fake_monarch):
        out = await _budgets(start_date="2026-09-01", end_date="2026-08-01")
        assert "start is after the end" in out
        assert not fake_monarch.calls

    async def test_refusal_is_named_as_a_bad_range_not_an_empty_period(self, fake_monarch):
        out = await _cashflow(start_date="2026-09-01", end_date="2026-08-01")
        assert "NOT an empty period" in out
        assert "$0.00" not in out

    async def test_refusal_carries_no_fault_language(self, fake_monarch):
        out = (await _cashflow(start_date="2026-09-01", end_date="2026-08-01")).lower()
        for word in FAULT_WORDS:
            assert word not in out, f"bad-range refusal implies a fault: {word!r}"

    async def test_a_valid_window_is_still_queried(self, fake_monarch):
        out = await _cashflow(start_date="2026-08-01", end_date="2026-08-11")
        assert "start is after the end" not in out
        assert fake_monarch.calls


class TestSavingsRate:
    """An undefined rate must not be printed as a number.

    The rate is derived from the two figures printed above it rather than taken
    from the upstream field: with no income it is undefined, and the upstream
    value's unit is ambiguous (a bare -25 could mean -25% or -2500%), so a
    magnitude heuristic guesses wrong on negative rates.
    """

    async def test_zero_income_reports_not_applicable(self, fake_monarch):
        fake_monarch.summary = {
            "total_income": 0.0, "total_expenses": 0.0, "savings_rate": 0,
        }
        out = await _cashflow()
        assert "**Savings Rate**: n/a (no income in this period)" in out
        assert "0.0%" not in out

    async def test_zero_income_with_spending_reports_not_applicable(self, fake_monarch):
        fake_monarch.summary = {
            "total_income": 0.0, "total_expenses": 200.0, "savings_rate": 0,
        }
        out = await _cashflow()
        assert "n/a (no income in this period)" in out

    async def test_rate_is_derived_from_the_printed_figures(self, fake_monarch):
        fake_monarch.summary = {
            "total_income": 5000.0, "total_expenses": 1000.0, "savings_rate": 0.99,
        }
        out = await _cashflow()
        # 4000/5000 = 80%, regardless of the upstream field's 0.99.
        assert "**Savings Rate**: 80.0%" in out

    async def test_overspending_reports_a_negative_rate(self, fake_monarch):
        fake_monarch.summary = {
            "total_income": 1000.0, "total_expenses": 1500.0, "savings_rate": -0.5,
        }
        out = await _cashflow()
        assert "**Savings Rate**: -50.0%" in out
