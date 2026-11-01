"""sync_vault_tag: incremental tagging, pruning, gating, stats-line contract (synthetic vault, mocked Jev)."""
import json
import re
from unittest.mock import patch

import pytest

from api.services.jev_client import JevClient, JevError
from api.services.vault_tag_store import VaultTagStore
from api.services.vault_taxonomy import load_taxonomy
from config.settings import settings
from scripts import sync_vault_tag

pytestmark = pytest.mark.unit

TX = load_taxonomy()
TOPIC = list(TX.topics)[0]
STAT_KEYS = {"tagged", "skipped_unchanged", "skipped_not_allowlisted", "code_only",
             "restricted", "low_confidence", "errors", "vocab_version", "updated", "processed"}


def _answers(topic_conf=0.9):
    def choice(probs):
        best = max(probs, key=probs.get)
        return {"choice": best, "probabilities": probs, "confidence": probs[best]}
    return {
        "doc_type": choice({TX.doc_types[0]: 0.8, TX.doc_types[1]: 0.2}),
        "domain": choice({TX.domains[0]: 0.7, TX.domains[1]: 0.3}),
        "topic": choice({TOPIC: topic_conf, list(TX.topics)[1]: 1 - topic_conf}),
        "project": choice({"none": 1.0}),
        "actionability": {"score": 1.0},
        "has_decision": {"noul": 0.1},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    for d in ("Notes", "Other", ".obsidian", ".trash", "Wiki/Vault Map"):
        (vault / d).mkdir(parents=True)
    (vault / "Notes" / "a.md").write_text("# A\nalpha\n")
    (vault / "Notes" / "b.md").write_text("# B\nbeta\n")
    (vault / "Other" / "c.md").write_text("# C\ngamma\n")
    (vault / ".obsidian" / "x.md").write_text("x")
    (vault / ".trash" / "y.md").write_text("y")
    (vault / "Wiki" / "Vault Map" / "index.md").write_text("generated")
    db = str(tmp_path / "tags.db")
    monkeypatch.setattr(settings, "vault_path", str(vault))
    monkeypatch.setattr(settings, "jev_vault_tagging", "on")
    monkeypatch.setattr(settings, "jev_vault_tag_paths", "Notes")
    monkeypatch.setattr(settings, "typesafe_api_key", "k")
    monkeypatch.setattr("api.services.vault_tag_store.get_vault_tags_db_path", lambda: db)
    return vault, VaultTagStore(db_path=db)


def _run(dry_run=False):
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        stats = sync_vault_tag.sync_vault_tag(dry_run=dry_run)
    return stats, ask


def _stats_lines(out):
    return re.findall(r"^SYNC_STATS:(\{.*\})$", out, re.M)


def test_first_run_tags_allowlisted_and_counts_others(env):
    _, store = env
    stats, ask = _run()
    assert stats["tagged"] == 3 and stats["errors"] == 0
    assert stats["skipped_not_allowlisted"] == 1 and stats["code_only"] == 1
    assert ask.call_count == 2
    assert store.get("Notes/a.md").backend == "jev"
    assert store.get("Other/c.md").backend == "code"
    assert store.get(".obsidian/x.md") is None
    assert store.get(".trash/y.md") is None
    assert store.get("Wiki/Vault Map/index.md") is None


def test_restricted_note_is_code_only_and_counted(env):
    vault, store = env
    (vault / "Notes" / "s.md").write_text("---\ntags: [therapy]\n---\nprivate\n")
    stats, ask = _run()
    assert stats["restricted"] == 1 and stats["code_only"] == 2
    assert ask.call_count == 2
    rec = store.get("Notes/s.md")
    assert rec.backend == "code" and rec.sensitivity == "restricted"


def test_unchanged_files_are_skipped(env):
    _run()
    stats, ask = _run()
    assert stats["tagged"] == 0 and stats["skipped_unchanged"] == 3
    ask.assert_not_called()


def test_changed_file_is_retagged(env):
    vault, _ = env
    _run()
    (vault / "Notes" / "a.md").write_text("# A\nchanged\n")
    stats, ask = _run()
    assert stats["tagged"] == 1 and stats["skipped_unchanged"] == 2
    assert ask.call_count == 1


def test_stale_vocab_version_is_retagged(env):
    _, store = env
    _run()
    rec = store.get("Notes/a.md")
    rec.vocab_version = "stale"
    store.upsert(rec)
    stats, _ = _run()
    assert stats["tagged"] == 1 and stats["skipped_unchanged"] == 2


def test_deleted_file_row_is_pruned(env):
    vault, store = env
    _run()
    (vault / "Notes" / "b.md").unlink()
    _run()
    assert store.get("Notes/b.md") is None
    assert store.get("Notes/a.md") is not None


def test_low_confidence_counted(env):
    with patch.object(JevClient, "ask", return_value=_answers(topic_conf=0.55)):
        stats = sync_vault_tag.sync_vault_tag(dry_run=False)
    assert stats["low_confidence"] == 2


def test_per_file_jev_errors_are_counted_and_exit_zero(env, capsys):
    _, store = env
    with patch.object(JevClient, "ask", side_effect=JevError("boom")):
        code = sync_vault_tag.main(["--execute"])
    stats = json.loads(_stats_lines(capsys.readouterr().out)[0])
    assert code == 0 and stats["errors"] == 2 and stats["tagged"] == 1
    assert store.get("Notes/a.md") is None  # retried on the next run


@pytest.mark.parametrize("mode,key", [("off", "k"), ("on", "")])
def test_unconfigured_is_a_clean_skip(env, capsys, monkeypatch, mode, key):
    _, store = env
    monkeypatch.setattr(settings, "jev_vault_tagging", mode)
    monkeypatch.setattr(settings, "typesafe_api_key", key)
    with patch.object(JevClient, "ask") as ask:
        code = sync_vault_tag.main(["--execute"])
    out = capsys.readouterr().out
    assert code == 0 and "SYNC_SKIPPED:" in out
    stats = json.loads(_stats_lines(out)[0])
    assert stats["tagged"] == 0 and stats["errors"] == 0
    ask.assert_not_called()
    assert store.get("Notes/a.md") is None


def test_exactly_one_stats_line_with_documented_keys(env, capsys):
    with patch.object(JevClient, "ask", return_value=_answers()):
        assert sync_vault_tag.main(["--execute"]) == 0
    lines = _stats_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert set(json.loads(lines[0])) == STAT_KEYS


def test_dry_run_sends_nothing_and_writes_nothing(env, capsys):
    _, store = env
    with patch.object(JevClient, "ask") as ask:
        code = sync_vault_tag.main(["--dry-run"])
        stats = sync_vault_tag.sync_vault_tag(dry_run=True)
    assert code == 0 and len(_stats_lines(capsys.readouterr().out)) == 1
    ask.assert_not_called()
    assert store.get("Notes/a.md") is None
    assert stats["would_tag"] == 3 and stats["tagged"] == 0


def test_restricted_tag_setting_change_retags_code_only_note(env, monkeypatch):
    vault, store = env
    (vault / "Notes" / "f.md").write_text("---\ntags: [finance]\n---\nbudget\n")
    (vault / "Notes" / "p.md").write_text("---\ntags: [private]\n---\nnotes\n")
    _run()
    for rel in ("Notes/f.md", "Notes/p.md"):
        rec = store.get(rel)
        assert rec.backend == "code" and rec.sensitivity == "restricted"

    monkeypatch.setattr(settings, "jev_vault_restricted_tags", "private,confidential")
    stats, ask = _run()
    assert ask.call_count == 1 and stats["tagged"] == 1
    rec = store.get("Notes/f.md")
    assert rec.backend == "jev" and rec.sensitivity == "private"
    assert store.get("Notes/p.md").backend == "code"

    stats, ask = _run()
    assert not ask.called and stats["tagged"] == 0


def test_folded_restricted_tag_is_not_sent_by_nightly_retag(env, monkeypatch):
    vault, store = env
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", "straße")
    (vault / "Notes" / "f.md").write_text("---\ntags: [STRASSE]\n---\nx\n#straße/child\n")
    _run()
    stats, ask = _run()
    assert ask.call_count == 0 and store.get("Notes/f.md").backend == "code"


@pytest.mark.parametrize("tag", ["1-1", "_private", "équipe"])
def test_retag_of_a_previously_sent_row_does_not_send_a_restricted_inline_tag(env, monkeypatch, tag):
    vault, store = env
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", tag)
    _run()
    assert store.get("Notes/a.md").backend == "jev"
    (vault / "Notes" / "a.md").write_text(f"# A\nchanged #{tag}\n")
    stats, ask = _run()
    assert stats["restricted"] == 1 and ask.call_count == 0
    rec = store.get("Notes/a.md")
    assert rec.backend == "code" and rec.sensitivity == "restricted"


def test_tagged_notes_count_as_orchestrator_yield(env, capsys):
    """The orchestrator's yield reads processed/created/updated; tagged notes
    must land in one of them or a night of real tagging looks like a no-op."""
    from scripts.run_all_syncs import _parse_sync_output, _run_yield

    with patch.object(JevClient, "ask", return_value=_answers()):
        assert sync_vault_tag.main(["--execute"]) == 0
    parsed = _parse_sync_output(capsys.readouterr().out)
    assert parsed["tagged"] == 3
    assert parsed["updated"] == 3
    assert _run_yield(parsed) == 3


def test_quiet_night_still_counts_notes_examined(env, capsys):
    """Nothing changed tonight: the examined notes keep the run from reading
    as a zero-yield night."""
    from scripts.run_all_syncs import _parse_sync_output, _run_yield

    with patch.object(JevClient, "ask", return_value=_answers()):
        sync_vault_tag.main(["--execute"])
        capsys.readouterr()
        sync_vault_tag.main(["--execute"])
    parsed = _parse_sync_output(capsys.readouterr().out)
    assert parsed["tagged"] == 0 and parsed["updated"] == 0
    assert parsed["processed"] == 3
    assert _run_yield(parsed) == 3


def test_processed_is_tagged_plus_unchanged(env, capsys):
    vault, _ = env
    with patch.object(JevClient, "ask", return_value=_answers()):
        sync_vault_tag.main(["--execute"])
        capsys.readouterr()
        (vault / "Notes" / "a.md").write_text("# A\nchanged\n")
        sync_vault_tag.main(["--execute"])
    stats = json.loads(_stats_lines(capsys.readouterr().out)[0])
    assert stats["tagged"] == 1 and stats["skipped_unchanged"] == 2
    assert stats["processed"] == 3 and stats["updated"] == 1


def test_failed_notes_are_not_counted_as_examined(env, capsys):
    with patch.object(JevClient, "ask", side_effect=JevError("boom")):
        sync_vault_tag.main(["--execute"])
    stats = json.loads(_stats_lines(capsys.readouterr().out)[0])
    assert stats["errors"] == 2
    assert stats["processed"] == stats["tagged"] + stats["skipped_unchanged"] == 1


def test_empty_vault_has_zero_yield(env, capsys):
    from scripts.run_all_syncs import _parse_sync_output, _run_yield

    vault, _ = env
    for note in vault.rglob("*.md"):
        note.unlink()
    with patch.object(JevClient, "ask", return_value=_answers()):
        assert sync_vault_tag.main(["--execute"]) == 0
    assert _run_yield(_parse_sync_output(capsys.readouterr().out)) == 0


def test_unreadable_vault_has_zero_yield(env, capsys, monkeypatch, tmp_path):
    from scripts.run_all_syncs import _parse_sync_output, _run_yield

    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "missing"))
    with patch.object(JevClient, "ask", return_value=_answers()):
        sync_vault_tag.main(["--execute"])
    parsed = _parse_sync_output(capsys.readouterr().out)
    assert parsed["errors"] == 1
    assert _run_yield(parsed) == 0
