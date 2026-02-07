"""
Document summarization service using local LLM.

Generates brief summaries for document discovery and high-level search.
Uses Ollama with qwen2.5:7b-instruct for zero-cost local summarization.

## Tiered Summarization

Files are assigned to tiers based on their directory:
- SKIP: No LLM summary (zArchive, Granola, Attachments) - embeddings only
- HIGH: Detailed summaries (Personal, Work, Omi, LifeOS) - important content

## Retry Logic

Files that fail summary generation (timeout, etc.) are tracked and can be
retried with a simpler prompt and longer timeout at the end of indexing.

## Usage

    from api.services.summarizer import generate_summary, get_summary_tier, SummaryTier

    tier = get_summary_tier(file_path)
    if tier != SummaryTier.SKIP:
        summary = generate_summary(content, file_name)
"""
import httpx
import json
import logging
from enum import Enum
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Track files that failed summary generation for retry
SUMMARY_FAILURES_FILE = "data/vault_summary_failures.json"


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

HIGH_DIRECTORIES = {
    "personal",
    "work",
    "omi",
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
    # Path structure: /Users/.../Notes 2025/TopLevelDir/...
    parts = path.parts

    # Find "Notes 2025" in path and get the next part
    for i, part in enumerate(parts):
        if "Notes" in part and "2025" in part:
            if i + 1 < len(parts):
                top_dir = parts[i + 1].lower()

                if top_dir in SKIP_DIRECTORIES:
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
    Generate a document summary using local LLM.

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

    if len(content) < 100:
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

        # Call Ollama synchronously
        url = f"{settings.ollama_host}/api/generate"
        payload = {
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.2,  # Low temperature for factual summary
                "num_predict": 75,   # Force very brief response
            }
        }

        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            summary = data.get("response", "").strip()

        # Validate summary (increased max for 7B model verbosity)
        if len(summary) < 20 or len(summary) > 1000:
            logger.warning(f"Invalid summary length for {file_name}: {len(summary)}")
            return None, False  # Mark as failure for retry

        logger.debug(f"Generated summary for {file_name}: {summary[:50]}...")
        return summary, True

    except httpx.TimeoutException as e:
        logger.warning(f"Ollama timeout for {file_name}: {e}")
        return None, False  # Mark as failure for retry
    except httpx.ConnectError as e:
        logger.warning(f"Ollama connection failed for {file_name}: {e}")
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
        Summary string, or fallback summary if retry also fails
    """
    summary, success = generate_summary(
        content=content,
        file_name=file_name,
        max_content_chars=max_content_chars,
        timeout=settings.ollama_retry_timeout,
        use_retry_prompt=True
    )

    if success and summary:
        logger.info(f"Retry succeeded for {file_name}")
        return summary

    # If retry also fails, use fallback
    logger.warning(f"Retry also failed for {file_name}, using fallback")
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
    failures_path.write_text(json.dumps(failures, indent=2))


def add_summary_failure(file_path: str, file_name: str) -> None:
    """Add a file to the failure list for retry."""
    failures = load_summary_failures()
    # Avoid duplicates
    if not any(f["file_path"] == file_path for f in failures["files"]):
        failures["files"].append({
            "file_path": file_path,
            "file_name": file_name
        })
        save_summary_failures(failures)


def clear_summary_failures() -> None:
    """Clear the summary failures list."""
    save_summary_failures({"files": []})


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


def is_ollama_available() -> bool:
    """Check if Ollama server is available for summarization."""
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(settings.ollama_host)
            return response.status_code == 200
    except Exception:
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
