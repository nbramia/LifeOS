"""
Markdown parsing and chunking service for LifeOS.

Chunking strategies:
- Granola notes: by section headers + action items as separate chunks
- Long notes (>500 tokens): ~500 token chunks with overlap
- Short notes (<500 tokens): whole note as single chunk

Every chunk is held to a hard token limit (``max_chunk_tokens``), measured in
the embedding model's own tokens, whatever the strategy produced: tables, URLs
and unbroken runs of text split on token boundaries, so no chunk can reach the
embedding model longer than the limit and no text is dropped.

Contextual chunking (P9.1):
- Each chunk is prefixed with 1-2 sentences describing document context
- This helps embeddings understand what document the chunk is from
- Expected to improve retrieval accuracy by 35-50%
"""
import logging
import math
import re
import threading
from pathlib import Path
import frontmatter
import tiktoken

from config.settings import settings

logger = logging.getLogger(__name__)


# Use cl100k_base tokenizer (same as GPT-4/Claude)
try:
    TOKENIZER = tiktoken.get_encoding("cl100k_base")
except Exception:
    TOKENIZER = None


def count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken."""
    if TOKENIZER is None:
        # Fallback: rough estimate of 4 chars per token
        return len(text) // 4
    return len(TOKENIZER.encode(text))


# Tokens kept free below the embedding ceiling for the contextual prefix that
# add_context_to_chunks() prepends after chunking (file name, attendees, tag
# phrase, part counter), so a chunk at the limit still fits once prefixed.
CONTEXT_PREFIX_RESERVE_TOKENS = 256

# Characters per token assumed when no tokenizer is available at all (the same
# rough figure count_tokens() falls back to).
FALLBACK_CHARS_PER_TOKEN = 4


def max_chunk_tokens() -> int:
    """Hard limit on a chunk's embedding-model tokens, before its contextual prefix."""
    return settings.embedding_max_seq_length - CONTEXT_PREFIX_RESERVE_TOKENS


_UNLOADED = object()
_embedding_tokenizer = _UNLOADED
_tokenizer_lock = threading.Lock()


def _get_embedding_tokenizer():
    """The embedding model's fast tokenizer from the local cache, or None.

    Never touches the network (``local_files_only``): the embedding service
    loads the model the same way, so a host that can embed has these files.
    """
    global _embedding_tokenizer
    if _embedding_tokenizer is _UNLOADED:
        with _tokenizer_lock:
            if _embedding_tokenizer is _UNLOADED:
                try:
                    from transformers import AutoTokenizer
                    tokenizer = AutoTokenizer.from_pretrained(
                        settings.embedding_model,
                        cache_dir=settings.embedding_cache_dir or None,
                        local_files_only=True,
                    )
                    _embedding_tokenizer = tokenizer if tokenizer.is_fast else None
                except Exception as exc:
                    logger.warning(
                        "Embedding tokenizer unavailable (%s); chunk sizes are measured "
                        "with the fallback tokenizer", exc.__class__.__name__,
                    )
                    _embedding_tokenizer = None
    return _embedding_tokenizer


def _token_starts(text: str) -> list[int]:
    """Character offset where each token of ``text`` begins."""
    tokenizer = _get_embedding_tokenizer()
    if tokenizer is not None:
        encoded = tokenizer(
            text, add_special_tokens=False, return_offsets_mapping=True, verbose=False
        )
        return [start for start, _end in encoded["offset_mapping"]]
    if TOKENIZER is not None:
        _decoded, offsets = TOKENIZER.decode_with_offsets(TOKENIZER.encode(text))
        return list(offsets)
    return list(range(0, len(text), FALLBACK_CHARS_PER_TOKEN))


def _count_embedding_tokens(texts: list[str]) -> list[int]:
    """Token count of each text, in the tokens the embedding model sees."""
    tokenizer = _get_embedding_tokenizer()
    if tokenizer is not None and texts:
        ids = tokenizer(texts, add_special_tokens=False, verbose=False)["input_ids"]
        return [len(i) for i in ids]
    return [len(_token_starts(text)) for text in texts]


