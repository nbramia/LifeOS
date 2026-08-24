"""
Persistent Memories for LifeOS (P6.3).

Stores memories that persist across all conversations and surface in future queries.

Storage: Human-readable JSON file at ~/.lifeos/memories.json
- Easily reviewable and editable
- Can be pre-populated with personal context
- Auto-loaded on startup
"""
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default storage path - JSON file for human readability
DEFAULT_MEMORIES_PATH = Path.home() / ".lifeos" / "memories.json"

# Minimum cosine similarity for a memory to count as a semantic match. Because
# relevant memories are injected into the chat prompt every turn, this floor
# keeps vague/unrelated queries from pulling in low-relevance memories. Tunable.
MEMORY_SEMANTIC_FLOOR = 0.35

# How close to MEMORY_SEMANTIC_FLOOR a score has to be to be reported as a near
# miss. Reporting only — it never changes which memories are returned. A
# paraphrase of a saved memory typically lands just under the floor, and the
# difference between "nothing is saved about this" and "something nearly matched"
# is what stops an empty search being read as a lost memory.
MEMORY_SEMANTIC_NEAR_MISS_MARGIN = 0.05

# Search scores the most recently saved memories only. Bounded because semantic
# recall embeds every memory it scores; when the bound binds it is reported in
# MemorySearchStats so a miss beyond it is never presented as absence.
MEMORY_SEARCH_CORPUS_LIMIT = 1000

# Memory categories and their trigger patterns
CATEGORY_PATTERNS = {
    "goals": [
        r"\b(?:I|Amir)\s+(?:want|wants|plan|plans|hope|hopes|aim|aims|intend|intends)\s+to\b",
        r"\btop\s+priorit(?:y|ies)\b",
        r"\b(?:my\s+)?(?:goal|vision)\s+(?:is|:)",
    ],
    "people": [
        r"\b(he|she|they)\s+(prefers?|likes?|wants?)",
        r"\b[A-Z][a-z]+\s+(prefers?|likes?|wants?|needs?|is|has)",
        r"(meeting|discussion|talk|call)\s+with\s+[A-Z]",
        r"\b(CEO|CTO|manager|boss|colleague|friend|family)\b",
    ],
    "preferences": [
        r"\bI\s+(prefer|like|want|need)",
        r"\bmy\s+(preference|style|habit)",
        r"(prefer|like).*\s+(over|instead|rather)",
    ],
    "decisions": [
        r"\b(we|I)\s+(decided|chose|agreed|committed)",
        r"decision\s*(is|was|to)",
        r"(postpone|delay|launch|start|cancel)",
    ],
    "facts": [
        r"\$[\d,]+[kmb]?",  # Money amounts
        r"\d+%",  # Percentages
        r"(budget|revenue|cost|price)\s+is",
        r"(deadline|due|launch)\s+(is|on)",
    ],
    "reminders": [
        r"\b(remember|don't forget|make sure)",
        r"\bfollow.?up\b",
        r"\b(todo|to.?do)\b",
    ],
    "projects": [
        r"\b(?:I|Amir)\s+(?:am\s+)?(?:building|developing|working\s+on)\b",
        r"\b(?:my|a)\s+(?:project|product)\s+(?:is|to)\b",
    ],
}

# Words to exclude from keywords
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "under",
    "again", "further", "then", "once", "here", "there", "when", "where",
    "why", "how", "all", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "just", "and", "but", "if", "or", "because", "until",
    "while", "about", "against", "i", "me", "my", "myself", "we", "our",
    "ours", "you", "your", "yours", "he", "him", "his", "she", "her",
    "hers", "it", "its", "they", "them", "their", "this", "that", "these",
    "those", "what", "which", "who", "whom", "prefers", "likes", "wants",
}


def categorize_memory(content: str) -> str:
    """
    Auto-categorize memory content.

    Args:
        content: Memory content text

    Returns:
        Category string (people, preferences, facts, decisions, reminders, context)
    """
    # Check each category's patterns
    for category, patterns in CATEGORY_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return category

    # Default to context
    return "context"


