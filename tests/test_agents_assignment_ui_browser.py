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

# Synthetic host registry — one of each `online` state: the API
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


def test_all_pickers_including_engine_disabled_when_fields_policy_refuses(page: Page, web_base_url):
    """`board.js` hides the engine row inside its own drawer, but this
    module is also usable directly (as this test file does), where the
    row IS visible, and must not leave it silently editable while every
    other picker is disabled."""
    _load_module(page, web_base_url)
    _render(page, {
        "id": "t9", "title": "Migrate the database", "tags": ["codex", "agent-running"],
        "assignee": "codex", "fields": {},
        "policy": {"fields": {"allowed": False, "reason": "the worker owns this task"}},
    })
    container = page.locator("#test-assignment-container")
    expect(container.locator("[data-field='assignee']")).to_be_disabled()
    expect(container.locator("[data-field='model']")).to_be_disabled()
    expect(container.locator("[data-field='effort']")).to_be_disabled()
    expect(container.locator("[data-field='host']")).to_be_disabled()


def test_fields_policy_allowed_leaves_engine_and_pickers_enabled(page: Page, web_base_url):
    """Positive case for RC8: a card with no policy refusal (the common
    case — every test above this one already relies on this implicitly)
    leaves the engine select enabled too, not just the model/effort/host
    pickers."""
    _load_module(page, web_base_url)
    _render(page, {"id": "t10", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})
    container = page.locator("#test-assignment-container")
    expect(container.locator("[data-field='assignee']")).to_be_enabled()
    expect(container.locator("[data-field='effort']")).to_be_enabled()


def test_fields_disabled_reason_renders_in_neutral_element_not_error(page: Page, web_base_url):
    """The disabled-fields explanation renders in its own
    `.drawer-field-reason` element, not `.assignment-error` — that one is
    reserved for a genuinely failed save, so a normal explanation doesn't
    paint red."""
    _load_module(page, web_base_url)
    _render(page, {
        "id": "t11", "title": "Migrate the database", "tags": ["codex", "agent-running"],
        "assignee": "codex", "fields": {},
        "policy": {"fields": {"allowed": False, "reason": "the worker owns this task while it is running"}},
    })
    container = page.locator("#test-assignment-container")
    expect(container.locator("[data-field='fields-reason']")).to_contain_text("the worker owns this task")
    expect(container.locator("[data-field='error']")).to_be_hidden()


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
# No false success toast; host picker as a registry dropdown
# ---------------------------------------------------------------------------

