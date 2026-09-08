"""Browser test for the /agents Graph tab — label
precedence against a linked card's title, the lane colour legend, the
five-engine shape legend, question/error/subagent badges, host columns,
the HTML hover card, synchronous click and double-click focus, zoom
controls, and the simulation reheating on a size-only change.

Serves `web/` itself on an ephemeral port and stubs every `/api/` call —
the same server-free pattern as `tests/test_agents_host_filter_ui_browser.py`
— so this carries no `requires_server` marker and runs at pre-push
(`browser and not requires_server`). `/agents` loads d3 from
`https://d3js.org/d3.v7.min.js`, which is left unstubbed (real network),
same as every other test in this family — hover, click, and collapse all
need the real force simulation.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _AgentsHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/agents", "/"):
            return str(WEB_DIR / "agents.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def agents_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AgentsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _row(**overrides):
    base = {
        "session_id": "sess_placeholder",
        "task_id": None,
        "status": "running",
        "status_inferred": False,
        "routing": "local",
        "source": "lifeos_agent",
        "host": "build-host",
        "parent_session_id": None,
        "is_subagent": False,
        "started_at": 1000,
        "last_activity_at": 2000,
        "total_input_tokens": 5,
        "total_output_tokens": 5,
        "total_dollars": 0.01,
        "total_active_seconds": 60,
        "spawn_depth": 0,
        "label": "placeholder",
        "model_label": "Local",
        "tool_call_count": 0,
        "error_count": 0,
        "lane": "in_progress",
        "pending_question": None,
        "decoded_cwd": "/home/synthetic/proj",
    }
    base.update(overrides)
    return base


# A card-linked Claude Code session — `label` is already the card title
# (`_label_for_session` resolves it server-side via the task manager), not a
# separately-fetched board record; the graph reads it straight off the
# snapshot row. Also the parent of a collapsed Claude Code subagent, so it
# doubles as the "collapse/expand" and "double-click focuses; a subagent
# never does" subject.
CC_PARENT = _row(
    session_id="cc:redesign-parent", task_id="t-cc", routing="claude_code",
    source="claude_code", host="studio-host", label="Fix the flaky graph test",
    model_label="Claude Code", total_active_seconds=600, tool_call_count=5,
    lane="in_progress",
)
CC_SUBAGENT = _row(
    session_id="cc:redesign-subagent", task_id="t-cc", routing="claude_code",
    source="claude_code", host="studio-host", label="cc:redesign-subagent",
    parent_session_id="cc:redesign-parent", is_subagent=True,
    model_label="Claude Code", total_active_seconds=30, lane="in_progress",
)
# Unlinked (no card) — `label` falls back to the raw session id, same as a
# real ingest with no title; the node's actual name comes from
# `prompt_preview`.
CODEX_ROW = _row(
    session_id="cx:redesign-codex", routing="codex", source="codex",
    host="studio-host", label="cx:redesign-codex",
    prompt_preview="investigate the codex regression",
    model_label="Codex", total_active_seconds=120, tool_call_count=1,
    error_count=2, lane="in_progress",
)
HERMES_ROW = _row(
    session_id="sess-hermes", routing="hermes", host="build-host",
    label="Deploy the nightly build", model_label="Hermes · deepseek-v3",
    total_active_seconds=300, lane="in_progress",
    pending_question={
        "id": 1, "session_id": "sess-hermes", "question": "Proceed with deploy?",
        "asked_at": 1700, "bot": None,
    },
)
LOCAL_ROW = _row(
    session_id="sess-local", routing="local", host="build-host",
    status="blocked", label="Local blocked task", model_label="Local",
    total_active_seconds=45, lane="human_queue",
)
CLAUDE_API_ROW = _row(
    session_id="sess-claude-api", routing="claude", host="build-host",
    status="completed", label="Summarize the quarterly report",
    model_label="Sonnet", total_active_seconds=3600, lane="done",
)
RAWID_ROW = _row(
    session_id="sess_rawid_9f8e7d6c", routing="local", host="build-host",
    label="sess_rawid_9f8e7d6c", custom_label=None, prompt_preview=None,
    model_label="Local", total_active_seconds=15, lane="in_progress",
)

SNAPSHOT = {
    "sessions": [CC_PARENT, CC_SUBAGENT, CODEX_ROW, HERMES_ROW, LOCAL_ROW,
                 CLAUDE_API_ROW, RAWID_ROW],
    "edges": [{"from": "cc:redesign-parent", "to": "cc:redesign-subagent", "type": "spawn"}],
    "generated_at": 1234567890,
    "api_host": "build-host",
}


def _make_handler(snapshot, focus_calls=None, search_matches=None):
    def handler(route):
        url = route.request.url
        if "/stream" in url and "/sessions/" not in url:
            route.fulfill(status=200, content_type="text/event-stream", body="")
            return
        if focus_calls is not None and "/focus" in url and route.request.method == "POST":
            focus_calls.append(url)
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        if "/api/agents/snapshot" in url:
            body = snapshot
        elif "/api/agents/board" in url:
            body = {"lanes": {}, "generated_at": 0}
        elif "/api/agents/search" in url:
            body = {"query": "", "matches": search_matches or []}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return handler


def _open_agents(page: Page, base_url, snapshot=None, focus_calls=None, search_matches=None):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.route("**/api/**", _make_handler(snapshot or SNAPSHOT, focus_calls, search_matches))
    page.goto(f"{base_url}/agents")
    page.click('[data-tab="graph"]')
    page.wait_for_selector("#filter-route")
    page.select_option("#filter-recency", "all")
    # The shared lane filter defaults to every lane but Done — several
    # fixtures below deliberately carry `lane: "done"`, so widen it the same
    # way the recency filter above is already widened for fixtures with old
    # timestamps.
    page.select_option("#filter-lane", "all")
    page.locator("#filter-terminal").check()
    page.wait_for_timeout(500)
    return errors


def _nodes(page: Page):
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('.node')).map(el => ({"
        "session_id: el.__data__.session_id,"
        "shape: el.querySelector('.node-shape').getAttribute('data-shape'),"
        "label: el.querySelector('text.node-label').textContent,"
        "}))"
    )


class TestLabelPrecedence:
    def test_linked_card_shows_the_card_title(self, page: Page, agents_base_url):
        # Long labels wrap across multiple <tspan> children (graph.js's
        # renderNodeLabel) without preserving the inter-word space, so
        # compare with whitespace collapsed rather than the raw textContent.
        errors = _open_agents(page, agents_base_url)
        rows = _nodes(page)
        by_id = {r["session_id"]: (r["label"] or "").replace(" ", "") for r in rows}
        assert by_id["cc:redesign-parent"] == "Fixtheflakygraphtest"
        assert errors == []

    def test_unlinked_session_falls_back_to_prompt_preview(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        rows = _nodes(page)
        by_id = {r["session_id"]: (r["label"] or "").replace(" ", "") for r in rows}
        assert by_id["cx:redesign-codex"] == "investigatethecodexregression".replace(" ", "")


class TestLegends:
    def test_lane_legend_lists_every_lane(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        text = page.locator("#graph-lane-legend").inner_text()
        for label in ["Unassigned", "Assigned", "In progress", "Human queue",
                      "Scheduled", "Review", "Done"]:
            assert label in text

    def test_engine_legend_names_all_five(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        text = page.locator("#graph-engine-legend").inner_text()
        for label in ["Claude Code", "Codex", "Hermes", "Local", "Claude"]:
            assert label in text

    def test_no_old_shape_only_legend_chip(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        assert page.locator(".chip.legend").count() == 0


class TestFiveEngineShapes:
    def test_five_distinct_shapes_render(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        rows = _nodes(page)
        shapes = {r["shape"] for r in rows}
        assert shapes == {"square", "hexagon", "star", "diamond", "circle"}

    def test_routing_change_on_existing_node_updates_its_shape(self, page: Page, agents_base_url):
        """`/snapshot` reports `routing: local` (a `<polygon>` diamond) for a
        session; a later `/stream` event reports `routing: claude_code` (a
        `<rect>` square) for the SAME session id — a tag-crossing transition
        (`polygon` -> `rect`), not just a different `data-shape` on the same
        tag. The `/stream` response is held back (not fulfilled) until the
        diamond has actually rendered, so this exercises the real d3 update
        path (an existing bound node changing shape) rather than racing
        `/snapshot` and `/stream` and asserting on whichever happens to
        finish last."""
        local_row = _row(session_id="sess-routing-change", routing="local", host="build-host",
                          label="Routing change target", model_label="Local", lane="in_progress")
        cc_row = dict(local_row, routing="claude_code", source="claude_code", model_label="Claude Code")
        snap_local = {"sessions": [local_row], "edges": [], "generated_at": 1, "api_host": "build-host"}
        snap_cc = {"sessions": [cc_row], "edges": [], "generated_at": 2, "api_host": "build-host"}
        stream_body = ": ok\n\nevent: snapshot\ndata: " + json.dumps(snap_cc) + "\n\n"

        pending_stream_route = {}

        def handler(route):
            url = route.request.url
            if "/api/agents/stream" in url:
                # Hold the response open — released explicitly below, once
                # the local/diamond render is confirmed on the page.
                pending_stream_route["route"] = route
                return
            if "/api/agents/snapshot" in url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(snap_local))
                return
            if "/api/agents/board" in url:
                route.fulfill(status=200, content_type="application/json",
                               body=json.dumps({"lanes": {}, "generated_at": 0}))
                return
            route.fulfill(status=200, content_type="application/json", body="{}")

        page.set_viewport_size({"width": 1280, "height": 800})
        page.route("**/api/**", handler)
        page.goto(f"{agents_base_url}/agents")
        page.click('[data-tab="graph"]')
        page.wait_for_selector("#filter-route")
        page.select_option("#filter-recency", "all")

        page.wait_for_function(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-routing-change');"
            " return g && g.querySelector('.node-shape').getAttribute('data-shape') === 'diamond'; }",
            timeout=8000,
        )
        initial = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-routing-change');"
            " return g.querySelector('.node-shape').tagName.toLowerCase(); }"
        )
        assert initial == "polygon"

        pending_stream_route["route"].fulfill(
            status=200, content_type="text/event-stream", body=stream_body)

        page.wait_for_function(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-routing-change');"
            " return g && g.querySelector('.node-shape').getAttribute('data-shape') === 'square'; }",
            timeout=8000,
        )
        result = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-routing-change');"
            " const el = g.querySelector('.node-shape');"
            " return { tag: el.tagName.toLowerCase(), shape: el.getAttribute('data-shape'),"
            " width: el.getAttribute('width') }; }"
        )
        assert result["tag"] == "rect"
        assert result["shape"] == "square"
        assert result["width"] is not None and float(result["width"]) > 0


class TestBadges:
    def test_pending_question_badge_present_for_hermes_row(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        node = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-hermes');"
            " const b = g.querySelector('.node-badge-question');"
            " return b ? getComputedStyle(b).display : null; }"
        )
        assert node != "none"

    def test_no_pending_question_badge_for_local_row(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        node = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-local');"
            " const b = g.querySelector('.node-badge-question');"
            " return b ? getComputedStyle(b).display : null; }"
        )
        assert node == "none"

    def test_error_count_badge_present(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        text = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cx:redesign-codex');"
            " return g.querySelector('.node-badge-errors').textContent; }"
        )
        assert text == "2"


class TestHostColumns:
    def test_host_column_headers_show_name_and_count(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        texts = page.eval_on_selector_all(
            "text.host-column-label", "els => els.map(e => e.textContent)"
        )
        assert "studio-host · 2" in texts
        assert "build-host · 4" in texts


class TestSubagentCollapse:
    def test_subagent_hidden_by_default(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        rows = _nodes(page)
        ids = {r["session_id"] for r in rows}
        assert "cc:redesign-subagent" not in ids
        assert "cc:redesign-parent" in ids

    def test_parent_badge_shows_hidden_count(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        text = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cc:redesign-parent');"
            " return g.querySelector('.node-badge-children').textContent; }"
        )
        assert text == "+1"

    def _badge_center(self, page: Page):
        return page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cc:redesign-parent');"
            " const b = g.querySelector('.node-badge-children');"
            " const box = b.getBoundingClientRect();"
            " return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }"
        )

    def test_click_badge_expands_then_collapses(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        badge = self._badge_center(page)
        page.mouse.click(badge["x"], badge["y"])
        page.wait_for_timeout(300)
        ids = {r["session_id"] for r in _nodes(page)}
        assert "cc:redesign-subagent" in ids
        # Clicking the badge must not also open the side panel.
        expect(page.locator("#panel-empty")).to_be_visible()

        # The simulation reheats on expand (the visible-id set changed) and
        # may have moved the badge — recompute its position rather than
        # reusing stale coordinates for the second click.
        badge = self._badge_center(page)
        page.mouse.click(badge["x"], badge["y"])
        page.wait_for_timeout(300)
        ids = {r["session_id"] for r in _nodes(page)}
        assert "cc:redesign-subagent" not in ids

    def test_click_8px_off_badge_center_still_toggles(self, page: Page, agents_base_url):
        # The clickable hit target (`.node-badge-children-hit`) is a
        # transparent circle sized well beyond the tiny `+N`/`−` text glyph
        # itself, so a click near but not exactly on the glyph still toggles
        # collapse/expand.
        _open_agents(page, agents_base_url)
        hit = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cc:redesign-parent');"
            " const b = g.querySelector('.node-badge-children-hit');"
            " const box = b.getBoundingClientRect();"
            " return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }"
        )
        page.mouse.click(hit["x"] + 8, hit["y"])
        page.wait_for_timeout(300)
        ids = {r["session_id"] for r in _nodes(page)}
        assert "cc:redesign-subagent" in ids


class TestHoverCard:
    def test_hover_shows_card_quickly_and_hides_on_mouseout(self, page: Page, agents_base_url):
        # `mouseenter`/`mousemove`/`mouseleave` are attached directly on the
        # `.node` group (graph.js's `entered.on(...)` chain) and don't
        # bubble — dispatch on that element itself, not a descendant found
        # via `elementFromPoint`, which would silently miss the listener.
        _open_agents(page, agents_base_url)
        result = page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess-hermes');
                const box = g.getBoundingClientRect();
                const clientX = box.x + box.width / 2, clientY = box.y + box.height / 2;
                const t0 = performance.now();
                g.dispatchEvent(new MouseEvent('mouseenter', { clientX, clientY }));
                const card = document.getElementById('graph-hover-card');
                const t1 = performance.now();
                return { elapsed: t1 - t0, hidden: card.hidden, text: card.innerText };
            }"""
        )
        assert result["hidden"] is False
        assert result["elapsed"] < 150
        assert "Deploy the nightly build" in result["text"]

        hidden = page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess-hermes');
                g.dispatchEvent(new MouseEvent('mouseleave', {}));
                return document.getElementById('graph-hover-card').hidden;
            }"""
        )
        assert hidden is True

    def test_hover_card_clamped_near_bottom_right_of_viewport(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        result = page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess-hermes');
                g.dispatchEvent(new MouseEvent('mouseenter', { clientX: 1270, clientY: 790 }));
                const box = document.getElementById('graph-hover-card').getBoundingClientRect();
                return { right: box.right, bottom: box.bottom };
            }"""
        )
        viewport = page.viewport_size
        assert result["right"] <= viewport["width"]
        assert result["bottom"] <= viewport["height"]


