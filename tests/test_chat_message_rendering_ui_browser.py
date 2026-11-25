"""Browser tests for how the chat renders message text.

Chat shows message content as text: markup in a message is displayed, never
interpreted, links are limited to web and mail addresses, and markdown images
render as links so message content never loads a remote resource.

Serves `web/` from an ephemeral port with every `/api/` call stubbed, so it
needs no running server (same pattern as test_mode_pill_ui_browser.py).
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
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/chat", "/"):
            return str(WEB_DIR / "index.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):
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


@pytest.fixture
def chat(page: Page, chat_base_url):
    attacker_requests = []
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({})))
    page.route("https://attacker.example/**", lambda route: (
        attacker_requests.append(route.request.url), route.abort()))
    page.goto(f"{chat_base_url}/chat")
    page.wait_for_selector("#messages")
    page.evaluate("window.__thread = import('/static/chat/thread.js')")
    page.wait_for_function("window.__thread !== undefined")
    page.attacker_requests = attacker_requests
    page.dialogs = dialogs
    return page


def _render(page: Page, text, role="assistant", sources=None):
    """Render through the real addMessage path; return the content element's
    inner HTML plus the live-DOM facts the assertions need."""
    return page.evaluate(
        """async ([text, role, sources]) => {
            const t = await window.__thread;
            const msg = t.addMessage(text, role, sources);
            const c = msg.querySelector('.message-content');
            const m = msg.querySelector('.message-meta');
            return {
              html: c.innerHTML,
              text: c.textContent,
              imgs: msg.querySelectorAll('img, script, iframe, svg, object, embed').length,
              handlers: [...msg.querySelectorAll('*')].filter(
                e => [...e.attributes].some(a => a.name.startsWith('on')
                  && !(m && m.contains(e)))).length,
              links: [...msg.querySelectorAll('.message-content a')].map(
                a => ({href: a.getAttribute('href'), target: a.target, rel: a.rel,
                       text: a.textContent})),
              metaLinks: [...msg.querySelectorAll('.message-meta a')].map(
                a => a.getAttribute('href')),
              metaImgs: m ? m.querySelectorAll('img, script').length : 0,
            };
        }""",
        [text, role, sources or []],
    )


def _settle(page: Page):
    page.wait_for_timeout(300)


class TestMarkupIsShownAsText:
    def test_img_onerror_is_inert(self, chat):
        r = _render(chat, "Subject: Invoice <img src=x onerror=alert(1)> ok")
        _settle(chat)
        assert r["imgs"] == 0
        assert r["handlers"] == 0
        assert "<img src=x onerror=alert(1)>" in r["text"]
        assert chat.dialogs == []

    def test_script_tag_is_inert(self, chat):
        r = _render(chat, "<script>alert(1)</script>")
        _settle(chat)
        assert r["imgs"] == 0
        assert "<script>alert(1)</script>" in r["text"]
        assert chat.dialogs == []

    def test_markup_inside_code_and_bold_is_inert(self, chat):
        r = _render(chat, "**<img src=x onerror=alert(1)>** `<svg onload=alert(1)>`")
        _settle(chat)
        assert r["imgs"] == 0
        assert r["handlers"] == 0
        assert chat.dialogs == []

    def test_attribute_breakout_in_link_is_inert(self, chat):
        r = _render(chat, '[a](https://ok.example/" onmouseover="alert(1))')
        _settle(chat)
        assert r["handlers"] == 0

    def test_streamed_update_is_escaped(self, chat):
        chat.evaluate("""async () => {
            const t = await window.__thread;
            t.addMessage('', 'assistant', [], 'live1');
            t.updateMessage('live1', 'x <img src=x onerror=alert(1)>');
        }""")
        _settle(chat)
        assert chat.locator("#live1 img").count() == 0
        assert chat.dialogs == []


class TestLinks:
    @pytest.mark.parametrize("url", [
        "javascript:alert(1)",
        "  JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
    ])
    def test_unsafe_schemes_are_not_links(self, chat, url):
        r = _render(chat, f"[click]({url.replace(' ', '')})")
        assert r["links"] == []
        assert "click" in r["text"]

    def test_unsafe_scheme_with_leading_space_is_not_a_link(self, chat):
        r = _render(chat, "[click]( javascript:alert(1))")
        assert all(
            not (link["href"] or "").lower().lstrip().startswith("javascript")
            for link in r["links"])
        assert chat.dialogs == []

    def test_allowed_schemes_render_safe_links(self, chat):
        r = _render(chat, "[web](https://example.com/a?b=1&c=2) "
                          "[mail](mailto:a@example.com) [plain](http://example.com)")
        assert [link["href"] for link in r["links"]] == [
            "https://example.com/a?b=1&c=2", "mailto:a@example.com",
            "http://example.com"]
        for link in r["links"]:
            assert link["target"] == "_blank"
            assert "noopener" in link["rel"] and "noreferrer" in link["rel"]

    def test_markdown_image_becomes_link_and_loads_nothing(self, chat):
        r = _render(chat, "![x](https://attacker.example/?d=1)")
        _settle(chat)
        assert r["imgs"] == 0
        assert [link["href"] for link in r["links"]] == ["https://attacker.example/?d=1"]
        assert chat.attacker_requests == []

    def test_unsafe_source_urls_are_not_linked(self, chat):
        r = _render(chat, "answer", sources=[
            {"file_name": "<img src=x onerror=alert(1)>", "source_type": "calendar",
             "url": "javascript:alert(1)"},
            {"file_name": "<b>note</b>.md", "source_type": "vault"},
        ])
        _settle(chat)
        assert r["metaImgs"] == 0
        assert all(not (h or "").lower().startswith("javascript") for h in r["metaLinks"])
        assert chat.dialogs == []


class TestOrdinaryFormatting:
    def test_bold_italic_code_and_newlines(self, chat):
        r = _render(chat, "**bold** and *em* and `code`\nsecond & <line>")
        assert "<strong>bold</strong>" in r["html"]
        assert "<em>em</em>" in r["html"]
        assert "<code>code</code>" in r["html"]
        assert "<br>" in r["html"]
        assert "second & <line>" in r["text"]

    def test_link_label_keeps_formatting_text(self, chat):
        r = _render(chat, "see [the docs](https://example.com/x*y*z) now")
        assert r["links"][0]["href"] == "https://example.com/x*y*z"
        assert r["links"][0]["text"] == "the docs"