def test_successful_save_fires_no_error_callback(page: Page, web_base_url):
    """The regression this issue exists for: `board.js` turns every
    `onError` call into a red toast, so a successful save must fire it
    ZERO times — not `onError('')`.

    The template renders `[data-field='error']`
    `hidden` from the start, so asserting it's hidden after only a
    SUCCESSFUL save would pass even if `clearError()` were a no-op. This
    test fails a save first — driving the element visible — then
    succeeds, so the final `to_be_hidden()` is a positive proof that
    `clearError()` actually ran, not an assertion that was already true
    before any save happened."""
    _load_module(page, web_base_url)
    page.evaluate(
        """(card) => {
            const container = document.createElement('div');
            container.id = 'test-assignment-container';
            document.body.appendChild(container);
            window.__lastCalls = [];
            window.__errorCalls = [];
            let attempt = 0;
            window.__renderAssignmentPickers(container, card, {
                putTask: (id, patch) => {
                    attempt += 1;
                    window.__lastCalls.push({ id, patch });
                    if (attempt === 1) return Promise.reject(new Error('boom'));
                    return Promise.resolve({ id, ...patch });
                },
                onError: (message) => { window.__errorCalls.push(message); },
            });
        }""",
        {"id": "t9", "title": "Fix the printer", "tags": ["codex"], "assignee": "codex", "fields": {}},
    )
    error_el = page.locator("#test-assignment-container [data-field='error']")

    # First save fails -- the error element becomes visible.
    page.locator("[data-field='effort']").select_option("high")
    expect(error_el).to_be_visible()
    expect(error_el).to_contain_text("boom")

    # Second save succeeds -- onError must not fire again, and the error
    # element must be genuinely hidden by clearError(), not merely
    # untouched from its initial template state.
    page.locator("[data-field='effort']").select_option("low")
    expect(error_el).to_be_hidden()

    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 2  # both saves happened
    error_calls = page.evaluate("() => window.__errorCalls")
    assert error_calls == ["boom"]  # exactly once, from the failed save only


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
    """Selects `laptop` — the `online: false` entry —
    rather than `studio-box` (`online: true`, whose label already equals
    its bare name). A mutation that writes the option's *label* instead of
    its *value* would leave `studio-box` green (label == value there
    anyway) but must fail here: `laptop`'s label is `"laptop (offline)"`,
    so a label/value mixup is only caught by asserting against this
    entry."""
    _load_module(page, web_base_url)
    _render(page, {"id": "t11", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})

    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))  # wait for the fetch

    host_select.select_option("laptop")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    assert calls[0]["patch"]["fields"]["host"] == "laptop"


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
    """The synchronous-seed requirement: the hosts fetch resolving
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
    # The seeded `(unknown)`-flagged option must be REPLACED once the real
    # catalog lands and shows studio-box is a known registry host — a
    # regression that kept the stale unknown flag around would pass every
    # assertion above without this one.
    expect(host_select.locator("option[data-unknown='true']")).to_have_count(0)


# ---------------------------------------------------------------------------
# Covers: live host value surviving the catalog landing; a transient
# hosts-fetch failure retrying on the next drawer open with the seed
# intact; client-side TTL re-fetching instead of caching forever; the
# flagged-unknown option surviving an engine change.
# ---------------------------------------------------------------------------

def test_host_change_before_hosts_resolve_survives_catalog_landing(page: Page, web_base_url):
    """A1: changing the HOST itself (not just effort — see
    test_effort_change_before_hosts_resolve_preserves_saved_host above,
    which is exactly why this slipped through review the first time)
    while GET /api/agents/hosts is still in flight must survive the
    catalog landing. populateHostOptions() must read the LIVE select
    value at render time, not the `currentHost` snapshot captured when
    the drawer first opened — reading the snapshot would revert the
    operator's choice the moment the catalog arrives, and a later
    unrelated save would then resurrect the stale host server-side."""
    pending = []

    def api_handler(route):
        if "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/agents/hosts" in route.request.url:
            pending.append(route)  # stashed — fulfilled later in the test
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t20", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    _render(page, {
        "id": "t20", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
        "fields": {"host": "laptop", "effort": "medium"},
    })

    host_select = page.locator("#test-assignment-container [data-field='host']")
    # The hosts fetch hasn't resolved yet (stashed) — pick "this machine"
    # right now, clearing the saved host, still inside the stashed window.
    host_select.select_option("")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    assert calls[0]["patch"]["fields"]["host"] is None

    # Now let the catalog land — the operator's LIVE choice ("this
    # machine", value "") must survive, not revert to the stale "laptop".
    for route in pending:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
    pending.clear()
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))
    assert host_select.input_value() == ""  # NOT reverted to "laptop"

    # A later, unrelated effort change must not resurrect the cleared host.
    page.locator("[data-field='effort']").select_option("high")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 2
    assert calls[1]["patch"]["fields"]["host"] is None  # still cleared, not "laptop"


def test_transient_hosts_fetch_failure_falls_back_to_seed_and_retries_next_drawer(page: Page, web_base_url):
    """R1: a transient `GET /api/agents/hosts` failure must not disable
    host assignment for the rest of the page session. The synchronous
    seed (this machine + the saved host, flagged unknown) must survive a
    failed fetch rather than collapsing to an empty list with no way to
    pick a host at all -- and the NEXT drawer open must retry, and on
    success populate the full registry list."""
    attempt = {"n": 0}

    def api_handler(route):
        if "/api/agents/hosts" in route.request.url:
            attempt["n"] += 1
            if attempt["n"] == 1:
                route.fulfill(status=503, content_type="application/json", body="{}")
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
        elif "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t21", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)

    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        _render(page, {
            "id": "t21", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
            "fields": {"host": "studio-box"},
        })

    host_select = page.locator("#test-assignment-container [data-field='host']")
    # The failed fetch must leave the synchronous seed intact -- NOT
    # collapse it to an empty catalog with nothing to pick from -- plus
    # the R7 "hosts unavailable" marker so the operator knows the
    # registry itself failed to load, not just "nothing is registered."
    expect(host_select.locator("option")).to_have_count(3)
    texts = host_select.locator("option").all_inner_texts()
    assert texts[0] == "this machine"
    assert "studio-box (unknown)" in texts
    assert any("hosts unavailable" in t for t in texts)
    assert host_select.input_value() == "studio-box"
    assert attempt["n"] == 1

    # The early return on a falsy catalog needs a mutation-proof pin: a
    # mutant that swaps `if (!catalog) return;` for `catalog = { hosts: [] };`
    # produces an IDENTICAL DOM at this point, because populateHostOptions's
    # own rebuild logic (given an empty hosts list and `current` still equal
    # to the seeded value) reconstructs the same two seed options. Pin it
    # by selecting away from the seeded host and back: the seeded
    # `data-unknown` option must still be present and re-selectable, and
    # picking it must still save the right value.
    host_select.select_option("")
    expect(host_select.locator("option[data-unknown='true']")).to_have_count(1)
    host_select.select_option("studio-box")
    assert host_select.input_value() == "studio-box"
    calls = page.evaluate("() => window.__lastCalls")
    assert calls[-1]["patch"]["fields"]["host"] == "studio-box"

    # A second drawer open retries -- and this time succeeds, replacing
    # the seed with the real registry list.
    page.evaluate("() => { document.getElementById('test-assignment-container').remove(); }")
    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        _render(page, {
            "id": "t22", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
            "fields": {"host": "studio-box"},
        })
    host_select2 = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select2.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))
    assert attempt["n"] == 2
    assert host_select2.input_value() == "studio-box"
    expect(host_select2.locator("option[data-unknown='true']")).to_have_count(0)


def test_host_catalog_refetches_after_client_ttl_and_renders_fresh_markers(page: Page, web_base_url):
    """R2: the client must not cache the host list forever the way it
    caches the (24h-TTL) model catalog. A drawer opened well past the
    client's short TTL (matched to host_catalog.py's own
    `_HOST_CATALOG_TTL_SECONDS`, 30s) must trigger a fresh fetch, and the
    newly-rendered reachability markers must reflect the fresh data --
    not just "a fetch happened", but the RIGHT fetch's data landing."""
    host_catalog_laptop_online = {
        "hosts": [
            {"name": "desktop-box", "ssh_target": None, "online": True, "is_api_host": True},
            {"name": "studio-box", "ssh_target": "operator@studio-box.example", "online": True, "is_api_host": False},
            {"name": "laptop", "ssh_target": "operator@laptop.example", "online": True, "is_api_host": False},  # was False
            {"name": "mystery-box", "ssh_target": "operator@mystery-box.example", "online": None, "is_api_host": False},
        ],
        "refreshed_at": "2026-01-01T01:00:00Z",
    }
    catalogs = [_HOST_CATALOG, host_catalog_laptop_online]
    attempt = {"n": 0}

    def api_handler(route):
        if "/api/agents/hosts" in route.request.url:
            catalog = catalogs[min(attempt["n"], len(catalogs) - 1)]
            attempt["n"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps(catalog))
        elif "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t23", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    page.clock.install()

    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        _render(page, {"id": "t23", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})
    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))
    assert attempt["n"] == 1
    assert "laptop (offline)" in host_select.locator("option").all_inner_texts()

    # A second drawer opened on the SAME page load, still within the
    # client TTL, must reuse the cached catalog -- no second fetch.
    page.evaluate("() => { document.getElementById('test-assignment-container').remove(); }")
    _render(page, {"id": "t24", "title": "Deploy", "tags": ["codex"], "assignee": "codex", "fields": {}})
    host_select_cached = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select_cached.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))
    assert attempt["n"] == 1  # still just the one fetch

    # Now advance well past the client TTL (~30s) and open a third drawer.
    page.clock.fast_forward(31_000)
    page.evaluate("() => { document.getElementById('test-assignment-container').remove(); }")
    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        _render(page, {"id": "t25", "title": "Reindex", "tags": ["claude"], "assignee": "claude", "fields": {}})
    host_select3 = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select3.locator("option")).to_have_count(1 + len(host_catalog_laptop_online["hosts"]))
    assert attempt["n"] == 2  # the stale-cache window forced a re-fetch
    texts3 = host_select3.locator("option").all_inner_texts()
    assert "laptop (offline)" not in texts3
    assert "laptop" in texts3  # the fresh "online" marker landed


