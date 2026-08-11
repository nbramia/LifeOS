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
"""
import inspect
from types import SimpleNamespace

import pytest

import api.services.fitness_store as fs
import api.services.hybrid_search as hs_mod
import api.services.task_manager as tm_mod
from api.services.agent_tools import (
    _VAULT_TOP_K_DEFAULT,
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
