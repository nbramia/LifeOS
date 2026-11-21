"""LIFEOS_VAULT_EXCLUDE_DIRS keeps tooling notes out of the vault search index."""
from unittest.mock import MagicMock

import pytest

from api.services.indexer import IndexerService, excluded_prefixes, is_excluded_path
from config.settings import settings

pytestmark = pytest.mark.unit


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for rel in (
        "Projects/plan.md",
        "skills/skill-one.md",
        "skills-extra/keep.md",
        "Meta/templates/t.md",
        "Meta/notes.md",
        ".claude/agent.md",
        "CLAUDE.md",
        "Reference/ideas.md",
    ):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# note\n")
    monkeypatch.setattr(settings, "vault_path", root)
    return root


def _exclude(monkeypatch, value):
    monkeypatch.setattr(settings, "vault_exclude_dirs", value)


def test_nothing_is_excluded_by_default(vault, monkeypatch):
    _exclude(monkeypatch, "")
    assert excluded_prefixes() == []
    assert not is_excluded_path(vault, vault / "skills" / "skill-one.md")


def test_folders_files_and_nested_paths_match_from_the_vault_root(vault, monkeypatch):
    _exclude(monkeypatch, "skills, .claude , Meta/templates, CLAUDE.md")
    assert is_excluded_path(vault, vault / "skills" / "skill-one.md")
    assert is_excluded_path(vault, vault / ".claude" / "agent.md")
    assert is_excluded_path(vault, vault / "Meta" / "templates" / "t.md")
    assert is_excluded_path(vault, vault / "CLAUDE.md")


def test_similar_names_and_siblings_are_not_matched(vault, monkeypatch):
    _exclude(monkeypatch, "skills, Meta/templates, CLAUDE.md")
    assert not is_excluded_path(vault, vault / "skills-extra" / "keep.md")
    assert not is_excluded_path(vault, vault / "Meta" / "notes.md")
    assert not is_excluded_path(vault, vault / "Projects" / "plan.md")
    assert not is_excluded_path(vault, vault / "Projects" / "CLAUDE.md")


def test_matching_ignores_case_and_backslashes(vault, monkeypatch):
    _exclude(monkeypatch, "SKILLS, meta\\TEMPLATES")
    assert is_excluded_path(vault, vault / "skills" / "skill-one.md")
    assert is_excluded_path(vault, vault / "Meta" / "templates" / "t.md")


def test_paths_outside_the_vault_are_never_excluded(vault, monkeypatch, tmp_path):
    _exclude(monkeypatch, "skills")
    assert not is_excluded_path(vault, tmp_path / "skills" / "x.md")


def _indexer(vault, state):
    indexer = IndexerService.__new__(IndexerService)
    indexer.vault_path = vault
    indexer._load_index_state = lambda: state
    indexer._save_index_state = lambda s: None
    indexer._tag_for = lambda path: None
    indexer._retry_failed_summaries = lambda: 0
    indexer.index_file = MagicMock(return_value=None)
    indexer.delete_file = MagicMock()
    return indexer


def test_full_scan_skips_excluded_notes(vault, monkeypatch):
    _exclude(monkeypatch, "skills,.claude,Meta/templates,CLAUDE.md")
    indexer = _indexer(vault, {})
    count = indexer.index_all(skip_summaries=True)
    indexed = sorted(call.args[0].replace(str(vault) + "/", "") for call in indexer.index_file.call_args_list)
    assert indexed == ["Meta/notes.md", "Projects/plan.md", "Reference/ideas.md", "skills-extra/keep.md"]
    assert count == 4


def test_excluding_a_folder_removes_its_notes_from_the_index(vault, monkeypatch):
    _exclude(monkeypatch, "skills")
    stale = str(vault / "skills" / "skill-one.md")
    indexer = _indexer(vault, {stale: 1.0})
    indexer.index_all(skip_summaries=True)
    indexer.delete_file.assert_called_once_with(stale)


def test_single_file_indexing_skips_excluded_notes(vault, monkeypatch):
    _exclude(monkeypatch, "skills")
    indexer = IndexerService.__new__(IndexerService)
    indexer.vault_path = vault
    indexer.vector_store = MagicMock()
    indexer.bm25_index = MagicMock()
    assert indexer.index_file(str(vault / "skills" / "skill-one.md")) is None
    indexer.vector_store.update_document.assert_not_called()
    indexer.bm25_index.add_document.assert_not_called()