def test_unknown_flagged_host_option_survives_engine_change(page: Page, web_base_url):
    """M5 regression pin: switching engines only toggles row visibility
    (`updateVisibility()`) -- it does NOT re-run populateHostOptions or
    seedHostOptions. A saved host that's dropped out of the registry must
    stay selected and flagged across an engine change between two engines
    that both show the host picker."""
    _load_module(page, web_base_url)
    _render(page, {
        "id": "t26", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude",
        "fields": {"host": "retired-box"},
    })
    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option[data-unknown='true']")).to_have_count(1)
    assert host_select.input_value() == "retired-box"

    # claude -> codex: both show the host picker.
    page.locator("[data-field='assignee']").select_option("codex")
    unknown_option = host_select.locator("option[data-unknown='true']")
    expect(unknown_option).to_have_count(1)
    expect(unknown_option).to_have_text("retired-box (unknown)")
    assert host_select.input_value() == "retired-box"


# ---------------------------------------------------------------------------
# Covers: a rejected host/effort/model change reverting instead of riding
# along on the next save; a permanently-failing hosts fetch cooling down
# instead of retrying once per drawer open; a failed/skipped fetch showing
# a disabled "unavailable" marker; the falsy-catalog mutation proof; and a
# stale failed fetch not clobbering a newer successful cache entry.
# ---------------------------------------------------------------------------

