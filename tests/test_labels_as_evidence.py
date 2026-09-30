"""The operator's `type:` and tags as evidence in vault tagging (synthetic notes, mocked Jev)."""
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.services import indexer as indexer_module
from api.services.jev_client import JevClient
from api.services.people import people_from_tags
from api.services.vault_tag_store import TagRecord, VaultTagStore
from api.services.vault_tagger import VaultTagger
from api.services.vault_taxonomy import (
    DEFAULT_TAXONOMY_PATH,
    TaxonomyError,
    load_taxonomy,
    reset_taxonomy_cache,
)
from api.services.vectorstore import VectorStore, is_people_tag
from config.settings import settings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "jev_eval"))
import silver_labels  # noqa: E402

pytestmark = pytest.mark.unit

TX = load_taxonomy(DEFAULT_TAXONOMY_PATH)
ONE_ON_ONE = TX.tag_topics["1-1"]
OTHER_DOMAIN_TOPIC = "health/fitness"


def _choice(probs):
    best = max(probs, key=probs.get)
    return {"choice": best, "probabilities": probs, "confidence": probs[best]}


def _answers(topic_probs=None, doc_type="reference"):
    return {
        "doc_type": _choice({doc_type: 0.9, "log": 0.1}),
        "domain": _choice({"work": 0.9, "health": 0.1}),
        "topic": _choice(topic_probs or {"work/operations": 0.9, "work/meetings": 0.1}),
        "project": _choice({"none": 1.0}),
        "actionability": {"score": 1.0},
        "has_decision": {"noul": 0.1},
    }


@pytest.fixture(autouse=True)
def _jev_on(monkeypatch):
    monkeypatch.setattr(settings, "jev_vault_tagging", "on")
    monkeypatch.setattr(settings, "jev_vault_tag_paths", "*")
    monkeypatch.setattr(settings, "typesafe_api_key", "k")
    reset_taxonomy_cache()
    yield
    reset_taxonomy_cache()


def _tag(tmp_path, text, answers=None, store=None):
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True, exist_ok=True)
    note = vault / "Notes" / "n.md"
    note.write_text(text)
    tagger = VaultTagger(store=store, client=JevClient(api_key="k"), taxonomy=TX, vault_root=vault)
    with patch.object(JevClient, "ask", return_value=answers or _answers()) as ask:
        return tagger.tag_file(note), ask


# --- type: -> doc_type -------------------------------------------------------


def test_mapped_type_sets_doc_type_whatever_jev_returns(tmp_path):
    rec, ask = _tag(tmp_path, "---\ntype: Meeting\n---\nBody\n", _answers(doc_type="log"))
    assert (rec.doc_type, rec.doc_type_conf, rec.doc_type_source) == ("meeting_notes", 1.0, "frontmatter")
    assert rec.backend == "jev"
    questions = ask.call_args.args[1]
    assert "doc_type" not in questions and {"domain", "topic", "project"} <= set(questions)
    assert rec.domain == "work"


def test_unmapped_or_absent_type_leaves_doc_type_to_jev(tmp_path):
    for text in ("---\ntype: mystery\n---\nBody\n", "Body\n"):
        rec, ask = _tag(tmp_path, text, _answers(doc_type="log"))
        assert (rec.doc_type, rec.doc_type_source) == ("log", "jev")
        assert "doc_type" in ask.call_args.args[1]


def test_code_only_record_is_sourced_code_and_type_still_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "jev_vault_tagging", "off")
    rec, ask = _tag(tmp_path, "---\ntype: journal\n---\nBody\n")
    assert not ask.called and rec.backend == "code"
    assert (rec.doc_type, rec.doc_type_source) == ("journal", "frontmatter")
    rec, _ = _tag(tmp_path, "Body\n")
    assert (rec.doc_type, rec.doc_type_source) == (None, "code")


