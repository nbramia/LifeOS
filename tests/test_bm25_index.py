"""Unit tests for the BM25 keyword index — delete behaviour matters most.

The historical regression was: ``delete_document(path)`` only clears the row
whose doc_id equals ``path`` exactly, but chunks are keyed
``{path}_{chunk_idx}`` and the summary is ``{path}::summary`` — so the
"clean before re-add" path silently leaked all chunks whenever a file's
chunk count shrank or its underlying path changed (e.g. vault migration
from macOS to Linux left two parallel sets of rows).
"""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def bm25(tmp_path):
    from api.services.bm25_index import BM25Index
    return BM25Index(db_path=str(tmp_path / "bm25_test.db"))


def _seed_file_chunks(bm25, path, chunk_count, with_summary=True):
    for i in range(chunk_count):
        bm25.add_document(
            doc_id=f"{path}_{i}",
            content=f"chunk {i} content for {path}",
            file_name=path.split("/")[-1],
        )
    if with_summary:
        bm25.add_document(
            doc_id=f"{path}::summary",
            content=f"summary of {path}",
            file_name=path.split("/")[-1],
        )


class TestDeleteByPath:
    def test_clears_all_chunks_for_a_file(self, bm25):
        _seed_file_chunks(bm25, "/notes/foo.md", chunk_count=5)
        assert bm25.count() == 6  # 5 chunks + 1 summary

        removed = bm25.delete_by_path("/notes/foo.md")
        assert removed == 6
        assert bm25.count() == 0

    def test_only_touches_the_named_path(self, bm25):
        _seed_file_chunks(bm25, "/notes/foo.md", chunk_count=3)
        _seed_file_chunks(bm25, "/notes/bar.md", chunk_count=2)

        removed = bm25.delete_by_path("/notes/foo.md")
        assert removed == 4
        # bar's 2 chunks + 1 summary survive
        assert bm25.count() == 3

    def test_path_prefix_collision_is_safe(self, bm25):
        """``/a/foo.md`` deletion must not clobber ``/a/foo.md.bak``.

        GLOB ``{path}_*`` is the underscore-suffix shape we use for numbered
        chunks; ``foo.md.bak_0`` starts with ``foo.md.b`` not ``foo.md_``, so
        the safety property here is just that we don't accidentally substring-
        match the wrong file.
        """
        _seed_file_chunks(bm25, "/a/foo.md", chunk_count=2)
        _seed_file_chunks(bm25, "/a/foo.md.bak", chunk_count=2)

        removed = bm25.delete_by_path("/a/foo.md")
        # 2 chunks + summary for /a/foo.md only
        assert removed == 3
        # /a/foo.md.bak still has 2 chunks + summary
        assert bm25.count() == 3

    def test_removes_stale_path_alongside_current_path(self, bm25):
        """The vault migration scenario: old macOS path and new Linux path coexist."""
        _seed_file_chunks(bm25, "/Users/x/Notes/q.md", chunk_count=3)
        _seed_file_chunks(bm25, "/home/x/Notes/q.md", chunk_count=3)

        # Drop the legacy macOS rows only.
        removed = bm25.delete_by_path("/Users/x/Notes/q.md")
        assert removed == 4
        # Linux rows survive.
        assert bm25.count() == 4

    def test_returns_zero_when_nothing_to_delete(self, bm25):
        assert bm25.delete_by_path("/nonexistent.md") == 0


class TestDocDates:
    """The sidecar date table backs recency ranking + date filtering for the
    keyword half of hybrid search."""

    def test_search_returns_modified_date(self, bm25):
        bm25.add_document(
            "doc1", "quarterly budget review", "Budget.md",
            modified_date="2026-06-05",
        )
        results = bm25.search("budget")
        assert len(results) == 1
        assert results[0]["modified_date"] == "2026-06-05"

    def test_search_returns_empty_string_when_undated(self, bm25):
        bm25.add_document("doc1", "quarterly budget review", "Budget.md")
        results = bm25.search("budget")
        assert results[0]["modified_date"] == ""

    def test_date_updates_on_readd(self, bm25):
        bm25.add_document("doc1", "budget", "B.md", modified_date="2026-01-01")
        bm25.add_document("doc1", "budget", "B.md", modified_date="2026-06-01")
        assert bm25.search("budget")[0]["modified_date"] == "2026-06-01"

    def test_readd_without_date_clears_stale_date(self, bm25):
        bm25.add_document("doc1", "budget", "B.md", modified_date="2026-01-01")
        bm25.add_document("doc1", "budget", "B.md")  # no date this time
        assert bm25.search("budget")[0]["modified_date"] == ""

    def test_delete_by_path_clears_dates(self, bm25):
        bm25.add_document(
            "/notes/q.md_0", "budget", "q.md", modified_date="2026-06-01"
        )
        bm25.delete_by_path("/notes/q.md")
        # Re-add undated; the old date must not linger via the sidecar table.
        bm25.add_document("/notes/q.md_0", "budget", "q.md")
        assert bm25.search("budget")[0]["modified_date"] == ""

    def test_bulk_add_persists_dates(self, bm25):
        bm25.bulk_add([
            {"doc_id": "d1", "content": "budget alpha", "file_name": "a.md",
             "modified_date": "2026-03-03"},
            {"doc_id": "d2", "content": "budget beta", "file_name": "b.md"},
        ])
        by_id = {r["doc_id"]: r for r in bm25.search("budget")}
        assert by_id["d1"]["modified_date"] == "2026-03-03"
        assert by_id["d2"]["modified_date"] == ""

    def test_clear_removes_dates(self, bm25):
        bm25.add_document("d1", "budget", "a.md", modified_date="2026-03-03")
        bm25.clear()
        bm25.add_document("d1", "budget", "a.md")
        assert bm25.search("budget")[0]["modified_date"] == ""