def test_rejected_host_save_reverts_then_next_save_sends_reverted_value(page: Page, web_base_url):
    """A3: a host change the server REJECTS must not ride along on the
    next unrelated save. Every other drawer control (title, notes,
    context, tags, the Assignee select) reverts to its last-known-good
    value on a failed save -- the host/effort/model pickers must too, and
    the NEXT save must send the reverted value, not the rejected one."""
    reject_next_host = {"on": False}
    puts = []

    def api_handler(route):
        if "/api/agents/hosts" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
        elif "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            puts.append(body)
            if reject_next_host["on"] and body.get("fields", {}).get("host") == "laptop":
                route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "laptop is unreachable"}))
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t50", **body}))
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
        {"id": "t50", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {"host": "studio-box", "effort": "medium"}},
    )
    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))  # wait for the catalog

    reject_next_host["on"] = True
    host_select.select_option("laptop")
    error_el = page.locator("#test-assignment-container [data-field='error']")
    expect(error_el).to_contain_text("laptop is unreachable")
    # The select snaps back to the last-known-good host (studio-box), NOT
    # left showing the rejected "laptop" -- the same revert-on-failed-save
    # convention every other drawer control follows, applied to the
    # host/effort/model pickers.
    expect(host_select).to_have_value("studio-box")

    # A later, unrelated effort change must send the REVERTED host, not
    # the rejected one -- and no toast, since this save succeeds.
    reject_next_host["on"] = False
    page.locator("[data-field='effort']").select_option("high")
    expect(error_el).to_be_hidden()
    last = puts[-1]["fields"]
    assert last["effort"] == "high"
    assert last["host"] == "studio-box"  # NOT "laptop"


