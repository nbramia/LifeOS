"""Calibrate the topic-match thresholds (TOPIC_MATCH_TOP_K / TOPIC_MATCH_MIN_P).

Uses notes that carry a mapped human tag as silver labels for the topic the
merged taxonomy (committed file plus operator override) maps that tag to, and
scores every (top-k, min-p) pair against the stored topic distributions:

  - recall    = silver-tagged notes the rule matches / silver-tagged notes,
  - precision = matched notes that carry the silver tag OR whose primary
    topic is the requested one / matched notes. Notes about the topic that
    were never human-tagged count as misses, so this is a lower bound.

Both are summed over the mapped topics (micro average). The chosen pair is
the one with the best precision among pairs reaching the recall target.

Reads `data/vault_tags.db` (opened read-only) and each tagged note's
frontmatter; writes nothing. Prints aggregate numbers only -- never note text,
titles, or paths.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def silver_tag_topics(override: Path | None = None) -> dict[str, str]:
    """Human tag -> topic from the merged taxonomy (committed plus override).

    `override` names an operator override file; without it the checkout's own
    `config/vault_taxonomy.local.yaml` applies when present.
    """
    from api.services.vault_taxonomy import DEFAULT_TAXONOMY_PATH, load_taxonomy

    if override is None:
        return dict(load_taxonomy().tag_topics)
    return dict(load_taxonomy(DEFAULT_TAXONOMY_PATH, override).tag_topics)


TOP_KS = (1, 2, 3, 4, 5)
MIN_PS = (0.05, 0.10, 0.15, 0.20)
RECALL_TARGET = 0.90


def matches(row: dict, topic: str, k: int, min_p: float) -> bool:
    """The store's rule: primary, secondary, top-k rank, or probability floor."""
    if row["topic"] == topic or topic in row["secondary"]:
        return True
    p = row["dist"].get(topic, 0.0)
    if p <= 0:
        return False
    ranked = sorted((v for v in row["dist"].values() if v > 0), reverse=True)
    return p >= min_p or p >= ranked[min(k, len(ranked)) - 1]


def score(rows: list[dict], silver: dict[str, set[int]], k: int, min_p: float,
          tag_topics: dict[str, str]) -> dict:
    hit = tagged = matched = precise = 0
    for tag, topic in tag_topics.items():
        members = silver[tag]
        tagged += len(members)
        for i, row in enumerate(rows):
            if matches(row, topic, k, min_p):
                matched += 1
                hit += i in members
                precise += i in members or row["topic"] == topic
    return {
        "k": k, "min_p": min_p, "tagged": tagged, "matched": matched,
        "recall": hit / tagged if tagged else 0.0,
        "precision": precise / matched if matched else 0.0,
    }


def choose(cells: list[dict], target: float = RECALL_TARGET) -> dict | None:
    ok = [c for c in cells if c["recall"] >= target]
    return max(ok, key=lambda c: (c["precision"], -c["k"], c["min_p"])) if ok else None


def load(db_path: Path, vault: Path, tag_topics: dict[str, str]) -> tuple[list[dict], dict[str, set[int]]]:
    from api.services import chunker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows, silver = [], {tag: set() for tag in tag_topics}
    for path, topic, topics_json in conn.execute(
        "SELECT file_path, topic, topics_json FROM vault_tags WHERE backend = 'jev'"
    ):
        data = json.loads(topics_json)
        dist = {k: float(v) for k, v in (data.get("topic") or {}).items()}
        try:
            text = (vault / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _ = chunker.extract_frontmatter(text)
        tags = {t.lower() for t in chunker.normalize_tags(fm.get("tags"))}
        rows.append({"topic": topic, "secondary": set(data.get("secondary") or []), "dist": dist})
        for tag in tag_topics:
            if tag in tags:
                silver[tag].add(len(rows) - 1)
    conn.close()
    return rows, silver


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "vault_tags.db")
    ap.add_argument("--recall-target", type=float, default=RECALL_TARGET)
    ap.add_argument("--taxonomy-override", type=Path, default=None,
                    help="operator taxonomy override file (default: this checkout's config/)")
    ap.add_argument("--env-file", default=None, help="dotenv file to read settings from")
    args = ap.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file)
    from config.settings import settings

    tag_topics = silver_tag_topics(args.taxonomy_override)
    rows, silver = load(args.db, Path(settings.vault_path), tag_topics)
    print(f"jev-tagged notes: {len(rows)}")
    for tag, topic in tag_topics.items():
        print(f"silver {tag!r} -> {topic}: {len(silver[tag])} notes")
    cells = [score(rows, silver, k, p, tag_topics) for k in TOP_KS for p in MIN_PS]
    print("\n k  min_p  recall  precision  matched")
    for c in cells:
        print(f" {c['k']}  {c['min_p']:.2f}   {c['recall']:.3f}   {c['precision']:.3f}     {c['matched']}")
    best = choose(cells, args.recall_target)
    if best is None:
        top = max(cells, key=lambda c: c["recall"])
        print(f"\nno pair reaches recall {args.recall_target:.2f}; the ceiling is "
              f"{top['recall']:.3f} (missed notes give the topic probability 0)")
        return
    print(f"\nchosen: TOPIC_MATCH_TOP_K={best['k']} TOPIC_MATCH_MIN_P={best['min_p']:.2f} "
          f"(recall {best['recall']:.3f}, precision {best['precision']:.3f})")
    print("per-topic recall at the chosen pair:")
    for tag, topic in tag_topics.items():
        m = silver[tag]
        r = sum(matches(rows[i], topic, best["k"], best["min_p"]) for i in m) / len(m) if m else 0.0
        print(f"  {topic}: {r:.3f} (n={len(m)})")


if __name__ == "__main__":
    main()