def _split_to_limit(text: str, limit: int, overlap: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` tokens, on token boundaries.

    Pieces are of near-equal size (no short remainder) and consecutive pieces
    share ``overlap`` tokens, so every character of ``text`` lies in a piece.
    """
    starts = _token_starts(text)
    n = len(starts)
    if n <= limit:
        return [text]

    overlap = min(overlap, limit // 4)
    pieces_needed = math.ceil((n - overlap) / (limit - overlap))
    stride = math.ceil((n - overlap) / pieces_needed)

    pieces = []
    for i in range(pieces_needed):
        first = i * stride
        last = min(n, first + stride + overlap)
        lo = 0 if i == 0 else starts[first]
        hi = len(text) if last >= n else starts[last]
        pieces.append(text[lo:hi].strip())
    pieces = [piece for piece in pieces if piece]

    if len(pieces) < 2 or any(len(piece) >= len(text.strip()) for piece in pieces):
        # Offsets that do not advance (a tokenizer reporting none): cut by characters.
        size = math.ceil(len(text) / pieces_needed)
        pieces = [text[i:i + size].strip() for i in range(0, len(text), size)]
        pieces = [piece for piece in pieces if piece]

    # A piece re-tokenized alone can differ by a token at its cut; re-check it.
    result: list[str] = []
    for piece in pieces:
        result.extend(_split_to_limit(piece, limit, overlap))
    return result


def enforce_token_limit(chunks: list[dict], limit: int | None = None, overlap: int = 50) -> list[dict]:
    """Split every chunk longer than ``limit`` embedding tokens.

    Chunks within the limit are returned untouched. A split chunk keeps its
    other fields (e.g. ``header``) on each piece; ``chunk_index`` is left for
    the caller to renumber.
    """
    limit = limit or max_chunk_tokens()
    counts = _count_embedding_tokens([chunk["content"] for chunk in chunks])
    limited: list[dict] = []
    for chunk, count in zip(chunks, counts):
        if count <= limit:
            limited.append(chunk)
            continue
        for piece in _split_to_limit(chunk["content"], limit, overlap):
            limited.append({**chunk, "content": piece})
    return limited


def extract_frontmatter(content: str) -> tuple[dict, str]:
    """
    Extract YAML frontmatter from markdown content.

    Returns:
        Tuple of (frontmatter_dict, body_content)
    """
    try:
        post = frontmatter.loads(content)
        return dict(post.metadata), post.content
    except Exception:
        return {}, content


def normalize_tags(value) -> list[str]:
    """
    Normalize a frontmatter ``tags`` value into a clean list of strings.

    Accepts a list, a comma- and/or whitespace-separated string, a single word,
    None, or any other scalar. Leading ``#`` is stripped, whitespace trimmed,
    empty entries dropped, and duplicates removed keeping first-seen order.
    Nested tags such as ``people/jane`` are kept intact. Never raises.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    if isinstance(value, set):
        items = sorted(str(item) for item in value)

    tags: list[str] = []
    for item in items:
        if item is None:
            continue
        for part in re.split(r"[,\s]+", str(item)):
            tag = part.lstrip("#").strip()
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def parse_markdown(content: str) -> list[dict]:
    """
    Parse markdown into sections by headers.

    Returns list of dicts with 'header', 'level', 'content' keys.
    """
    sections = []

    # Split by headers (# ## ### etc)
    header_pattern = r'^(#{1,6})\s+(.+)$'
    lines = content.split('\n')

    current_section = {
        "header": "",
        "level": 0,
        "content": ""
    }

    for line in lines:
        header_match = re.match(header_pattern, line)
        if header_match:
            # Save previous section if it has content
            if current_section["content"].strip():
                sections.append(current_section)

            # Start new section
            level = len(header_match.group(1))
            header = header_match.group(2).strip()
            current_section = {
                "header": header,
                "level": level,
                "content": line + "\n"
            }
        else:
            current_section["content"] += line + "\n"

    # Don't forget the last section
    if current_section["content"].strip():
        sections.append(current_section)

    return sections


def chunk_by_headers(
    content: str,
    max_chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> list[dict]:
    """
    Chunk content by H2 headers (Granola-style).

    Each H2 section becomes its own chunk, but if a single section exceeds
    ``max_chunk_size`` tokens it's re-split via :func:`chunk_by_tokens`.
    Without this guard a very long unbroken section (e.g. a Granola note with
    no H2 separators, or a single H2 that contains the entire transcript)
    produces a single oversize chunk that explodes embedding-model memory at
    O(seq_len^2) attention cost.

    Each H2 section becomes its own chunk.
    Action items are extracted as separate chunks if present.
    """
    chunks: list[dict] = []
    sections = parse_markdown(content)

    for i, section in enumerate(sections):
        chunk_content = section["content"].strip()
        if not chunk_content:
            continue

        if count_tokens(chunk_content) <= max_chunk_size:
            chunks.append({
                "content": chunk_content,
                "header": section["header"],
                "chunk_index": len(chunks),
            })
        else:
            for sub in chunk_by_tokens(chunk_content, max_chunk_size, chunk_overlap):
                chunks.append({
                    "content": sub["content"],
                    "header": section["header"],
                    "chunk_index": len(chunks),
                })

    # If no chunks created, split the whole content rather than emitting
    # one oversize chunk (which would OOM at embedding time).
    if not chunks:
        body = content.strip()
        if count_tokens(body) <= max_chunk_size:
            chunks.append({
                "content": body,
                "header": "",
                "chunk_index": 0,
            })
        else:
            for sub in chunk_by_tokens(body, max_chunk_size, chunk_overlap):
                chunks.append({
                    "content": sub["content"],
                    "header": "",
                    "chunk_index": len(chunks),
                })

    return chunks


def chunk_by_tokens(
    content: str,
    chunk_size: int = 500,
    overlap: int = 50
) -> list[dict]:
    """
    Chunk content by token count with overlap.

    Args:
        content: Text to chunk
        chunk_size: Target tokens per chunk
        overlap: Token overlap between chunks

    Returns:
        List of chunk dicts with 'content' and 'chunk_index'
    """
    if not content.strip():
        return []

    # Simple word-based chunking (approximates tokens)
    words = content.split()

    # Estimate tokens per word (roughly 1.3 tokens per word)
    words_per_chunk = int(chunk_size / 1.3)
    overlap_words = int(overlap / 1.3)

    if len(words) <= words_per_chunk:
        return [{
            "content": content.strip(),
            "chunk_index": 0
        }]

    chunks = []
    start = 0

    while start < len(words):
        end = min(start + words_per_chunk, len(words))
        chunk_words = words[start:end]
        chunk_content = " ".join(chunk_words)

        chunks.append({
            "content": chunk_content,
            "chunk_index": len(chunks)
        })

        # Move start forward, accounting for overlap
        start = end - overlap_words
        if start >= len(words) - overlap_words:
            break

    return chunks


def _strip_base64_images(text: str) -> str:
    """Strip base64-encoded image data from markdown to prevent OOM during embedding."""
    return re.sub(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', '[image]', text)


def has_meeting_id(metadata: dict) -> bool:
    """True when frontmatter/metadata carries a meeting-recorder id (Granola or Wispr)."""
    return bool(metadata.get("granola_id") or metadata.get("wispr_id"))


def chunk_document(
    content: str,
    is_meeting: bool = False,
    chunk_size: int = 500,
    chunk_overlap: int = 50
) -> list[dict]:
    """
    Main chunking dispatcher.

    Determines chunking strategy based on document type and size:
    - Meeting notes: chunk by headers
    - Long notes (>chunk_size tokens): chunk by tokens with overlap
    - Short notes: single chunk

    Args:
        content: Document content
        is_meeting: Whether this is a meeting note
        chunk_size: Target tokens per chunk (for long notes)
        chunk_overlap: Token overlap for long notes

    Returns:
        List of chunk dicts with 'content', 'chunk_index', and optionally 'header'
    """
    # Strip base64 images before chunking (they cause OOM in large embedding models)
    content = _strip_base64_images(content)

    # Extract frontmatter first
    metadata, body = extract_frontmatter(content)

    # Meeting note: granola_id or wispr_id in frontmatter, or explicit flag
    if is_meeting or has_meeting_id(metadata):
        chunks = chunk_by_headers(body)
    else:
        # Check document length
        token_count = count_tokens(body)

        if token_count <= chunk_size:
            # Short note: single chunk
            chunks = [{
                "content": body.strip(),
                "chunk_index": 0
            }]
        else:
            # Long note: chunk by tokens
            chunks = chunk_by_tokens(body, chunk_size, chunk_overlap)

    chunks = enforce_token_limit(chunks, overlap=chunk_overlap)

    # Ensure all chunks have chunk_index
    for i, chunk in enumerate(chunks):
        chunk["chunk_index"] = i

    return chunks


def _infer_topic(file_name: str, content: str) -> str:
    """
    Infer document topic from filename or first header.

    Args:
        file_name: Name of the file
        content: Document content

    Returns:
        Inferred topic string (max 50 chars)
    """
    # Try H1 header first
    h1_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if h1_match:
        topic = h1_match.group(1).strip()
        return topic[:50] if len(topic) > 50 else topic

    # Use cleaned filename
    topic = file_name.replace(".md", "")
    # Remove common date patterns
    topic = re.sub(r'\d{4}[-_]?\d{2}[-_]?\d{2}', '', topic)
    topic = re.sub(r'^\d{8}\s*', '', topic)  # YYYYMMDD at start
    topic = re.sub(r'\s*\d{8}$', '', topic)  # YYYYMMDD at end
    # Clean up separators
    topic = re.sub(r'[_-]+', ' ', topic).strip()

    return topic if topic else "general notes"


TAG_PHRASE_MIN_CONF = 0.6


def _readable_tag(value: str) -> str:
    """Tag value as plain words: ``meeting_notes`` -> ``meeting notes``, ``work/hiring`` -> ``work hiring``."""
    return re.sub(r"[_/\s]+", " ", value).strip()


def tag_phrase_key(tags) -> tuple | None:
    """The ``(doc_type, domain, topic, project)`` tuple that shapes the tag phrase.

    ``None`` when ``tags`` is absent or its document type is below the
    confidence floor (no phrase is produced). The project is ``None`` unless
    its confidence clears the floor and it is not ``none``.
    """
    if tags is None or not tags.doc_type or (tags.doc_type_conf or 0.0) < TAG_PHRASE_MIN_CONF:
        return None
    project = tags.project
    if not project or project == "none" or (tags.project_conf or 0.0) < TAG_PHRASE_MIN_CONF:
        project = None
    return (tags.doc_type, tags.domain or None, tags.topic or None, project)


def build_tag_phrase(tags) -> str:
    """``"<doc_type> in the <domain> domain about <topic> for project <project>"``, or ``""``.

    Parts without a value are left out. Never contains people names.
    """
    key = tag_phrase_key(tags)
    if key is None:
        return ""
    doc_type, domain, topic, project = key
    phrase = _readable_tag(doc_type)
    if domain:
        phrase += f" in the {_readable_tag(domain)} domain"
    if topic:
        phrase += f" about {_readable_tag(topic)}"
    if project:
        phrase += f" for project {_readable_tag(project)}"
    return phrase


def generate_chunk_context(
    file_path: Path,
    metadata: dict,
    chunk_content: str,
    chunk_index: int,
    total_chunks: int,
    tags=None,
) -> str:
    """
    Generate contextual prefix for a chunk.

    Adds 1-2 sentences describing what document this chunk is from,
    which gives the embedding model more context to work with when
    understanding the chunk.

    Args:
        file_path: Path to the source file
        metadata: Document metadata (frontmatter, extracted info)
        chunk_content: The actual chunk content
        chunk_index: Position of this chunk (0-indexed)
        total_chunks: Total number of chunks in document
        tags: Optional TagRecord; when its document type is confident, a
            tag phrase is added so keyword and vector search can reach the
            chunk by what the note is about

    Returns:
        Context string to prepend to chunk
    """
    file_name = file_path.name
    folder = file_path.parent.name

    # Build context based on document type
    if has_meeting_id(metadata):
        # Meeting note (Granola or Wispr)
        people = metadata.get("people", [])
        date = metadata.get("modified_date", "")
        if people:
            attendees = ", ".join(people[:3])
            if len(people) > 3:
                attendees += f" and {len(people) - 3} others"
        else:
            attendees = "attendees"

        if date:
            context = f"This chunk is from {file_name}, meeting notes from {date} with {attendees}."
        else:
            context = f"This chunk is from {file_name}, meeting notes with {attendees}."

    elif folder == "People" or "/People/" in str(file_path):
        # People profile
        person_name = file_name.replace(".md", "")
        context = (
            f"This chunk is from {file_name}, a personal profile document "
            f"containing contact info, travel documents, and notes about {person_name}."
        )

    elif "Daily" in str(file_path) or re.match(r"\d{4}-\d{2}-\d{2}", file_name):
        # Daily notes
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', file_name)
        if date_match:
            context = f"This chunk is from {file_name}, a daily log containing activities and notes from {date_match.group(1)}."
        else:
            context = f"This chunk is from {file_name}, a daily log containing activities and notes."

    elif "Meetings" in str(file_path) or "Meeting" in file_name:
        # Meeting notes without a recorder id
        topic = _infer_topic(file_name, chunk_content)
        people = metadata.get("people", [])
        if people:
            attendees = ", ".join(people[:3])
            context = f"This chunk is from {file_name} in {folder}/, meeting notes about {topic} with {attendees}."
        else:
            context = f"This chunk is from {file_name} in {folder}/, meeting notes about {topic}."

    else:
        # General note - infer topic from header or filename
        topic = _infer_topic(file_name, chunk_content)
        context = f"This chunk is from {file_name} in {folder}/, containing notes about {topic}."

    tag_phrase = build_tag_phrase(tags)
    if tag_phrase:
        context += f" Classified as {tag_phrase}."

    # Add chunk position for multi-chunk docs
    if total_chunks > 1:
        context += f" (Part {chunk_index + 1} of {total_chunks})"

    return context


def add_context_to_chunks(
    chunks: list[dict],
    file_path: Path,
    metadata: dict,
    tags=None,
) -> list[dict]:
    """
    Add contextual prefix to each chunk.

    This is the main entry point for contextual chunking (P9.1).
    Call this after chunk_document() to add context.

    Args:
        chunks: List of chunk dicts from chunk_document()
        file_path: Path to source file
        metadata: Document metadata
        tags: Optional TagRecord for the file (see generate_chunk_context)

    Returns:
        Chunks with context prepended to content
    """
    total_chunks = len(chunks)

    for i, chunk in enumerate(chunks):
        context = generate_chunk_context(
            file_path=file_path,
            metadata=metadata,
            chunk_content=chunk["content"],
            chunk_index=i,
            total_chunks=total_chunks,
            tags=tags,
        )
        # Prepend context with blank line separator
        chunk["content"] = f"{context}\n\n{chunk['content']}"
        chunk["has_context"] = True

    return chunks