def test_successful_host_change_sticks_and_rides_along_on_next_save(page: Page, web_base_url):
    """Positive counterpart to the A3 revert test above -- guarding
    against over-correction. A SUCCESSFUL host change must still stick
    (not be reverted by the same guard that reverts a rejected one), and
    the next save must carry the newly-saved host forward. This also
    exercises A1's live-value read alongside A3's revert tracking, since
    both read/write the same `hostEl.value`."""
    puts = []

    def api_handler(route):
        if "/api/agents/hosts" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
        elif "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            puts.append(body)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t51", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    page.evaluate(
        """(card) => {
            const container = document.createElement('div');
            container.id = 'test-assignment-container';
            document.body.appendChild(container);
            window.__renderAssignmentPickers(container, card, {});
        }""",
        {"id": "t51", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {"host": "studio-box", "effort": "medium"}},
    )
    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))

    host_select.select_option("laptop")
    expect(host_select).to_have_value("laptop")

    page.locator("[data-field='effort']").select_option("high")
    expect(page.locator("#test-assignment-container [data-field='error']")).to_be_hidden()
    assert puts[-1]["fields"]["host"] == "laptop"  # the successful change rode along
    assert host_select.input_value() == "laptop"


def test_failed_host_fetch_shows_unavailable_option_never_selectable(page: Page, web_base_url):
    """R7: a failed (or R6-cooldown-skipped) `/api/agents/hosts` fetch
    must tell the operator the registry itself failed to load -- not look
    like "no hosts are registered." The appended option is disabled so it
    can never become the selected value, and thus never rides along as
    `fields.host` on the next save."""
    def api_handler(route):
        if "/api/agents/hosts" in route.request.url:
            route.fulfill(status=503, content_type="application/json", body="{}")
        elif "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t27", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        _render(page, {"id": "t27", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})

    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(2)  # "this machine" + the unavailable marker
    unavailable = host_select.locator("option", has_text="hosts unavailable")
    expect(unavailable).to_have_count(1)
    assert unavailable.get_attribute("disabled") is not None
    assert host_select.input_value() == ""  # still "this machine" -- the marker was never selected

    # A save that doesn't touch the host at all must never send the
    # marker's value.
    page.locator("[data-field='effort']").select_option("high")
    calls = page.evaluate("() => window.__lastCalls")
    assert calls[-1]["patch"]["fields"]["host"] is None


def test_repeated_host_fetch_failures_cool_down_then_recover(page: Page, web_base_url):
    """R6: a permanently failing `/api/agents/hosts` must not be retried
    once per drawer open forever -- that costs one request per UI open
    against a dead endpoint indefinitely. After the SECOND consecutive
    failure, a short cooldown suppresses further fetches; once it elapses,
    the very next drawer open retries for real and renders the full list.
    (A single-blip recovery must still hold -- the cooldown only arms
    after a SECOND failure in a row, proven by the first two opens below
    each issuing a real request.)"""
    attempt = {"n": 0}
    healthy = {"on": False}

    def api_handler(route):
        if "/api/agents/hosts" in route.request.url:
            attempt["n"] += 1
            if healthy["on"]:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
            else:
                route.fulfill(status=503, content_type="application/json", body="{}")
        elif "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "cooldown", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    page.clock.install()

    def open_drawer(card_id):
        page.evaluate("() => { const c = document.getElementById('test-assignment-container'); if (c) c.remove(); }")
        _render(page, {"id": card_id, "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})

    # Failure #1 -- a real fetch happens.
    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        open_drawer("t30")
    assert attempt["n"] == 1

    # Failure #2 -- still a real fetch; this one arms the cooldown.
    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        open_drawer("t31")
    assert attempt["n"] == 2

    # A third drawer opened immediately, still inside the cooldown window,
    # must NOT issue a third request.
    open_drawer("t32")
    page.wait_for_timeout(100)
    assert attempt["n"] == 2
    host_select3 = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select3.locator("option")).to_have_count(2)  # this machine + unavailable marker

    # Advance past the cooldown -- the endpoint recovers too -- and the
    # next drawer open both retries AND renders the full list.
    healthy["on"] = True
    page.clock.fast_forward(11_000)
    with page.expect_response(lambda r: "/api/agents/hosts" in r.url):
        open_drawer("t33")
    assert attempt["n"] == 3
    host_select4 = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select4.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))