def extract_keywords(content: str) -> list[str]:
    """
    Extract keywords from memory content.

    Args:
        content: Memory content text

    Returns:
        List of keywords
    """
    keywords = set()

    # Extract capitalized words (likely names or important terms)
    capitalized = re.findall(r'\b[A-Z][a-z]+\b', content)
    keywords.update(capitalized)

    # Extract words with numbers (e.g., Q4, 2025)
    with_numbers = re.findall(r'\b[A-Z]?\d+[A-Za-z]*\b', content)
    keywords.update(with_numbers)

    # Extract quoted phrases
    quoted = re.findall(r'"([^"]+)"', content)
    keywords.update(quoted)

    # Extract significant words (longer than 5 chars, not stopwords)
    words = re.findall(r'\b[a-zA-Z]{5,}\b', content.lower())
    significant = [w for w in words if w not in STOPWORDS]
    keywords.update(significant)

    return list(keywords)


@dataclass
class Memory:
    """A persistent memory."""
    id: str
    content: str
    category: str
    keywords: list[str]
    created_at: datetime
    updated_at: datetime
    is_active: bool = True
    source: dict | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "keywords": self.keywords,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "is_active": self.is_active,
            "source": self.source,
        }


@dataclass
class MemorySearchStats:
    """What bounded a memory search, so the caller can disclose it.

    Returned alongside results rather than logged: a bare empty list can't tell
    "nothing is saved about this" from "something nearly matched but no floor was
    cleared", and only the first justifies telling the user the memory is gone.
    """
    total_saved: int      # active memories in the store, before the corpus bound
    searched: int         # memories actually scored
    corpus_limit: int     # the bound applied to the corpus
    matched: int          # matches found, before the `limit` slice
    # Scored memories that cleared neither floor: keyword overlap under
    # min_relevance, or a semantic score inside MEMORY_SEMANTIC_NEAR_MISS_MARGIN
    # of the floor. The keyword half is a weak signal — a single common word in a
    # long query counts — so a caller may report that candidates were scored and
    # rejected, but not that a relevant memory is likely saved.
    near_misses: int
    semantic_available: bool  # False when scoring fell back to keyword-only


