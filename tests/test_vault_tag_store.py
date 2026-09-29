"""VaultTagStore: idempotency, facet queries, and pruning (synthetic rows only)."""
import os

import pytest

from api.services.vault_tag_store import TagRecord, VaultTagStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return VaultTagStore(str(tmp_path / "vault_tags.db"))


def _rec(path, **kw):
    base = dict(file_path=path, content_sha256="sha1", vocab_version="v1")
    base.update(kw)
    return TagRecord(**base)


def test_upsert_get_roundtrip_and_replace(store):
    store.upsert(_rec("A/one.md", doc_type="journal", doc_type_conf=0.9, topics_json='{"x": 1}'))
    got = store.get("A/one.md")
    assert got.doc_type == "journal" and got.doc_type_conf == 0.9 and got.topics_json == '{"x": 1}'
    store.upsert(_rec("A/one.md", doc_type="reference"))
    assert store.get("A/one.md").doc_type == "reference"
    assert store.get("missing.md") is None


def test_needs_tagging_matches_on_sha_and_vocab_not_mtime(store, tmp_path):
    f = tmp_path / "note.md"
    f.write_text("hello")
    store.upsert(_rec(str(f)))
    assert store.needs_tagging(str(f), "sha1", "v1") is False
    os.utime(f, (1_900_000_000, 1_900_000_000))  # new mtime, same content
    assert store.needs_tagging(str(f), "sha1", "v1") is False
    assert store.needs_tagging(str(f), "sha2", "v1") is True
    assert store.needs_tagging(str(f), "sha1", "v2") is True
    assert store.needs_tagging("other.md", "sha1", "v1") is True


def test_paths_matching_single_and_multiple_facets(store):
    store.upsert(_rec("a.md", doc_type="journal", domain="health", topic="health/sleep"))
    store.upsert(_rec("b.md", doc_type="journal", domain="work", topic="work/hiring"))
    store.upsert(_rec("c.md", doc_type="meeting_notes", domain="work", topic="work/hiring"))
    assert store.paths_matching(doc_type="journal") == ["a.md", "b.md"]
    assert store.paths_matching(doc_type="journal", domain="work") == ["b.md"]
    assert store.paths_matching(domain=["health", "work"], doc_type="meeting_notes") == ["c.md"]
    assert store.paths_matching(topic="work") == ["b.md", "c.md"]  # parent matches children
    assert store.paths_matching(topic="work/hiring", domain="health") == []
    assert store.paths_matching(doc_type=[]) == []
    with pytest.raises(ValueError):
        store.paths_matching(content_sha256="sha1")


def test_delete_missing(store):
    for p in ("a.md", "b.md", "c.md"):
        store.upsert(_rec(p))
    assert store.delete_missing(["a.md", "c.md", "not-stored.md"]) == 1
    assert store.get("b.md") is None and store.get("a.md") is not None
