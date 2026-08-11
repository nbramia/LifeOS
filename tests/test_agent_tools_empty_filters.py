"""
Tests for filter echoing on empty results from the workout, task, and vault tools.

Regression context (issue #535): same bug class as
tests/test_agent_tools_scope_widening.py, one notch milder. These three tools
applied the filters they were given, found nothing in the slice, and then
described the *whole record* as empty — "No sessions logged.", "No tasks
found.", "No vault results found." Asked whether any training happened in June,
the workout log answered that no sessions are logged, which reads as "you have
no training history"; a lift logged under another name read as never performed;
a slightly misspelled task context read as an empty task list.

No widening ladder belongs here — these corpora are cheap to re-query. The fix
is only that the reply stops asserting more than the search established, so
these tests pin the distinction that motivates the issue: a *filtered* empty
must name its filters, while an *unfiltered* empty may still say the record is
empty.

Second half of the same bug class (issue #537): a *non-empty* result that hides
its own ceiling, which is the more deceptive half — an empty answer invites a
follow-up, a list that looks complete does not. Two things are pinned below for
each capped path: the cap is disclosed when it binds (quoting the value actually
in effect, caller-supplied or not), and the default is large enough for the real
corpus. The measured facts behind each default are in the code comment beside it
and restated in the docstring of the test that pins it, so a silent reduction
fails a test rather than quietly undercounting again.
"""
import inspect
from types import SimpleNamespace

import pytest

import api.services.fitness_store as fs
import api.services.hybrid_search as hs_mod
import api.services.task_manager as tm_mod
from api.services import agent_tools
from api.services.agent_tools import (
    _VAULT_CHAR_BUDGET,
    _VAULT_TOP_K_DEFAULT,
    _VAULT_TOP_K_MAX,
    _WORKOUT_HISTORY_LIMIT,
    _WORKOUT_LIST_LIMIT,
    _WORKOUT_METRICS_LIMIT,
    _applied_filters,
    _tool_manage_tasks,
    _tool_manage_workouts,
    _tool_search_vault,
)
from api.services.fitness_store import FitnessStore
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit

# Phrases that would tell the orchestrator the backend broke. An empty result is
# a fact about the data, so none of these may appear in one.
FAULT_WORDS = ("sync issue", "permission", "failed", "error", "unavailable")


