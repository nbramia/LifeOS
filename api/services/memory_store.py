"""
Persistent Memories for LifeOS (P6.3).

Stores memories that persist across all conversations and surface in future queries.

Storage: Human-readable JSON file at ~/.lifeos/memories.json
- Easily reviewable and editable
- Can be pre-populated with personal context
- Auto-loaded on startup
"""
import json
import logging
import re
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default storage path - JSON file for human readability
DEFAULT_MEMORIES_PATH = Path.home() / ".lifeos" / "memories.json"

# Memory categories and their trigger patterns
CATEGORY_PATTERNS = {
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
    content_lower = content.lower()

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
        }


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
            is_active=data.get("is_active", True)
        )

    def create_memory(self, content: str, category: str = None) -> Memory:
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
            is_active=True
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
            is_active=True
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
            is_active=False
        )

        self._memories[memory_id] = deactivated
        self._save()

        return True

    def search_memories(self, query: str, limit: int = 10) -> list[Memory]:
        """
        Search memories by keyword matching.

        Args:
            query: Search query
            limit: Maximum results to return

        Returns:
            List of matching Memory objects
        """
        # Extract keywords from query
        query_keywords = set(extract_keywords(query))
        query_words = set(query.lower().split())
        search_terms = query_keywords | query_words

        # Get all active memories and score them
        memories = self.list_memories(limit=1000)
        scored = []

        for memory in memories:
            # Score based on keyword overlap
            memory_keywords = set(kw.lower() for kw in memory.keywords)
            content_words = set(memory.content.lower().split())
            all_terms = memory_keywords | content_words

            overlap = len(search_terms & all_terms)
            if overlap > 0:
                scored.append((memory, overlap))

        # Sort by score descending
        scored.sort(key=lambda x: -x[1])

        return [m for m, score in scored[:limit]]

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
