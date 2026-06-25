"""Browser test for the web /chat answer affordance (#412).

Drives the orchestrating-persona answer UI — the inline card that renders when a
conversation's spawned session is awaiting a `[CLARIFY]`/`[GOAL]` — with the
`/api/conversations/*` endpoints mocked via Playwright route interception. The
spawn → pending_question → render → answer → POST round-trip is exercised
deterministically, without a real doctor session or worker.

Verifies the FRONTEND: that a conversation whose `GET` returns a
`pending_question` renders the answer card, that Send POSTs to `/answer`, and
that a successful answer clears the affordance. The server-side deposit/resume
(#403) is covered separately; this never spawns a real session. Requires the
server serving the chat page (defaults to localhost:8000; override the base via
LIFEOS_TEST_BASE_URL, e.g. a worktree run on a spare port), like the rest of the
browser suite.
"""
import json
import os
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
# The production server owns :8000; a worktree run can point at its own instance
# (e.g. a localhost-only server on a spare port serving the worktree's web/) via
# this env var. Defaults to :8000 to match the rest of the browser suite. The
# chat SPA is served at /chat (the canonical route; `/` may be the landing page).
BASE_URL = os.environ.get("LIFEOS_TEST_BASE_URL", "http://localhost:8000")
CHAT_URL = BASE_URL.rstrip("/") + "/chat"

CONV_ID = "conv_doctor_1"
SESSION_ID = "sess_doctor_1"


def _install_conversation_mocks(page: Page, state: dict):
    """Single dispatcher for /api/conversations/* calls, driven by `state`.

    - GET list → one conversation (the sidebar load).
    - GET /{id} → messages + a `pending_question` while `state["awaiting"]`.
    - POST /{id}/answer → records the body, clears the pending question, 200.
    """

    def handler(route):
        req = route.request
        path = req.url.split("?")[0]

        # POST /{id}/answer
        if req.method == "POST" and path.endswith("/answer"):
            body = json.loads(req.post_data or "{}")
            answer = (body.get("answer") or "").strip()
            if not answer:
                return route.fulfill(
                    status=400, content_type="application/json",
                    body=json.dumps({"detail": "answer cannot be empty"}))
            state["answers"].append(body)
            state["awaiting"] = False  # question resolved → next poll drops the card
            return route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"ok": True, "session_id": SESSION_ID,
                                 "status": "answer_deposited"}))

        # GET /{id} detail (a segment after conversations/)
        m = re.search(r"/conversations/([^/]+)$", path)
        if m and m.group(1) != "conversations":
            detail = {
                "id": CONV_ID,
                "title": "Knee pain follow-up",
                "created_at": "2026-06-25T10:00:00",
                "updated_at": "2026-06-25T10:01:00",
                "messages": [
                    {"id": "m1", "role": "user", "content": "my knee hurts after runs",
                     "created_at": "2026-06-25T10:00:00", "sources": None, "routing": None},
                    {"id": "m2", "role": "assistant",
                     "content": "\U0001fa7a On it — running as a Claude Code session.",
                     "created_at": "2026-06-25T10:00:05", "sources": None, "routing": None},
                ],
                "pending_question": None,
            }
            if state["awaiting"]:
                detail["pending_question"] = {
                    "session_id": SESSION_ID,
                    "question": state["question"],
                    "kind": state["kind"],
                }
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(detail))

        # GET list
        return route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"conversations": [{
                "id": CONV_ID, "title": "Knee pain follow-up",
                "created_at": "2026-06-25T10:00:00", "updated_at": "2026-06-25T10:01:00",
                "message_count": 2, "persona_id": "primary",
            }]}))

    page.route("**/api/conversations**", handler)


class TestPendingQuestionUI:
    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        page.set_viewport_size(DESKTOP_VIEWPORT)
        self.state = {
            "awaiting": True,
            "question": "Is the goal to draft a PT plan, or just log the symptom?",
            "kind": "goal_approval",
            "answers": [],
        }
        _install_conversation_mocks(page, self.state)
        page.goto(CHAT_URL)
        page.wait_for_selector("#messages")

    def _open_conversation(self, page: Page):
        # loadConversation() loads the detail (with pending_question) and starts
        # polling — the same entry the sidebar click uses.
        page.evaluate(f"window.loadConversation('{CONV_ID}')")

    def test_pending_question_renders_answer_card(self, page: Page):
        self._open_conversation(page)
        card = page.locator("#pendingQuestionCard")
        expect(card).to_be_visible(timeout=8000)
        # goal_approval framing + the question text.
        expect(card.locator(".pq-heading")).to_contain_text("Approve the goal")
        expect(card.locator(".pq-question")).to_contain_text("draft a PT plan")
        expect(card.locator("#pqInput")).to_be_visible()
        expect(card.locator("#pqSend")).to_be_visible()

    def test_send_posts_answer_and_clears_card(self, page: Page):
        self._open_conversation(page)
        expect(page.locator("#pendingQuestionCard")).to_be_visible(timeout=8000)
        page.locator("#pqInput").fill("yes, draft the PT plan")
        page.locator("#pqSend").click()
        # The POST fired with the typed answer...
        expect(page.locator("#pendingQuestionCard")).to_have_count(0, timeout=8000)
        assert self.state["answers"], "answer POST should have fired"
        assert self.state["answers"][0]["answer"] == "yes, draft the PT plan"

    def test_empty_answer_does_not_post(self, page: Page):
        self._open_conversation(page)
        card = page.locator("#pendingQuestionCard")
        expect(card).to_be_visible(timeout=8000)
        # Clicking Send with an empty input nudges instead of POSTing.
        page.locator("#pqSend").click()
        page.wait_for_timeout(300)
        assert not self.state["answers"], "empty answer must not POST"
        expect(card).to_be_visible()  # card stays so the user can answer
        expect(card.locator("#pqStatus")).to_contain_text("Enter an answer")

    def test_followup_kind_uses_plain_framing(self, page: Page):
        # A clarification/followup question is framed as a plain answer, not a
        # goal approval.
        self.state["kind"] = "followup"
        self.state["question"] = "Which knee — left or right?"
        self._open_conversation(page)
        card = page.locator("#pendingQuestionCard")
        expect(card).to_be_visible(timeout=8000)
        expect(card.locator(".pq-heading")).to_contain_text("needs your input")
        expect(card.locator(".pq-question")).to_contain_text("left or right")

    def test_no_pending_question_renders_no_card(self, page: Page):
        # A conversation with no open question shows no affordance.
        self.state["awaiting"] = False
        self._open_conversation(page)
        page.wait_for_timeout(500)
        expect(page.locator("#pendingQuestionCard")).to_have_count(0)
