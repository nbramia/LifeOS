#!/usr/bin/env python3
"""
Generate queued LLM summaries for vault notes into BM25.

The queue holds notes the 15-minute vault index job indexed and notes whose
summary failed during a reindex. Runs last in the nightly sync. It depends only on
the LLM answering, and reports SYNC_SKIPPED when it doesn't, leaving the queue as
it is. It holds no lock while it works, so the job keeps running: see
summarizer.drain_summary_queue for how a note rewritten meanwhile stays correct.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Generate queued vault note summaries')
    parser.add_argument('--execute', action='store_true', help='Actually generate summaries (default: report the queue)')
    args = parser.parse_args(argv)

    from api.services.bm25_index import BM25Index
    from api.services.summarizer import drain_summary_queue, is_summarizer_llm_available, load_summary_failures
    from api.services.sync_health import emit_sync_stats

    queued = len(load_summary_failures().get("files", []))
    if not args.execute:
        logger.info(f"DRY RUN - {queued} summaries queued")
        return 0
    if not queued:
        logger.info("Summary queue is empty")
        emit_sync_stats({"processed": 0, "remaining": 0})
        return 0
    if not is_summarizer_llm_available():
        print(f"SYNC_SKIPPED: summarizer LLM not answering; {queued} summaries stay queued", flush=True)
        return 0

    stats = drain_summary_queue(BM25Index())
    emit_sync_stats({"processed": stats["generated"] + stats["fallback"], "errors": stats["failed"], **stats})
    return 0


if __name__ == '__main__':
    sys.exit(main())
