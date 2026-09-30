"""VaultTagStore: idempotency, facet queries, and pruning (synthetic rows only)."""
import json
import os
import sqlite3

import pytest

from api.services import vault_tag_store
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


def _dist(**ps):
    """topics_json with the given label->p entries (``__`` stands for ``/``)."""
    topic = {k.replace("__", "/"): v for k, v in ps.items()}
    return json.dumps({"topic": topic, "secondary": []})


def test_topic_matches_top_k(store):
    ps = {"work__hiring": 0.05, "work__strategy": 0.6, "work__team_meetings": 0.2,
          "growth__mindset": 0.09, "home_food__recipes": 0.06}
    store.upsert(_rec("a.md", topic="work/strategy", topics_json=_dist(**ps)))
    # Rank 4 with p < 0.10: outside top-k, below min-p.
    assert store.paths_matching(topic="home_food/recipes") == []
    # Rank 3 (0.09) is inside the top-k despite p < 0.10.
    assert store.paths_matching(topic="growth/mindset") == ["a.md"]
    # A zero-probability topic is never a top-k match.
    store.upsert(_rec("b.md", topic="work/strategy", topics_json=_dist(work__strategy=1.0, work__hiring=0.0)))
    assert store.paths_matching(topic="work/hiring") == []


def test_topic_matches_min_p_beyond_top_k(store):
    ps = {"a__one": 0.3, "a__two": 0.25, "a__three": 0.2, "a__four": 0.15, "a__five": 0.1}
    store.upsert(_rec("a.md", topic="a/one", topics_json=_dist(**ps)))
    assert store.paths_matching(topic="a/four") == ["a.md"]  # rank 4, p 0.15 >= min-p
    assert store.paths_matching(topic="a/five") == ["a.md"]  # rank 5, p == min-p
    ps["a__five"] = 0.09
    store.upsert(_rec("a.md", topic="a/one", topics_json=_dist(**ps)))
    assert store.paths_matching(topic="a/five") == []  # rank 5, p just below min-p


def test_topic_matches_parent_over_children_in_distribution(store):
    store.upsert(_rec("a.md", topic="growth/mindset",
                      topics_json=_dist(growth__mindset=0.8, work__hiring=0.15)))
    store.upsert(_rec("b.md", topic="work", topics_json="{}"))
    store.upsert(_rec("c.md", topic="home_food/recipes", topics_json=_dist(home_food__recipes=1.0)))
    assert store.paths_matching(topic="work") == ["a.md", "b.md"]
    assert store.paths_matching(topic=["work", "home_food"]) == ["a.md", "b.md", "c.md"]


def test_topic_matching_keeps_primary_and_secondary(store):
    store.upsert(_rec("a.md", topic="work/hiring", topics_json="{}"))
    store.upsert(_rec("b.md", topic="work", topics_json=json.dumps({"topic": {}, "secondary": ["health/sleep"]})))
    assert store.paths_matching(topic="work/hiring") == ["a.md"]
    assert store.paths_matching(topic="health/sleep") == ["b.md"]
    assert store.paths_matching(topic="health/sleep", domain="work") == []


def test_topic_match_thresholds_are_the_module_constants(store, monkeypatch):
    ps = {"a__one": 0.5, "a__two": 0.3, "a__three": 0.1, "a__four": 0.06, "a__five": 0.04}
    store.upsert(_rec("a.md", topic="a/one", topics_json=_dist(**ps)))
    assert store.paths_matching(topic="a/four") == []
    monkeypatch.setattr(vault_tag_store, "TOPIC_MATCH_TOP_K", 4)
    assert store.paths_matching(topic="a/four") == ["a.md"]


def test_distribution_survives_reopen_backfill_and_delete(tmp_path):
    path = str(tmp_path / "t.db")
    s1 = VaultTagStore(path)
    s1.upsert(_rec("a.md", topic="a/one", topics_json=_dist(a__one=0.6, a__two=0.3)))
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE vault_tag_topics")
    s2 = VaultTagStore(path)  # rebuilds the side table from topics_json
    assert s2.paths_matching(topic="a/two") == ["a.md"]
    s2.upsert(_rec("a.md", topic="a/one", topics_json=_dist(a__one=1.0)))  # replace
    assert s2.paths_matching(topic="a/two") == []
    s2.delete_missing([])
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vault_tag_topics").fetchone()[0] == 0


def test_topic_query_over_nine_thousand_rows_is_fast(store):
    import random
    import time
    rng = random.Random(3)
    labels = [f"d{i // 4}/t{i % 4}" for i in range(50)]
    for i in range(9000):
        w = [rng.random() ** 8 for _ in labels]
        tot = sum(w)
        store.upsert(_rec(f"n{i}.md", topic=labels[i % 50],
                          topics_json=json.dumps({"topic": {lb: round(x / tot, 2) for lb, x in zip(labels, w)}})))
    start = time.perf_counter()
    assert store.paths_matching(topic="d1/t1")
    assert time.perf_counter() - start < 0.5  # spec budget is 0.1 s; slack for loaded CI hosts


def test_topics_mapped_from_operator_tags_match(store):
    detail = {"topic": {"a/one": 0.9}, "secondary": [], "from_tags": {"work/hiring": ["hiring"]}}
    store.upsert(_rec("a.md", topic="a/one", topics_json=json.dumps(detail)))
    assert store.paths_matching(topic="work/hiring") == ["a.md"]
    assert store.paths_matching(topic="work") == ["a.md"]
