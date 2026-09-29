#!/usr/bin/env python3
"""Score a search arm against the labeled pairs: recall@k, recall@40, MRR.

Usage:
    python scripts/retrieval_eval/score.py --arm hybrid|bm25|vector [--k 10]
        [--pairs data/retrieval_eval/pairs.jsonl] [--bm25-db PATH]
        [--exclude-source mined|cited|manual ...] [--weighted] [--verbose]

Arms:
  hybrid  POST /api/search on LIFEOS_SERVER_URL (default http://localhost:8000).
  bm25    BM25Index.search on --bm25-db. BM25Index creates its tables on open,
          so point this at a COPY of data/bm25_index.db, never the live file.
  vector  VectorStore.search directly. Loads the embedding model in this
          process; run with HIP_VISIBLE_DEVICES="" (CPU) to avoid GPU contention.

Each record counts once; --weighted weights it by its `count` (1 when absent).

Queries and file names are never printed unless --verbose is given.
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

WIDE_K = 40


def load_pairs(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _hybrid(query: str, top_k: int) -> list[str]:
    import httpx

    base = os.environ.get("LIFEOS_SERVER_URL", "http://localhost:8000").rstrip("/")
    r = httpx.post(f"{base}/api/search", json={"query": query, "top_k": top_k}, timeout=120)
    r.raise_for_status()
    return [x.get("file_path") or x["file_name"] for x in r.json()["results"]]


def make_searcher(arm: str, bm25_db: str):
    if arm == "hybrid":
        return _hybrid
    if arm == "bm25":
        from api.services.bm25_index import BM25Index

        index = BM25Index(db_path=bm25_db)
        return lambda q, k: [r["doc_id"] for r in index.search(q, limit=k)]
    if arm == "vector":
        print("warning: the vector arm loads the embedding model in this process; "
              "set HIP_VISIBLE_DEVICES=\"\" to run on CPU and avoid GPU contention.", file=sys.stderr)
        from api.services.vectorstore import VectorStore

        store = VectorStore()
        return lambda q, k: [
            r.get("doc_id") or r["metadata"]["file_path"] for r in store.search(q, top_k=k)
        ]
    raise ValueError(arm)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", required=True, choices=["hybrid", "bm25", "vector"])
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--pairs", default=str(REPO / "data/retrieval_eval/pairs.jsonl"))
    ap.add_argument("--bm25-db", default=str(REPO / "data/bm25_index.db"))
    ap.add_argument("--exclude-source", action="append", default=[],
                    choices=["mined", "cited", "manual"],
                    help="drop pairs with this source (repeatable), e.g. cited to avoid retriever bias")
    ap.add_argument("--weighted", action="store_true",
                    help="weight each record by its `count` field (default 1) instead of once each")
    ap.add_argument("--verbose", "--show-queries", action="store_true",
                    help="list queries (and their relevant files) that miss at top-k")
    args = ap.parse_args(argv)

    from _match import filter_pairs, recall_at_k, score_queries

    pairs = filter_pairs(load_pairs(Path(args.pairs)), args.exclude_source)
    search = make_searcher(args.arm, args.bm25_db)
    rankings = [(search(p["query"], WIDE_K), p["relevant_files"]) for p in pairs]
    weights = [p.get("count", 1) for p in pairs] if args.weighted else None
    res = score_queries(rankings, k=args.k, k_wide=WIDE_K, weights=weights)
    print(f"arm={args.arm} n={res['n']}" + (f" weight={sum(weights)}" if weights else ""))
    print(f"recall@{args.k}={res[f'recall@{args.k}']:.3f} "
          f"recall@{WIDE_K}={res[f'recall@{WIDE_K}']:.3f} mrr={res['mrr']:.3f}")
    if args.verbose:
        for p, (ranked, rel) in zip(pairs, rankings):
            if recall_at_k(ranked, rel, args.k) < 1.0:
                print(f"miss: {p['query']!r} relevant={p['relevant_files']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
