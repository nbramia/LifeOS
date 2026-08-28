#!/usr/bin/env python3
"""
One-off cleanup for #828: remove stray rows written by a test run that
indexed a synthetic tmp vault into the live ChromaDB collection instead of
an isolated one.

The stray rows this was written for have a `file_path` like
`/tmp/tmpXXXXXXXX/vault/...` — an absolute path under an OS temp
directory, carrying real vault `note_type` values (Personal/Work/ML). Real
vault documents are always indexed under the configured vault root, never
under `/tmp`, and non-vault sources (calendar events, Slack messages) use
*relative* pseudo-paths ("calendar/<event-id>"), not absolute ones — so a
strict `/tmp/` prefix check can't match either, only the actual debris.

Dry-run by default; pass --apply to actually delete. Always paginates
`collection.get()` with a bounded `limit`/`offset` rather than fetching
everything at once — the live collection has ~45k rows, and fetching them
all in one `.get()` risks chromadb's sqlite backend hitting "too many SQL
variables".

Usage:
    python3 scripts/cleanup_vectorstore_tmp_rows.py               # dry run
    python3 scripts/cleanup_vectorstore_tmp_rows.py --apply        # delete
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PAGE_SIZE = 500
DEFAULT_DELETE_BATCH_SIZE = 200


def is_stray_tmp_row(file_path: str) -> bool:
    """True only for an absolute file_path rooted at an OS temp directory.

    - Real vault documents always store an absolute, resolved path under
      the configured vault root (see `indexer.py`) — never under `/tmp`.
    - Non-vault sources (calendar events, Slack messages) use relative
      pseudo-paths, which can't start with `/tmp/` either.
    - A prefix check (not a substring check) so a real document that merely
      *mentions* "/tmp/" somewhere in its path is never swept up.
    """
    return bool(file_path) and file_path.startswith("/tmp/")


def find_stray_rows(collection, page_size: int = DEFAULT_PAGE_SIZE) -> list[tuple[str, str]]:
    """Page through `collection` and return (id, file_path) for every row
    whose file_path looks like tmp-vault test debris."""
    stray: list[tuple[str, str]] = []
    offset = 0
    while True:
        page = collection.get(limit=page_size, offset=offset, include=["metadatas"])
        ids = page.get("ids") or []
        if not ids:
            break
        metadatas = page.get("metadatas") or []
        for row_id, meta in zip(ids, metadatas):
            file_path = (meta or {}).get("file_path", "")
            if is_stray_tmp_row(file_path):
                stray.append((row_id, file_path))
        offset += len(ids)
        if len(ids) < page_size:
            break
    return stray


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually delete the stray rows (default: dry run)")
    parser.add_argument("--collection", default="lifeos_vault", help="Collection to scan (default: lifeos_vault)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Rows per .get() page")
    parser.add_argument("--delete-batch-size", type=int, default=DEFAULT_DELETE_BATCH_SIZE)
    args = parser.parse_args()

    from api.services.vectorstore import VectorStore

    store = VectorStore(collection_name=args.collection)
    total = store.get_document_count()
    print(f"Collection '{args.collection}': {total} total row(s)")

    stray = find_stray_rows(store._collection, page_size=args.page_size)
    print(f"Found {len(stray)} stray row(s) with a file_path under /tmp/")

    preview_limit = 20
    for row_id, file_path in stray[:preview_limit]:
        print(f"  {row_id}  {file_path}")
    if len(stray) > preview_limit:
        print(f"  ... and {len(stray) - preview_limit} more")

    if not stray:
        print("Nothing to clean up.")
        return

    if not args.apply:
        print("\nDry run only — pass --apply to delete these rows.")
        return

    ids = [row_id for row_id, _ in stray]
    deleted = 0
    for i in range(0, len(ids), args.delete_batch_size):
        batch = ids[i:i + args.delete_batch_size]
        store._collection.delete(ids=batch)
        deleted += len(batch)
    print(f"\nDeleted {deleted} row(s) from '{args.collection}'.")


if __name__ == "__main__":
    main()
