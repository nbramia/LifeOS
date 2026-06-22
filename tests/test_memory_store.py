"""
Tests for api/services/memory_store.py

Tests persistent memory storage, categorization, and retrieval.
"""
import json
import os
import tempfile
import pytest
from datetime import datetime
from pathlib import Path

from api.services.memory_store import (
    Memory,
    MemoryStore,
    categorize_memory,
    extract_keywords,
    format_memories_for_prompt,
    get_memory_store,
    MEMORY_SEMANTIC_FLOOR,
)


class _FakeEmbeddingService:
    """Deterministic stand-in for the embedding service used in hybrid tests.

    Returns a fixed vector per exact text (falling back to ``default`` for
    unknown text) and records every text it is asked to embed, so tests can
    assert what was (re)embedded.
    """

    def __init__(self, vectors, default, model_name="fake-embed-model"):
        self._vectors = vectors          # text -> vector
        self._default = default          # vector for unknown text
        self.model_name = model_name
        self.embedded_texts = []         # every text passed to embed_text/embed_texts

    def _lookup(self, text):
        return list(self._vectors.get(text, self._default))

    def embed_text(self, text):
        self.embedded_texts.append(text)
        return self._lookup(text)

    def embed_texts(self, texts):
        self.embedded_texts.extend(texts)
        return [self._lookup(t) for t in texts]


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_json_file():
    """Create a temporary JSON file for testing."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    yield path
    # Clean up both the memories file and its sidecar embedding cache.
    sidecar = Path(path).with_name(Path(path).stem + "_embeddings.json")
    for p in (Path(path), sidecar):
        if p.exists():
            p.unlink()


@pytest.fixture
def store(temp_json_file):
    """Create a MemoryStore with a temporary file."""
    return MemoryStore(file_path=temp_json_file)


@pytest.fixture
def sample_memory():
    """Create a sample Memory object."""
    now = datetime.now()
    return Memory(
        id="test-id-123",
        content="John prefers async communication over meetings",
        category="people",
        keywords=["John", "async", "communication", "meetings"],
        created_at=now,
        updated_at=now,
        is_active=True
    )


@pytest.fixture
def populated_store(store):
    """Create a store with multiple memories."""
    store.create_memory("John prefers async communication")
    store.create_memory("Sarah likes detailed documentation")
    store.create_memory("Budget for Q4 is $50,000")
    store.create_memory("We decided to launch in March")
    store.create_memory("Remember to follow up with the client")
    return store


# =============================================================================
# categorize_memory Tests
# =============================================================================

@pytest.mark.unit
class TestCategorizeMemory:
    """Tests for categorize_memory function."""

    def test_categorize_people_prefers(self):
        """Test categorization of people-related content with 'prefers'."""
        result = categorize_memory("John prefers morning meetings")
        assert result == "people"

    def test_categorize_people_with_name(self):
        """Test categorization with capitalized name."""
        result = categorize_memory("Sarah needs the report by Friday")
        assert result == "people"

    def test_categorize_people_with_title(self):
        """Test categorization with job title."""
        result = categorize_memory("The CEO mentioned new initiatives")
        assert result == "people"

    def test_categorize_preferences(self):
        """Test categorization of preferences."""
        result = categorize_memory("I prefer Python over JavaScript")
        assert result == "preferences"

    def test_categorize_preferences_style(self):
        """Test categorization of style preferences."""
        # Note: "My preference" doesn't match patterns, use exact pattern
        result = categorize_memory("I prefer detailed specs")
        assert result == "preferences"

    def test_categorize_decisions(self):
        """Test categorization of decisions."""
        result = categorize_memory("We decided to postpone the launch")
        assert result == "decisions"

    def test_categorize_decisions_commitment(self):
        """Test categorization of commitments."""
        result = categorize_memory("I committed to deliver by Monday")
        assert result == "decisions"

    def test_categorize_facts_money(self):
        """Test categorization of facts with money amounts."""
        # Pattern: r"\$[\d,]+[kmb]?" - matches dollar amounts
        result = categorize_memory("spent $100,000 on the project")
        assert result == "facts"

    def test_categorize_facts_percentage(self):
        """Test categorization of facts with percentages."""
        # Pattern: r"\d+%" - matches percentages
        result = categorize_memory("growth was 25% this quarter")
        assert result == "facts"

    def test_categorize_facts_deadline(self):
        """Test categorization of deadline facts."""
        # Pattern: r"(deadline|due|launch)\s+(is|on)"
        # Note: Must avoid triggering earlier "people" patterns
        # The people pattern with re.IGNORECASE matches "X is" patterns
        # So use "due on" instead which doesn't trigger people patterns
        result = categorize_memory("project due on Friday")
        assert result == "facts"

    def test_categorize_reminders(self):
        """Test categorization of reminders."""
        result = categorize_memory("Remember to send the invoice")
        assert result == "reminders"

    def test_categorize_reminders_followup(self):
        """Test categorization of follow-up reminders."""
        result = categorize_memory("Follow up with the vendor")
        assert result == "reminders"

    def test_categorize_default_context(self):
        """Test that unmatched content defaults to 'context'."""
        result = categorize_memory("The weather was nice today")
        assert result == "context"


# =============================================================================
# extract_keywords Tests
# =============================================================================

@pytest.mark.unit
class TestExtractKeywords:
    """Tests for extract_keywords function."""

    def test_extract_capitalized_words(self):
        """Test extraction of capitalized words (names)."""
        keywords = extract_keywords("John met with Sarah yesterday")
        assert "John" in keywords
        assert "Sarah" in keywords

    def test_extract_numbers_with_letters(self):
        """Test extraction of terms with numbers."""
        keywords = extract_keywords("Review Q4 results for 2025")
        assert "Q4" in keywords
        assert "2025" in keywords

    def test_extract_quoted_phrases(self):
        """Test extraction of quoted phrases."""
        keywords = extract_keywords('The project is called "Operation Phoenix"')
        assert "Operation Phoenix" in keywords

    def test_extract_significant_words(self):
        """Test extraction of significant long words."""
        keywords = extract_keywords("Consider implementing authentication")
        lower_keywords = [k.lower() for k in keywords]
        assert "consider" in lower_keywords
        assert "implementing" in lower_keywords
        assert "authentication" in lower_keywords

    def test_exclude_stopwords(self):
        """Test that stopwords are excluded from significant words."""
        # Note: Capitalized "The" is extracted as a potential proper noun
        # but stopwords are excluded from the significant words extraction
        keywords = extract_keywords("the quick brown fox jumps over the lazy dog")
        # Significant words (5+ chars) that aren't stopwords should be included
        lower_keywords = [k.lower() for k in keywords]
        assert "quick" in lower_keywords  # 5 chars, not a stopword
        assert "jumps" in lower_keywords  # 5 chars, not a stopword
        assert "brown" in lower_keywords  # 5 chars, not a stopword

    def test_empty_content(self):
        """Test extraction from empty content."""
        keywords = extract_keywords("")
        assert keywords == []

    def test_short_words_excluded(self):
        """Test that very short words are excluded."""
        keywords = extract_keywords("is as to be or if")
        assert len(keywords) == 0


# =============================================================================
# Memory Dataclass Tests
# =============================================================================

@pytest.mark.unit
class TestMemoryDataclass:
    """Tests for Memory dataclass."""

    def test_memory_creation(self, sample_memory):
        """Test creating a Memory instance."""
        assert sample_memory.id == "test-id-123"
        assert sample_memory.content == "John prefers async communication over meetings"
        assert sample_memory.category == "people"
        assert "John" in sample_memory.keywords
        assert sample_memory.is_active is True

    def test_memory_to_dict(self, sample_memory):
        """Test converting Memory to dictionary."""
        result = sample_memory.to_dict()

        assert result["id"] == "test-id-123"
        assert result["content"] == "John prefers async communication over meetings"
        assert result["category"] == "people"
        assert result["keywords"] == ["John", "async", "communication", "meetings"]
        assert result["is_active"] is True
        assert "created_at" in result
        assert "updated_at" in result

    def test_memory_default_is_active(self):
        """Test that is_active defaults to True."""
        now = datetime.now()
        memory = Memory(
            id="test",
            content="Test content",
            category="context",
            keywords=[],
            created_at=now,
            updated_at=now
        )
        assert memory.is_active is True


# =============================================================================
# MemoryStore Initialization Tests
# =============================================================================

@pytest.mark.unit
class TestMemoryStoreInit:
    """Tests for MemoryStore initialization."""

    def test_init_creates_empty_store(self, temp_json_file):
        """Test initialization with no existing file."""
        store = MemoryStore(file_path=temp_json_file)
        assert len(store.list_memories()) == 0

    def test_init_loads_existing_file(self, temp_json_file):
        """Test initialization loads existing memories."""
        # Pre-populate the file
        data = {
            "memories": [
                {
                    "id": "existing-1",
                    "content": "Existing memory",
                    "category": "context",
                    "keywords": ["existing"],
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                    "is_active": True
                }
            ]
        }
        with open(temp_json_file, 'w') as f:
            json.dump(data, f)

        store = MemoryStore(file_path=temp_json_file)
        memories = store.list_memories()

        assert len(memories) == 1
        assert memories[0].id == "existing-1"
        assert memories[0].content == "Existing memory"

    def test_init_handles_corrupt_file(self, temp_json_file):
        """Test initialization handles corrupt JSON gracefully."""
        with open(temp_json_file, 'w') as f:
            f.write("not valid json {{{")

        store = MemoryStore(file_path=temp_json_file)
        assert len(store.list_memories()) == 0

    def test_init_creates_directory(self):
        """Test initialization creates parent directory if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = Path(tmpdir) / "subdir" / "memories.json"
            MemoryStore(file_path=str(nested_path))

            assert nested_path.parent.exists()


