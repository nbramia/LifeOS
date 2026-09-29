#!/usr/bin/env python3
"""Propose vault taxonomy topics from a stratified sample of notes.

Samples notes across the vault's top-level folders, collects their frontmatter
tags and folder names, and asks the configured specialist LLM (Anthropic, else
local, else remote — the same client the fact-extraction calls use) to propose
``parent/child`` topics in the shape of ``config/vault_taxonomy.yaml``.

The proposal is written to ``data/taxonomy_proposal.yaml`` only; the operator
reviews it and copies entries into ``config/vault_taxonomy.local.yaml``.
Note titles, tags, and a short excerpt of each sampled note are sent to the
configured LLM backend, so run it only against a backend you trust with them.

Usage:
    python scripts/taxonomy_bootstrap.py --sample 200
    python scripts/taxonomy_bootstrap.py --dry-run
"""
import argparse
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.chunker import extract_frontmatter  # noqa: E402
from api.services.vault_taxonomy import load_taxonomy  # noqa: E402

PROPOSAL_PATH = Path("data/taxonomy_proposal.yaml")
EXCERPT_CHARS = 200
MAX_TOKENS = 4096


def list_notes_by_folder(vault: Path) -> dict[str, list[Path]]:
    """Group markdown files by top-level folder ('' for files at the root)."""
    groups: dict[str, list[Path]] = {}
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if any(part.startswith(".") for part in rel.parts):
            continue
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        groups.setdefault(folder, []).append(path)
    return groups


def stratified_sample(groups: dict[str, list[Path]], n: int, seed: int = 0) -> list[Path]:
    """Sample about ``n`` notes, proportional to folder size, at least one per folder."""
    total = sum(len(v) for v in groups.values())
    if total <= n:
        return [p for paths in groups.values() for p in paths]
    rng = random.Random(seed)
    sample: list[Path] = []
    for folder in sorted(groups):
        paths = groups[folder]
        take = max(1, round(n * len(paths) / total))
        sample.extend(rng.sample(paths, min(take, len(paths))))
    return sample


def _normalize_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [t for t in raw.replace(",", " ").split() if t]
    if not isinstance(raw, list):
        return []
    return [str(t).lstrip("#").strip() for t in raw if str(t).strip()]


def describe_sample(vault: Path, sample: list[Path]) -> dict[str, Any]:
    """Collect per-note records plus tag and folder counts."""
    notes: list[dict[str, Any]] = []
    tag_counts: Counter = Counter()
    folder_counts: Counter = Counter()
    for path in sample:
        try:
            meta, body = extract_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        rel = path.relative_to(vault)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        tags = _normalize_tags(meta.get("tags"))
        tag_counts.update(tags)
        folder_counts[folder] += 1
        notes.append({
            "folder": folder,
            "title": path.stem,
            "tags": tags,
            "excerpt": " ".join(body.split())[:EXCERPT_CHARS],
        })
    return {"notes": notes, "tags": tag_counts, "folders": folder_counts}


def build_prompt(description: dict[str, Any], domains: tuple[str, ...]) -> str:
    lines = [
        "You are designing a controlled topic vocabulary for a personal note vault.",
        "Propose 40-50 topics. Each topic is `parent/child`, where parent is exactly one of:",
        ", ".join(domains) + ".",
        "Topics must be generic themes that recur across many notes: never names of",
        "people, employers, or specific projects. Give each a one-line description.",
        "Reply with only YAML of this shape:",
        "topics:",
        "  - name: parent/child",
        "    description: One line.",
        "",
        "Existing top-level folders (note counts in the sample):",
    ]
    lines += [f"- {f or '(root)'}: {c}" for f, c in description["folders"].most_common()]
    lines += ["", "Existing frontmatter tags (uses):"]
    lines += [f"- {t}: {c}" for t, c in description["tags"].most_common(150)]
    lines += ["", "Sampled notes (folder | title | tags | excerpt):"]
    for note in description["notes"]:
        lines.append(
            f"- {note['folder'] or '(root)'} | {note['title']} | "
            f"{', '.join(note['tags'])} | {note['excerpt']}"
        )
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def parse_proposal(text: str, domains: tuple[str, ...]) -> tuple[list[dict[str, str]], list[str]]:
    """Extract well-formed topics from the model reply; return (topics, dropped names)."""
    body = text.strip()
    if "```" in body:
        parts = body.split("```")
        body = parts[1] if len(parts) > 1 else body
        if body.startswith(("yaml", "yml")):
            body = body.split("\n", 1)[1] if "\n" in body else ""
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError:
        return [], []
    items = data.get("topics", []) if isinstance(data, dict) else []
    topics: list[dict[str, str]] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        name = str(item.get("name", "")).strip() if isinstance(item, dict) else ""
        parent, sep, child = name.partition("/")
        if not sep or not child or "/" in child or parent not in domains or name in seen:
            dropped.append(name or "?")
            continue
        seen.add(name)
        topics.append({"name": name, "description": str(item.get("description", "")).strip()})
    return topics, dropped


def write_proposal(topics: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Proposed topics from scripts/taxonomy_bootstrap.py. Review, edit, and copy\n"
        "# entries into config/vault_taxonomy.local.yaml.\n"
    )
    path.write_text(header + yaml.safe_dump({"topics": topics}, sort_keys=False), encoding="utf-8")


def run(
    vault: Path,
    sample_size: int,
    *,
    dry_run: bool = False,
    client: Any = None,
    output: Path = PROPOSAL_PATH,
    seed: int = 0,
) -> dict[str, Any]:
    taxonomy = load_taxonomy()
    groups = list_notes_by_folder(vault)
    sample = stratified_sample(groups, sample_size, seed)
    description = describe_sample(vault, sample)
    prompt = build_prompt(description, taxonomy.domains)
    result: dict[str, Any] = {
        "folders": len(groups),
        "sampled": len(description["notes"]),
        "distinct_tags": len(description["tags"]),
        "prompt_tokens_estimate": estimate_tokens(prompt),
    }
    if dry_run:
        return result

    if client is None:
        from api.services.llm_client import get_anthropic_llm
        client = get_anthropic_llm()
    response = client.create(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS,
    )
    topics, dropped = parse_proposal(response.text, taxonomy.domains)
    if not topics:
        raise SystemExit("The model returned no usable topics; nothing written.")
    write_proposal(topics, output)
    result.update({"topics": len(topics), "dropped": dropped, "output": str(output)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sample", type=int, default=200, help="notes to sample (default 200)")
    parser.add_argument("--vault", type=Path, help="vault path (default LIFEOS_VAULT_PATH)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print sample sizes and prompt token estimate; no LLM call")
    args = parser.parse_args()

    vault = args.vault
    if vault is None:
        from config.settings import settings
        vault = settings.vault_path
    if not Path(vault).is_dir():
        print(f"Vault path not found: {vault}", file=sys.stderr)
        return 1

    result = run(Path(vault), args.sample, dry_run=args.dry_run)
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
