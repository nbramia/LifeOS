"""Tag phrase in chunk context, its BM25 reachability, and the re-embed trigger."""
from pathlib import Path

import pytest

from api.services import indexer as indexer_mod
from api.services.bm25_index import BM25Index
from api.services.chunker import generate_chunk_context
from api.services.indexer import IndexerService
from api.services.vault_tag_store import TagRecord, VaultTagStore

pytestmark = pytest.mark.unit


def _tags(**over) -> TagRecord:
    base = dict(
        file_path="Notes/retro.md",
        content_sha256="x",
        vocab_version="v1",
        doc_type="meeting_notes",
        doc_type_conf=0.9,
        domain="work",
        domain_conf=0.9,
        topic="work/hiring",
        topic_conf=0.9,
        project="apollo",
        project_conf=0.8,
    )
    base.update(over)
    return TagRecord(**base)


def _ctx(tags=None) -> str:
    return generate_chunk_context(Path("/v/Notes/retro.md"), {}, "body text", 0, 1, tags=tags)


class TestGenerateChunkContext:
    def test_phrase_added_with_project(self):
        ctx = _ctx(_tags())
        assert "meeting notes in the work domain about work hiring for project apollo" in ctx

    def test_no_project_below_confidence_or_none(self):
        assert "for project" not in _ctx(_tags(project_conf=0.59))
        assert "for project" not in _ctx(_tags(project="none"))
        assert "about work hiring" in _ctx(_tags(project=None))

    def test_no_record_or_low_doc_type_confidence_is_unchanged(self):
        plain = _ctx(None)
        assert _ctx(_tags(doc_type_conf=0.59)) == plain
        assert _ctx(_tags(doc_type_conf=None)) == plain
        assert _ctx(_tags(doc_type=None)) == plain
        assert "Classified" not in plain

    def test_threshold_is_inclusive(self):
        assert "Classified as meeting notes" in _ctx(_tags(doc_type_conf=0.6))

    def test_phrase_precedes_part_marker_and_has_no_people(self):
        ctx = generate_chunk_context(
            Path("/v/Notes/retro.md"), {"people": ["Zed Person"]}, "b", 1, 3, tags=_tags()
        )
        assert ctx.endswith("(Part 2 of 3)")
        assert ctx.index("Classified as") < ctx.index("(Part 2")
        assert "Zed" not in ctx.split("Classified as")[1]


class _FakeVectors:
    def __init__(self):
        self.calls: list[str] = []

    def update_document(self, chunks, metadata):
        self.calls.append(metadata["file_path"])

    def delete_document(self, file_path):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    monkeypatch.setattr(IndexerService, "INDEX_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(indexer_mod, "HAS_V2_PEOPLE", False)
    svc = IndexerService.__new__(IndexerService)
    svc.vault_path = vault
    svc.vector_store = _FakeVectors()
    svc.bm25_index = BM25Index(db_path=str(tmp_path / "bm25.db"))
    svc._tag_store = VaultTagStore(db_path=str(tmp_path / "tags.db"))
    svc._tag_store_failed = False
    return svc, vault


def _write(vault: Path, name: str, body: str) -> Path:
    p = vault / "Notes" / name
    p.write_text(f"# Retro\n\n{body}\n")
    return p


def _tag(svc, rel: str, **over):
    svc._tag_store.upsert(_tags(file_path=rel, **over))


class TestBm25Reachability:
    def test_topic_word_absent_from_body_is_searchable(self, env):
        svc, vault = env
        p = _write(vault, "retro.md", "We reviewed candidates and pipeline throughput.")
        assert svc.bm25_index.search("hiring") == []
        _tag(svc, "Notes/retro.md")
        svc.index_all(skip_summaries=True)
        hits = svc.bm25_index.search("hiring")
        assert any(h["doc_id"].startswith(str(p.resolve())) for h in hits)

    def test_missing_store_row_indexes_without_phrase(self, env):
        svc, vault = env
        _write(vault, "retro.md", "We reviewed candidates.")
        svc.index_all(skip_summaries=True)
        assert svc.bm25_index.search("hiring") == []
        assert svc.bm25_index.search("candidates")


class TestReembedTrigger:
    def test_tuple_change_reembeds_unchanged_does_not(self, env):
        svc, vault = env
        _write(vault, "retro.md", "We reviewed candidates.")
        _tag(svc, "Notes/retro.md")
        assert svc.index_all(skip_summaries=True) == 1
        assert svc.index_all(skip_summaries=True) == 0  # nothing changed
        _tag(svc, "Notes/retro.md", topic="work/onboarding")
        assert svc.index_all(skip_summaries=True) == 1
        assert svc.index_all(skip_summaries=True) == 0
        assert len(svc.vector_store.calls) == 2

    def test_confidence_only_change_does_not_reembed(self, env):
        svc, vault = env
        _write(vault, "retro.md", "We reviewed candidates.")
        _tag(svc, "Notes/retro.md")
        svc.index_all(skip_summaries=True)
        _tag(svc, "Notes/retro.md", topic_conf=0.7, doc_type_conf=0.95)
        assert svc.index_all(skip_summaries=True) == 0

    def test_tag_appearing_after_index_reembeds_once(self, env):
        svc, vault = env
        _write(vault, "retro.md", "We reviewed candidates.")
        svc.index_all(skip_summaries=True)
        _tag(svc, "Notes/retro.md")
        assert svc.index_all(skip_summaries=True) == 1
        assert svc.index_all(skip_summaries=True) == 0

    def test_untagged_file_is_not_reembedded_across_runs(self, env):
        svc, vault = env
        _write(vault, "retro.md", "We reviewed candidates.")
        assert svc.index_all(skip_summaries=True) == 1
        assert svc.index_all(skip_summaries=True) == 0
        assert svc.index_all(skip_summaries=True) == 0
        assert len(svc.vector_store.calls) == 1

    def test_low_confidence_doc_type_is_treated_as_untagged(self, env):
        svc, vault = env
        _write(vault, "retro.md", "We reviewed candidates.")
        _tag(svc, "Notes/retro.md", doc_type_conf=0.3)
        svc.index_all(skip_summaries=True)
        assert svc.index_all(skip_summaries=True) == 0

    def test_deleted_file_drops_its_recorded_tuple(self, env):
        svc, vault = env
        p = _write(vault, "retro.md", "We reviewed candidates.")
        _tag(svc, "Notes/retro.md")
        svc.index_all(skip_summaries=True)
        p.unlink()
        svc.index_all(skip_summaries=True)
        state = svc._load_index_state()
        assert str(p) not in state[indexer_mod.TAG_STATE_KEY]
