"""Browser test for the /agents drawer's "resume here" control.

Serves `web/` itself from an ephemeral port (like
`test_agents_board_ui_browser.py` / `test_voice_mic_block_ui_browser.py`)
and stubs every `/api/` call the page makes, including `GET
/api/agents/hosts` — the assertions are about the JS in
`web/agents/panel.js`, not the live backend. No `requires_server` marker,
so this runs at pre-push (`browser and not requires_server`).

Covers: the resume-host `<select>` lists the API host plus every registry
host, choosing a host and clicking Resume sends `target_host` in the POST
body, and a stubbed 400 whose detail carries a `command` renders it as
copyable text in the drawer instead of only a toast.
"""
import copy
import http.server
import json
import re
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Obviously synthetic — same shape GET /api/agents/hosts returns.
_HOSTS_FIXTURE = {
    "hosts": [
        {"name": "studio", "ssh_target": "", "online": True, "is_api_host": True},
        {"name": "laptop", "ssh_target": "user@laptop.example", "online": True, "is_api_host": False},
    ],
    "refreshed_at": "2026-01-01T00:00:00Z",
}

_MODEL_CATALOG = {
    "engines": {"claude": [], "codex": [], "local": [], "hermes": []},
    "refreshed_at": "2026-01-01T00:00:00Z",
    "stale": False,
}

_SESSION_CARD = {
    "kind": "task", "id": "t1", "title": "Fix the flaky test",
    "notes": "", "status": "in_progress", "tags": ["claude"], "assignee": "claude",
    "fields": {}, "context": "Work", "updated_at": "2026-01-01T00:00:00+00:00",
    "pending_question": None,
    "session": {
        "session_id": "cc:test-session-1",
        "source": "claude_code",
        "routing": "claude_code",
        "status": "inactive",
        "status_inferred": True,
        "host": "laptop",
        "branch": "",
        "prompt_preview": "",
        "decoded_cwd": "/home/user/proj",
        "total_dollars": 0.01,
        "total_input_tokens": 10,
        "total_output_tokens": 20,
        "spawn_depth": 0,
        "label": "Test session",
        "custom_label": None,
        "short_label": None,
        "is_subagent": False,
    },
}


def _board_fixture(card=None):
    return {
        "lanes": {
            "unassigned": [], "assigned": [card or _SESSION_CARD], "in_progress": [],
            "human_queue": [], "scheduled": [], "review": [], "done": [],
        },
        "generated_at": 0,
    }


def _session_card(**session_overrides):
    """Deep copy of `_SESSION_CARD` with `session` field overrides — lets a
    test vary the linked session's `host` (or other fields) without
    mutating the shared module-level fixture."""
    card = copy.deepcopy(_SESSION_CARD)
    card["session"].update(session_overrides)
    return card


