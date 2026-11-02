#!/usr/bin/env python3
"""
Generate queued LLM summaries for vault notes into BM25.

The queue holds notes the 15-minute vault index job indexed and notes whose
summary failed during a reindex. Runs last in the nightly sync. It depends only on
the LLM answering. With notes queued it waits up to LLM_READY_WAIT_SECONDS for a
restarted LLM to load its model, then reports SYNC_SKIPPED and leaves the queue as
it is. It holds no lock while it works, so the job keeps running: see
summarizer.drain_summary_queue for how a note rewritten meanwhile stays correct.

An LLM that answers its health check but keeps failing to generate ends the pass
early. That exits non-zero, so the sync records this source as failed. Logs go to
stdout, leaving stderr, which the sync shows as the error, with that one line.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
import time

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s', stream=sys.stdout)
logger = logging.getLogger(__name__)


_LLM_POLL_SECONDS = 5


def _wait_for_llm() -> bool:
    """Whether the summarizer LLM answers within the wait."""
    from api.services import summarizer

    deadline = time.monotonic() + summarizer.LLM_READY_WAIT_SECONDS
    while not summarizer.is_summarizer_llm_available():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LLM_POLL_SECONDS)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Generate queued vault note summaries')
    parser.add_argument('--execute', action='store_true', help='Actually generate summaries (default: report the queue)')
    args = parser.parse_args(argv)

    from api.services.bm25_index import BM25Index
    from api.services.summarizer import LLM_READY_WAIT_SECONDS, drain_summary_queue, load_summary_failures
    from api.services.sync_health import emit_sync_stats

    queued = len(load_summary_failures().get("files", []))
    if not args.execute:
        logger.info(f"DRY RUN - {queued} summaries queued")
        return 0
    if not queued:
        logger.info("Summary queue is empty")
        emit_sync_stats({"processed": 0, "remaining": 0})
        return 0
    if not _wait_for_llm():
        print(
            f"SYNC_SKIPPED: summarizer LLM not answering after {LLM_READY_WAIT_SECONDS}s; "
            f"{queued} summaries stay queued",
            flush=True,
        )
        return 0

    stats = drain_summary_queue(BM25Index())
    emit_sync_stats({"processed": stats["generated"] + stats["fallback"], "errors": stats["failed"], **stats})
    if stats["stopped_on_failures"]:
        print(
            f"Summary pass stopped: {stats['failed']} failed while the LLM reported healthy; "
            f"{stats['remaining']} summaries stay queued",
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
