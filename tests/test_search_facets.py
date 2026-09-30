"""Facet pre-filtering across hybrid search, the BM25 index and the vector store."""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api.services.bm25_index import BM25Index, file_path_of_doc_id
from api.services.hybrid_search import HybridSearch
from api.services.search_facets import SearchFacets, resolve_allowed_paths
from api.services.vault_tag_store import TagRecord, VaultTagStore
from config.settings import settings

pytestmark = pytest.mark.unit

VAULT = "/synthetic/vault"


def _abs(rel: str) -> str:
    return f"{VAULT}/{rel}"


FILES = {
    "Work/Meetings/hiring-sync.md": ("meeting", "work/hiring", ["Avery"]),
    "Work/Meetings/budget-sync.md": ("meeting", "work/budget", ["Blake"]),
    "Personal/Journal/day.md": ("journal", "life/reflection", ["Avery"]),
}


class FakeVectorStore:
    """Records the restriction it receives and honours it like the real store."""

    def __init__(self, note_types=None, tag_files=None):
        self.calls: list[dict] = []
        self.candidates: list[list[str]] = []
        self.note_types = note_types or {}
        self.tag_files = tag_files or {}

    def search(self, query, top_k=20, filters=None, **kwargs):
        self.calls.append(kwargs)
        allowed = kwargs.get("file_paths")
        rows = []
        for i, rel in enumerate(FILES):
            path = _abs(rel)
            if allowed is not None and path not in allowed:
                continue
            rows.append({
                "id": f"{path}::0", "content": f"budget hiring notes {i}",
                "file_path": path, "file_name": Path(path).name,
                "score": 1.0 - i * 0.1, "modified_date": "2025-01-01",
            })
        self.candidates.append([r["file_path"] for r in rows])
        return rows

    def file_paths_with_people(self, file_paths, names):
        wanted = {n.casefold() for n in names}
        return {
            p for p in file_paths
            if any(x.casefold() in wanted for _, _, people in [FILES[p[len(VAULT) + 1:]]] for x in people)
        }

    def file_paths_matching(self, where=None):
        if where and "note_type" in where:
            wanted = where["note_type"]["$in"]
            return {p for p, t in self.note_types.items() if t in wanted}
        if where:
            keys = [k for c in (where.get("$or") or [where]) for k in c]
            return {p for p, tags in self.tag_files.items() if any(k in tags for k in keys)}
        return set(self.note_types)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", Path(VAULT))
    bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
    store = VaultTagStore(str(tmp_path / "tags.db"))
    for rel, (doc_type, topic, people) in FILES.items():
        bm25.add_document(
            f"{_abs(rel)}_0", "budget hiring notes for the team", Path(rel).name, people=people
        )
        store.upsert(TagRecord(
            file_path=rel, content_sha256="x", vocab_version="v1",
            doc_type=doc_type, topic=topic,
        ))
    vec = FakeVectorStore()
    hs = HybridSearch(vector_store=vec, bm25_index=bm25, tag_store=store)
    return hs, vec, bm25, store


def _spy_bm25(bm25):
    seen = []
    real = bm25.search

    def spy(query, limit=20, **kw):
        out = real(query, limit=limit, **kw)
        seen.append(({r["doc_id"] for r in out}, kw))
        return out

    bm25.search = spy
    return seen


def test_machine_facets_prefilter_both_arms(env):
    hs, vec, bm25, _ = env
    seen = _spy_bm25(bm25)
    results = hs.search(
        "budget hiring", top_k=10, use_reranker=False,
        facets=SearchFacets(doc_type="meeting"),
    )
    allowed = {_abs("Work/Meetings/hiring-sync.md"), _abs("Work/Meetings/budget-sync.md")}
    assert set(vec.calls[0]["file_paths"]) == allowed
    assert set(vec.candidates[0]) == allowed
    assert {file_path_of_doc_id(d) for d in seen[0][0]} == allowed
    assert {r["file_path"] for r in results} == allowed


def test_folder_and_topic_child_match_narrow_to_one_file(env):
    hs, vec, _, _ = env
    results = hs.search(
        "budget", top_k=10, use_reranker=False,
        facets=SearchFacets(folder="Work/Meetings", topic="work/hiring"),
    )
    assert {r["file_path"] for r in results} == {_abs("Work/Meetings/hiring-sync.md")}