class TestClickAndDoubleClick:
    def test_single_click_opens_panel_synchronously(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        opened = page.evaluate(
            """() => new Promise(resolve => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess-local');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                requestAnimationFrame(() => {
                    const label = document.querySelector('[data-field="label"]');
                    resolve(!!label && label.textContent.includes('Local blocked task'));
                });
            })"""
        )
        assert opened is True

    def test_double_click_cli_node_focuses_exactly_once(self, page: Page, agents_base_url):
        focus_calls = []
        _open_agents(page, agents_base_url, focus_calls=focus_calls)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'cc:redesign-parent');
                g.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(300)
        assert len(focus_calls) == 1
        assert "cc%3Aredesign-parent" in focus_calls[0] or "cc:redesign-parent" in focus_calls[0]

    def test_double_click_subagent_does_not_focus(self, page: Page, agents_base_url):
        focus_calls = []
        _open_agents(page, agents_base_url, focus_calls=focus_calls)
        # Expand the parent so the subagent node exists to double-click.
        badge = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cc:redesign-parent');"
            " const b = g.querySelector('.node-badge-children');"
            " const box = b.getBoundingClientRect();"
            " return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }"
        )
        page.mouse.click(badge["x"], badge["y"])
        page.wait_for_timeout(300)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'cc:redesign-subagent');
                g.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(300)
        assert focus_calls == []

    def test_double_click_routing_derived_subagent_does_not_focus(self, page: Page, agents_base_url):
        """A row surfaced by `_session_to_dict` on the server carries no
        `is_subagent` key at all (only rows this test suite's own fixtures
        set it on do) — the dblclick guard must still catch it via
        `parent_session_id` alone."""
        child = _row(session_id="cc:routing-derived-child", routing="claude_code",
                      source="claude_code", host="build-host",
                      label="Routing-derived child", parent_session_id="cc:redesign-parent")
        del child["is_subagent"]
        snapshot = dict(SNAPSHOT, sessions=SNAPSHOT["sessions"] + [child])
        focus_calls = []
        _open_agents(page, agents_base_url, snapshot=snapshot, focus_calls=focus_calls)
        # Expand the parent so this child node renders (collapsed by default).
        badge = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cc:redesign-parent');"
            " const b = g.querySelector('.node-badge-children');"
            " const box = b.getBoundingClientRect();"
            " return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }"
        )
        page.mouse.click(badge["x"], badge["y"])
        page.wait_for_timeout(300)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'cc:routing-derived-child');
                g.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(300)
        assert focus_calls == []

    def test_real_doubleclick_cli_node_opens_panel_once_and_focuses_once(self, page: Page, agents_base_url):
        """A browser-emitted double-click (not a synthetic single `dblclick`
        dispatch) fires click(detail=1), click(detail=2), dblclick in
        sequence — the panel must open from the first click and stay open
        (not toggle closed by the second), focus must fire exactly once,
        and the zoom transform must be untouched by d3's own double-click
        zoom, which is disabled."""
        focus_calls = []
        _open_agents(page, agents_base_url, focus_calls=focus_calls)
        k_before = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        pos = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cc:redesign-parent');"
            " const box = g.getBoundingClientRect();"
            " return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }"
        )
        page.mouse.dblclick(pos["x"], pos["y"])
        page.wait_for_timeout(400)
        header = page.locator('[data-field="label"]')
        expect(header).to_be_visible()
        assert "flaky graph test" in header.inner_text()
        assert len(focus_calls) == 1
        k_after = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        assert abs(k_after - k_before) < 0.01

    def test_double_click_on_already_selected_cli_node_reopens_panel(self, page: Page, agents_base_url):
        """A prior single click already selected and opened the panel for
        this node (outside any double-click window). A later double-click's
        own first click (`detail === 1`) then toggles that already-selected
        node's panel closed before `dblclick` ever fires — the panel must
        come back open for this session once `dblclick` runs, not stay
        closed while only focus fires."""
        focus_calls = []
        _open_agents(page, agents_base_url, focus_calls=focus_calls)
        pos = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'cc:redesign-parent');"
            " const box = g.getBoundingClientRect();"
            " return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }"
        )
        page.mouse.click(pos["x"], pos["y"])
        page.wait_for_timeout(600)
        header = page.locator('[data-field="label"]')
        expect(header).to_be_visible()
        assert "flaky graph test" in header.inner_text()

        page.mouse.dblclick(pos["x"], pos["y"])
        page.wait_for_timeout(400)
        header = page.locator('[data-field="label"]')
        expect(header).to_be_visible()
        assert "flaky graph test" in header.inner_text()
        assert len(focus_calls) == 1

    def test_real_doubleclick_non_cli_node_opens_panel_and_does_not_zoom(self, page: Page, agents_base_url):
        focus_calls = []
        _open_agents(page, agents_base_url, focus_calls=focus_calls)
        k_before = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        pos = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-local');"
            " const box = g.getBoundingClientRect();"
            " return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }"
        )
        page.mouse.dblclick(pos["x"], pos["y"])
        page.wait_for_timeout(400)
        header = page.locator('[data-field="label"]')
        expect(header).to_be_visible()
        assert "Local blocked task" in header.inner_text()
        assert focus_calls == []
        k_after = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        assert abs(k_after - k_before) < 0.01


