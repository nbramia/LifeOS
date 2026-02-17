"""
Performance benchmark suite for LifeOS.

Runs queries against a live server, collects timing from perf trace data,
and validates answer quality. Designed to be run periodically to track
performance regressions.

Usage:
    # Run on Mac Mini (requires live server):
    ~/.venvs/lifeos/bin/python -m pytest tests/test_perf_benchmark.py -v -s

    # Save results for comparison:
    ~/.venvs/lifeos/bin/python -m pytest tests/test_perf_benchmark.py -v -s --benchmark-save
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVER_URL = os.environ.get("LIFEOS_SERVER_URL", "http://localhost:8000")
BENCHMARK_TIMEOUT = 60.0  # seconds per query
RESULTS_DIR = Path(__file__).parent.parent / "data" / "benchmark_results"

# Load personal config (gitignored) with fallback defaults
_config_path = Path(__file__).parent / "fixtures" / "benchmark_config.json"
if _config_path.exists():
    with open(_config_path) as f:
        _config = json.load(f)
else:
    _config = {}

_PERSON = _config.get("person_name", "a]friend")
_TOPIC = _config.get("vault_topic", "projects")


# Benchmark query definitions
BENCHMARK_QUERIES = [
    {
        "id": "calendar",
        "category": "Calendar",
        "query": "What meetings do I have this week?",
        "expected_tools": ["search_calendar"],
        "quality_check": lambda text: any(w in text.lower() for w in ["monday", "tuesday", "wednesday", "thursday", "friday", "today", "tomorrow", "meeting", "no meetings", "schedule"]),
    },
    {
        "id": "person_lookup",
        "category": "Person lookup",
        "query": f"Tell me about {_PERSON}",
        "expected_tools": ["person_info"],
        "quality_check": lambda text: len(text) > 50,
    },
    {
        "id": "vault_search",
        "category": "Vault search",
        "query": f"Search my notes for {_TOPIC}",
        "expected_tools": ["search_vault"],
        "quality_check": lambda text: len(text) > 50,
    },
    {
        "id": "direct_answer",
        "category": "Direct answer",
        "query": "What's the capital of France?",
        "expected_tools": [],
        "quality_check": lambda text: "paris" in text.lower(),
    },
    {
        "id": "task_list",
        "category": "Task list",
        "query": "Show my tasks",
        "expected_tools": ["manage_tasks"],
        "quality_check": lambda text: any(w in text.lower() for w in ["task", "to-do", "todo", "no tasks", "nothing"]),
    },
    {
        "id": "web_search",
        "category": "Web search",
        "query": "What's the weather today?",
        "expected_tools": ["search_web"],
        "quality_check": lambda text: len(text) > 20,
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def send_query(server_url: str, query: str, timeout: float = BENCHMARK_TIMEOUT) -> dict:
    """
    Send a query via POST /api/ask/stream and collect SSE events.

    Returns dict with: conversation_id, text, events, elapsed_s, error
    """
    result = {
        "conversation_id": None,
        "text": "",
        "events": [],
        "perf_trace": None,
        "elapsed_s": 0.0,
        "error": None,
    }

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            with client.stream(
                "POST",
                f"{server_url}/api/ask/stream",
                json={"question": query, "include_sources": True},
            ) as response:
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = json.loads(line[6:])
                    result["events"].append(data)

                    if data.get("type") == "conversation_id":
                        result["conversation_id"] = data["conversation_id"]
                    elif data.get("type") == "content":
                        result["text"] += data.get("content", "")
                    elif data.get("type") == "perf_trace":
                        result["perf_trace"] = data
                    elif data.get("type") == "error":
                        result["error"] = data.get("message", "Unknown error")
    except Exception as e:
        result["error"] = str(e)

    result["elapsed_s"] = time.monotonic() - t0
    return result


def fetch_trace(server_url: str, conversation_id: str) -> Optional[dict]:
    """Fetch the perf trace for a conversation from the API."""
    try:
        resp = httpx.get(
            f"{server_url}/api/perf/traces",
            params={"conversation_id": conversation_id, "limit": 1},
            timeout=10.0,
        )
        if resp.status_code == 200:
            traces = resp.json().get("traces", [])
            return traces[0] if traces else None
    except Exception:
        pass
    return None


def format_duration(ms: float) -> str:
    """Format milliseconds as human-readable string."""
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms:.0f}ms"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def server_url():
    """Get the server URL, skipping if server is unreachable."""
    try:
        resp = httpx.get(f"{SERVER_URL}/health", timeout=5.0)
        if resp.status_code != 200:
            pytest.skip(f"Server at {SERVER_URL} returned {resp.status_code}")
    except Exception as e:
        pytest.skip(f"Server at {SERVER_URL} unreachable: {e}")
    return SERVER_URL


@pytest.fixture(scope="session")
def benchmark_results():
    """Shared list to collect results across parametrized tests."""
    return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.slow
class TestPerfBenchmark:
    """Performance benchmark tests that run against a live server."""

    @pytest.mark.parametrize(
        "query_spec",
        BENCHMARK_QUERIES,
        ids=[q["id"] for q in BENCHMARK_QUERIES],
    )
    def test_query(self, query_spec, server_url, benchmark_results):
        """Run a single benchmark query and validate results."""
        result = send_query(server_url, query_spec["query"])

        # Collect timing data
        timing = {
            "id": query_spec["id"],
            "category": query_spec["category"],
            "query": query_spec["query"],
            "elapsed_s": result["elapsed_s"],
            "error": result["error"],
            "text_length": len(result["text"]),
            "perf_trace": result["perf_trace"],
        }

        # Extract span timings if perf trace available
        if result["perf_trace"]:
            spans = result["perf_trace"].get("spans", [])
            timing["total_ms"] = result["perf_trace"].get("total_ms", 0)
            timing["claude_ms"] = sum(
                s["duration_ms"] for s in spans
                if s["name"].startswith("claude_api_round_")
            )
            timing["tool_ms"] = sum(
                s["duration_ms"] for s in spans
                if s["name"].startswith("tool_")
            )
            timing["search_ms"] = sum(
                s["duration_ms"] for s in spans
                if s["name"].startswith("search_") and s.get("parent") == "tool_search_vault"
            )
        else:
            # Fall back to wall clock
            timing["total_ms"] = result["elapsed_s"] * 1000
            timing["claude_ms"] = 0
            timing["tool_ms"] = 0
            timing["search_ms"] = 0

        benchmark_results.append(timing)

        # Assertions
        assert result["error"] is None, f"Query failed: {result['error']}"
        assert len(result["text"]) > 0, "Empty response"

        # Quality check
        if query_spec["quality_check"]:
            assert query_spec["quality_check"](result["text"]), (
                f"Quality check failed for '{query_spec['id']}': "
                f"response ({len(result['text'])} chars) did not pass validation"
            )

    def test_summary_report(self, server_url, benchmark_results):
        """Print a formatted summary report after all queries complete."""
        if not benchmark_results:
            pytest.skip("No benchmark results collected")

        # Print report
        print("\n")
        print("=" * 70)
        print("BENCHMARK RESULTS")
        print("=" * 70)
        print(f"{'Query':<35} {'Total':>8} {'Claude':>8} {'Tools':>8} {'Search':>8}")
        print("-" * 70)

        totals = {"total_ms": 0, "claude_ms": 0, "tool_ms": 0, "search_ms": 0, "count": 0}

        for r in benchmark_results:
            if r.get("error"):
                status = "ERROR"
                print(f"{r['query'][:34]:<35} {status:>8}")
                continue

            total = format_duration(r["total_ms"])
            claude = format_duration(r["claude_ms"]) if r["claude_ms"] else "--"
            tools = format_duration(r["tool_ms"]) if r["tool_ms"] else "--"
            search = format_duration(r["search_ms"]) if r["search_ms"] else "--"

            print(f"{r['query'][:34]:<35} {total:>8} {claude:>8} {tools:>8} {search:>8}")

            totals["total_ms"] += r["total_ms"]
            totals["claude_ms"] += r["claude_ms"]
            totals["tool_ms"] += r["tool_ms"]
            totals["search_ms"] += r["search_ms"]
            totals["count"] += 1

        if totals["count"] > 0:
            print("-" * 70)
            n = totals["count"]
            avg_total = format_duration(totals["total_ms"] / n)
            avg_claude = format_duration(totals["claude_ms"] / n)
            avg_tools = format_duration(totals["tool_ms"] / n)
            avg_search = format_duration(totals["search_ms"] / n)
            print(f"{'Average':<35} {avg_total:>8} {avg_claude:>8} {avg_tools:>8} {avg_search:>8}")

        passed = sum(1 for r in benchmark_results if not r.get("error"))
        print(f"\nQuality: {passed}/{len(benchmark_results)} passed")

        # Save results
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        results_file = RESULTS_DIR / f"{timestamp}.json"

        save_data = {
            "timestamp": datetime.now().isoformat(),
            "server_url": server_url,
            "results": benchmark_results,
            "summary": {
                "total_queries": len(benchmark_results),
                "passed": passed,
                "avg_total_ms": totals["total_ms"] / max(totals["count"], 1),
                "avg_claude_ms": totals["claude_ms"] / max(totals["count"], 1),
            },
        }
        results_file.write_text(json.dumps(save_data, indent=2, default=str))
        print(f"\nResults saved to: {results_file}")

        # Compare with previous run
        previous_files = sorted(RESULTS_DIR.glob("*.json"))
        if len(previous_files) >= 2:
            prev_file = previous_files[-2]  # Second-to-last (current is last)
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

        print("=" * 70)
