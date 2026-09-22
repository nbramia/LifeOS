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
    works regardless of the cwd the MCP server was spawned with. We point
    those constants at the sandbox. The chdir is defense in depth, not load-
    bearing for `_handle_inter_agent` itself (which never looks at cwd) — a
    test that also wants a bare `SessionStore()`/`TranscriptStore()` to land
    in this sandbox must still pass the monkeypatched constant explicitly
    (see `test_call_api_dispatches_to_inter_agent_handler`), since both
    classes' own defaults are repo-root-anchored too, not
    cwd-relative.
    """
    monkeypatch.setenv("LIFEOS_AGENT_VAULT_ID", "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "AGENT_SESSIONS_DB", tmp_path / "data" / "agent_sessions.db")
    monkeypatch.setattr(mcp_server, "AGENT_TRANSCRIPTS_DIR", tmp_path / "data" / "agent_transcripts")
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer,
        "_load_openapi_spec",
        lambda self: self._build_tools_fallback(),
    )
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
def test_mcp_catalog_count_and_inter_agent_schema_contract(server):
    """The registered fallback catalog is 71 curated + 11 inter-agent tools.

    The eleventh inter-agent tool is the attested project-owner review/
    completion surface (`lifeos_agent_project_owner`); keeping this
    assertion next to the schema checks prevents docs and live registration
    from silently disagreeing after tool additions.
    """
    from api.services.agent_worker.inter_agent import INTER_AGENT_TOOL_SCHEMAS

    assert len(mcp_server.CURATED_ENDPOINTS) == mcp_server.CURATED_TOOL_COUNT == 71
    assert len(INTER_AGENT_TOOL_SCHEMAS) == 11
    assert len(server.tools) == 82
    assert len({tool["name"] for tool in server.tools}) == len(server.tools)


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
def test_mcp_rejects_spoofed_caller_against_process_identity(server):
    server._trusted_session_id = "sess-trusted"
    result = server._call_api("lifeos_agent_check", {
        "caller_session_id": "sess-forged",
        "caller_proof": "ignored-for-stdio",
        "session_id": "sess-forged",
    })
    assert result["error"] == "caller_session_id does not match trusted MCP identity"


@pytest.mark.unit
def test_mcp_accepts_matching_stdio_process_identity(server):
    """A CLI child carrying its own trusted session id can call inter-agent
    tools; the caller id is accepted because it matches the process identity."""
    from api.services.agent_worker.session_store import STATUS_RUNNING, SessionStore

    store = SessionStore(db_path=mcp_server.AGENT_SESSIONS_DB)
    sess = store.create(
        task_id="t_mcp_stdio", status=STATUS_RUNNING, routing="codex",
        budget={"max_dollars": 5.0, "wall_seconds": 60, "max_tokens": 1000},
    )
    server._trusted_session_id = sess.session_id

    result = server._call_api("lifeos_agent_check", {
        "caller_session_id": sess.session_id,
        "caller_proof": "stdio-process-identity-is-the-proof",
        "session_id": sess.session_id,
    })

    assert result["ok"]
    assert result["status"] == STATUS_RUNNING


@pytest.mark.unit
def test_mcp_rejects_caller_without_trusted_identity_or_transport_proof(server):
    """A bare caller-supplied id is never enough when no stdio identity or
    authenticated HTTP transport proof is available."""
    server._trusted_session_id = ""
    server._mcp_transport_secret = ""
    result = server._call_api("lifeos_agent_check", {
        "caller_session_id": "sess-forged",
        "caller_proof": "",
        "session_id": "sess-forged",
    })
    assert result["error"] == "trusted MCP caller identity is unavailable"


@pytest.mark.unit
def test_handoff_stdio_requires_process_bound_exact_turn(server):
    server._trusted_session_id = "sess-synthetic"
    server._trusted_attempt_id = ""
    server._trusted_turn_id = ""

    result = server._call_api("lifeos_agent_project_handoff", {
        "caller_session_id": "sess-synthetic",
        "caller_proof": "ignored-for-stdio",
        "caller_attempt_id": "attempt-model-supplied",
        "caller_turn_id": "turn-model-supplied",
        "caller_turn_proof": "model-supplied",
        "operation_id": "synthetic-v1",
        "children": [{"key": "one", "description": "Synthetic child"}],
    })

    assert result["error"] == "trusted MCP turn identity is unavailable"


@pytest.mark.unit
def test_handoff_http_rejects_session_valid_but_turn_forged_proof(server):
    from api.services.agent_worker.inter_agent import caller_proof_for_session

    server._trusted_session_id = ""
    server._mcp_transport_secret = "synthetic-mcp-secret"
    result = server._call_api("lifeos_agent_project_handoff", {
        "caller_session_id": "sess-synthetic",
        "caller_proof": caller_proof_for_session(
            "sess-synthetic", "synthetic-mcp-secret",
        ),
        "caller_attempt_id": "attempt-synthetic",
        "caller_turn_id": "turn-forged",
        "caller_turn_proof": "forged",
        "operation_id": "synthetic-v1",
        "children": [{"key": "one", "description": "Synthetic child"}],
    })

    assert result["error"] == "invalid MCP caller turn proof"


@pytest.mark.unit
def test_call_api_dispatches_to_inter_agent_handler(server):
    """End-to-end MCP → inter_agent.dispatch path."""
    # Must point at the SAME db `_handle_inter_agent` resolves to -- the
    # fixture's monkeypatched `mcp_server.AGENT_SESSIONS_DB`, not a bare
    # `SessionStore()`. `SessionStore`'s own default is repo-root-anchored,
    # so a bare default here would (correctly) resolve to the
    # real repo db regardless of the fixture's `monkeypatch.chdir(tmp_path)`,
    # missing the session this test just created.
    from api.services.agent_worker.session_store import (
        STATUS_RUNNING,
        SessionStore,
    )
    store = SessionStore(db_path=mcp_server.AGENT_SESSIONS_DB)
    sess = store.create(
        task_id="t_mcp", status=STATUS_RUNNING, routing="claude",
        budget={"max_dollars": 5.0, "wall_seconds": 60, "max_tokens": 1000},
    )
    # A direct unit invocation stands in for the process-bound stdio identity;
    # production CLI children receive this from LIFEOS_AGENT_SESSION_ID.
    server._trusted_session_id = sess.session_id

    result = server._call_api("lifeos_agent_check", {
        "caller_session_id": sess.session_id,
        "session_id": sess.session_id,
    })
    assert result["ok"]
    assert result["status"] == STATUS_RUNNING