def test_stale_failed_fetch_does_not_clobber_a_newer_cache_entry(page: Page, web_base_url):
    """M6: a slow fetch that fails AFTER a newer, faster fetch already
    cached a success must not null out that newer cache entry -- only the
    fetch that's still the current `_hostsCache` entry may clear it on
    failure."""
    responses = []  # stashed hosts-fetch routes, fulfilled out of order below

    def api_handler(route):
        if "/api/agents/hosts" in route.request.url:
            responses.append(route)
        elif "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/tasks/" in route.request.url and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "clobber", **body}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    def wait_for_stashed(n):
        # Both stashed requests are deliberately left un-fulfilled by
        # api_handler above, so neither `expect_response` nor
        # `expect_request` (both wait for network EVENTS, and a route this
        # test never calls .fulfill() on inline never completes one) can
        # be used here -- poll the Python-side stash list instead.
        for _ in range(100):
            if len(responses) >= n:
                return
            page.wait_for_timeout(20)
        raise AssertionError(f"expected {n} stashed /api/agents/hosts request(s), got {len(responses)}")

    _load_module(page, web_base_url, api_handler=api_handler)
    page.clock.install()

    # Drawer #1 -- fetch #1 goes out and is stashed (deliberately left
    # unfulfilled until after fetch #2 resolves, below).
    _render(page, {"id": "t40", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {}})
    wait_for_stashed(1)

    # Past the client TTL, drawer #2 issues fetch #2 -- and it resolves
    # (successfully) BEFORE the still-pending fetch #1 does.
    page.clock.fast_forward(31_000)
    page.evaluate("() => { document.getElementById('test-assignment-container').remove(); }")
    _render(page, {"id": "t41", "title": "Deploy", "tags": ["codex"], "assignee": "codex", "fields": {}})
    wait_for_stashed(2)
    responses[1].fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))

    host_select2 = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select2.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))

    # NOW let the stale fetch #1 fail. Its `.catch` must not clobber the
    # cache entry fetch #2 just installed.
    responses[0].fulfill(status=503, content_type="application/json", body="{}")
    page.wait_for_timeout(100)

    # A third drawer, still well within fetch #2's cache TTL, must be a
    # cache hit -- no third request.
    page.evaluate("() => { document.getElementById('test-assignment-container').remove(); }")
    _render(page, {"id": "t42", "title": "Reindex", "tags": ["claude"], "assignee": "claude", "fields": {}})
    page.wait_for_timeout(100)
    assert len(responses) == 2  # still just the two -- no clobber-triggered refetch
    host_select3 = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select3.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))


# ---------------------------------------------------------------------------
# Covers: overlapping saves are serialized so a rejected save cannot revert
# an accepted one, or vice versa; a revert to a host whose option was
# removed re-seeds it, flagged, instead of silently clearing to
# "this machine".
# ---------------------------------------------------------------------------

def test_serialized_save_lone_change_still_saves_immediately_and_sticks(page: Page, web_base_url):
    """Over-correction guard for A5's serialization: a single, non-
    overlapping change must still save right away and stick -- chaining
    onto an already-resolved `saveChain` must not add a meaningful delay
    or drop the update."""
    _load_module(page, web_base_url)
    _render(page, {"id": "t60", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {"host": "laptop", "effort": "medium"}})

    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))

    host_select.select_option("studio-box")
    expect(host_select).to_have_value("studio-box")
    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 1
    assert calls[0]["patch"]["fields"]["host"] == "studio-box"


