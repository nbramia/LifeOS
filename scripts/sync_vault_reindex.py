#!/usr/bin/env python3
"""
Reindex the Obsidian vault to ChromaDB and BM25.

This script triggers indexing of vault notes, which:
1. Chunks documents and indexes to ChromaDB (vector search)
2. Indexes to BM25 (keyword search)
3. Extracts people mentions and syncs to PersonEntity via EntityResolver
4. Creates vault mention interactions in InteractionStore

By default uses incremental indexing (only changed files).
Use --force for full reindex (required when changing embedding models).

Should run AFTER all CRM data collection is complete so that entity
resolution has access to the latest people data.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
import time

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def clear_vector_store():
    """Clear the ChromaDB collection (needed when changing embedding dimensions)."""
    from api.services.vectorstore import VectorStore

    logger.info("Clearing existing vector store collection...")
    store = VectorStore()
    # Delete and recreate the collection
    store._client.delete_collection(store.collection_name)
    store._collection = store._client.get_or_create_collection(
        name=store.collection_name,
        metadata={"hnsw:space": "cosine"}
    )
    logger.info("Vector store cleared")


def sync_vault_reindex(dry_run: bool = True, force: bool = False, skip_summaries: bool = False) -> dict:
    """
    Reindex the Obsidian vault.

    Args:
        dry_run: If True, just report what would happen
        force: If True, do full reindex (not incremental)

    Returns:
        Stats dict
    """
    from config.settings import settings
    from api.services.indexer import IndexerService

    vault_path = settings.vault_path

    if not Path(vault_path).exists():
        logger.error(f"Vault path not found: {vault_path}")
        return {"status": "error", "reason": "vault_not_found"}

    # Count markdown files
    md_files = list(Path(vault_path).rglob("*.md"))
    logger.info(f"Found {len(md_files)} markdown files in vault")

    if dry_run:
        logger.info("DRY RUN - would reindex vault")
        logger.info(f"  Vault path: {vault_path}")
        logger.info(f"  Files to index: {len(md_files)}")
        logger.info(f"  Mode: {'FULL (force)' if force else 'incremental'}")
        return {"status": "dry_run", "files_found": len(md_files)}

    # If forcing full reindex, clear the index state file to ensure all files are indexed
    if force:
        state_file = Path(IndexerService.INDEX_STATE_FILE)
        if state_file.exists():
            logger.info("Removing index state file for full reindex...")
            state_file.unlink()

    logger.info(f"Starting vault reindex for {vault_path}...")
    logger.info(f"Mode: {'FULL (force)' if force else 'incremental'}")
    start_time = time.time()

    indexer = IndexerService(vault_path=vault_path)
    files_indexed = indexer.index_all(force=force, skip_summaries=skip_summaries)

    elapsed = time.time() - start_time

    logger.info("\n=== Vault Reindex Results ===")
    logger.info(f"  Files indexed: {files_indexed}")
    logger.info(f"  Time elapsed: {elapsed:.1f}s ({elapsed/60:.1f} min)")

    return {
        "status": "success",
        "files_indexed": files_indexed,
        "elapsed_seconds": round(elapsed, 1),
    }


def clear_bm25_index():
    """Drop all BM25 chunks. Use when the index has stale doc_ids (e.g. after
    migrating the vault from a different OS path, so the macOS doc_ids
    accumulated alongside the current Linux ones)."""
    from api.services.bm25_index import BM25Index
    index = BM25Index()
    before = index.count()
    index.clear()
    logger.info(f"BM25 index cleared: {before} rows dropped")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Reindex Obsidian vault')
    parser.add_argument('--execute', action='store_true', help='Actually perform reindex')
    parser.add_argument('--force', action='store_true', help='Force full reindex (not incremental)')
    parser.add_argument('--clear-vectors', action='store_true', help='Clear vector store before indexing (use when changing embedding model)')
    parser.add_argument('--clear-bm25', action='store_true', help='Clear BM25 index before indexing (use to drop stale doc_ids from a previous vault path)')
    parser.add_argument('--skip-summaries', action='store_true', help='Skip LLM summary generation for faster indexing')
    args = parser.parse_args()

    if args.clear_vectors and args.execute:
        clear_vector_store()
    if args.clear_bm25 and args.execute:
        clear_bm25_index()

    result = sync_vault_reindex(dry_run=not args.execute, force=args.force, skip_summaries=args.skip_summaries)

    # Canonical line consumed by run_all_syncs._parse_sync_output. "processed"
    # is the file count so the orchestrator's generic counter matches reality.
    from api.services.sync_health import emit_sync_stats
    emit_sync_stats({
        "processed": int(result.get("files_indexed", 0) or 0),
    })
