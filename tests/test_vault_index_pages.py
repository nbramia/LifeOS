"""Generated per-folder index pages."""
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from api.services.summarizer import SummaryTier, get_summary_tier
from api.services.vault_index_pages import INDEX_FOLDER, render_index_page, write_index_pages
from config.settings import settings

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 29)


def _touch(path: Path, text: str, ymd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    d = date.fromisoformat(ymd)
    ts = int(datetime(d.year, d.month, d.day, 12).timestamp())
    os.utime(path, (ts, ts))


def _no_summaries(paths):
    return {}


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    _touch(root / "Work" / "a.md", "a", "2026-09-28")
    _touch(root / "Work" / "b.md", "b", "2026-09-01")
    _touch(root / "Work" / "Sub" / "old.md", "c", "2026-01-01")
    _touch(root / "Personal" / "p.md", "p", "2026-09-20")
    _touch(root / ".obsidian" / "x.md", "x", "2026-09-20")
    _touch(root / "Work" / ".trash" / "t.md", "t", "2026-09-20")
    return root


def test_render_counts_order_and_frontmatter():
    entries = [
        {"relative_path": "W/a.md", "name": "a.md", "modified_date": "2026-09-28"},
        {"relative_path": "W/b.md", "name": "b.md", "modified_date": "2026-09-28"},
        {"relative_path": "W/old.md", "name": "old.md", "modified_date": "2026-01-01"},
    ]
    page = render_index_page("W", entries, {"W/a.md": "About  A\nthing"}, today=TODAY)
    assert page.startswith(
        "---\ntype: index\nsource: lifeos-index\ndate: 2026-09-28\ngenerated: true\n---\n# W index\n\n"
        "Part of [[Wiki/index|Wiki Index]] → Vault map.\n"
    )
    assert "- Notes: 3" in page
    assert "- Changed in the last 30 days: 2" in page
    bullets = [line for line in page.splitlines() if line.startswith("- [[")]
    assert bullets == [
        "- [[W/a|a]] (2026-09-28) — About A thing",
        "- [[W/b|b]] (2026-09-28)",
        "- [[W/old|old]] (2026-01-01)",
    ]


def test_render_caps_at_30_and_is_deterministic():
    entries = [
        {"relative_path": f"W/{i:02d}.md", "name": f"{i:02d}.md", "modified_date": "2026-09-01"}
        for i in range(45)
    ]
    page = render_index_page("W", entries, {}, today=TODAY)
    assert sum(1 for line in page.splitlines() if line.startswith("- [[")) == 30
    assert "- Notes: 45" in page
    assert page == render_index_page("W", entries, {}, today=TODAY)


def test_write_creates_folder_pages_and_excludes_hidden_and_index(vault):
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert (vault / INDEX_FOLDER).is_dir()
    assert sorted(p.name for p in (vault / INDEX_FOLDER).iterdir()) == ["Personal.md", "Wiki.md", "Work.md", "index.md"]
    assert (stats["written"], stats["unchanged"], stats["folders"]) == (4, 0, 3)
    work = (vault / INDEX_FOLDER / "Work.md").read_text()
    assert "- Notes: 3" in work
    assert ".trash" not in work and ".obsidian" not in work


def test_second_run_is_byte_identical_and_skips_write(vault):
    write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    target = vault / INDEX_FOLDER / "Work.md"
    before = target.stat().st_mtime_ns
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert stats["written"] == 0 and stats["unchanged"] == 4
    assert target.stat().st_mtime_ns == before


def test_map_notes_do_not_count_toward_wiki_page(tmp_path):
    root = tmp_path / "vault"
    _touch(root / "Wiki" / "n.md", "n", "2026-09-28")
    write_index_pages(root, today=TODAY, summary_lookup=_no_summaries)
    write_index_pages(root, today=TODAY, summary_lookup=_no_summaries)
    assert "- Notes: 1" in (root / INDEX_FOLDER / "Wiki.md").read_text()


def test_summaries_are_used_and_lookup_failure_falls_back_to_titles(vault):
    def lookup(paths):
        return {p: "Summary text" for p in paths if p.endswith("a.md")}

    write_index_pages(vault, today=TODAY, summary_lookup=lookup)
    work = (vault / INDEX_FOLDER / "Work.md").read_text()
    assert "[[Work/a|a]] (2026-09-28) — Summary text" in work
    assert "[[Work/b|b]] (2026-09-01)\n" in work

    def boom(paths):
        raise RuntimeError("index down")

    write_index_pages(vault, today=TODAY, summary_lookup=boom)
    assert "Summary text" not in (vault / INDEX_FOLDER / "Work.md").read_text()


def test_bm25_get_summaries_round_trip(tmp_path):
    from api.services.bm25_index import BM25Index

    idx = BM25Index(db_path=str(tmp_path / "bm25.db"))
    idx.add_document("/v/Work/a.md::summary", "Document summary for a.md: A note about: things", "a.md")
    idx.add_document("/v/Work/a.md_0", "body chunk", "a.md")
    assert idx.get_summaries(["/v/Work/a.md", "/v/Work/none.md"]) == {"/v/Work/a.md": "A note about: things"}


def test_index_folder_is_never_summarized(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", tmp_path)
    assert get_summary_tier(str(tmp_path / "Wiki" / "Vault Map" / "Work.md")) == SummaryTier.SKIP
    assert get_summary_tier(str(tmp_path / "Wiki" / "Vault Maps" / "x.md")) == SummaryTier.HIGH
    assert get_summary_tier(str(tmp_path / "LifeOS" / "note.md")) == SummaryTier.HIGH


def _set_mtime(path: Path, ts: int) -> None:
    os.utime(path, (ts, ts))


def test_selection_and_summaries_follow_full_mtime_not_filename(tmp_path):
    root = tmp_path / "vault"
    base = int(datetime(2026, 9, 28, 0, 0).timestamp())
    for i in range(31):
        f = root / "Work" / f"n{i:02d}.md"
        _touch(f, "x", "2026-09-28")
        _set_mtime(f, base + i * 60)  # n30 is newest, n00 oldest, all the same day
    seen = []

    def lookup(paths):
        seen.extend(paths)
        return {p: "sum " + Path(p).stem for p in paths}

    write_index_pages(root, today=TODAY, summary_lookup=lookup)
    page = (root / INDEX_FOLDER / "Work.md").read_text()
    bullets = [line for line in page.splitlines() if line.startswith("- [[")]
    assert len(bullets) == 30
    assert bullets[0].startswith("- [[Work/n30|n30]]") and "sum n30" in bullets[0]
    assert not any("n00" in b for b in bullets)
    assert len(seen) == 30 and all("n00" not in p for p in seen)
    assert all("— sum " in b for b in bullets)


def test_symlinked_index_dir_outside_vault_is_refused(tmp_path):
    root = tmp_path / "vault"
    _touch(root / "Work" / "a.md", "a", "2026-09-28")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Wiki").symlink_to(outside)
    stats = write_index_pages(root, today=TODAY, summary_lookup=_no_summaries)
    assert stats["written"] == 0 and stats["changed"] == []
    assert list(outside.iterdir()) == []


def test_stale_generated_pages_removed_but_handwritten_kept(vault):
    write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    (vault / "Personal").rename(vault / "Renamed")
    (vault / INDEX_FOLDER / "Custom.md").write_text("# mine\n", encoding="utf-8")
    (vault / INDEX_FOLDER / "Gone.md").write_text("---\nsource: other\n---\nx\n", encoding="utf-8")
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    names = sorted(p.name for p in (vault / INDEX_FOLDER).iterdir())
    assert "Personal.md" not in names and "Renamed.md" in names
    assert "index.md" in names
    assert "Custom.md" in names and "Gone.md" in names
    assert stats["removed"] == [str(vault / INDEX_FOLDER / "Personal.md")]


def test_nightly_script_generates_and_indexes_changed_pages(vault, monkeypatch):
    import sys

    import api.services.indexer as indexer_mod
    import api.services.vault_index_pages as pages_mod

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import sync_vault_reindex

    calls = {"index_file": [], "delete_file": [], "index_all": 0}

    class FakeIndexer:
        INDEX_STATE_FILE = "unused"

        def __init__(self, vault_path=None):
            pass

        def index_all(self, force=False, skip_summaries=False):
            calls["index_all"] += 1
            return 0

        def index_file(self, path, **kw):
            calls["index_file"].append((path, kw))

        def delete_file(self, path):
            calls["delete_file"].append(path)

    monkeypatch.setattr(indexer_mod, "IndexerService", FakeIndexer)
    monkeypatch.setattr(pages_mod, "_bm25_summary_lookup", _no_summaries)
    monkeypatch.setattr(settings, "vault_path", vault)

    result = sync_vault_reindex.sync_vault_reindex(dry_run=False)
    assert result["status"] == "success" and calls["index_all"] == 1
    indexed = sorted(Path(p).name for p, _ in calls["index_file"])
    assert indexed == ["Personal.md", "Wiki.md", "Work.md", "index.md"]
    assert all(kw.get("skip_summaries") is True for _, kw in calls["index_file"])

    calls["index_file"].clear()
    sync_vault_reindex.sync_vault_reindex(dry_run=False)
    assert calls["index_file"] == []  # unchanged pages are not re-indexed

    (vault / "Personal").rename(vault / "Renamed")
    sync_vault_reindex.sync_vault_reindex(dry_run=False)
    assert [Path(p).name for p in calls["delete_file"]] == ["Personal.md"]


def test_handwritten_file_with_page_name_survives_byte_for_byte(vault):
    index_dir = vault / INDEX_FOLDER
    index_dir.mkdir(parents=True)
    mine = b"---\nsource: handwritten\n---\nMy own Work page\n"
    (index_dir / "Work.md").write_bytes(mine)
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert (index_dir / "Work.md").read_bytes() == mine
    assert stats["skipped_collision"] == 1
    assert str(index_dir / "Work.md") not in stats["changed"]
    assert (index_dir / "Personal.md").exists()


def test_generated_page_is_indexed_without_any_summary_call(vault, tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from api.services.bm25_index import BM25Index
    from api.services.indexer import IndexerService

    monkeypatch.setattr(settings, "vault_path", vault)
    write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    page = vault / INDEX_FOLDER / "Work.md"

    indexer = IndexerService.__new__(IndexerService)
    indexer.vault_path = vault
    indexer._interaction_store = indexer._source_entity_store = indexer._entity_resolver = None
    indexer._tag_store, indexer._tag_store_failed = None, True
    indexer.vector_store = MagicMock()
    indexer.bm25_index = BM25Index(db_path=str(tmp_path / "bm25.db"))
    indexer._sync_people_to_v2 = MagicMock(return_value=set())

    summary = MagicMock(return_value=("never", True))
    monkeypatch.setattr("api.services.summarizer.generate_summary", summary)
    indexer.index_file(str(page), skip_summaries=False)

    indexer.vector_store.update_document.assert_called_once()
    assert summary.call_count == 0
    assert indexer.bm25_index.get_summaries([str(page.resolve())]) == {}


def test_pages_link_back_to_wiki_index_and_map_index_lists_folders(vault):
    write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    work = (vault / INDEX_FOLDER / "Work.md").read_text()
    assert "Part of [[Wiki/index|Wiki Index]] → Vault map." in work
    index = (vault / INDEX_FOLDER / "index.md").read_text()
    assert index.startswith("---\ntype: index\nsource: lifeos-index\ndate: 2026-09-28\ngenerated: true\n---\n")
    assert "- [[Wiki/Vault Map/Work|Work]] — 3 notes, 2 changed in 30 days" in index
    assert "- [[Wiki/Vault Map/Personal|Personal]] — 1 notes, 1 changed in 30 days" in index
    assert "[[Wiki/Vault Map/Wiki|Wiki]] — 0 notes, 0 changed in 30 days" in index


def test_handwritten_wiki_index_is_never_modified(vault):
    _touch(vault / "Wiki" / "index.md", "# Wiki\n", "2026-09-20")
    before = (vault / "Wiki" / "index.md").read_bytes()
    write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert (vault / "Wiki" / "index.md").read_bytes() == before
    assert "- Notes: 1" in (vault / INDEX_FOLDER / "Wiki.md").read_text()


def test_legacy_index_folder_generated_pages_removed_handwritten_kept(vault):
    legacy = vault / "LifeOS" / "Index"
    legacy.mkdir(parents=True)
    (legacy / "Work.md").write_text("---\ngenerated: true\nsource: lifeos-index\n---\nold\n", encoding="utf-8")
    (legacy / "Mine.md").write_text("---\nsource: handwritten\n---\nkeep\n", encoding="utf-8")
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert stats["removed_legacy"] == 1
    assert [p.name for p in legacy.iterdir()] == ["Mine.md"]
    assert (legacy / "Mine.md").read_text() == "---\nsource: handwritten\n---\nkeep\n"


def test_legacy_index_folder_removed_when_emptied(vault):
    legacy = vault / "LifeOS" / "Index"
    legacy.mkdir(parents=True)
    (legacy / "Work.md").write_text("---\nsource: lifeos-index\n---\nold\n", encoding="utf-8")
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert stats["removed_legacy"] == 1 and not legacy.exists()
    assert write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)["removed_legacy"] == 0


def test_unchanged_vault_writes_nothing_on_a_later_day(vault):
    write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    stats = write_index_pages(vault, today=TODAY + timedelta(days=1), summary_lookup=_no_summaries)
    assert stats["changed"] == [] and stats["written"] == 0 and stats["unchanged"] == 4
    work = (vault / INDEX_FOLDER / "Work.md").read_text()
    assert "\ndate: 2026-09-28\n" in work
    assert "\ndate: 2026-09-20\n" in (vault / INDEX_FOLDER / "Personal.md").read_text()


@pytest.fixture(autouse=True)
def _no_real_tag_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "api.services.vault_tag_store.get_vault_tags_db_path", lambda: str(tmp_path / "absent-tags.db")
    )
