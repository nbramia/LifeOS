"""The people-description eval: its counting, its query list, and that it prints counts only."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "retrieval_eval"))

import people_description as ev  # noqa: E402

pytestmark = pytest.mark.unit


def match(*words):
    return SimpleNamespace(matched_words=list(words))


class TestLoadQueries:
    def test_blank_lines_and_comments_are_skipped(self, tmp_path):
        path = tmp_path / "q.txt"
        path.write_text("# header\n\nfundraising\n  pediatric dentist  \n# note\n")
        assert ev.load_queries(path) == ["fundraising", "pediatric dentist"]

    def test_the_committed_list_is_generic_and_substantial(self):
        queries = ev.load_queries(ev.DEFAULT_QUERIES)
        assert len(queries) >= 30
        assert len(set(queries)) == len(queries)
        # Descriptions of fields, never names: no capitalized word in any query but the pronoun.
        assert not [q for q in queries if any(word[0].isupper() and word != "I" for word in q.split())]


class TestScoreSuggestions:
    def test_counts_suggestions_and_those_that_fit(self):
        answers = {"a": [match("x"), match()], "b": [], "c": [match("y", "z")], "d": [match(), match(), match()]}
        result = ev.score_suggestions(["a", "b", "c", "d"], lambda q, top_k: answers[q], top_k=5)
        assert result == {"queries": 4, "answered": 3, "suggestions": 6, "fit": 2}

    def test_the_cutoff_is_passed_on(self):
        seen = []
        ev.score_suggestions(["a"], lambda q, top_k: seen.append(top_k) or [], top_k=7)
        assert seen == [7]

    def test_no_queries_is_all_zeros(self):
        assert ev.score_suggestions([], lambda q, top_k: [match("x")], top_k=5) == {
            "queries": 0, "answered": 0, "suggestions": 0, "fit": 0,
        }


class TestScoreCoverage:
    def test_counts_profiles_with_a_descriptive_line(self):
        docs = ["has", "none", "has", "none", "none"]
        result = ev.score_coverage(docs, lambda d: d == "has")
        assert result == {"profiles": 5, "described": 2}

    def test_an_empty_index_is_zero_of_zero(self):
        assert ev.score_coverage([], lambda d: True) == {"profiles": 0, "described": 0}

    def test_the_real_test_ignores_other_names_alone(self):
        from api.services.person_indexer import has_descriptive_content

        docs = [
            "# A\nModerate relationship (strength: 30/100)\nAlso known as: Rob",
            "# B\nModerate relationship (strength: 30/100)\nWorks at Lantern Fundraising Partners",
            "# C\nModerate relationship (strength: 30/100)\n## Known Facts\n- occupation: pediatric dentist",
            "# D\nModerate relationship (strength: 30/100)\nCategory: Work contact",
        ]
        assert ev.score_coverage(docs, has_descriptive_content) == {"profiles": 4, "described": 2}


class TestIterDocuments:
    def test_every_document_comes_back_across_batches(self):
        import uuid

        import chromadb

        from api.services.vectorstore import VectorStore

        store = VectorStore.__new__(VectorStore)
        store._collection = chromadb.EphemeralClient().create_collection(name=f"docs-{uuid.uuid4().hex}")
        store._collection.add(
            ids=[f"id{n}" for n in range(5)],
            documents=[f"document {n}" for n in range(5)],
            embeddings=[[float(n), 1.0] for n in range(5)],
        )

        assert sorted(store.iter_documents(batch_size=2)) == [f"document {n}" for n in range(5)]

    def test_an_empty_collection_yields_nothing(self):
        import uuid

        import chromadb

        from api.services.vectorstore import VectorStore

        store = VectorStore.__new__(VectorStore)
        store._collection = chromadb.EphemeralClient().create_collection(name=f"docs-{uuid.uuid4().hex}")

        assert list(store.iter_documents()) == []


class TestShare:
    def test_a_share_of_nothing_is_not_a_number(self):
        assert ev._share(0, 0) == "n/a"
        assert ev._share(3, 4) == "0.750"


def test_main_exits_with_a_message_when_the_index_is_unreachable(monkeypatch, capsys):
    from api.services import vectorstore

    class Down:
        def __init__(self, collection_name):
            raise ConnectionError("connection refused to somewhere")

    monkeypatch.setattr(vectorstore, "VectorStore", Down)

    assert ev.main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "people index is not reachable (ConnectionError)" in captured.err
    assert "LIFEOS_CHROMA_URL" in captured.err
    assert "Traceback" not in captured.err and "somewhere" not in captured.err


def test_main_prints_counts_only(tmp_path, monkeypatch, capsys):
    from api.services import person_indexer, vectorstore

    queries = tmp_path / "q.txt"
    queries.write_text("secretfield\n")
    person = SimpleNamespace(canonical_name="Robin Example")
    monkeypatch.setattr(
        person_indexer, "find_people_by_description",
        lambda query, top_k: [SimpleNamespace(person=person, profile="Works at Secret Corp", matched_words=["secretfield"])],
    )

    class Store:
        def __init__(self, collection_name):
            pass

        def iter_documents(self):
            return iter(["# A\nWorks at Secret Corp", "# B\nCategory: Work contact"])

    monkeypatch.setattr(vectorstore, "VectorStore", Store)

    assert ev.main(["--queries", str(queries), "--top-k", "3"]) == 0

    out = capsys.readouterr().out
    assert "queries=1 answered=1" in out
    assert "suggestions=1 fit=1 fit_share=1.000" in out
    assert "profiles=2 described=1 described_share=0.500" in out
    assert not {"secretfield", "Robin", "Example", "Secret", "Corp"} & set(out.replace("=", " ").split())