def test_restricted_note_takes_no_label_evidence(tmp_path):
    rec, _ = _tag(tmp_path, "---\ntype: journal\ntags: [therapy, hiring]\n---\nBody\n")
    assert rec.sensitivity == "restricted" and rec.doc_type is None and rec.topic is None


# --- tag -> topic ------------------------------------------------------------


def test_mapped_tag_lands_in_stored_topics_and_facet_search(tmp_path):
    store = VaultTagStore(str(tmp_path / "tags.db"))
    rec, _ = _tag(tmp_path, "---\ntags: ['1-1']\n---\nBody\n", store=store)
    assert rec.topic == "work/operations"  # Jev's confident full topic stays primary
    detail = json.loads(rec.topics_json)
    assert ONE_ON_ONE in detail["secondary"]
    assert detail["from_tags"] == {ONE_ON_ONE: ["1-1"]}
    store.upsert(rec)
    assert store.paths_matching(topic=[ONE_ON_ONE]) == [rec.file_path]
    assert store.paths_matching(topic=["work"]) == [rec.file_path]
    assert store.paths_matching(topic=["health"]) == []


def test_inline_tag_counts_and_two_tags_to_one_topic_are_recorded_once(tmp_path):
    rec, _ = _tag(tmp_path, "---\ntags: ['1-1']\n---\nBody #1-1 #hiring\n")
    detail = json.loads(rec.topics_json)
    assert set(detail["from_tags"]) == {ONE_ON_ONE, "work/hiring"}
    assert {ONE_ON_ONE, "work/hiring"} <= set(detail["secondary"])


def test_parent_only_primary_is_promoted_to_the_mapped_topic(tmp_path):
    weak = {"work/operations": 0.4, "work/meetings": 0.3, "health/fitness": 0.3}
    rec, _ = _tag(tmp_path, "---\ntags: [hiring]\n---\nBody\n", _answers(weak))
    assert (rec.topic, rec.topic_conf) == ("work/hiring", 1.0)
    # a parent-only primary in another domain is left alone
    weak = {"health/fitness": 0.4, "health/sleep": 0.3, "health/medical": 0.3}
    rec, _ = _tag(tmp_path, "---\ntags: [hiring]\n---\nBody\n", _answers(weak))
    assert rec.topic == "health" and "work/hiring" in json.loads(rec.topics_json)["secondary"]


def test_mapped_tag_equal_to_jev_primary_gets_full_confidence(tmp_path):
    rec, _ = _tag(tmp_path, "---\ntags: [strategy]\n---\nBody\n", _answers({"work/strategy": 0.7, "work/hiring": 0.3}))
    assert (rec.topic, rec.topic_conf) == ("work/strategy", 1.0)


def test_tag_matching_is_case_and_hash_insensitive(tmp_path):
    rec, _ = _tag(tmp_path, "---\ntags: ['#Recipes']\n---\nBody\n")
    assert TX.tag_topics["recipes"] in json.loads(rec.topics_json)["secondary"]


# --- taxonomy maps -----------------------------------------------------------

BASE = """
version: "1"
doc_types: [journal, reference]
domains: [work, health]
project_sources: []
topics:
  - name: work/hiring
  - name: health/fitness
type_doc_types:
  Diary: journal
tag_topics:
  "#Hiring": work/hiring
"""


def _load(tmp_path, base=BASE, local=None):
    (tmp_path / "base.yaml").write_text(base)
    if local is not None:
        (tmp_path / "local.yaml").write_text(local)
        return load_taxonomy(tmp_path / "base.yaml", tmp_path / "local.yaml")
    return load_taxonomy(tmp_path / "base.yaml")


def test_committed_maps_are_generic_and_valid():
    assert TX.tag_topics == {
        "1-1": "work/management", "hiring": "work/hiring", "strategy": "work/strategy",
        "recipes": "home_food/cooking", "coding": "tech_projects/software_development",
        "ideas": "growth/ideas",
    }
    assert TX.type_doc_types["meeting"] == "meeting_notes"
    assert all(v in TX.doc_types for v in TX.type_doc_types.values())