def test_parent_topic_matches_children(env):
    hs, vec, _, _ = env
    hs.search("budget", top_k=10, use_reranker=False, facets=SearchFacets(topic="work"))
    assert len(vec.calls[0]["file_paths"]) == 2


def test_people_facet_filters_via_bm25_people_column(env):
    hs, vec, _, _ = env
    results = hs.search(
        "budget", top_k=10, use_reranker=False, facets=SearchFacets(people=["Avery"])
    )
    assert {r["file_path"] for r in results} == {
        _abs("Work/Meetings/hiring-sync.md"), _abs("Personal/Journal/day.md"),
    }


def test_note_type_and_tags_use_vector_metadata(env):
    hs, vec, _, _ = env
    vec.note_types = {_abs("Personal/Journal/day.md"): "Personal",
                      _abs("Work/Meetings/hiring-sync.md"): "Work"}
    vec.tag_files = {_abs("Personal/Journal/day.md"): {"tag:reflection"}}
    results = hs.search(
        "budget", top_k=10, use_reranker=False,
        facets=SearchFacets(note_type="Personal", tags=["Reflection"]),
    )
    assert [r["file_path"] for r in results] == [_abs("Personal/Journal/day.md")]


def test_zero_matching_files_returns_empty_without_degradation(env, monkeypatch):
    hs, vec, bm25, _ = env
    degraded = MagicMock()
    monkeypatch.setattr("api.services.service_health.record_degradation", degraded)
    seen = _spy_bm25(bm25)
    assert hs.search("budget", facets=SearchFacets(doc_type="contract")) == []
    assert vec.calls == [] and seen == []
    degraded.assert_not_called()


def test_missing_tag_store_matches_nothing_for_machine_facets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", Path(VAULT))
    monkeypatch.setattr(
        "api.services.vault_tag_store.get_vault_tags_db_path",
        lambda: str(tmp_path / "absent.db"),
    )
    bm25 = BM25Index(db_path=str(tmp_path / "b.db"))
    bm25.add_document(f"{_abs('Work/a.md')}_0", "budget", "a.md")
    hs = HybridSearch(vector_store=FakeVectorStore(), bm25_index=bm25)
    assert hs.search("budget", facets=SearchFacets(doc_type="meeting")) == []
    assert not (tmp_path / "absent.db").exists()
    # Non-machine facets still work without the tag store.
    got = hs.search("budget", use_reranker=False, facets=SearchFacets(folder="Work"))
    assert {r["file_path"] for r in got} == {_abs("Work/a.md")}


def test_empty_tag_store_matches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", Path(VAULT))
    empty = VaultTagStore(str(tmp_path / "t.db"))
    allowed = resolve_allowed_paths(
        SearchFacets(doc_type="meeting"), FakeVectorStore(), None, tag_store=empty
    )
    assert allowed == set()


def test_folder_cannot_escape_the_vault(env):
    _, vec, bm25, store = env
    for folder in ("../other", "/etc", "Work/../../x"):
        assert resolve_allowed_paths(
            SearchFacets(folder=folder), vec, bm25, tag_store=store
        ) == set()


def test_folder_matches_on_a_directory_boundary(env):
    _, vec, bm25, store = env
    bm25.add_document(f"{_abs('Work/Meetings-old/x.md')}_0", "budget", "x.md")
    allowed = resolve_allowed_paths(
        SearchFacets(folder="Work/Meetings"), vec, bm25, tag_store=store
    )
    assert allowed == {_abs("Work/Meetings/hiring-sync.md"), _abs("Work/Meetings/budget-sync.md")}


def test_no_facets_leaves_both_arms_untouched(env):
    hs, vec, bm25, _ = env
    seen = _spy_bm25(bm25)
    baseline = hs.search("budget hiring", top_k=10, use_reranker=False)
    with_empty = hs.search(
        "budget hiring", top_k=10, use_reranker=False, facets=SearchFacets()
    )
    assert vec.calls == [{}, {}]
    assert [kw for _, kw in seen] == [{}, {}]
    assert [(r["id"], r["hybrid_score"]) for r in baseline] == [
        (r["id"], r["hybrid_score"]) for r in with_empty
    ]
    assert len(baseline) == 3