def assert_no_fault_language(text: str) -> None:
    lowered = text.lower()
    for word in FAULT_WORDS:
        assert word not in lowered, f"empty result blames a fault ({word!r}): {text!r}"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Real FitnessStore on a temp sqlite db — the service boundary, and the
    only way to exercise the real normalize_exercise title-casing."""
    instance = FitnessStore(db_path=str(tmp_path / "fitness.db"))
    monkeypatch.setattr(fs, "_store_instance", instance)
    return instance


@pytest.fixture
def store_limits(store, monkeypatch):
    """Records the limit each workout query was actually asked for, while the real
    store still answers.

    Output alone can't distinguish a raised default from a raised note: the cap is
    both the argument sent to the store and the yardstick the disclosure is keyed
    on, so both ends need pinning.
    """
    calls = []

    def spy(name):
        original = getattr(store, name)

        def wrapper(*args, **kwargs):
            calls.append({"query": name, "limit": kwargs.get("limit")})
            return original(*args, **kwargs)

        return wrapper

    for name in ("list_sessions", "exercise_history", "list_metrics", "daily_metric_totals"):
        monkeypatch.setattr(store, name, spy(name))
    return calls


def log_sessions(store, count, date="2026-06-10"):
    for _ in range(count):
        store.add_session(sets=[{"exercise": "flamingo hold", "reps": 3}], date=date)


def vault_chunk(i, content_len=200):
    """One synthetic result, tagged so a dropped chunk can be told from a kept one."""
    marker = f"chunk-{i:03d}"
    return {
        "file_name": f"Kayak Trip {i:03d}.md",
        "content": marker + "x" * max(0, content_len - len(marker)),
        "hybrid_score": 1.0 - i / 1000,
    }


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    manager = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json")
    # agent_tools imports get_task_manager lazily from this module, so patching
    # the module attribute is enough.
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)
    return manager


@pytest.fixture
def fake_vault(monkeypatch):
    """Stub HybridSearch, recording the top_k the tool actually asked for."""
    state = SimpleNamespace(calls=[], results=[])

    class FakeHybridSearch:
        def search(self, query, top_k=20, **kwargs):
            state.calls.append({"query": query, "top_k": top_k})
            return list(state.results)

    monkeypatch.setattr(hs_mod, "HybridSearch", FakeHybridSearch)
    return state


# ---------------------------------------------------------------------------
# manage_workouts — action="list"
# ---------------------------------------------------------------------------

class TestWorkoutListEmpty:
    def test_dated_empty_names_the_range_and_does_not_deny_the_log(self, store):
        out = _tool_manage_workouts({
            "action": "list", "date_start": "2026-06-01", "date_end": "2026-06-30",
        })
        assert "2026-06-01" in out and "2026-06-30" in out
        # The core claim: a June miss is not evidence the log is empty.
        assert "No sessions logged" not in out
        assert_no_fault_language(out)

    def test_dated_empty_while_sessions_exist_outside_the_range(self, store):
        # The shipped misdiagnosis in miniature: sessions exist, just not in the
        # window asked about.
        store.add_session(sets=[{"exercise": "flamingo hold", "reps": 3}], date="2026-01-05")
        out = _tool_manage_workouts({
            "action": "list", "date_start": "2026-06-01", "date_end": "2026-06-30",
        })
        assert "2026-06-01" in out and "2026-06-30" in out
        assert "No sessions logged" not in out
        assert_no_fault_language(out)

    def test_kind_filter_is_echoed(self, store):
        out = _tool_manage_workouts({"action": "list", "kind": "mobility"})
        assert "mobility" in out
        assert "No sessions logged" not in out
        assert_no_fault_language(out)

    def test_unfiltered_empty_may_state_the_log_is_empty(self, store):
        # The other direction. With nothing filtered, "no sessions are logged" is
        # exactly what the search established — this must not collapse into the
        # filtered wording, or the distinction stops carrying information.
        out = _tool_manage_workouts({"action": "list"})
        assert out == "No sessions logged."
        assert_no_fault_language(out)


class TestWorkoutListCap:
    def test_default_covers_a_real_month_of_training(self):
        """Pinned against the corpus: the log holds 16 sessions in June 2026 and 18
        in its densest 30-day window, so the old default of 10 answered "how did
        June go" from 10 of 16. Also pinned at or above the store's own
        list_sessions default, which the tool used to undercut fivefold."""
        service_default = inspect.signature(fs.FitnessStore.list_sessions).parameters["limit"].default
        assert _WORKOUT_LIST_LIMIT >= 18
        assert _WORKOUT_LIST_LIMIT >= service_default

    def test_the_new_default_is_what_the_store_is_asked_for(self, store, store_limits):
        _tool_manage_workouts({"action": "list"})
        assert store_limits == [{"query": "list_sessions", "limit": _WORKOUT_LIST_LIMIT}]

    def test_a_full_page_at_the_default_discloses_the_cap(self, store):
        log_sessions(store, _WORKOUT_LIST_LIMIT)
        out = _tool_manage_workouts({"action": "list"})
        assert f"Capped at {_WORKOUT_LIST_LIMIT} sessions" in out
        assert "older sessions may also match" in out
        # A full page is not a fault.
        assert_no_fault_language(out)

    def test_one_below_the_cap_says_nothing(self, store):
        log_sessions(store, _WORKOUT_LIST_LIMIT - 1)
        out = _tool_manage_workouts({"action": "list"})
        assert "Capped at" not in out

    def test_the_disclosure_quotes_the_caller_supplied_cap(self, store):
        log_sessions(store, 4)
        out = _tool_manage_workouts({"action": "list", "limit": 3})
        assert "Capped at 3 sessions" in out
        # The default is not what bound, so quoting it would misdirect the retry.
        assert f"Capped at {_WORKOUT_LIST_LIMIT}" not in out
        assert_no_fault_language(out)

    def test_an_unusable_cap_still_bounds_the_query(self, store, store_limits):
        # The model writes this argument. A 0 or None reaching the store would
        # both distort the page and silently disable the truncation check, since
        # the same number is the yardstick.
        log_sessions(store, 2)
        for bad in (0, None, "loads", -4):
            store_limits.clear()
            _tool_manage_workouts({"action": "list", "limit": bad})
            assert store_limits[0]["limit"] >= 1


# ---------------------------------------------------------------------------
# manage_workouts — action="history"
# ---------------------------------------------------------------------------

class TestWorkoutHistoryEmpty:
    def test_states_the_normalised_name_that_was_queried(self, store):
        # normalize_exercise title-cases anything it has no alias for, so the
        # name looked up differs from the name asked about. Out of scope to
        # resolve the alias; in scope to say which name was queried.
        out = _tool_manage_workouts({"action": "history", "exercise": "flamingo hold"})
        assert "Flamingo Hold" in out
        assert "flamingo hold" in out  # the name as asked, so the model sees both
        assert "normalised from" in out
        assert_no_fault_language(out)

    def test_already_canonical_name_is_still_stated(self, store):
        out = _tool_manage_workouts({"action": "history", "exercise": "Flamingo Hold"})
        assert "Flamingo Hold" in out
        # Nothing was rewritten, so claiming a normalisation would be noise.
        assert "normalised from" not in out
        assert_no_fault_language(out)

    def test_empty_does_not_claim_the_exercise_was_never_done(self, store):
        store.add_session(sets=[{"exercise": "Flamingo Hold", "reps": 3}], date="2026-06-07")
        out = _tool_manage_workouts({"action": "history", "exercise": "flamingo holds"})
        # "Flamingo Holds" (plural) is a different key; the reply must attribute
        # the miss to the name looked up, not to the work never happening.
        assert "different name" in out
        assert_no_fault_language(out)


class TestWorkoutHistoryCap:
    def test_default_spans_more_than_a_month_of_one_lift(self):
        """One row is one set, and sessions in the log carry 2.3 sets on average
        (4 at worst), so a twice-weekly lift produces roughly 20 sets a month —
        the old default of 20 could not span a progression question, which is
        asked over months."""
        assert _WORKOUT_HISTORY_LIMIT >= 60

    def test_the_new_default_is_what_the_store_is_asked_for(self, store, store_limits):
        _tool_manage_workouts({"action": "history", "exercise": "flamingo hold"})
        assert store_limits == [{"query": "exercise_history", "limit": _WORKOUT_HISTORY_LIMIT}]

    def test_a_full_page_at_the_default_discloses_the_cap(self, store):
        store.add_session(
            sets=[{"exercise": "flamingo hold", "reps": 3, "count": _WORKOUT_HISTORY_LIMIT}],
            date="2026-06-10",
        )
        out = _tool_manage_workouts({"action": "history", "exercise": "flamingo hold"})
        assert f"Capped at {_WORKOUT_HISTORY_LIMIT} sets" in out
        # Names the lift as looked up, so the note can't be read as being about
        # some other exercise.
        assert "Flamingo Hold" in out
        assert_no_fault_language(out)

    def test_one_below_the_cap_says_nothing(self, store):
        store.add_session(
            sets=[{"exercise": "flamingo hold", "reps": 3, "count": _WORKOUT_HISTORY_LIMIT - 1}],
            date="2026-06-10",
        )
        out = _tool_manage_workouts({"action": "history", "exercise": "flamingo hold"})
        assert "Capped at" not in out

    def test_the_disclosure_quotes_the_caller_supplied_cap(self, store):
        store.add_session(sets=[{"exercise": "flamingo hold", "reps": 3, "count": 5}], date="2026-06-10")
        out = _tool_manage_workouts({"action": "history", "exercise": "flamingo hold", "limit": 2})
        assert "Capped at 2 sets" in out
        assert f"Capped at {_WORKOUT_HISTORY_LIMIT}" not in out
        assert_no_fault_language(out)

    def test_an_unusable_cap_still_bounds_the_query(self, store, store_limits):
        store.add_session(sets=[{"exercise": "flamingo hold", "reps": 3}], date="2026-06-10")
        for bad in (0, None, "all of them"):
            store_limits.clear()
            _tool_manage_workouts({"action": "history", "exercise": "flamingo hold", "limit": bad})
            assert store_limits[0]["limit"] >= 1


# ---------------------------------------------------------------------------
# manage_workouts — action="metrics"
# ---------------------------------------------------------------------------

class TestWorkoutMetricsEmpty:
    def test_dated_empty_names_the_window(self, store):
        out = _tool_manage_workouts({
            "action": "metrics", "metric_type": "body_weight",
            "date_start": "2026-06-01", "date_end": "2026-06-30",
        })
        assert "2026-06-01" in out and "2026-06-30" in out
        assert "No body weight recorded" not in out
        assert_no_fault_language(out)

    def test_dated_empty_while_values_exist_outside_the_window(self, store):
        store.log_metric("body_weight", 171.0, unit="lb", start_at="2026-01-05T07:00:00+00:00")
        out = _tool_manage_workouts({
            "action": "metrics", "metric_type": "body_weight",
            "date_start": "2026-06-01", "date_end": "2026-06-30",
        })
        assert "2026-06-01" in out and "2026-06-30" in out
        assert "No body weight recorded" not in out
        assert_no_fault_language(out)

    def test_cumulative_dated_empty_names_the_window(self, store):
        # steps takes the daily-rollup path, which has its own empty return.
        out = _tool_manage_workouts({
            "action": "metrics", "metric_type": "steps", "date_start": "2026-06-01",
        })
        assert "2026-06-01" in out
        assert "No steps recorded" not in out
        assert_no_fault_language(out)

    def test_unfiltered_empty_may_state_the_metric_is_not_recorded(self, store):
        out = _tool_manage_workouts({"action": "metrics", "metric_type": "body_weight"})
        assert out == "No body weight recorded."
        assert_no_fault_language(out)


class TestWorkoutMetricsCap:
    def test_default_spans_a_year_of_a_daily_metric(self):
        """A metric question is a trend question, and its natural span is a year.
        The cumulative path returns one row per day, and HRV arrives about 275
        samples a year in the real record, so the old default of 100 clipped both
        to under four months."""
        assert _WORKOUT_METRICS_LIMIT >= 365

    def test_the_new_default_is_what_the_store_is_asked_for(self, store, store_limits):
        _tool_manage_workouts({"action": "metrics", "metric_type": "body_weight"})
        _tool_manage_workouts({"action": "metrics", "metric_type": "steps"})
        assert store_limits == [
            {"query": "list_metrics", "limit": _WORKOUT_METRICS_LIMIT},
            {"query": "daily_metric_totals", "limit": _WORKOUT_METRICS_LIMIT},
        ]

    def test_a_full_page_at_the_default_discloses_the_cap(self, store):
        # A year of daily samples is exactly the case the raised default is for,
        # so it is worth filling the page for real rather than stubbing it.
        for i in range(_WORKOUT_METRICS_LIMIT):
            store.log_metric(
                "body_weight", 171.0 + i % 4, unit="lb",
                start_at=f"2026-06-10T{i % 24:02d}:{i % 60:02d}:00+00:00",
            )
        out = _tool_manage_workouts({"action": "metrics", "metric_type": "body_weight"})
        assert f"Capped at {_WORKOUT_METRICS_LIMIT} samples" in out
        assert "earlier body weight may also be recorded" in out
        assert_no_fault_language(out)

    def test_one_below_the_cap_says_nothing(self, store):
        for i in range(4):
            store.log_metric("body_weight", 171.0, unit="lb", start_at=f"2026-06-1{i}T07:00:00+00:00")
        out = _tool_manage_workouts({"action": "metrics", "metric_type": "body_weight", "limit": 5})
        assert "Capped at" not in out

    def test_the_disclosure_quotes_the_caller_supplied_cap(self, store):
        for i in range(4):
            store.log_metric("body_weight", 171.0, unit="lb", start_at=f"2026-06-1{i}T07:00:00+00:00")
        out = _tool_manage_workouts({"action": "metrics", "metric_type": "body_weight", "limit": 2})
        assert "Capped at 2 samples" in out
        assert f"Capped at {_WORKOUT_METRICS_LIMIT}" not in out
        assert_no_fault_language(out)

    def test_the_daily_rollup_path_discloses_its_cap_in_days(self, store):
        # steps takes the daily-total path, where the cap counts days, not samples
        # — describing it as samples would understate what is missing.
        for day in ("08", "09", "10"):
            store.log_metric("steps", 4200, unit="count", start_at=f"2026-06-{day}T09:00:00+00:00")
        out = _tool_manage_workouts({"action": "metrics", "metric_type": "steps", "limit": 2})
        assert "Capped at 2 days" in out
        assert_no_fault_language(out)

    def test_an_unusable_cap_still_bounds_the_query(self, store, store_limits):
        store.log_metric("body_weight", 171.0, unit="lb", start_at="2026-06-10T07:00:00+00:00")
        for bad in (0, None, "everything"):
            store_limits.clear()
            _tool_manage_workouts({"action": "metrics", "metric_type": "body_weight", "limit": bad})
            assert store_limits[0]["limit"] >= 1


# ---------------------------------------------------------------------------
# manage_tasks — action="list"
# ---------------------------------------------------------------------------

class TestTaskListEmpty:
    def test_filtered_empty_echoes_status_context_and_query(self, tasks):
        out = _tool_manage_tasks({
            "action": "list", "status": "done", "context": "Kayak Trip", "query": "portage map",
        })
        assert "done" in out
        assert "Kayak Trip" in out
        assert "portage map" in out
        assert "No tasks found" not in out
        assert_no_fault_language(out)

    def test_context_miss_does_not_read_as_an_empty_task_list(self, tasks):
        # context is exact case-insensitive equality (loosening it is out of
        # scope), so a near-miss spelling matches nothing while tasks do exist.
        tasks.create("Wax the synthetic canoe")
        out = _tool_manage_tasks({"action": "list", "context": "Kayak Trips"})
        assert "Kayak Trips" in out
        assert "No tasks found" not in out
        assert_no_fault_language(out)

    def test_unfiltered_empty_may_state_there_are_no_tasks(self, tasks):
        out = _tool_manage_tasks({"action": "list"})
        assert out == "No tasks found."
        assert_no_fault_language(out)


# ---------------------------------------------------------------------------
# search_vault
# ---------------------------------------------------------------------------

class TestSearchVault:
    def test_default_top_k_is_not_below_the_service_default(self):
        """Pinned against HybridSearch.search's own signature so the two can't
        drift apart again — the tool's old default of 10 halved it."""
        service_default = inspect.signature(hs_mod.HybridSearch.search).parameters["top_k"].default
        assert _VAULT_TOP_K_DEFAULT >= service_default

    def test_passes_the_default_when_the_caller_states_no_count(self, fake_vault):
        _tool_search_vault({"query": "kayak portage checklist"})
        assert fake_vault.calls[0]["top_k"] == _VAULT_TOP_K_DEFAULT

    def test_explicit_top_k_is_honoured(self, fake_vault):
        _tool_search_vault({"query": "kayak portage checklist", "top_k": 5})
        assert fake_vault.calls[0]["top_k"] == 5

    def test_unusable_top_k_still_searches_for_something(self, fake_vault):
        # The model writes this argument; a 0 or None would return nothing and
        # read as an empty vault.
        for bad in (0, None, -3):
            fake_vault.calls.clear()
            _tool_search_vault({"query": "kayak portage checklist", "top_k": bad})
            assert fake_vault.calls[0]["top_k"] >= 1

    def test_empty_echoes_the_query(self, fake_vault):
        out = _tool_search_vault({"query": "kayak portage checklist"})
        assert "kayak portage checklist" in out
        assert_no_fault_language(out)

    def test_results_still_render(self, fake_vault):
        fake_vault.results = [
            {"file_name": "Kayak Trip Planning.md", "content": "Portage route notes.", "hybrid_score": 0.42},
        ]
        out = _tool_search_vault({"query": "kayak portage checklist"})
        assert "Kayak Trip Planning.md" in out
        assert "Portage route notes." in out