class MemoryStore:
    """
    Service for storing and retrieving persistent memories.

    Uses a human-readable JSON file for storage, making it easy to
    review, edit, and pre-populate with personal context.
    """

    def __init__(self, file_path: Optional[str] = None):
        """
        Initialize memory store.

        Args:
            file_path: Path to JSON file (default: ~/.lifeos/memories.json)
        """
        self.file_path = Path(file_path) if file_path else DEFAULT_MEMORIES_PATH

        # Ensure directory exists
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        # Sidecar cache of memory embeddings, kept next to the memories file.
        # Pure cache (regenerable from memories.json) — kept separate so the
        # human-readable memories file never carries raw float vectors.
        self.embeddings_path = self.file_path.with_name(
            self.file_path.stem + "_embeddings.json"
        )
        self._embedding_cache: dict = self._load_embedding_cache()

        # Load or initialize memories
        self._memories: dict[str, Memory] = {}
        self._load()

    def _load(self):
        """Load memories from JSON file."""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                    for mem_data in data.get("memories", []):
                        memory = self._dict_to_memory(mem_data)
                        self._memories[memory.id] = memory
                logger.info(f"Loaded {len(self._memories)} memories from {self.file_path}")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Error loading memories: {e}. Starting fresh.")
                self._memories = {}
        else:
            logger.info(f"No memories file found at {self.file_path}. Starting fresh.")

    def _save(self):
        """Save memories to JSON file."""
        data = {
            "description": "LifeOS Persistent Memories - Edit this file to add/modify memories",
            "last_updated": datetime.now().isoformat(),
            "memories": [
                mem.to_dict() for mem in self._memories.values()
                if mem.is_active
            ]
        }
        with open(self.file_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)

    def _dict_to_memory(self, data: dict) -> Memory:
        """Convert dictionary to Memory object."""
        created_at = data.get("created_at")
        updated_at = data.get("updated_at")

        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)

        return Memory(
            id=data.get("id", str(uuid.uuid4())),
            content=data["content"],
            category=data.get("category") or categorize_memory(data["content"]),
            keywords=data.get("keywords") or extract_keywords(data["content"]),
            created_at=created_at or datetime.now(),
            updated_at=updated_at or datetime.now(),
            is_active=data.get("is_active", True),
            source=data.get("source"),
        )

    def create_memory(self, content: str, category: str = None, source: dict | None = None) -> Memory:
        """
        Create a new memory.

        Args:
            content: Memory content
            category: Optional category (auto-detected if not provided)

        Returns:
            Created Memory object
        """
        memory = Memory(
            id=str(uuid.uuid4()),
            content=content,
            category=category or categorize_memory(content),
            keywords=extract_keywords(content),
            created_at=datetime.now(),
            updated_at=datetime.now(),
            is_active=True,
            source=source,
        )

        self._memories[memory.id] = memory
        self._save()

        logger.info(f"Created memory: {memory.id} - {memory.category}")
        return memory

    def get_memory(self, memory_id: str) -> Optional[Memory]:
        """
        Get a memory by ID.

        Args:
            memory_id: Memory ID

        Returns:
            Memory object or None if not found
        """
        memory = self._memories.get(memory_id)
        if memory and memory.is_active:
            return memory
        return None

    def list_memories(self, category: str = None, limit: int = 100) -> list[Memory]:
        """
        List all active memories.

        Args:
            category: Optional category filter
            limit: Maximum number of memories to return

        Returns:
            List of Memory objects
        """
        memories = [m for m in self._memories.values() if m.is_active]

        if category:
            memories = [m for m in memories if m.category == category]

        # Sort by created_at descending
        memories.sort(key=lambda m: m.created_at, reverse=True)

        return memories[:limit]

    def update_memory(self, memory_id: str, content: str) -> Optional[Memory]:
        """
        Update memory content.

        Args:
            memory_id: Memory ID
            content: New content

        Returns:
            Updated Memory object or None if not found
        """
        memory = self._memories.get(memory_id)
        if not memory or not memory.is_active:
            return None

        # Create updated memory
        updated = Memory(
            id=memory.id,
            content=content,
            category=categorize_memory(content),
            keywords=extract_keywords(content),
            created_at=memory.created_at,
            updated_at=datetime.now(),
            is_active=True,
            source=memory.source,
        )

        self._memories[memory_id] = updated
        self._save()

        return updated

    def delete_memory(self, memory_id: str) -> bool:
        """
        Soft-delete a memory.

        Args:
            memory_id: Memory ID

        Returns:
            True if deleted, False if not found
        """
        memory = self._memories.get(memory_id)
        if not memory:
            return False

        # Create deactivated memory
        deactivated = Memory(
            id=memory.id,
            content=memory.content,
            category=memory.category,
            keywords=memory.keywords,
            created_at=memory.created_at,
            updated_at=datetime.now(),
            is_active=False,
            source=memory.source,
        )

        self._memories[memory_id] = deactivated
        self._save()

        return True

    @staticmethod
    def _content_hash(content: str) -> str:
        """Stable hash of a memory's content, used to invalidate cached vectors."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _load_embedding_cache(self) -> dict:
        """Load the sidecar embedding cache (defensive — a missing or corrupt
        cache just starts empty and is rebuilt on the next search)."""
        empty = {"model": None, "vectors": {}}
        if not self.embeddings_path.exists():
            return empty
        try:
            with open(self.embeddings_path, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("vectors"), dict):
                return empty
            return {"model": data.get("model"), "vectors": data["vectors"]}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Error loading memory embeddings cache: {e}. Rebuilding.")
            return empty

    def _save_embedding_cache(self) -> None:
        """Persist the sidecar embedding cache atomically (compact — it is not
        meant to be read by humans, unlike memories.json). The temp-file +
        os.replace keeps a crash mid-write from leaving truncated JSON behind."""
        try:
            tmp = self.embeddings_path.with_name(self.embeddings_path.name + ".tmp")
            with open(tmp, "w") as f:
                json.dump(self._embedding_cache, f)
            os.replace(tmp, self.embeddings_path)
        except OSError as e:
            logger.warning(f"Error saving memory embeddings cache: {e}")

    def _semantic_scores(self, query: str, memories: list[Memory]) -> Optional[dict[str, float]]:
        """Cosine similarity between the query and each memory.

        Embeddings are cached per memory (keyed by content hash) in the sidecar
        file and only recomputed when a memory's content or the embedding model
        changes. Returns a {memory_id: cosine} map, or None if the embedding
        service is unavailable (the caller then falls back to keyword-only).
        """
        try:
            import numpy as np
            from api.services.embeddings import get_embedding_service

            service = get_embedding_service()
            model_name = service.model_name

            cache = self._embedding_cache
            # A model swap invalidates every cached vector at once.
            if cache.get("model") != model_name:
                cache = {"model": model_name, "vectors": {}}
            vectors = dict(cache["vectors"])

            # (Re)embed memories whose content changed or were never embedded.
            pending = [
                m for m in memories
                if vectors.get(m.id, {}).get("hash") != self._content_hash(m.content)
            ]
            if pending:
                fresh = service.embed_texts([m.content for m in pending])
                if len(fresh) != len(pending):
                    # Defensive: a well-behaved service returns one vector per
                    # input. If it doesn't, score what we got rather than letting
                    # a truncated zip silently disable semantic recall entirely.
                    logger.warning(
                        f"Embedding service returned {len(fresh)} vectors for "
                        f"{len(pending)} memories; scoring only what was returned."
                    )
                for memory, vector in zip(pending, fresh):
                    vectors[memory.id] = {"hash": self._content_hash(memory.content), "vector": vector}

            # Drop vectors for memories that no longer exist (e.g. deleted).
            active_ids = {m.id for m in memories}
            pruned = {mid: v for mid, v in vectors.items() if mid in active_ids}

            dirty = bool(pending) or len(pruned) != len(cache["vectors"])
            self._embedding_cache = {"model": model_name, "vectors": pruned}
            if dirty:
                self._save_embedding_cache()

            query_vec = np.asarray(service.embed_text(query), dtype=float)
            query_norm = np.linalg.norm(query_vec)
            if query_norm == 0:
                return {}

            scores: dict[str, float] = {}
            for memory in memories:
                entry = pruned.get(memory.id)
                if entry is None:
                    continue  # never embedded (e.g. partial service return) — skip, don't fail
                vec = np.asarray(entry["vector"], dtype=float)
                norm = np.linalg.norm(vec)
                if norm == 0:
                    continue
                scores[memory.id] = float(np.dot(query_vec, vec) / (query_norm * norm))
            return scores
        except Exception as e:
            logger.warning(f"Semantic memory recall unavailable, using keyword-only: {e}")
            return None

    def search_memories(self, query: str, limit: int = 10, min_relevance: float = 0.15) -> list[Memory]:
        """
        Search memories with hybrid recall: semantic (embedding cosine) fused
        with keyword overlap via Reciprocal Rank Fusion.

        Keyword matching alone misses paraphrases ("keep it short" vs. a "prefers
        terse replies" memory); semantic alone is weak on the alias/spelling
        memories this store is built for. Fusing both keeps each one's strength.
        Falls back to keyword-only when the embedding service is unavailable.

        Args:
            query: Search query
            limit: Maximum results to return
            min_relevance: Minimum keyword relevance (overlap / search_terms count).
                           Default 0.15 requires ~15% keyword overlap.

        Returns:
            List of matching Memory objects
        """
        memories, _stats = self.search_memories_detailed(
            query, limit=limit, min_relevance=min_relevance
        )
        return memories

    def search_memories_detailed(
        self, query: str, limit: int = 10, min_relevance: float = 0.15
    ) -> tuple[list[Memory], MemorySearchStats]:
        """search_memories, plus the bounds that shaped the result.

        Same ranking and same results as search_memories; the second element
        reports what was scored, how many matched before `limit` was applied, and
        how many were scored and rejected by a floor. Callers that render an
        empty result to a user need that to avoid reporting a relevance miss as a
        memory the system lost. See MemorySearchStats.
        """
        total_saved = sum(1 for m in self._memories.values() if m.is_active)

        def _stats(searched=0, matched=0, near_misses=0, semantic_available=True):
            return MemorySearchStats(
                total_saved=total_saved,
                searched=searched,
                corpus_limit=MEMORY_SEARCH_CORPUS_LIMIT,
                matched=matched,
                near_misses=near_misses,
                semantic_available=semantic_available,
            )

        if not query.strip():
            return [], _stats()

        memories = self.list_memories(limit=MEMORY_SEARCH_CORPUS_LIMIT)
        if not memories:
            return [], _stats()

        by_id = {m.id: m for m in memories}

        # Keyword overlap scoring (the original signal).
        query_keywords = set(extract_keywords(query))
        query_words = set(query.lower().split())
        search_terms = query_keywords | query_words
        term_count = max(len(search_terms), 1)

        keyword_scores: dict[str, int] = {}
        # Memories that overlapped the query but not by enough to clear a floor.
        # Tracked as ids so a memory caught by both floors is only counted once.
        near_miss_ids: set[str] = set()
        for memory in memories:
            memory_keywords = set(kw.lower() for kw in memory.keywords)
            content_words = set(memory.content.lower().split())
            all_terms = memory_keywords | content_words

            overlap = len(search_terms & all_terms)
            if overlap == 0:
                continue
            if overlap / term_count >= min_relevance:
                keyword_scores[memory.id] = overlap
            else:
                near_miss_ids.add(memory.id)

        keyword_ranked = [mid for mid, _ in sorted(keyword_scores.items(), key=lambda x: -x[1])]

        semantic_scores = self._semantic_scores(query, memories)

        # Embedding service unavailable — preserve the original keyword behavior.
        if semantic_scores is None:
            results = [by_id[mid] for mid in keyword_ranked[:limit]]
            near_miss_ids -= {m.id for m in results}
            return results, _stats(
                searched=len(memories),
                matched=len(keyword_ranked),
                near_misses=len(near_miss_ids),
                semantic_available=False,
            )

        semantic_ranked = [
            mid for mid, score in sorted(semantic_scores.items(), key=lambda x: -x[1])
            if score >= MEMORY_SEMANTIC_FLOOR
        ]
        near_miss_ids.update(
            mid for mid, score in semantic_scores.items()
            if MEMORY_SEMANTIC_FLOOR - MEMORY_SEMANTIC_NEAR_MISS_MARGIN <= score < MEMORY_SEMANTIC_FLOOR
        )

        if not semantic_ranked and not keyword_ranked:
            return [], _stats(searched=len(memories), near_misses=len(near_miss_ids))

        from api.services.hybrid_search import reciprocal_rank_fusion
        fused = reciprocal_rank_fusion(semantic_ranked, keyword_ranked)
        matched = [by_id[mid] for mid, _ in fused if mid in by_id]
        results = matched[:limit]
        near_miss_ids -= {m.id for m in results}
        return results, _stats(
            searched=len(memories),
            matched=len(matched),
            near_misses=len(near_miss_ids),
        )

    def get_relevant_memories(self, query: str, limit: int = 5) -> list[Memory]:
        """
        Get memories relevant to a query.

        Args:
            query: The user's query
            limit: Maximum memories to return

        Returns:
            List of relevant Memory objects
        """
        return self.search_memories(query, limit=limit)


def format_memories_for_prompt(memories: list[Memory]) -> str:
    """
    Format memories for inclusion in a prompt.

    Args:
        memories: List of Memory objects

    Returns:
        Formatted string for the prompt
    """
    if not memories:
        return ""

    lines = ["## Your Memories\n"]
    for memory in memories:
        lines.append(f"- {memory.content}")

    return "\n".join(lines)


# Singleton instance
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """Get or create MemoryStore singleton."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store