def test_boost_multiplies_matching_scores_and_does_not_filter(env, monkeypatch):
    hs, vec, _, _ = env
    monkeypatch.setattr(settings, "search_facet_boost", 1.2)
    base = {
        r["file_path"]: r["hybrid_score"]
        for r in hs.search("budget hiring", top_k=10, use_reranker=False)
    }
    boosted = {
        r["file_path"]: r["hybrid_score"]
        for r in hs.search(
            "budget hiring", top_k=10, use_reranker=False,
            facets=SearchFacets(doc_type="journal", boost=True),
        )
    }
    assert set(boosted) == set(base)
    assert "file_paths" not in vec.calls[-1]
    journal = _abs("Personal/Journal/day.md")
    for path, score in base.items():
        expected = score * 1.2 if path == journal else score
        assert boosted[path] == pytest.approx(expected)


def test_attribution_records_facet_names_not_values(env, monkeypatch):
    hs, _, _, _ = env
    spans = []
    import api.services.hybrid_search as mod
    real = mod.trace_span

    def capture(name, **kw):
        spans.append((name, kw))
        return real(name, **kw)

    monkeypatch.setattr(mod, "trace_span", capture)
    hs.search("budget", use_reranker=False,
              facets=SearchFacets(doc_type="meeting", folder="Work"))
    attribution = [kw for n, kw in spans if n == "search_attribution"][0]
    assert attribution["facets"] == ["folder", "doc_type"]
    assert "meeting" not in str(attribution)

    spans.clear()
    hs.search("budget", use_reranker=False)
    assert "facets" not in [kw for n, kw in spans if n == "search_attribution"][0]


def test_tag_store_paths_are_mapped_from_vault_relative_to_absolute(env):
    _, vec, bm25, store = env
    allowed = resolve_allowed_paths(SearchFacets(doc_type="journal"), vec, bm25, tag_store=store)
    assert allowed == {os.path.join(VAULT, "Personal/Journal/day.md")}


# -- BM25 index ---------------------------------------------------------------

def test_file_path_of_doc_id_forms():
    assert file_path_of_doc_id("/v/a_b.md_3") == "/v/a_b.md"
    assert file_path_of_doc_id("/v/a_b.md::summary") == "/v/a_b.md"
    assert file_path_of_doc_id("plain") == "plain"


def test_bm25_restriction_applies_before_the_limit(tmp_path):
    bm25 = BM25Index(db_path=str(tmp_path / "b.db"))
    for i in range(30):
        bm25.add_document(f"/v/noise{i}.md_0", "quarterly budget budget budget", "n.md")
    bm25.add_document("/v/target.md_0", "budget", "t.md")
    bm25.add_document("/v/target.md::summary", "budget summary", "t.md")
    got = bm25.search("budget", limit=5, file_paths={"/v/target.md"})
    assert {r["doc_id"] for r in got} == {"/v/target.md_0", "/v/target.md::summary"}
    assert bm25.search("budget", limit=5, file_paths=set()) == []
    assert len(bm25.search("budget", limit=5)) == 5


# -- Vector store --------------------------------------------------------------

class FakeCollection:
    def __init__(self, rows):
        self.rows = rows  # (id, distance, file_path)
        self.wheres = []

    def query(self, query_embeddings, n_results, where=None, include=None):
        self.wheres.append(where)
        conds = where["$and"] if "$and" in where else [where]
        paths = next(c["file_path"]["$in"] for c in conds if "file_path" in c)
        rows = sorted((r for r in self.rows if r[2] in paths), key=lambda r: r[1])[:n_results]
        return {
            "ids": [[r[0] for r in rows]],
            "documents": [[f"doc {r[0]}" for r in rows]],
            "metadatas": [[{"file_path": r[2], "modified_date": ""} for r in rows]],
            "distances": [[r[1] for r in rows]],
        }

    def get(self, where=None, include=None):
        return {"ids": [r[0] for r in self.rows]}


def _vector_store(rows):
    from api.services.vectorstore import VectorStore
    vs = VectorStore.__new__(VectorStore)
    vs._collection = FakeCollection(rows)
    vs._embedding_service = MagicMock()
    vs._embedding_service.embed_text.return_value = [0.0]
    return vs