@pytest.fixture
def seeded(bm25):
    bm25.bulk_add([
        {"doc_id": "d1", "content": "we discussed hiring plans for the platform team", "file_name": "Hiring.md"},
        {"doc_id": "d2", "content": "the roadmap owner is the platform lead", "file_name": "Roadmap.md"},
        {"doc_id": "d3", "content": "cloud costs are rising while local inference is cheap", "file_name": "Infra.md"},
        {"doc_id": "d4", "content": "alpha and beta rollout notes", "file_name": "Rollout.md"},
        {"doc_id": "d5", "content": "project tag planning session", "file_name": "Tags.md"},
        {"doc_id": "d6", "content": "gamma launch checklist", "file_name": "Gamma.md"},
        {"doc_id": "d7", "content": "delta budget summary", "file_name": "Delta.md"},
    ])
    return bm25


class TestQuerySanitizing:
    @pytest.mark.parametrize("query,expected_doc", [
        ("what did we decide about hiring, and who owns it?", "d1"),
        ("alpha/beta rollout", "d4"),
        ("platform = lead", "d2"),
        ("cloud OR local costs", "d3"),
        ("/roadmap", "d2"),
        ("alpha & beta", "d4"),
        ("#tag planning", "d5"),
        ('\U0001F680 "gamma" launch', "d6"),
        ("-budget ^delta", "d7"),
        ("notes:rollout", "d4"),
    ])
    def test_query_shapes_return_results_without_error(self, seeded, caplog, query, expected_doc):
        with caplog.at_level("WARNING"):
            results = seeded.search(query)
        assert "BM25 search error" not in caplog.text
        assert expected_doc in [r["doc_id"] for r in results]

    @pytest.mark.parametrize("char", list(",/=&|+#@<>'\"()[]{}*^~.:;?!"))
    def test_every_syntax_character_is_safe(self, seeded, caplog, char):
        with caplog.at_level("WARNING"):
            results = seeded.search(f"platform {char} lead {char}")
        assert "BM25 search error" not in caplog.text
        assert "d2" in [r["doc_id"] for r in results]

    def test_symbol_only_query_returns_empty(self, seeded):
        assert seeded.search("&&& ,,, //") == []

    def test_uppercase_operator_words_are_plain_terms(self, seeded):
        # As an FTS5 operator, NOT would exclude d2 (contains "lead"); as a
        # plain term the query has no co-occurring match and falls back to OR.
        results = seeded.search("platform NOT lead")
        assert "d2" in [r["doc_id"] for r in results]

    def test_quoted_terms_are_stemmed(self, seeded):
        results = seeded.search("discussing plan")
        assert results[0]["doc_id"] == "d1"
        assert results[0]["match_mode"] == "and"


class TestStrictThenLenient:
    def test_cooccurring_terms_rank_and_matches_first(self, seeded):
        results = seeded.search("platform team hiring")
        assert [(r["doc_id"], r["match_mode"]) for r in results] == [("d1", "and"), ("d2", "or")]

    def test_non_cooccurring_terms_fall_back_to_or(self, seeded):
        results = seeded.search("hiring roadmap")
        assert {r["doc_id"] for r in results} == {"d1", "d2"}
        assert all(r["match_mode"] == "or" for r in results)

    def test_fallback_drops_stop_words(self, seeded):
        # "the" appears in d1 and d2; as an OR term it would pull in d2.
        results = seeded.search("the hiring gamma")
        assert {r["doc_id"] for r in results} == {"d1", "d6"}

    def test_stop_word_only_query_still_searches(self, seeded):
        assert {r["doc_id"] for r in seeded.search("the")} == {"d1", "d2"}

    def test_no_matching_terms_returns_empty(self, seeded):
        assert seeded.search("zzzunknown qqqmissing") == []


