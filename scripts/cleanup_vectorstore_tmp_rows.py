#!/usr/bin/env python3
"""
One-off cleanup for #828: remove stray rows written by a test run that
indexed a synthetic tmp vault into the live ChromaDB collection instead of
an isolated one.

The stray rows this was written for have a `file_path` like
`/tmp/tmpXXXXXXXX/vault/...` (or, on macOS, under `/var/folders/...` /
`/private/var/folders/...` — `tempfile`'s actual default there) — an
absolute path under an OS temp directory, carrying real vault `note_type`
values (Personal/Work/ML). Real vault documents are always indexed under
the configured vault root, never under a temp directory, and non-vault
sources (calendar events, Slack messages) use *relative* pseudo-paths
("calendar/<event-id>", "slack:<channel>:<ts>"), not absolute ones — so a
prefix check against `TEMP_PREFIXES` can't match either, only the actual
debris.

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

from api.services.interaction_store import TEMP_PREFIXES  # noqa: E402

DEFAULT_PAGE_SIZE = 500
DEFAULT_DELETE_BATCH_SIZE = 200
DEFAULT_PREVIEW_LIMIT = 20


def is_stray_tmp_row(file_path, vault_root: "Path | None" = None) -> bool:
    """True only for an absolute file_path rooted at an OS temp directory.

    - Real vault documents always store an absolute, resolved path under
      the configured vault root (see `indexer.py`) — never under a temp
      directory.
    - Non-vault sources (calendar events, Slack messages) use relative
      pseudo-paths, which can't match a `TEMP_PREFIXES` prefix either.
    - Matched with a trailing separator (not a bare/substring check) so a
      real document that merely *mentions* one of these prefixes, or lives
      under an unrelated directory that happens to share the prefix string
      (e.g. `/tmp-backup/...`), is never swept up.
    - `file_path` isn't guaranteed to be a string (chromadb metadata is
      whatever was written) — a non-string value is never treated as a
      match rather than raising.
    """
    if not isinstance(file_path, str) or not file_path:
        return False
    if not any(file_path.startswith(prefix + "/") for prefix in TEMP_PREFIXES):
        return False
    if vault_root is not None:
        try:
            if Path(file_path).is_relative_to(vault_root):
                return False
        except (TypeError, ValueError):
            pass
    return True


def find_stray_rows(
    collection, page_size: int = DEFAULT_PAGE_SIZE, vault_root: "Path | None" = None
) -> list[tuple[str, str]]:
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
        for row_id, meta in zip(ids, metadatas, strict=True):
            file_path = (meta or {}).get("file_path", "")
            if is_stray_tmp_row(file_path, vault_root=vault_root):
                stray.append((row_id, file_path))
        offset += len(ids)
        if len(ids) < page_size:
            break
    return stray


def _connect_collection(collection_name: str):
    """Connect directly via chromadb, bypassing `VectorStore` -- this script
    only ever reads metadata and deletes by id, so it has no use for
    `VectorStore`'s embedding-service load (heavy, GPU-touching) or its
    `get_or_create_collection` (which would silently create an empty
    collection on a typo'd `--collection`, masking the mistake instead of
    failing it)."""
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    from config.settings import settings

    host_port = settings.chroma_url.replace("http://", "").replace("https://", "")
    parts = host_port.split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 8000

    client = chromadb.HttpClient(host=host, port=port, settings=ChromaSettings(anonymized_telemetry=False))
    return client.get_collection(collection_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually delete the stray rows (default: dry run)")
    parser.add_argument("--collection", default="lifeos_vault", help="Collection to scan (default: lifeos_vault)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Rows per .get() page")
    parser.add_argument("--delete-batch-size", type=int, default=DEFAULT_DELETE_BATCH_SIZE)
    parser.add_argument(
        "--preview-limit", type=int, default=DEFAULT_PREVIEW_LIMIT,
        help="Rows to print in a dry run (default: 20). Ignored with --apply, which always prints every row removed.",
    )
    args = parser.parse_args()

    from config.settings import settings

    try:
        collection = _connect_collection(args.collection)
    except Exception as e:
        print(f"Collection '{args.collection}' does not exist or is unreachable: {e}", file=sys.stderr)
        sys.exit(1)

    total = collection.count()
    print(f"Collection '{args.collection}': {total} total row(s)")

    vault_root = Path(settings.vault_path).resolve()
    stray = find_stray_rows(collection, page_size=args.page_size, vault_root=vault_root)
    print(f"Found {len(stray)} stray row(s) under a temp-directory prefix")

    if not stray:
        print("Nothing to clean up.")
        return

    if not args.apply:
        for row_id, file_path in stray[:args.preview_limit]:
            print(f"  {row_id}  {file_path}")
        if len(stray) > args.preview_limit:
            print(f"  ... and {len(stray) - args.preview_limit} more")
        print("\nDry run only — pass --apply to delete these rows.")
        return

    # --apply: print the full record of what's being removed (issue's
    # acceptance criteria calls for "a record of what was removed"), not
    # just a truncated preview.
    deleted = 0
    for i in range(0, len(stray), args.delete_batch_size):
        batch = stray[i:i + args.delete_batch_size]
        collection.delete(ids=[row_id for row_id, _ in batch])
        for row_id, file_path in batch:
            print(f"  deleted  {row_id}  {file_path}")
        deleted += len(batch)
    print(f"\nDeleted {deleted} row(s) from '{args.collection}'.")


if __name__ == "__main__":
    main()