def test_vector_search_batches_large_path_sets_and_merges_nearest(monkeypatch):
    import api.services.vectorstore as vsmod
    monkeypatch.setattr(vsmod, "FILE_PATH_BATCH", 3)
    rows = [(f"/f{i}.md::0", i / 100, f"/f{i}.md") for i in range(10)]
    vs = _vector_store(rows)
    got = vs.search("q", top_k=2, file_paths=[f"/f{i}.md" for i in range(10)], recency_weight=0.0)
    assert len(vs._collection.wheres) == 4
    assert [r["id"] for r in got] == ["/f0.md::0", "/f1.md::0"]


def test_vector_search_empty_path_list_matches_nothing():
    vs = _vector_store([("/a.md::0", 0.1, "/a.md")])
    assert vs.search("q", top_k=3, file_paths=[]) == []
    assert vs._collection.wheres == []


def test_vector_search_without_paths_passes_no_restriction():
    vs = _vector_store([])
    vs._collection.query = MagicMock(return_value={"ids": [[]]})
    vs.search("q", top_k=3)
    assert vs._collection.query.call_args.kwargs["where"] is None


def test_file_paths_matching_reads_chunk_ids():
    vs = _vector_store([("/a.md::0", 0, "/a.md"), ("/a.md::1", 0, "/a.md"), ("/b::c.md::0", 0, "/b::c.md")])
    assert vs.file_paths_matching() == {"/a.md", "/b::c.md"}


def test_add_document_writes_scalar_tag_keys_and_modified_day():
    vs = _vector_store([])
    vs._collection.add = MagicMock()
    vs._embedding_service.embed_texts.return_value = [[0.0]]
    vs.add_document(
        [{"content": "c", "chunk_index": 0}],
        {"file_path": "/a.md", "file_name": "a.md", "tags": ["#Project/X", "plain"]},
    )
    meta = vs._collection.add.call_args.kwargs["metadatas"][0]
    assert meta["tag:project/x"] is True and meta["tag:plain"] is True
    assert meta["tags"] == '["#Project/X", "plain"]'
    assert meta["modified_day"] == -1_000_000
    vs.add_document(
        [{"content": "c", "chunk_index": 0}],
        {"file_path": "/a.md", "file_name": "a.md", "modified_date": "1970-01-11T08:00:00"},
    )
    assert vs._collection.add.call_args.kwargs["metadatas"][0]["modified_day"] == 10


# -- Tag key backfill (real Chroma) --------------------------------------------

@pytest.fixture
def real_store(tmp_path):
    import chromadb
    from api.services.vectorstore import VectorStore
    vs = VectorStore.__new__(VectorStore)
    vs._collection = chromadb.PersistentClient(path=str(tmp_path / "chroma")).create_collection(
        "backfill_col", metadata={"hnsw:space": "cosine"}
    )
    vs._embedding_service = MagicMock()
    vs._collection.add(
        ids=["/v/a.md::0", "/v/a.md::1", "/v/b.md::0", "/v/c.md::0"],
        embeddings=[[1.0, 0.0], [0.5, 0.5], [0.0, 1.0], [0.3, 0.7]],
        documents=["alpha", "beta", "gamma", "delta"],
        metadatas=[
            {"file_path": "/v/a.md", "note_type": "Work", "chunk_index": 0, "modified_day": -1, "tags": '["Project/X", "plain"]'},
            {"file_path": "/v/a.md", "note_type": "Work", "chunk_index": 1, "modified_day": -1, "tags": '["Project/X", "plain"]'},
            {"file_path": "/v/b.md", "note_type": "Work", "chunk_index": 0, "modified_day": -1, "tags": "[]"},
            {"file_path": "/v/c.md", "note_type": "Work", "chunk_index": 0, "modified_day": -1, "tags": '["Hashed"]',
             "tag:hashed": True},
        ],
    )
    return vs


def test_backfill_adds_keys_and_preserves_metadata_documents_and_embeddings(real_store):
    col = real_store._collection
    before = col.get(include=["embeddings", "metadatas", "documents"])
    assert real_store.backfill_search_keys(batch_size=2) == 2
    after = col.get(include=["embeddings", "metadatas", "documents"])
    assert before["ids"] == after["ids"]
    assert before["embeddings"].tolist() == after["embeddings"].tolist()
    assert before["documents"] == after["documents"]
    for b, a in zip(before["metadatas"], after["metadatas"]):
        assert {k: v for k, v in a.items() if not k.startswith("tag:")} == {
            k: v for k, v in b.items() if not k.startswith("tag:")
        }
    a0 = after["metadatas"][after["ids"].index("/v/a.md::0")]
    assert a0["tag:project/x"] is True and a0["tag:plain"] is True
    assert not any(
        k.startswith("tag:") for k in after["metadatas"][after["ids"].index("/v/b.md::0")]
    )


