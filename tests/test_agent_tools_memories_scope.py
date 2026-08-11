"""
Tests for scope disclosure in the search_memories tool.

Regression context: same bug class as tests/test_agent_tools_scope_widening.py,
but this instance lands hardest. The user personally told the assistant to
remember something, so an empty answer reads as the system having lost it — and
memory search was the one search on the tool surface with no caller lever at
all: ten results, two relevance floors and a 1000-memory corpus bound, none of
them adjustable or disclosed, behind a bare "No matching memories found."

These tests pin the fix: the result cap is caller-supplied and normalised, every
bound that binds is disclosed, and an empty result says whether nothing is saved
or whether candidates were scored and fell below the relevance floors — because
only the first justifies telling the user the memory is gone.

All memory content here is obviously synthetic; this store holds real personal
data in production and none of it may appear in a test.
"""
from types import SimpleNamespace

import pytest

from api.services.agent_tools import (
    _MEMORY_LIMIT_DEFAULT,
    _MEMORY_LIMIT_MAX,
    _tool_search_memories,
    TOOL_DEFINITIONS,
    _TOOL_HANDLERS,
)
from api.services.memory_store import (
    MemorySearchStats,
    MemoryStore,
    MEMORY_SEARCH_CORPUS_LIMIT,
    MEMORY_SEMANTIC_FLOOR,
    MEMORY_SEMANTIC_NEAR_MISS_MARGIN,
)

pytestmark = pytest.mark.unit

# Phrases that would tell the orchestrator the backend broke. An empty result is
# a fact about the data, so none of these may appear in one.
FAULT_WORDS = ("sync issue", "permission", "failed", "error", "unavailable")


def _memory(content: str, category: str = "preferences", mid: str = None):
    """Stand-in for a Memory; the tool reads only `.category` and `.content`."""
    return SimpleNamespace(id=mid or content[:8], category=category, content=content)


# Obviously synthetic memories — no real personal values anywhere in this file.
SYNTHETIC = [
    _memory("Pat Placeholder prefers decaf after 15:00", "people", mid="m1"),
    _memory("Synthetic Project Zed ships on 2099-01-01", "facts", mid="m2"),
    _memory("Keeps replies terse in the sample workspace", "preferences", mid="m3"),
]


@pytest.fixture
def fake_store(monkeypatch):
    """Stub the memory store at its service boundary, recording every call.

    Behaves like the real store in the two ways the tool depends on: it slices
    the match pool to `limit`, and reports the pre-slice match count in the
    stats, so truncation disclosure is exercised rather than assumed.
    """
    state = SimpleNamespace(
        matches=[],
        total_saved=None,      # None -> derived from the match pool
        searched=None,         # None -> derived from the match pool
        near_misses=0,
        semantic_available=True,
        calls=[],
    )

    class FakeStore:
        def search_memories_detailed(self, query, limit=10, min_relevance=0.15):
            state.calls.append(
                {"query": query, "limit": limit, "min_relevance": min_relevance}
            )
            matched = list(state.matches)
            pool = len(matched)
            stats = MemorySearchStats(
                total_saved=pool if state.total_saved is None else state.total_saved,
                searched=pool if state.searched is None else state.searched,
                corpus_limit=MEMORY_SEARCH_CORPUS_LIMIT,
                matched=pool,
                near_misses=state.near_misses,
                semantic_available=state.semantic_available,
            )
            return matched[:limit], stats

    monkeypatch.setattr(
        "api.services.memory_store.get_memory_store", lambda *a, **k: FakeStore()
    )
    return state


