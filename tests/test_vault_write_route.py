"""Tests for the /api/vault/write endpoint (lifeos_vault_write MCP tool).

Closes the silent-failure mode where a #cloud agent task asked for a `.md`
deliverable but the MCP toolset had no write tool — the agent generated the
content, never wrote it, and the worker marked the task complete based on
remote_status:idle. With this endpoint the agent can produce the file
directly; the worker enforces non-empty results separately
(see test_empty_final_text_without_side_effect_marks_failed).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


@pytest.fixture
def app_and_vault(monkeypatch):
    """Spin up a minimal FastAPI app with vault.router mounted on a tmp vault.

    #682: monkeypatch the shared `settings` singleton's .vault_path attribute
    directly, rather than pointing LIFEOS_VAULT_PATH at the tmp dir and
    importlib.reload()-ing config.settings. A reload rebinds
    `config.settings.settings` to a brand-new object — every module that
    already did `from config.settings import settings` earlier in the
    process (api.services.agent_worker.worker, for one) keeps its old
    reference, so a *later* test that also does a fresh
    `from config.settings import settings` and monkeypatches THAT object is
    silently patching a different object than the one worker.py reads,
    while a module-level `settings.vault_path` read (e.g.
    tests/test_directory_resolver.py) sees whatever the reload left behind.
    Patching the attribute in place keeps identity intact for every
    consumer and monkeypatch reverts it automatically — no reload needed at
    all, and the route (api/routes/vault.py) already does a normal
    `from config.settings import settings` so it picks up the patched value
    with no importlib.reload(vault) required either.
    """
    from config.settings import settings
    tmpdir = tempfile.mkdtemp(prefix="lifeos-vault-test-")
    monkeypatch.setattr(settings, "vault_path", Path(tmpdir))
    from api.routes import vault
    app = FastAPI()
    app.include_router(vault.router)
    yield TestClient(app), Path(tmpdir)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

def test_create_writes_file_with_parents(app_and_vault):
    c, vault = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "Inbox/news monitoring prompt.md",
        "content": "# Prompt\n\nbody",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["mode"] == "create"
    assert (vault / "Inbox" / "news monitoring prompt.md").read_text() == "# Prompt\n\nbody"


def test_create_fails_on_existing_file(app_and_vault):
    c, vault = app_and_vault
    (vault / "x.md").write_text("old")
    r = c.post("/api/vault/write", json={"path": "x.md", "content": "new"})
    assert r.status_code == 409
    assert (vault / "x.md").read_text() == "old"


def test_overwrite_replaces_content(app_and_vault):
    c, vault = app_and_vault
    (vault / "x.md").write_text("old")
    r = c.post("/api/vault/write", json={
        "path": "x.md", "content": "new", "mode": "overwrite",
    })
    assert r.status_code == 200
    assert (vault / "x.md").read_text() == "new"


def test_append_adds_to_end(app_and_vault):
    c, vault = app_and_vault
    (vault / "x.md").write_text("one\n")
    r = c.post("/api/vault/write", json={
        "path": "x.md", "content": "two\n", "mode": "append",
    })
    assert r.status_code == 200
    assert (vault / "x.md").read_text() == "one\ntwo\n"


def test_append_creates_file_when_missing(app_and_vault):
    c, vault = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "new.md", "content": "first line", "mode": "append",
    })
    assert r.status_code == 200
    assert (vault / "new.md").read_text() == "first line"


@pytest.mark.parametrize("bad_path,expected_fragment", [
    ("../escape.md", "must not contain `..`"),
    ("/etc/passwd", "must be vault-relative"),
    ("~/escape.md", "must be vault-relative"),
    ("foo/../bar.md", "must not contain `..`"),
])
def test_rejects_unsafe_paths(app_and_vault, bad_path, expected_fragment):
    c, _ = app_and_vault
    r = c.post("/api/vault/write", json={"path": bad_path, "content": "x"})
    assert r.status_code == 400, r.text
    assert expected_fragment in r.json()["detail"]


def test_writes_unicode_correctly(app_and_vault):
    c, vault = app_and_vault
    content = "héllo — résumé 🚀\n"
    r = c.post("/api/vault/write", json={
        "path": "u.md", "content": content,
    })
    assert r.status_code == 200
    assert (vault / "u.md").read_text(encoding="utf-8") == content
    # bytes_written should reflect UTF-8 byte count, not character count
    assert r.json()["bytes_written"] == len(content.encode("utf-8"))


def test_invalid_mode_rejected_by_pydantic(app_and_vault):
    c, _ = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "x.md", "content": "x", "mode": "delete",
    })
    assert r.status_code == 422  # Pydantic Literal validation


# ---------------------------------------------------------------------------
# Personal/Journal/ is reserved (#659) — those files are generated by
# gsheet_sync.py and drive journal_trends.py's analytics; a free-form write
# there would be clobbered by the next sync and corrupt the scalars it reads.
# Blocked here, for every caller, rather than left to a persona's prompt
# discipline (e.g. the journal persona, config/personas/journal.md).
#
# Since #769 the reservation only applies when this install actually
# generates Personal/Journal/ files: either the journal persona is enabled
# (a `journal` entry in settings.telegram_bots, i.e. TELEGRAM_JOURNAL_BOT_TOKEN
# is set) or gsheet_sync.py's own `journal_notes` output is enabled in
# config/gsheet_sync.yaml — the two are independent (api/routes/vault.py's
# `_journal_enabled`). Tests that exercise the reservation itself explicitly
# enable it below (`_enable_journal_persona`) as a regression guard.
#
# The "not enabled" tests pin BOTH signals to an empty state explicitly
# (`_disable_journal_persona`) rather than relying on the ambient test
# process having neither configured: this suite's autouse
# `_no_local_telegram_bots_override` fixture (tests/conftest.py) already
# keeps a real config/telegram_bots.local.json out of the run, but nothing
# clears a real TELEGRAM_JOURNAL_BOT_TOKEN set in the actual server's `.env`
# (settings.py's registry loader reads `.env` directly, not just
# os.environ), and gsheet_sync.CONFIG_PATH is an absolute path to the real
# repo's config/gsheet_sync.yaml regardless of cwd — on the maintainer's own
# server both are plausibly already configured. Codex review of #769 caught
# that these tests would give a false pass in this worktree while actually
# failing on that server.
# ---------------------------------------------------------------------------

def _enable_journal_persona(monkeypatch):
    """Make settings.telegram_bots include the shipped `journal` entry, i.e.
    simulate an install with TELEGRAM_JOURNAL_BOT_TOKEN configured (the
    maintainer's install). config/telegram_bots.json already ships a
    `journal` entry naming this exact env var. Hermetic regardless of a real
    ambient `.env`: the registry loader merges `{**dotenv_values(env_file),
    **os.environ}`, and monkeypatch.setenv always wins that merge."""
    monkeypatch.setenv("TELEGRAM_JOURNAL_BOT_TOKEN", "test-token")


def _disable_journal_persona(monkeypatch, tmp_path):
    """Force both signals _journal_enabled() checks to empty, regardless of
    what the actual environment/config on this machine has configured (see
    the module comment above)."""
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", tmp_path / "no-registry.json")
    monkeypatch.setattr("api.services.gsheet_sync.CONFIG_PATH", tmp_path / "no-gsheet-sync.yaml")


@pytest.mark.parametrize("bad_path", [
    "Personal/Journal/2026-08-23.md",
    "Personal/Journal/nested/note.md",
])
def test_rejects_writes_into_reserved_journal_dir(app_and_vault, bad_path, monkeypatch):
    """Regression guard (#769): with the journal persona enabled, behavior is
    byte-for-byte what it was before the reservation became conditional —
    asserted against the exact message text, not just a substring (Codex
    review of #769: a substring check wouldn't catch the message itself
    regressing)."""
    _enable_journal_persona(monkeypatch)
    c, vault = app_and_vault
    r = c.post("/api/vault/write", json={"path": bad_path, "content": "x"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == (
        "path must not target Personal/Journal/ (reserved for the generated "
        "daily journal; capture free-form notes under Personal/Log/ instead)"
    )
    assert not (vault / "Personal" / "Journal").exists()


def test_rejects_reserved_journal_dir_regardless_of_mode(app_and_vault, monkeypatch):
    _enable_journal_persona(monkeypatch)
    c, _ = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "Personal/Journal/2026-08-23.md", "content": "x", "mode": "append",
    })
    assert r.status_code == 400, r.text


def test_allows_writes_into_sibling_personal_log_dir(app_and_vault, monkeypatch):
    # Personal/Log/ (the journal persona's capture target) is unaffected —
    # only the Personal/Journal/ subtree itself is reserved. True regardless
    # of whether the journal persona is enabled; enable it here to exercise
    # the stricter (default/maintainer) state.
    _enable_journal_persona(monkeypatch)
    c, vault = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "Personal/Log/2026-08-23.md", "content": "x",
    })
    assert r.status_code == 200, r.text
    assert (vault / "Personal" / "Log" / "2026-08-23.md").read_text() == "x"


def test_does_not_reject_journal_named_sibling_file(app_and_vault, monkeypatch):
    # A file that merely starts with "Journal" alongside the reserved dir
    # (not inside it) must not be caught by a naive string-prefix check.
    _enable_journal_persona(monkeypatch)
    c, vault = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "Personal/Journal-notes.md", "content": "x",
    })
    assert r.status_code == 200, r.text
    assert (vault / "Personal" / "Journal-notes.md").read_text() == "x"


def test_allows_writes_into_journal_dir_when_persona_not_enabled(app_and_vault, tmp_path, monkeypatch):
    """#769: when neither pipeline that generates Personal/Journal/ files is
    configured on this install (no journal entry in the Telegram bot
    registry, and no gsheet_sync journal_notes output — pinned explicitly
    here, matching a fresh clone), a write under Personal/Journal/ is
    allowed like any other vault path — there is no generated file here for
    the reservation to protect."""
    _disable_journal_persona(monkeypatch, tmp_path)
    c, vault = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "Personal/Journal/2026-08-23.md", "content": "x",
    })
    assert r.status_code == 200, r.text
    assert (vault / "Personal" / "Journal" / "2026-08-23.md").read_text() == "x"


def test_other_validation_still_applies_when_journal_persona_not_enabled(app_and_vault, tmp_path, monkeypatch):
    """Disabling the reservation must not disable the other path-safety
    checks (traversal, absolute paths) — those are unconditional."""
    _disable_journal_persona(monkeypatch, tmp_path)
    c, _ = app_and_vault
    r = c.post("/api/vault/write", json={
        "path": "Personal/Journal/../../escape.md", "content": "x",
    })
    assert r.status_code == 400, r.text
    assert "must not contain `..`" in r.json()["detail"]


class TestJournalEnabledSignal:
    """Direct, hermetic unit coverage of _journal_enabled()'s two
    independent signals (#769 Codex review) — the route-level tests above
    only exercise the Telegram-bot side for "enabled" and pin both signals
    off for "not enabled"; these confirm the gsheet_sync side works too,
    including its OR with the Telegram signal."""

    def test_true_via_gsheet_journal_notes_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", tmp_path / "no-registry.json")
        config_path = tmp_path / "gsheet_sync.yaml"
        config_path.write_text("""
sync_enabled: true
sheets:
  - sheet_id: "test123"
    outputs:
      journal_notes:
        enabled: true
""")
        monkeypatch.setattr("api.services.gsheet_sync.CONFIG_PATH", config_path)
        from api.routes import vault
        assert vault._journal_enabled() is True

    def test_false_when_gsheet_configured_but_journal_notes_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", tmp_path / "no-registry.json")
        config_path = tmp_path / "gsheet_sync.yaml"
        config_path.write_text("""
sync_enabled: true
sheets:
  - sheet_id: "test123"
    outputs:
      journal_notes:
        enabled: false
""")
        monkeypatch.setattr("api.services.gsheet_sync.CONFIG_PATH", config_path)
        from api.routes import vault
        assert vault._journal_enabled() is False

    def test_false_when_neither_signal_present(self, tmp_path, monkeypatch):
        _disable_journal_persona(monkeypatch, tmp_path)
        from api.routes import vault
        assert vault._journal_enabled() is False
