"""Server-free browser test for the Graph tab's threshold auto-selection
(#896 review round 3, blocker 1).

`calculateOptimalEdgeThreshold()` used to pick a threshold that hid every
first-degree node whenever every first-degree edge shared exactly one
weight: forcing a minimum weightRange of 1 when maxWeight == minWeight made
even the first non-zero sweep step (5%) exclude every edge. Bounding the
returned edge set (#870/#896) made every returned edge cluster into a
narrower set of weight values, so this pre-existing bug started firing for
a real, common shape of response instead of a rare one -- a real person's
Graph tab loaded and immediately showed "No connections match current
filters" with zero user interaction.

Unlike the rest of the browser suite this serves `web/` itself from an
ephemeral port rather than pointing at a running API, because the
assertion is about the JS in *this* checkout and every API call the page
makes is intercepted anyway. That is why it carries no `requires_server`
marker, and so runs at pre-push (`browser and not requires_server`). Keep
it that way -- reaching for a live server here would silently drop this
regression from the push gate.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CENTER_ID = "flat-weight-center"


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the CRM SPA the way api/main.py does: any /crm/* path returns
    crm.html; everything else falls through to web/ as static files."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path == "/" or path.startswith("/crm"):
            return str(WEB_DIR / "crm.html")
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def crm_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CrmHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _flat_weight_network_fixture(node_count=149):
    """A `GET /api/crm/network` response shaped like the real one that
    triggered this bug: more first-degree nodes than the ~25-node display
    target, every one of them connected by an edge of the exact same
    weight (a common case once the network-graph endpoint bounds edges to
    the strongest N by weight -- ties at the boundary are common)."""
    nodes = [{
        "id": CENTER_ID, "name": "Center", "category": "personal",
        "strength": 50, "interaction_count": 10, "degree": 0,
    }]
    edges = []
    for i in range(node_count):
        node_id = f"n{i}"
        nodes.append({
            "id": node_id, "name": f"Person {i}", "category": "personal",
            "strength": 50, "interaction_count": 5, "degree": 1,
        })
        edges.append({
            "source": CENTER_ID, "target": node_id, "weight": 9, "type": "inferred",
            "shared_events_count": 1, "shared_threads_count": 0, "shared_messages_count": 0,
            "shared_whatsapp_count": 0, "shared_slack_count": 0, "shared_phone_calls_count": 0,
            "shared_photos_count": 0, "is_linkedin_connection": False,
        })
    return {"nodes": nodes, "edges": edges}


def test_flat_weight_graph_still_renders_nodes(page: Page, crm_base_url):
    """When every first-degree edge shares one weight, the auto-selected
    threshold must not hide every node."""
    fixture = _flat_weight_network_fixture()

    def handler(route):
        if "/api/crm/network" in route.request.url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(fixture))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handler)
    page.goto(f"{crm_base_url}/crm/{CENTER_ID}/graph")
    page.wait_for_selector("#graphContainer")

    # Drive the graph tab's real loading path directly (this test is only
    # about calculateOptimalEdgeThreshold()'s interaction with the fixture
    # above; it doesn't exercise the rest of the SPA's bootstrap -
    # config/people-list/stats - which init() would otherwise need stubbed
    # in full to complete without errors).
    page.evaluate("(id) => window.loadGraph(id)", CENTER_ID)
    page.wait_for_timeout(300)

    circle_count = page.locator("#graphContainer svg circle").count()
    empty_state_visible = page.locator("#graphContainer .empty-state").count() > 0

    assert circle_count > 0, (
        "Graph tab rendered zero nodes for a flat-weight response "
        f"(empty-state shown: {empty_state_visible})"
    )
    # The center plus at least a handful of first-degree nodes - not just
    # the bare minimum of one circle.
    assert circle_count >= 10


def test_calculate_optimal_edge_threshold_flat_weight_returns_zero(page: Page, crm_base_url):
    """Direct unit-style check on the function itself, independent of the
    rendering pipeline: a flat-weight edge set (more first-degree nodes
    than the target) must resolve to threshold 0, not a percent that
    excludes every edge."""
    page.goto(f"{crm_base_url}/crm/{CENTER_ID}/graph")
    page.wait_for_function("typeof window.calculateOptimalEdgeThreshold === 'function'")

    threshold = page.evaluate("""
        () => {
            const links = [];
            for (let i = 0; i < 149; i++) {
                links.push({ source: 'center', target: 'n' + i, weight: 9 });
            }
            return window.calculateOptimalEdgeThreshold([], links, 'center', 25);
        }
    """)

    assert threshold == 0

    # Sanity: a varied weight distribution over the same shape still picks
    # a non-trivial threshold - the fix isn't just "always return 0".
    varied_threshold = page.evaluate("""
        () => {
            const links = [];
            for (let i = 0; i < 149; i++) {
                links.push({ source: 'center', target: 'n' + i, weight: i % 100 });
            }
            return window.calculateOptimalEdgeThreshold([], links, 'center', 25);
        }
    """)
    assert varied_threshold > 0