def test_backfill_is_idempotent(real_store):
    assert real_store.backfill_search_keys() == 2
    assert real_store.backfill_search_keys() == 0


def test_tags_facet_finds_a_chunk_indexed_without_keys_once_backfilled(real_store):
    facets = SearchFacets(tags=["project/x"])
    assert resolve_allowed_paths(facets, real_store, None) == set()
    real_store.backfill_search_keys()
    assert resolve_allowed_paths(facets, real_store, None) == {"/v/a.md"}


def test_reindex_script_reports_the_count_and_a_second_run_writes_nothing(real_store):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import sync_vault_reindex as script

    indexer = MagicMock()
    indexer.vector_store = real_store
    assert script.backfill_search_keys(indexer) == 2
    assert script.backfill_search_keys(indexer) == 0


def _tagged_chunks(store, migrated, legacy):
    """``migrated`` complete tagged chunks, then ``legacy`` without keys."""
    ids, metas = [], []
    for i in range(migrated + legacy):
        meta = {"file_path": f"/v/t{i}.md", "modified_date": "2025-01-01", "tags": '["Project/X"]'}
        if i < migrated:
            meta.update({"tag:project/x": True, "modified_day": 20089})
        ids.append(f"/v/t{i}.md::0")
        metas.append(meta)
    store._collection.add(
        ids=ids, embeddings=[[1.0, 0.0]] * len(ids), documents=["d"] * len(ids), metadatas=metas
    )


def test_backfill_scans_every_chunk_so_a_late_legacy_chunk_is_repaired(tmp_path):
    vs = _legacy_store(tmp_path)
    vs._collection.delete(ids=["/v/z.md::0"])
    _tagged_chunks(vs, migrated=20, legacy=1)
    assert vs.backfill_search_keys(batch_size=5) == 1
    legacy = vs._collection.get(ids=["/v/t20.md::0"], include=["metadatas"])["metadatas"][0]
    assert legacy["tag:project/x"] is True and legacy["modified_day"] == 20089
    assert vs.backfill_search_keys(batch_size=5) == 0


def test_backfill_writes_modified_day_on_untagged_legacy_chunks(tmp_path):
    vs = _legacy_store(tmp_path)
    vs._collection.add(
        ids=["/v/u.md::0", "/v/n.md::0"], embeddings=[[0.0, 1.0]] * 2, documents=["a", "b"],
        metadatas=[
            {"file_path": "/v/u.md", "tags": "[]", "modified_date": "2025-01-01"},
            {"file_path": "/v/n.md", "tags": "[]", "modified_date": ""},
        ],
    )
    vs.backfill_search_keys()
    got = vs._collection.get(include=["metadatas"])
    days = {m["file_path"]: m["modified_day"] for m in got["metadatas"]}
    assert days["/v/u.md"] == 20089 and days["/v/n.md"] == -1_000_000


def _legacy_store(tmp_path):
    import chromadb
    from api.services.vectorstore import VectorStore
    vs = VectorStore.__new__(VectorStore)
    vs._collection = chromadb.PersistentClient(path=str(tmp_path / "chroma2")).create_collection("legacy_col")
    vs._embedding_service = MagicMock()
    vs._collection.add(
        ids=["/v/z.md::0"], embeddings=[[1.0, 0.0]], documents=["zeta"],
        metadatas=[{"file_path": "/v/z.md", "tags": '["Project/X"]'}],
    )
    return vs


# -- People facet: whole person values -----------------------------------------

@pytest.fixture
def people_env(tmp_path):
    import chromadb
    from api.services.vectorstore import VectorStore
    vs = VectorStore.__new__(VectorStore)
    vs._collection = chromadb.PersistentClient(path=str(tmp_path / "chroma")).create_collection("people_col")
    vs._embedding_service = MagicMock()
    bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
    people_by_file = {
        "/v/roberts.md": ["Roberts"],
        "/v/split.md": ["Avery Stone", "Blake Reed"],
        "/v/exact.md": ["Stone Blake"],
        "/v/robert.md": ["robert"],
    }
    for path, people in people_by_file.items():
        vs._collection.add(
            ids=[f"{path}::0"], embeddings=[[1.0, 0.0]], documents=["notes"],
            metadatas=[{"file_path": path, "people": json.dumps(people)}],
        )
        bm25.add_document(f"{path}_0", "notes", Path(path).name, people=people)
    return vs, bm25


