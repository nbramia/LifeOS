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


def _load_module(page: Page, base_url: str, *, api_handler=None):
    """Serve a page on the target origin, stub every /api/ call, and inject
    assignment.js as a module — exposing `window.__renderAssignmentPickers`
    for the test to drive."""
    def default_handler(route):
        if "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
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
    page.evaluate(
        """(card) => {
            const container = document.createElement('div');
            container.id = 'test-assignment-container';
            document.body.appendChild(container);
            window.__lastCalls = [];
            window.__renderAssignmentPickers(container, card, {
                putTask: (id, patch) => {
                    window.__lastCalls.push({ id, patch });
                    return Promise.resolve({ id, ...patch });
                },
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
