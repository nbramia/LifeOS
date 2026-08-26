"""Browser test for the conversation sidebar's persona subtitle suffix.

Drives `web/chat/conversations.js`'s `renderConversations()` — a conversation
row's date subtitle (e.g. "Aug 25") gains a " · (Persona Label)" suffix when
the conversation's `persona_id` isn't the default ("primary"), resolved from
`config.personas` (loaded from `/api/personas` at boot, the same source the
persona picker itself renders from — see `web/chat/persona.js`), never a
hardcoded id→label map.

Self-contained like tests/test_mode_pill_ui_browser.py: serves `web/` itself
on an ephemeral port and stubs every `/api/` call, so it carries no
`requires_server` marker and runs at pre-push (`browser and not
requires_server`) — the only gate that catches a `web/` JS regression before
it reaches main.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _ChatHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the chat SPA the way api/main.py does: `/chat` is index.html and
    the module tree hangs off `/static/`."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/chat", "/"):
            return str(WEB_DIR / "index.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def chat_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _conversations_payload():
    return {
        "conversations": [
            {
                "id": "conv-primary",
                "title": "Weekend plans",
                "created_at": "2026-08-25T10:00:00",
                "updated_at": "2026-08-25T10:05:00",
                "message_count": 4,
                "persona_id": "primary",
                "backend": "lifeos",
            },
            {
                "id": "conv-therapist",
                "title": "Check-in",
                "created_at": "2026-08-25T09:00:00",
                "updated_at": "2026-08-25T09:05:00",
                "message_count": 4,
                "persona_id": "therapist",
                "backend": "lifeos",
            },
        ]
    }


def _open_chat(page: Page, base_url, *, personas_payload):
    def handle_api(route):
        req = route.request
        path = req.url.split("?", 1)[0]
        if path.endswith("/api/personas"):
            return route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(personas_payload))
        if path.endswith("/api/conversations"):
            return route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(_conversations_payload()))
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({}))

    page.route("**/api/**", handle_api)
    page.goto(f"{base_url}/chat")
    page.wait_for_selector("#conversationsList .conversation-item")


def _date_text_for(page: Page, title_substring: str) -> str:
    item = page.locator(".conversation-item", has_text=title_substring)
    return item.locator(".conversation-date").inner_text()


class TestConversationPersonaSubtitle:
    PERSONAS = {
        "personas": [
            {"id": "primary", "label": "Primary", "capabilities": [], "orchestrates": False},
            {"id": "therapist", "label": "Therapist", "capabilities": [], "orchestrates": False},
        ]
    }

    def test_primary_persona_conversation_has_no_suffix(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, personas_payload=self.PERSONAS)
        date_text = _date_text_for(page, "Weekend plans")
        assert "·" not in date_text
        assert "(" not in date_text

    def test_non_primary_persona_conversation_shows_persona_suffix(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, personas_payload=self.PERSONAS)
        date_text = _date_text_for(page, "Check-in")
        assert date_text.endswith("· (Therapist)")
        # The date portion itself is preserved (appended to, not replaced) --
        # whatever formatDate() produced for this fixed-in-the-past
        # timestamp, relative to the real "now" the test runs at.
        date_part = date_text[: -len("· (Therapist)")].strip()
        assert date_part  # non-empty: formatDate()'s own output is untouched

    def test_persona_label_resolved_from_loaded_personas_not_hardcoded(self, page: Page, chat_base_url):
        """The suffix text comes from whatever label `/api/personas` served,
        not a hardcoded id→label map in the JS — renaming the persona's label
        upstream changes the rendered suffix with no client-side changes."""
        renamed = {
            "personas": [
                {"id": "primary", "label": "Primary", "capabilities": [], "orchestrates": False},
                {"id": "therapist", "label": "Wellness Coach", "capabilities": [], "orchestrates": False},
            ]
        }
        _open_chat(page, chat_base_url, personas_payload=renamed)
        date_text = _date_text_for(page, "Check-in")
        assert date_text.endswith("· (Wellness Coach)")

    def test_unknown_persona_id_falls_back_to_raw_id(self, page: Page, chat_base_url):
        """A persona_id absent from the loaded personas list (e.g. a persona
        removed from config after the conversation was created) still renders
        something useful instead of silently dropping the suffix."""
        empty_personas = {"personas": [{"id": "primary", "label": "Primary", "capabilities": [], "orchestrates": False}]}
        _open_chat(page, chat_base_url, personas_payload=empty_personas)
        date_text = _date_text_for(page, "Check-in")
        assert date_text.endswith("· (therapist)")
