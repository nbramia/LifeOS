"""Pure matching and scoring helpers shared by the retrieval eval scripts."""
import re
from pathlib import PurePosixPath

_CHUNK_SUFFIX = re.compile(r"(::\d+|_\d+)$")


def normalize_file(ref: str) -> str:
    """Reduce a file path or chunk id to a lowercase basename.

    Strips a trailing chunk suffix (``::<n>`` or ``_<n>``) before taking the
    basename, so every chunk of one file maps to the same key.
    """
    ref = _CHUNK_SUFFIX.sub("", ref.strip())
    return PurePosixPath(ref.replace("\\", "/")).name.lower()


def distinct_files(ranked_refs: list[str]) -> list[str]:
    """Normalized files in rank order, keeping each file's first appearance."""
    seen: set[str] = set()
    out: list[str] = []
    for ref in ranked_refs:
        key = normalize_file(ref)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def recall_at_k(ranked_refs: list[str], relevant: list[str], k: int) -> float:
    """Fraction of relevant files found among the top-k distinct files."""
    want = {normalize_file(r) for r in relevant if normalize_file(r)}
    if not want:
        return 0.0
    return len(want & set(distinct_files(ranked_refs)[:k])) / len(want)


def reciprocal_rank(ranked_refs: list[str], relevant: list[str]) -> float:
    """1 / rank of the first relevant distinct file, or 0.0 when none appears."""
    want = {normalize_file(r) for r in relevant}
    for i, key in enumerate(distinct_files(ranked_refs), start=1):
        if key in want:
            return 1.0 / i
    return 0.0


def score_queries(rankings: list[tuple[list[str], list[str]]], k: int = 10, k_wide: int = 40,
                  weights: list[float] | None = None) -> dict:
    """Average recall@k, recall@k_wide and MRR over (ranked_refs, relevant) pairs.

    `weights` (one per pair, default 1 each) makes each average a weighted mean.
    """
    n = len(rankings)
    w = weights if weights is not None else [1.0] * n
    total = float(sum(w))
    if n == 0 or total == 0:
        return {"n": n, f"recall@{k}": 0.0, f"recall@{k_wide}": 0.0, "mrr": 0.0}
    return {
        "n": n,
        f"recall@{k}": sum(x * recall_at_k(r, rel, k) for x, (r, rel) in zip(w, rankings)) / total,
        f"recall@{k_wide}": sum(x * recall_at_k(r, rel, k_wide) for x, (r, rel) in zip(w, rankings)) / total,
        "mrr": sum(x * reciprocal_rank(r, rel) for x, (r, rel) in zip(w, rankings)) / total,
    }


def filter_pairs(pairs: list[dict], exclude_sources: list[str] | None = None) -> list[dict]:
    """Drop pairs whose `source` is in `exclude_sources`."""
    excluded = set(exclude_sources or [])
    return [p for p in pairs if p.get("source") not in excluded]
