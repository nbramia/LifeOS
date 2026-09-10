"""
Explicit, opt-in live-provider performance benchmark.

Deliberately named without a ``test_`` prefix so ``pyproject.toml``'s
``python_files = ["test_*.py"]`` never auto-discovers it — a bare
``pytest``/``pytest tests/`` (and therefore every default
``scripts/test.sh`` lane) never collects, skips, or reports it, so this
mode can never silently affect a default run's pass count even if
``LIFEOS_SERVER_URL`` happens to be set in the environment. Run it only
by naming this file (or a node id inside it) explicitly.

Runs against a real, already-running server and whatever live providers
(Anthropic, Google Calendar, DuckDuckGo) that server is actually
configured with — natural-language queries and loose quality checks,
since real provider answers aren't deterministic. See
``tests/test_perf_benchmark.py``'s ``TestPerfBenchmark`` for the
deterministic candidate-lane mode these numbers are never compared
against.

Usage:
    LIFEOS_SERVER_URL=http://localhost:8000 ~/.venvs/lifeos/bin/python -m pytest \\
        tests/perf_benchmark_live.py -v -s
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
import httpx

from tests.perf_benchmark_helpers import print_summary, send_query, timing_from_result

SERVER_URL = os.environ.get("LIFEOS_SERVER_URL", "").strip()
LIVE_BENCHMARK_TIMEOUT = 60.0
RESULTS_DIR = Path(__file__).parent.parent / "data" / "benchmark_results"

# Load personal config (gitignored) with fallback defaults
_config_path = Path(__file__).parent / "fixtures" / "benchmark_config.json"
if _config_path.exists():
    with open(_config_path) as f:
        _config = json.load(f)
else:
    _config = {}

_PERSON = _config.get("person_name", "a friend")
_TOPIC = _config.get("vault_topic", "projects")

LIVE_BENCHMARK_QUERIES = [
    {
        "id": "calendar",
        "category": "Calendar",
        "query": "What meetings do I have this week?",
        "quality_check": lambda text: any(w in text.lower() for w in ["monday", "tuesday", "wednesday", "thursday", "friday", "today", "tomorrow", "meeting", "no meetings", "schedule"]),
    },
    {
        "id": "person_lookup",
        "category": "Person lookup",
        "query": f"Tell me about {_PERSON}",
        "quality_check": lambda text: len(text) > 50,
    },
    {
        "id": "vault_search",
        "category": "Vault search",
        "query": f"Search my notes for {_TOPIC}",
        "quality_check": lambda text: len(text) > 50,
    },
    {
        "id": "direct_answer",
        "category": "Direct answer",
        "query": "What's the capital of France?",
        "quality_check": lambda text: "paris" in text.lower(),
    },
    {
        "id": "task_list",
        "category": "Task list",
        "query": "Show my tasks",
        "quality_check": lambda text: any(w in text.lower() for w in ["task", "to-do", "todo", "no tasks", "nothing"]),
    },
    {
        "id": "web_search",
        "category": "Web search",
        "query": "What's the weather today?",
        "quality_check": lambda text: len(text) > 20,
    },
]


@pytest.fixture(scope="session")
def live_server_url():
    """Explicitly selected live-provider benchmark server, failing closed
    if not configured — this file is never auto-collected, so there is no
    default-run path for this fixture to accidentally break."""
    if not SERVER_URL:
        pytest.fail("LIFEOS_SERVER_URL is required for the live-provider perf benchmark")
    try:
        resp = httpx.get(f"{SERVER_URL}/health", timeout=5.0)
        if resp.status_code != 200:
            pytest.fail(f"Live-provider benchmark server returned {resp.status_code}")
    except Exception as e:
        pytest.fail(f"Live-provider benchmark server unreachable: {type(e).__name__}")
    return SERVER_URL.rstrip("/")


@pytest.mark.integration
class TestPerfBenchmarkLiveProvider:
    """Benchmark against a real running server and real external providers."""

    @pytest.fixture(scope="session")
    def live_benchmark_results(self):
        return []

    @pytest.mark.parametrize(
        "query_spec",
        LIVE_BENCHMARK_QUERIES,
        ids=[q["id"] for q in LIVE_BENCHMARK_QUERIES],
    )
    def test_query(self, query_spec, live_server_url, live_benchmark_results):
        result = send_query(live_server_url, query_spec["query"], LIVE_BENCHMARK_TIMEOUT)
        live_benchmark_results.append(timing_from_result(query_spec, result))

        assert result["error"] is None, f"Query failed: {result['error']}"
        assert len(result["text"]) > 0, "Empty response"
        if query_spec["quality_check"]:
            assert query_spec["quality_check"](result["text"]), (
                f"Quality check failed for '{query_spec['id']}': "
                f"response ({len(result['text'])} chars) did not pass validation"
            )

    def test_summary_report(self, live_server_url, live_benchmark_results):
        """Print a formatted summary, save results, and compare with the
        previous live-provider run — these are real network/model
        latencies, unlike the candidate lane's synthetic-mode numbers."""
        if not live_benchmark_results:
            pytest.fail("No live-provider benchmark results collected")

        passed = print_summary(live_benchmark_results)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        results_file = RESULTS_DIR / f"{timestamp}.json"

        totals_ms = sum(r["total_ms"] for r in live_benchmark_results if not r.get("error"))
        claude_ms = sum(r["claude_ms"] for r in live_benchmark_results if not r.get("error"))
        n = max(passed, 1)
        save_data = {
            "timestamp": datetime.now().isoformat(),
            "server_url": live_server_url,
            "results": live_benchmark_results,
            "summary": {
                "total_queries": len(live_benchmark_results),
                "passed": passed,
                "avg_total_ms": totals_ms / n,
                "avg_claude_ms": claude_ms / n,
            },
        }
        results_file.write_text(json.dumps(save_data, indent=2, default=str))
        print(f"\nResults saved to: {results_file}")

        previous_files = sorted(RESULTS_DIR.glob("*.json"))
        if len(previous_files) >= 2:
            prev_file = previous_files[-2]
            try:
                prev_data = json.loads(prev_file.read_text())
                prev_avg = prev_data["summary"]["avg_total_ms"]
                curr_avg = save_data["summary"]["avg_total_ms"]
                diff = curr_avg - prev_avg
                pct = (diff / prev_avg * 100) if prev_avg else 0
                direction = "slower" if diff > 0 else "faster"
                print(f"\nvs previous run ({prev_file.stem}): {abs(diff):.0f}ms {direction} ({abs(pct):.1f}%)")
            except Exception:
                pass
