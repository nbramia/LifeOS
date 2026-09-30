"""Silver agreement between the operator's own labels and the stored vault tags.

Reads the tag store and the vault, changing neither, and prints aggregate
numbers only (never note text, titles, or paths):

  - doc_type: among tagged notes whose frontmatter `type:` the taxonomy maps,
    the share whose stored document type equals the mapped one,
  - per mapped tag: among tagged notes carrying the tag, the share whose stored
    topics (primary or secondary) include the mapped topic.

Usage: silver_labels.py [--vault PATH] [--db PATH]
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from api.services import chunker  # noqa: E402
from api.services.vault_tag_store import VaultTagStore, get_vault_tags_db_path  # noqa: E402
from api.services.vault_tagger import _label_evidence, inline_tags, parse_note  # noqa: E402
from api.services.vault_taxonomy import Taxonomy, load_taxonomy  # noqa: E402
from config.settings import settings  # noqa: E402


def _ratio(hits: int, n: int) -> dict:
    return {"n": n, "agreement": round(hits / n, 4) if n else None}


def measure(vault_root: Path, store: VaultTagStore, taxonomy: Taxonomy) -> dict:
    """Aggregate agreement of stored tags with `type:` and mapped tags."""
    doc = [0, 0]
    tags: dict[str, list[int]] = {}
    for path in sorted(vault_root.rglob("*.md")):
        record = store.get(path.relative_to(vault_root).as_posix())
        if record is None or record.sensitivity == "restricted":
            continue
        fm, body, parsed = parse_note(path.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            continue
        note_tags = chunker.normalize_tags(fm.get("tags")) + inline_tags(body)
        label_type, mapped = _label_evidence(taxonomy, fm, note_tags)
        if label_type:
            doc[0] += record.doc_type == label_type
            doc[1] += 1
        stored = {record.topic, *json.loads(record.topics_json or "{}").get("secondary", [])}
        for topic, names in mapped.items():
            for name in names:
                counts = tags.setdefault(name, [0, 0])
                counts[0] += topic in stored
                counts[1] += 1
    return {
        "doc_type": _ratio(*doc),
        "tags": {name: _ratio(*counts) for name, counts in sorted(tags.items())},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", type=Path, default=Path(settings.vault_path))
    ap.add_argument("--db", default=None, help="tag store path (default: the production store)")
    args = ap.parse_args()
    db = args.db or get_vault_tags_db_path()
    if not Path(db).exists():
        sys.exit("tag store not found")
    print(json.dumps(measure(args.vault, VaultTagStore(db), load_taxonomy()), indent=2))


if __name__ == "__main__":
    main()
