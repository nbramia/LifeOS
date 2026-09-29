"""list_vault tool, GET /api/vault/list, and the shared listing service."""
import json
import os
from pathlib import Path

import pytest

from api.services import agent_tools
from api.services.vault_listing import MAX_LIMIT, VaultListError, list_vault_entries
from config.settings import settings

pytestmark = pytest.mark.unit


def _touch(path: Path, text: str, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    _touch(root / "Work" / "old.md", "---\ntags: [alpha, beta]\n---\nbody", 1_700_000_000)
    _touch(root / "Work" / "new.md", "---\ntags: solo\n---\nbody", 1_760_000_000)
    _touch(root / "Work" / "Sub" / "mid.md", "no frontmatter", 1_730_000_000)
    _touch(root / "Work" / ".obsidian" / "hidden.md", "x", 1_770_000_000)
    _touch(root / ".trash" / "gone.md", "x", 1_770_000_000)
    _touch(root / "Work" / "notes.txt", "not markdown", 1_770_000_000)
    monkeypatch.setattr(settings, "vault_path", root)
    return root


def _call(**inp):
    return agent_tools._tool_list_vault(inp)


def test_sorted_newest_first_with_total_and_metadata(vault):
    data = json.loads(_call(path="Work"))
    assert data["total"] == 3
    assert [e["relative_path"] for e in data["entries"]] == ["Work/new.md", "Work/Sub/mid.md", "Work/old.md"]
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["old.md"]["tags"] == ["alpha", "beta"]
    assert by_name["new.md"]["tags"] == ["solo"]
    assert by_name["mid.md"]["tags"] == []
    assert set(by_name["old.md"]) == {"name", "relative_path", "modified_date", "note_type", "tags"}
    assert by_name["old.md"]["modified_date"].startswith("2023-11-")
    assert data["folders"] == ["Sub"]


def test_hidden_directories_excluded_at_root(vault):
    data = json.loads(_call(path=""))
    assert data["total"] == 3
    assert all(".obsidian" not in e["relative_path"] and ".trash" not in e["relative_path"] for e in data["entries"])
    assert data["folders"] == ["Work"]


def test_pagination(vault):
    first = json.loads(_call(path="Work", limit=2))
    second = json.loads(_call(path="Work", limit=2, offset=2))
    assert first["total"] == second["total"] == 3
    assert [e["name"] for e in first["entries"]] == ["new.md", "mid.md"]
    assert [e["name"] for e in second["entries"]] == ["old.md"]


def test_glob(vault):
    data = json.loads(_call(path="Work", glob="o*.md"))
    assert [e["name"] for e in data["entries"]] == ["old.md"]


@pytest.mark.parametrize("bad", ["..", "../outside", "Work/../..", "/etc", "~"])
def test_escaping_paths_return_error_string(vault, bad):
    result = _call(path=bad)
    assert isinstance(result, str) and result.startswith("Error:")


def test_missing_folder_is_error_string(vault):
    assert _call(path="Nope").startswith("Error:")


def test_symlink_escape_is_rejected(vault, tmp_path):
    outside = tmp_path / "outside"
    _touch(outside / "secret.md", "secret", 1_700_000_000)
    (vault / "link").symlink_to(outside)
    assert _call(path="link").startswith("Error:")
    data = json.loads(_call(path=""))
    assert all("secret" not in e["name"] for e in data["entries"])


def test_symlinked_vault_root_still_lists(tmp_path):
    real = tmp_path / "real"
    _touch(real / "A" / "n.md", "x", 1_700_000_000)
    link = tmp_path / "linkroot"
    link.symlink_to(real)
    result = list_vault_entries(link, "A")
    assert [e["relative_path"] for e in result["entries"]] == ["A/n.md"]


def test_escape_raises_list_error(vault):
    with pytest.raises(VaultListError):
        list_vault_entries(vault, "..")
    with pytest.raises(VaultListError):
        list_vault_entries(vault, "Work/../..")


def test_file_symlink_pointing_outside_is_dropped(vault, tmp_path):
    outside = tmp_path / "outside.md"
    _touch(outside, "secret", 1_700_000_000)
    (vault / "Work" / "leak.md").symlink_to(outside)
    data = json.loads(_call(path="Work"))
    assert "leak.md" not in {e["name"] for e in data["entries"]}
    assert data["total"] == 3


def test_registered_as_sync_tool():
    names = {t["name"] for t in agent_tools.TOOL_DEFINITIONS}
    assert "list_vault" in names
    assert "list_vault" in agent_tools._TOOL_HANDLERS
    assert "list_vault" in agent_tools._SYNC_HANDLERS


def test_route_returns_entries_and_400(vault):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes.vault import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    ok = client.get("/api/vault/list", params={"path": "Work", "limit": 1})
    assert ok.status_code == 200
    body = ok.json()
    assert body["total"] == 3 and body["entries"][0]["name"] == "new.md"
    assert client.get("/api/vault/list", params={"path": "../x"}).status_code == 400


def test_service_enforces_pagination_bounds(vault):
    assert list_vault_entries(vault, "Work", limit=MAX_LIMIT)["limit"] == MAX_LIMIT == 200
    for bad in (0, MAX_LIMIT + 1, -1):
        with pytest.raises(VaultListError):
            list_vault_entries(vault, "Work", limit=bad)
    with pytest.raises(VaultListError):
        list_vault_entries(vault, "Work", offset=-1)


def test_tool_rejects_out_of_range_limit_instead_of_clamping(vault):
    assert _call(path="Work", limit=0).startswith("Error:")
    assert _call(path="Work", limit=201).startswith("Error:")
    assert json.loads(_call(path="Work"))["limit"] == 50


def test_route_rejects_out_of_range_limit(vault):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes.vault import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/vault/list", params={"limit": 201}).status_code == 422
    assert client.get("/api/vault/list", params={"limit": 0}).status_code == 422


def test_tool_and_mcp_schemas_agree(vault):
    import mcp_server

    tool = next(t for t in agent_tools.TOOL_DEFINITIONS if t["name"] == "list_vault")["input_schema"]
    server = mcp_server.LifeOSMCPServer.__new__(mcp_server.LifeOSMCPServer)
    mcp = server._get_fallback_schema("lifeos_vault_list")
    assert mcp["properties"], "fallback schema missing"
    assert set(mcp["properties"]) == set(tool["properties"]) == {"path", "glob", "limit", "offset"}
    assert mcp["required"] == tool["required"] == []
    for schema in (tool, mcp):
        assert "1-200" in schema["properties"]["limit"]["description"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_folder_is_error_not_exception(vault):
    locked = vault / "Locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert _call(path="Locked").startswith("Error:")
        with pytest.raises(VaultListError):
            list_vault_entries(vault, "Locked")
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_child_of_unreadable_folder_is_error_not_exception(vault):
    locked = vault / "Locked"
    (locked / "Child").mkdir(parents=True)
    locked.chmod(0)
    try:
        assert _call(path="Locked/Child").startswith("Error:")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.routes.vault import router

        app = FastAPI()
        app.include_router(router)
        assert TestClient(app).get("/api/vault/list", params={"path": "Locked/Child"}).status_code == 400
    finally:
        locked.chmod(0o755)


def test_numeric_constraints_agree_across_tool_mcp_and_openapi(vault):
    import mcp_server
    from fastapi import FastAPI

    from api.routes.vault import router

    tool = next(t for t in agent_tools.TOOL_DEFINITIONS if t["name"] == "list_vault")["input_schema"]["properties"]
    server = mcp_server.LifeOSMCPServer.__new__(mcp_server.LifeOSMCPServer)
    mcp = server._get_fallback_schema("lifeos_vault_list")["properties"]
    app = FastAPI()
    app.include_router(router)
    spec = app.openapi()
    op = spec["paths"]["/api/vault/list"]["get"]
    live = server._build_input_schema(op, spec.get("components", {}).get("schemas", {}), "get", "/api/vault/list")["properties"]
    for name, lo, hi, default in (("limit", 1, 200, 50), ("offset", 0, None, 0)):
        for schema in (tool[name], mcp[name], live[name]):
            assert schema.get("minimum") == lo
            assert schema.get("maximum") == hi
            assert schema.get("default") == default
            assert schema["description"] and not schema["description"].startswith("Query parameter")
