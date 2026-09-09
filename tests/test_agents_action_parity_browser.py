"""Browser test for web/agents/session_actions.js — the shared action-row
decision (`decideActions`) and its cross-surface parity: the same session
(and, when the surface has one, its linked board card) must render the
IDENTICAL action set — same ids, same order, same labels, same
enabled/disabled state, same reason text — whether it's rendered by the
Board tab's card drawer (web/agents/board.js) or the Graph tab's side
panel (web/agents/panel.js's `SessionPanel`), since both call the same
`decideActions` + `renderActionRow`.

Two kinds of coverage:

- `TestDecideActions` drives `decideActions` directly (no DOM), the same
  `add_script_tag` harness `tests/test_agents_graph_encoding_browser.py`
  uses for graph_encoding.js's pure functions.
- `TestActionParity` renders the SAME (session, card) pair on both real
  surfaces — the Board drawer via `web/agents/board.js` (served as the
  actual `/agents` page) and a directly-mounted `SessionPanel` (the exact
  class `web/agents/graph.js` uses for its own side panel, given a card via
  `.open(session, card)` — a real, supported call the Board drawer's own
  embedded panel doesn't use only because it renders its own action row
  instead, per `showActions: false`) — and asserts the two rendered rows
  are byte-identical.

Serves `web/` itself from an ephemeral port and stubs every `/api/` call,
so this carries no `requires_server` marker and runs at pre-push (`browser
and not requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _WebHandler(http.server.SimpleHTTPRequestHandler):
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
def web_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# TestDecideActions — the pure decision function, no DOM.
# ---------------------------------------------------------------------------

def _load_actions_module(page: Page, base_url: str):
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body="{}"))
    page.goto(f"{base_url}/agents.html")
    page.add_script_tag(
        content="""
        import * as actions from '/agents/session_actions.js';
        window.__actions = actions;
        window.__actionsReady = true;
        """,
        type="module",
    )
    page.wait_for_function("() => window.__actionsReady === true")


def _decide(page: Page, session, card):
    return page.evaluate(
        "(args) => window.__actions.decideActions(args[0], args[1])",
        [session, card],
    )


def _ids(descriptors):
    return [d["id"] for d in descriptors]


# A CLI, non-subagent, resumable (`inactive`) session with no linked card.
_BARE_SESSION = {
    "session_id": "cc:parity-bare", "source": "claude_code", "status": "inactive",
    "is_subagent": False, "parent_session_id": None,
}


class TestDecideActions:
    def test_bare_session_no_card_offers_only_session_actions(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        out = _decide(page, _BARE_SESSION, None)
        # No card at all — Open/Answer/Accept/Resolve/Cancel/Delete must
        # never be invented; Rename/Focus/Resume/Kill are session-level and
        # don't need one.
        assert _ids(out) == ["rename", "focus", "resume", "kill"], out
        kill = next(d for d in out if d["id"] == "kill")
        assert kill["enabled"] is False
        assert "claude code" in kill["reason"].lower()

    def test_routing_derived_subagent_offers_neither_focus_nor_resume(self, page: Page, web_base_url):
        """A routing-derived subagent (no `is_subagent` flag, but
        `parent_session_id` set) must be refused Focus and Resume exactly
        like a worker-flagged one."""
        _load_actions_module(page, web_base_url)
        session = dict(_BARE_SESSION, session_id="cc:parity-routing-subagent",
                        is_subagent=False, parent_session_id="cc:parity-bare")
        out = _decide(page, session, None)
        assert "focus" not in _ids(out), out
        assert "resume" not in _ids(out), out

    def test_flagged_subagent_also_refused(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        session = dict(_BARE_SESSION, session_id="cc:parity-flagged-subagent", is_subagent=True)
        out = _decide(page, session, None)
        assert "focus" not in _ids(out), out
        assert "resume" not in _ids(out), out

    def test_running_session_offers_resume_but_hidden(self, page: Page, web_base_url):
        """Resume is decided (mounted) the moment a session is CLI and not
        a subagent, regardless of current status — `visible` carries
        whether it's actually showable right now — so a caller that
        refreshes in place (the Graph panel's `updateMeta`) can reveal it
        later without recreating it."""
        _load_actions_module(page, web_base_url)
        session = dict(_BARE_SESSION, session_id="cc:parity-running", status="running")
        out = _decide(page, session, None)
        resume = next(d for d in out if d["id"] == "resume")
        assert resume["visible"] is False, resume
        # And still offers Kill, disabled, for the same live CLI session.
        kill = next(d for d in out if d["id"] == "kill")
        assert kill["enabled"] is False

    def test_non_cli_live_session_offers_enabled_kill_no_resume_no_focus(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        session = {
            "session_id": "sess-local-parity", "source": "lifeos_agent", "status": "running",
            "is_subagent": False, "parent_session_id": None,
        }
        out = _decide(page, session, None)
        assert _ids(out) == ["rename", "kill"], out
        kill = next(d for d in out if d["id"] == "kill")
        assert kill["enabled"] is True

    def test_terminal_session_offers_neither_kill_nor_resume_nor_focus_when_not_cli(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        session = {
            "session_id": "sess-done-parity", "source": "lifeos_agent", "status": "completed",
            "is_subagent": False, "parent_session_id": None,
        }
        out = _decide(page, session, None)
        # Rename is session-level and applies regardless of live/terminal
        # status — everything operational (Kill/Resume/Focus) is still
        # refused for a terminal, non-CLI session.
        assert _ids(out) == ["rename"], out

    def test_card_assigned_lane_offers_open_before_focus(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        session = dict(_BARE_SESSION, session_id="cc:parity-open")
        card = {"kind": "task", "lane": "assigned", "assignee": "claude", "pending_question": None}
        out = _decide(page, session, card)
        assert _ids(out) == ["open", "rename", "focus", "resume", "kill", "delete"], out

    def test_card_review_lane_offers_accept(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        card = {"kind": "task", "lane": "review", "assignee": "claude", "pending_question": None}
        out = _decide(page, None, card)
        assert _ids(out) == ["accept", "delete"], out

    def test_card_human_queue_offers_resolve_when_done_move_allowed(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        card = {
            "kind": "task", "lane": "human_queue", "assignee": None, "pending_question": None,
            "policy": {"lanes": {}},
        }
        out = _decide(page, None, card)
        assert "resolve" in _ids(out), out

    def test_card_human_queue_omits_resolve_when_done_move_refused(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        card = {
            "kind": "task", "lane": "human_queue", "assignee": None, "pending_question": None,
            "policy": {"lanes": {"done": {"allowed": False, "reason": "still claimed"}}},
        }
        out = _decide(page, None, card)
        assert "resolve" not in _ids(out), out

    def test_card_human_queue_with_pending_question_omits_resolve(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        card = {
            "kind": "task", "lane": "human_queue", "assignee": None,
            "pending_question": {"id": 1, "question": "Proceed?"},
        }
        out = _decide(page, None, card)
        assert "resolve" not in _ids(out), out
        assert "answer" in _ids(out), out

    def test_cancel_disabled_carries_server_reason(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        card = {
            "kind": "task", "lane": "in_progress", "assignee": None, "pending_question": None,
            "policy": {"cancel": {"allowed": False, "reason": "already claimed by another agent"}},
        }
        out = _decide(page, None, card)
        cancel = next(d for d in out if d["id"] == "cancel")
        assert cancel["enabled"] is False
        assert cancel["reason"] == "already claimed by another agent"

    def test_delete_always_last_and_danger_for_any_card(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        card = {"kind": "schedule", "lane": "scheduled"}
        out = _decide(page, None, card)
        assert out[-1]["id"] == "delete"
        assert out[-1]["danger"] is True

    def test_canonical_order_is_stable_with_every_action_present(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        session = dict(_BARE_SESSION, session_id="cc:parity-all", status="running")
        card = {
            "kind": "task", "lane": "review", "assignee": "claude",
            "pending_question": {"id": 2, "question": "Proceed?"},
            "policy": {"cancel": {"allowed": False, "reason": "n/a"}},
        }
        out = _decide(page, session, card)
        # Open needs lane=assigned (not review) and Resolve needs
        # lane=human_queue, so neither applies here — everything else does.
        assert _ids(out) == ["rename", "focus", "resume", "kill", "answer", "accept", "cancel", "delete"], out


# ---------------------------------------------------------------------------
# TestActionParity — the same (session, card) rendered on both real
# surfaces, asserting byte-identical output.
# ---------------------------------------------------------------------------

# Reads the actual rendered action row in DOM order — walking `container`'s
# own children, not iterating a hard-coded id list with `querySelector` —
# so a wrong order, a duplicated action, or an unexpected extra action all
# change the extracted sequence and fail an equality assertion against it,
# rather than silently passing because every expected id still happens to
# exist somewhere in the container.
_EXTRACT_JS = """
(container) => {
  const out = [];
  for (const el of container.children) {
    if (el.tagName !== 'BUTTON' || !el.dataset.action || el.hidden) continue;
    const id = el.dataset.action;
    const reasonEl = container.querySelector(`[data-field="${id}-reason"]`);
    out.push({
      id, label: el.textContent, enabled: !el.disabled,
      reason: reasonEl ? reasonEl.textContent : null,
    });
  }
  return out;
}
"""


def _extract_actions(page: Page, container_selector: str):
    return page.eval_on_selector(container_selector, _EXTRACT_JS)


def _click_graph_node(page: Page, session_id: str) -> None:
    page.wait_for_function(
        """sid => [...document.querySelectorAll('#graph-svg .node')]
            .some(node => node.__data__ && node.__data__.session_id === sid)""",
        arg=session_id,
    )
    page.evaluate(
        """sid => {
            const node = [...document.querySelectorAll('#graph-svg .node')]
                .find(candidate => candidate.__data__ && candidate.__data__.session_id === sid);
            node.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
        }""",
        session_id,
    )


# A rich session+card pair exercising Open, Focus, Resume, Kill (disabled),
# Answer, Cancel (disabled), and Delete together — Accept/Resolve need
# different lanes, covered separately below and by TestDecideActions above.
_PARITY_SESSION = {
    "session_id": "cc:parity-shared", "source": "claude_code", "status": "inactive",
    "status_inferred": False, "routing": "claude_code", "host": "studio",
    "branch": "", "prompt_preview": "", "decoded_cwd": "/home/synthetic/proj",
    "total_dollars": 0.01, "total_input_tokens": 10, "total_output_tokens": 20,
    "spawn_depth": 0, "label": "Shared parity session", "custom_label": None,
    "short_label": None, "is_subagent": False, "parent_session_id": None,
    "pending_question": {
        "id": 7, "session_id": "cc:parity-shared", "question": "Proceed with deploy?",
        "asked_at": 1700, "bot": None,
    },
}
_PARITY_CARD = {
    "kind": "task", "id": "t-parity", "title": "Ship the synthetic feature",
    "notes": "", "status": "in_progress", "tags": ["claude"], "assignee": "claude",
    "fields": {}, "context": "Work", "updated_at": "2026-01-01T00:00:00+00:00",
    "lane": "assigned",
    "pending_question": _PARITY_SESSION["pending_question"],
    "policy": {"cancel": {"allowed": False, "reason": "already claimed by another agent"}},
    "session": _PARITY_SESSION,
}


def _board_fixture(card):
    return {
        "lanes": {
            "unassigned": [], "assigned": [card], "in_progress": [],
            "human_queue": [], "scheduled": [], "review": [], "done": [],
        },
        "generated_at": 0,
    }


def _stub_common(route, board_card):
    req = route.request
    url = req.url
    if "/api/agents/board/stream" in url:
        return  # inert EventSource — nothing pushed during this test
    if "/api/agents/board" in url.split("?", 1)[0]:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_board_fixture(board_card)))
        return
    if "/api/agents/snapshot" in url:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"sessions": [], "edges": [], "generated_at": 0, "api_host": "studio"}))
        return
    if "/api/agents/hosts" in url:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"hosts": [{"name": "studio", "is_api_host": True}]}))
        return
    if "/sessions/" in url and url.endswith("/summary"):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"short_label": "", "summary": ""}))
        return
    if "/sessions/" in url and "/events" in url:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"events": []}))
        return
    route.fulfill(status=200, content_type="application/json", body="{}")


class TestActionParity:
    def test_board_drawer_and_panel_render_identical_action_rows(self, page: Page, web_base_url):
        board_card = json.loads(json.dumps(_PARITY_CARD))

        page.route("**/api/**", lambda route: _stub_common(route, board_card))
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{web_base_url}/agents")
        page.wait_for_selector('.board-card[data-card-id="t-parity"]')
        page.locator('.board-card[data-card-id="t-parity"]').click()
        page.wait_for_selector('#board-drawer [data-field="actions"] [data-action]')

        board_actions = _extract_actions(page, '#board-drawer [data-field="actions"]')

        # Mount a second, independent SessionPanel — the exact class
        # web/agents/graph.js uses for the Graph tab's side panel — with
        # the SAME session and card, and read its own action row the same
        # way. `.open(session, card)` is the real, supported two-arg call;
        # the Board drawer's own embedded panel just never uses it (it
        # constructs with `showActions: false` and renders its own row
        # instead, in `board_actions` above).
        panel_actions = page.evaluate(
            """
            async (args) => {
              const [session, card, extractSrc] = args;
              const mod = await import('/agents/panel.js');
              const el = document.createElement('div');
              document.body.appendChild(el);
              const panel = new mod.SessionPanel({ container: el });
              panel.open(session, card);
              // eslint-disable-next-line no-eval
              const extract = eval(extractSrc);
              return extract(el.querySelector('[data-field="actions"]'));
            }
            """,
            [_PARITY_SESSION, board_card, _EXTRACT_JS],
        )

        assert board_actions, "board drawer rendered no actions"
        assert panel_actions, "panel rendered no actions"
        assert board_actions == panel_actions, (board_actions, panel_actions)
        # And the canonical set — proves this scenario actually exercises
        # Open/Focus/Resume/Kill/Answer/Cancel/Delete together, not just
        # that two empty lists match.
        assert [a["id"] for a in board_actions] == [
            "open", "rename", "focus", "resume", "kill", "answer", "cancel", "delete",
        ], board_actions

    def test_review_lane_accept_parity(self, page: Page, web_base_url):
        # `card["lane"]` (a client-side field board.js's `allCards()`
        # derives from which lane bucket a card is nested under in
        # GET /api/agents/board, never a server field) only needs to match
        # the bucket below for the Board tab's own rendering — it's not
        # read from the fixture itself, but kept here for clarity and for
        # the panel-side `.open(session, card)` call further down, which
        # DOES read it directly (mirroring board.js's own `findCard`).
        card = json.loads(json.dumps(_PARITY_CARD))
        card["lane"] = "review"
        card["id"] = "t-parity-review"
        card["pending_question"] = None
        card["session"]["pending_question"] = None
        card["policy"] = {}

        def handler(route):
            url = route.request.url
            if "/api/agents/board/stream" in url:
                return
            if "/api/agents/board" in url.split("?", 1)[0]:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "lanes": {
                        "unassigned": [], "assigned": [], "in_progress": [],
                        "human_queue": [], "scheduled": [], "review": [card], "done": [],
                    },
                    "generated_at": 0,
                }))
                return
            _stub_common(route, card)

        page.route("**/api/**", handler)
        page.goto(f"{web_base_url}/agents")
        page.wait_for_selector(f'.board-card[data-card-id="{card["id"]}"]')
        page.locator(f'.board-card[data-card-id="{card["id"]}"]').click()
        page.wait_for_selector('#board-drawer [data-field="actions"] [data-action]')
        board_actions = _extract_actions(page, '#board-drawer [data-field="actions"]')

        panel_actions = page.evaluate(
            """
            async (args) => {
              const [session, boardCard, extractSrc] = args;
              const mod = await import('/agents/panel.js');
              const el = document.createElement('div');
              document.body.appendChild(el);
              const panel = new mod.SessionPanel({ container: el });
              panel.open(session, boardCard);
              const extract = eval(extractSrc);
              return extract(el.querySelector('[data-field="actions"]'));
            }
            """,
            [card["session"], card, _EXTRACT_JS],
        )

        assert board_actions == panel_actions, (board_actions, panel_actions)
        assert [a["id"] for a in board_actions] == ["rename", "focus", "resume", "kill", "accept", "delete"], board_actions


# ---------------------------------------------------------------------------
# TestGraphTabParity — drives the REAL Graph tab (web/agents/graph.js), not
# a directly-mounted SessionPanel. `TestActionParity` above proves
# `SessionPanel` renders an identical row when handed a card directly
# (`panel.open(session, card)`); it does not prove `graph.js` itself ever
# makes that two-argument call. This does: it opens the Board drawer for a
# card, reads its row, then switches to the Graph tab, clicks the node for
# that same card's linked session, and asserts the two rendered rows match.
# ---------------------------------------------------------------------------

class TestGraphTabParity:
    def test_graph_tab_node_matches_board_drawer_for_same_card(self, page: Page, web_base_url):
        card = json.loads(json.dumps(_PARITY_CARD))
        session = card["session"]
        # The shape `_build_snapshot` (api/routes/agents.py) actually sends:
        # every row plus two derived fields, `lane` and `pending_question`,
        # neither of which board.js's own card carries under a `session`
        # key with quite this name — `lane` here comes from the CARD's
        # bucket, `pending_question` is copied onto the session row too.
        snapshot_session = dict(session, lane=card["lane"], pending_question=session["pending_question"])

        def handler(route):
            url = route.request.url
            if "/api/agents/board/stream" in url:
                return
            if "/api/agents/stream" in url and "/sessions/" not in url:
                return  # inert EventSource — nothing pushed during this test
            if "/api/agents/board" in url.split("?", 1)[0]:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_board_fixture(card)))
                return
            if "/api/agents/snapshot" in url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "sessions": [snapshot_session], "edges": [], "generated_at": 0, "api_host": "studio",
                }))
                return
            _stub_common(route, card)

        page.route("**/api/**", handler)
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{web_base_url}/agents")
        page.wait_for_selector(f'.board-card[data-card-id="{card["id"]}"]')
        page.locator(f'.board-card[data-card-id="{card["id"]}"]').click()
        page.wait_for_selector('#board-drawer [data-field="actions"] [data-action]')
        board_actions = _extract_actions(page, '#board-drawer [data-field="actions"]')
        page.keyboard.press("Escape")  # close the drawer before switching tabs

        page.click('[data-tab="graph"]')
        page.wait_for_selector("#filter-route")
        page.select_option("#filter-recency", "all")
        page.locator("#filter-terminal").check()
        _click_graph_node(page, session["session_id"])
        page.wait_for_selector('#panel [data-field="actions"] [data-action]')
        graph_actions = _extract_actions(page, '#panel [data-field="actions"]')

        assert board_actions, "board drawer rendered no actions"
        assert graph_actions, (
            "graph panel rendered no actions for a card-linked session — "
            "the Graph tab never handed SessionPanel the card"
        )
        assert graph_actions == board_actions, (board_actions, graph_actions)
        assert [a["id"] for a in graph_actions] == [
            "open", "rename", "focus", "resume", "kill", "answer", "cancel", "delete",
        ], graph_actions


# ---------------------------------------------------------------------------
# TestGraphPanelCardOnlyHandlersWired — a card-only action button rendering
# proves nothing about whether it's wired: `_EXTRACT_JS` above reads
# `id`/`label`/`enabled`/`reason`, never `onclick`. This drives the real
# Graph tab, clicks a card-only button (Open), and asserts the request it's
# supposed to fire actually fires.
# ---------------------------------------------------------------------------

class TestGraphPanelCardOnlyHandlersWired:
    def test_open_button_on_graph_panel_fires_the_open_request(self, page: Page, web_base_url):
        card = json.loads(json.dumps(_PARITY_CARD))
        card["id"] = "t-wiring-open"
        card["lane"] = "assigned"
        card["pending_question"] = None
        card["session"]["pending_question"] = None
        card["policy"] = {}
        session = card["session"]
        snapshot_session = dict(session, lane=card["lane"], pending_question=None)

        open_requests = []

        def handler(route):
            url = route.request.url
            path = url.split("?", 1)[0]
            if path.endswith(f"/board/cards/{card['id']}/open") and route.request.method == "POST":
                open_requests.append(url)
                route.fulfill(status=200, content_type="application/json", body="{}")
                return
            if "/api/agents/board/stream" in url:
                return
            if "/api/agents/stream" in url and "/sessions/" not in url:
                return
            if "/api/agents/board" in path:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_board_fixture(card)))
                return
            if "/api/agents/snapshot" in url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "sessions": [snapshot_session], "edges": [], "generated_at": 0, "api_host": "studio",
                }))
                return
            _stub_common(route, card)

        page.route("**/api/**", handler)
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{web_base_url}/agents")
        page.wait_for_selector(f'.board-card[data-card-id="{card["id"]}"]')

        page.click('[data-tab="graph"]')
        page.wait_for_selector("#filter-route")
        page.select_option("#filter-recency", "all")
        page.locator("#filter-terminal").check()
        _click_graph_node(page, session["session_id"])
        page.wait_for_selector('#panel [data-field="actions"] [data-action="open"]')
        page.click('#panel [data-field="actions"] [data-action="open"]')
        page.wait_for_timeout(300)

        assert open_requests, (
            "clicking Open on the Graph tab's panel fired no request — "
            "the card-only handlers aren't wired into this panel"
        )


# ---------------------------------------------------------------------------
# TestGraphTabDeleteUsesFreshCard — the Graph tab's Delete confirmation must
# re-resolve its card through the board's own live lookup at open/confirm
# time, not the card captured when the panel opened — the same freshness
# the Board drawer's own `findCard` wiring gives Delete.
# ---------------------------------------------------------------------------

class TestGraphTabDeleteUsesFreshCard:
    def test_delete_reflects_a_session_that_went_live_after_the_panel_opened(self, page: Page, web_base_url):
        def make_card(status):
            return {
                "kind": "task", "id": "t-fresh-delete", "title": "Fresh delete probe",
                "notes": "", "status": "in_progress", "tags": [], "assignee": "",
                "fields": {}, "context": "Work", "updated_at": "2026-01-01T00:00:00+00:00",
                "lane": "in_progress",
                "pending_question": None,
                "policy": {},
                "session": {
                    "session_id": "sess-fresh-delete", "source": "lifeos_agent", "status": status,
                    "status_inferred": False, "routing": "local", "host": "studio",
                    "branch": "", "prompt_preview": "", "decoded_cwd": "/home/synthetic/proj",
                    "total_dollars": 0.01, "total_input_tokens": 10, "total_output_tokens": 20,
                    "spawn_depth": 0, "label": "Fresh delete session", "custom_label": None,
                    "short_label": None, "is_subagent": False, "parent_session_id": None,
                    "pending_question": None,
                },
            }

        stale_card = make_card("completed")   # terminal — no live session when the panel opens
        live_card = make_card("running")      # goes live before Delete is clicked

        def board_fixture(card):
            return {
                "lanes": {
                    "unassigned": [], "assigned": [], "in_progress": [card],
                    "human_queue": [], "scheduled": [], "review": [], "done": [],
                },
                "generated_at": 0,
            }

        delete_requests = []
        kill_requests = []
        stream_routes = []

        def handler(route):
            url = route.request.url
            path = url.split("?", 1)[0]
            if "/api/agents/board/stream" in url:
                stream_routes.append(route)  # held open; fulfilled explicitly below
                return
            if "/api/agents/stream" in url and "/sessions/" not in url:
                return
            if path.endswith("/api/agents/board"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps(board_fixture(stale_card)))
                return
            if "/api/agents/snapshot" in url:
                snap_session = dict(stale_card["session"], lane="in_progress", pending_question=None)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "sessions": [snap_session], "edges": [], "generated_at": 0, "api_host": "studio",
                }))
                return
            if path.endswith(f"/tasks/{stale_card['id']}") and route.request.method == "DELETE":
                delete_requests.append(url)
                route.fulfill(status=200, content_type="application/json", body="{}")
                return
            if "/sessions/" in url and path.endswith("/kill"):
                kill_requests.append(url)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"killed": [stale_card["session"]["session_id"]], "failures": []}))
                return
            if "/sessions/" in url and path.endswith("/summary"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"short_label": "", "summary": ""}))
                return
            if "/sessions/" in url and "/events" in url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"events": []}))
                return
            if "/api/agents/hosts" in url:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"hosts": [{"name": "studio", "is_api_host": True}]}))
                return
            route.fulfill(status=200, content_type="application/json", body="{}")

        page.route("**/api/**", handler)
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{web_base_url}/agents")
        page.wait_for_selector(f'.board-card[data-card-id="{stale_card["id"]}"]')

        page.click('[data-tab="graph"]')
        page.wait_for_selector("#filter-route")
        page.select_option("#filter-recency", "all")
        page.locator("#filter-terminal").check()
        _click_graph_node(page, stale_card["session"]["session_id"])
        page.wait_for_selector('#panel [data-field="actions"] [data-action="delete"]')

        # Deliver the board's live update NOW — after the panel has already
        # captured the (terminal) card, the same window a session starting
        # mid-review occupies.
        assert stream_routes, "board's SSE stream was never opened"
        # `board.js`'s `connectStream()` listens for a NAMED `board` SSE
        # event, not the default unnamed `message` one — an `event:` line
        # is required or the browser fires `message` and board.js never
        # hears it.
        stream_routes[0].fulfill(
            status=200, content_type="text/event-stream",
            body=f"event: board\ndata: {json.dumps(board_fixture(live_card))}\n\n",
        )
        page.wait_for_timeout(300)

        page.click('#panel [data-field="actions"] [data-action="delete"]')
        page.wait_for_selector('#delete-confirm')
        note = page.text_content('.modal .descendants')
        assert "kill" in note.lower(), (
            "Delete's note didn't reflect the live session — the Graph tab's "
            "panel used the card captured when it opened instead of a fresh "
            f"lookup: {note!r}"
        )

        page.click('#delete-confirm')
        page.wait_for_timeout(300)

        assert kill_requests, "the now-live session was never killed before delete"
        assert delete_requests


# ---------------------------------------------------------------------------
# TestKillModalDescendantGating — the destructive confirm must stay disabled
# until the descendant-preview lookup settles, and a failed lookup must be
# disclosed rather than rendered identically to "no descendants".
# ---------------------------------------------------------------------------

_KILL_GATING_SESSION = {
    "session_id": "sess-kill-gating", "source": "lifeos_agent", "status": "running",
    "label": "Kill gating probe",
}


class TestKillModalDescendantGating:
    def test_confirm_disabled_until_resolved_then_enabled_with_no_descendants(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        state = page.evaluate(
            """
            async (session) => {
              let resolveFn;
              const pending = new Promise((res) => { resolveFn = res; });
              window.__actions.openKillModal(session, { getDescendants: () => pending });
              const confirmBtn = document.querySelector('#kill-confirm');
              const disabledBefore = confirmBtn.disabled;
              resolveFn([]);
              await pending;
              await new Promise((r) => setTimeout(r, 0));
              const descendantsEl = document.querySelector('[data-field="kill-descendants"]');
              return {
                disabledBefore,
                disabledAfter: confirmBtn.disabled,
                descendantsHidden: descendantsEl.hidden,
              };
            }
            """,
            _KILL_GATING_SESSION,
        )
        assert state["disabledBefore"] is True, "confirm was clickable before the preview resolved"
        assert state["disabledAfter"] is False
        assert state["descendantsHidden"] is True

    def test_confirm_disabled_until_resolved_then_shows_descendants(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        descendant = {"session_id": "sess-kill-gating-child", "status": "running", "label": "child"}
        state = page.evaluate(
            """
            async (args) => {
              const [session, child] = args;
              window.__actions.openKillModal(session, { getDescendants: () => [child] });
              const confirmBtn = document.querySelector('#kill-confirm');
              const disabledBefore = confirmBtn.disabled;
              await new Promise((r) => setTimeout(r, 0));
              const descendantsEl = document.querySelector('[data-field="kill-descendants"]');
              return {
                disabledBefore,
                disabledAfter: confirmBtn.disabled,
                descendantsHidden: descendantsEl.hidden,
                descendantsText: descendantsEl.textContent,
              };
            }
            """,
            [_KILL_GATING_SESSION, descendant],
        )
        assert state["disabledBefore"] is True
        assert state["disabledAfter"] is False
        assert state["descendantsHidden"] is False
        assert "1 descendant" in state["descendantsText"]

    def test_failed_lookup_is_disclosed_not_treated_as_no_descendants(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        state = page.evaluate(
            """
            async (session) => {
              window.__actions.openKillModal(session, {
                getDescendants: () => Promise.reject(new Error('network down')),
              });
              const confirmBtn = document.querySelector('#kill-confirm');
              const disabledBefore = confirmBtn.disabled;
              await new Promise((r) => setTimeout(r, 0));
              const descendantsEl = document.querySelector('[data-field="kill-descendants"]');
              return {
                disabledBefore,
                disabledAfter: confirmBtn.disabled,
                descendantsHidden: descendantsEl.hidden,
                descendantsText: descendantsEl.textContent,
              };
            }
            """,
            _KILL_GATING_SESSION,
        )
        assert state["disabledBefore"] is True
        assert state["disabledAfter"] is False
        assert state["descendantsHidden"] is False
        assert "couldn't check" in state["descendantsText"].lower()


# ---------------------------------------------------------------------------
# TestBoardDrawerKillDescendantLookupFailure — the same gating and failure
# disclosure `TestKillModalDescendantGating` proves against a synthetic
# `getDescendants`, but driven through the Board drawer's REAL descendant
# lookup (`fetchDescendantsForKill` in web/agents/board.js), which fetches
# `GET /api/agents/snapshot` on demand. A failed fetch there must propagate
# as a rejection, not resolve to an empty list indistinguishable from "no
# descendants".
# ---------------------------------------------------------------------------

class TestBoardDrawerKillDescendantLookupFailure:
    def test_kill_confirm_gated_and_failure_disclosed_when_snapshot_fetch_fails(self, page: Page, web_base_url):
        card = json.loads(json.dumps(_PARITY_CARD))
        card["id"] = "t-board-kill-lookup-fail"
        card["lane"] = "in_progress"
        card["pending_question"] = None
        card["session"]["pending_question"] = None
        card["session"]["source"] = "lifeos_agent"
        card["policy"] = {}

        snapshot_routes = []

        def handler(route):
            url = route.request.url
            if "/api/agents/board/stream" in url:
                return
            path = url.split("?", 1)[0]
            if path.endswith("/api/agents/board"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_board_fixture(card)))
                return
            if "/api/agents/snapshot" in url:
                # Held open — fulfilled explicitly below, after we've
                # already checked the confirm button's state. A local mock
                # otherwise resolves so fast that checking "disabled before
                # the lookup settles" across two separate Playwright calls
                # races the fetch itself.
                snapshot_routes.append(route)
                return
            _stub_common(route, card)

        page.route("**/api/**", handler)
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{web_base_url}/agents")
        page.wait_for_selector(f'.board-card[data-card-id="{card["id"]}"]')
        page.click(f'.board-card[data-card-id="{card["id"]}"]')
        page.wait_for_selector('#board-drawer [data-field="actions"] [data-action="kill"]')
        page.click('#board-drawer [data-field="actions"] [data-action="kill"]')
        page.wait_for_selector('#kill-confirm')

        disabled_immediately = page.eval_on_selector('#kill-confirm', 'el => el.disabled')
        assert disabled_immediately is True, "confirm was clickable before the descendant lookup settled"

        assert snapshot_routes, "the descendant-preview fetch was never issued"
        snapshot_routes[0].fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
        page.wait_for_timeout(400)

        disabled_after = page.eval_on_selector('#kill-confirm', 'el => el.disabled')
        note_hidden = page.eval_on_selector('[data-field="kill-descendants"]', 'el => el.hidden')
        note_text = page.text_content('[data-field="kill-descendants"]')

        assert disabled_after is False
        assert note_hidden is False
        assert "couldn't check" in note_text.lower(), note_text


# ---------------------------------------------------------------------------
# TestResumeRowMountedOnce — a status flip that only changes Resume's own
# `visible` signal must reveal/hide the existing button/select in place, not
# rebuild the row — otherwise a chosen resume host is silently discarded.
# ---------------------------------------------------------------------------

_RESUME_MOUNT_SESSION = {
    "session_id": "cc:resume-mount-once", "source": "claude_code", "status": "running",
    "is_subagent": False, "parent_session_id": None,
}


class TestResumeRowMountedOnce:
    def test_status_flip_reveals_resume_without_recreating_its_elements(self, page: Page, web_base_url):
        _load_actions_module(page, web_base_url)
        result = page.evaluate(
            """
            async (session) => {
              const container = document.createElement('div');
              document.body.appendChild(container);
              window.__actions.renderActionRow(container, { session });
              // Let populateResumeHosts' own fetch chain settle before we
              // touch the select ourselves.
              await new Promise((r) => setTimeout(r, 100));
              const select1 = container.querySelector('[data-action="resume-host"]');
              const opt = document.createElement('option');
              opt.value = 'chosen-host';
              opt.textContent = 'chosen-host';
              select1.appendChild(opt);
              select1.value = 'chosen-host';
              const btn1 = container.querySelector('[data-action="resume"]');
              const hiddenBefore = btn1.hidden;

              const session2 = Object.assign({}, session, { status: 'inactive' });
              window.__actions.renderActionRow(container, { session: session2 });

              const btn2 = container.querySelector('[data-action="resume"]');
              const select2 = container.querySelector('[data-action="resume-host"]');
              return {
                sameButton: btn1 === btn2,
                sameSelect: select1 === select2,
                hiddenBefore,
                hiddenAfter: btn2.hidden,
                selectValuePreserved: select2.value === 'chosen-host',
              };
            }
            """,
            _RESUME_MOUNT_SESSION,
        )
        assert result["hiddenBefore"] is True, "Resume should start hidden for a live (non-resumable) session"
        assert result["hiddenAfter"] is False, "Resume should reveal once the session becomes resumable"
        assert result["sameButton"], "Resume's button was recreated instead of revealed in place"
        assert result["sameSelect"], "Resume's host select was recreated instead of revealed in place"
        assert result["selectValuePreserved"], "a chosen resume host was discarded by the rebuild"
