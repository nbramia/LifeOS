"""API tests for the vault file browser and intake endpoints (/api/vault/tree, upload, jobs, download, move)."""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import vault_files
from api.services import vault_ingest
from config.settings import settings

pytestmark = pytest.mark.unit


class _SyncPool:
    """Runs submitted work immediately so a test sees the finished job."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / "Reference").mkdir()
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.json").write_text("{}")
    monkeypatch.setattr(settings, "vault_path", root)
    monkeypatch.setattr(settings, "vault_upload_ai", False)
    monkeypatch.setattr(settings, "vault_upload_max_mb", 100)
    monkeypatch.setattr(vault_ingest, "_POOL", _SyncPool())
    monkeypatch.setattr(vault_ingest, "_index_note", lambda path: True)
    monkeypatch.setattr(vault_files, "_journal_enabled", lambda: False)
    return root


@pytest.fixture
def client(vault):
    app = FastAPI()
    app.include_router(vault_files.router)
    return TestClient(app)


def _upload(client, folder, name, data, content_type="text/plain"):
    return client.post("/api/vault/upload", data={"folder": folder}, files={"files": (name, io.BytesIO(data), content_type)})


# --- tree ------------------------------------------------------------------

def test_tree_lists_folders_and_files_and_hides_dot_entries(client, vault):
    (vault / "Projects" / "plan.md").write_text("# plan")
    (vault / "Projects" / "scan.pdf").write_bytes(b"%PDF")
    (vault / "Projects" / "scan.pdf.md").write_text("note")
    root = client.get("/api/vault/tree").json()
    assert [f["name"] for f in root["folders"]] == ["Projects", "Reference"]
    projects = client.get("/api/vault/tree", params={"path": "Projects"}).json()
    kinds = {f["name"]: (f["kind"], f["sidecar"]) for f in projects["files"]}
    assert kinds["plan.md"] == ("note", None)
    assert kinds["scan.pdf"] == ("pdf", "Projects/scan.pdf.md")


@pytest.mark.parametrize("path", ["../x", "/etc", ".obsidian", "a/../../b"])
def test_tree_rejects_unsafe_paths(client, path):
    assert client.get("/api/vault/tree", params={"path": path}).status_code == 400


def test_tree_missing_folder_is_404(client):
    assert client.get("/api/vault/tree", params={"path": "Nope"}).status_code == 404


# --- upload ----------------------------------------------------------------

def test_upload_stores_the_file_and_writes_a_note(client, vault):
    body = b"Please sign the renewal by December 1."
    response = _upload(client, "Projects/Leases", "lease.txt", body)
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["ok"] and result["path"] == "Projects/Leases/lease.txt"
    assert (vault / "Projects" / "Leases" / "lease.txt").read_bytes() == body
    job = client.get(f"/api/vault/jobs/{result['job_id']}").json()
    assert job["status"] == "done" and job["note_path"] == "Projects/Leases/lease.txt.md"
    assert "abs_path" not in job
    note = (vault / "Projects" / "Leases" / "lease.txt.md").read_text()
    assert "Please sign the renewal" in note


def test_upload_to_the_vault_root(client, vault):
    assert _upload(client, "", "root.txt", b"x").json()["results"][0]["path"] == "root.txt"
    assert (vault / "root.txt").exists()


def test_duplicate_names_are_numbered_not_overwritten(client, vault):
    _upload(client, "Projects", "a.txt", b"first")
    second = _upload(client, "Projects", "a.txt", b"second").json()["results"][0]
    assert second["path"] == "Projects/a (1).txt"
    assert (vault / "Projects" / "a.txt").read_bytes() == b"first"


def test_program_files_are_refused(client, vault):
    result = _upload(client, "Projects", "setup.exe", b"MZ").json()["results"][0]
    assert not result["ok"] and ".exe" in result["error"]
    assert not (vault / "Projects" / "setup.exe").exists()


def test_a_file_name_cannot_climb_out_of_the_folder(client, vault):
    result = _upload(client, "Projects", "../../evil.txt", b"x").json()["results"][0]
    assert result["path"] == "Projects/evil.txt"
    assert not (vault.parent / "evil.txt").exists()


def test_oversized_files_are_refused_and_leave_no_partial(client, vault, monkeypatch):
    monkeypatch.setattr(settings, "vault_upload_max_mb", 1)
    result = _upload(client, "Projects", "big.bin", b"0" * (2 * 1024 * 1024)).json()["results"][0]
    assert not result["ok"] and "larger" in result["error"]
    assert list((vault / "Projects").iterdir()) == []


def test_empty_files_are_refused(client):
    assert not _upload(client, "Projects", "empty.txt", b"").json()["results"][0]["ok"]


@pytest.mark.parametrize("folder", ["../outside", "/etc", ".obsidian", "a/../../b"])
def test_upload_rejects_unsafe_folders(client, vault, folder):
    assert _upload(client, folder, "a.txt", b"x").status_code == 400


def test_upload_respects_the_reserved_journal_folder(client, monkeypatch):
    monkeypatch.setattr(vault_files, "_journal_enabled", lambda: True)
    assert _upload(client, "Personal/Journal", "a.txt", b"x").status_code == 400


def test_many_files_in_one_request_are_limited(client):
    files = [("files", (f"{i}.txt", io.BytesIO(b"x"), "text/plain")) for i in range(26)]
    assert client.post("/api/vault/upload", data={"folder": ""}, files=files).status_code == 400


def test_unknown_job_is_404(client):
    assert client.get("/api/vault/jobs/nope").status_code == 404


# --- download --------------------------------------------------------------

def test_download_is_an_attachment_with_safe_headers(client, vault):
    (vault / "Projects" / "a.html").write_text("<script>alert(1)</script>")
    response = client.get("/api/vault/download", params={"path": "Projects/a.html", "inline": "true"})
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_pdf_can_be_shown_inline(client, vault):
    (vault / "Projects" / "a.pdf").write_bytes(b"%PDF-1.4")
    response = client.get("/api/vault/download", params={"path": "Projects/a.pdf", "inline": "true"})
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" not in response.headers.get("content-disposition", "")


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", ".obsidian/app.json"])
def test_download_rejects_unsafe_paths(client, path):
    assert client.get("/api/vault/download", params={"path": path}).status_code == 400


def test_download_missing_file_is_404(client):
    assert client.get("/api/vault/download", params={"path": "Projects/none.txt"}).status_code == 404


# --- move ------------------------------------------------------------------

def test_move_takes_the_sidecar_note_along(client, vault):
    (vault / "Projects" / "a.pdf").write_bytes(b"%PDF")
    (vault / "Projects" / "a.pdf.md").write_text("note")
    response = client.post("/api/vault/move", json={"path": "Projects/a.pdf", "to_folder": "Reference"})
    assert response.status_code == 200
    assert response.json()["moved"] == ["Reference/a.pdf", "Reference/a.pdf.md"]
    assert not (vault / "Projects" / "a.pdf").exists() and (vault / "Reference" / "a.pdf.md").exists()


def test_move_never_overwrites(client, vault):
    (vault / "Projects" / "a.txt").write_text("new")
    (vault / "Reference" / "a.txt").write_text("old")
    assert client.post("/api/vault/move", json={"path": "Projects/a.txt", "to_folder": "Reference"}).status_code == 409
    assert (vault / "Reference" / "a.txt").read_text() == "old"


def test_move_to_the_same_folder_is_refused(client, vault):
    (vault / "Projects" / "a.txt").write_text("x")
    assert client.post("/api/vault/move", json={"path": "Projects/a.txt", "to_folder": "Projects"}).status_code == 400


@pytest.mark.parametrize("to_folder", ["../outside", "/etc", ".obsidian"])
def test_move_rejects_unsafe_destinations(client, vault, to_folder):
    (vault / "Projects" / "a.txt").write_text("x")
    assert client.post("/api/vault/move", json={"path": "Projects/a.txt", "to_folder": to_folder}).status_code == 400
    assert (vault / "Projects" / "a.txt").exists()


def test_move_missing_file_is_404(client):
    assert client.post("/api/vault/move", json={"path": "Projects/none.txt", "to_folder": "Reference"}).status_code == 404
