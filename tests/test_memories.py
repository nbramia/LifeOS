"""
Tests for Persistent Memories (P6.3).

Tests memory storage, retrieval, and categorization.
"""
import pytest
import tempfile
import os
from datetime import datetime

# All tests in this file are fast unit tests
pytestmark = pytest.mark.unit


class TestMemoryCategories:
    """Test auto-categorization of memory content."""

    def test_categorizes_people_memory(self):
        """Memory about a person should be categorized as 'people'."""
        from api.services.memory_store import categorize_memory

        content = "Kevin prefers async communication"
        category = categorize_memory(content)
        assert category == "people"

    def test_categorizes_preferences_memory(self):
        """Memory about preferences should be categorized as 'preferences'."""
        from api.services.memory_store import categorize_memory

        content = "I prefer morning meetings over afternoon ones"
        category = categorize_memory(content)
        assert category == "preferences"

    def test_categorizes_facts_memory(self):
        """Memory about facts should be categorized as 'facts'."""
        from api.services.memory_store import categorize_memory

        # Use a clearer facts pattern - money amounts
        content = "Revenue target: $500,000 for Q4"
        category = categorize_memory(content)
        assert category == "facts"

    def test_categorizes_decisions_memory(self):
        """Memory about decisions should be categorized as 'decisions'."""
        from api.services.memory_store import categorize_memory

        content = "We decided to postpone the launch until Q2"
        category = categorize_memory(content)
        assert category == "decisions"

    def test_default_category_is_context(self):
        """Unknown content should be categorized as 'context'."""
        from api.services.memory_store import categorize_memory

        # Use content that doesn't match any pattern
        content = "General project background information"
        category = categorize_memory(content)
        assert category == "context"


class TestKeywordExtraction:
    """Test keyword extraction from memory content."""

    def test_extracts_person_names(self):
        """Should extract person names as keywords."""
        from api.services.memory_store import extract_keywords

        content = "Kevin prefers async communication"
        keywords = extract_keywords(content)
        assert "Kevin" in keywords

    def test_extracts_capitalized_words(self):
        """Should extract capitalized words as potential keywords."""
        from api.services.memory_store import extract_keywords

        content = "The Q4 budget review is important"
        keywords = extract_keywords(content)
        assert "Q4" in keywords

    def test_extracts_key_terms(self):
        """Should extract important terms."""
        from api.services.memory_store import extract_keywords

        content = "Meeting with the engineering team about infrastructure"
        keywords = extract_keywords(content)
        assert "engineering" in keywords or "infrastructure" in keywords


