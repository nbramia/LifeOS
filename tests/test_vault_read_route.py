"""GET /api/vault/read: full note content by vault-relative path, confined to the vault."""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.vault import router
from config.settings import settings

pytestmark = pytest.mark.unit

NOTE = "---\ntags: [alpha, beta]\n---\n# Synthetic Note\n\nBody line one.\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Work").mkdir(parents=True)
    note = root / "Work" / "Example.md"
    note.write_text(NOTE, encoding="utf-8")
    os.utime(note, (1_700_000_000, 1_700_000_000))
    (root / "Plain.md").write_text("no frontmatter\n", encoding="utf-8")
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (tmp_path / "outside.md").write_text("OUTSIDE-SECRET", encoding="utf-8")
    (tmp_path / "outside_dir").mkdir()
    (tmp_path / "outside_dir" / "x.md").write_text("OUTSIDE-SECRET", encoding="utf-8")
    (root / "escape.md").symlink_to(tmp_path / "outside.md")
    (root / "escape_dir").symlink_to(tmp_path / "outside_dir")
    (root / "Work" / "inside_link.md").symlink_to(root / "Plain.md")
    monkeypatch.setattr(settings, "vault_path", root)
    return root


@pytest.fixture
def client(vault):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _read(client, path):
    return client.get("/api/vault/read", params={"path": path})


def test_returns_content_matching_disk_and_list_style_metadata(client, vault):
    resp = _read(client, "Work/Example.md")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == (vault / "Work" / "Example.md").read_text(encoding="utf-8")
    assert body["relative_path"] == "Work/Example.md"
    assert body["modified_date"].startswith("2023-11-")
    assert body["note_type"] == "Work"
    assert body["tags"] == ["alpha", "beta"]


def test_note_without_frontmatter_has_empty_tags(client):
    body = _read(client, "Plain.md").json()
    assert body["tags"] == []
    assert body["note_type"] == "Other"
    assert body["content"] == "no frontmatter\n"


def test_symlink_staying_inside_the_vault_is_readable(client):
    assert _read(client, "Work/inside_link.md").json()["content"] == "no frontmatter\n"


@pytest.mark.parametrize("path", ["../outside.md", "Work/../../outside.md", "a/../../outside.md"])
def test_traversal_is_rejected_without_content(client, path):
    resp = _read(client, path)
    assert resp.status_code == 400
    assert "OUTSIDE-SECRET" not in resp.text


@pytest.mark.parametrize("path", ["/etc/passwd", "~/notes.md"])
def test_absolute_paths_are_rejected(client, path):
    assert _read(client, path).status_code == 400


def test_absolute_path_of_a_file_outside_the_vault_is_rejected(client, vault):
    resp = _read(client, str(vault.parent / "outside.md"))
    assert resp.status_code == 400
    assert "OUTSIDE-SECRET" not in resp.text


@pytest.mark.parametrize("path", ["escape.md", "escape_dir/x.md"])
def test_symlinks_escaping_the_vault_are_rejected(client, path):
    resp = _read(client, path)
    assert resp.status_code == 400
    assert "OUTSIDE-SECRET" not in resp.text


@pytest.mark.parametrize("path", ["Missing.md", "Work", ".", ".obsidian/app.json"])
def test_missing_directory_and_hidden_paths_are_404(client, path):
    resp = _read(client, path)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "note not found in vault"}


def test_missing_path_parameter_is_422(client):
    assert client.get("/api/vault/read").status_code == 422
