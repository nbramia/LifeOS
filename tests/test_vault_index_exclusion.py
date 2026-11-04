"""Personas folders stay out of the index and out of vault search (nbramia/personas#63)."""
import json

import pytest

from api.services.bm25_index import BM25Index
from api.services.hybrid_search import HybridSearch
from api.services.index_exclusion import index_exclude_matcher, matches_exclude_globs
from api.services.indexer import IndexerService
from config.settings import settings

pytestmark = pytest.mark.unit

DEFAULT_GLOBS = ["Work/ML/Personas/**", "Personal/Personas/**", "**/_system/**"]

# One planted marker per excluded path, plus one in an ordinary note.
NOTES = {
    "Work/ML/Personas/Jordan Profile.md": "zzmarkerworkpersona",
    "Personal/Personas/Sam Profile.md": "zzmarkerpersonalpersona",
    "Work/ML/Fundraising/_system/roster.md": "zzmarkersystemroster",
    "Work/ML/Meetings/Standup.md": "zzmarkerordinary",
}
EXCLUDED = ["zzmarkerworkpersona", "zzmarkerpersonalpersona", "zzmarkersystemroster"]


class FakeVectorStore:
    """Returns every chunk it holds, as the real store does for a close query."""

    def __init__(self):
        self.chunks = []

    def add(self, path, text):
        self.chunks.append({
            "id": f"{path}::0", "content": text, "file_path": path,
            "file_name": path.rsplit("/", 1)[-1], "score": 0.9,
            "metadata": {"file_path": path, "file_name": path.rsplit("/", 1)[-1]},
        })

    def search(self, query, top_k=20, **kwargs):
        terms = query.lower().split()
        return [dict(c) for c in self.chunks if all(t in c["content"].lower() for t in terms)][:top_k]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for rel, marker in NOTES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Note\n\nPlanted {marker} text.\n")
    monkeypatch.setattr(settings, "vault_path", root)
    monkeypatch.setattr(settings, "vault_index_exclude_globs_raw", ",".join(DEFAULT_GLOBS))
    monkeypatch.setattr(settings, "vault_no_people_paths_raw", "")
    return root


@pytest.fixture
def search(vault, tmp_path):
    """Search over an index that still holds chunks for every note, as before the exclusion."""
    bm25 = BM25Index(db_path=str(tmp_path / "bm25.db"))
    vectors = FakeVectorStore()
    for rel, marker in NOTES.items():
        path = str((vault / rel).resolve())
        bm25.add_document(f"{path}_0", f"Planted {marker} text.", path.rsplit("/", 1)[-1])
        vectors.add(path, f"Planted {marker} text.")
    return HybridSearch(vector_store=vectors, bm25_index=bm25)


class TestGlobs:
    @pytest.mark.parametrize("rel,expected", [
        ("Work/ML/Personas/a.md", True),
        ("Work/ML/Personas/deep/er/a.md", True),
        ("work/ml/personas/a.md", True),
        ("Personal/Personas/a.md", True),
        ("Work/ML/Fundraising/_system/roster.md", True),
        ("_system/roster.md", True),
        ("Work/ML/Meetings/a.md", False),
        ("Work/ML/PersonasNotes/a.md", False),
        ("Personal/Relationship/Personas.md", False),
        ("Work/ML/Personas Archive/a.md", False),
    ])
    def test_default_globs(self, rel, expected):
        assert matches_exclude_globs(rel, DEFAULT_GLOBS) is expected

    def test_globs_are_configuration(self, vault, monkeypatch):
        monkeypatch.setattr(settings, "vault_index_exclude_globs_raw", "Work/ML/Meetings/**")
        excluded = index_exclude_matcher()
        assert excluded(str((vault / "Work/ML/Meetings/Standup.md").resolve()))
        assert not excluded(str((vault / "Work/ML/Personas/Jordan Profile.md").resolve()))

    def test_empty_config_excludes_nothing(self, vault, monkeypatch):
        monkeypatch.setattr(settings, "vault_index_exclude_globs_raw", "")
        assert index_exclude_matcher() is None

    def test_glob_without_wildcard_is_not_a_prefix_match(self, vault, monkeypatch):
        monkeypatch.setattr(settings, "vault_index_exclude_globs_raw", "Work/ML/Personas.md")
        excluded = index_exclude_matcher()
        assert excluded(str((vault / "Work/ML/Personas.md").resolve()) + "_2")
        assert not excluded(str((vault / "Work/ML/Personas.md.bak").resolve()))

    def test_chat_vault_fetch_drops_excluded(self, vault, monkeypatch):
        import asyncio
        from api.routes import chat

        class Stub:
            def __init__(self, *a, **k): pass
            def search(self, **k):
                return [{"file_path": str((vault / "Work/ML/Personas/Jordan Profile.md").resolve()), "id": "x::0"},
                        {"file_path": str((vault / "Work/ML/Meetings/Standup.md").resolve()), "id": "y::0"}]
        monkeypatch.setattr(chat, "HybridSearch", Stub)
        out = asyncio.run(chat._fetch_vault("q", 5))
        assert [c["id"] for c in out] == ["y::0"]

    def test_tagger_skips_excluded(self, vault):
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
        import sync_vault_tag
        assert [p.name for p in sync_vault_tag.list_vault_notes(vault)] == ["Standup.md"]

    def test_matcher_accepts_chunk_ids(self, vault):
        excluded = index_exclude_matcher()
        assert excluded(f"{(vault / 'Personal/Personas/Sam Profile.md').resolve()}_3")


