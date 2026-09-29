#!/usr/bin/env python3
"""Report per-arm attribution of vault searches from the perf-trace database.

Reads the ``search_attribution`` spans recorded by ``HybridSearch.search()``
and prints how much of the final results the keyword (BM25) arm can reach.
The database is opened read-only. Query text is printed only with
``--show-queries``.

Usage:
    python scripts/search_attribution_report.py --since 2026-09-01
    python scripts/search_attribution_report.py --since 2026-09-01 --show-queries
"""
import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "perf_traces.db"


def load_attributions(db_path: str, since: str) -> list[dict]:
    """Return the metadata of every span carrying ``attribution`` since a date."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT span_data FROM traces WHERE created_at >= ? ORDER BY created_at",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    found = []
    for (span_data,) in rows:
        try:
            spans = json.loads(span_data)
        except (TypeError, ValueError):
            continue
        for span in spans:
            meta = span.get("metadata") or {}
            if isinstance(meta.get("attribution"), dict):
                found.append(meta)
    return found


def _bm25_share(meta: dict) -> float | None:
    attr = meta["attribution"]
    total = sum(attr.get(k, 0) for k in ("vector_only", "bm25_only", "both"))
    if total == 0:
        return None
    return (attr.get("bm25_only", 0) + attr.get("both", 0)) / total


def _mode_label(mode) -> str:
    """One label per search: a string mode as-is, a count dict as its sorted
    mode names joined with ``+`` (``and``, ``or``, or ``and+or`` when mixed)."""
    if isinstance(mode, dict):
        return "+".join(sorted(mode)) or "none"
    return str(mode) if mode else "unknown"


def build_report(searches: list[dict], show_queries: bool = False) -> str:
    n = len(searches)
    lines = [f"Searches counted: {n}"]
    if n == 0:
        return "\n".join(lines)
    shares = [s for s in (_bm25_share(m) for m in searches) if s is not None]
    if shares:
        lines.append(
            f"Share of results reachable by BM25 (bm25 or both): "
            f"mean {statistics.mean(shares):.1%}, median {statistics.median(shares):.1%} "
            f"(over {len(shares)} searches with results)"
        )
    else:
        lines.append("Share of results reachable by BM25: n/a (no search returned results)")
    zero_bm25 = sum(1 for m in searches if m.get("bm25_candidates") == 0)
    zero_vec = sum(1 for m in searches if m.get("vector_candidates") == 0)
    lines.append(f"Searches with zero BM25 candidates: {zero_bm25 / n:.1%} ({zero_bm25}/{n})")
    lines.append(f"Searches with zero vector candidates: {zero_vec / n:.1%} ({zero_vec}/{n})")
    modes = Counter(_mode_label(m.get("bm25_match_mode")) for m in searches)
    lines.append("BM25 match mode: " + ", ".join(f"{k}={v}" for k, v in sorted(modes.items())))
    top_ks = Counter(m.get("top_k") for m in searches)
    lines.append(
        "top_k distribution: "
        + ", ".join(f"{k}={v}" for k, v in sorted(top_ks.items(), key=lambda kv: str(kv[0])))
    )
    if show_queries:
        lines.append("Queries:")
        lines.extend(f"  {m.get('query', '')!r}" for m in searches)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="ISO date/time lower bound")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="perf trace database path")
    parser.add_argument("--show-queries", action="store_true", help="also print query text")
    args = parser.parse_args(argv)
    try:
        searches = load_attributions(args.db, args.since)
    except sqlite3.Error as e:
        print(f"Cannot read trace database {args.db}: {e}", file=sys.stderr)
        return 1
    print(build_report(searches, args.show_queries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
