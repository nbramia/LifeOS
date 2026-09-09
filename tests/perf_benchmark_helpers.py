"""Shared helpers for the two perf-benchmark entry points.

Not itself a test module (no ``test_`` prefix — see ``pyproject.toml``'s
``python_files``), so importing this never adds collected items to any
lane: ``tests/test_perf_benchmark.py`` (candidate lane) and
``tests/perf_benchmark_live.py`` (explicit opt-in live-provider mode,
also outside ordinary discovery) both import it rather than duplicating
this SSE-parsing and summary-printing logic.
"""
from __future__ import annotations

import json
import time
from typing import Optional

import httpx


def send_query(server_url: str, query: str, timeout: float) -> dict:
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


def format_duration(ms: float) -> str:
    """Format milliseconds as human-readable string."""
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms:.0f}ms"


def span_names(perf_trace: Optional[dict]) -> set[str]:
    if not perf_trace:
        return set()
    return {s["name"] for s in perf_trace.get("spans", [])}


def timing_from_result(query_spec: dict, result: dict) -> dict:
    timing = {
        "id": query_spec["id"],
        "category": query_spec["category"],
        "query": query_spec["query"],
        "elapsed_s": result["elapsed_s"],
        "error": result["error"],
        "text_length": len(result["text"]),
        "perf_trace": result["perf_trace"],
    }
    if result["perf_trace"]:
        spans = result["perf_trace"].get("spans", [])
        timing["total_ms"] = result["perf_trace"].get("total_ms", 0)
        timing["claude_ms"] = sum(
            s["duration_ms"] for s in spans if s["name"].startswith("claude_api_round_")
        )
        timing["tool_ms"] = sum(
            s["duration_ms"] for s in spans if s["name"].startswith("tool_")
        )
    else:
        timing["total_ms"] = result["elapsed_s"] * 1000
        timing["claude_ms"] = 0
        timing["tool_ms"] = 0
    return timing


def print_summary(benchmark_results: list[dict]) -> int:
    print("\n")
    print("=" * 70)
    print("BENCHMARK RESULTS")
    print("=" * 70)
    print(f"{'Query':<35} {'Total':>8} {'Claude':>8} {'Tools':>8}")
    print("-" * 70)

    totals = {"total_ms": 0, "claude_ms": 0, "tool_ms": 0, "count": 0}
    for r in benchmark_results:
        if r.get("error"):
            print(f"{r['query'][:34]:<35} {'ERROR':>8}")
            continue
        total = format_duration(r["total_ms"])
        claude = format_duration(r["claude_ms"]) if r["claude_ms"] else "--"
        tools = format_duration(r["tool_ms"]) if r["tool_ms"] else "--"
        print(f"{r['query'][:34]:<35} {total:>8} {claude:>8} {tools:>8}")
        totals["total_ms"] += r["total_ms"]
        totals["claude_ms"] += r["claude_ms"]
        totals["tool_ms"] += r["tool_ms"]
        totals["count"] += 1

    if totals["count"] > 0:
        print("-" * 70)
        n = totals["count"]
        print(
            f"{'Average':<35} {format_duration(totals['total_ms'] / n):>8} "
            f"{format_duration(totals['claude_ms'] / n):>8} {format_duration(totals['tool_ms'] / n):>8}"
        )

    passed = sum(1 for r in benchmark_results if not r.get("error"))
    print(f"\nQuality: {passed}/{len(benchmark_results)} passed")
    print("=" * 70)
    return passed