class _AgentsHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the agents board the way api/main.py does: `/agents` is
    agents.html and the module tree hangs off `/static/`."""

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


def _open_board_with_drawer(
    page: Page, base_url, *, resume_status=200, resume_detail=None,
    hosts_status=200, hosts_fixture=None, hang_hosts=False, snapshot_api_host=None,
    capture_focus=False, focus_status=200, focus_detail=None,
    card=None, omit_api_host=False, pending_stream_routes=None,
):
    """Loads /agents, stubs every API call, opens the one card's drawer
    (which mounts a SessionPanel for its linked `claude_code` session),
    and returns the list of parsed JSON bodies posted to .../resume (or,
    with `capture_focus=True`, a `(resume_calls, focus_calls)` pair).

    `hosts_status`/`hosts_fixture` let a test simulate GET /api/agents/hosts
    404ing (unavailable) or returning a custom host list; `hang_hosts`
    simulates it never responding at all (never calls `route.fulfill`, so
    the request stays pending — Playwright's documented pattern for a
    loading-state test). `snapshot_api_host` feeds GET /api/agents/snapshot's
    `api_host` field the resume-host fallback reads when /hosts is
    unavailable. `pending_stream_routes`, when given a list, appends
    `GET /api/agents/board/stream`'s (board.js's `EventSource`) Playwright
    `route` object to it WITHOUT fulfilling — letting a test fulfill it
    itself, later, once the drawer's PRE-update state has already been
    asserted. Fulfilling inside this helper's own
    handler would race the initial render, since `connectStream()` opens
    this connection at page load, before the test's click ever happens.
    """
    resume_calls: list[dict] = []
    focus_calls: list[dict] = []

    def handler(route):
        req = route.request
        url = req.url
        if url.endswith("/api/agents/board/stream"):
            if pending_stream_routes is not None:
                pending_stream_routes.append(route)
            return  # never fulfilled here — inert unless a test fulfills it later
        if re.search(r"/api/agents/board(\?|$)", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_board_fixture(card)))
            return
        if url.endswith("/api/agents/hosts"):
            if hang_hosts:
                return  # never fulfilled — simulates a hanging endpoint
            body = hosts_fixture if hosts_fixture is not None else _HOSTS_FIXTURE
            route.fulfill(status=hosts_status, content_type="application/json", body=json.dumps(body))
            return
        if re.search(r"/api/agents/snapshot(\?|$)", url):
            payload = {"sessions": [], "edges": [], "generated_at": 0}
            # `omit_api_host` simulates a snapshot
            # response that genuinely carries no `api_host` field (as
            # opposed to `snapshot_api_host` below, which supplies one) —
            # `_apiHostName()` treats a missing/non-string field as
            # "unknown", not as the empty string.
            if not omit_api_host:
                payload["api_host"] = snapshot_api_host or "synthetic-api-host"
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        if url.endswith("/api/agents/models"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
            return
        if re.search(r"/sessions/[^/]+/summary", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"short_label": "", "body": ""}))
            return
        if re.search(r"/sessions/[^/]+/events", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"events": [], "total": 0}))
            return
        if re.search(r"/sessions/[^/]+/resume", url) and req.method == "POST":
            try:
                resume_calls.append(json.loads(req.post_data or "{}"))
            except json.JSONDecodeError:
                resume_calls.append({})
            if resume_status == 200:
                body = {
                    "spawned": True, "pid": 4242, "pane_id": None,
                    "command": [], "cwd": "/home/user/proj",
                    "inner_command": "", "clipboard_copied": False,
                }
            else:
                body = {"detail": resume_detail}
            route.fulfill(status=resume_status, content_type="application/json", body=json.dumps(body))
            return
        if re.search(r"/sessions/[^/]+/focus", url) and req.method == "POST":
            try:
                focus_calls.append(json.loads(req.post_data or "{}"))
            except json.JSONDecodeError:
                focus_calls.append({})
            if focus_status == 200:
                body = {"focused": True}
            else:
                body = {"detail": focus_detail}
            route.fulfill(status=focus_status, content_type="application/json", body=json.dumps(body))
            return
        # Default: stub anything else empty so nothing depends on a live server.
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handler)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('.board-card[data-card-id="t1"]')
    page.locator('.board-card[data-card-id="t1"]').click()
    page.wait_for_selector('[data-action="resume"]')
    if capture_focus:
        return resume_calls, focus_calls
    return resume_calls


class TestResumeHostSelect:
    def test_select_lists_api_host_and_registry_hosts(self, page: Page, agents_base_url):
        _open_board_with_drawer(page, agents_base_url)
        select = page.locator('[data-action="resume-host"]')
        expect(select).to_be_visible()
        options = select.locator("option").all_inner_texts()
        assert any("studio" in o for o in options), options
        assert any("this machine" in o for o in options), options
        assert any(o.strip() == "laptop" for o in options), options


class TestResumeSendsTargetHost:
    def test_choosing_host_and_clicking_resume_sends_target_host(self, page: Page, agents_base_url):
        resume_calls = _open_board_with_drawer(page, agents_base_url)
        page.locator('[data-action="resume-host"]').select_option("laptop")
        with page.expect_response(lambda r: "/resume" in r.url):
            page.locator('[data-action="resume"]').click()
        assert resume_calls, "no resume POST was captured"
        assert resume_calls[-1].get("target_host") == "laptop"

    def test_choosing_a_different_host_sends_that_one(self, page: Page, agents_base_url):
        resume_calls = _open_board_with_drawer(page, agents_base_url)
        page.locator('[data-action="resume-host"]').select_option("studio")
        with page.expect_response(lambda r: "/resume" in r.url):
            page.locator('[data-action="resume"]').click()
        assert resume_calls[-1].get("target_host") == "studio"


class TestResumeCommandOn400:
    def test_400_with_command_renders_copyable_command_text(self, page: Page, agents_base_url):
        expected_command = "cd /home/user/proj && claude --resume test-session-1"
        _open_board_with_drawer(
            page, agents_base_url, resume_status=400,
            resume_detail={
                "error": "host 'ghost' is not this API host and not in LIFEOS_AGENT_HOSTS",
                "command": expected_command,
            },
        )
        with page.expect_response(lambda r: "/resume" in r.url):
            page.locator('[data-action="resume"]').click()

        code = page.locator('[data-field="resume-command-text"]')
        expect(code).to_be_visible()
        expect(code).to_have_text(expected_command)

    def test_a_200_response_does_not_show_the_command_box(self, page: Page, agents_base_url):
        _open_board_with_drawer(page, agents_base_url)
        box = page.locator('[data-field="resume-command"]')
        # Present in the DOM (rendered whenever Resume is shown) but hidden
        # until a 400-with-command response actually populates it.
        expect(box).to_be_hidden()
        with page.expect_response(lambda r: "/resume" in r.url):
            page.locator('[data-action="resume"]').click()
        expect(box).to_be_hidden()


class TestResumeHostSelectFallback:
    """When GET /api/agents/hosts is unavailable or fails, the select must
    still offer a real, selectable API host — not just the session's own
    recorded host."""

    def test_404_hosts_endpoint_falls_back_to_api_host_and_session_host(self, page: Page, agents_base_url):
        _open_board_with_drawer(page, agents_base_url, hosts_status=404, snapshot_api_host="fallback-api-host")
        select = page.locator('[data-action="resume-host"]')
        expect(select.locator("option")).to_have_count(2)
        options = select.locator("option").all_inner_texts()
        assert any("fallback-api-host" in o and "this machine" in o for o in options), options
        assert any(o.strip() == "laptop" for o in options), options
        # And it must be a real, selectable option — not decorative text.
        select.select_option("fallback-api-host")
        assert select.input_value() == "fallback-api-host"

    def test_hanging_hosts_endpoint_falls_back_after_timeout(self, page: Page, agents_base_url):
        """The fetch carries an AbortSignal.timeout, so
        a GET /api/agents/hosts that never responds doesn't permanently
        leave the select with zero options."""
        _open_board_with_drawer(page, agents_base_url, hang_hosts=True, snapshot_api_host="hang-fallback-host")
        select = page.locator('[data-action="resume-host"]')
        expect(select.locator("option")).to_have_count(2, timeout=8000)
        options = select.locator("option").all_inner_texts()
        assert any("hang-fallback-host" in o and "this machine" in o for o in options), options
        assert any(o.strip() == "laptop" for o in options), options


class TestResumeHostOptionValue:
    def test_dotted_host_name_option_value_matches_display_text(self, page: Page, agents_base_url):
        """`escapeAttr` builds the option's
        `value=` attribute from a template string, replacing every char
        outside `[a-zA-Z0-9_-]` with `_` — a dotted host like
        `mac-mini.local` must display correctly and NOT post the mangled
        `mac-mini_local`. Options are now built via
        `document.createElement('option')` with `.value` assigned as a
        property, so the posted value matches the registry name exactly."""
        dotted_hosts = {
            "hosts": [
                {"name": "studio", "ssh_target": "", "online": True, "is_api_host": True},
                {"name": "mac-mini.local", "ssh_target": "user@mac-mini.local", "online": True, "is_api_host": False},
            ],
            "refreshed_at": "2026-01-01T00:00:00Z",
        }
        resume_calls = _open_board_with_drawer(page, agents_base_url, hosts_fixture=dotted_hosts)
        select = page.locator('[data-action="resume-host"]')
        select.select_option("mac-mini.local")
        with page.expect_response(lambda r: "/resume" in r.url):
            page.locator('[data-action="resume"]').click()
        assert resume_calls, "no resume POST was captured"
        assert resume_calls[-1].get("target_host") == "mac-mini.local"


class TestResumeHostsValidation:
    def test_hosts_response_with_invalid_rows_is_filtered(self, page: Page, agents_base_url):
        """Round-1 finding #17: a row with no non-empty string `name`
        (missing, or explicitly null) must not render as a bogus
        'null'/'undefined' option that would post a bogus target_host."""
        bad_hosts = {
            "hosts": [
                {"name": "studio", "is_api_host": True},
                {"name": None, "is_api_host": True},
                {"is_api_host": False},
                {"name": "laptop", "is_api_host": False},
            ],
        }
        _open_board_with_drawer(page, agents_base_url, hosts_fixture=bad_hosts)
        select = page.locator('[data-action="resume-host"]')
        options = select.locator("option").all_inner_texts()
        assert not any(("null" in o or "undefined" in o) for o in options), options
        assert any(o.strip() == "laptop" for o in options), options
        expect(select.locator("option")).to_have_count(2)


class TestFocusSendsTargetHost:
    """`_focusSession` must send the selected
    resume-host as `target_host` — the UI side of the backend's `/focus`
    `target_host` param."""

    def test_focus_sends_selected_target_host(self, page: Page, agents_base_url):
        _resume_calls, focus_calls = _open_board_with_drawer(page, agents_base_url, capture_focus=True)
        page.locator('[data-action="resume-host"]').select_option("studio")
        with page.expect_response(lambda r: "/focus" in r.url):
            page.locator('[data-action="focus"]').click()
        assert focus_calls, "no focus POST was captured"
        assert focus_calls[-1].get("target_host") == "studio"

    def test_focus_400_with_command_renders_copyable_command_text(self, page: Page, agents_base_url):
        """The other half of finding #13: a 400 whose detail carries
        `{error, command}` must render as readable, copyable text — the
        same handling `_resumeSession` already has — not `[object Object]`."""
        expected_command = "cd /home/user/proj && claude --resume test-session-1"
        _resume_calls, _focus_calls = _open_board_with_drawer(
            page, agents_base_url, capture_focus=True, focus_status=400,
            focus_detail={
                "error": "host 'ghost' is not this API host and not in LIFEOS_AGENT_HOSTS",
                "command": expected_command,
            },
        )
        with page.expect_response(lambda r: "/focus" in r.url):
            page.locator('[data-action="focus"]').click()
        code = page.locator('[data-field="resume-command-text"]')
        expect(code).to_be_visible()
        expect(code).to_have_text(expected_command)


class TestResumeHostSelectIncludesSessionsOwnHost:
    """The registry list from `GET /api/agents/hosts`
    alone may not include this session's own recorded host — e.g. it was
    registered by the hook on a machine not listed in LIFEOS_AGENT_HOSTS.
    Without adding it, the select defaults to its first option (the API
    host after the `is_api_host` sort) and "Go To"/"Resume" silently
    target the wrong machine."""

    def test_session_host_missing_from_registry_is_added_and_selected(self, page: Page, agents_base_url):
        card = _session_card(host="orchard")  # not in _HOSTS_FIXTURE
        _open_board_with_drawer(page, agents_base_url, card=card)
        select = page.locator('[data-action="resume-host"]')
        options = select.locator("option").all_inner_texts()
        assert any(o.strip() == "orchard" for o in options), options
        assert select.input_value() == "orchard"

    def test_resume_still_targets_the_added_session_host_by_default(self, page: Page, agents_base_url):
        card = _session_card(host="orchard")
        resume_calls = _open_board_with_drawer(page, agents_base_url, card=card)
        with page.expect_response(lambda r: "/resume" in r.url):
            page.locator('[data-action="resume"]').click()
        assert resume_calls, "no resume POST was captured"
        assert resume_calls[-1].get("target_host") == "orchard"


class TestNoKnowableHostOmitsTargetHost:
    """When neither `/api/agents/hosts` nor
    `/api/agents/snapshot`'s `api_host` resolves AND the session itself
    carries no recorded host, there is nothing real to offer — the select
    must not fall back to sending the human-readable placeholder "this
    host" as a machine identifier (a 400 in practice)."""

    def test_placeholder_option_has_empty_value(self, page: Page, agents_base_url):
        card = _session_card(host="")
        _open_board_with_drawer(page, agents_base_url, card=card, hosts_status=404, omit_api_host=True)
        select = page.locator('[data-action="resume-host"]')
        expect(select.locator("option")).to_have_count(1)
        assert select.input_value() == ""

    def test_resume_omits_target_host_entirely(self, page: Page, agents_base_url):
        card = _session_card(host="")
        resume_calls = _open_board_with_drawer(
            page, agents_base_url, card=card, hosts_status=404, omit_api_host=True,
        )
        with page.expect_response(lambda r: "/resume" in r.url):
            page.locator('[data-action="resume"]').click()
        assert resume_calls, "no resume POST was captured"
        assert "target_host" not in resume_calls[-1]