class TestMemoryRecord:
    """Test Memory dataclass."""

    def test_creates_memory_record(self):
        """Should create memory record with all fields."""
        from api.services.memory_store import Memory

        memory = Memory(
            id="mem-123",
            content="Kevin prefers async communication",
            category="people",
            keywords=["Kevin", "communication", "async"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
            is_active=True
        )

        assert memory.id == "mem-123"
        assert memory.content == "Kevin prefers async communication"
        assert memory.category == "people"
        assert "Kevin" in memory.keywords

    def test_memory_to_dict(self):
        """Memory should convert to dict."""
        from api.services.memory_store import Memory

        memory = Memory(
            id="mem-123",
            content="Test content",
            category="facts",
            keywords=["test"],
            created_at=datetime(2026, 1, 7, 12, 0, 0),
            updated_at=datetime(2026, 1, 7, 12, 0, 0),
            is_active=True
        )

        d = memory.to_dict()
        assert d["id"] == "mem-123"
        assert d["content"] == "Test content"
        assert d["category"] == "facts"


class TestMemoryStore:
    """Test MemoryStore JSON storage."""

    @pytest.fixture
    def temp_json(self):
        """Create a temporary JSON file."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_store_initialization(self, temp_json):
        """MemoryStore should initialize and create JSON file."""
        from api.services.memory_store import MemoryStore
        import json

        store = MemoryStore(file_path=temp_json)

        # Create a memory to trigger file creation
        store.create_memory("Test memory")

        # Verify JSON file exists and is valid
        with open(temp_json, 'r') as f:
            data = json.load(f)

        assert "memories" in data
        assert "description" in data

    def test_create_memory(self, temp_json):
        """Should create memory in JSON file."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        memory = store.create_memory("Kevin prefers async communication")

        assert memory is not None
        assert memory.content == "Kevin prefers async communication"
        assert memory.category == "people"
        assert "Kevin" in memory.keywords

    def test_get_memory(self, temp_json):
        """Should retrieve memory by ID."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        created = store.create_memory("Test memory")

        retrieved = store.get_memory(created.id)
        assert retrieved is not None
        assert retrieved.content == "Test memory"

    def test_get_memory_not_found(self, temp_json):
        """Should return None for non-existent memory."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        memory = store.get_memory("nonexistent-id")

        assert memory is None

    def test_list_memories(self, temp_json):
        """Should list all active memories."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        store.create_memory("Memory 1")
        store.create_memory("Memory 2")
        store.create_memory("Memory 3")

        memories = store.list_memories()
        assert len(memories) == 3

    def test_list_memories_by_category(self, temp_json):
        """Should filter memories by category."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        store.create_memory("Kevin likes coffee")  # people
        store.create_memory("I prefer morning meetings")  # preferences
        store.create_memory("Alex is the CEO")  # people

        people_memories = store.list_memories(category="people")
        assert len(people_memories) == 2

    def test_update_memory(self, temp_json):
        """Should update memory content."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        memory = store.create_memory("Original content")

        updated = store.update_memory(memory.id, "Updated content")
        assert updated is not None
        assert updated.content == "Updated content"

    def test_delete_memory(self, temp_json):
        """Should soft-delete memory."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        memory = store.create_memory("To be deleted")

        result = store.delete_memory(memory.id)
        assert result is True

        # Should not appear in list
        memories = store.list_memories()
        assert len(memories) == 0

    def test_search_by_keyword(self, temp_json):
        """Should search memories by keyword."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        store.create_memory("Kevin prefers async")
        store.create_memory("Alex likes meetings")
        store.create_memory("Budget is $500k")

        results = store.search_memories("Kevin")
        assert len(results) >= 1
        assert any("Kevin" in m.content for m in results)

    def test_persistence_across_instances(self, temp_json):
        """Memories should persist across MemoryStore instances."""
        from api.services.memory_store import MemoryStore

        # Create memory with first instance
        store1 = MemoryStore(file_path=temp_json)
        store1.create_memory("Persistent memory")

        # Load with second instance
        store2 = MemoryStore(file_path=temp_json)
        memories = store2.list_memories()

        assert len(memories) == 1
        assert memories[0].content == "Persistent memory"


class TestMemoryRetrieval:
    """Test memory retrieval for query context."""

    @pytest.fixture
    def temp_json(self):
        """Create a temporary JSON file."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    def test_get_relevant_memories(self, temp_json):
        """Should retrieve relevant memories for a query."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        store.create_memory("Kevin prefers async communication")
        store.create_memory("Alex likes stand-up meetings")
        store.create_memory("The budget is $500k")

        relevant = store.get_relevant_memories("meeting with Kevin")
        assert len(relevant) >= 1
        # Should find Kevin's communication preference
        assert any("Kevin" in m.content for m in relevant)

    def test_limits_relevant_memories(self, temp_json):
        """Should limit number of relevant memories returned."""
        from api.services.memory_store import MemoryStore

        store = MemoryStore(file_path=temp_json)
        for i in range(10):
            store.create_memory(f"Important fact number {i}")

        relevant = store.get_relevant_memories("facts", limit=3)
        assert len(relevant) <= 3


class TestFormatMemoriesForPrompt:
    """Test formatting memories for inclusion in prompts."""

    def test_format_memories_section(self):
        """Should format memories as a section for the prompt."""
        from api.services.memory_store import format_memories_for_prompt, Memory

        memories = [
            Memory(
                id="1", content="Kevin prefers async communication",
                category="people", keywords=["Kevin"],
                created_at=datetime.now(), updated_at=datetime.now(), is_active=True
            ),
            Memory(
                id="2", content="Budget is $500k",
                category="facts", keywords=["budget"],
                created_at=datetime.now(), updated_at=datetime.now(), is_active=True
            ),
        ]

        formatted = format_memories_for_prompt(memories)
        assert "## Your Memories" in formatted
        assert "Kevin prefers async communication" in formatted
        assert "Budget is $500k" in formatted

    def test_empty_memories_returns_empty_string(self):
        """Should return empty string for no memories."""
        from api.services.memory_store import format_memories_for_prompt

        formatted = format_memories_for_prompt([])
        assert formatted == ""
