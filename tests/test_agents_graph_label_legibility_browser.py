"""Browser test for the Graph tab's node-label legibility (web/agents/graph.js's
`updateLabelLegibility`).

A `.node-label`'s on-screen CSS pixel size is its font-size in the SVG's
own user-space units times BOTH the zoom transform's scale (`k`) AND the
ratio between the SVG element's actual rendered width and its `viewBox`
width — `#graph-svg` sits next to `#panel-outer` (the side panel), so that
ratio is well under 1 at any realistic viewport, not the ~1 a `k`-only
threshold would assume. `updateLabelLegibility` counter-scales each
label's own font-size to hold it at a legible on-screen floor across most
zoom levels, including the resting zoom (`k = 1`, what Reset restores);
only past a point where no reasonable font-size compensates without the
labels overlapping and cluttering a dense, zoomed-far-out graph does it
hide them instead, relying on the hover card (already independent of a
node's own DOM label) to name a node.

Serves `web/` itself on an ephemeral port and stubs every `/api/` call —
the same server-free pattern as
tests/test_agents_graph_redesign_ui_browser.py — so this carries no
`requires_server` marker and runs at pre-push (`browser and not
requires_server`). `/agents` loads d3 from
`https://d3js.org/d3.v7.min.js`, left unstubbed (real network), same as
every other test in this family — the force simulation and zoom-to-fit's
bounding-box math both need the real thing.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# A shown label never renders below about this many CSS pixels on screen,
# at any zoom including the resting one. A small epsilon absorbs
# floating-point noise in the font-size/CTM arithmetic, not a relaxation
# of the requirement.
LABEL_MIN_SCREEN_PX = 11
_EPSILON = 0.05


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


def _row(i):
    # Every node shares the same host column and lane band — `columnTargetX`
    # and `laneTargetY` (web/agents/graph.js) pull the whole graph toward
    # ONE cell, so with many nodes the collision force (which keeps
    # same-cell nodes from overlapping — sized off each node's actual
    # label width) is what spreads the bounding box far beyond a single
    # column/lane's nominal share of the viewBox, the way a real board
    # with most of its sessions crowded into one lane on one host does.
    return {
        "session_id": f"sess-legibility-{i}",
        "task_id": None,
        "status": "running",
        "status_inferred": False,
        "routing": "local",
        "source": "lifeos_agent",
        "host": "host-0",
        "parent_session_id": None,
        "is_subagent": False,
        "started_at": 1000,
        "last_activity_at": 2000,
        "total_input_tokens": 5,
        "total_output_tokens": 5,
        "total_dollars": 0.01,
        "total_active_seconds": 60,
        "spawn_depth": 0,
        "label": f"Synthetic legibility node {i}",
        "model_label": "Local",
        "tool_call_count": 0,
        "error_count": 0,
        "lane": "in_progress",
        "pending_question": None,
        "decoded_cwd": "/home/synthetic/proj",
    }


def _snapshot(n):
    return {
        "sessions": [_row(i) for i in range(n)],
        "edges": [],
        "generated_at": 1234567890,
        "api_host": "host-0",
    }


SNAPSHOT_100 = _snapshot(100)


def _make_handler(snapshot):
    def handler(route):
        url = route.request.url
        if "/stream" in url and "/sessions/" not in url:
            route.fulfill(status=200, content_type="text/event-stream", body="")
            return
        if "/api/agents/snapshot" in url:
            body = snapshot
        elif "/api/agents/board" in url:
            body = {"lanes": {}, "generated_at": 0}
        elif "/api/agents/search" in url:
            body = {"query": "", "matches": []}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return handler


def _open_agents(page: Page, base_url, snapshot=None, width=1280, height=800):
    page.set_viewport_size({"width": width, "height": height})
    page.route("**/api/**", _make_handler(snapshot or SNAPSHOT_100))
    page.goto(f"{base_url}/agents")
    page.click('[data-tab="graph"]')
    page.wait_for_selector("#filter-route")
    page.select_option("#filter-recency", "all")
    page.locator("#filter-terminal").check()
    # Let the force simulation settle enough for zoom-to-fit's own bounding
    # box (computed from live node `x`/`y`) to reflect the converged, not
    # still-collapsing-from-center, layout.
    page.wait_for_timeout(1200)


def _label_display_states(page: Page):
    """The COMPUTED `display` of every rendered `.node-label` — the actual
    effect of the CSS class `updateLabelLegibility` toggles, not the class
    name or a CSS attribute that might not be applied."""
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('#graph-svg .node-label'))"
        ".map(el => getComputedStyle(el).display)"
    )


def _label_screen_sizes(page: Page):
    """The on-screen CSS-pixel size of every SHOWN `.node-label` — computed
    font-size times the label's own `getScreenCTM().a`, which folds in
    both the zoom transform's scale and the SVG element's own
    viewBox-to-rendered-width ratio. This is the quantity legibility
    actually depends on; `display` alone only proves a label is present,
    not that it's readable, and the d3 zoom scale `k` alone ignores the
    viewBox ratio entirely (see this file's module docstring)."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#graph-svg .node-label'))
        .filter(el => getComputedStyle(el).display !== 'none')
        .map(el => parseFloat(getComputedStyle(el).fontSize) * el.getScreenCTM().a)
        """
    )


def _label_line_ratios(page: Page):
    """For every multi-line SHOWN `.node-label`, the ratio of its
    consecutive-line baseline-to-baseline screen-px gap to its own
    screen-px font size. `graph.js` ties per-line spacing to
    `LABEL_LINE_H / LABEL_BASE_FONT_PX` (13/12 ≈ 1.083) of the CURRENT
    (possibly boosted) font size, so this ratio should hold regardless of
    boost; a line spacing left at its unboosted 12px-era value while the
    font itself grows would read far below that once any boost is in
    effect."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#graph-svg .node-label'))
        .filter(el => getComputedStyle(el).display !== 'none')
        .map(el => {
          const fontPx = parseFloat(getComputedStyle(el).fontSize) * el.getScreenCTM().a;
          const tops = Array.from(el.querySelectorAll('tspan')).map(t => t.getBoundingClientRect().top);
          const gaps = [];
          for (let i = 1; i < tops.length; i++) gaps.push((tops[i] - tops[i - 1]) / fontPx);
          return gaps;
        })
        .filter(gaps => gaps.length > 0)
        """
    )


def _label_bounding_rects(page: Page):
    """The axis-aligned screen-px bounding rect of every SHOWN
    `.node-label` — for detecting neighbouring nodes' labels overlapping
    each other, independent of the within-label line spacing
    `_label_line_ratios` checks."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#graph-svg .node-label'))
        .filter(el => getComputedStyle(el).display !== 'none')
        .map(el => {
          const r = el.getBoundingClientRect();
          return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        })
        """
    )


def _rects_overlap(a, b):
    return a["left"] < b["right"] and b["left"] < a["right"] and a["top"] < b["bottom"] and b["top"] < a["bottom"]


def _count_overlapping_pairs(rects):
    count = 0
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _rects_overlap(rects[i], rects[j]):
                count += 1
    return count


class TestLabelLegibilityAtZoomToFit:
    def test_labels_meet_legibility_floor_at_default_zoom(self, page: Page, agents_base_url):
        """The resting zoom (`k = 1`, what Reset restores) is where the
        SVG's own viewBox-to-rendered-width ratio dominates a `k`-only
        threshold, at ~6.9 CSS px on a 1280×800 viewport regardless of
        node count. Every shown label must clear the legibility floor
        here, not just be `display !== 'none'`."""
        _open_agents(page, agents_base_url)
        sizes = _label_screen_sizes(page)
        assert sizes, "no node labels rendered at all"
        assert all(px >= LABEL_MIN_SCREEN_PX - _EPSILON for px in sizes), sizes

    def test_labels_meet_legibility_floor_after_fit_with_moderate_graph(self, page: Page, agents_base_url):
        """A moderate, realistic node count (well under the density that
        forces a hide) must still clear the legibility floor after Fit —
        `zoomFit`'s own `k` doesn't drop far at this density, so a
        threshold based on `k` alone would leave these labels both small
        and shown."""
        _open_agents(page, agents_base_url, snapshot=_snapshot(19))
        page.click("#graph-zoom-fit")
        page.wait_for_timeout(600)
        k = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        sizes = _label_screen_sizes(page)
        assert sizes, f"no node labels shown after Fit (k={k})"
        assert all(px >= LABEL_MIN_SCREEN_PX - _EPSILON for px in sizes), (k, sizes)

    def test_labels_hidden_at_zoom_to_fit_with_100_nodes(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.click("#graph-zoom-fit")
        page.wait_for_timeout(500)
        k = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        assert k < 1.0, f"zoom-to-fit did not zoom out for 100 nodes (k={k})"

        states = _label_display_states(page)
        assert states, "no node labels rendered at all"
        assert all(s == "none" for s in states), states

    def test_labels_reappear_after_reset(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.click("#graph-zoom-fit")
        page.wait_for_timeout(500)
        assert all(s == "none" for s in _label_display_states(page))

        page.click("#graph-zoom-reset")
        page.wait_for_timeout(500)
        k = float(page.get_attribute("#graph-svg", "data-zoom-k"))
        assert abs(k - 1.0) < 0.01

        states = _label_display_states(page)
        assert states, "no node labels rendered at all"
        assert all(s != "none" for s in states), states

        # Reappearing is necessary but not sufficient — Reset must also
        # land back at a legible size, not just a shown one.
        sizes = _label_screen_sizes(page)
        assert sizes, "no node labels shown after Reset"
        assert all(px >= LABEL_MIN_SCREEN_PX - _EPSILON for px in sizes), sizes

    def test_hover_card_still_names_a_node_at_zoom_to_fit(self, page: Page, agents_base_url):
        """Labels are hidden, not the nodes themselves or their data — the
        hover card (already independent of the node's own DOM label —
        `showHoverCard` reads `nodeLabel(d)` directly) still names whichever
        node the operator points at."""
        _open_agents(page, agents_base_url)
        page.click("#graph-zoom-fit")
        page.wait_for_timeout(500)
        assert all(s == "none" for s in _label_display_states(page))

        result = page.evaluate(
            """() => {
                const g = document.querySelector('.node');
                const box = g.getBoundingClientRect();
                const clientX = box.x + box.width / 2, clientY = box.y + box.height / 2;
                g.dispatchEvent(new MouseEvent('mouseenter', { clientX, clientY }));
                const card = document.getElementById('graph-hover-card');
                return { hidden: card.hidden, text: card.innerText, sessionId: g.__data__.session_id };
            }"""
        )
        assert result["hidden"] is False
        expected_i = result["sessionId"].rsplit("-", 1)[-1]
        assert f"Synthetic legibility node {expected_i}" in result["text"], result


class TestLabelLegibilityMatrix:
    """The on-screen floor, per-line spacing, and neighbour-label collision
    must all hold together across a range of node counts and viewports —
    including two height-bound viewports (1280×650, 1920×500) where
    `#graph-svg`'s `preserveAspectRatio="xMidYMid meet"` fit makes the
    height ratio, not the width ratio, the binding one, and so requires a
    larger font boost than a width-bound 1280×800 viewport ever exercises.
    A font boost that isn't matched by a proportional boost to line
    spacing and the collision-force radius makes a multi-line label's own
    lines overlap and neighbouring nodes' labels collide with each other."""

    @pytest.mark.parametrize("width,height", [(1280, 800), (1280, 650), (1920, 500)])
    @pytest.mark.parametrize("n", [8, 20, 40])
    def test_floor_line_spacing_and_collision_hold_at_rest(self, page: Page, agents_base_url, n, width, height):
        _open_agents(page, agents_base_url, snapshot=_snapshot(n), width=width, height=height)

        sizes = _label_screen_sizes(page)
        assert sizes, f"no node labels shown at {width}x{height}, n={n}"
        assert all(px >= LABEL_MIN_SCREEN_PX - _EPSILON for px in sizes), (width, height, n, sizes)

        base_ratio = 13 / 12  # LABEL_LINE_H / LABEL_BASE_FONT_PX
        ratio_groups = _label_line_ratios(page)
        assert ratio_groups, f"no multi-line labels rendered at {width}x{height}, n={n}"
        for gaps in ratio_groups:
            for r in gaps:
                assert abs(r - base_ratio) < base_ratio * 0.2, (width, height, n, r, ratio_groups)

        rects = _label_bounding_rects(page)
        overlaps = _count_overlapping_pairs(rects)
        assert overlaps == 0, f"{overlaps} overlapping label pairs at {width}x{height}, n={n}"