class TestUnicodeQueries:
    def test_decomposed_query_matches_composed_content(self, bm25):
        import unicodedata
        composed = "résumé"
        bm25.add_document("d1", f"updated {composed} draft", "Doc.md")
        decomposed = unicodedata.normalize("NFD", composed)
        assert decomposed != composed
        results = bm25.search(decomposed)
        assert [r["doc_id"] for r in results] == ["d1"]
        assert results[0]["match_mode"] == "and"

    def test_composed_query_matches_decomposed_content(self, bm25):
        import unicodedata
        composed = "résumé"
        bm25.add_document("d1", unicodedata.normalize("NFD", composed), "Doc.md")
        assert [r["doc_id"] for r in bm25.search(composed)] == ["d1"]

    def test_combining_marks_survive_term_extraction(self, bm25):
        # Devanagari vowel signs are combining marks with no precomposed form.
        term = "किताब"
        assert bm25._sanitize_query(term) == f'"{term}"'


class TestLiteralOperatorWords:
    @pytest.fixture
    def operator_index(self, bm25):
        bm25.bulk_add([
            {"doc_id": "all3", "content": "alpha or beta comparison", "file_name": "A.md"},
            {"doc_id": "without", "content": "alpha and beta comparison", "file_name": "B.md"},
            {"doc_id": "choice", "content": "choose one or the other", "file_name": "C.md"},
        ])
        return bm25

    def test_uppercase_or_is_a_required_term_in_strict_mode(self, operator_index):
        results = operator_index.search("alpha OR beta")
        assert results[0]["doc_id"] == "all3"
        assert results[0]["match_mode"] == "and"

    def test_or_alone_is_a_plain_term(self, operator_index, caplog):
        with caplog.at_level("WARNING"):
            results = operator_index.search("OR")
        assert "BM25 search error" not in caplog.text
        assert {r["doc_id"] for r in results} == {"all3", "choice"}


class TestFillWithOr:
    @pytest.fixture
    def filled(self, bm25):
        bm25.bulk_add(
            [{"doc_id": f"both{i}", "content": "kiwi mango", "file_name": f"B{i}.md"} for i in range(2)]
            + [{"doc_id": f"kiwi{i}", "content": "kiwi only", "file_name": f"K{i}.md"} for i in range(4)]
            + [{"doc_id": f"mango{i}", "content": "mango only", "file_name": f"M{i}.md"} for i in range(4)]
        )
        return bm25

    @staticmethod
    def _modes(results):
        return [r["match_mode"] for r in results]

    def test_and_rows_first_then_or_fill_without_duplicates(self, filled):
        results = filled.search("kiwi mango", limit=6)
        ids = [r["doc_id"] for r in results]
        assert len(results) == 6
        assert len(set(ids)) == 6
        assert self._modes(results) == ["and"] * 2 + ["or"] * 4
        assert set(ids[:2]) == {"both0", "both1"}

    def test_or_query_is_skipped_when_and_fills_limit(self, filled, monkeypatch):
        modes_run = []
        original = filled._match

        def spy(conn, match_expr, limit, match_mode):
            modes_run.append(match_mode)
            return original(conn, match_expr, limit, match_mode)

        monkeypatch.setattr(filled, "_match", spy)
        results = filled.search("kiwi mango", limit=2)
        assert self._modes(results) == ["and", "and"]
        assert modes_run == ["and"]

    def test_zero_and_matches_equals_or_only(self, filled):
        results = filled.search("kiwi mango zzzunknown", limit=5)
        assert len(results) == 5
        assert set(self._modes(results)) == {"or"}


class TestIntraWordJoiners:
    @pytest.fixture
    def joined(self, bm25):
        bm25.bulk_add([
            {"doc_id": "poss", "content": "Name's launch plan", "file_name": "P.md"},
            {"doc_id": "lone_s", "content": "s", "file_name": "S.md"},
            {"doc_id": "hyph", "content": "weekly follow-up notes", "file_name": "H.md"},
            {"doc_id": "spaced", "content": "a follow up call", "file_name": "F.md"},
            {"doc_id": "only_follow", "content": "follow the leader", "file_name": "O.md"},
        ])
        return bm25

    @pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
    def test_possessive_is_one_term_and_ignores_lone_s(self, joined, apostrophe):
        query = f"name{apostrophe}s"
        assert joined._extract_terms(query) == [query]
        assert [r["doc_id"] for r in joined.search(query)] == ["poss"]

    def test_hyphenated_term_matches_phrase_not_partial(self, joined):
        assert joined._extract_terms("follow-up") == ["follow-up"]
        ids = {r["doc_id"] for r in joined.search("follow-up")}
        assert ids == {"hyph", "spaced"}

    @pytest.mark.parametrize("query", ["-alpha", "'alpha'", "\u2019alpha\u2019", "alpha-"])
    def test_leading_and_trailing_joiners_are_separators(self, bm25, query):
        assert bm25._extract_terms(query) == ["alpha"]
        assert bm25._sanitize_query(query) == '"alpha"'
