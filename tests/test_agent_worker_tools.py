"""Standard-tool handler tests for the local executor.

LifeOS MCP proxying is covered indirectly via the registry; we don't spin up
the full MCP catalog here. The standard tools (Read/Write/Edit/Bash/WebFetch)
have well-defined inputs and are exercised directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.tools import (
    STANDARD_HANDLERS,
    ToolRegistry,
)


# ---------------------------------------------------------------------------
# Read / Write / Edit
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_read_returns_contents(tmp_path: Path):
    p = tmp_path / "hi.txt"
    p.write_text("hello", encoding="utf-8")
    r = STANDARD_HANDLERS["Read"]({"file_path": str(p)})
    assert not r.is_error
    assert r.output == "hello"


@pytest.mark.unit
def test_read_missing_file(tmp_path: Path):
    r = STANDARD_HANDLERS["Read"]({"file_path": str(tmp_path / "nope.txt")})
    assert r.is_error
    assert "not found" in r.output


@pytest.mark.unit
def test_read_requires_path():
    r = STANDARD_HANDLERS["Read"]({})
    assert r.is_error


@pytest.mark.unit
def test_write_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "sub" / "deep" / "out.txt"
    r = STANDARD_HANDLERS["Write"]({"file_path": str(target), "content": "bye"})
    assert not r.is_error
    assert target.read_text() == "bye"


@pytest.mark.unit
def test_edit_replaces_unique_occurrence(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("alpha beta gamma", encoding="utf-8")
    r = STANDARD_HANDLERS["Edit"]({
        "file_path": str(p), "old_string": "beta", "new_string": "BETA",
    })
    assert not r.is_error
    assert p.read_text() == "alpha BETA gamma"


@pytest.mark.unit
def test_edit_requires_unique_match(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("dup dup", encoding="utf-8")
    r = STANDARD_HANDLERS["Edit"]({
        "file_path": str(p), "old_string": "dup", "new_string": "x",
    })
    assert r.is_error
    assert "occurs 2 times" in r.output


@pytest.mark.unit
def test_edit_old_string_not_found(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("hello world", encoding="utf-8")
    r = STANDARD_HANDLERS["Edit"]({
        "file_path": str(p), "old_string": "nope", "new_string": "x",
    })
    assert r.is_error


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_bash_captures_stdout():
    r = STANDARD_HANDLERS["Bash"]({"command": "echo hi"})
    assert not r.is_error
    assert "hi" in r.output


@pytest.mark.unit
def test_bash_returns_error_on_nonzero():
    r = STANDARD_HANDLERS["Bash"]({"command": "exit 7"})
    assert r.is_error
    assert "exit 7" in r.output


@pytest.mark.unit
def test_bash_timeout():
    # Use a short timeout — we don't actually want to sleep 60s in tests.
    r = STANDARD_HANDLERS["Bash"]({"command": "sleep 3", "timeout_seconds": 1})
    assert r.is_error
    assert "timed out" in r.output


# ---------------------------------------------------------------------------
# WebSearch stub
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_websearch_returns_not_configured():
    r = STANDARD_HANDLERS["WebSearch"]({"query": "anything"})
    assert r.is_error
    assert "not configured" in r.output


# ---------------------------------------------------------------------------
# Registry / dispatch
# ---------------------------------------------------------------------------

class _FakeMCPServer:
    """Minimal stand-in for LifeOSMCPServer — exposes one fake LifeOS tool."""

    def __init__(self, response):
        self.tools = [
            {
                "name": "lifeos_fake",
                "description": "test tool",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        self._response = response

    def _call_api(self, name, arguments):
        return self._response

    def _format_response(self, name, data):
        return f"formatted: {data}"


@pytest.mark.unit
def test_registry_dispatches_to_standard_handler(tmp_path: Path):
    reg = ToolRegistry(lifeos_mcp_server=_FakeMCPServer({"ok": True}))
    r = reg.dispatch("Bash", {"command": "echo from-registry"})
    assert not r.is_error
    assert "from-registry" in r.output


@pytest.mark.unit
def test_registry_proxies_lifeos_tool():
    reg = ToolRegistry(lifeos_mcp_server=_FakeMCPServer({"hits": 3}))
    r = reg.dispatch("lifeos_fake", {})
    assert not r.is_error
    assert "hits" in r.output


@pytest.mark.unit
def test_registry_lifeos_tool_error_surfaced():
    reg = ToolRegistry(lifeos_mcp_server=_FakeMCPServer({"error": "boom"}))
    r = reg.dispatch("lifeos_fake", {})
    assert r.is_error
    assert "boom" in r.output


@pytest.mark.unit
def test_registry_sleep_returns_yield_seconds():
    reg = ToolRegistry(lifeos_mcp_server=_FakeMCPServer({}))
    r = reg.dispatch("sleep", {"seconds": 30, "reason": "waiting on CI"})
    assert r.yield_seconds == 30
    assert "waiting on CI" in r.output


@pytest.mark.unit
def test_registry_unknown_tool_is_error():
    reg = ToolRegistry(lifeos_mcp_server=_FakeMCPServer({}))
    r = reg.dispatch("DoesNotExist", {})
    assert r.is_error
    assert "unknown" in r.output


@pytest.mark.unit
def test_registry_definitions_includes_standard_and_mcp():
    reg = ToolRegistry(lifeos_mcp_server=_FakeMCPServer({}))
    names = {t["name"] for t in reg.definitions()}
    # Standard tools
    for n in ("Read", "Write", "Edit", "Bash", "WebFetch", "WebSearch", "sleep"):
        assert n in names
    # LifeOS tool
    assert "lifeos_fake" in names
