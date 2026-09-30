"""Vault Map domain and topic pages (synthetic vault, tag store, and taxonomy)."""
import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from api.services.vault_index_pages import INDEX_FOLDER, write_index_pages
from api.services.vault_tag_store import TagRecord, VaultTagStore
from api.services.vault_taxonomy import Taxonomy

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 29)
TAXONOMY = Taxonomy(
    version="t",
    doc_types=("note",),
    domains=("work", "money_admin", "health"),
    topics={"work/hiring": "", "work/strategy": "", "money_admin/budgeting": "", "health/sleep": ""},
    project_sources=(),
    vocab_version="v",
)


def _touch(path: Path, ymd: str, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    d = date.fromisoformat(ymd)
    ts = int(datetime(d.year, d.month, d.day, 12).timestamp())
    os.utime(path, (ts, ts))


def _rec(path, **kw):
    return TagRecord(file_path=path, content_sha256="s", vocab_version="v", **kw)


def _dist(**probs):
    return json.dumps({"topic": probs})


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "vault"
    _touch(root / "Work" / "hire1.md", "2026-09-28")
    _touch(root / "Work" / "hire2.md", "2026-09-20")
    _touch(root / "Work" / "strat.md", "2026-09-10")
    _touch(root / "Personal" / "budget.md", "2026-09-15")
    _touch(root / "Personal" / "domain_only.md", "2026-09-01")
    _touch(root / "Personal" / "secret.md", "2026-09-29")
    _touch(root / "Personal" / "gone_topic.md", "2026-09-02")
    store = VaultTagStore(str(tmp_path / "tags.db"))
    store.upsert(_rec("Work/hire1.md", domain="work", topic="work/hiring", backend="jev"))
    # primary is strategy, but the distribution also ranks hiring in the top 3
    store.upsert(_rec("Work/strat.md", domain="work", topic="work/strategy", backend="jev",
                      topics_json=_dist(**{"work/strategy": 0.6, "work/hiring": 0.3})))
    store.upsert(_rec("Work/hire2.md", domain="work", topic="work/hiring", backend="jev"))
    store.upsert(_rec("Personal/budget.md", domain="money_admin", topic="money_admin/budgeting", backend="jev"))
    store.upsert(_rec("Personal/domain_only.md", domain="money_admin", backend="jev"))
    store.upsert(_rec("Personal/secret.md", domain="work", topic="work/hiring", sensitivity="restricted"))
    store.upsert(_rec("Personal/gone_topic.md", domain="health", topic="health/sleep", backend="jev"))
    return root, store


def _run(root, store, summaries=lambda paths: {}):
    return write_index_pages(root, TODAY, summaries, tag_store=store, taxonomy=TAXONOMY)


def _tree(root):
    d = root / INDEX_FOLDER
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*.md"))}


def test_index_lists_domains_first_then_folders(env):
    root, store = env
    _run(root, store)
    text = (root / INDEX_FOLDER / "index.md").read_text()
    assert text.index("## By domain") < text.index("## By folder")
    # the restricted note is not counted
    assert "- [[Wiki/Vault Map/Domains/work|work]] — 3 notes" in text
    assert "- [[Wiki/Vault Map/Domains/money_admin|money admin]] — 2 notes" in text
    assert "[[Wiki/Vault Map/Personal|Personal]]" in text and "[[Wiki/Vault Map/Work|Work]]" in text


def test_domain_page_lists_topics_with_counts_and_domain_only(env):
    root, store = env
    _run(root, store)
    text = (root / INDEX_FOLDER / "Domains" / "money_admin.md").read_text()
    assert "- [[Wiki/Vault Map/Topics/money_admin--budgeting|budgeting]] — 1 notes" in text
    assert "Notes classified only at the domain level: 1" in text
    work = (root / INDEX_FOLDER / "Domains" / "work.md").read_text()
    assert "[[Wiki/Vault Map/Topics/work--hiring|hiring]] — 3 notes" in work


def test_topic_page_membership_matches_search_facet(env):
    root, store = env
    _run(root, store)
    page = (root / INDEX_FOLDER / "Topics" / "work--hiring.md").read_text()
    facet = set(store.paths_matching(topic=["work/hiring"])) - {"Personal/secret.md"}
    listed = {ln.split("[[")[1].split("|")[0] + ".md" for ln in page.splitlines() if ln.startswith("- [[")}
    assert listed == facet == {"Work/hire1.md", "Work/hire2.md", "Work/strat.md"}
    assert "Total: 3" in page


def test_restricted_notes_never_on_topic_or_domain_pages(env):
    root, store = env
    _run(root, store)
    for rel, body in _tree(root).items():
        if rel.startswith(("Topics/", "Domains/")):
            assert b"secret" not in body
    assert b"secret" in _tree(root)["Personal.md"]  # still on its folder page


def test_topic_page_caps_at_30_newest_and_shows_summaries(tmp_path):
    root = tmp_path / "vault"
    store = VaultTagStore(str(tmp_path / "tags.db"))
    for i in range(35):
        _touch(root / "Work" / f"n{i:02d}.md", "2026-09-%02d" % (i % 28 + 1))
        store.upsert(_rec(f"Work/n{i:02d}.md", domain="work", topic="work/hiring", backend="jev"))

    def lookup(paths):
        return {paths[0]: "  One   line\nsummary "}

    write_index_pages(root, TODAY, lookup, tag_store=store, taxonomy=TAXONOMY)
    page = (root / INDEX_FOLDER / "Topics" / "work--hiring.md").read_text()
    assert page.count("\n- [[") == 30 and "Total: 35" in page
    assert " — One line summary" in page
    dates = [ln.split("(")[1][:10] for ln in page.splitlines() if ln.startswith("- [[")]
    assert dates == sorted(dates, reverse=True)


def test_frontmatter_backlink_and_only_populated_topics(env):
    root, store = env
    _run(root, store)
    tree = _tree(root)
    assert "Topics/work--hiring.md" in tree and "Topics/work--strategy.md" in tree
    page = tree["Topics/work--hiring.md"].decode()
    assert page.startswith("---\ntype: index\nsource: lifeos-index\ndate: 2026-09-28\ngenerated: true\n---\n")
    assert "Part of [[Wiki/index|Wiki Index]] → Vault map." in page
    store.delete_missing(set(store.paths_matching()) - {"Work/strat.md"})
    _run(root, store)
    assert "Topics/work--strategy.md" not in _tree(root)


def test_rerun_is_byte_identical_and_writes_nothing_on_a_later_day(env):
    root, store = env
    _run(root, store)
    before = _tree(root)
    mtimes = {p: p.stat().st_mtime_ns for p in (root / INDEX_FOLDER).rglob("*.md")}
    later = write_index_pages(root, date(2026, 10, 30), lambda p: {}, tag_store=store, taxonomy=TAXONOMY)
    # only the folder pages and the map index carry a today-relative count; domain and topic pages do not
    assert {Path(c).name for c in later["changed"]} <= {"Work.md", "Personal.md", "index.md"}
    after = _tree(root)
    assert {k: v for k, v in after.items() if "/" in k} == {
        k: v for k, v in before.items() if "/" in k
    }
    same_day = write_index_pages(root, date(2026, 10, 30), lambda p: {}, tag_store=store, taxonomy=TAXONOMY)
    assert same_day["written"] == 0 and _tree(root) == after
    assert all(
        p.stat().st_mtime_ns == mtimes[p] for p in mtimes if p.parent.name in ("Domains", "Topics")
    )


def test_topic_that_becomes_empty_has_its_page_removed(env):
    root, store = env
    _run(root, store)
    sleep = root / INDEX_FOLDER / "Topics" / "health--sleep.md"
    assert sleep.exists()
    store.delete_missing(set(store.paths_matching()) - {"Personal/gone_topic.md"})
    result = _run(root, store)
    assert not sleep.exists() and str(sleep) in result["removed"]
    assert not (root / INDEX_FOLDER / "Domains" / "health.md").exists()


def test_handwritten_collisions_and_files_are_untouched(env):
    root, store = env
    topics = root / INDEX_FOLDER / "Topics"
    topics.mkdir(parents=True)
    (topics / "work--hiring.md").write_text("mine\n")
    (topics / "notes.md").write_text("mine too\n")
    (root / INDEX_FOLDER / "Domains").mkdir()
    (root / INDEX_FOLDER / "Domains" / "old.md").write_text("---\nsource: lifeos-index\n---\n")
    (root / INDEX_FOLDER / "Domains" / "keep.md").write_text("handwritten\n")
    result = _run(root, store)
    assert (topics / "work--hiring.md").read_text() == "mine\n"
    assert (topics / "notes.md").read_text() == "mine too\n"
    assert result["skipped_collision"] == 1
    assert not (root / INDEX_FOLDER / "Domains" / "old.md").exists()
    assert (root / INDEX_FOLDER / "Domains" / "keep.md").read_text() == "handwritten\n"


def test_map_folder_notes_excluded_from_counts_and_summaries(env):
    root, store = env
    _run(root, store)
    asked = []
    _run(root, store, lambda paths: asked.extend(paths) or {})
    assert asked and not any(INDEX_FOLDER in p for p in asked)
    store.upsert(_rec(f"{INDEX_FOLDER}/Topics/extra.md", domain="work", topic="work/hiring", backend="jev"))
    _touch(root / INDEX_FOLDER / "Topics" / "extra.md", "2026-09-29")
    _run(root, store)
    assert b"extra" not in (root / INDEX_FOLDER / "Topics" / "work--hiring.md").read_bytes()


@pytest.mark.parametrize("populated", [False, True])
def test_missing_or_empty_tag_store_writes_no_domain_or_topic_pages(env, tmp_path, populated):
    root, store = env
    if populated:
        _run(root, store)
    empty = VaultTagStore(str(tmp_path / "empty.db"))
    result = _run(root, empty)
    text = (root / INDEX_FOLDER / "index.md").read_text()
    assert "No tags stored yet" in text and "## By folder" in text
    assert not list((root / INDEX_FOLDER).rglob("Topics/*.md"))
    assert not list((root / INDEX_FOLDER).rglob("Domains/*.md"))
    assert (root / INDEX_FOLDER / "Work.md").exists()
    assert result["domain_pages"] == result["topic_pages"] == 0


def test_page_count_for_a_50_topic_taxonomy(tmp_path):
    root = tmp_path / "vault"
    store = VaultTagStore(str(tmp_path / "tags.db"))
    domains = tuple(f"d{i}" for i in range(8))
    topics = {f"d{i % 8}/t{i}": "" for i in range(50)}
    for i, t in enumerate(topics):
        _touch(root / "F" / f"n{i}.md", "2026-09-01")
        store.upsert(_rec(f"F/n{i}.md", domain=t.split("/")[0], topic=t, backend="jev"))
    tax = Taxonomy("t", ("note",), domains, topics, (), "v")
    result = write_index_pages(root, TODAY, lambda p: {}, tag_store=store, taxonomy=tax)
    assert (result["domain_pages"], result["topic_pages"]) == (8, 50)
    assert len(list((root / INDEX_FOLDER).rglob("*.md"))) == 8 + 50 + 1 + 2  # domain + topic + index + folder pages (F, Wiki)


def test_unchanged_tags_and_notes_write_nothing_on_the_next_day(env):
    root, store = env
    write_index_pages(root, TODAY, lambda p: {}, tag_store=store, taxonomy=TAXONOMY)
    before = _tree(root)
    mtimes = {p: p.stat().st_mtime_ns for p in (root / INDEX_FOLDER).rglob("*.md")}
    result = write_index_pages(root, date(2026, 9, 30), lambda p: {}, tag_store=store, taxonomy=TAXONOMY)
    assert result["written"] == 0 and result["changed"] == [] and result["removed"] == []
    assert _tree(root) == before
    assert {p: p.stat().st_mtime_ns for p in (root / INDEX_FOLDER).rglob("*.md")} == mtimes
    generated = b"".join(v for k, v in before.items() if k.startswith(("Topics/", "Domains/")))
    assert b"changed in" not in generated