def _empties(state) -> list[str]:
    """Every empty-result string the tool can produce, for blanket assertions."""
    state.matches = []
    out = []

    state.total_saved, state.searched, state.near_misses = 0, 0, 0
    out.append(_tool_search_memories({"query": "synthetic sample topic"}))

    state.total_saved, state.searched, state.near_misses = 4, 4, 0
    out.append(_tool_search_memories({"query": "synthetic sample topic"}))

    state.near_misses = 2
    out.append(_tool_search_memories({"query": "synthetic sample topic"}))

    state.total_saved, state.searched, state.near_misses = 1200, 1000, 0
    out.append(_tool_search_memories({"query": "synthetic sample topic"}))

    state.near_misses = 3
    out.append(_tool_search_memories({"query": "synthetic sample topic"}))

    state.total_saved, state.searched, state.near_misses = 4, 4, 0
    state.semantic_available = False
    out.append(_tool_search_memories({"query": "synthetic sample topic"}))
    state.semantic_available = True

    out.append(_tool_search_memories({"query": "   "}))
    return out


# ---------------------------------------------------------------------------
# The caller lever — a result cap the model can widen
# ---------------------------------------------------------------------------

class TestMemoriesLimit:
    def test_default_limit_is_ten(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        _tool_search_memories({"query": "decaf"})
        assert fake_store.calls[0]["limit"] == _MEMORY_LIMIT_DEFAULT == 10

    def test_explicit_limit_reaches_the_store(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        _tool_search_memories({"query": "decaf", "limit": 50})
        assert fake_store.calls[0]["limit"] == 50

    def test_limit_actually_scopes_the_result(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        out = _tool_search_memories({"query": "synthetic", "limit": 1})
        assert out.count("\n- ") == 0
        assert SYNTHETIC[0].content in out
        assert SYNTHETIC[1].content not in out

    def test_query_is_passed_through_stripped(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        _tool_search_memories({"query": "  decaf  "})
        assert fake_store.calls[0]["query"] == "decaf"

    def test_blank_query_never_reaches_the_store(self, fake_store):
        out = _tool_search_memories({"query": "   "})
        assert fake_store.calls == []
        assert "non-empty query" in out


class TestMemoriesLimitNormalisation:
    """The model writes these, so None/0/-5/"25"/absurd all arrive.

    A bad cap must not reach the store (it doubles as the truncation yardstick,
    so 0 or None would silently disable the disclosure).
    """

    @pytest.mark.parametrize("raw", [None, "not-a-number", [], {}])
    def test_unusable_limit_falls_back_to_the_default(self, fake_store, raw):
        fake_store.matches = list(SYNTHETIC)
        _tool_search_memories({"query": "decaf", "limit": raw})
        assert fake_store.calls[0]["limit"] == _MEMORY_LIMIT_DEFAULT

    @pytest.mark.parametrize("raw", [0, -1, -50])
    def test_non_positive_limit_is_raised_to_one(self, fake_store, raw):
        fake_store.matches = list(SYNTHETIC)
        _tool_search_memories({"query": "decaf", "limit": raw})
        assert fake_store.calls[0]["limit"] == 1

    def test_absurd_limit_is_capped(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        _tool_search_memories({"query": "decaf", "limit": 10**9})
        assert fake_store.calls[0]["limit"] == _MEMORY_LIMIT_MAX

    def test_numeric_string_is_accepted(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        _tool_search_memories({"query": "decaf", "limit": "25"})
        assert fake_store.calls[0]["limit"] == 25

    def test_zero_limit_still_searches_rather_than_faking_an_empty(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        out = _tool_search_memories({"query": "decaf", "limit": 0})
        assert SYNTHETIC[0].content in out

    def test_normalised_limit_is_the_truncation_yardstick(self, fake_store):
        """The limit sent to the store is the number truncation compares against.

        A 0 cap normalised to 1 must disclose that 1 of 3 is being shown — the
        yardstick and the cap can never drift apart.
        """
        fake_store.matches = list(SYNTHETIC)
        out = _tool_search_memories({"query": "decaf", "limit": 0})
        assert "Showing 1 of 3" in out

    def test_capped_limit_is_the_yardstick_used(self, fake_store):
        fake_store.matches = [_memory(f"Synthetic sample memory {i}") for i in range(_MEMORY_LIMIT_MAX + 5)]
        out = _tool_search_memories({"query": "sample", "limit": 10**6})
        assert f"Showing {_MEMORY_LIMIT_MAX} of {_MEMORY_LIMIT_MAX + 5}" in out


# ---------------------------------------------------------------------------
# Truncation disclosure
# ---------------------------------------------------------------------------

class TestMemoriesTruncation:
    def test_truncation_is_disclosed_when_the_cap_binds(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        out = _tool_search_memories({"query": "synthetic", "limit": 2})
        assert "Showing 2 of 3 matching memories" in out
        assert "raise" in out.lower()

    def test_no_truncation_note_below_the_cap(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        out = _tool_search_memories({"query": "synthetic", "limit": 10})
        assert "Showing" not in out

    def test_no_truncation_note_when_matches_exactly_fill_the_cap(self, fake_store):
        """Exactly `limit` matches means nothing was hidden, so no note."""
        fake_store.matches = list(SYNTHETIC)
        out = _tool_search_memories({"query": "synthetic", "limit": 3})
        assert "Showing" not in out

    def test_no_truncation_note_on_an_empty_result(self, fake_store):
        fake_store.matches = []
        fake_store.total_saved = fake_store.searched = 5
        out = _tool_search_memories({"query": "synthetic"})
        assert "Showing" not in out


# ---------------------------------------------------------------------------
# The two empty branches — a relevance miss is not a lost memory
# ---------------------------------------------------------------------------

def _nothing_saved(state, query="synthetic sample topic") -> str:
    state.matches = []
    state.total_saved = state.searched = 6
    state.near_misses = 0
    return _tool_search_memories({"query": query})


def _below_threshold(state, query="synthetic sample topic", near=2) -> str:
    state.matches = []
    state.total_saved = state.searched = 6
    state.near_misses = near
    return _tool_search_memories({"query": query})


class TestMemoriesHonestEmpty:
    def test_below_threshold_names_the_threshold(self, fake_store):
        out = _below_threshold(fake_store)
        assert "relevance threshold" in out
        assert "2 came close" in out

    def test_below_threshold_suggests_rewording(self, fake_store):
        out = _below_threshold(fake_store)
        assert "wording" in out.lower()

    def test_below_threshold_does_not_claim_nothing_is_saved(self, fake_store):
        """The whole point: candidates existed, so absence was never established."""
        out = _below_threshold(fake_store)
        assert "nothing saved" not in out.lower()
        assert "likely saved" in out.lower()

    def test_nothing_saved_says_so(self, fake_store):
        out = _nothing_saved(fake_store)
        assert "Nothing saved matches" in out
        assert "6 saved" in out
        assert "wording and meaning" in out

    def test_nothing_saved_does_not_mention_a_threshold(self, fake_store):
        out = _nothing_saved(fake_store)
        assert "threshold" not in out.lower()

    def test_the_two_empty_branches_are_not_the_same_text(self, fake_store):
        assert _nothing_saved(fake_store) != _below_threshold(fake_store)

    def test_empty_store_is_named_as_such(self, fake_store):
        fake_store.matches = []
        fake_store.total_saved = fake_store.searched = 0
        out = _tool_search_memories({"query": "synthetic sample topic"})
        assert "no memories have been saved yet" in out

    def test_empty_store_text_differs_from_the_scored_empty(self, fake_store):
        fake_store.matches = []
        fake_store.total_saved = fake_store.searched = 0
        empty_store = _tool_search_memories({"query": "synthetic sample topic"})
        assert empty_store != _nothing_saved(fake_store)

    def test_empty_result_names_the_query(self, fake_store):
        assert "'synthetic sample topic'" in _nothing_saved(fake_store)
        assert "'synthetic sample topic'" in _below_threshold(fake_store)

    @pytest.mark.parametrize("index", range(7))
    def test_no_empty_path_suggests_a_backend_fault(self, fake_store, index):
        text = _empties(fake_store)[index]
        lowered = text.lower()
        assert not any(word in lowered for word in FAULT_WORDS), text

    def test_every_empty_path_is_covered_by_the_fault_check(self, fake_store):
        assert len(_empties(fake_store)) == 7


# ---------------------------------------------------------------------------
# The corpus bound — a miss beyond it establishes nothing
# ---------------------------------------------------------------------------

class TestMemoriesCorpusBound:
    def test_bound_is_disclosed_when_it_binds(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        fake_store.total_saved, fake_store.searched = 1200, 1000
        out = _tool_search_memories({"query": "synthetic"})
        assert "1000 most recently saved memories of 1200" in out

    def test_no_bound_note_when_the_whole_corpus_was_scored(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        fake_store.total_saved = fake_store.searched = 40
        out = _tool_search_memories({"query": "synthetic"})
        assert "most recently saved" not in out

    def test_capped_empty_does_not_assert_nothing_was_saved(self, fake_store):
        """A match may sit outside the bound, so absence was never established."""
        fake_store.matches = []
        fake_store.total_saved, fake_store.searched = 1200, 1000
        out = _tool_search_memories({"query": "synthetic sample topic"})
        assert "nothing saved" not in out.lower()
        assert "not scored" in out or "not checked" in out

    def test_capped_empty_differs_from_the_full_corpus_empty(self, fake_store):
        fake_store.matches = []
        fake_store.total_saved, fake_store.searched = 1200, 1000
        capped = _tool_search_memories({"query": "synthetic sample topic"})
        assert capped != _nothing_saved(fake_store)

    def test_capped_empty_still_discloses_near_misses_first(self, fake_store):
        fake_store.matches = []
        fake_store.total_saved, fake_store.searched = 1200, 1000
        fake_store.near_misses = 3
        out = _tool_search_memories({"query": "synthetic sample topic"})
        assert "relevance threshold" in out
        assert "1000 most recently saved memories of 1200" in out


# ---------------------------------------------------------------------------
# Keyword-only fallback — disclosed without blaming the backend
# ---------------------------------------------------------------------------

class TestMemoriesSemanticFallback:
    def test_fallback_is_disclosed_on_an_empty_result(self, fake_store):
        fake_store.matches = []
        fake_store.total_saved = fake_store.searched = 6
        fake_store.semantic_available = False
        out = _tool_search_memories({"query": "synthetic sample topic"})
        assert "only word overlap was scored" in out

    def test_fallback_is_disclosed_alongside_hits(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        fake_store.semantic_available = False
        out = _tool_search_memories({"query": "synthetic"})
        assert SYNTHETIC[0].content in out
        assert "only word overlap was scored" in out

    def test_fallback_empty_does_not_claim_meaning_was_scored(self, fake_store):
        """Only a wording miss was established, so that is all it may assert."""
        fake_store.matches = []
        fake_store.total_saved = fake_store.searched = 6
        fake_store.semantic_available = False
        out = _tool_search_memories({"query": "synthetic sample topic"})
        sentence = out.split("\n\n[")[0]  # the finding itself, without the notes
        assert "meaning" not in sentence.lower()
        assert "wording" in sentence.lower()

    def test_no_fallback_note_when_semantic_recall_ran(self, fake_store):
        fake_store.matches = list(SYNTHETIC)
        out = _tool_search_memories({"query": "synthetic"})
        assert "word overlap" not in out


# ---------------------------------------------------------------------------
# Tool schema — the model can only use a lever that is advertised
# ---------------------------------------------------------------------------

class TestMemoriesToolDefinition:
    @staticmethod
    def _schema() -> dict:
        return next(
            t for t in TOOL_DEFINITIONS if t["name"] == "search_memories"
        )["input_schema"]

    def test_tool_is_registered(self):
        assert "search_memories" in _TOOL_HANDLERS
        assert any(t["name"] == "search_memories" for t in TOOL_DEFINITIONS)

    def test_limit_is_advertised(self):
        props = self._schema()["properties"]
        assert "limit" in props
        assert props["limit"]["type"] == "integer"

    def test_limit_documents_the_default_and_widening(self):
        desc = self._schema()["properties"]["limit"]["description"]
        assert str(_MEMORY_LIMIT_DEFAULT) in desc
        assert "capped" in desc.lower()

    def test_limit_is_optional(self):
        assert self._schema()["required"] == ["query"]

    def test_description_warns_that_an_empty_may_be_a_wording_miss(self):
        desc = next(
            t for t in TOOL_DEFINITIONS if t["name"] == "search_memories"
        )["description"].lower()
        assert "relevance threshold" in desc
        assert "wording" in desc


# ---------------------------------------------------------------------------
# Store boundary — the stats the disclosure is built from must be real
# ---------------------------------------------------------------------------

class _FakeEmbeddingService:
    """Deterministic embeddings so the semantic floor can be placed exactly."""

    def __init__(self, vectors, default, model_name="fake-embed-model"):
        self._vectors = vectors
        self._default = default
        self.model_name = model_name

    def _lookup(self, text):
        return list(self._vectors.get(text, self._default))

    def embed_text(self, text):
        return self._lookup(text)

    def embed_texts(self, texts):
        return [self._lookup(t) for t in texts]


@pytest.fixture
def store(tmp_path):
    return MemoryStore(file_path=str(tmp_path / "synthetic_memories.json"))


@pytest.fixture
def keyword_only(monkeypatch):
    """Force the keyword-only path — no model needed, and no GPU touched."""
    def _unavailable(*args, **kwargs):
        raise RuntimeError("embedding service disabled for keyword-only tests")
    monkeypatch.setattr("api.services.embeddings.get_embedding_service", _unavailable)


class TestSearchMemoriesDetailedStats:
    def test_matches_the_plain_search(self, store, keyword_only):
        """search_memories must stay a thin wrapper — same results, same order."""
        store.create_memory("Pat Placeholder prefers decaf after 15:00")
        store.create_memory("Synthetic Project Zed ships on 2099-01-01")
        plain = store.search_memories("Placeholder decaf")
        detailed, _stats = store.search_memories_detailed("Placeholder decaf")
        assert [m.id for m in plain] == [m.id for m in detailed]

    def test_matched_counts_before_the_limit(self, store, keyword_only):
        for i in range(5):
            store.create_memory(f"Synthetic placeholder sample memory number {i}")
        results, stats = store.search_memories_detailed("placeholder sample", limit=2)
        assert len(results) == 2
        assert stats.matched == 5

    def test_keyword_floor_exclusions_are_counted_as_near_misses(self, store, keyword_only):
        """The exact site the disclosure depends on: a memory that overlapped the
        query but not by enough is a near miss, not an absence.

        The floor is a ratio of shared terms to query terms, so the same single
        shared word clears it in a one-word query and misses it in a wordy one.
        Both are asserted against an explicit min_relevance so the boundary is
        determinate; the default is pinned in the test below.
        """
        store.create_memory("Pat Placeholder prefers decaf after 15:00")

        results, stats = store.search_memories_detailed("placeholder", min_relevance=0.9)
        assert [m.content for m in results] == ["Pat Placeholder prefers decaf after 15:00"]
        assert stats.near_misses == 0  # ratio 1.0 — a hit, not a near miss

        results, stats = store.search_memories_detailed(
            "placeholder wandering unrelated synthetic verbiage padding", min_relevance=0.9
        )
        assert results == []
        assert stats.near_misses == 1

    def test_near_miss_is_reachable_at_the_default_floor(self, store, keyword_only):
        """Not just at a contrived floor: one shared word in an eight-term query
        misses the shipped 0.15 default (1/8 = 0.125) and is disclosed as a near
        miss rather than an absence."""
        store.create_memory("Pat Placeholder prefers decaf after 15:00")
        results, stats = store.search_memories_detailed(
            "placeholder wandering unrelated synthetic verbiage padding thicker phrasing"
        )
        assert results == []
        assert stats.near_misses == 1

    def test_a_returned_memory_is_never_also_a_near_miss(self, store, keyword_only):
        store.create_memory("Pat Placeholder prefers decaf after 15:00")
        results, stats = store.search_memories_detailed("Placeholder decaf")
        assert len(results) == 1
        assert stats.near_misses == 0

    def test_zero_overlap_is_not_a_near_miss(self, store, keyword_only):
        store.create_memory("Pat Placeholder prefers decaf after 15:00")
        results, stats = store.search_memories_detailed("wxyzzy qwertyx")
        assert results == []
        assert stats.near_misses == 0
        assert stats.total_saved == 1

    def test_sub_floor_semantic_score_is_a_near_miss(self, store, monkeypatch):
        """A paraphrase that lands just under the cosine floor is the motivating
        case: no shared words, nothing returned, yet something nearly matched."""
        import math
        store.create_memory("Keeps replies terse in the sample workspace")
        near = MEMORY_SEMANTIC_FLOOR - MEMORY_SEMANTIC_NEAR_MISS_MARGIN / 2
        fake = _FakeEmbeddingService(
            vectors={
                "Keeps replies terse in the sample workspace": [near, math.sqrt(1 - near ** 2), 0.0],
                "wxyzzy qwertyx": [1.0, 0.0, 0.0],
            },
            default=[0.0, 0.0, 1.0],
        )
        monkeypatch.setattr(
            "api.services.embeddings.get_embedding_service", lambda *a, **k: fake
        )
        results, stats = store.search_memories_detailed("wxyzzy qwertyx")
        assert results == []
        assert stats.near_misses == 1

    def test_far_below_the_floor_is_not_a_near_miss(self, store, monkeypatch):
        import math
        store.create_memory("Keeps replies terse in the sample workspace")
        far = MEMORY_SEMANTIC_FLOOR - MEMORY_SEMANTIC_NEAR_MISS_MARGIN - 0.1
        fake = _FakeEmbeddingService(
            vectors={
                "Keeps replies terse in the sample workspace": [far, math.sqrt(1 - far ** 2), 0.0],
                "wxyzzy qwertyx": [1.0, 0.0, 0.0],
            },
            default=[0.0, 0.0, 1.0],
        )
        monkeypatch.setattr(
            "api.services.embeddings.get_embedding_service", lambda *a, **k: fake
        )
        results, stats = store.search_memories_detailed("wxyzzy qwertyx")
        assert results == []
        assert stats.near_misses == 0

    def test_semantic_outage_is_reported_not_hidden(self, store, keyword_only):
        store.create_memory("Pat Placeholder prefers decaf after 15:00")
        _results, stats = store.search_memories_detailed("Placeholder decaf")
        assert stats.semantic_available is False

    def test_semantic_availability_is_reported_when_scoring_ran(self, store, monkeypatch):
        store.create_memory("Pat Placeholder prefers decaf after 15:00")
        fake = _FakeEmbeddingService(vectors={}, default=[0.0, 0.0, 1.0])
        monkeypatch.setattr(
            "api.services.embeddings.get_embedding_service", lambda *a, **k: fake
        )
        _results, stats = store.search_memories_detailed("Placeholder decaf")
        assert stats.semantic_available is True

    def test_corpus_bound_is_reported_when_it_binds(self, store, keyword_only, monkeypatch):
        """total_saved counts everything active; searched counts what was scored."""
        monkeypatch.setattr("api.services.memory_store.MEMORY_SEARCH_CORPUS_LIMIT", 2)
        for i in range(4):
            store.create_memory(f"Synthetic placeholder sample memory number {i}")
        _results, stats = store.search_memories_detailed("placeholder sample")
        assert stats.total_saved == 4
        assert stats.searched == 2
        assert stats.corpus_limit == 2

    def test_soft_deleted_memories_are_not_counted_as_saved(self, store, keyword_only):
        keep = store.create_memory("Pat Placeholder prefers decaf after 15:00")
        gone = store.create_memory("Synthetic Project Zed ships on 2099-01-01")
        store.delete_memory(gone.id)
        _results, stats = store.search_memories_detailed("Placeholder decaf")
        assert stats.total_saved == 1
        assert stats.searched == 1
        assert keep.id in store._memories

    def test_blank_query_reports_nothing_searched(self, store, keyword_only):
        store.create_memory("Pat Placeholder prefers decaf after 15:00")
        results, stats = store.search_memories_detailed("   ")
        assert results == []
        assert stats.searched == 0
        assert stats.total_saved == 1
