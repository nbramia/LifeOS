"""Tests for the /api/vault/write endpoint (lifeos_vault_write MCP tool).

Closes the silent-failure mode where a #cloud agent task asked for a `.md`
deliverable but the MCP toolset had no write tool — the agent generated the
content, never wrote it, and the worker marked the task complete based on
remote_status:idle. With this endpoint the agent can produce the file
directly; the worker enforces non-empty results separately
(see test_empty_final_text_without_side_effect_marks_failed).
"""
from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_and_vault(monkeypatch):
    """Spin up a minimal FastAPI app with vault.router mounted on a tmp vault.

    settings is module-scoped so we reload it after pointing LIFEOS_VAULT_PATH
    at the tmp dir; otherwise the route picks up the user's real vault.
    """
    tmpdir = tempfile.mkdtemp(prefix="lifeos-vault-test-")
    monkeypatch.setenv("LIFEOS_VAULT_PATH", tmpdir)
    import config.settings
    importlib.reload(config.settings)
    from api.routes import vault
    importlib.reload(vault)
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
