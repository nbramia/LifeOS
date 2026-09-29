#!/usr/bin/env python3
"""Mine query -> relevant-file pairs from conversation history.

A user turn qualifies when the following assistant message's `routing` lists
`search_vault` in `sources` and the persisted `sources` display column records
a `read_vault_file` call whose path is recoverable. The display column keeps
each call as `tool(json-args)` truncated to 80 characters, so a path cut off
by that truncation is skipped; entries carrying an explicit `file_path` are
used as-is.

Usage:
    python scripts/retrieval_eval/mine_pairs.py [--db data/conversations.db]
        [--out data/retrieval_eval/pairs.jsonl] [--manual FILE.yaml|FILE.jsonl]
        [--manual-only]

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
_PATH_ARG = re.compile(r'"(?:path|file_path)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _json(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def files_read(sources) -> list[str]:
    """Vault file paths recoverable from a message's `sources` column value."""
    out: list[str] = []
    for entry in sources if isinstance(sources, list) else []:
        if not isinstance(entry, dict):
            continue
        m = _READ_CALL.match(entry.get("file_name") or "")
        if m:
            p = _PATH_ARG.search(m.group(1))
            if p:
                out.append(json.loads(f'"{p.group(1)}"'))
        elif entry.get("source_type") == "vault" and entry.get("file_path"):
            out.append(entry["file_path"])
    return list(dict.fromkeys(out))


def mine(db_path: Path) -> list[dict]:
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
        tools = (_json(routing) or {}).get("sources") or []
        if "search_vault" not in tools:
            continue
        read = files_read(_json(sources))
        if read and last_user[1].strip():
            pairs.append({"query": last_user[1].strip(), "relevant_files": read, "source": "mined"})
        last_user = None
    return pairs


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
    ap.add_argument("--manual", help="YAML or JSONL file of operator-written pairs to append")
    ap.add_argument("--manual-only", action="store_true",
                    help="skip mining; append --manual pairs to the existing output file")
    args = ap.parse_args(argv)

    mined = [] if args.manual_only else mine(Path(args.db))
    manual = load_manual(Path(args.manual)) if args.manual else []
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = out.read_text().splitlines() if args.manual_only and out.exists() else []
    with out.open("w") as f:
        for line in kept:
            f.write(line + "\n")
        for rec in mined + manual:
            f.write(json.dumps(rec) + "\n")
    print(f"mined={len(mined)} manual={len(manual)} kept={len(kept)} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
