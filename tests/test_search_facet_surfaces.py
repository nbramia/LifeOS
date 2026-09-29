"""Facet parameters on the /api/search route, the search_vault tool and lifeos_search."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api.routes.search as search_route_mod
from api.main import app
from api.services import agent_tools
from api.services.search_facets import SearchFacets

pytestmark = pytest.mark.slow

MCP_SERVER_PATH = Path(__file__).resolve().parent.parent / "mcp_server.py"
FACET_PARAMS = {
    "folder", "note_type", "people", "tags", "doc_type", "domain", "topic", "project",
}


class _RecordingSearch:
    def __init__(self):
        self.calls = []

    def search(self, query, top_k=20, **kwargs):
        self.calls.append(kwargs)
        return []


@pytest.fixture
def recorder(monkeypatch):
    rec = _RecordingSearch()
    monkeypatch.setattr(search_route_mod, "get_hybrid_search", lambda: rec)
    return rec


def test_route_passes_every_filter_to_hybrid_search_as_facets(recorder):
    body = {"query": "hiring", "filters": {
        "note_type": ["Granola"], "people": ["Avery"], "folder": "Work",
        "tags": ["t1"], "doc_type": ["meeting"], "domain": ["work"],
        "topic": ["hiring"], "project": ["alpha"],
    }}
    assert TestClient(app).post("/api/search", json=body).status_code == 200
    facets = recorder.calls[0]["facets"]
    assert facets.note_type == ["Granola"]
    assert facets.people == ["Avery"]
    assert facets.folder == "Work"
    assert facets.tags == ["t1"]
    assert facets.doc_type == ["meeting"]
    assert facets.domain == ["work"]
    assert facets.topic == ["hiring"]
    assert facets.project == ["alpha"]


def test_route_multi_value_note_type_is_a_pre_filter_too(recorder):
    TestClient(app).post(
        "/api/search", json={"query": "x", "filters": {"note_type": ["Work", "Personal"]}}
    )
    assert recorder.calls[0]["facets"].note_type == ["Work", "Personal"]


def test_route_without_filters_passes_no_facets(recorder):
    TestClient(app).post("/api/search", json={"query": "x"})
    TestClient(app).post("/api/search", json={"query": "x", "filters": {}})
    assert [c["facets"] for c in recorder.calls] == [None, None]


def test_route_does_not_post_filter_ranked_results(monkeypatch):
    """Filtering is the search's job; the route returns what it is given."""
    class Stub:
        def search(self, query, top_k=20, **kw):
            return [{"content": "c", "file_path": "/v/a.md", "file_name": "a.md",
                     "note_type": "Work", "people": [], "score": 1.0}]
    monkeypatch.setattr(search_route_mod, "get_hybrid_search", lambda: Stub())
    r = TestClient(app).post(
        "/api/search", json={"query": "x", "filters": {"people": ["Someone"]}}
    )
    assert len(r.json()["results"]) == 1


def test_search_vault_tool_schema_exposes_all_facets_and_dates():
    tool = next(t for t in agent_tools.TOOL_DEFINITIONS if t["name"] == "search_vault")
    props = tool["input_schema"]["properties"]
    assert FACET_PARAMS | {"date_from", "date_to", "query", "top_k"} == set(props)
    assert tool["input_schema"]["required"] == ["query"]
    for phrase in ("doc_type", "date_from", "meeting notes about hiring since July"):
        assert phrase in tool["description"]


def test_search_vault_handler_builds_facets_and_dates():
    seen = {}

    class HS:
        def search(self, query, top_k=20, **kw):
            seen.update(kw)
            return []

    with patch("api.services.hybrid_search.HybridSearch", HS):
        out = agent_tools._tool_search_vault({
            "query": "hiring", "doc_type": "meeting", "topic": ["hiring"],
            "date_from": "2026-07-01", "folder": "Work",
        })
    assert seen["date_from"] == "2026-07-01"
    facets = seen["facets"]
    assert isinstance(facets, SearchFacets)
    assert (facets.doc_type, facets.topic, facets.folder) == (["meeting"], ["hiring"], "Work")
    assert "doc_type" in out and "topic" in out and "folder" in out


def test_search_vault_handler_without_filters_calls_search_as_before():
    seen = {}

    class HS:
        def search(self, *args, **kw):
            seen["args"], seen["kw"] = args, kw
            return []

    with patch("api.services.hybrid_search.HybridSearch", HS):
        agent_tools._tool_search_vault({"query": "hiring"})
    assert seen["kw"] == {"top_k": agent_tools._VAULT_TOP_K_DEFAULT}


def _server(spec=None):
    module_spec = importlib.util.spec_from_file_location("mcp_server", MCP_SERVER_PATH)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    return module, server


def test_lifeos_search_live_and_fallback_schemas_expose_the_same_facets():
    _, server = _server()
    server.openapi_spec = app.openapi()
    server.tools = []
    server._build_tools_from_spec()
    live = next(t for t in server.tools if t["name"] == "lifeos_search")["inputSchema"]["properties"]
    server.tools = []
    server._build_tools_fallback()
    fallback = next(t for t in server.tools if t["name"] == "lifeos_search")["inputSchema"]["properties"]
    for props in (live, fallback):
        assert FACET_PARAMS | {"date_from", "date_to", "query", "top_k"} <= set(props)
        assert "filters" not in props
    assert {k: live[k].get("type") for k in FACET_PARAMS} == {
        k: fallback[k].get("type") for k in FACET_PARAMS
    }


def test_mcp_and_agent_tool_facet_schemas_agree():
    module, _ = _server()
    tool = next(t for t in agent_tools.TOOL_DEFINITIONS if t["name"] == "search_vault")
    for name in FACET_PARAMS:
        agent = tool["input_schema"]["properties"][name]
        mcp = module._SEARCH_FACET_PROPERTIES[name]
        assert agent["type"] == mcp["type"]
        assert agent.get("items") == mcp.get("items")


def test_lifeos_search_folds_flat_facets_into_filters():
    module, server = _server()
    args = {"query": "q", "people": "Avery", "folder": "Work", "doc_type": ["meeting"],
            "topic": [], "date_from": "2026-07-01"}
    server._fold_search_facets(args)
    assert args == {
        "query": "q", "date_from": "2026-07-01",
        "filters": {"people": ["Avery"], "folder": "Work", "doc_type": ["meeting"]},
    }
    bare = {"query": "q"}
    server._fold_search_facets(bare)
    assert bare == {"query": "q"}
