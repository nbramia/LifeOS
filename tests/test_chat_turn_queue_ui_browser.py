"""Browser tests for asynchronous chat turns in `/chat` (web/chat/ask-stream.js,
web/chat/turn-recovery.js).

Serves `web/` from an ephemeral port together with a small in-process fake of
the chat API: `POST /api/ask/stream` runs each turn on its own thread
(independent of the HTTP connection, as the real server's turn registry
does), honours `if_busy: "queue"` with a per-conversation FIFO, and
`GET /api/conversations/{id}` reports `active_turn`/`queued_turns`. That lets
these tests drive real streaming, real disconnects (a page reload), and real
polling, with no running API. No `requires_server` marker, for the same
reason as tests/test_backend_selector_ui_browser.py.
"""
import http.server
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class FakeChatApi:
    """Server-owned turns with a per-conversation queue, in memory."""

    def __init__(self):
        self.lock = threading.Condition()
        self.conversations: dict[str, dict] = {}
        self.running: dict[str, str] = {}       # conversation id -> turn id
        self.queues: dict[str, list[str]] = {}  # conversation id -> waiting turn ids
        self.requests: list[dict] = []
        self.turn_seconds = 1.5
        self.conversation_id_delay = 0.0
        # Cut the HTTP stream after this many frames (the turn keeps running).
        self.drop_after_frames = None

    def reset(self):
        with self.lock:
            self.conversations.clear()
            self.running.clear()
            self.queues.clear()
            self.requests.clear()
            self.turn_seconds = 1.5
            self.conversation_id_delay = 0.0
            self.drop_after_frames = None

    def start_turn(self, body: dict):
        """Returns (frames, done_event): a list the turn thread appends SSE
        frames to, plus an event set when the turn has finished."""
        frames: list[str] = []
        finished = threading.Event()
        turn_id = str(uuid.uuid4())
        question = body["question"]
        with self.lock:
            self.requests.append(body)
            conv_id = body.get("conversation_id")
            queued = False
            if conv_id and body.get("if_busy") == "queue" and (
                conv_id in self.running or self.queues.get(conv_id)
            ):
                self.queues.setdefault(conv_id, []).append(turn_id)
                queued = True
                frames.append(json.dumps({
                    "type": "queued", "conversation_id": conv_id,
                    "position": len(self.queues[conv_id]),
                }))
            elif conv_id:
                self.running[conv_id] = turn_id

        def run():
            nonlocal conv_id
            if not conv_id:
                time.sleep(self.conversation_id_delay)
            with self.lock:
                if queued:
                    while self.running.get(conv_id) != turn_id:
                        self.lock.wait()
                if not conv_id:
                    conv_id = str(uuid.uuid4())
                    self.conversations[conv_id] = {"title": question[:40], "messages": []}
                    self.running[conv_id] = turn_id
                conv = self.conversations[conv_id]
                frames.append(json.dumps({"type": "conversation_id", "conversation_id": conv_id}))
                history = [m["content"] for m in conv["messages"]]
                conv["messages"].append({"role": "user", "content": question})
                self.lock.notify_all()
            time.sleep(self.turn_seconds)
            answer = f"Answer to {question} (after {len(history) // 2} earlier)"
            with self.lock:
                frames.append(json.dumps({"type": "content", "content": answer}))
                conv["messages"].append({"role": "assistant", "content": answer})
                frames.append(json.dumps({"type": "done"}))
                waiting = self.queues.get(conv_id) or []
                if waiting:
                    self.running[conv_id] = waiting.pop(0)
                else:
                    self.running.pop(conv_id, None)
                self.lock.notify_all()
            finished.set()

        threading.Thread(target=run, daemon=True).start()
        return frames, finished

    def conversation(self, conv_id: str):
        with self.lock:
            conv = self.conversations.get(conv_id)
            if conv is None:
                return None
            return {
                "id": conv_id, "title": conv["title"], "created_at": "", "updated_at": "",
                "messages": list(conv["messages"]),
                "active_turn": {"turn_id": self.running[conv_id], "conversation_id": conv_id,
                                "started_at": ""} if conv_id in self.running else None,
                "queued_turns": len(self.queues.get(conv_id) or []),
            }

    def listing(self):
        with self.lock:
            return [{"id": cid, "title": c["title"], "created_at": "", "updated_at": "",
                     "message_count": len(c["messages"]), "persona_id": "primary",
                     "backend": "lifeos"}
                    for cid, c in reversed(list(self.conversations.items()))]


