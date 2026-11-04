"""How bulk content ranks in hybrid search, over a real keyword index and a stand-in vector store."""
from unittest.mock import MagicMock, patch

import pytest

from api.services.bm25_index import BM25Index
from api.services.bulk_content import bulk_path_matcher
from api.services.hybrid_search import BULK_VECTOR_FETCH, HybridSearch, blend_bulk_results, split_vector_candidates
from api.services.search_facets import SearchFacets
from api.services.vectorstore import VectorStore, default_fetch_count
from config.settings import settings

pytestmark = pytest.mark.unit

BULK_FOLDER = "Imports/Lists"


class StandInVectorStore:
    """Fetches the nearest chunks, then re-ranks them by recency like ``VectorStore.search``."""

    def __init__(self):
        self.chunks = []
        self.calls = []

    def add(self, file_path, index, text, similarity, recency=0.0):
        self.chunks.append({
            "id": f"{file_path}::{index}", "content": text, "file_path": file_path,
            "file_name": file_path.rsplit("/", 1)[-1], "semantic_score": similarity,
            "score": 0.4 * similarity + 0.6 * recency,
        })

    def search(self, query, top_k=20, fetch_count=None, **kwargs):
        self.calls.append({"query": query, "top_k": top_k, "fetch_count": fetch_count, **kwargs})
        allowed = kwargs.get("file_paths")
        pool = [c for c in self.chunks if allowed is None or c["file_path"] in allowed]
        pool.sort(key=lambda c: c["semantic_score"], reverse=True)
        fetched = pool[:fetch_count if fetch_count is not None else default_fetch_count(top_k)]
        fetched.sort(key=lambda c: c["score"], reverse=True)
        return [dict(c) for c in fetched[:top_k]]


class Corpus:
    def __init__(self, tmp_path):
        self.vault = tmp_path / "vault"
        self.vault.mkdir()
        self.bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
        self.vectors = StandInVectorStore()
        self.hybrid = HybridSearch(vector_store=self.vectors, bm25_index=self.bm25)

    def path(self, rel):
        return f"{self.vault.resolve().as_posix()}/{rel}"

    def ordinary(self, name, text, similarity=0.5, recency=0.0):
        path = self.path(f"Notes/{name}.md")
        self.vectors.add(path, 0, text, similarity, recency)
        self.bm25.add_document(f"{path}_0", text, f"{name}.md")
        return path

    def list_rows(self, name, rows, text, similarity=0.9, recency=1.0):
        # Every third chunk, so overlap removal leaves each one standing.
        path = self.path(f"{BULK_FOLDER}/{name}.md")
        for row in range(rows):
            self.vectors.add(path, row * 3, text, similarity - row * 1e-5, recency)
        self.bm25.bulk_add([
            {"doc_id": f"{path}_{row * 3}", "content": text, "file_name": f"{name}.md"} for row in range(rows)
        ])
        return path

    def search(self, query, top_k=10, **kwargs):
        kwargs.setdefault("use_reranker", False)
        return self.hybrid.search(query, top_k=top_k, **kwargs)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    built = Corpus(tmp_path)
    monkeypatch.setattr(settings, "vault_path", built.vault)
    monkeypatch.setattr(settings, "vault_no_people_paths_raw", BULK_FOLDER)
    return built


def paths_of(results):
    return [r["file_path"] for r in results]


def is_bulk_result(result):
    return f"/{BULK_FOLDER}/" in result["file_path"]


def add_ordinary_notes(corpus, count=12):
    for i in range(count):
        # Equal lengths, so the keyword order rests on the term counts alone.
        corpus.ordinary(
            f"Note {i:02}", "harvest pledge " * (count - i) + "filler words " * i,
            similarity=0.5 - i * 0.01, recency=(i % 4) * 0.25,
        )