class TestSearchVaultCap:
    def test_default_keeps_what_the_pipeline_already_retrieved(self):
        """Ordinary queries have far more matches than the old cap of 20 — measured
        against the live index, "meeting notes" 50, "project" 48, "kayak trip" 42,
        "workout" 29 — and the extras are free, because the pipeline fetches
        rerank_candidates candidates whatever top_k says (top_k=80 was measured to
        return exactly what top_k=50 does)."""
        assert _VAULT_TOP_K_DEFAULT >= 40

    def test_default_stays_under_the_rerank_candidate_pool(self):
        """HybridSearch.search only runs the cross-encoder when more candidates
        survive than top_k asks for, so a default at or above the candidate pool
        would silently switch reranking off — and then the character budget would
        drop the tail by RRF order instead of by relevance."""
        candidates = inspect.signature(hs_mod.HybridSearch.search).parameters["rerank_candidates"].default
        assert _VAULT_TOP_K_DEFAULT < candidates

    def test_a_full_page_discloses_the_cap(self, fake_vault):
        fake_vault.results = [vault_chunk(i) for i in range(_VAULT_TOP_K_DEFAULT)]
        out = _tool_search_vault({"query": "kayak portage checklist"})
        assert f"Capped at top_k={_VAULT_TOP_K_DEFAULT}" in out
        assert "more matching chunks may exist" in out
        # A full page is not a fault.
        assert_no_fault_language(out)

    def test_one_below_the_cap_says_nothing(self, fake_vault):
        fake_vault.results = [vault_chunk(i) for i in range(_VAULT_TOP_K_DEFAULT - 1)]
        out = _tool_search_vault({"query": "kayak portage checklist"})
        assert "Capped at" not in out

    def test_the_disclosure_quotes_the_caller_supplied_cap(self, fake_vault):
        fake_vault.results = [vault_chunk(i) for i in range(3)]
        out = _tool_search_vault({"query": "kayak portage checklist", "top_k": 3})
        assert "Capped at top_k=3" in out
        assert f"top_k={_VAULT_TOP_K_DEFAULT}" not in out
        assert_no_fault_language(out)

    def test_a_page_of_full_length_chunks_is_trimmed_to_the_char_budget(self, fake_vault):
        # 40 chunks at the 800-char render slice is ~34 KB — measured over the live
        # index, the same 40 results cost 15 KB for one query and 36 KB for
        # another, which is why the ceiling is characters and not count.
        fake_vault.results = [vault_chunk(i, content_len=800) for i in range(_VAULT_TOP_K_DEFAULT)]
        out = _tool_search_vault({"query": "kayak portage checklist"})
        assert f"Chunk text capped at {_VAULT_CHAR_BUDGET} characters" in out
        assert f"of {_VAULT_TOP_K_DEFAULT} chunks returned" in out
        # Both bounds bound here, and both are facts the model needs: the count
        # cap means more exist beyond these 40, the budget means fewer than 40 are
        # shown.
        assert f"Capped at top_k={_VAULT_TOP_K_DEFAULT}" in out
        assert_no_fault_language(out)

    def test_trimming_drops_whole_chunks_from_the_tail(self, fake_vault, monkeypatch):
        # A chunk shown without its own header would be attributed to the note
        # above it, so the cut lands between chunks. Budget shrunk rather than
        # padded so the note's quoted value is checked against a value in effect.
        monkeypatch.setattr(agent_tools, "_VAULT_CHAR_BUDGET", 700)
        fake_vault.results = [vault_chunk(i, content_len=300) for i in range(5)]
        out = _tool_search_vault({"query": "kayak portage checklist"})
        assert "Chunk text capped at 700 characters" in out
        assert "of 5 chunks returned" in out
        kept = [i for i in range(5) if f"chunk-{i:03d}" in out]
        # Highest-ranked first, contiguous from the top, and each kept chunk whole.
        assert kept == list(range(len(kept)))
        for i in kept:
            assert vault_chunk(i, content_len=300)["content"] in out

    def test_a_budget_smaller_than_one_chunk_still_returns_a_chunk(self, fake_vault, monkeypatch):
        # Never trade a truncation note for an empty body: one whole chunk always
        # survives, however tight the budget.
        monkeypatch.setattr(agent_tools, "_VAULT_CHAR_BUDGET", 10)
        fake_vault.results = [vault_chunk(i, content_len=300) for i in range(3)]
        out = _tool_search_vault({"query": "kayak portage checklist"})
        assert "chunk-000" in out
        assert "chunk-001" not in out
        assert "showing the 1 highest-ranked of 3 chunks returned" in out