# =============================================================================
# MemoryStore CRUD Tests
# =============================================================================

@pytest.mark.unit
class TestMemoryStoreCreate:
    """Tests for create_memory method."""

    def test_create_memory(self, store):
        """Test creating a memory."""
        memory = store.create_memory("John prefers async communication")

        assert memory.id is not None
        assert len(memory.id) == 36  # UUID length
        assert memory.content == "John prefers async communication"
        assert memory.is_active is True

    def test_create_memory_auto_categorizes(self, store):
        """Test that memory is auto-categorized."""
        memory = store.create_memory("Budget for Q4 is $50,000")
        assert memory.category == "facts"

    def test_create_memory_extracts_keywords(self, store):
        """Test that keywords are extracted."""
        memory = store.create_memory("Sarah prefers detailed documentation")
        assert "Sarah" in memory.keywords

    def test_create_memory_with_explicit_category(self, store):
        """Test creating memory with explicit category."""
        memory = store.create_memory("Some content", category="custom")
        assert memory.category == "custom"

    def test_create_memory_persists(self, store, temp_json_file):
        """Test that created memory is saved to file."""
        store.create_memory("Persistent memory")

        # Read the file directly
        with open(temp_json_file, 'r') as f:
            data = json.load(f)

        assert len(data["memories"]) == 1
        assert data["memories"][0]["content"] == "Persistent memory"