API = FakeChatApi()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/conversations/"):
            data = API.conversation(path[len("/api/conversations/"):])
            return self._json(200 if data else 404, data or {"detail": "not found"})
        if path == "/api/conversations":
            return self._json(200, {"conversations": API.listing()})
        if path.startswith("/api/"):
            return self._json(200, {})
        if path in ("/chat", "/"):
            file = WEB_DIR / "index.html"
        elif path.startswith("/static/"):
            file = WEB_DIR / path[len("/static/"):]
        else:
            file = WEB_DIR / path.lstrip("/")
        if not file.is_file():
            return self._json(404, {})
        body = file.read_bytes()
        ctype = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(
            file.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path != "/api/ask/stream":
            return self._json(200, {})
        frames, finished = API.start_turn(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        sent = 0
        try:
            while True:
                while sent < len(frames):
                    chunk = f"data: {frames[sent]}\n\n".encode()
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                    sent += 1
                    if API.drop_after_frames is not None and sent >= API.drop_after_frames:
                        # A network drop mid-turn: chunked encoding never
                        # terminated, so the browser sees a failed read.
                        self.connection.shutdown(2)
                        return
                if finished.is_set() and sent >= len(frames):
                    self.wfile.write(b"0\r\n\r\n")
                    break
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the turn keeps running on its own thread
        self.close_connection = True


@pytest.fixture(scope="module")
def chat_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def api():
    API.reset()
    return API


def _open_chat(page: Page, base_url):
    page.add_init_script("window.__LIFEOS_TEST_RECOVERY_POLL_MS__ = 200;")
    page.goto(f"{base_url}/chat")
    page.wait_for_selector("#backendLifeos", state="attached")
    page.evaluate(
        "async () => {"
        "  while (!(window.lifeChat && window.lifeChat.backendReady)) {"
        "    await new Promise((r) => setTimeout(r, 10));"
        "  }"
        "  await window.lifeChat.backendReady;"
        "}"
    )


def _send(page: Page, text: str):
    page.fill("#inputField", text)
    page.keyboard.press("Enter")


def test_send_while_busy_queues_and_both_replies_arrive(page: Page, chat_base_url, api):
    _open_chat(page, chat_base_url)
    _send(page, "first question")
    # Wait for the first turn's conversation id, then send again mid-turn.
    page.wait_for_function("window.lifeChat.state.currentConversationId !== null")
    _send(page, "second question")

    second_bubble = page.locator(".message.user", has_text="second question")
    expect(second_bubble.locator(".queued-marker")).to_have_text("Queued")

    expect(page.locator(".message.assistant", has_text="Answer to first question (after 0 earlier)")).to_be_visible(timeout=10000)
    # The second turn ran after the first, with its answer in history.
    expect(page.locator(".message.assistant", has_text="Answer to second question (after 1 earlier)")).to_be_visible(timeout=10000)
    expect(second_bubble.locator(".queued-marker")).to_have_count(0)

    assert [r.get("if_busy") for r in api.requests] == [None, "queue"]
    texts = page.locator(".message .message-content").all_inner_texts()
    assert [t for t in texts if "question" in t] == [
        "first question",
        "Answer to first question (after 0 earlier)",
        "second question",
        "Answer to second question (after 1 earlier)",
    ]


def test_three_sends_in_a_row_all_answer_in_order(page: Page, chat_base_url, api):
    api.turn_seconds = 0.8
    _open_chat(page, chat_base_url)
    _send(page, "q1")
    _send(page, "q2")
    _send(page, "q3")
    expect(page.locator(".message.assistant", has_text="Answer to q3 (after 2 earlier)")).to_be_visible(timeout=15000)
    conv_id = page.evaluate("window.lifeChat.state.currentConversationId")
    stored = [m["content"] for m in api.conversation(conv_id)["messages"]]
    assert stored == [
        "q1", "Answer to q1 (after 0 earlier)",
        "q2", "Answer to q2 (after 1 earlier)",
        "q3", "Answer to q3 (after 2 earlier)",
    ]


def test_reload_mid_turn_recovers_the_reply(page: Page, chat_base_url, api):
    api.turn_seconds = 3.0
    _open_chat(page, chat_base_url)
    _send(page, "slow question")
    page.wait_for_function("window.lifeChat.state.currentConversationId !== null")
    conv_id = page.evaluate("window.lifeChat.state.currentConversationId")
    # The id is persisted beyond this tab's session.
    assert page.evaluate("window.localStorage.getItem('lifeos:chat:conv:lifeos:primary')") == conv_id

    page.reload()
    page.wait_for_selector("#backendLifeos", state="attached")
    # The reopened page shows the thread and keeps waiting on the running turn.
    expect(page.locator(".message.user", has_text="slow question")).to_be_visible()
    expect(page.locator("#turn-recovery-indicator")).to_be_visible()
    expect(page.locator(".message.assistant", has_text="Answer to slow question")).to_be_visible(timeout=10000)
    expect(page.locator("#turn-recovery-indicator")).to_have_count(0)


def test_fresh_tab_reopens_the_last_conversation(page: Page, chat_base_url, api):
    api.turn_seconds = 0.2
    _open_chat(page, chat_base_url)
    _send(page, "remember me")
    expect(page.locator(".message.assistant", has_text="Answer to remember me")).to_be_visible(timeout=10000)

    fresh = page.context.new_page()  # new tab: no sessionStorage, shared localStorage
    _open_chat(fresh, chat_base_url)
    expect(fresh.locator(".message.assistant", has_text="Answer to remember me")).to_be_visible(timeout=10000)
    fresh.close()


def test_page_closed_before_the_conversation_id_arrived_finds_the_new_conversation(
    page: Page, chat_base_url, api,
):
    api.conversation_id_delay = 1.0
    api.turn_seconds = 1.0
    _open_chat(page, chat_base_url)
    _send(page, "brand new thread")
    deadline = time.time() + 5
    while not api.requests and time.time() < deadline:
        page.wait_for_timeout(50)
    assert api.requests  # sent; its conversation_id frame is still a second away
    assert page.evaluate("window.lifeChat.state.currentConversationId") is None

    page.reload()
    page.wait_for_selector("#backendLifeos", state="attached")
    expect(page.locator(".message.user", has_text="brand new thread")).to_be_visible(timeout=10000)
    expect(page.locator(".message.assistant", has_text="Answer to brand new thread")).to_be_visible(timeout=10000)
    assert page.evaluate("window.lifeChat.state.currentConversationId") is not None


def test_dropped_stream_mid_turn_recovers_without_an_error(page: Page, chat_base_url, api):
    api.turn_seconds = 1.5
    api.drop_after_frames = 1  # the stream dies right after `conversation_id`
    _open_chat(page, chat_base_url)
    _send(page, "flaky network question")
    expect(page.locator(".message.assistant", has_text="Answer to flaky network question")).to_be_visible(timeout=10000)
    expect(page.locator("text=something went wrong")).to_have_count(0)
    expect(page.locator("#statusText")).to_have_text("Ready")