class TestUpdateMetaHidesResumeWhenSessionGoesLive:
    """`updateMeta` (the in-place refresh a board SSE
    tick uses) must re-evaluate `showResume` — a mirrored session
    commonly opens `inactive` (Resume + host select rendered) and later
    flips to `running` via a hook event with no full re-render in
    between. The stale controls must be hidden once the session is
    actually live, not just at the next full drawer open."""

    def test_resume_and_select_hide_when_status_flips_to_running(self, page: Page, agents_base_url):
        """The drawer must be FOCUSED (e.g. the operator mid-edit in
        Notes) for this to isolate `updateMeta` specifically:
        `updateOpenDrawer` (board.js) skips its full drawer rebuild
        (`renderDrawer`, which would reopen the panel from scratch and
        incidentally recompute `showResume` correctly on its own) while
        the drawer has focus — `panel.updateMeta` is the ONLY thing that
        still runs in that case, which is exactly the reachable scenario
        finding #8 reported live."""
        running_card = _session_card(status="running", status_inferred=False)
        pending_routes = []
        _open_board_with_drawer(page, agents_base_url, pending_stream_routes=pending_routes)
        resume_btn = page.locator('[data-action="resume"]')
        select = page.locator('[data-action="resume-host"]')
        # Pre-update state, asserted BEFORE any SSE frame is sent — proves
        # this is a live transition, not the initial render already having
        # the running status baked in.
        expect(resume_btn).to_be_visible()
        expect(select).to_be_visible()
        page.locator('[data-field="notes"]').click()

        assert pending_routes, "no SSE connection to /api/agents/board/stream was captured"
        frame = "event: board\ndata: " + json.dumps(_board_fixture(running_card)) + "\n\n"
        pending_routes[0].fulfill(status=200, content_type="text/event-stream", body=frame)

        expect(resume_btn).to_be_hidden(timeout=8000)
        expect(select).to_be_hidden(timeout=8000)

    def test_resume_and_select_reappear_if_status_goes_back_inactive(self, page: Page, agents_base_url):
        """Contrast case: the toggle must work in both directions, not
        just hide-and-forget — a session that goes live and later becomes
        resumable again must show Resume/select once more. Kept focused
        throughout for the same reason as the test above."""
        running_card = _session_card(status="running", status_inferred=False)
        inactive_again_card = _session_card(status="inactive", status_inferred=True)
        pending_routes = []
        _open_board_with_drawer(page, agents_base_url, pending_stream_routes=pending_routes)
        resume_btn = page.locator('[data-action="resume"]')
        select = page.locator('[data-action="resume-host"]')
        expect(resume_btn).to_be_visible()
        page.locator('[data-field="notes"]').click()

        assert pending_routes, "no SSE connection to /api/agents/board/stream was captured"
        # `retry: 50` tells EventSource to reconnect quickly once this
        # response ends, so the second frame below can ride the natural
        # reconnect instead of waiting out the ~3s browser default.
        frame1 = "retry: 50\nevent: board\ndata: " + json.dumps(_board_fixture(running_card)) + "\n\n"
        pending_routes[0].fulfill(status=200, content_type="text/event-stream", body=frame1)
        expect(resume_btn).to_be_hidden(timeout=8000)
        expect(select).to_be_hidden(timeout=8000)

        for _ in range(50):
            if len(pending_routes) >= 2:
                break
            page.wait_for_timeout(100)
        assert len(pending_routes) >= 2, "SSE did not reconnect after the first frame"
        frame2 = "event: board\ndata: " + json.dumps(_board_fixture(inactive_again_card)) + "\n\n"
        pending_routes[1].fulfill(status=200, content_type="text/event-stream", body=frame2)

        expect(resume_btn).to_be_visible(timeout=8000)
        expect(select).to_be_visible(timeout=8000)


