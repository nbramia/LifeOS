"""Tests that the LifeOS MCP server exposes the lifeos_agent_* tool family.

Managed Agents reach inter-agent tools via the MCP server, so the schema
must be correct AND `_call_api` must dispatch to the right handler with
the `caller_session_id` arg.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server


@pytest.fixture
def server(monkeypatch, tmp_path: Path):
    """A LifeOSMCPServer with the agent stack pointed at a temp DB.

    `_handle_inter_agent` anchors its SessionStore/TranscriptStore to the
    repo-root constants (`AGENT_SESSIONS_DB` / `AGENT_TRANSCRIPTS_DIR`) so it
    works regardless of the cwd the MCP server was spawned with. We point those
    constants at the sandbox, and also chdir so a bare `SessionStore()` in a
    test resolves to the same place.
    """
    monkeypatch.setenv("LIFEOS_AGENT_VAULT_ID", "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "AGENT_SESSIONS_DB", tmp_path / "data" / "agent_sessions.db")
    monkeypatch.setattr(mcp_server, "AGENT_TRANSCRIPTS_DIR", tmp_path / "data" / "agent_transcripts")
    srv = mcp_server.LifeOSMCPServer()
    return srv


@pytest.mark.unit
def test_inter_agent_stores_anchored_to_repo_not_cwd(monkeypatch, tmp_path: Path):
    """Regression: the MCP server is spawned by CLI agents with the agent's
    `-C` dir as cwd, so inter-agent tools must NOT resolve the session store
    relative to cwd (that opened a phantom empty DB → every call failed
    'no_caller'). The anchored paths must be absolute and repo-rooted even when
    cwd is elsewhere."""
    monkeypatch.chdir(tmp_path)
    assert mcp_server.AGENT_SESSIONS_DB.is_absolute()
    assert mcp_server.AGENT_SESSIONS_DB == mcp_server._REPO_ROOT / "data" / "agent_sessions.db"
    assert mcp_server.AGENT_TRANSCRIPTS_DIR == mcp_server._REPO_ROOT / "data" / "agent_transcripts"
    # The anchor is the repo root (where mcp_server.py lives), not the cwd.
    assert mcp_server._REPO_ROOT != Path(tmp_path)


@pytest.mark.unit
def test_inter_agent_tools_are_registered(server):
    tool_names = {t["name"] for t in server.tools}
    expected = {
        "lifeos_agent_spawn",
        "lifeos_agent_send",
        "lifeos_agent_check",
        "lifeos_agent_yield_until",
        "lifeos_agent_kill",
        "lifeos_agent_transcript_read",
        "lifeos_agent_sessions_list",
    }
    assert expected.issubset(tool_names)


@pytest.mark.unit
def test_inter_agent_tool_schemas_require_caller_session_id(server):
    """Remote agents must pass their own session_id explicitly."""
    for tool in server.tools:
        if not tool["name"].startswith("lifeos_agent_"):
            continue
        schema = tool.get("inputSchema") or tool.get("input_schema")
        assert "caller_session_id" in schema["properties"]
        assert "caller_session_id" in schema["required"]
        # caller_session_id should appear first in the required list.
        assert schema["required"][0] == "caller_session_id"


@pytest.mark.unit
def test_call_api_missing_caller_returns_error(server):
    result = server._call_api("lifeos_agent_check", {"session_id": "x"})
    assert "error" in result
    assert "caller_session_id" in result["error"]


@pytest.mark.unit
def test_call_api_dispatches_to_inter_agent_handler(server):
    """End-to-end MCP → inter_agent.dispatch path."""
    # SessionStore() uses the default relative path; the server fixture
    # chdir'd into tmp_path so this is sandboxed.
    from api.services.agent_worker.session_store import (
        STATUS_RUNNING,
        SessionStore,
    )
    store = SessionStore()
    sess = store.create(
        task_id="t_mcp", status=STATUS_RUNNING, routing="claude",
        budget={"max_dollars": 5.0, "wall_seconds": 60, "max_tokens": 1000},
    )

    result = server._call_api("lifeos_agent_check", {
        "caller_session_id": sess.session_id,
        "session_id": sess.session_id,
    })
    assert result["ok"]
    assert result["status"] == STATUS_RUNNING