class TestZoomControls:
    def test_fit_changes_zoom_k_and_reset_returns_to_one(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.click("#graph-zoom-fit")
        page.wait_for_timeout(400)
        k_after_fit = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        assert k_after_fit > 0

        # After Fit, the union of every visible node's client rect must lie
        # inside the svg's own client rect and cover a real majority of it
        # — not a small box tucked into a corner (the symptom of computing
        # scale/translate in viewBox units against a CSS-pixel client box).
        boxes = page.evaluate(
            """() => {
                const toBox = (r) => ({ left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                                          width: r.width, height: r.height });
                const svgBox = toBox(document.getElementById('graph-svg').getBoundingClientRect());
                const nodes = [...document.querySelectorAll('.node')];
                let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
                for (const n of nodes) {
                    const r = n.getBoundingClientRect();
                    minX = Math.min(minX, r.left); maxX = Math.max(maxX, r.right);
                    minY = Math.min(minY, r.top); maxY = Math.max(maxY, r.bottom);
                }
                return { svg: svgBox, union: { left: minX, right: maxX, top: minY, bottom: maxY } };
            }"""
        )
        svg = boxes["svg"]
        union = boxes["union"]
        slack = 5  # anti-aliasing / sub-pixel rounding
        assert union["left"] >= svg["left"] - slack
        assert union["top"] >= svg["top"] - slack
        assert union["right"] <= svg["right"] + slack
        assert union["bottom"] <= svg["bottom"] + slack
        span_w = union["right"] - union["left"]
        span_h = union["bottom"] - union["top"]
        assert span_w >= 0.6 * svg["width"] or span_h >= 0.6 * svg["height"]

        page.click("#graph-zoom-reset")
        page.wait_for_timeout(400)
        k_after_reset = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        assert abs(k_after_reset - 1.0) < 0.01


class TestStreamTickUpdatesNodeSize:
    def test_stream_delivered_active_seconds_renders_as_size(self, page: Page, agents_base_url):
        """`renderGraph` re-applies `nodeRadius` (from `total_active_seconds`)
        on every render, including one driven by a `/stream` tick — proven
        here by having `/snapshot` (the initial fetch) and `/stream` (the
        live tick graph.js reacts to on every subsequent update) disagree:
        `/snapshot` reports a small value, `/stream`'s one queued event
        reports a much larger one for the same session id. Only the
        stream-driven value can end up rendered, since nothing else in the
        page ever re-fetches `/snapshot`."""
        small_row = _row(session_id="sess-tick", routing="claude", host="build-host",
                          label="Tick target", model_label="Sonnet",
                          total_active_seconds=1, lane="in_progress")
        grown_row = dict(small_row)
        grown_row["total_active_seconds"] = 200000
        snap_small = {"sessions": [small_row], "edges": [], "generated_at": 1, "api_host": "build-host"}
        snap_grown = {"sessions": [grown_row], "edges": [], "generated_at": 2, "api_host": "build-host"}
        stream_body = ": ok\n\nevent: snapshot\ndata: " + json.dumps(snap_grown) + "\n\n"

        def handler(route):
            url = route.request.url
            if "/api/agents/stream" in url:
                route.fulfill(status=200, content_type="text/event-stream", body=stream_body)
                return
            if "/api/agents/snapshot" in url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(snap_small))
                return
            if "/api/agents/board" in url:
                route.fulfill(status=200, content_type="application/json",
                               body=json.dumps({"lanes": {}, "generated_at": 0}))
                return
            route.fulfill(status=200, content_type="application/json", body="{}")

        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.set_viewport_size({"width": 1280, "height": 800})
        page.route("**/api/**", handler)
        page.goto(f"{agents_base_url}/agents")
        page.click('[data-tab="graph"]')
        page.wait_for_selector("#filter-route")
        page.select_option("#filter-recency", "all")
        # Poll instead of a fixed sleep — how long the browser takes to
        # receive and parse the SSE body (one static chunk carrying two
        # queued events) varies with machine load; the assertion is about
        # the final rendered value, not about it appearing within a
        # specific window.
        radius_fn = (
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-tick');"
            " return g ? +g.querySelector('.node-shape').getAttribute('r') : null; }"
        )
        page.wait_for_function(f"() => {{ const r = ({radius_fn})(); return r !== null && r >= 35; }}", timeout=8000)
        r = page.evaluate(radius_fn)
        assert r is not None and r >= 35
        assert errors == []