# ---------------------------------------------------------------------------
# The Board-tab tests above all open the drawer on an `inactive` card, so
# the elements are already rendered before `updateMeta` ever runs. That
# doesn't exercise a drawer opened on a `running` session: `_renderHeader`
# always creates the Resume button and host select (hidden when
# `!showResume`), so `updateMeta`'s `querySelector` lookups find them and
# the toggle can show them once the session ends — the routine case
# (every session ends). The Graph tab is the tab where this path is
# unconditionally reachable — panel.open() only runs on node/search-result
# click, and the only refresh path once the panel is open is graph.js's
# applySnapshot -> panel.updateMeta (no full re-render on every tick, unlike
# the Board tab's unfocused-drawer self-heal).
# ---------------------------------------------------------------------------

GRAPH_SNAPSHOT_RUNNING = {
    "sessions": [
        {
            "session_id": "cc:graph-resume-target",
            "status": "running",
            "status_inferred": False,
            "routing": "claude_code",
            "source": "claude_code",
            "host": "laptop",
            "branch": "",
            "prompt_preview": "",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 10,
            "total_output_tokens": 20,
            "total_dollars": 0.01,
            "spawn_depth": 0,
            "label": "Graph resume target session",
            "custom_label": None,
            "short_label": None,
            "is_subagent": False,
            "decoded_cwd": "/home/synthetic/proj",
        },
    ],
    "edges": [],
    "generated_at": 1234567890,
}


