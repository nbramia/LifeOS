"""
Document summarization service using local LLM.

Generates brief summaries for document discovery and high-level search.
Talks to an OpenAI-compatible LLM server: ``settings.summarizer_llm_url`` when
set, otherwise ``settings.local_llm_url`` — the same endpoint the orchestrator
uses, so there's no second model to keep running by default. Hosts without a
llama-server can point the override at Ollama instead (see
``settings.summarizer_model``, which llama-server ignores but Ollama requires).

## Tiered Summarization

Files are assigned to tiers based on their directory:
- SKIP: No LLM summary (zArchive, Granola, Attachments) - embeddings only
- HIGH: Detailed summaries (Personal, Work, Omi, LifeOS) - important content

## Retry Logic

Files that fail summary generation (timeout, etc.) join a queue, and so do the
notes the 15-minute vault index job indexes. The ``vault_summaries`` sync source
works through it nightly with a simpler prompt and longer timeout.

## Usage

    from api.services.summarizer import generate_summary, get_summary_tier, SummaryTier

    tier = get_summary_tier(file_path)
    if tier != SummaryTier.SKIP:
        summary = generate_summary(content, file_name)
"""
import contextlib
import fcntl
import httpx
import json
import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Track files that failed summary generation for retry
SUMMARY_FAILURES_FILE = "data/vault_summary_failures.json"

# Content shorter than this is not worth summarizing and gets no summary.
MIN_SUMMARY_CONTENT_CHARS = 100

# How long the vault_summaries source waits for an LLM that isn't answering yet.
# A restarted llama-server takes a while to load its model.
LLM_READY_WAIT_SECONDS = 600

# How many failures in a row end a drain pass. That many point to a stalled LLM.
MAX_CONSECUTIVE_SUMMARY_FAILURES = 3
# Passes a note may fail before it gets a fallback summary and leaves the queue.
MAX_SUMMARY_ATTEMPTS = 3


class SummaryTier(Enum):
    """Summarization priority tiers based on directory."""
    SKIP = "skip"      # No LLM summary, embeddings only
    HIGH = "high"      # Detailed summary for important content


# Directory patterns for each tier (case-insensitive matching)
# Directories not listed default to HIGH (summarize everything important)
SKIP_DIRECTORIES = {
    "zarchive",
    "granola",
    "attachments",
}

# Nested folders (vault-relative, lowercase) that are never summarized. The
# generated index pages live here; summarizing them would only describe
# generated text.
SKIP_PATH_PREFIXES = (
    "wiki/vault map",
)

HIGH_DIRECTORIES = {
    "personal",
    "work",
    "omi",
    "pendant",
    "lifeos",
}


def get_summary_tier(file_path: str) -> SummaryTier:
    """
    Determine the summary tier for a file based on its path.

    Args:
        file_path: Full path to the file

    Returns:
        SummaryTier indicating how to handle summarization
    """
    path = Path(file_path)

    # Get the top-level directory within the vault
    # Check if path is inside the vault, then get the first subdirectory
    vault_str = str(settings.vault_path)
    file_str = str(path)
    if file_str.startswith(vault_str):
        relative = file_str[len(vault_str):].strip("/")
        top_dir = relative.split("/")[0].lower() if "/" in relative else ""
        if top_dir in SKIP_DIRECTORIES:
            return SummaryTier.SKIP
        rel_lower = relative.lower()
        if any(rel_lower == p or rel_lower.startswith(p + "/") for p in SKIP_PATH_PREFIXES):
            return SummaryTier.SKIP

    # Default: summarize (HIGH tier)
    return SummaryTier.HIGH


SUMMARY_PROMPT = """Write ONE brief sentence describing this document's type and main topic. Be extremely concise (under 100 words).

{content}

One-sentence summary:"""

# Simpler prompt for retry attempts - faster to process
RETRY_PROMPT = """Describe this document in one sentence:

{content}

Summary:"""