class TestPanelRawIdGuardAndHermesBadge:
    def test_panel_header_for_raw_id_session_matches_node_label(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess_rawid_9f8e7d6c');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(200)
        header = page.locator('[data-field="label"]').inner_text()
        assert header != "sess_rawid_9f8e7d6c"

    def test_rename_input_is_empty_for_raw_id_session(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess_rawid_9f8e7d6c');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(200)
        page.click('[data-field="label"]')
        value = page.locator("#label-edit-input").input_value()
        assert value == ""

    def test_hermes_routing_badge_shows_reported_model(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess-hermes');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(200)
        routing_text = page.locator('[data-field="routing"]').inner_text()
        assert routing_text == "Hermes · deepseek-v3"

    def test_claude_code_routing_badge_is_plain_engine_name(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'cc:redesign-parent');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(200)
        routing_text = page.locator('[data-field="routing"]').inner_text()
        assert routing_text == "Claude Code"

    def test_claude_code_chips_row_does_not_duplicate_engine_name(self, page: Page, agents_base_url):
        # The engine name already renders once, in the Routing badge — the
        # chips row must not repeat it.
        _open_agents(page, agents_base_url)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'cc:redesign-parent');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(200)
        chips_text = page.locator('[data-field="panel-chips"]').inner_text()
        assert "Claude Code" not in chips_text

    def test_claude_chips_row_shows_model_without_separate_engine_chip(self, page: Page, agents_base_url):
        # routing "claude" with model_label "Sonnet": the Routing badge
        # reads "Claude" (routingLabel's fallback for the "claude" routing),
        # so a bare "Claude" engine chip would just repeat it — only the
        # more specific "Sonnet" model chip should render.
        _open_agents(page, agents_base_url)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess-claude-api');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(200)
        routing_text = page.locator('[data-field="routing"]').inner_text()
        assert routing_text == "Claude"
        chips_text = page.locator('[data-field="panel-chips"]').inner_text()
        assert "Sonnet" in chips_text
        assert "Claude" not in chips_text


class TestSearchUnknownFieldGuard:
    def test_unknown_field_match_is_skipped_summary_match_renders(self, page: Page, agents_base_url):
        matches = [
            {"session_id": "sess-hermes", "field": "constructor", "snippet": "junk"},
            {"session_id": "sess-local", "field": "summary", "snippet": "matched summary text"},
        ]
        _open_agents(page, agents_base_url, search_matches=matches)
        page.fill("#search-input", "matched")
        page.wait_for_timeout(400)
        rows = page.locator(".search-result")
        expect(rows).to_have_count(1)
        # `.sr-badge` renders uppercase via CSS `text-transform` — compare
        # the underlying text, not the rendered (CSS-transformed) case.
        assert rows.first.locator(".sr-badge").inner_text().lower() == "summary"