def _people(env, *names):
    vs, bm25 = env
    return resolve_allowed_paths(SearchFacets(people=list(names)), vs, bm25)


def test_people_facet_does_not_stem_a_name_onto_a_longer_one(people_env):
    assert _people(people_env, "Robert") == {"/v/robert.md"}


def test_people_facet_does_not_match_across_adjacent_people(people_env):
    assert _people(people_env, "Stone Blake") == {"/v/exact.md"}


def test_people_facet_exact_and_multi_value_matches(people_env):
    assert _people(people_env, "Avery Stone") == {"/v/split.md"}
    assert _people(people_env, "blake reed", "ROBERTS") == {"/v/split.md", "/v/roberts.md"}


# -- Symlinked vault root --------------------------------------------------------

def test_machine_and_folder_facets_find_files_under_a_symlinked_vault_root(tmp_path, monkeypatch):
    real = tmp_path / "real_vault"
    (real / "Work").mkdir(parents=True)
    note = real / "Work" / "sync.md"
    note.write_text("budget")
    link = tmp_path / "vault_link"
    link.symlink_to(real)
    monkeypatch.setattr(settings, "vault_path", link)
    indexed = str(note.resolve())
    bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
    bm25.add_document(f"{indexed}_0", "budget", "sync.md")
    store = VaultTagStore(str(tmp_path / "tags.db"))
    store.upsert(TagRecord(
        file_path="Work/sync.md", content_sha256="x", vocab_version="v1", doc_type="meeting",
    ))
    vec = FakeVectorStore()
    assert resolve_allowed_paths(
        SearchFacets(doc_type="meeting"), vec, bm25, tag_store=store
    ) == {indexed}
    assert resolve_allowed_paths(SearchFacets(folder="Work"), vec, bm25) == {indexed}
    assert resolve_allowed_paths(
        SearchFacets(folder="Work", doc_type="meeting"), vec, bm25, tag_store=store
    ) == {indexed}


# -- Date window before candidate limits ------------------------------------------

def test_bm25_date_window_applies_before_the_limit(tmp_path):
    bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
    for i in range(5):
        bm25.add_document(f"/v/new{i}.md_0", "budget budget budget", "n.md", modified_date="2026-05-01")
    bm25.add_document("/v/old.md_0", "budget", "o.md", modified_date="2025-01-01")
    bm25.add_document("/v/undated.md_0", "budget budget", "u.md")
    got = bm25.search("budget", limit=1, date_to="2025-12-31")
    assert got and got[0]["doc_id"] in {"/v/old.md_0", "/v/undated.md_0"}
    assert {r["doc_id"] for r in bm25.search("budget", limit=10, date_from="2026-01-01")} == {
        f"/v/new{i}.md_0" for i in range(5)
    } | {"/v/undated.md_0"}


def _dated_store(tmp_path, n_new):
    import chromadb
    from api.services.vectorstore import VectorStore, modified_day
    vs = VectorStore.__new__(VectorStore)
    vs._collection = chromadb.PersistentClient(path=str(tmp_path / "chroma")).create_collection(
        "dates_col", metadata={"hnsw:space": "cosine"}
    )
    vs._embedding_service = MagicMock()
    vs._embedding_service.embed_text.return_value = [1.0, 0.0]
    rows = [(f"/v/new{i}.md", "2026-05-01", [1.0, 0.0001 * i]) for i in range(n_new)]
    rows += [("/v/old.md", "2025-01-01", [0.0, 1.0]), ("/v/undated.md", "", [0.1, 1.0])]
    for i in range(0, len(rows), 2000):
        part = rows[i:i + 2000]
        vs._collection.add(
            ids=[f"{p}::0" for p, _, _ in part],
            embeddings=[e for _, _, e in part],
            documents=["x"] * len(part),
            metadatas=[
                {"file_path": p, "modified_date": d, "modified_day": modified_day(d)}
                for p, d, _ in part
            ],
        )
    return vs


