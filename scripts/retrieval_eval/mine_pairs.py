#!/usr/bin/env python3
"""Mine query -> relevant-file pairs from conversation history.

Two signals, kept as separate `source` values:

  mined  The assistant message's `routing` lists `search_vault` and the
         persisted `sources` column records a `read_vault_file` call. The
         column keeps each call as `tool(json-args)` cut to 80 characters, so
         a long argument arrives truncated; a truncated prefix (of a
         path or a bare file name) is resolved by unique prefix match against
         the vault's `.md` files (`.obsidian/` and `.trash/` excluded). Zero or
         several matches skip the path. A complete name without an extension
         gets `.md` appended.
  cited  The assistant message's `sources` carries vault entries with a
         `file_path` or `obsidian_path`: files the answer cited. These are
         biased toward whatever the live retriever returned.

Usage:
    python scripts/retrieval_eval/mine_pairs.py [--db data/conversations.db]
        [--out data/retrieval_eval/pairs.jsonl] [--vault DIR]
        [--manual FILE.yaml|FILE.jsonl] [--manual-only]

Records with the same source, query and relevant files collapse into one
carrying `count`, the number of turns that produced it.

Prints counts only. The output file holds real queries and file names; it lives
under data/ and is never committed.
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_READ_CALL = re.compile(r"^read_vault_file\((.*)$", re.DOTALL)
_PATH_ARG = re.compile(r'"(?:path|file_path|filename)"\s*:\s*"((?:[^"\\]|\\.)*)("?)')
_EXCLUDED_DIRS = {".obsidian", ".trash"}


class VaultResolver:
    """Resolves a truncated path prefix to the one vault file it identifies."""

    def __init__(self, vault: Path):
        self.vault = Path(vault)
        self._files: list[str] | None = None

    @property
    def files(self) -> list[str]:
        if self._files is None:
            self._files = sorted(
                p.as_posix()
                for p in self.vault.rglob("*.md")
                if not _EXCLUDED_DIRS & set(p.relative_to(self.vault).parts)
            )
        return self._files

    def resolve(self, prefix: str) -> tuple[str, str | None]:
        """Return ("match", path), ("ambiguous", None) or ("nomatch", None)."""
        root = self.vault.as_posix().rstrip("/") + "/"
        if prefix.startswith("/"):
            hits = [f for f in self.files if f.startswith(prefix)]
        else:
            hits = [f for f in self.files
                    if f[len(root):].startswith(prefix) or f.rsplit("/", 1)[-1].startswith(prefix)]
        if len(hits) == 1:
            return "match", hits[0]
        return ("ambiguous", None) if hits else ("nomatch", None)


def _json(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def _decode(fragment: str) -> str:
    """Decode a JSON string body that may end in a cut-off escape sequence."""
    while True:
        try:
            return json.loads(f'"{fragment}"')
        except ValueError:
            cut = fragment.rfind("\\")
            if cut < 0:
                return fragment
            fragment = fragment[:cut]


def new_stats() -> dict:
    return {"skipped_truncated_ambiguous": 0, "skipped_truncated_nomatch": 0}


def files_read(sources, resolver: VaultResolver | None, stats: dict) -> list[str]:
    """Vault file paths read by `read_vault_file` calls in a `sources` value."""
    out: list[str] = []
    for entry in sources if isinstance(sources, list) else []:
        if not isinstance(entry, dict):
            continue
        m = _READ_CALL.match(entry.get("file_name") or "")
        p = _PATH_ARG.search(m.group(1)) if m else None
        if not p:
            continue
        path = _decode(p.group(1))
        if p.group(2):  # closing quote present: the path is complete
            out.append(path if path.lower().endswith(".md") else path + ".md")
            continue
        status, hit = resolver.resolve(path) if resolver else ("nomatch", None)
        if hit:
            out.append(hit)
        else:
            stats[f"skipped_truncated_{status}"] += 1
    return list(dict.fromkeys(out))


def files_cited(sources) -> list[str]:
    out: list[str] = []
    for entry in sources if isinstance(sources, list) else []:
        if isinstance(entry, dict) and entry.get("source_type") == "vault":
            ref = entry.get("file_path") or entry.get("obsidian_path")
            if ref:
                out.append(ref)
    return list(dict.fromkeys(out))


def mine(db_path: Path, resolver: VaultResolver | None = None, stats: dict | None = None) -> list[dict]:
    stats = stats if stats is not None else new_stats()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT conversation_id, role, content, sources, routing FROM messages "
            "ORDER BY conversation_id, created_at, rowid"
        ).fetchall()
    finally:
        conn.close()
    pairs: list[dict] = []
    last_user: tuple[str, str] | None = None
    for conv, role, content, sources, routing in rows:
        if role == "user":
            last_user = (conv, content or "")
            continue
        if role != "assistant" or not last_user or last_user[0] != conv:
            continue
        query = last_user[1].strip()
        last_user = None
        if not query:
            continue
        src = _json(sources)
        tools = (_json(routing) or {}).get("sources") or []
        if "search_vault" in tools:
            read = files_read(src, resolver, stats)
            if read:
                pairs.append({"query": query, "relevant_files": read, "source": "mined"})
        cited = files_cited(src)
        if cited:
            pairs.append({"query": query, "relevant_files": cited, "source": "cited"})
    return pairs


def dedupe(pairs: list[dict]) -> list[dict]:
    """Collapse records with the same source, query and file set into one with a `count`.

    Queries compare stripped and case-folded; the first-seen record and order win.
    """
    merged: dict[tuple, dict] = {}
    for p in pairs:
        key = (p["source"], p["query"].strip().lower(), tuple(sorted(p["relevant_files"])))
        if key in merged:
            merged[key]["count"] += 1
        else:
            merged[key] = {**p, "count": 1}
    return list(merged.values())


def load_manual(path: Path) -> list[dict]:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        items = yaml.safe_load(text) or []
    else:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    out = []
    for it in items:
        files = it.get("relevant_files") or []
        if it.get("query") and files:
            out.append({"query": it["query"], "relevant_files": list(files), "source": "manual"})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=str(REPO / "data/conversations.db"))
    ap.add_argument("--out", default=str(REPO / "data/retrieval_eval/pairs.jsonl"))
    ap.add_argument("--vault", help="vault directory used to resolve truncated paths "
                                    "(default: settings.vault_path)")
    ap.add_argument("--manual", help="YAML or JSONL file of operator-written pairs to append")
    ap.add_argument("--manual-only", action="store_true",
                    help="skip mining; append --manual pairs to the existing output file")
    args = ap.parse_args(argv)

    stats = new_stats()
    pairs: list[dict] = []
    deduped = 0
    if not args.manual_only:
        vault = args.vault
        if not vault:
            sys.path.insert(0, str(REPO))
            from config.settings import settings

            vault = settings.vault_path
        raw = mine(Path(args.db), VaultResolver(Path(vault)), stats)
        pairs = dedupe(raw)
        deduped = len(raw) - len(pairs)
    manual = load_manual(Path(args.manual)) if args.manual else []
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = out.read_text().splitlines() if args.manual_only and out.exists() else []
    with out.open("w") as f:
        for line in kept:
            f.write(line + "\n")
        for rec in pairs + manual:
            f.write(json.dumps(rec) + "\n")
    n = lambda s: sum(1 for p in pairs if p["source"] == s)  # noqa: E731
    print(f"mined={n('mined')} cited={n('cited')} manual={len(manual)} "
          f"skipped_truncated_ambiguous={stats['skipped_truncated_ambiguous']} "
          f"skipped_truncated_nomatch={stats['skipped_truncated_nomatch']} deduped={deduped} kept={len(kept) + len(pairs) + len(manual)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
