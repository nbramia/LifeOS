"""Browser test for linking the /agents Board and Graph tabs (#865): card
clusters on the graph, cross-tab jumps in both directions, URL deep links,
shared persistent filters, and the card-anchor pending-question badge with
its inline Answer form.

Serves `web/` itself on an ephemeral port and stubs every `/api/` call — the
same server-free pattern as `tests/test_agents_graph_redesign_ui_browser.py`
— so this carries no `requires_server` marker and runs at pre-push
(`browser and not requires_server`). d3 loads from the real
`https://d3js.org/d3.v7.min.js` (left unstubbed, same as the redesign
suite), since clustering and node selection need the real force simulation.
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


# ---------------------------------------------------------------------------
# Fixtures — three sessions sharing one card ("t-cluster"), one session with
# no card (attaches to its host anchor), and one card whose one session
# carries an open pending question ("t-question", its own single-session
# card anchor). The board fixture's two task cards are the `_task_card`-
# shaped view of the same two cards, each embedding its own most-recently-
# active session — the snapshot and board fixtures describe the same world
# from the graph's and the board's own angle, as the real API does.
# ---------------------------------------------------------------------------


def _session(**overrides):
    base = {
        "session_id": "sess-placeholder",
        "task_id": None,
        "card_id": None,
        "card_title": None,
        "assignee": None,
        "card_tags": [],
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


CLUSTER_TITLE = "Ship the release notes"

CLUSTER_1 = _session(
    session_id="sess-cluster-1", task_id="t-cluster", card_id="t-cluster",
    card_title=CLUSTER_TITLE, assignee="codex", card_tags=["codex"],
    label=CLUSTER_TITLE, last_activity_at=3000,
)
CLUSTER_2 = _session(
    session_id="sess-cluster-2", task_id="t-cluster", card_id="t-cluster",
    card_title=CLUSTER_TITLE, assignee="codex", card_tags=["codex"],
    label=CLUSTER_TITLE, last_activity_at=2000,
)
CLUSTER_3 = _session(
    session_id="cc:cluster-3", task_id="t-cluster", card_id="t-cluster",
    card_title=CLUSTER_TITLE, assignee="codex", card_tags=["codex"],
    label=CLUSTER_TITLE, source="claude_code", routing="claude_code",
    last_activity_at=1000,
)
UNLINKED = _session(
    session_id="sess-unlinked", label="Ad-hoc exploration", last_activity_at=2500,
)
QUESTION_SESSION = _session(
    session_id="sess-question", task_id="t-question", card_id="t-question",
    card_title="Answer the operator's question", assignee="me", card_tags=["me"],
    label="Answer the operator's question", status="blocked", lane="human_queue",
    last_activity_at=1800,
    pending_question={
        "id": 42, "session_id": "sess-question",
        "question": "Deploy now?", "asked_at": 1700, "bot": None,
    },
)

SNAPSHOT = {
    "sessions": [CLUSTER_1, CLUSTER_2, CLUSTER_3, UNLINKED, QUESTION_SESSION],
    "edges": [],
    "generated_at": 1234567890,
    "api_host": "build-host",
}

CLUSTER_CARD = {
    "kind": "task", "id": "t-cluster", "title": CLUSTER_TITLE,
    "notes": "", "status": "in_progress", "tags": ["codex"], "assignee": "codex",
    "fields": {}, "context": "Work", "updated_at": "2026-01-01T00:00:00+00:00",
    "pending_question": None,
    "session": CLUSTER_1,  # most recently active of the three
}
QUESTION_CARD = {
    "kind": "task", "id": "t-question", "title": "Answer the operator's question",
    "notes": "", "status": "blocked", "tags": ["me"], "assignee": "me",
    "fields": {}, "context": "Work", "updated_at": "2026-01-01T00:00:00+00:00",
    "pending_question": QUESTION_SESSION["pending_question"],
    "session": QUESTION_SESSION,
}

BOARD = {
    "lanes": {
        "unassigned": [], "assigned": [], "in_progress": [CLUSTER_CARD],
        "human_queue": [QUESTION_CARD], "scheduled": [], "review": [], "done": [],
    },
    "generated_at": 1234567890,
    "api_host": "build-host",
}


def _make_handler(snapshot=None, board=None, answer_calls=None):
    def handler(route):
        req = route.request
        url = req.url
        if answer_calls is not None and "/pending-questions/" in url and "/answer" in url and req.method == "POST":
            try:
                answer_calls.append(json.loads(req.post_data or "{}"))
            except json.JSONDecodeError:
                answer_calls.append({})
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        if "/stream" in url and "/sessions/" not in url:
            route.fulfill(status=200, content_type="text/event-stream", body="")
            return
        if "/api/agents/snapshot" in url:
            body = snapshot or SNAPSHOT
        elif "/api/agents/board" in url:
            body = board or BOARD
        elif "/api/agents/search" in url:
            body = {"query": "", "matches": []}
        elif "/api/agents/models" in url:
            body = {"engines": {"claude": [], "codex": [], "local": [], "hermes": []},
                     "refreshed_at": "2026-01-01T00:00:00Z", "stale": False}
        elif "/api/agents/hosts" in url:
            body = {"hosts": [{"name": "build-host", "ssh_target": "", "online": True, "is_api_host": True}],
                     "refreshed_at": "2026-01-01T00:00:00Z"}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return handler


def _open_agents(page: Page, base_url, path="/agents", snapshot=None, board=None, answer_calls=None):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.route("**/api/**", _make_handler(snapshot, board, answer_calls))
    page.goto(f"{base_url}{path}")
    # `state="attached"`, not the default "visible" — a `?session=<id>` deep
    # link hides `#board-view` (the graph tab activates instead), and
    # `#board-lanes` is inside it.
    page.wait_for_selector("#board-lanes", state="attached")
    page.wait_for_timeout(300)
    return page


def _go_to_graph(page: Page):
    page.click('[data-tab="graph"]')
    page.wait_for_selector("#filter-route")
    page.select_option("#filter-recency", "all")
    page.select_option("#filter-lane", "all")
    page.locator("#filter-terminal").check()
    page.wait_for_timeout(500)


def _nodes(page: Page):
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('.node')).map(el => el.__data__.session_id)"
    )


def _anchors(page: Page):
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('.anchor')).map(el => ({"
        "id: el.__data__.id, kind: el.__data__.anchor_kind, card_id: el.__data__.card_id,"
        "label: el.querySelector('.anchor-label').textContent,"
        "}))"
    )


class TestClustering:
    def test_three_sessions_on_one_card_render_one_anchor_with_the_card_title(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        _go_to_graph(page)
        anchors = _anchors(page)
        card_anchors = [a for a in anchors if a["kind"] == "card" and a["card_id"] == "t-cluster"]
        assert len(card_anchors) == 1, anchors
        assert card_anchors[0]["label"] == CLUSTER_TITLE

    def test_three_sessions_on_one_card_all_link_to_its_anchor(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        _go_to_graph(page)
        anchor_ids = {a["id"] for a in _anchors(page)}
        linked = page.evaluate(
            "() => Array.from(document.querySelectorAll('path.link-anchor')).map(el => "
            "({source: (el.__data__.source.session_id || el.__data__.source),"
            "  target: (el.__data__.target.session_id || el.__data__.target)}))"
        )
        cluster_ids = {"sess-cluster-1", "sess-cluster-2", "cc:cluster-3"}
        targets = {edge["target"] for edge in linked if edge["source"] in cluster_ids}
        assert targets == {"card:t-cluster"}, linked
        # A link's own target string is not enough on its own — it must name
        # an anchor that's actually rendered, not a dangling id nothing in
        # the anchor layer answers to.
        assert targets <= anchor_ids, (targets, anchor_ids)

    def test_session_with_no_card_attaches_to_its_host_anchor(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        _go_to_graph(page)
        anchors = _anchors(page)
        host_anchors = [a for a in anchors if a["kind"] == "host"]
        assert any(a["label"] == "build-host" for a in host_anchors), anchors
        linked_to_host = page.evaluate(
            "() => { const l = Array.from(document.querySelectorAll('path.link-anchor'))"
            ".find(el => (el.__data__.source.session_id || el.__data__.source) === 'sess-unlinked');"
            " return l ? (l.__data__.target.session_id || l.__data__.target) : null; }"
        )
        assert linked_to_host == "host:build-host"


class TestCardChipToGraph:
    def test_session_chip_switches_to_graph_and_selects_the_node(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        chip = page.locator('.board-card[data-card-id="t-cluster"] .board-chip-session')
        expect(chip).to_be_visible()
        chip.click()
        page.wait_for_timeout(600)
        expect(page.locator("#graph-view")).to_be_visible()
        assert "active" in (page.get_attribute('[data-tab="graph"]', "class") or "")
        selected = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-cluster-1');"
            " return g ? g.querySelector('.node-shape').classList.contains('selected') : false; }"
        )
        assert selected is True
        expect(page.locator("#panel-empty")).to_have_count(0)


class TestNodeToBoardReveal:
    def test_selecting_a_node_then_switching_to_board_reveals_and_highlights_its_card(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        _go_to_graph(page)
        page.evaluate(
            """() => {
                const g = [...document.querySelectorAll('.node')]
                  .find(n => n.__data__.session_id === 'sess-cluster-2');
                g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(300)
        showBtn = page.locator('#graph-panel-actions [data-action="show-on-board"]')
        expect(showBtn).to_be_visible()
        showBtn.click()
        page.wait_for_timeout(400)
        expect(page.locator("#board-view")).to_be_visible()
        card = page.locator('.board-card[data-card-id="t-cluster"]')
        expect(card).to_be_visible()
        assert "reveal-highlight" in (card.get_attribute("class") or "")


class TestUrlDeepLinks:
    def test_session_deep_link_opens_graph_tab_and_selects_the_node(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url, path="/agents?session=sess-cluster-2")
        page.wait_for_timeout(700)
        assert "active" in (page.get_attribute('[data-tab="graph"]', "class") or "")
        selected = page.evaluate(
            "() => { const g = [...document.querySelectorAll('.node')]"
            ".find(n => n.__data__.session_id === 'sess-cluster-2');"
            " return g ? g.querySelector('.node-shape').classList.contains('selected') : false; }"
        )
        assert selected is True

    def test_card_deep_link_opens_board_with_drawer_open(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url, path="/agents?card=t-question")
        page.wait_for_timeout(500)
        assert "active" in (page.get_attribute('[data-tab="board"]', "class") or "")
        drawer = page.locator("#board-drawer .drawer-title")
        expect(drawer).to_be_visible()
        expect(drawer).to_have_value("Answer the operator's question")

    def test_unknown_session_deep_link_shows_a_toast(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url, path="/agents?session=does-not-exist")
        page.wait_for_timeout(700)
        expect(page.locator(".toast.error")).to_be_visible()
        # Left at the default (empty) panel — the unknown id was reported,
        # nothing was selected.
        expect(page.locator("#panel-empty")).to_be_visible()


class TestSharedFilters:
    def test_filter_change_on_board_applies_on_graph(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#board-filter-assignee", "codex")
        page.wait_for_timeout(200)
        _go_to_graph(page)
        assert page.input_value("#filter-assignee") == "codex"

    def test_filter_change_on_graph_applies_on_board(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        _go_to_graph(page)
        page.select_option("#filter-assignee", "me")
        page.wait_for_timeout(200)
        page.click('[data-tab="board"]')
        page.wait_for_timeout(200)
        assert page.input_value("#board-filter-assignee") == "me"

    def test_reload_restores_filters_from_local_storage(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#board-filter-assignee", "codex")
        page.wait_for_timeout(200)
        _open_agents(page, agents_base_url)  # a fresh goto() — same origin, same localStorage
        assert page.input_value("#board-filter-assignee") == "codex"

    def test_clear_resets_every_shared_filter(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#board-filter-assignee", "codex")
        page.fill("#board-filter-tag", "urgent")
        page.wait_for_timeout(200)
        page.click("#board-filter-clear")
        page.wait_for_timeout(200)
        assert page.input_value("#board-filter-assignee") == "all"
        assert page.input_value("#board-filter-tag") == ""


class TestAnchorPendingQuestionBadgeAndAnswer:
    def test_card_anchor_with_pending_question_renders_the_badge(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        _go_to_graph(page)
        display = page.evaluate(
            "() => { const a = [...document.querySelectorAll('.anchor')]"
            ".find(n => n.__data__.card_id === 't-question');"
            " const b = a.querySelector('.anchor-badge-question');"
            " return b ? getComputedStyle(b).display : null; }"
        )
        assert display != "none"

    def test_answer_posts_to_the_pending_question_endpoint(self, page: Page, agents_base_url):
        answer_calls = []
        _open_agents(page, agents_base_url, answer_calls=answer_calls)
        _go_to_graph(page)
        page.evaluate(
            """() => {
                const a = [...document.querySelectorAll('.anchor')]
                  .find(n => n.__data__.card_id === 't-question');
                a.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(300)
        answerBtn = page.locator('#graph-panel-actions [data-action="answer"]')
        expect(answerBtn).to_be_visible()
        answerBtn.click()
        page.fill("#graph-panel-actions textarea", "Yes, proceed.")
        page.click("#graph-panel-actions .graph-panel-answer-form button")
        page.wait_for_timeout(400)
        assert len(answer_calls) == 1
        assert answer_calls[0] == {"answer": "Yes, proceed."}
        expect(page.locator(".toast")).to_be_visible()