def test_vector_date_window_restricts_eligibility_inside_the_query(tmp_path):
    vs = _dated_store(tmp_path, 6400)
    got = vs.search("q", top_k=1, recency_weight=0.0, date_to="2025-12-31")
    assert [r["file_path"] for r in got] and got[0]["file_path"] in {"/v/old.md", "/v/undated.md"}
    assert vs.search("q", top_k=1, recency_weight=0.0)[0]["file_path"].startswith("/v/new")


def test_vector_date_window_bounds_and_undated_pass(tmp_path):
    vs = _dated_store(tmp_path, 3)
    both = vs.search("q", top_k=10, recency_weight=0.0, date_from="2025-01-01", date_to="2025-12-31")
    assert {r["file_path"] for r in both} == {"/v/old.md", "/v/undated.md"}
    later = vs.search("q", top_k=10, recency_weight=0.0, date_from="2026-01-01")
    assert {r["file_path"] for r in later} == {f"/v/new{i}.md" for i in range(3)} | {"/v/undated.md"}


def test_hybrid_top_k_one_with_facet_and_date_returns_the_eligible_note(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", Path(VAULT))
    bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
    for i in range(5):
        bm25.add_document(f"{_abs(f'Work/n{i}.md')}_0", "budget budget budget", "n.md", modified_date="2026-05-01")
    bm25.add_document(f"{_abs('Work/old.md')}_0", "budget", "old.md", modified_date="2025-01-01")
    vec = FakeVectorStore()
    vec.search = lambda *a, **k: []
    hs = HybridSearch(vector_store=vec, bm25_index=bm25)
    got = hs.search(
        "budget", top_k=1, use_reranker=False, date_to="2025-12-31",
        facets=SearchFacets(note_type=["Work"]),
    )
    assert [r["file_path"] for r in got] == [_abs("Work/old.md")]


def test_note_type_equals_folder_and_ignores_stale_stored_values(env):
    hs, vec, _, _ = env
    # Stored metadata is deliberately wrong: the alias must not read it.
    vec.note_types = {_abs("Work/Meetings/hiring-sync.md"): "Personal",
                      _abs("Personal/Journal/day.md"): "Work"}
    by_type = hs.search("budget", top_k=10, use_reranker=False,
                        facets=SearchFacets(note_type=["Work"]))
    by_folder = hs.search("budget", top_k=10, use_reranker=False,
                          facets=SearchFacets(folder="Work"))
    assert {r["file_path"] for r in by_type} == {r["file_path"] for r in by_folder}
    assert {r["file_path"] for r in by_type} == {
        _abs("Work/Meetings/hiring-sync.md"), _abs("Work/Meetings/budget-sync.md"),
    }


def test_note_type_ml_maps_to_work_ml_and_other_is_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", Path(VAULT))
    bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
    rels = ["Work/ML/a.md", "Work/b.md", "Personal/c.md", "Inbox/d.md", "Granola/e.md"]
    for rel in rels:
        bm25.add_document(f"{_abs(rel)}_0", "budget", Path(rel).name)
    vec = FakeVectorStore()
    got = lambda nt: {  # noqa: E731
        p[len(VAULT) + 1:] for p in resolve_allowed_paths(
            SearchFacets(note_type=nt), vec, bm25, vault_root=VAULT)
    }
    assert got(["ML"]) == {"Work/ML/a.md"}
    assert got(["Granola"]) == {"Granola/e.md"}
    assert got(["Other"]) == {"Inbox/d.md"}
    assert got(["Personal", "Other"]) == {"Personal/c.md", "Inbox/d.md"}


def test_unknown_note_type_still_matches_stored_metadata(env):
    hs, vec, _, _ = env
    vec.note_types = {_abs("Work/Meetings/hiring-sync.md"): "calendar_event"}
    got = resolve_allowed_paths(
        SearchFacets(note_type=["calendar_event"]), vec, hs.bm25_index, vault_root=VAULT)
    assert got == {_abs("Work/Meetings/hiring-sync.md")}


def test_infer_note_type_is_vault_relative():
    from api.services.vault_listing import infer_note_type
    assert infer_note_type("Personal/Journal/day.md") == "Personal"
    assert infer_note_type("Work/ML/a.md") == "ML"
    assert infer_note_type("Work/a.md") == "Work"
    assert infer_note_type("LifeOS/a.md") == "LifeOS"
    assert infer_note_type("Granola/a.md") == "Granola"
    assert infer_note_type("Inbox/work-notes.md") == "Other"