# ---------------------------------------------------------------------------
# the shared filter-naming helper
# ---------------------------------------------------------------------------

class TestAppliedFilters:
    def test_no_filters_is_empty(self):
        # The empty list is what licenses the "the record is empty" wording.
        assert _applied_filters() == []
        assert _applied_filters(status=None, context="") == []

    def test_named_filters_keep_caller_order(self):
        assert _applied_filters(status="todo", context="Kayak Trip") == [
            "status='todo'", "context='Kayak Trip'",
        ]

    def test_one_sided_date_bounds_are_described_as_such(self):
        assert _applied_filters(date_start="2026-06-01") == ["dates from 2026-06-01"]
        assert _applied_filters(date_end="2026-06-30") == ["dates through 2026-06-30"]
        assert _applied_filters(date_start="2026-06-01", date_end="2026-06-30") == [
            "dates 2026-06-01 to 2026-06-30",
        ]


class TestVaultUnreachableTopK:
    """A request the pipeline cannot serve must not read as a complete answer.

    `HybridSearch` fetches `rerank_candidates` candidates regardless of `top_k`,
    so a larger `top_k` was never served — it came back at the candidate limit.
    Because the truncation check compares against the number *asked for*, a short
    return then read as complete when it was really ceiling-bound. The accepted
    value is now held below that ceiling, which also keeps the cross-encoder on.
    """

    def test_the_max_stays_below_the_rerank_candidate_ceiling(self):
        """At or above it, HybridSearch silently stops reranking."""
        import inspect
        from api.services.hybrid_search import HybridSearch

        candidates = inspect.signature(HybridSearch.search).parameters[
            "rerank_candidates"
        ].default
        assert _VAULT_TOP_K_MAX < candidates
        assert _VAULT_TOP_K_DEFAULT < candidates

    def test_an_unreachable_request_is_reduced_and_disclosed(self, fake_vault):
        out = _tool_search_vault({"query": "kayak", "top_k": 500})
        assert fake_vault.calls[-1]["top_k"] == _VAULT_TOP_K_MAX
        assert "reduced to" in out
        assert "would have gone unserved" in out

    def test_a_reachable_request_is_passed_through_unreduced(self, fake_vault):
        out = _tool_search_vault({"query": "kayak", "top_k": 12})
        assert fake_vault.calls[-1]["top_k"] == 12
        assert "reduced to" not in out

    def test_the_default_is_not_reported_as_reduced(self, fake_vault):
        out = _tool_search_vault({"query": "kayak"})
        assert "reduced to" not in out

    def test_a_garbled_top_k_is_not_reported_as_reduced(self, fake_vault):
        """Absent or unusable is not the same as "you asked for too much"."""
        for bad in (None, "lots", [], 0):
            out = _tool_search_vault({"query": "kayak", "top_k": bad})
            assert "reduced to" not in out, f"{bad!r} reported as a reduction"