COMMITTED_OVERRIDE_BASE = BASE + "  gym: health/fitness\n"


def test_committed_file_alone_loads_with_every_target_valid():
    tx = load_taxonomy(DEFAULT_TAXONOMY_PATH)
    assert all(t in tx.topics for t in tx.tag_topics.values())
    assert all(t in tx.doc_types for t in tx.type_doc_types.values())
    assert tx.type_doc_types["transcript"] == "meeting_notes"


def test_override_removing_a_committed_target_drops_the_mapping_with_a_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        tx = _load(tmp_path, COMMITTED_OVERRIDE_BASE, "remove:\n  topics: [health/fitness]\n")
    assert "gym" not in tx.tag_topics and "hiring" in tx.tag_topics
    assert "health/fitness" in caplog.text
    reset_taxonomy_cache()
    same = _load(tmp_path, COMMITTED_OVERRIDE_BASE.replace("  gym: health/fitness\n", ""), "remove:\n  topics: [health/fitness]\n")
    assert same.vocab_version == tx.vocab_version


def test_override_can_repoint_a_dropped_mapping_to_its_own_topic(tmp_path):
    tx = _load(tmp_path, COMMITTED_OVERRIDE_BASE, """
domains: [work, health]
topics:
  - name: health/workouts
remove:
  topics: [health/fitness]
tag_topics:
  gym: health/workouts
""")
    assert tx.tag_topics["gym"] == "health/workouts"


def test_override_mapping_to_an_undeclared_topic_raises(tmp_path):
    with pytest.raises(TaxonomyError):
        _load(tmp_path, BASE, "tag_topics:\n  gym: health/nowhere\n")


def test_map_keys_fold_and_targets_are_validated(tmp_path):
    tx = _load(tmp_path)
    assert tx.type_doc_types == {"diary": "journal"} and tx.tag_topics == {"hiring": "work/hiring"}
    for bad in (BASE.replace("work/hiring\n", "work/nope\n").replace("- name: work/nope", "- name: work/hiring"),
                BASE.replace("Diary: journal", "Diary: nonesuch")):
        reset_taxonomy_cache()
        with pytest.raises(TaxonomyError):
            _load(tmp_path, bad)
    reset_taxonomy_cache()
    with pytest.raises(TaxonomyError):
        _load(tmp_path, BASE, "tag_topics:\n  gym: health/nowhere\n")


def test_local_override_adds_replaces_and_removes_by_key(tmp_path):
    tx = _load(tmp_path, BASE, """
tag_topics:
  hiring: health/fitness
  gym: health/fitness
type_doc_types:
  memo: reference
remove:
  type_doc_types: [Diary]
""")
    assert tx.tag_topics == {"hiring": "health/fitness", "gym": "health/fitness"}
    assert tx.type_doc_types == {"memo": "reference"}
    reset_taxonomy_cache()
    assert _load(tmp_path, BASE, "remove:\n  tag_topics: ['#HIRING']\n").tag_topics == {}


def test_vocab_version_changes_with_either_map(tmp_path):
    base = _load(tmp_path).vocab_version
    versions = {base}
    for name, text in (
        ("tag", BASE.replace('"#Hiring": work/hiring', '"#Hiring": health/fitness')),
        ("type", BASE.replace("Diary: journal", "Diary: reference")),
        ("tag_extra", BASE + "  gym: health/fitness\n"),
    ):
        reset_taxonomy_cache()
        versions.add(_load(tmp_path, text).vocab_version)
    assert len(versions) == 4
    reset_taxonomy_cache()
    reordered = BASE.replace('  Diary: journal', '  DIARY: journal')  # same folded content
    assert _load(tmp_path, reordered).vocab_version == base


# --- store migration ---------------------------------------------------------