def generate_summary(
    content: str,
    file_name: str,
    max_content_chars: int = 2000,
    timeout: int = None,
    use_retry_prompt: bool = False
) -> tuple[Optional[str], bool]:
    """
    Generate a document summary using the local OpenAI-compatible LLM server.

    Args:
        content: Document content to summarize
        file_name: Name of file (for logging)
        max_content_chars: Max chars to send to LLM
        timeout: Timeout in seconds for LLM call
        use_retry_prompt: If True, use simpler retry prompt

    Returns:
        Tuple of (summary or None, success bool).
        On failure, returns (None, False) so caller can track for retry.
    """
    # Use settings timeout if not provided
    if timeout is None:
        timeout = settings.ollama_timeout

    if len(content) < MIN_SUMMARY_CONTENT_CHARS:
        # Too short to summarize meaningfully
        return None, True  # Not a failure, just skip

    try:
        # Truncate content if needed
        truncated = content[:max_content_chars]
        if len(content) > max_content_chars:
            truncated += "\n[... content truncated ...]"

        # Use simpler prompt for retries
        prompt_template = RETRY_PROMPT if use_retry_prompt else SUMMARY_PROMPT
        prompt = prompt_template.format(content=truncated)

        # OpenAI-compatible chat completions against the local llama-server.
        # llama-server ignores the "model" field (one model per process), so the
        # default is the placeholder "local"; the OpenAI spec requires the key
        # to be present. Ollama does NOT ignore it and needs a real tag, which
        # is what settings.summarizer_model is for.
        url = f"{_summarizer_base_url()}/v1/chat/completions"
        payload = {
            "model": settings.summarizer_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 512,
            "stream": False,
            # A one-sentence summary needs no reasoning. With thinking on, the
            # model can spend the whole 512-token budget reasoning and return
            # empty content, which counts as a failure.
            "chat_template_kwargs": {"enable_thinking": False},
        }

        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            summary = ""
            if choices:
                summary = (choices[0].get("message", {}).get("content") or "").strip()

        # Validate summary (increased max for 7B model verbosity)
        if len(summary) < 20 or len(summary) > 1000:
            logger.warning(f"Invalid summary length for {file_name}: {len(summary)}")
            return None, False  # Mark as failure for retry

        logger.debug(f"Generated summary for {file_name}: {summary[:50]}...")
        return summary, True

    except httpx.TimeoutException as e:
        logger.warning(f"LLM timeout for {file_name}: {e}")
        return None, False  # Mark as failure for retry
    except httpx.ConnectError as e:
        logger.warning(f"LLM connection failed for {file_name}: {e}")
        return None, False  # Mark as failure for retry
    except Exception as e:
        logger.warning(f"Summary generation failed for {file_name}: {e}")
        return None, False  # Mark as failure for retry


def retry_summary(
    content: str,
    file_name: str,
    max_content_chars: int = 1500,  # Slightly less content for retry
) -> Optional[str]:
    """
    Retry summary generation with simpler prompt and longer timeout.

    Args:
        content: Document content to summarize
        file_name: Name of file (for logging)
        max_content_chars: Max chars to send to LLM (smaller for retry)

    Returns:
        Summary string, a fallback summary if the content is too short for a
        real one, or None if the LLM failed
    """
    summary, success = generate_summary(
        content=content,
        file_name=file_name,
        max_content_chars=max_content_chars,
        timeout=settings.ollama_retry_timeout,
        use_retry_prompt=True
    )

    if not success:
        logger.warning(f"Retry failed for {file_name}")
        return None
    if summary:
        logger.info(f"Retry succeeded for {file_name}")
        return summary
    return _fallback_summary(content, file_name)


def load_summary_failures() -> dict:
    """Load the list of files that failed summary generation."""
    failures_path = Path(SUMMARY_FAILURES_FILE)
    if failures_path.exists():
        try:
            return json.loads(failures_path.read_text())
        except Exception as e:
            logger.warning(f"Failed to load summary failures: {e}")
    return {"files": []}