class TestRule:
    @pytest.mark.parametrize("rel,expected", [
        (f"{BULK_FOLDER}/a.md", True),
        (f"{BULK_FOLDER}/deep/a.md_12", True),
        (f"{BULK_FOLDER.lower()}/a.md", True),
        (f"{BULK_FOLDER} 2/a.md", False),
        ("Notes/a.md", False),
    ])
    def test_a_path_under_a_configured_folder_is_bulk(self, tmp_path, monkeypatch, rel, expected):
        monkeypatch.setattr(settings, "vault_path", tmp_path)
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", f" {BULK_FOLDER}/ ")
        assert bulk_path_matcher()(f"{tmp_path.resolve().as_posix()}/{rel}") is expected

    def test_nothing_configured_means_no_rule(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", "")
        assert bulk_path_matcher() is None

    def test_arms_are_called_as_usual_with_no_rule(self, corpus, monkeypatch):
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", "")
        monkeypatch.setattr(settings, "vault_index_exclude_globs_raw", "")
        list_path = corpus.list_rows("Givers", 30, "harvest pledge row")
        spy = MagicMock(wraps=corpus.bm25.search)
        corpus.bm25.search = spy

        results = corpus.search("harvest pledge", top_k=10)

        assert corpus.vectors.calls == [{"query": "harvest pledge", "top_k": 10, "fetch_count": None}]
        spy.assert_called_once_with("harvest pledge", limit=10)
        assert paths_of(results).count(list_path) == 10


class TestOrdinaryPool:
    def test_ordinary_ranking_is_that_of_a_corpus_without_the_bulk_chunks(self, corpus):
        add_ordinary_notes(corpus)
        plain = corpus.search("harvest pledge", top_k=10)
        corpus.list_rows("Givers", 300, "harvest pledge " * 40, similarity=0.95)

        crowded = corpus.search("harvest pledge", top_k=10)

        assert paths_of(crowded) == paths_of(plain)
        assert [r["hybrid_score"] for r in crowded] == [r["hybrid_score"] for r in plain]

    def test_vector_arm_fetches_wide_and_keeps_only_as_many_ordinary_as_a_plain_search(self, corpus):
        for i in range(40):
            corpus.ordinary(f"Note {i:02}", "unrelated", similarity=0.8 - i * 0.01, recency=i * 0.02)
        corpus.list_rows("Givers", 100, "nothing here", similarity=0.95)

        results = corpus.search("other words", top_k=2)

        assert corpus.vectors.calls[0]["fetch_count"] == BULK_VECTOR_FETCH
        assert corpus.vectors.calls[0]["top_k"] == BULK_VECTOR_FETCH
        # Ten nearest ordinary chunks at top_k 2, re-ranked by score: the two most recent of them.
        assert [r["file_name"] for r in results] == ["Note 09.md", "Note 08.md"]
        assert len(split_vector_candidates(corpus.vectors.chunks, bulk_path_matcher(), 2)[0]) == 2

    def test_keyword_arm_keeps_ordinary_candidates_when_bulk_matches_more(self, corpus):
        add_ordinary_notes(corpus)
        corpus.list_rows("Givers", 600, "harvest pledge " * 40, similarity=0.1)
        corpus.vectors.chunks = []

        results = corpus.search("harvest pledge", top_k=5)

        assert len(results) == 5
        assert not any(is_bulk_result(r) for r in results)

    def test_keyword_arm_still_answers_when_bulk_takes_every_near_vector_slot(self, corpus):
        add_ordinary_notes(corpus)
        corpus.list_rows("Givers", BULK_VECTOR_FETCH + 10, "nothing here", similarity=0.99)

        results = corpus.search("harvest pledge", top_k=5)

        assert len(results) == 5
        assert not any(is_bulk_result(r) for r in results)

    def test_recent_import_does_not_lift_a_bulk_chunk_over_an_old_note(self, corpus):
        old = corpus.ordinary("Old", "harvest pledge old", similarity=0.2)
        corpus.list_rows("Givers", 40, "harvest pledge new", similarity=0.9, recency=1.0)
        assert paths_of(corpus.search("harvest pledge"))[0] == old

    def test_cross_encoder_only_sees_ordinary_chunks(self, corpus):
        add_ordinary_notes(corpus)
        corpus.list_rows("Givers", 200, "harvest pledge " * 40, similarity=0.95)
        reranker = MagicMock()
        reranker.rerank.side_effect = lambda query, results, top_k, **_: list(reversed(results))[:top_k]

        with patch("api.services.reranker.get_reranker", return_value=reranker):
            results = corpus.search("harvest pledge", top_k=5, use_reranker=True)

        seen = reranker.rerank.call_args.kwargs["results"]
        assert seen and not any(is_bulk_result(r) for r in seen)
        assert not any(is_bulk_result(r) for r in results)

    def test_a_list_cannot_lead_by_sharing_words_with_its_name(self, corpus):
        for i in range(15):
            corpus.ordinary(f"Budget Note {i}", "2026 budget review discussion", similarity=0.6)
        corpus.list_rows("2026 Budget Review Donors", 40, "alpha beta gamma", similarity=0.3)

        assert not any(is_bulk_result(r) for r in corpus.search("2026 budget review"))


class TestExactWords:
    def test_terms_only_in_bulk_find_the_row_first(self, corpus):
        for i in range(15):
            corpus.ordinary(f"Note {i}", "harvest planning notes", similarity=0.6)
        listed = corpus.list_rows("Givers", 5, "mirel example org row", similarity=0.1)
        assert paths_of(corpus.search("mirel example org"))[0] == listed

    def test_an_ordinary_note_with_only_some_terms_does_not_keep_the_row_down(self, corpus):
        corpus.ordinary("Plan", "mirel planning notes", similarity=0.9)
        listed = corpus.list_rows("Givers", 5, "mirel example org row", similarity=0.1)
        assert paths_of(corpus.search("mirel example org"))[0] == listed

    def test_all_terms_in_an_ordinary_note_keep_the_row_down(self, corpus):
        note = corpus.ordinary("Plan", "mirel example org row", similarity=0.2)
        corpus.list_rows("Givers", 40, "mirel example org row", similarity=0.9)
        results = corpus.search("mirel example org")
        assert paths_of(results)[0] == note
        assert [r["hybrid_score"] for r in results if is_bulk_result(r)] == [0.0] * 3

    def test_a_vector_only_search_blends_the_same_way(self, corpus):
        for i in range(12):
            corpus.ordinary(f"Note {i}", "completely different words", similarity=0.5)
        listed = corpus.list_rows("Givers", 40, "harvest pledges row", similarity=0.2)

        results = corpus.search("harvest pledges", top_k=10)

        assert paths_of(results)[:3] == [listed] * 3
        assert len(results) == 10


class TestBulkKeywordQuery:
    @pytest.mark.parametrize("ordinary_text,notes,queries", [
        ("mirel example org", 10, 1),
        ("mirel example org", 9, 2),
        ("mirel planning notes", 10, 2),
    ])
    def test_it_runs_unless_ordinary_results_hold_all_the_terms_and_fill_top_k(self, corpus, ordinary_text, notes, queries):
        for i in range(notes):
            corpus.ordinary(f"Note {i}", ordinary_text, similarity=0.6)
        corpus.list_rows("Givers", 5, "mirel example org row", similarity=0.1)
        spy = MagicMock(wraps=corpus.bm25.search)
        corpus.bm25.search = spy

        corpus.search("mirel example org", top_k=10)

        assert spy.call_count == queries


class TestBound:
    @pytest.mark.parametrize("top_k,cap", [(1, 1), (3, 1), (7, 2), (10, 3), (20, 6)])
    def test_the_share_holds_when_every_bulk_chunk_has_the_terms(self, corpus, top_k, cap):
        for i in range(30):
            corpus.ordinary(f"Note {i}", "unrelated words", similarity=0.6)
        corpus.list_rows("Givers", 60, "harvest pledges row", similarity=0.9)

        results = corpus.search("harvest pledges", top_k=top_k)

        assert len(results) == top_k
        assert sum(is_bulk_result(r) for r in results) == cap

    def test_one_list_cannot_fill_a_result_set_with_nothing_else_indexed(self, corpus):
        corpus.list_rows("Givers", 500, "harvest row", similarity=0.9)
        assert len(corpus.search("harvest", top_k=10)) == 3

    def test_other_bulk_chunks_fill_only_empty_slots(self):
        ordinary = [{"id": f"o{i}"} for i in range(2)]
        keyword = [{"doc_id": f"/b/k.md_{i * 3}", "file_name": "k.md", "match_mode": "or"} for i in range(5)]
        vector = [{"id": f"/b/v.md::{i * 3}", "file_path": "/b/v.md", "file_name": "v.md"} for i in range(5)]

        results = blend_bulk_results(ordinary, keyword, vector, False, 10)

        assert [r["id"] for r in results] == ["o0", "o1", "/b/k.md_0", "/b/k.md_3", "/b/k.md_6"]
        assert [r["found_by"] for r in results[2:]] == ["bm25"] * 3
        assert results[2]["file_path"] == "/b/k.md"
        assert results[2]["metadata"]["file_name"] == "k.md"
        assert blend_bulk_results([{"id": f"o{i}"} for i in range(10)], keyword, vector, False, 10)[-1]["id"] == "o9"
        assert blend_bulk_results([], [], vector, False, 10)[0]["found_by"] == "vector"

    def test_a_chunk_found_by_both_arms_counts_once(self):
        keyword = [{"doc_id": "/b/k.md_3", "file_name": "k.md", "match_mode": "or"}]
        vector = [{"id": "/b/k.md::3", "file_path": "/b/k.md", "file_name": "k.md"}]
        assert len(blend_bulk_results([], keyword, vector, False, 10)) == 1


class TestExemptions:
    def test_a_facet_narrowed_to_bulk_files_ranks_them_with_no_cap(self, corpus):
        corpus.ordinary("Plan", "harvest row", similarity=0.9)
        listed = corpus.list_rows("Givers", 30, "harvest row", similarity=0.5)
        results = corpus.search("harvest row", top_k=10, facets=SearchFacets(folder=BULK_FOLDER))
        assert paths_of(results) == [listed] * 10

    def test_a_facet_spanning_both_kinds_keeps_the_bound(self, corpus):
        path = corpus.path("Imports/note.md")
        corpus.vectors.add(path, 0, "harvest row", 0.4)
        corpus.bm25.add_document(f"{path}_0", "harvest row", "note.md")
        listed = corpus.list_rows("Givers", 30, "harvest row", similarity=0.9)

        results = corpus.search("harvest row", top_k=10, facets=SearchFacets(folder="Imports"))

        assert paths_of(results)[0] == path
        assert paths_of(results).count(listed) == 3

    def test_include_bulk_false_leaves_bulk_out_entirely(self, corpus):
        note = corpus.ordinary("Plan", "unrelated", similarity=0.1)
        corpus.list_rows("Givers", 30, "harvest pledges row", similarity=0.9)
        spy = MagicMock(wraps=corpus.bm25.search)
        corpus.bm25.search = spy

        assert paths_of(corpus.search("harvest pledges", include_bulk=False)) == [note]
        assert spy.call_count == 1

    def test_include_bulk_false_holds_for_a_facet_narrowed_to_bulk(self, corpus):
        corpus.list_rows("Givers", 5, "harvest pledges row", similarity=0.9)
        facets = SearchFacets(folder=BULK_FOLDER)
        assert corpus.search("harvest pledges", include_bulk=False, facets=facets) == []

    def test_person_briefing_search_excludes_bulk(self):
        from api.services.briefings import BriefingsService

        hybrid = MagicMock()
        hybrid.search.return_value = []
        resolver = MagicMock()
        resolver.resolve.return_value = None
        service = BriefingsService(
            hybrid_search=hybrid, task_manager=MagicMock(), entity_resolver=resolver,
            interaction_store=MagicMock(), imessage_store=MagicMock(),
        )
        with patch("api.services.briefings.resolve_person_name", return_value="Robin Gamma"):
            service.gather_context("Robin Gamma")
        assert hybrid.search.call_args.kwargs["include_bulk"] is False

    def test_meeting_prep_searches_exclude_bulk(self):
        from api.services import meeting_prep

        search = MagicMock()
        search.search.return_value = []
        meeting_prep._find_people_notes(["Robin Gamma"], search)
        meeting_prep._find_past_meetings("Weekly sync", "2026-01-01T10:00:00", search)
        meeting_prep._find_topic_notes("Weekly sync", "agenda review", ["Robin Gamma"], search)
        assert search.search.call_count == 3
        assert all(call.kwargs["include_bulk"] is False for call in search.search.call_args_list)


class TestStores:
    def test_vector_fetch_defaults_to_five_times_top_k_and_can_be_set(self):
        store = VectorStore.__new__(VectorStore)
        store._collection = MagicMock()
        store._collection.query.return_value = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        store._embedding_service = MagicMock()
        store._embedding_service.embed_text.return_value = [0.0]
        for kwargs, fetched in (({"top_k": 10}, 50), ({"top_k": 50}, 100), ({"top_k": 400, "fetch_count": 400}, 400)):
            store.search("question", **kwargs)
            assert store._collection.query.call_args.kwargs["n_results"] == fetched

    def test_keyword_doc_filter_applies_before_the_limit_on_both_passes(self, tmp_path):
        index = BM25Index(db_path=str(tmp_path / "bm25.db"))
        for i in range(30):
            index.add_document(f"/bulk/{i}.md_0", "harvest " * 20, f"{i}.md")
        index.add_document("/note/a.md_0", "harvest row", "a.md")
        index.add_document("/note/b.md_0", "harvest harvest harvest row", "b.md")
        keep = lambda doc_id: doc_id.startswith("/note/")  # noqa: E731

        for query in ("harvest", "harvest absent"):
            results = index.search(query, limit=2, doc_filter=keep)
            assert sorted(r["doc_id"] for r in results) == ["/note/a.md_0", "/note/b.md_0"]
        assert index.search("harvest row", limit=2)[0]["bm25_score"] < 0
