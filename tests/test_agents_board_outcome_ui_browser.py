"""Browser test for the Review-lane card outcome: the compact
pull-request badge on the card face and the drawer's read-only "Agent
outcome" section.

Serves `web/` itself from an ephemeral port and stubs `GET
/api/agents/board` with a static fixture — the assertions are about the JS
in `web/agents/board.js` and `web/agents.html`, not a live backend. No
`requires_server` marker, so this runs at pre-push (`browser and not
requires_server`).

Covers: the card-face PR badge (number + open/merged/closed) only on a
Review-lane card that has a PR; the drawer's outcome section rendering the
engine label, summary, branch, and a clickable PR link with a merge-status
badge; a PR the background refresher hasn't reached yet showing no
fabricated number/state; no outcome section (and no badge) for a card with
no recorded outcome; and that agent-provided summary/PR text is rendered as
inert text, never as HTML.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_ALLOWED_POLICY = {
    "claimed": False, "agent_owned": True,
    "cancel": {"allowed": True, "reason": None},
    "assignee": {"allowed": True, "reason": None},
    "fields": {"allowed": True, "reason": None},
    "lanes": {},
}

# Deliberately malicious-looking synthetic text — an agent's own report is
# untrusted, so the board must render it as text, never parse it as markup.
_HOSTILE_SUMMARY = "Fixed <script>window.__xss=1</script> the bug & shipped it."


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


def _task_card(card_id: str, title: str, *, outcome=None) -> dict:
    return {
        "kind": "task", "id": card_id, "title": title,
        "notes": "", "status": "done", "tags": ["agent-completed", "claude"],
        "assignee": "claude", "fields": {}, "context": "Work",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "session": None, "pending_question": None,
        "policy": _ALLOWED_POLICY,
        "outcome": outcome,
    }


def _board_fixture():
    merged_pr_card = _task_card("t-merged", "Ship the merged fix", outcome={
        "session_id": "sess-1", "engine_label": "Claude Code",
        "summary": _HOSTILE_SUMMARY,
        "branch": "feat/fix-it",
        "created_at": "2026-09-18T04:05:00+00:00",
        "prs": [{
            "url": "https://github.com/nbramia/LifeOS/pull/1234",
            "number": 1234, "state": "MERGED",
            "merged_at": "2026-09-18T05:00:00Z", "stale": False,
        }],
    })
    unrefreshed_pr_card = _task_card("t-unrefreshed", "Ship the pending fix", outcome={
        "session_id": "sess-2", "engine_label": "Codex",
        "summary": "Opened a PR; status not refreshed yet.",
        "branch": "feat/pending",
        "created_at": "2026-09-18T04:05:00+00:00",
        "prs": [{
            "url": "https://github.com/nbramia/LifeOS/pull/5678",
            "number": None, "state": None, "merged_at": None, "stale": True,
        }],
    })
    no_outcome_card = _task_card("t-no-outcome", "Still nothing on this card", outcome=None)
    return {
        "lanes": {
            "unassigned": [], "assigned": [], "in_progress": [], "human_queue": [],
            "scheduled": [],
            "review": [merged_pr_card, unrefreshed_pr_card, no_outcome_card],
            "done": [], "snoozed": [],
        },
        "generated_at": 0,
        "api_host": "primary-host",
    }


def _stub_routes(page: Page, board_state: dict):
    def d3_handler(route):
        route.fulfill(status=200, content_type="application/javascript", body="window.d3 = window.d3 || {};")
    page.route("**/d3.v7.min.js", d3_handler)

    def api_handler(route):
        url = route.request.url
        method = route.request.method
        if "/api/agents/board/stream" in url:
            route.fulfill(status=200, content_type="text/event-stream", body="retry: 60000\n: ok\n\n")
            return
        if url.rstrip("/").endswith("/api/agents/board") and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(board_state))
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", api_handler)


def _open_board(page: Page, base_url, board_state=None):
    _stub_routes(page, board_state if board_state is not None else _board_fixture())
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('[data-card-id="t-merged"]')


class TestCardFacePrBadge:
    def test_review_card_with_a_merged_pr_shows_a_compact_badge(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        badge = page.locator('[data-card-id="t-merged"] .board-pr-badge')
        expect(badge).to_have_text("#1234 · merged")
        expect(badge).to_have_class("board-pr-badge board-pr-badge-merged")

    def test_unrefreshed_pr_shows_no_fabricated_number_or_state(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        badge = page.locator('[data-card-id="t-unrefreshed"] .board-pr-badge')
        expect(badge).to_have_text("PR · checking…")
        expect(badge).to_have_class("board-pr-badge board-pr-badge-unknown")

    def test_card_with_no_outcome_shows_no_pr_badge(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        badge = page.locator('[data-card-id="t-no-outcome"] .board-pr-badge')
        expect(badge).to_have_count(0)


class TestDrawerOutcomeSection:
    def test_drawer_shows_engine_summary_branch_and_pr_link(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t-merged"] .board-card-title').click()
        outcome = page.locator('[data-field="outcome"]')
        expect(outcome).to_be_visible()
        expect(outcome.locator(".drawer-outcome-engine")).to_have_text("Claude Code")
        # The hostile summary renders as inert text: the literal tag
        # characters are visible in the rendered text, not parsed as an
        # actual <script> element, and nothing it contained ever executed.
        expect(outcome.locator(".drawer-outcome-summary")).to_have_text(_HOSTILE_SUMMARY)
        assert page.locator(".drawer-outcome-summary script").count() == 0
        assert page.evaluate("window.__xss") is None
        expect(outcome.locator(".drawer-outcome-branch code")).to_have_text("feat/fix-it")
        pr_link = outcome.locator(".drawer-outcome-pr a")
        expect(pr_link).to_have_attribute("href", "https://github.com/nbramia/LifeOS/pull/1234")
        expect(pr_link).to_have_text("#1234")
        expect(outcome.locator(".drawer-outcome-pr .board-pr-badge")).to_have_text("merged")

    def test_drawer_outcome_section_is_separate_from_the_editable_notes_box(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t-merged"] .board-card-title').click()
        outcome = page.locator('[data-field="outcome"]')
        notes = page.locator('[data-field="notes"]')
        expect(outcome).to_be_visible()
        expect(notes).to_be_visible()
        # The outcome markdown-ish content (summary/branch/PR link) never
        # leaks into the editable notes textarea's value.
        assert "feat/fix-it" not in (notes.input_value() or "")
        assert "Agent outcome" not in (notes.input_value() or "")

    def test_drawer_has_no_outcome_section_for_a_card_that_never_completed(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t-no-outcome"] .board-card-title').click()
        page.wait_for_selector('[data-field="notes"]')
        expect(page.locator('[data-field="outcome"]')).to_have_count(0)