def save_summary_failures(failures: dict) -> None:
    """Save the list of files that failed summary generation."""
    failures_path = Path(SUMMARY_FAILURES_FILE)
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    # Replaced atomically: a torn file would load as an empty queue.
    tmp_path = failures_path.with_name(f"{failures_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(failures, indent=2))
    os.replace(tmp_path, failures_path)


@contextlib.contextmanager
def _summary_queue_lock() -> Iterator[None]:
    """Serialize read-modify-write of the queue across processes and threads.

    On a sibling file, because the queue file itself is replaced on every save.
    Held only for one read and one write. The kernel drops it if the holder dies.
    """
    lock_path = Path(f"{SUMMARY_FAILURES_FILE}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _find_entry(failures: dict, file_path: str) -> Optional[dict]:
    return next((f for f in failures["files"] if f["file_path"] == file_path), None)


def _invalidate_entry(entry: dict) -> None:
    """Mark an entry's note as changed, so a summary in progress is discarded."""
    entry["generation"] = entry.get("generation", 0) + 1
    entry.pop("attempts", None)


def add_summary_failure(file_path: str, file_name: str) -> None:
    """Queue a file for a summary, or mark it changed if it is already queued."""
    with _summary_queue_lock():
        failures = load_summary_failures()
        entry = _find_entry(failures, file_path)
        if entry is None:
            failures["files"].append({
                "file_path": file_path,
                "file_name": file_name
            })
        else:
            _invalidate_entry(entry)
        save_summary_failures(failures)


def invalidate_queued_summary(file_path: str) -> None:
    """Discard any summary in progress for a note about to be rewritten or removed.

    Called before the note's chunks and summary leave BM25. Any summary the drain
    wrote earlier is then deleted with them, and any it writes later is refused.
    """
    if not Path(SUMMARY_FAILURES_FILE).exists():
        return
    with _summary_queue_lock():
        failures = load_summary_failures()
        entry = _find_entry(failures, file_path)
        if entry is not None:
            _invalidate_entry(entry)
            save_summary_failures(failures)


def has_no_people_reason(file_path: str, frontmatter: dict) -> bool:
    """Whether the note is bulk content (a no-people folder or marker), which gets no summary."""
    from api.services.vault_listing import no_people_reason, vault_relative_path

    rel = vault_relative_path(file_path, settings.vault_path)
    return no_people_reason(rel, frontmatter, settings.vault_no_people_paths) is not None


def queue_summary(file_path: str, file_name: str, body: str, frontmatter: dict) -> None:
    """Queue a note for a summary, unless it would get none anyway.

    Call before the note's chunks and summary leave BM25: see
    ``invalidate_queued_summary``.
    """
    if (
        get_summary_tier(file_path) == SummaryTier.SKIP
        or len(body) < MIN_SUMMARY_CONTENT_CHARS
        or has_no_people_reason(file_path, frontmatter)
    ):
        invalidate_queued_summary(file_path)
        return
    add_summary_failure(file_path, file_name)


def finish_queued_summary(file_path: str, generation: int, write: Optional[Callable[[], None]] = None) -> bool:
    """Remove an entry if its note is unchanged, first running ``write``.

    Both happen under the queue lock, so no reindex can invalidate the entry
    between the check and the write.

    Returns:
        False, without writing, if the note changed since ``generation`` was read
    """
    with _summary_queue_lock():
        failures = load_summary_failures()
        entry = _find_entry(failures, file_path)
        if entry is None or entry.get("generation", 0) != generation:
            return False
        if write is not None:
            write()
        failures["files"].remove(entry)
        save_summary_failures(failures)
        return True


def _record_failed_attempt(file_path: str, generation: int) -> int:
    """Count a failed attempt on an unchanged entry and return the total."""
    with _summary_queue_lock():
        failures = load_summary_failures()
        entry = _find_entry(failures, file_path)
        if entry is None or entry.get("generation", 0) != generation:
            return 0
        entry["attempts"] = entry.get("attempts", 0) + 1
        save_summary_failures(failures)
        return entry["attempts"]


def _drain_order(entry: dict) -> tuple[int, float]:
    """Entries with the fewest failures first, so a note that keeps failing can't
    block the rest, then newest first. A vanished note goes early, since dropping
    it costs nothing."""
    try:
        recency = os.path.getmtime(entry["file_path"])
    except OSError:
        recency = float("inf")
    return entry.get("attempts", 0), -recency


def drain_summary_queue(bm25_index) -> dict:
    """Generate queued summaries into BM25, within the configured time budget.

    Drops entries for bulk no-people notes without calling the LLM. Holds no
    lock while the LLM works, so the vault index job keeps running. A
    note rewritten meanwhile keeps its entry and gets a summary on a later pass.
    A failed attempt also keeps its entry, and the pass stops after
    ``MAX_CONSECUTIVE_SUMMARY_FAILURES`` in a row, since the LLM has probably
    stalled. A note that fails on ``MAX_SUMMARY_ATTEMPTS`` passes gets a fallback
    summary built from its first line.

    Returns:
        Dict of counts: generated, fallback, failed, changed_during_pass, remaining,
        and stopped_on_failures (1 if failures in a row ended the pass, else 0)
    """
    from api.services.chunker import extract_frontmatter
    from api.utils.date_parser import extract_note_date

    stats = {"generated": 0, "fallback": 0, "failed": 0, "changed_during_pass": 0, "stopped_on_failures": 0}
    deadline = time.monotonic() + settings.summary_retry_budget_seconds
    consecutive_failures = 0

    for queued in sorted(load_summary_failures().get("files", []), key=_drain_order):
        if time.monotonic() >= deadline:
            logger.warning(f"Summary budget of {settings.summary_retry_budget_seconds}s used; the rest stay queued")
            break

        file_path = queued["file_path"]
        file_name = queued["file_name"]
        entry = _find_entry(load_summary_failures(), file_path)
        if entry is None:
            continue
        generation = entry.get("generation", 0)

        path = Path(file_path)
        try:
            frontmatter, body = extract_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            # Gone or unreadable, so no summary can come of it. A later reindex
            # queues the note again if it comes back.
            logger.warning(f"Dropping queued summary for {file_path}: {e}")
            finish_queued_summary(file_path, generation)
            continue

        if has_no_people_reason(file_path, frontmatter):
            finish_queued_summary(file_path, generation)
            continue

        summary = retry_summary(body, file_name)
        counter = "generated"
        if summary is None:
            stats["failed"] += 1
            consecutive_failures += 1
            if _record_failed_attempt(file_path, generation) >= MAX_SUMMARY_ATTEMPTS:
                summary = _fallback_summary(body, file_name)
                counter = "fallback"
        else:
            consecutive_failures = 0

        if summary is not None:
            def write() -> None:
                bm25_index.add_document(
                    doc_id=f"{path.resolve()}::summary",
                    content=f"Document summary for {file_name}: {summary}",
                    file_name=file_name,
                    modified_date=extract_note_date(path, frontmatter, body),
                )

            try:
                if finish_queued_summary(file_path, generation, write):
                    stats[counter] += 1
                else:
                    stats["changed_during_pass"] += 1
            except Exception as e:
                logger.warning(f"Could not store the summary for {file_path}: {e}")
                stats["failed"] += 1
                consecutive_failures += 1

        if consecutive_failures >= MAX_CONSECUTIVE_SUMMARY_FAILURES:
            logger.warning(f"{consecutive_failures} summaries failed in a row; stopping this pass")
            stats["stopped_on_failures"] = 1
            break

    stats["remaining"] = len(load_summary_failures().get("files", []))
    logger.info(f"Summary pass complete: {stats}")
    return stats


def _fallback_summary(content: str, file_name: str) -> str:
    """Generate fallback summary from first content lines."""
    # Extract first meaningful line (skip frontmatter, headers)
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        # Skip empty lines, frontmatter markers, and headers
        if line and not line.startswith('#') and not line.startswith('---'):
            if len(line) > 20:
                # Clean up the line and truncate
                clean_line = line[:150]
                if len(line) > 150:
                    clean_line += "..."
                return f"Document '{file_name}': {clean_line}"

    return f"Document '{file_name}' containing various notes."


def _summarizer_base_url() -> str:
    """Endpoint for summarization calls.

    Falls back to ``local_llm_url`` when unset, so installs that never set the
    override keep their existing behaviour exactly.
    """
    return (settings.summarizer_llm_url or settings.local_llm_url).rstrip("/")


def is_summarizer_llm_available() -> bool:
    """Check if the summarization LLM server is reachable.

    Tries ``/health`` first (llama-server), then ``/v1/models`` (the
    OpenAI-compatible probe, which Ollama answers and llama-server also
    serves). Ollama has no ``/health`` route, so checking only that would
    report a perfectly healthy Ollama endpoint as unavailable.
    """
    base = _summarizer_base_url()
    for path in ("/health", "/v1/models"):
        try:
            with httpx.Client(timeout=2.0) as client:
                if client.get(f"{base}{path}").status_code == 200:
                    return True
        except Exception:
            continue
    return False


def create_summary_chunk(
    summary: str,
    file_path: str,
    file_name: str,
    metadata: dict
) -> dict:
    """
    Create a summary chunk for indexing.

    Args:
        summary: Generated summary text
        file_path: Full path to the document
        file_name: Name of the file
        metadata: Document metadata

    Returns:
        Chunk dict ready for indexing
    """
    return {
        "content": f"Document summary for {file_name}: {summary}",
        "chunk_index": -1,  # Special index for summary
        "is_summary": True,
        "file_path": file_path,
        "file_name": file_name,
        "metadata": {
            **metadata,
            "is_summary": True,
            "chunk_type": "summary"
        }
    }
