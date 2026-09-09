"""
Performance benchmark suite for LifeOS — owned candidate lane.

Runs against the owned candidate server every ``requires_server``/
``integration`` test uses, with deterministic provider-boundary fixtures
installed by ``scripts/_test_instance_bootstrap.py`` standing in for
Google Calendar and DuckDuckGo, and synthetic vault/task/CRM data seeded
into the candidate's own manifest-owned storage. Every query still goes
through the real agent loop, real tool dispatch, real response
formatting, and real perf tracing — only the two external-provider
boundaries and the LLM backend are deterministic stand-ins. Per-query
timings measure the real agent/tool/formatting pipeline, not real
network or model latency, so they are printed for visibility but never
persisted for cross-run comparison — that would silently mix synthetic
and live numbers into one misleading trend line.

See ``tests/perf_benchmark_live.py`` for the separate, explicit opt-in
mode against a real running server and real external providers — it is
deliberately named without a ``test_`` prefix so no default
``scripts/test.sh`` lane (or bare ``pytest``/``pytest tests/``) ever
collects it, even when ``LIFEOS_SERVER_URL`` happens to be set.

Usage:
    ~/.venvs/lifeos/bin/python scripts/test_instance.py run --source . -- \\
        ~/.venvs/lifeos/bin/python -m pytest tests/test_perf_benchmark.py -v
"""
import pytest

from tests.perf_benchmark_helpers import print_summary, send_query, span_names, timing_from_result
from tests.synthetic_perf_fixtures import (
    SYNTHETIC_PERSON_NAME,
    SYNTHETIC_TASK_TRIGGER,
    seed_synthetic_perf_fixtures,
)
from tests.synthetic_qa_corpus import SYNTHETIC_ML_RECORD, seed_synthetic_qa_corpus

# Trigger strings recognized by scripts/test_instance.py's _StubLLMHandler —
# each drives one real tool call through the real agent tool registry
# against real, seeded fixture data, then derives its final answer only
# from that real tool result (or, for direct_answer, recognizes no tool
# call is needed at all).
_VAULT_TRIGGER = "[synthetic-perf-vault]"
_PERSON_TRIGGER = "[synthetic-perf-person]"
_CALENDAR_TRIGGER = "[synthetic-perf-calendar]"
_WEB_TRIGGER = "[synthetic-perf-web]"
_DIRECT_TRIGGER = "[synthetic-perf-direct]"

_SYNTHETIC_TIMEOUT = 30.0

SYNTHETIC_BENCHMARK_QUERIES = [
    {
        "id": "vault_search",
        "category": "Vault search",
        "query": f"Search my notes for the campaign checklist. {_VAULT_TRIGGER}",
        "expected_tool": "search_vault",
        "expected_answer": f"Synthetic vault result from {SYNTHETIC_ML_RECORD.file_name}.",
    },
    {
        "id": "task_list",
        "category": "Task list",
        "query": f"Show my tasks. {SYNTHETIC_TASK_TRIGGER}",
        "expected_tool": "manage_tasks",
        "expected_answer": "Synthetic task found: verify benchmark checklist.",
    },
    {
        "id": "person_lookup",
        "category": "CRM person",
        "query": f"Tell me about {SYNTHETIC_PERSON_NAME}. {_PERSON_TRIGGER}",
        "expected_tool": "person_info",
        "expected_answer": f"Synthetic person found: {SYNTHETIC_PERSON_NAME}.",
    },
    {
        "id": "calendar",
        "category": "Calendar",
        "query": f"What's on my calendar? {_CALENDAR_TRIGGER}",
        "expected_tool": "search_calendar",
        "expected_answer": "Synthetic calendar event found: Quarterly sync.",
    },
    {
        "id": "web_search",
        "category": "Web search",
        "query": f"What's the weather today? {_WEB_TRIGGER}",
        "expected_tool": "search_web",
        "expected_answer": "Synthetic weather result: Sunny, high of 72F.",
    },
    {
        "id": "direct_answer",
        "category": "Direct answer",
        "query": f"What's the capital of France? {_DIRECT_TRIGGER}",
        "expected_tool": None,
        "expected_answer": "The capital of France is Paris.",
    },
]


@pytest.mark.integration
@pytest.mark.requires_server
class TestPerfBenchmark:
    """Owned-candidate-lane benchmark: six categories plus a summary, all
    driving the real agent loop/tool dispatch/formatting/perf tracing
    through deterministic provider-boundary fixtures.
    """

    @pytest.fixture(scope="class", autouse=True)
    def _seeded_vault(self, candidate_base_url):
        with seed_synthetic_qa_corpus(candidate_base_url) as corpus:
            yield corpus

    @pytest.fixture(scope="class", autouse=True)
    def _seeded_perf_fixtures(self, candidate_base_url):
        with seed_synthetic_perf_fixtures(candidate_base_url) as fixtures:
            yield fixtures

    @pytest.fixture(scope="class")
    def benchmark_results(self):
        return []

    @pytest.mark.parametrize(
        "query_spec",
        SYNTHETIC_BENCHMARK_QUERIES,
        ids=[q["id"] for q in SYNTHETIC_BENCHMARK_QUERIES],
    )
    def test_query(self, query_spec, candidate_base_url, benchmark_results):
        """Run one synthetic-trigger query and verify real dispatch end to end."""
        result = send_query(candidate_base_url, query_spec["query"], _SYNTHETIC_TIMEOUT)
        benchmark_results.append(timing_from_result(query_spec, result))

        assert result["error"] is None, f"Query failed: {result['error']}"
        assert result["text"] == query_spec["expected_answer"], (
            f"'{query_spec['id']}' answer did not match the real tool result's "
            f"deterministic extraction: got {result['text']!r}"
        )

        names = span_names(result["perf_trace"])
        expected_tool = query_spec["expected_tool"]
        if expected_tool:
            assert f"tool_{expected_tool}" in names, (
                f"'{query_spec['id']}' perf trace is missing a tool_{expected_tool} "
                f"span — real tool dispatch did not occur (spans: {sorted(names)})"
            )
        else:
            dispatched = {n for n in names if n.startswith("tool_")}
            assert not dispatched, (
                f"'{query_spec['id']}' should need no tool dispatch, but spans "
                f"show: {sorted(dispatched)}"
            )

    def test_summary_report(self, benchmark_results):
        """Print a summary and require all six categories to have passed.

        Synthetic-mode timings are for visibility only: the LLM backend and
        both external-provider boundaries are deterministic stand-ins here,
        so these numbers measure the real agent/tool/formatting pipeline,
        not real network or model latency, and are intentionally never
        persisted for comparison against tests/perf_benchmark_live.py runs.
        """
        if len(benchmark_results) != len(SYNTHETIC_BENCHMARK_QUERIES):
            pytest.fail(
                f"Expected {len(SYNTHETIC_BENCHMARK_QUERIES)} benchmark results, "
                f"got {len(benchmark_results)} — some categories did not run."
            )
        passed = print_summary(benchmark_results)
        assert passed == len(SYNTHETIC_BENCHMARK_QUERIES), (
            f"Only {passed}/{len(SYNTHETIC_BENCHMARK_QUERIES)} synthetic categories passed"
        )
