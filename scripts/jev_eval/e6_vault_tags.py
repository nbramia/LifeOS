"""E6 -- quality of Jev vault tagging.

Tags the notes under one vault folder with the production `VaultTagger` (shadow
mode, that folder as the only allowlisted path) and reports aggregate numbers:

  - silver agreement: how often the machine facet equals the facet implied by
    the note's own human `type:` / `tags:` (small mappings below; notes with no
    mappable human value are not counted),
  - gold accuracy per facet from a local labeled file
    (`data/vault_tag_gold.jsonl`, one JSON object per line with `file_path`
    plus any of `doc_type`, `domain`, `topic`, `project`; never committed),
  - calibration per 0.1 confidence bucket (mean confidence vs. gold accuracy),
  - flip rate: the share of (note, facet) values that differ between two runs.

Reads the vault; reads and writes only under `data/` or the `--out` path.
Prints aggregate numbers only -- never note text, titles, or paths.
Sends the folder's notes to TypeSafe: run it only on a folder you have decided
may leave the box. Cost is ~$0.001 per 20 notes.
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from api.services import chunker  # noqa: E402
from api.services.vault_tagger import VaultTagger  # noqa: E402
from config.settings import settings  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
FACETS = ("doc_type", "domain", "topic", "project")

# Human `type:` frontmatter value -> taxonomy doc_type.
TYPE_MAP = {
    "meeting": "meeting_notes", "meeting-notes": "meeting_notes", "meeting_notes": "meeting_notes",
    "journal": "journal", "daily": "journal", "log": "log", "reference": "reference",
    "project": "project_doc", "book": "book_notes", "research": "research",
    "task": "task_record", "transcript": "ambient_transcript",
}
# Human tag -> taxonomy domain.
TAG_DOMAIN_MAP = {
    "work": "work", "health": "health", "fitness": "health", "finance": "money_admin",
    "family": "relationships", "relationships": "relationships", "home": "home_food",
    "food": "home_food", "learning": "growth", "tech": "tech_projects", "code": "tech_projects",
}


def silver_labels(frontmatter: dict) -> dict:
    """Facet values implied by a note's own human `type:` and `tags:`."""
    labels = {}
    t = str(frontmatter.get("type") or "").strip().lower()
    if t in TYPE_MAP:
        labels["doc_type"] = TYPE_MAP[t]
    domains = {TAG_DOMAIN_MAP[tag.lower()] for tag in chunker.normalize_tags(frontmatter.get("tags"))
               if tag.lower() in TAG_DOMAIN_MAP}
    if len(domains) == 1:
        labels["domain"] = domains.pop()
    return labels


def agreement(pairs: list[tuple[str | None, str | None]]) -> dict:
    n = len(pairs)
    return {"n": n, "agreement": round(sum(a == b for a, b in pairs) / n, 4) if n else None}


def calibration(rows: list[tuple[float, bool]]) -> list[dict]:
    """Per 0.1 confidence bucket: count, mean confidence, accuracy."""
    buckets: dict[int, list[tuple[float, bool]]] = {}
    for conf, ok in rows:
        buckets.setdefault(min(int(conf * 10), 9), []).append((conf, ok))
    return [
        {
            "bucket": f"{b / 10:.1f}-{(b + 1) / 10:.1f}",
            "n": len(v),
            "mean_conf": round(sum(c for c, _ in v) / len(v), 3),
            "accuracy": round(sum(ok for _, ok in v) / len(v), 3),
        }
        for b, v in sorted(buckets.items())
    ]


def flip_rate(run_a: dict, run_b: dict) -> dict:
    out = {}
    for facet in FACETS:
        pairs = [(getattr(run_a[p], facet), getattr(run_b[p], facet)) for p in run_a if p in run_b]
        out[facet] = round(sum(a != b for a, b in pairs) / len(pairs), 4) if pairs else None
    return out


def load_gold(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    gold = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            gold[row["file_path"]] = row
    return gold


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--folder", required=True, help="vault-relative folder to tag (the only allowlisted path)")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--gold", type=Path, default=DATA_DIR / "vault_tag_gold.jsonl")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "jev_eval" / "e6_results.json")
    ap.add_argument("--runs", type=int, default=2, help="tagging passes for the flip rate (default 2)")
    args = ap.parse_args()

    settings.jev_vault_tagging = "shadow"
    settings.jev_vault_tag_paths = args.folder
    tagger = VaultTagger()
    root = Path(settings.vault_path)
    files = sorted((root / args.folder).rglob("*.md"))[: args.limit]

    runs = [{tagger._rel(f): tagger.tag_file(f) for f in files} for _ in range(max(1, args.runs))]
    first = runs[0]
    tagged = {p: r for p, r in first.items() if r.backend == "jev"}

    silver: dict[str, list] = {"doc_type": [], "domain": []}
    for f in files:
        rel = tagger._rel(f)
        if rel not in tagged:
            continue
        fm, _ = chunker.extract_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        for facet, value in silver_labels(fm).items():
            silver[facet].append((getattr(tagged[rel], facet), value))

    gold = load_gold(args.gold)
    gold_acc, cal_rows = {}, {}
    for facet in FACETS:
        rows = [(r, gold[p][facet]) for p, r in tagged.items() if p in gold and facet in gold[p]]
        gold_acc[facet] = agreement([(getattr(r, facet), want) for r, want in rows])
        cal_rows[facet] = calibration(
            [(getattr(r, f"{facet}_conf") or 0.0, getattr(r, facet) == want) for r, want in rows]
        )

    report = {
        "notes": len(files),
        "tagged_by_jev": len(tagged),
        "silver_agreement": {f: agreement(p) for f, p in silver.items()},
        "gold_accuracy": gold_acc,
        "calibration": cal_rows,
        "flip_rate": flip_rate(runs[0], runs[1]) if len(runs) > 1 else None,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
