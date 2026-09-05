"""Browser test for web/agents/graph_encoding.js — the graph's pure
encoding functions (label precedence, engine shape, lane colour, node size,
search-tier validation, hover-card rows), unit-tested in isolation via the
same `add_script_tag` harness `tests/test_agents_assignment_ui_browser.py`
uses for web/agents/assignment.js: no DOM, no d3, no fetch, so exercising
these directly needs neither a running API nor a rendered graph.

Serves `web/` itself from an ephemeral port rather than a running API —
every function under test is pure — so this carries no `requires_server`
marker and runs at pre-push (`browser and not requires_server`).
"""
import http.server
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _WebHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def web_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _load_module(page: Page, base_url: str):
    """Serve a page on the target origin and inject graph_encoding.js as a
    module, exposing every export on `window.__enc` for the test to drive."""
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body="{}"))
    page.goto(f"{base_url}/agents.html")
    page.add_script_tag(
        content="""
        import * as enc from '/agents/graph_encoding.js';
        window.__enc = enc;
        window.__encReady = true;
        """,
        type="module",
    )
    page.wait_for_function("() => window.__encReady === true")


def _call(page: Page, fn: str, *args):
    return page.evaluate(f"(args) => window.__enc.{fn}(...args)", list(args))


# ---------------------------------------------------------------------------
# nodeLabel — precedence + raw-id guard + "model badge never the label"
# ---------------------------------------------------------------------------

class TestNodeLabel:
    def test_custom_label_wins(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"session_id": "sess_abc12345", "custom_label": "My Session",
             "label": "raw fallback", "prompt_preview": "do the thing"}
        assert _call(page, "nodeLabel", d) == "My Session"

    def test_raw_id_custom_label_is_skipped(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"session_id": "sess_exact_a8", "custom_label": "sess_exact_a8",
             "label": "Real Title"}
        assert _call(page, "nodeLabel", d) == "Real Title"

    def test_cc_prefixed_raw_id_label_is_skipped(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"session_id": "cc:uuid-1234", "label": "cc:uuid-1234",
             "prompt_preview": "refactor the widget"}
        assert _call(page, "nodeLabel", d) == "refactor the widget"

    def test_task_id_raw_label_is_skipped(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"session_id": "sess_1", "task_id": "t-42", "label": "t-42",
             "routing": "local"}
        assert _call(page, "nodeLabel", d) == "Local"

    def test_model_label_is_never_the_node_label(self, page: Page, web_base_url):
        """A row whose only non-raw field is `model_label` must NOT show
        the model as its name — the label falls through to the routing
        name instead, since `model_label` is a chip, never a label."""
        _load_module(page, web_base_url)
        d = {
            "session_id": "sess_modelonly", "label": "sess_modelonly",
            "model_label": "Hermes · deepseek-v3", "routing": "hermes",
        }
        label = _call(page, "nodeLabel", d)
        assert label == "Hermes"
        assert "deepseek" not in label.lower()

    def test_falls_back_to_routing_label_when_nothing_else_is_real(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"session_id": "sess_12345678", "label": "sess_12345678", "routing": None}
        assert _call(page, "nodeLabel", d) == "Local"

    def test_never_emits_a_literal_question_mark(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"session_id": "sess_only_id"}
        assert _call(page, "nodeLabel", d) != "?"


# ---------------------------------------------------------------------------
# engineOf
# ---------------------------------------------------------------------------

class TestEngineOf:
    @pytest.mark.parametrize("row,expected", [
        ({"source": "claude_code", "routing": "hermes"}, "claude_code"),
        ({"source": "codex", "routing": "local"}, "codex"),
        ({"routing": "hermes"}, "hermes"),
        ({"routing": "remote"}, "hermes"),
        ({"routing": "local"}, "local"),
        ({"routing": None}, "local"),
        ({"routing": "claude_code"}, "claude_code"),
        ({"routing": "code"}, "claude_code"),
        ({"routing": "codex"}, "codex"),
        ({"routing": "claude"}, "claude"),
        ({"routing": "something-unrecognized"}, "claude"),
    ])
    def test_engine_precedence(self, page: Page, web_base_url, row, expected):
        _load_module(page, web_base_url)
        assert _call(page, "engineOf", row) == expected


# ---------------------------------------------------------------------------
# shapeTagFor / ENGINE_SHAPES — five distinct glyphs
# ---------------------------------------------------------------------------

