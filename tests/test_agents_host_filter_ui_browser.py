"""Browser test for the /agents host filter + side panel fields (#849).

Serves `web/` itself on an ephemeral port and stubs every `/api/` call, the
same server-free pattern as `tests/test_voice_mic_block_ui_browser.py` —
the assertions are about the JS in *this* checkout, and `/api/agents/snapshot`
is the only response that matters, so a live LifeOS API isn't needed. That is
why this carries no `requires_server` marker and runs at pre-push
(`browser and not requires_server`).

`/agents` loads d3 from a CDN (`https://d3js.org/d3.v7.min.js`) — only
`/api/**` is intercepted here (same as the voice test), so that request goes
to the real network like it does for every other browser test against this
page family.

#850 made the Kanban board the default /agents view and moved this graph
(unchanged) behind a Graph tab, lazily initialized on first open — so every
scenario here clicks into the Graph tab before touching graph-specific
elements like `#filter-host` or `.node`, which now live in initially-hidden
markup and aren't wired up until `initGraph()` runs.

`TestRecentChipAndRouteFilterAndNodeLabels` covers the "recent" chip
(completed + ended), the route filter's `hermes`/`ask` options, and that no
rendered node label is a literal '?' — using a second synthetic snapshot
(`SNAPSHOT2`) on a single host so the host filter doesn't interfere.
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
    """Serves agents.html the way api/main.py does: `/agents` is agents.html
    and the web/agents/*.js modules (#850) hang off `/static/`."""

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


# Two sessions on two different hosts — one carries branch + prompt_preview
# (a cli_sessions row merged onto/registered without a local transcript),
# the other is a bare local session so the "single host" collapse case
# (host filter hidden) isn't accidentally exercised by every test.
SNAPSHOT = {
    "sessions": [
        {
            "session_id": "cc:host-filter-target",
            "task_id": "t1",
            "status": "running",
            "status_inferred": False,
            "routing": "claude_code",
            "source": "claude_code",
            "host": "laptop-a",
            "branch": "feat/synthetic-branch",
            "prompt_preview": "refactor the synthetic widget",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 10,
            "total_output_tokens": 20,
            "total_dollars": 0.01,
            "spawn_depth": 0,
            "label": "hosttest-alpha-session",
            "model_label": "Sonnet",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            "session_id": "sess_worker_on_apihost",
            "task_id": "t2",
            "status": "running",
            "status_inferred": False,
            "routing": "local",
            "source": "lifeos_agent",
            "host": "api-host",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 5,
            "total_output_tokens": 5,
            "total_dollars": 0.0,
            "spawn_depth": 0,
            "label": "hosttest-beta-session",
            "model_label": "Local",
            "decoded_cwd": "/home/synthetic/proj-b",
        },
        # A cli_sessions row whose SessionEnd event already fired — must be
        # treated as terminal (hidden by default, shown with "include
        # finished" on) rather than staying in the live view forever (#849
        # round-1 finding: 'ended' was missing from the frontend TERMINAL set).
        {
            "session_id": "cc:host-filter-ended",
            "task_id": "t3",
            "status": "ended",
            "status_inferred": False,
            "routing": "claude_code",
            "source": "claude_code",
            "host": "laptop-a",
            "branch": "feat/synthetic-branch",
            "prompt_preview": "wrap up the synthetic widget",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 10,
            "total_output_tokens": 20,
            "total_dollars": 0.01,
            "spawn_depth": 0,
            "label": "hosttest-gamma-session",
            "model_label": "Sonnet",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
    ],
    "edges": [],
    "generated_at": 1234567890,
}


def _open_agents(page: Page, base_url):
    def handler(route):
        if "/api/agents/snapshot" in route.request.url:
            body = SNAPSHOT
        elif "/api/agents/board" in route.request.url:
            body = {"lanes": {}, "generated_at": 0}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/agents")
    # #850: the board is the default tab; the graph (and its filters) live
    # behind the Graph tab and only initialize once it's opened.
    page.click('[data-tab="graph"]')
    page.wait_for_selector("#filter-host")


class TestHostFilter:
    def test_filter_host_lists_every_host_from_the_snapshot(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        select = page.locator("#filter-host")
        options = select.locator("option").all_inner_texts()
        assert "all" in options
        assert "laptop-a" in options
        assert "api-host" in options

    def test_selecting_a_host_hides_sessions_from_other_hosts(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        # Both sessions are recent enough to be visible under the default
        # 30-minute recency filter only if last_activity_at is "now" — the
        # fixture's timestamps are fixed epoch seconds in the past, so
        # widen recency to "all time" first.
        page.select_option("#filter-recency", "all")
        page.select_option("#filter-host", "laptop-a")
        page.wait_for_timeout(400)  # let the filtered re-render settle
        nodes = page.locator(".node")
        expect(nodes).to_have_count(1)


class TestEndedStatus:
    # The fixture has 3 sessions total: two 'running' and one 'ended'
    # (cc:host-filter-ended). 'ended' must be treated as terminal — hidden
    # by default and only shown once "include finished" is checked (#849
    # round-1 finding: 'ended' was missing from the frontend TERMINAL set,
    # so a closed CLI session stayed in the live view indefinitely).
    def test_ended_session_hidden_by_default(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.wait_for_timeout(400)
        expect(page.locator(".node")).to_have_count(2)

    def test_ended_session_shown_when_include_finished_is_checked(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.locator("#filter-terminal").check()
        page.wait_for_timeout(400)
        expect(page.locator(".node")).to_have_count(3)


class TestPanelFields:
    def test_panel_shows_host_branch_and_prompt_preview(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.locator("#search-input").fill("hosttest-alpha-session")
        result = page.locator(".search-result", has_text="hosttest-alpha-session")
        expect(result).to_be_visible()
        result.click()

        panel = page.locator("#panel")
        expect(panel.locator('[data-field="host"]')).to_have_text("laptop-a")
        expect(panel.locator('[data-field="branch-hint"]')).to_contain_text("feat/synthetic-branch")
        expect(panel.locator('[data-field="prompt-preview-hint"]')).to_contain_text(
            "refactor the synthetic widget")

    def test_panel_omits_branch_and_prompt_fields_when_absent(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.locator("#search-input").fill("hosttest-beta-session")
        result = page.locator(".search-result", has_text="hosttest-beta-session")
        expect(result).to_be_visible()
        result.click()

        panel = page.locator("#panel")
        expect(panel.locator('[data-field="host"]')).to_have_text("api-host")
        expect(panel.locator('[data-field="branch-hint"]')).to_have_count(0)
        expect(panel.locator('[data-field="prompt-preview-hint"]')).to_have_count(0)


# ---------------------------------------------------------------------------
# Covers: the recent chip (completed + ended), the route filter's hermes/ask
# options, and the graph's node label precedence (never a literal '?').
# Same server-free pattern as above: everything on one host so the host
# filter doesn't interfere with these assertions, a mix of routing/status
# values, and a CLI row with no label/short_label/prompt_preview at all —
# the shape that would otherwise render a literal '?'.
# ---------------------------------------------------------------------------

SNAPSHOT2 = {
    "sessions": [
        {
            "session_id": "sess_label_completed",
            "task_id": "t-completed",
            "status": "completed",
            "status_inferred": False,
            "routing": "local",
            "source": "lifeos_agent",
            "host": "host-1",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 5,
            "total_output_tokens": 5,
            "total_dollars": 0.0,
            "spawn_depth": 0,
            "label": "labeltest-alpha-completed",
            "model_label": "Local",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            # Shape a locally-scanned CC session actually ingests as: no
            # custom title / AI title / user text / cwd basename, so
            # `claude_code/session_ingest.py` falls `meta.label` back to the
            # bare raw session id, while `session_id` carries the "cc:"
            # prefix (`CC_PREFIX + raw_id`); the ingest always supplies a
            # `label`, never omits it. This row also has a real
            # `prompt_preview` — the node label must prefer that over the
            # raw-id `label` (leak shape 1 below), and with neither present
            # it must still never fall through to a literal '?'.
            #
            # `short_label` also carries the raw id here — exactly what
            # `_fallback_label` caches when a terminal CLI session has no
            # real content — to prove `short_label` gets the same raw-id
            # guard as `label` rather than leaking through one precedence
            # slot higher.
            "session_id": "cc:0e6b2c14-9f77-4a1e-8b55-3c2f9d10aa42",
            "task_id": None,
            "status": "ended",
            "status_inferred": False,
            "routing": "claude_code",
            "source": "claude_code",
            "host": "host-1",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 10,
            "total_output_tokens": 20,
            "total_dollars": 0.01,
            "spawn_depth": 0,
            "label": "0e6b2c14-9f77-4a1e-8b55-3c2f9d10aa42",
            "short_label": "0e6b2c14-9f77-4a1e-8b55-3c2f9d10aa42",
            "prompt_preview": "fix the synthetic widget parser",
            "model_label": "Claude Code",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            # An orphaned worker row: `_label_for_session` falls back
            # to `s.task_id` when the task lookup finds no description (e.g.
            # the vault task file was deleted while the `sessions` row
            # survived), so `label` equals `task_id` verbatim. That raw id
            # must not render as the node label (leak shape 2 below).
            #
            # `short_label` also equals `task_id` here, proving the guard
            # catches `short_label` == task_id, not just
            # `short_label` == session_id.
            "session_id": "sess_label_orphan",
            "task_id": "t-orphan-deleted",
            "status": "running",
            "status_inferred": False,
            "routing": "local",
            "source": "lifeos_agent",
            "host": "host-1",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_dollars": 0.0,
            "spawn_depth": 0,
            "label": "t-orphan-deleted",
            "short_label": "t-orphan-deleted",
            "model_label": "Local",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            "session_id": "sess_label_ask",
            "task_id": "t-ask",
            "status": "running",
            "status_inferred": False,
            "routing": "ask",
            "source": "lifeos_agent",
            "host": "host-1",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_dollars": 0.0,
            "spawn_depth": 0,
            "label": "labeltest-ask",
            "model_label": "Waiting on you",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            "session_id": "sess_label_hermes",
            "task_id": "t-hermes",
            "status": "running",
            "status_inferred": False,
            "routing": "hermes",
            "source": "lifeos_agent",
            "host": "host-1",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 1,
            "total_output_tokens": 1,
            "total_dollars": 0.001,
            "spawn_depth": 0,
            "label": "labeltest-hermes",
            "model_label": "Hermes",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            "session_id": "sess_label_remote",
            "task_id": "t-remote",
            "status": "running",
            "status_inferred": False,
            "routing": "remote",
            "source": "lifeos_agent",
            "host": "host-1",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 1,
            "total_output_tokens": 1,
            "total_dollars": 0.002,
            "spawn_depth": 0,
            "label": "labeltest-remote",
            "model_label": "Remote",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            # A CLI row whose real title is non-Latin (CJK here).
            # `_fallback_label` returns "" for a genuinely non-empty title
            # that tokenizes to zero ASCII words — returning "Untitled"
            # instead would sit ABOVE `label` in `nodeLabel`'s precedence
            # and clobber the real title on screen for every non-Latin
            # session. This row models the correct shape — empty
            # `short_label`, the real title in `label` — end to end through
            # the browser: the node must render the title, never
            # "Untitled" and never blank.
            "session_id": "cc:nonlatin-title-session",
            "task_id": None,
            "status": "running",
            "status_inferred": False,
            "routing": "claude_code",
            "source": "claude_code",
            "host": "host-1",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 3,
            "total_output_tokens": 6,
            "total_dollars": 0.001,
            "spawn_depth": 0,
            "label": "検索インデックスをリファクタリングする",
            "short_label": "",
            "model_label": "Claude Code",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
    ],
    "edges": [],
    "generated_at": 1234567890,
}


def _open_agents2(page: Page, base_url):
    def handler(route):
        if "/stream" in route.request.url:
            # graph.js opens an EventSource against /api/agents/stream — a
            # plain JSON stub response makes the browser log a real MIME-type
            # console error unrelated to anything under test here.
            route.fulfill(status=200, content_type="text/event-stream", body="")
            return
        if "/api/agents/snapshot" in route.request.url:
            body = SNAPSHOT2
        elif "/api/agents/board" in route.request.url:
            body = {"lanes": {}, "generated_at": 0}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/agents")
    page.click('[data-tab="graph"]')
    page.wait_for_selector("#filter-route")
    page.select_option("#filter-recency", "all")
    page.locator("#filter-terminal").check()
    page.wait_for_timeout(400)


class TestRecentChipAndRouteFilterAndNodeLabels:
    def test_recent_chip_counts_completed_and_ended(self, page: Page, agents_base_url):
        _open_agents2(page, agents_base_url)
        expect(page.locator("#chip-recent")).to_have_text("2")

    def test_route_filter_lists_hermes_remote_and_ask(self, page: Page, agents_base_url):
        _open_agents2(page, agents_base_url)
        options = page.locator("#filter-route option").all_inner_texts()
        assert "hermes" in options
        assert "remote" in options
        assert "ask" in options

    def test_route_filter_selecting_hermes_shows_only_hermes_node(self, page: Page, agents_base_url):
        _open_agents2(page, agents_base_url)
        page.select_option("#filter-route", "hermes")
        page.wait_for_timeout(400)
        expect(page.locator(".node")).to_have_count(1)

    def test_route_filter_selecting_ask_shows_only_ask_node(self, page: Page, agents_base_url):
        _open_agents2(page, agents_base_url)
        page.select_option("#filter-route", "ask")
        page.wait_for_timeout(400)
        expect(page.locator(".node")).to_have_count(1)

    def test_no_node_label_is_a_literal_question_mark(self, page: Page, agents_base_url):
        """The node label is an SVG `<text class="node-label">` whose text
        lives in appended `<tspan>` children (graph.js's `renderNodeLabel`).
        SVG `<text>` has no `innerText`, so Playwright's `all_inner_texts()`
        (which reads `innerText`) returns `None` for every element here,
        which would make this assertion vacuously true even against a
        `nodeLabel` that really does render a literal '?'.
        Read `textContent` instead, which SVG supports."""
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

        _open_agents2(page, agents_base_url)
        expect(page.locator(".node")).to_have_count(7)
        # Let the force-layout / label-render tick settle before reading text.
        page.wait_for_timeout(300)
        labels = page.eval_on_selector_all(
            "text.node-label", "els => els.map(e => e.textContent)"
        )
        assert len(labels) == 7
        assert all((label or "").strip() != "?" for label in labels)
        assert all((label or "").strip() != "" for label in labels)
        # Never a bare "Untitled" clobbering a real (non-Latin) title.
        assert all((label or "").strip() != "Untitled" for label in labels)
        assert errors == []

    def test_node_label_renders_a_non_latin_title_not_untitled(self, page: Page, agents_base_url):
        """`_fallback_label` must not return "Untitled" for a title that
        tokenizes to zero ASCII words — that fires for every non-Latin
        (CJK/Cyrillic/Greek/Arabic) or emoji-only title, not just a
        genuinely empty one, and since "Untitled" sits ABOVE `label` in
        `nodeLabel`'s precedence it would clobber the real title on screen.
        It returns "" instead, so the node falls through to `label` exactly
        as it already does for a raw-id `short_label`."""
        _open_agents2(page, agents_base_url)
        page.wait_for_timeout(300)
        rows = page.evaluate(
            "() => Array.from(document.querySelectorAll('.node')).map(el => ({"
            "session_id: el.__data__.session_id,"
            "label: el.querySelector('text.node-label').textContent,"
            "}))"
        )
        by_id = {r["session_id"]: r["label"] for r in rows}
        rendered = (by_id["cc:nonlatin-title-session"] or "").replace(" ", "")
        assert rendered == "検索インデックスをリファクタリングする".replace(" ", "")

    def test_node_label_prefers_prompt_preview_over_a_raw_id_label(self, page: Page, agents_base_url):
        """`nodeLabel`'s precedence guard compares equality against the
        session id with its "cc:"/"cx:" prefix stripped, rather than
        `session_id.startsWith(label)` — the latter never matches a
        prefixed CLI session id ("cc:<uuid>".startsWith("<uuid>") is false),
        which would let the raw id `label` the ingest falls back to shadow
        the far more useful `prompt_preview`."""
        _open_agents2(page, agents_base_url)
        page.wait_for_timeout(300)
        rows = page.evaluate(
            "() => Array.from(document.querySelectorAll('.node')).map(el => ({"
            "session_id: el.__data__.session_id,"
            "label: el.querySelector('text.node-label').textContent,"
            "}))"
        )
        by_id = {r["session_id"]: r["label"] for r in rows}
        # Long labels wrap across multiple <tspan> children (renderNodeLabel
        # in graph.js) without preserving the inter-word space, so compare
        # with whitespace collapsed rather than the raw textContent.
        rendered = (by_id["cc:0e6b2c14-9f77-4a1e-8b55-3c2f9d10aa42"] or "").replace(" ", "")
        assert rendered == "fix the synthetic widget parser".replace(" ", "")

    def test_node_label_suppresses_a_worker_label_that_equals_its_task_id(self, page: Page, agents_base_url):
        """An orphaned worker row's `label` falls back to its bare
        `task_id` in `_label_for_session`; the node label must not render
        that raw id and must fall through to the next real candidate
        (`model_label` here, since there's no `prompt_preview`)."""
        _open_agents2(page, agents_base_url)
        page.wait_for_timeout(300)
        rows = page.evaluate(
            "() => Array.from(document.querySelectorAll('.node')).map(el => ({"
            "session_id: el.__data__.session_id,"
            "label: el.querySelector('text.node-label').textContent,"
            "}))"
        )
        by_id = {r["session_id"]: r["label"] for r in rows}
        assert by_id["sess_label_orphan"] == "Local"

    def test_panel_routing_badge_says_ask_not_a_model_name(self, page: Page, agents_base_url):
        """panel.js's routingLabel() has an `ask` arm — the side
        panel's Routing badge must say "Ask", never fall through to the
        default "Claude"."""
        _open_agents2(page, agents_base_url)
        page.locator("#search-input").fill("labeltest-ask")
        result = page.locator(".search-result", has_text="labeltest-ask")
        expect(result).to_be_visible()
        result.click()
        expect(page.locator("#panel [data-field=\"routing\"]")).to_have_text("Ask")

    def test_panel_routing_badge_says_hermes(self, page: Page, agents_base_url):
        _open_agents2(page, agents_base_url)
        page.locator("#search-input").fill("labeltest-hermes")
        result = page.locator(".search-result", has_text="labeltest-hermes")
        expect(result).to_be_visible()
        result.click()
        expect(page.locator("#panel [data-field=\"routing\"]")).to_have_text("Hermes")