def test_serialized_saves_two_sequential_changes_both_land_in_order(page: Page, web_base_url):
    """Over-correction guard for A5: two changes made one after the
    OTHER has already settled must both land, each carrying the other's
    already-saved value forward -- serialization must not turn into
    losing or reordering non-overlapping saves."""
    _load_module(page, web_base_url)
    _render(page, {"id": "t61", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {"host": "laptop", "effort": "medium"}})

    host_select = page.locator("#test-assignment-container [data-field='host']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))

    host_select.select_option("studio-box")
    expect(host_select).to_have_value("studio-box")
    page.locator("[data-field='effort']").select_option("high")
    expect(page.locator("[data-field='effort']")).to_have_value("high")

    calls = page.evaluate("() => window.__lastCalls")
    assert len(calls) == 2
    first, second = calls[0]["patch"]["fields"], calls[1]["patch"]["fields"]
    assert (first["effort"], first["host"]) == ("medium", "studio-box")
    assert (second["effort"], second["host"]) == ("high", "studio-box")


def test_overlapping_saves_accepted_host_change_survives_a_later_rejected_effort_change(page: Page, web_base_url):
    """With two saves in flight -- an ACCEPTED host change that takes a
    while, and a REJECTED effort change sent shortly after -- the accepted
    host must not be silently reverted by the unrelated rejection, and the
    rejected effort must revert to what was actually last committed.
    Reconstructing `lastSaved*` from the live controls at whichever save
    resolves LAST would let the rejected save clobber a host the server
    had already committed, with no toast at any point -- serialization
    guards against exactly that."""
    _load_module(page, web_base_url)
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
                    return new Promise((resolve, reject) => {
                        // The host change (effort still 'high' at the
                        // moment IT runs) takes 300ms and is accepted; a
                        // save actually carrying the rejected effort
                        // value is rejected near-instantly (20ms).
                        const delay = patch.fields.effort === 'low' ? 20 : 300;
                        setTimeout(() => {
                            if (patch.fields.effort === 'low') {
                                reject(new Error('effort low is not permitted'));
                            } else {
                                resolve({ id, ...patch });
                            }
                        }, delay);
                    });
                },
                onError: (message) => { window.__errorCalls.push(message); },
            });
        }""",
        {"id": "t62", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {"host": "laptop", "effort": "high"}},
    )
    host_select = page.locator("#test-assignment-container [data-field='host']")
    effort_select = page.locator("#test-assignment-container [data-field='effort']")
    error_el = page.locator("#test-assignment-container [data-field='error']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))  # wait for the catalog

    # Two changes fired back to back, well inside the first save's 300ms
    # flight -- the second is queued behind the first (A5's serialization),
    # not fired concurrently.
    host_select.select_option("studio-box")
    effort_select.select_option("low")

    # Wait for BOTH saves to settle (the rejection's error surfacing is
    # the signal the second -- and thus also the first -- has resolved).
    expect(error_el).to_contain_text("effort low is not permitted")

    # The ACCEPTED host must still be selected -- not reverted by the
    # unrelated rejection.
    expect(host_select).to_have_value("studio-box")
    # The REJECTED effort must revert to what was actually last
    # committed ("high", from the accepted save), not silently stay at
    # the operator's rejected "low".
    expect(effort_select).to_have_value("high")

    # A later, unrelated save must carry the ACCEPTED host forward.
    effort_select.select_option("medium")
    expect(error_el).to_be_hidden()
    calls = page.evaluate("() => window.__lastCalls")
    last = calls[-1]["patch"]["fields"]
    assert last["host"] == "studio-box"  # not lost
    assert last["effort"] == "medium"


def test_overlapping_saves_rejected_effort_reverts_even_when_the_accepted_save_settles_first(page: Page, web_base_url):
    """Mirror image of the test above: the ACCEPTED host change settles
    quickly, the REJECTED effort change (sent shortly after) takes longer
    to come back. Capturing `lastSaved*` from the live controls AFTER each
    save's own `await` would let the accepted save, resolving first while
    the operator had already moved the effort control, record the
    operator's unconfirmed value as "last known good," making the
    later rejection's revert a no-op. The rejected value must still
    revert here, and must not ride along on the next save."""
    _load_module(page, web_base_url)
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
                    return new Promise((resolve, reject) => {
                        const delay = patch.fields.effort === 'low' ? 700 : 100;
                        setTimeout(() => {
                            if (patch.fields.effort === 'low') {
                                reject(new Error('effort low is not permitted'));
                            } else {
                                resolve({ id, ...patch });
                            }
                        }, delay);
                    });
                },
                onError: (message) => { window.__errorCalls.push(message); },
            });
        }""",
        {"id": "t63", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {"host": "laptop", "effort": "high"}},
    )
    host_select = page.locator("#test-assignment-container [data-field='host']")
    effort_select = page.locator("#test-assignment-container [data-field='effort']")
    error_el = page.locator("#test-assignment-container [data-field='error']")
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))

    host_select.select_option("studio-box")
    effort_select.select_option("low")

    expect(error_el).to_contain_text("effort low is not permitted")
    expect(effort_select).to_have_value("high")  # reverted, not left at the rejected "low"
    expect(host_select).to_have_value("studio-box")  # the accepted change unaffected

    # The next save must not carry the rejected effort forward.
    host_select.select_option("laptop")
    expect(error_el).to_be_hidden()
    calls = page.evaluate("() => window.__lastCalls")
    last = calls[-1]["patch"]["fields"]
    assert last["effort"] == "high"  # NOT "low"
    assert last["host"] == "laptop"