class TestShapes:
    def test_five_distinct_glyphs(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        glyphs = page.evaluate(
            "() => Object.values(window.__enc.ENGINE_SHAPES).map(v => v.glyph)"
        )
        assert len(glyphs) == 5
        assert len(set(glyphs)) == 5

    def test_legend_names_every_engine(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        labels = page.evaluate(
            "() => Object.values(window.__enc.ENGINE_SHAPES).map(v => v.label)"
        )
        assert set(labels) == {"Claude Code", "Codex", "Hermes", "Local", "Claude"}

    def test_shape_tag_defined_for_every_engine(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        for engine in ["claude_code", "codex", "hermes", "local", "claude"]:
            tag = _call(page, "shapeTagFor", engine)
            assert tag in ("rect", "circle", "polygon", "path")


# ---------------------------------------------------------------------------
# radiusForActiveSeconds — monotonic, floored, capped, tokens-independent
# ---------------------------------------------------------------------------

class TestRadius:
    def test_floor_at_zero(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        assert _call(page, "radiusForActiveSeconds", 0) == 12

    def test_monotonic_increase(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        r1 = _call(page, "radiusForActiveSeconds", 30)
        r2 = _call(page, "radiusForActiveSeconds", 600)
        r3 = _call(page, "radiusForActiveSeconds", 6000)
        assert r1 <= r2 <= r3

    def test_capped(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        assert _call(page, "radiusForActiveSeconds", 10_000_000) == 40

    def test_never_below_floor_or_above_cap(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        for seconds in [-100, 0, 1, 60, 3600, 999999]:
            r = _call(page, "radiusForActiveSeconds", seconds)
            assert 12 <= r <= 40


class TestRingWidth:
    def test_monotonic_and_bounded(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        w0 = _call(page, "ringWidthForToolCalls", 0)
        w5 = _call(page, "ringWidthForToolCalls", 5)
        w100 = _call(page, "ringWidthForToolCalls", 100)
        assert w0 <= w5 <= w100
        assert 1 <= w0 <= 6
        assert 1 <= w100 <= 6


# ---------------------------------------------------------------------------
# laneColor — every lane, all distinct
# ---------------------------------------------------------------------------

class TestLaneColor:
    def test_every_lane_has_a_distinct_color(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        lane_ids = ["unassigned", "assigned", "in_progress", "human_queue",
                    "scheduled", "review", "done"]
        colors = [_call(page, "laneColor", lane_id) for lane_id in lane_ids]
        assert len(set(colors)) == len(lane_ids)

    def test_unknown_lane_falls_back_to_unassigned(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        assert _call(page, "laneColor", "nonexistent-lane") == _call(page, "laneColor", "unassigned")


# ---------------------------------------------------------------------------
# isKnownSearchField — own-property check rejects prototype members
# ---------------------------------------------------------------------------

class TestIsKnownSearchField:
    @pytest.mark.parametrize("field", ["label", "short_label", "summary"])
    def test_known_fields_accepted(self, page: Page, web_base_url, field):
        _load_module(page, web_base_url)
        assert _call(page, "isKnownSearchField", field) is True

    @pytest.mark.parametrize("field", ["constructor", "cwd", "toString", "hasOwnProperty"])
    def test_unknown_and_prototype_fields_rejected(self, page: Page, web_base_url, field):
        _load_module(page, web_base_url)
        assert _call(page, "isKnownSearchField", field) is False


# ---------------------------------------------------------------------------
# hoverCardRows — ordering + formatting
# ---------------------------------------------------------------------------

class TestHoverCardRows:
    def test_row_order_and_formatting(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {
            "total_active_seconds": 125,
            "total_dollars": 1.5,
            "model_label": "Sonnet",
            "effort": "high",
            "host": "studio",
            "branch": "feat/synthetic",
            "decoded_cwd": "/home/synthetic/proj",
            "last_event_kind": "tool_call",
        }
        rows = _call(page, "hoverCardRows", d)
        labels = [r[0] for r in rows]
        assert labels == ["Duration", "Cost", "Model", "Host", "Branch", "Last event"]
        values = dict(rows)
        assert values["Duration"] == "2m 5s"
        assert values["Cost"] == "$1.5000"
        assert values["Model"] == "Sonnet · high"
        assert values["Host"] == "studio"
        assert values["Branch"] == "feat/synthetic"
        assert values["Last event"] == "tool_call"

    def test_branch_preferred_over_cwd(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"total_active_seconds": 5, "total_dollars": 0,
             "branch": "main", "decoded_cwd": "/home/synthetic/proj"}
        rows = _call(page, "hoverCardRows", d)
        by_label = dict(rows)
        assert "Branch" in by_label
        assert "Cwd" not in by_label

    def test_cwd_used_when_no_branch(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"total_active_seconds": 5, "total_dollars": 0,
             "decoded_cwd": "/home/synthetic/proj"}
        rows = _call(page, "hoverCardRows", d)
        by_label = dict(rows)
        assert by_label.get("Cwd") == "/home/synthetic/proj"

    def test_omits_empty_optional_rows(self, page: Page, web_base_url):
        _load_module(page, web_base_url)
        d = {"total_active_seconds": 0, "total_dollars": 0}
        rows = _call(page, "hoverCardRows", d)
        labels = [r[0] for r in rows]
        assert labels == ["Duration", "Cost"]
