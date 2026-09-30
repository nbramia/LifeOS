"""Calibration script: scoring and threshold choice on synthetic rows."""
import pytest

from scripts.jev_eval import calibrate_topic_match as cal

pytestmark = pytest.mark.unit

T = "work/hiring"


def _row(primary, **dist):
    return {"topic": primary, "secondary": set(), "dist": {k.replace("__", "/"): v for k, v in dist.items()}}


def test_matches_follows_the_store_rule():
    row = _row("a/x", a__x=0.6, b__y=0.2, work__hiring=0.05)
    assert cal.matches(row, T, k=3, min_p=0.10)  # rank 3
    assert not cal.matches(row, T, k=2, min_p=0.10)  # rank 3, below min-p
    assert cal.matches(row, T, k=2, min_p=0.05)  # at min-p
    assert not cal.matches(_row("a/x", a__x=1.0, work__hiring=0.0), T, k=5, min_p=0.0)
    assert cal.matches(_row(T), T, k=1, min_p=1.0)  # primary always matches


def test_score_and_choose_reach_the_recall_target():
    rows = [_row("a/x", a__x=0.97, work__hiring=0.03), _row("a/x", a__x=0.9, work__hiring=0.0),
            _row(T, work__hiring=1.0)]
    tag_topics = {"hiring": T}
    silver = {"hiring": {0, 1}}
    narrow = cal.score(rows, silver, 1, 0.05, tag_topics)
    assert (narrow["recall"], narrow["matched"]) == (0.0, 1)
    wide = cal.score(rows, silver, 2, 0.05, tag_topics)
    assert (wide["recall"], wide["matched"], wide["precision"]) == (0.5, 2, 1.0)
    assert cal.choose([narrow, wide], target=0.5) is wide
    assert cal.choose([narrow, wide], target=0.9) is None


def test_silver_labels_come_from_the_merged_taxonomy(tmp_path, monkeypatch):
    from api.services import vault_taxonomy as vt

    override = tmp_path / "override.yaml"
    override.write_text("tag_topics:\n  " + "zz-tag" + ": " + T + "\n")
    vt.reset_taxonomy_cache()
    monkeypatch.setattr(vt, "DEFAULT_OVERRIDE_PATH", override)
    try:
        mapping = cal.silver_tag_topics()
        assert mapping["zz-tag"] == T and "hiring" in mapping
    finally:
        vt.reset_taxonomy_cache()


def test_every_mapped_tag_with_its_topic_primary_has_recall_one(tmp_path):
    import json
    import sqlite3

    vault = tmp_path / "vault"
    vault.mkdir()
    tag_topics = {"alpha-tag": "a/x", "beta-tag": "b/y"}
    db = tmp_path / "tags.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE vault_tags (file_path TEXT, topic TEXT, topics_json TEXT, backend TEXT)")
    for i, (tag, topic) in enumerate(tag_topics.items()):
        (vault / f"{i}.md").write_text(f"---\ntags: [{tag}]\n---\nbody\n")
        conn.execute("INSERT INTO vault_tags VALUES (?, ?, ?, 'jev')",
                     (f"{i}.md", topic, json.dumps({"topic": {topic: 1.0}, "secondary": []})))
    conn.commit()
    conn.close()
    rows, silver = cal.load(db, vault, tag_topics)
    cell = cal.score(rows, silver, 1, 0.05, tag_topics)
    assert (cell["tagged"], cell["recall"], cell["precision"]) == (2, 1.0, 1.0)
