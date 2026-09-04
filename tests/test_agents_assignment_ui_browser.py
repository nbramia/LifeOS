"""Browser test for web/agents/assignment.js (#851) — the isolated
model/effort/host/engine assignment-picker module, built ahead of the
Kanban board UI (#850) merging (see this PR's description for why it's
standalone rather than wired into web/agents/board.js).

Serves `web/` itself from an ephemeral port (same pattern as
tests/test_voice_mic_block_ui_browser.py) rather than pointing at a running
API — every `/api/**` call the page makes is stubbed, so this carries no
`requires_server` marker and runs at pre-push (`browser and not
requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

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


_MODEL_CATALOG = {
    "engines": {
        "claude": [
            {"id": "claude-opus-5", "label": "Claude Opus 5", "pricing": {"input": 5e-6, "output": 25e-6}},
            {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "pricing": {"input": 2e-6, "output": 10e-6}},
        ],
        "codex": [{"id": "gpt-5.5", "label": "GPT-5.5", "pricing": None}],
        "local": [],
        "hermes": [],
    },
    "refreshed_at": "2026-01-01T00:00:00Z",
    "stale": False,
}

# Synthetic host registry (#883) — one of each `online` state: the API
# host itself, an online registry host, an offline one, and one Tailscale
# couldn't place (`online: null` -> "(unknown)").
_HOST_CATALOG = {
    "hosts": [
        {"name": "desktop-box", "ssh_target": None, "online": True, "is_api_host": True},
        {"name": "studio-box", "ssh_target": "operator@studio-box.example", "online": True, "is_api_host": False},
        {"name": "laptop", "ssh_target": "operator@laptop.example", "online": False, "is_api_host": False},
        {"name": "mystery-box", "ssh_target": "operator@mystery-box.example", "online": None, "is_api_host": False},
    ],
    "refreshed_at": "2026-01-01T00:00:00Z",
}


def _load_module(page: Page, base_url: str, *, api_handler=None):
    """Serve a page on the target origin, stub every /api/ call, and inject
    assignment.js as a module — exposing `window.__renderAssignmentPickers`
    for the test to drive."""
    def default_handler(route):
        if "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/agents/hosts" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t1", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", api_handler or default_handler)
    # agents.html is a real, already-served page — used purely as a host
    # document so assignment.js's relative-to-origin import resolves; its
    # own legacy inline scripts hit /api/** too, which the stub above
    # already covers harmlessly.
    page.goto(f"{base_url}/agents.html")
    page.add_script_tag(
        content="""
        import { renderAssignmentPickers } from '/agents/assignment.js';
        window.__renderAssignmentPickers = renderAssignmentPickers;
        window.__assignmentReady = true;
        """,
        type="module",
    )
    page.wait_for_function("() => window.__assignmentReady === true")


def _render(page: Page, card: dict):
    """Render with a fake, always-succeeding putTask. Also installs an
    onError spy (`window.__errorCalls`) so tests can assert a successful
    save fires it zero times, alongside the existing `window.__lastCalls`
    PUT-call spy."""
    page.evaluate(
        """(card) => {
            const container = document.createElement('div');
            container.id = 'test-assignment-container';
            document.body.appendChild(container);
            window.__lastCalls = [];
            window.__errorCalls = [];
            window.__renderAssignmentPickers(container, card, {
                putTask: (id, patch) => {
                    window.__lastCalls.push({ id, patch });
                    return Promise.resolve({ id, ...patch });
                },
                onError: (message) => { window.__errorCalls.push(message); },
            });
        }""",
        card,
    )


def test_renders_engine_model_effort_host_pickers(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {"id": "t1", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})

    container = page.locator("#test-assignment-container")
    expect(container.locator("[data-field='assignee']")).to_be_visible()
    expect(container.locator("[data-row='model']")).to_be_visible()
    expect(container.locator("[data-row='effort']")).to_be_visible()
    expect(container.locator("[data-row='host']")).to_be_visible()
    # Model options populate asynchronously from the catalog fetch.
    expect(container.locator("[data-field='model'] option")).to_have_count(3)  # default + 2 claude models


def test_local_engine_hides_model_and_host_pickers(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {"id": "t2", "title": "Reindex the vault", "tags": ["local"], "assignee": "local", "fields": {}})

    container = page.locator("#test-assignment-container")
    expect(container.locator("[data-row='model']")).to_be_hidden()
    expect(container.locator("[data-row='effort']")).to_be_visible()
    expect(container.locator("[data-row='host']")).to_be_hidden()


def test_hermes_engine_hides_model_effort_and_host_pickers(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {"id": "t3", "title": "Ask hermes", "tags": ["hermes"], "assignee": "hermes", "fields": {}})

    container = page.locator("#test-assignment-container")
    expect(container.locator("[data-row='model']")).to_be_hidden()
    expect(container.locator("[data-row='effort']")).to_be_hidden()
    expect(container.locator("[data-row='host']")).to_be_hidden()


def test_changing_effort_saves_with_assigned_by_board(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {"id": "t4", "title": "Deploy the service", "tags": ["codex"], "assignee": "codex", "fields": {}})

    page.locator("[data-field='effort']").select_option("high")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    assert calls[0]["id"] == "t4"
    assert calls[0]["patch"]["fields"]["effort"] == "high"
    assert calls[0]["patch"]["fields"]["assigned_by"] == "board"


def test_changing_engine_updates_tags_and_saves(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {"id": "t5", "title": "Fix the printer", "tags": ["agent"], "assignee": "", "fields": {}})

    page.locator("[data-field='assignee']").select_option("codex")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    assert calls[0]["patch"]["tags"] == ["codex", "agent"]
    assert calls[0]["patch"]["fields"]["assigned_by"] == "board"


def test_shows_what_actually_ran_from_session(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {
        "id": "t6", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
        "fields": {"model": "opus", "effort": "high"},
        "session": {"model": "opus", "effort": "high", "host": "studio"},
    })
    container = page.locator("#test-assignment-container")
    ran_text = container.locator("[data-field='ran']").inner_text()
    assert "opus" in ran_text
    assert "high" in ran_text
    assert "studio" in ran_text


def test_effort_change_before_catalog_resolves_does_not_clear_model(page: Page, web_base_url):
    """#861 regression: changing effort/host before GET /api/agents/models
    resolves must not send `model: null` and clear a previously saved model
    field — the model select's options (and thus its value) don't exist yet
    on the first drawer open of a page load, so `save()` must omit the
    `model` key entirely until the catalog has populated the select."""
    pending = []

    def api_handler(route):
        if "/api/agents/models" in route.request.url:
            pending.append(route)  # stashed — fulfilled later in the test
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t1", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    _render(page, {
        "id": "t8", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
        "fields": {"model": "claude-sonnet-5", "effort": "medium"},
    })

    # Catalog hasn't resolved yet (its route is stashed) — change effort now.
    page.locator("[data-field='effort']").select_option("high")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    fields = calls[0]["patch"]["fields"]
    assert fields["effort"] == "high"
    assert fields["assigned_by"] == "board"
    assert "model" not in fields  # omitted, not nulled — must not clear the saved model

    # Now let the catalog resolve (fulfill every stashed models route).
    for route in pending:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
    pending.clear()

    container = page.locator("#test-assignment-container")
    expect(container.locator("[data-field='model'] option")).to_have_count(3)  # default + 2 claude models

    # Once the catalog is ready, effort changes carry the saved model along.
    page.locator("[data-field='effort']").select_option("low")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 2
    fields2 = calls[1]["patch"]["fields"]
    assert fields2["model"] == "claude-sonnet-5"


def test_put_failure_surfaces_error_without_crashing(page: Page, web_base_url):
    _load_module(page, web_base_url)
    page.evaluate(
        """(card) => {
            const container = document.createElement('div');
            container.id = 'test-assignment-container';
            document.body.appendChild(container);
            window.__renderAssignmentPickers(container, card, {
                putTask: () => Promise.reject(new Error('save failed')),
            });
        }""",
        {"id": "t7", "title": "Fix the printer", "tags": ["codex"], "assignee": "codex", "fields": {}},
    )
    page.locator("[data-field='effort']").select_option("low")
    error_el = page.locator("#test-assignment-container [data-field='error']")
    expect(error_el).to_be_visible()
    expect(error_el).to_contain_text("save failed")


# ---------------------------------------------------------------------------
# #883: no false success toast; host picker as a registry dropdown
# ---------------------------------------------------------------------------

def test_successful_save_fires_no_error_callback(page: Page, web_base_url):
    """The regression this issue exists for: `board.js` turns every
    `onError` call into a red toast, so a successful save must fire it
    ZERO times — not `onError('')`."""
    _load_module(page, web_base_url)
    _render(page, {"id": "t9", "title": "Fix the printer", "tags": ["codex"], "assignee": "codex", "fields": {}})

    page.locator("[data-field='effort']").select_option("high")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1  # the save did happen...
    error_calls = page.evaluate("() => window.__errorCalls")
    assert error_calls == []  # ...but onError was never called for it
    error_el = page.locator("#test-assignment-container [data-field='error']")
    expect(error_el).to_be_hidden()


def test_failed_save_fires_onerror_exactly_once_with_response_detail(page: Page, web_base_url):
    """Drives the REAL `defaultPutTask` (no `opts.putTask` override) against
    a stubbed 4xx response, so this proves `onError`'s message is exactly
    the server's `detail` — not a generic HTTP-status string — and fires
    exactly once."""
    def api_handler(route):
        if "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/agents/hosts" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            route.fulfill(
                status=422, content_type="application/json",
                body=json.dumps({"detail": "effort must be one of low/medium/high/max"}),
            )
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    page.evaluate(
        """(card) => {
            const container = document.createElement('div');
            container.id = 'test-assignment-container';
            document.body.appendChild(container);
            window.__errorCalls = [];
            window.__renderAssignmentPickers(container, card, {
                onError: (message) => { window.__errorCalls.push(message); },
            });
        }""",
        {"id": "t14", "title": "Fix the printer", "tags": ["codex"], "assignee": "codex", "fields": {}},
    )
    page.locator("[data-field='effort']").select_option("high")
    error_el = page.locator("#test-assignment-container [data-field='error']")
    expect(error_el).to_contain_text("effort must be one of low/medium/high/max")  # waits for the async PUT
    error_calls = page.evaluate("() => window.__errorCalls")
    assert error_calls == ["effort must be one of low/medium/high/max"]


def test_host_select_lists_this_machine_plus_registry_hosts_with_markers(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {"id": "t10", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})

    host_select = page.locator("#test-assignment-container [data-field='host']")
    options = host_select.locator("option")
    expect(options).to_have_count(1 + len(_HOST_CATALOG["hosts"]))
    texts = options.all_inner_texts()
    assert texts[0] == "this machine"
    assert "desktop-box" in texts       # online: true, is_api_host — no marker
    assert "studio-box" in texts        # online: true — no marker
    assert "laptop (offline)" in texts  # online: false
    assert "mystery-box (unknown)" in texts  # online: null


def test_selecting_host_puts_bare_name(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {"id": "t11", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})

    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))  # wait for the fetch

    host_select.select_option("studio-box")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    assert calls[0]["patch"]["fields"]["host"] == "studio-box"


def test_unknown_saved_host_still_appears_selected_and_flagged(page: Page, web_base_url):
    _load_module(page, web_base_url)
    _render(page, {
        "id": "t12", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
        "fields": {"host": "retired-box"},
    })
    host_select = page.locator("#test-assignment-container [data-field='host']")
    # "this machine" + every registry host + the one flagged-unknown extra.
    expect(host_select.locator("option")).to_have_count(2 + len(_HOST_CATALOG["hosts"]))
    unknown_option = host_select.locator("option[data-unknown='true']")
    expect(unknown_option).to_have_count(1)
    expect(unknown_option).to_have_text("retired-box (unknown)")
    assert host_select.input_value() == "retired-box"


def test_effort_change_before_hosts_resolve_preserves_saved_host(page: Page, web_base_url):
    """The synchronous-seed requirement (#883): the hosts fetch resolving
    asynchronously must never cause an early effort/engine change to write
    `host: null` over a card's already-saved host."""
    pending = []

    def api_handler(route):
        if "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/agents/hosts" in route.request.url:
            pending.append(route)  # stashed — fulfilled later in the test
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t13", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    _render(page, {
        "id": "t13", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
        "fields": {"host": "studio-box", "effort": "medium"},
    })

    # The hosts fetch hasn't resolved yet (stashed) — change effort now.
    page.locator("[data-field='effort']").select_option("high")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    fields = calls[0]["patch"]["fields"]
    assert fields["effort"] == "high"
    assert fields["host"] == "studio-box"  # preserved, not nulled

    # Now let the hosts fetch resolve — the saved host is a real registry
    # entry, so the flagged-unknown seed option is replaced by the real one.
    for route in pending:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
    pending.clear()

    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))
    assert host_select.input_value() == "studio-box"