class TestIndexRun:
    def test_excluded_files_are_not_indexed_and_prior_entries_are_removed(self, vault, tmp_path):
        indexer = IndexerService(vault_path=str(vault), db_path=str(tmp_path / "db"), connect_stores=False)
        state_file = tmp_path / "index_state.json"
        indexer.INDEX_STATE_FILE = str(state_file)
        # Indexed before the exclusion existed: every note has an entry.
        state_file.write_text(json.dumps({str(vault / rel): 9e12 for rel in NOTES}))

        result = indexer.scan_changes(compare_tags=False)

        assert result["to_index"] == []
        assert sorted(result["deleted"]) == sorted(
            str(vault / rel) for rel in NOTES if "Meetings" not in rel
        )

    def test_fresh_run_indexes_only_ordinary_notes(self, vault, tmp_path):
        indexer = IndexerService(vault_path=str(vault), db_path=str(tmp_path / "db"), connect_stores=False)
        indexer.INDEX_STATE_FILE = str(tmp_path / "missing.json")
        assert indexer.scan_changes(compare_tags=False)["to_index"] == [str(vault / "Work/ML/Meetings/Standup.md")]

    def test_index_file_skips_excluded(self, vault, tmp_path):
        indexer = IndexerService(vault_path=str(vault), db_path=str(tmp_path / "db"), connect_stores=False)
        assert indexer.index_file(str(vault / "Personal/Personas/Sam Profile.md")) is None
        assert indexer._is_excluded_from_index(vault / "Work/ML/Personas/Jordan Profile.md")
        assert not indexer._is_excluded_from_index(vault / "Work/ML/Meetings/Standup.md")


class TestSearch:
    @pytest.mark.parametrize("marker", EXCLUDED)
    def test_excluded_marker_returns_nothing(self, search, marker):
        assert search.search(marker, use_reranker=False) == []

    def test_ordinary_marker_is_returned(self, search):
        hits = search.search("zzmarkerordinary", use_reranker=False)
        assert [h["file_path"].rsplit("/", 1)[-1] for h in hits] == ["Standup.md"]

    def test_excluded_chunks_never_appear_in_a_broad_query(self, search):
        hits = search.search("Planted text", use_reranker=False)
        assert hits
        assert not any("Personas" in h["file_path"] or "_system" in h["file_path"] for h in hits)

    def test_removing_the_globs_restores_results(self, search, monkeypatch):
        monkeypatch.setattr(settings, "vault_index_exclude_globs_raw", "")
        assert search.search("zzmarkerworkpersona", use_reranker=False)


class TestLiveVaultTools:
    def test_read_vault_file_skips_excluded(self, vault):
        from api.services.agent_tools import _tool_read_vault_file
        assert "not found" in _tool_read_vault_file({"filename": "Jordan Profile"})
        assert "zzmarkerordinary" in _tool_read_vault_file({"filename": "Standup"})

    def test_list_skips_excluded(self, vault):
        from api.services.vault_listing import list_vault_entries
        names = [e["relative_path"] for e in list_vault_entries(vault, "", None, 50, 0)["entries"]]
        assert names == ["Work/ML/Meetings/Standup.md"]