def _open_graph_panel_on_running_session(page: Page, base_url, pending_stream_routes):
    """Loads /agents, opens the Graph tab, and opens the side panel for a
    session whose INITIAL status is `running` — the case the Board-tab
    helper above never reaches, since its fixture card always starts
    `inactive`. Captures (without fulfilling) the Graph tab's own
    `GET /api/agents/stream` EventSource route in `pending_stream_routes`
    so the test can push a later snapshot frame itself, after the
    pre-transition state has already been asserted."""

    def handler(route):
        req = route.request
        url = req.url
        if url.endswith("/api/agents/stream"):
            pending_stream_routes.append(route)
            return  # never fulfilled here — captured for the test to drive
        if re.search(r"/api/agents/snapshot(\?|$)", url):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(GRAPH_SNAPSHOT_RUNNING))
            return
        if url.endswith("/api/agents/hosts"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOSTS_FIXTURE))
            return
        if re.search(r"/api/agents/board(\?|$)", url) or url.endswith("/api/agents/board/stream"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"lanes": {}, "generated_at": 0}))
            return
        # Default: stub anything else empty so nothing depends on a live server.
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/agents")
    page.click('[data-tab="graph"]')
    # Not `#filter-host` — graph.js hides that filter's wrapping `<label>`
    # entirely when the snapshot has one distinct host or fewer
    # (`updateHostOptions`), which this single-session fixture always is.
    # `#search-input` isn't conditionally hidden and is present as soon as
    # the Graph tab's static markup is unhidden.
    page.wait_for_selector("#search-input")
    page.select_option("#filter-recency", "all")
    page.locator("#search-input").fill("resume target session")
    result = page.locator(".search-result", has_text="resume target session")
    expect(result).to_be_visible()
    result.click()


