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

# The client polls every 4s; wait a touch past one full interval to be sure a
# second poll fired (so the dedup guard is actually exercised).
POLL_WAIT_FOR_DEDUP_MS = 4500
# A touch past one 4s poll interval — long enough that a still-running loop
# would have fired at least once more in the window.
POLL_INTERVAL_MS_PLUS = 4500


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
            # Count detail polls so a test can assert the loop stopped (the count
            # plateaus once the client stops polling).
            state["detail_get_count"] = state.get("detail_get_count", 0) + 1
            detail = {
                "id": CONV_ID,
                "title": "Knee pain follow-up",
                "created_at": "2026-06-25T10:00:00",
                "updated_at": "2026-06-25T10:01:00",
                # `state["messages"]` is mutable so a test can simulate a
                # late-arriving message (the worker mirroring the spawned
                # session's result, #311) landing in a later poll.
                "messages": list(state["messages"]),
                "pending_question": None,
                # #311: whether the spawned session is still running. The client
                # stops polling once this is False AND no question is pending.
                # Defaults to active so a test that never sets it keeps the
                # historical "poll runs" behavior.
                "agent_session_active": state.get("agent_session_active", True),
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
            "messages": [
                {"id": "m1", "role": "user", "content": "my knee hurts after runs",
                 "created_at": "2026-06-25T10:00:00", "sources": None, "routing": None},
                {"id": "m2", "role": "assistant",
                 "content": "\U0001fa7a On it — running as a Claude Code session.",
                 "created_at": "2026-06-25T10:00:05", "sources": None, "routing": None},
            ],
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
        # ...and the answer is echoed into the thread as a user message so it
        # doesn't vanish until the conversation is reopened.
        expect(page.locator("#messages")).to_contain_text("yes, draft the PT plan")

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

    def test_late_mirrored_message_renders_once(self, page: Page):
        """#311: a message that appears in a LATER poll (the worker mirroring the
        spawned session's result into the thread) is rendered into #messages and
        is NOT duplicated on subsequent polls.

        Opening with an outstanding question starts the same 4s poll the result
        rides on. The first poll seeds the seen-set from m1+m2 (already on
        screen); a later m3 (the mirrored result) is the only id the dedup hasn't
        seen, so it renders exactly once."""
        self._open_conversation(page)
        expect(page.locator("#pendingQuestionCard")).to_be_visible(timeout=8000)
        # The seeded messages are on screen, but the late result is not yet.
        expect(page.locator("#messages")).not_to_contain_text("Opened PR #5")

        # The worker mirrors the result: a new assistant message lands in the GET
        # (and the question resolves, as it would when the session finishes).
        self.state["messages"].append({
            "id": "m3", "role": "assistant",
            "content": "All done — Opened PR #5.",
            "created_at": "2026-06-25T10:05:00", "sources": None, "routing": None,
        })
        self.state["awaiting"] = False

        # The next poll renders it into the thread exactly once.
        result = page.locator("#messages .message.assistant",
                              has_text="Opened PR #5")
        expect(result).to_have_count(1, timeout=8000)
        # Subsequent polls must NOT duplicate it (dedup by message id).
        page.wait_for_timeout(POLL_WAIT_FOR_DEDUP_MS)
        expect(result).to_have_count(1)

    def test_poll_stops_when_session_terminal_and_no_question(self, page: Page):
        """#311: once the spawned session reaches a terminal status AND no
        question is pending, the client stops the 4s poll instead of running
        forever. The stop needs TWO consecutive terminal polls (race guard), so
        this allows a couple of cycles before asserting the detail-GET count
        plateaus."""
        self._open_conversation(page)
        # A question is pending, so the poll is running and the card is shown.
        expect(page.locator("#pendingQuestionCard")).to_be_visible(timeout=8000)

        # The session finishes: the question resolves and the server now reports
        # the linked session as terminal (not active).
        self.state["awaiting"] = False
        self.state["agent_session_active"] = False

        # The card clears on the first terminal poll; the loop stops on the
        # second. Wait past two full intervals so the stop has definitely fired,
        # then snapshot the GET count and confirm it no longer grows.
        expect(page.locator("#pendingQuestionCard")).to_have_count(0, timeout=8000)
        page.wait_for_timeout(POLL_INTERVAL_MS_PLUS * 2)
        settled = self.state.get("detail_get_count", 0)
        # Across two more full intervals the count must not increase — the loop
        # is stopped, not merely idle for one tick.
        page.wait_for_timeout(POLL_INTERVAL_MS_PLUS * 2)
        assert self.state.get("detail_get_count", 0) == settled, (
            "detail GET fired after the session went terminal — poll did not stop"
        )

    def test_terminal_poll_before_result_is_stored_still_renders_result(self, page: Page):
        """#311 (race guard): the executor flips the session row terminal BEFORE
        the dispatch handler writes the result mirror, so a poll can see
        agent_session_active=false with the result not yet in the GET. A single
        terminal poll must NOT stop — the next poll, once the mirrored result has
        landed, must render it and only THEN stop. (For the codex handoff path
        this mirror is the only web output, so a premature stop = lost result.)"""
        self._open_conversation(page)
        expect(page.locator("#pendingQuestionCard")).to_be_visible(timeout=8000)

        # Simulate the race window: the session is reported terminal (status
        # already flipped) and the question is resolved, but the result mirror
        # has NOT been written to the conversation yet.
        self.state["awaiting"] = False
        self.state["agent_session_active"] = False

        # The card clears on the first terminal poll, but the result isn't here.
        expect(page.locator("#pendingQuestionCard")).to_have_count(0, timeout=8000)
        expect(page.locator("#messages")).not_to_contain_text("Opened PR #5")
        # Record the GET count: if the single terminal poll wrongly stopped the
        # loop, the count will not advance and the result below never renders.
        count_after_first_terminal = self.state.get("detail_get_count", 0)

        # The mirror lands (the dispatch handler finally wrote the result).
        self.state["messages"].append({
            "id": "m3", "role": "assistant",
            "content": "All done — Opened PR #5.",
            "created_at": "2026-06-25T10:05:00", "sources": None, "routing": None,
        })

        # The poll did NOT stop after one terminal observation, so a subsequent
        # poll fires, renders the late result exactly once, and only then stops.
        result = page.locator("#messages .message.assistant", has_text="Opened PR #5")
        expect(result).to_have_count(1, timeout=8000)
        assert self.state.get("detail_get_count", 0) > count_after_first_terminal, (
            "no further poll fired after the first terminal poll — the loop "
            "stopped before the mirrored result was stored (the race bug)"
        )

        # And now (two consecutive terminal polls observed) the loop stops and
        # does not duplicate the result.
        page.wait_for_timeout(POLL_INTERVAL_MS_PLUS * 2)
        settled = self.state.get("detail_get_count", 0)
        page.wait_for_timeout(POLL_INTERVAL_MS_PLUS * 2)
        assert self.state.get("detail_get_count", 0) == settled, (
            "poll did not stop after the result was rendered + two terminal polls"
        )
        expect(result).to_have_count(1)