def test_unknown_saved_host_survives_a_rejected_save_racing_the_hosts_fetch(page: Page, web_base_url):
    """Three realistic preconditions stacked:
    the card's saved host has dropped out of the registry (unknown,
    flagged); the operator clears the host to "this machine" WHILE `GET
    /api/agents/hosts` is still in flight (the live selection intentionally
    supports changing mid-fetch); and that catalog
    landing's rebuild (still keyed off the live selection, now "") drops
    the flagged-unknown option entirely, right before the save is
    REJECTED. The naive `hostEl.value = lastSavedHost` then finds no
    matching option, and the DOM silently sets the value to `""` --
    itself a real, different, meaningful value ("this machine") -- rather
    than restoring the original unknown host."""
    pending = []

    def api_handler(route):
        if "/api/agents/models" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
        elif "/api/agents/hosts" in route.request.url:
            pending.append(route)  # stashed -- fulfilled mid-test, below
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    _load_module(page, web_base_url, api_handler=api_handler)
    page.evaluate(
        """(card) => {
            const container = document.createElement('div');
            container.id = 'test-assignment-container';
            document.body.appendChild(container);
            window.__lastCalls = [];
            window.__errorCalls = [];
            window.__pendingSaves = [];
            window.__renderAssignmentPickers(container, card, {
                putTask: (id, patch) => {
                    window.__lastCalls.push({ id, patch });
                    return new Promise((resolve, reject) => {
                        window.__pendingSaves.push({ resolve, reject });
                    });
                },
                onError: (message) => { window.__errorCalls.push(message); },
            });
        }""",
        {"id": "t64", "title": "Fix the printer", "tags": ["claude"], "assignee": "claude", "fields": {"host": "ghost-box", "effort": "medium"}},
    )

    host_select = page.locator("#test-assignment-container [data-field='host']")
    error_el = page.locator("#test-assignment-container [data-field='error']")
    # Seeded synchronously, before the (still-stashed) hosts fetch: "this
    # machine" + the flagged-unknown ghost-box.
    expect(host_select.locator("option")).to_have_count(2)
    assert host_select.input_value() == "ghost-box"

    # Clear the host to "this machine" while the fetch is still in flight
    # -- this save is left pending (its putTask promise is stashed, not
    # settled) until explicitly resolved below.
    host_select.select_option("")
    for _ in range(50):
        if page.evaluate("() => window.__pendingSaves.length") >= 1:
            break
        page.wait_for_timeout(20)
    assert page.evaluate("() => window.__pendingSaves.length") == 1

    # NOW let the hosts catalog land, while the save above is STILL
    # pending. A1's rebuild reads the live selection ("") -- known
    # ("this machine" always is) -- and drops the flagged-unknown
    # ghost-box option entirely, since nothing needs it anymore.
    for route in pending:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_HOST_CATALOG))
    pending.clear()
    expect(host_select.locator("option")).to_have_count(1 + len(_HOST_CATALOG["hosts"]))
    expect(host_select.locator("option[data-unknown='true']")).to_have_count(0)

    # THEN the save is rejected -- the option it needs to revert to no
    # longer exists in the DOM.
    page.evaluate("() => window.__pendingSaves[0].reject(new Error('cannot clear host'))")
    expect(error_el).to_contain_text("cannot clear host")

    # R9: restored, flagged, and re-selected -- not silently cleared.
    expect(host_select.locator("option[data-unknown='true']")).to_have_count(1)
    assert host_select.input_value() == "ghost-box"  # NOT "" ("this machine")

    # A later, unrelated save must still send the ORIGINAL host, not None.
    page.locator("[data-field='effort']").select_option("high")
    calls = page.evaluate("() => window.__lastCalls")
    assert calls[-1]["patch"]["fields"]["host"] == "ghost-box"  # NOT None