@pytest.mark.unit
class TestMemoryStoreGet:
    """Tests for get_memory method."""

    def test_get_existing_memory(self, store):
        """Test getting an existing memory."""
        created = store.create_memory("Test content")
        retrieved = store.get_memory(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.content == created.content

    def test_get_nonexistent_memory(self, store):
        """Test getting a non-existent memory."""
        result = store.get_memory("nonexistent-id")
        assert result is None

    def test_get_deleted_memory_returns_none(self, store):
        """Test that getting a deleted memory returns None."""
        memory = store.create_memory("To be deleted")
        store.delete_memory(memory.id)

        result = store.get_memory(memory.id)
        assert result is None


@pytest.mark.unit
class TestMemoryStoreList:
    """Tests for list_memories method."""

    def test_list_empty(self, store):
        """Test listing with no memories."""
        result = store.list_memories()
        assert result == []

    def test_list_all_memories(self, populated_store):
        """Test listing all memories."""
        result = populated_store.list_memories()
        assert len(result) == 5

    def test_list_by_category(self, populated_store):
        """Test filtering by category."""
        people_memories = populated_store.list_memories(category="people")

        for memory in people_memories:
            assert memory.category == "people"

    def test_list_with_limit(self, populated_store):
        """Test limiting results."""
        result = populated_store.list_memories(limit=2)
        assert len(result) == 2

    def test_list_excludes_deleted(self, store):
        """Test that deleted memories are excluded."""
        memory = store.create_memory("Will be deleted")
        store.delete_memory(memory.id)

        result = store.list_memories()
        ids = [m.id for m in result]
        assert memory.id not in ids

    def test_list_ordered_by_created_at_desc(self, store):
        """Test that memories are ordered by created_at descending."""
        # Create with slight delays
        mem1 = store.create_memory("First")
        mem2 = store.create_memory("Second")
        mem3 = store.create_memory("Third")

        result = store.list_memories()

        # Most recent first
        assert result[0].id == mem3.id
        assert result[1].id == mem2.id
        assert result[2].id == mem1.id


@pytest.mark.unit
class TestMemoryStoreUpdate:
    """Tests for update_memory method."""

    def test_update_memory(self, store):
        """Test updating memory content."""
        memory = store.create_memory("Original content")
        updated = store.update_memory(memory.id, "Updated content")

        assert updated is not None
        assert updated.content == "Updated content"
        assert updated.id == memory.id

    def test_update_recategorizes(self, store):
        """Test that update recategorizes the memory."""
        memory = store.create_memory("plain content", category="context")
        # Use content that matches facts pattern: r"\$[\d,]+[kmb]?"
        updated = store.update_memory(memory.id, "spent $100,000 on supplies")

        assert updated.category == "facts"

    def test_update_reextracts_keywords(self, store):
        """Test that update reextracts keywords."""
        memory = store.create_memory("John said hello")
        updated = store.update_memory(memory.id, "Sarah mentioned the project")

        assert "Sarah" in updated.keywords
        assert "John" not in updated.keywords

    def test_update_preserves_created_at(self, store):
        """Test that update preserves created_at timestamp."""
        memory = store.create_memory("Original")
        original_created = memory.created_at

        import time
        time.sleep(0.01)

        updated = store.update_memory(memory.id, "Updated")

        assert updated.created_at == original_created
        assert updated.updated_at > original_created

    def test_update_nonexistent_returns_none(self, store):
        """Test updating non-existent memory returns None."""
        result = store.update_memory("nonexistent-id", "New content")
        assert result is None

    def test_update_deleted_returns_none(self, store):
        """Test updating deleted memory returns None."""
        memory = store.create_memory("To delete")
        store.delete_memory(memory.id)

        result = store.update_memory(memory.id, "New content")
        assert result is None


@pytest.mark.unit
class TestMemoryStoreDelete:
    """Tests for delete_memory method."""

    def test_delete_memory(self, store):
        """Test deleting a memory."""
        memory = store.create_memory("To delete")
        result = store.delete_memory(memory.id)

        assert result is True
        assert store.get_memory(memory.id) is None

    def test_delete_nonexistent_returns_false(self, store):
        """Test deleting non-existent memory returns False."""
        result = store.delete_memory("nonexistent-id")
        assert result is False

    def test_delete_is_soft_delete(self, store, temp_json_file):
        """Test that delete is a soft delete (sets is_active=False)."""
        memory = store.create_memory("Soft delete test")
        store.delete_memory(memory.id)

        # The memory should still be in internal storage
        assert memory.id in store._memories
        assert store._memories[memory.id].is_active is False


# =============================================================================
# MemoryStore Search Tests
# =============================================================================

@pytest.mark.unit
class TestMemoryStoreSearch:
    """Tests for the keyword-only behavior of search_memories.

    These predate hybrid recall and assert the keyword path in isolation, so
    the embedding service is forced unavailable (search then falls back to
    keyword-only) — keeping them fast and independent of any installed model.
    """

    @pytest.fixture(autouse=True)
    def _force_keyword_only(self, monkeypatch):
        def _unavailable(*args, **kwargs):
            raise RuntimeError("embedding service disabled for keyword-only tests")
        monkeypatch.setattr(
            "api.services.embeddings.get_embedding_service", _unavailable
        )

    def test_search_by_keyword(self, populated_store):
        """Test searching by keyword."""
        results = populated_store.search_memories("John")

        contents = [m.content for m in results]
        assert any("John" in c for c in contents)

    def test_search_returns_relevant_memories(self, populated_store):
        """Test that search returns relevant memories."""
        results = populated_store.search_memories("budget Q4")

        assert len(results) > 0
        assert any("Budget" in m.content for m in results)

    def test_search_with_limit(self, populated_store):
        """Test search respects limit."""
        results = populated_store.search_memories("the", limit=2)
        assert len(results) <= 2

    def test_search_no_matches(self, populated_store):
        """Test search with no matches."""
        results = populated_store.search_memories("xyznonexistent123")
        assert len(results) == 0

    def test_search_empty_query(self, populated_store):
        """Test search with empty query."""
        results = populated_store.search_memories("")
        assert len(results) == 0

    def test_get_relevant_memories(self, populated_store):
        """Test get_relevant_memories method."""
        results = populated_store.get_relevant_memories("communication preferences")

        assert len(results) <= 5
        assert len(results) > 0


@pytest.mark.unit
class TestMemoryStoreHybridSearch:
    """Tests for hybrid (semantic + keyword) recall in search_memories.

    A fake embedding service supplies deterministic vectors so the semantic
    path, RRF fusion, and precision floor can be asserted without a real model.
    """

    def _use(self, monkeypatch, fake):
        monkeypatch.setattr(
            "api.services.embeddings.get_embedding_service", lambda *a, **k: fake
        )

    def test_semantic_match_without_keyword_overlap(self, store, monkeypatch):
        """A paraphrase with zero shared tokens is recalled via embeddings."""
        memory = store.create_memory("I prefer terse replies")
        fake = _FakeEmbeddingService(
            vectors={
                "I prefer terse replies": [1.0, 0.0, 0.0],
                "keep it short": [0.96, 0.05, 0.0],
            },
            default=[0.0, 0.0, 1.0],
        )
        self._use(monkeypatch, fake)

        # No keyword overlap between query and memory — only semantics can match.
        results = store.search_memories("keep it short")

        assert [m.id for m in results] == [memory.id]

    def test_alias_match_via_keyword_no_regression(self, store, monkeypatch):
        """Alias/spelling memories still match on keyword even when the query is
        semantically distant (cosine below the floor)."""
        memory = store.create_memory("Sarah may also be spelled Sara")
        fake = _FakeEmbeddingService(
            vectors={
                "Sarah may also be spelled Sara": [1.0, 0.0, 0.0],
                "Sara": [0.0, 1.0, 0.0],  # orthogonal -> cosine 0, below floor
            },
            default=[0.0, 0.0, 1.0],
        )
        self._use(monkeypatch, fake)

        results = store.search_memories("Sara")

        assert memory.id in [m.id for m in results]

    def test_rrf_ranks_dual_matches_first(self, store, monkeypatch):
        """A memory matched by BOTH signals outranks single-signal matches."""
        both = store.create_memory("alpha report")      # keyword 'alpha' + near query
        kw_only = store.create_memory("alpha notes")     # keyword 'alpha', far vector
        sem_only = store.create_memory("zzzzz wxyzz")    # no shared keyword, near query
        fake = _FakeEmbeddingService(
            vectors={
                "alpha report": [1.0, 0.0, 0.0],
                "alpha notes": [0.0, 1.0, 0.0],
                "zzzzz wxyzz": [1.0, 0.0, 0.0],
                "alpha signal": [1.0, 0.0, 0.0],
            },
            default=[0.0, 0.0, 1.0],
        )
        self._use(monkeypatch, fake)

        results = store.search_memories("alpha signal")
        ids = [m.id for m in results]

        assert set(ids) == {both.id, kw_only.id, sem_only.id}
        # The dual-signal match must outrank BOTH single-signal matches — a
        # naive "sort by best single score" would tie `both` with `sem_only`
        # (both cosine 1.0); only rank fusion makes `both` strictly first.
        assert ids[0] == both.id
        assert ids.index(both.id) < ids.index(kw_only.id)
        assert ids.index(both.id) < ids.index(sem_only.id)

    def test_precision_floor_blocks_unrelated(self, store, monkeypatch):
        """A vague query with no keyword overlap and sub-floor cosine returns
        nothing — memories are injected every turn, so junk must be excluded."""
        import math
        store.create_memory("banana bread recipe")
        store.create_memory("quarterly revenue figures")
        # cosine ~0.30 (just below the 0.35 floor) for the second memory.
        near = [0.30, math.sqrt(1 - 0.30 ** 2), 0.0]
        fake = _FakeEmbeddingService(
            vectors={
                "banana bread recipe": [0.0, 0.0, 1.0],
                "quarterly revenue figures": near,
                "qwerty asdf": [1.0, 0.0, 0.0],
            },
            default=[0.0, 0.0, 1.0],
        )
        self._use(monkeypatch, fake)

        results = store.search_memories("qwerty asdf")

        assert results == []
        assert MEMORY_SEMANTIC_FLOOR > 0.30  # guards the boundary this test assumes

    def test_semantic_match_just_above_floor_is_returned(self, store, monkeypatch):
        """A semantic-only match just above the floor IS recalled — locks the
        inclusion side of the boundary (paired with the 0.30-excluded test above,
        this brackets the floor to (0.30, 0.40))."""
        import math
        memory = store.create_memory("quarterly revenue figures")
        # cosine 0.40 with the query (unit vectors), comfortably above the floor.
        above = [0.40, math.sqrt(1 - 0.40 ** 2), 0.0]
        fake = _FakeEmbeddingService(
            vectors={
                "quarterly revenue figures": above,
                "zzzzz wxyzz": [1.0, 0.0, 0.0],  # query along axis 0, no keyword overlap
            },
            default=[0.0, 0.0, 1.0],
        )
        self._use(monkeypatch, fake)

        results = store.search_memories("zzzzz wxyzz")

        assert [m.id for m in results] == [memory.id]
        assert MEMORY_SEMANTIC_FLOOR < 0.40  # guards the boundary this test assumes

    def test_embedding_failure_falls_back_to_keyword(self, store, monkeypatch):
        """If the embedding service raises, search degrades to keyword-only."""
        memory = store.create_memory("John drinks black coffee")

        def _boom(*args, **kwargs):
            raise RuntimeError("model unavailable")
        monkeypatch.setattr("api.services.embeddings.get_embedding_service", _boom)

        results = store.search_memories("John")

        assert memory.id in [m.id for m in results]

    def test_memories_json_has_no_vectors_and_sidecar_used(self, store, monkeypatch):
        """Vectors live in the sidecar cache, never in the human-readable file."""
        memory = store.create_memory("I prefer terse replies")
        fake = _FakeEmbeddingService(
            vectors={
                "I prefer terse replies": [1.0, 0.0, 0.0],
                "keep it short": [0.96, 0.05, 0.0],
            },
            default=[0.0, 0.0, 1.0],
        )
        self._use(monkeypatch, fake)
        store.search_memories("keep it short")

        raw = Path(store.file_path).read_text()
        assert "vector" not in raw
        memories_data = json.loads(raw)
        for mem in memories_data["memories"]:
            assert "vector" not in mem and "embedding" not in mem

        assert store.embeddings_path.exists()
        cache = json.loads(store.embeddings_path.read_text())
        assert cache["model"] == "fake-embed-model"
        entry = cache["vectors"][memory.id]
        assert entry["vector"] == [1.0, 0.0, 0.0]
        assert len(entry["hash"]) == 64  # sha256 hex digest

    def test_cache_recomputes_only_on_content_change(self, store, monkeypatch):
        """A memory is re-embedded when its content changes, not on every search."""
        memory = store.create_memory("original wording here")
        fake = _FakeEmbeddingService(vectors={}, default=[1.0, 0.0, 0.0])
        self._use(monkeypatch, fake)

        store.search_memories("first query")
        # Warm cache: a second search must NOT re-embed the unchanged memory.
        fake.embedded_texts.clear()
        store.search_memories("second query")
        assert "original wording here" not in fake.embedded_texts

        # Editing the content invalidates the cached vector.
        store.update_memory(memory.id, "completely new wording now")
        fake.embedded_texts.clear()
        store.search_memories("third query")
        assert "completely new wording now" in fake.embedded_texts

        import hashlib
        cache = json.loads(store.embeddings_path.read_text())
        expected = hashlib.sha256("completely new wording now".encode("utf-8")).hexdigest()
        assert cache["vectors"][memory.id]["hash"] == expected

    def test_cache_invalidated_on_model_change(self, store, temp_json_file, monkeypatch):
        """Swapping the embedding model rebuilds the cache from scratch."""
        memory = store.create_memory("durable memory content")

        fake_a = _FakeEmbeddingService(vectors={}, default=[1.0, 0.0, 0.0], model_name="model-A")
        self._use(monkeypatch, fake_a)
        store.search_memories("query one")
        cache = json.loads(store.embeddings_path.read_text())
        assert cache["model"] == "model-A"

        # New store instance (same files) with a different model.
        store2 = MemoryStore(file_path=temp_json_file)
        fake_b = _FakeEmbeddingService(vectors={}, default=[0.0, 1.0, 0.0], model_name="model-B")
        self._use(monkeypatch, fake_b)
        store2.search_memories("query two")

        assert "durable memory content" in fake_b.embedded_texts
        cache = json.loads(store2.embeddings_path.read_text())
        assert cache["model"] == "model-B"
        assert cache["vectors"][memory.id]["vector"] == [0.0, 1.0, 0.0]


# =============================================================================
# format_memories_for_prompt Tests
# =============================================================================

@pytest.mark.unit
class TestFormatMemoriesForPrompt:
    """Tests for format_memories_for_prompt function."""

    def test_format_empty_list(self):
        """Test formatting empty list."""
        result = format_memories_for_prompt([])
        assert result == ""

    def test_format_single_memory(self):
        """Test formatting single memory."""
        now = datetime.now()
        memories = [Memory(
            id="1",
            content="Test memory content",
            category="context",
            keywords=[],
            created_at=now,
            updated_at=now
        )]

        result = format_memories_for_prompt(memories)

        assert "## Your Memories" in result
        assert "- Test memory content" in result

    def test_format_multiple_memories(self):
        """Test formatting multiple memories."""
        now = datetime.now()
        memories = [
            Memory(id="1", content="First memory", category="context",
                   keywords=[], created_at=now, updated_at=now),
            Memory(id="2", content="Second memory", category="context",
                   keywords=[], created_at=now, updated_at=now),
            Memory(id="3", content="Third memory", category="context",
                   keywords=[], created_at=now, updated_at=now),
        ]

        result = format_memories_for_prompt(memories)

        assert "- First memory" in result
        assert "- Second memory" in result
        assert "- Third memory" in result


# =============================================================================
# Singleton Tests
# =============================================================================

@pytest.mark.unit
class TestGetMemoryStore:
    """Tests for get_memory_store singleton function."""

    def test_get_memory_store_returns_instance(self):
        """Test that get_memory_store returns a MemoryStore."""
        # Reset singleton for test
        import api.services.memory_store as module
        module._memory_store = None

        store = get_memory_store()
        assert isinstance(store, MemoryStore)

    def test_get_memory_store_returns_same_instance(self):
        """Test that get_memory_store returns the same instance."""
        # Reset singleton for test
        import api.services.memory_store as module
        module._memory_store = None

        store1 = get_memory_store()
        store2 = get_memory_store()

        assert store1 is store2


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.unit
class TestMemoryWorkflow:
    """Integration tests for memory workflows."""

    @pytest.fixture(autouse=True)
    def _force_keyword_only(self, monkeypatch):
        def _unavailable(*args, **kwargs):
            raise RuntimeError("embedding service disabled for keyword-only tests")
        monkeypatch.setattr(
            "api.services.embeddings.get_embedding_service", _unavailable
        )

    def test_complete_memory_lifecycle(self, store):
        """Test complete memory lifecycle."""
        # Create
        memory = store.create_memory("John prefers email communication")
        assert memory.category == "people"

        # Read
        retrieved = store.get_memory(memory.id)
        assert retrieved.content == memory.content

        # Update
        updated = store.update_memory(memory.id, "John prefers Slack communication")
        assert "Slack" in updated.content

        # Search
        results = store.search_memories("Slack")
        assert any(m.id == memory.id for m in results)

        # Delete
        deleted = store.delete_memory(memory.id)
        assert deleted is True
        assert store.get_memory(memory.id) is None

    def test_persistence_across_instances(self, temp_json_file):
        """Test that memories persist across store instances."""
        # Create memory with first instance
        store1 = MemoryStore(file_path=temp_json_file)
        memory = store1.create_memory("Persisted memory")
        memory_id = memory.id

        # Create new instance and verify memory exists
        store2 = MemoryStore(file_path=temp_json_file)
        retrieved = store2.get_memory(memory_id)

        assert retrieved is not None
        assert retrieved.content == "Persisted memory"

    def test_category_filtering_workflow(self, store):
        """Test filtering memories by category."""
        # Create memories in different categories with patterns that match
        store.create_memory("John likes Python")  # people (Name + likes)
        store.create_memory("spent $10,000 on tools")  # facts (dollar amount)
        store.create_memory("I prefer vim over emacs")  # preferences (I prefer)
        store.create_memory("remember to call tomorrow")  # reminders (remember)

        # Filter by each category
        people = store.list_memories(category="people")
        facts = store.list_memories(category="facts")
        preferences = store.list_memories(category="preferences")
        reminders = store.list_memories(category="reminders")

        assert len(people) >= 1
        assert len(facts) >= 1
        assert len(preferences) >= 1
        assert len(reminders) >= 1
