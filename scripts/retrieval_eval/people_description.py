#!/usr/bin/env python3
"""Measure how well the people index answers descriptions such as "who do I know in fundraising".

Usage:
    python scripts/retrieval_eval/people_description.py [--queries PATH] [--top-k 5]

Runs each query in people_description_queries.txt through find_people_by_description
against the people index and the person store the environment points at, then reports:

  suggestions   people returned across all queries
  fit           suggestions whose descriptive lines share at least one content word
                with the query, by the stem rule the ranking uses
  fit_share     fit / suggestions
  profiles      people in the index
  described     profiles with a descriptive line other than an alternate-names line
  described_share

Output is counts only. No query, name or profile text is printed or written.

This embeds each query with the local embedding model. Run it with
HIP_VISIBLE_DEVICES="" ROCR_VISIBLE_DEVICES="" CUDA_VISIBLE_DEVICES="" so the model stays
on the CPU and the GPU the production model uses is left alone.
"""
import argparse
import sys
from pathlib import Path
from typing import Callable, Iterable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DEFAULT_QUERIES = Path(__file__).resolve().parent / "people_description_queries.txt"


def load_queries(path: Path) -> list[str]:
    """Queries from a file, one per line, skipping blanks and # comments."""
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def score_suggestions(queries: Iterable[str], find: Callable, top_k: int) -> dict:
    """
    Count the suggestions returned for each query and how many fit.

    Args:
        queries: The descriptions to run
        find: find_people_by_description, or anything with its signature
        top_k: Most suggestions per query

    Returns:
        queries, answered (queries with a suggestion), suggestions, fit
    """
    queries = list(queries)
    answered = suggestions = fit = 0
    for query in queries:
        matches = find(query, top_k=top_k)
        answered += bool(matches)
        suggestions += len(matches)
        fit += sum(1 for match in matches if match.matched_words)
    return {"queries": len(queries), "answered": answered, "suggestions": suggestions, "fit": fit}


def score_coverage(documents: Iterable[str], is_described: Callable[[str], bool]) -> dict:
    """Count the profiles, and those with a descriptive line other than alternate names."""
    profiles = described = 0
    for document in documents:
        profiles += 1
        described += bool(is_described(document))
    return {"profiles": profiles, "described": described}


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.3f}" if whole else "n/a"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES))
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args(argv)

    from api.services.person_indexer import PERSON_COLLECTION, find_people_by_description, has_descriptive_content
    from api.services.vectorstore import VectorStore

    try:
        store = VectorStore(collection_name=PERSON_COLLECTION)
        coverage = score_coverage(store.iter_documents(), has_descriptive_content)
    except Exception as e:
        print(
            f"error: the people index is not reachable ({type(e).__name__}); "
            "start Chroma or point LIFEOS_CHROMA_URL at it",
            file=sys.stderr,
        )
        return 2
    suggestions = score_suggestions(load_queries(Path(args.queries)), find_people_by_description, args.top_k)

    print(f"queries={suggestions['queries']} answered={suggestions['answered']}")
    print(
        f"suggestions={suggestions['suggestions']} fit={suggestions['fit']} "
        f"fit_share={_share(suggestions['fit'], suggestions['suggestions'])}"
    )
    print(
        f"profiles={coverage['profiles']} described={coverage['described']} "
        f"described_share={_share(coverage['described'], coverage['profiles'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