class TestGraphTabRendersResumeControlsOnLiveSessionForLaterReveal:
    def test_running_session_gets_hidden_but_present_controls_that_become_visible_and_populated_on_terminal(
        self, page: Page, agents_base_url,
    ):
        pending_routes = []
        _open_graph_panel_on_running_session(page, agents_base_url, pending_routes)

        panel = page.locator("#panel")
        resume_btn = panel.locator('[data-action="resume"]')
        select = panel.locator('[data-action="resume-host"]')

        # Pre-transition: the session is `running`, so Resume is not
        # actionable yet — but (finding #1's fix) the elements must already
        # be IN THE DOM, just hidden, so a later in-place `updateMeta` can
        # reveal them. Under the pre-fix (revert-to-conditional-creation)
        # behaviour these elements never exist at all, and `to_have_count(1)`
        # below fails.
        expect(resume_btn).to_have_count(1)
        expect(select).to_have_count(1)
        expect(resume_btn).to_be_hidden()
        expect(select).to_be_hidden()

        assert pending_routes, "no SSE connection to /api/agents/stream was captured"
        terminal_snapshot = json.loads(json.dumps(GRAPH_SNAPSHOT_RUNNING))
        terminal_snapshot["sessions"][0]["status"] = "completed"
        terminal_snapshot["sessions"][0]["status_inferred"] = False
        frame = "event: snapshot\ndata: " + json.dumps(terminal_snapshot) + "\n\n"
        pending_routes[0].fulfill(status=200, content_type="text/event-stream", body=frame)

        # Positive direction: once the session reaches a terminal state,
        # Resume and the host select must end up VISIBLE — the routine case
        # (every session ends) that finding #1 reported as unreachable via
        # updateMeta alone on the Graph tab.
        expect(resume_btn).to_be_visible(timeout=8000)
        expect(select).to_be_visible(timeout=8000)
        # And POPULATED, not merely visible with no options — panel.js's
        # `_populateResumeHosts` is called at initial `_renderHeader` time
        # (bound to `canResume`, not `showResume`), so the options should
        # already be there by the time visibility flips.
        options = select.locator("option").all_inner_texts()
        assert any("studio" in o for o in options), options
        assert any(o.strip() == "laptop" for o in options), options