def test_store_adds_doc_type_source_to_an_existing_table_idempotently(tmp_path):
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE vault_tags (
            file_path TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL, vocab_version TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '', tagged_at TEXT NOT NULL DEFAULT '',
            doc_type TEXT, doc_type_conf REAL, domain TEXT, domain_conf REAL,
            topic TEXT, topic_conf REAL, topics_json TEXT NOT NULL DEFAULT '{}',
            project TEXT, project_conf REAL, actionability REAL, has_decision REAL,
            sensitivity TEXT NOT NULL DEFAULT 'private', backend TEXT NOT NULL DEFAULT 'code');
        INSERT INTO vault_tags (file_path, content_sha256, vocab_version, doc_type) VALUES ('a.md', 's', 'v', 'log');
    """)
    conn.commit()
    conn.close()
    store = VaultTagStore(db)
    VaultTagStore(db)  # second open must not fail
    assert store.get("a.md").doc_type_source == "jev"
    store.upsert(TagRecord("b.md", "s", "v", doc_type="log", doc_type_source="frontmatter"))
    assert store.get("b.md").doc_type_source == "frontmatter"


# --- people/<slug> tags ------------------------------------------------------


def test_people_tags_resolve_slugs_to_names():
    assert people_from_tags(["people/jane-doe", "#People/sam_rivera", "work", "people/a/b", "people/"]) == [
        "Jane Doe",
        "Sam Rivera",
    ]
    with patch("api.services.people.resolve_person_name", side_effect=lambda n: {"Jane Doe": "Jane D."}.get(n, n)):
        assert people_from_tags(["people/jane-doe"]) == ["Jane D."]


def test_people_tags_are_not_tag_vocabulary():
    assert is_people_tag("people/jane-doe") and is_people_tag("#People/x") and not is_people_tag("peoples/x")
    vs = VectorStore.__new__(VectorStore)
    vs._collection = MagicMock()
    vs._embedding_service = MagicMock()
    vs._embedding_service.embed_texts.return_value = [[0.0]]
    vs.add_document(
        [{"content": "c", "chunk_index": 0}],
        {"file_path": "/a.md", "file_name": "a.md", "tags": ["people/jane-doe", "plain"]},
    )
    meta = vs._collection.add.call_args.kwargs["metadatas"][0]
    assert "tag:plain" in meta and "tag:people/jane-doe" not in meta


def test_indexer_routes_people_tags_to_people_and_out_of_tags(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("---\ntags: [people/jane-doe, plain]\n---\n# T\nBody text.\n")
    svc = indexer_module.IndexerService.__new__(indexer_module.IndexerService)
    svc.vault_path = vault
    svc.vector_store = MagicMock()
    svc.bm25_index = MagicMock()
    svc._tag_for = lambda path: None
    monkeypatch.setattr(indexer_module, "HAS_V2_PEOPLE", False)
    svc.index_file(str(note), skip_summaries=True)
    metadata = svc.vector_store.update_document.call_args.args[1]
    assert "Jane Doe" in metadata["people"] and metadata["tags"] == ["plain"]


# --- silver script -----------------------------------------------------------


def test_silver_script_reports_aggregate_agreement(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = VaultTagStore(str(tmp_path / "tags.db"))
    notes = {
        "a.md": ("---\ntype: meeting\ntags: [hiring]\n---\nx\n", "meeting_notes", "work/hiring"),
        "b.md": ("---\ntype: meeting\n---\nx\n", "log", None),
        "c.md": ("---\ntags: [hiring]\n---\nx\n", None, "work/operations"),
        "d.md": ("---\ntype: journal\ntags: [therapy, hiring]\n---\nx\n", "journal", "work/hiring"),
    }
    for name, (text, doc_type, topic) in notes.items():
        (vault / name).write_text(text)
        store.upsert(TagRecord(name, "s", "v", doc_type=doc_type, topic=topic,
                               sensitivity="restricted" if name == "d.md" else "private"))
    out = silver_labels.measure(vault, store, TX)
    assert out["doc_type"] == {"n": 2, "agreement": 0.5}
    assert out["tags"] == {"hiring": {"n": 2, "agreement": 0.5}}
    assert "a.md" not in json.dumps(out)
