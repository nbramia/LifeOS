"""Generated per-folder index pages."""
import os
from datetime import date, datetime
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
        {"relative_path": "W/old.md", "name": "old.md", "modified_date": "2026-01-01"},
        {"relative_path": "W/b.md", "name": "b.md", "modified_date": "2026-09-28"},
        {"relative_path": "W/a.md", "name": "a.md", "modified_date": "2026-09-28"},
    ]
    page = render_index_page("W", entries, {"W/a.md": "About  A\nthing"}, today=TODAY)
    assert page.startswith("---\ngenerated: true\nsource: lifeos-index\n---\n")
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
    assert page == render_index_page("W", list(reversed(entries)), {}, today=TODAY)


def test_write_creates_folder_pages_and_excludes_hidden_and_index(vault):
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert (vault / INDEX_FOLDER).is_dir()
    assert sorted(p.name for p in (vault / INDEX_FOLDER).iterdir()) == ["LifeOS.md", "Personal.md", "Work.md"]
    assert stats == {"written": 3, "unchanged": 0, "folders": 3}
    work = (vault / INDEX_FOLDER / "Work.md").read_text()
    assert "- Notes: 3" in work
    assert ".trash" not in work and ".obsidian" not in work


def test_second_run_is_byte_identical_and_skips_write(vault):
    write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    target = vault / INDEX_FOLDER / "Work.md"
    before = target.stat().st_mtime_ns
    stats = write_index_pages(vault, today=TODAY, summary_lookup=_no_summaries)
    assert stats["written"] == 0 and stats["unchanged"] == 3
    assert target.stat().st_mtime_ns == before


def test_index_notes_do_not_count_toward_lifeos_page(tmp_path):
    root = tmp_path / "vault"
    _touch(root / "LifeOS" / "n.md", "n", "2026-09-28")
    write_index_pages(root, today=TODAY, summary_lookup=_no_summaries)
    write_index_pages(root, today=TODAY, summary_lookup=_no_summaries)
    assert "- Notes: 1" in (root / INDEX_FOLDER / "LifeOS.md").read_text()


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
    assert get_summary_tier(str(tmp_path / "LifeOS" / "Index" / "Work.md")) == SummaryTier.SKIP
    assert get_summary_tier(str(tmp_path / "LifeOS" / "Indexes" / "x.md")) == SummaryTier.HIGH
    assert get_summary_tier(str(tmp_path / "LifeOS" / "note.md")) == SummaryTier.HIGH
