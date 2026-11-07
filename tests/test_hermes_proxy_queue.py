"""`if_busy: "queue"` on the Hermes proxy (`api/routes/_proxy.py`).

A queued Hermes turn waits server-side for the turn running on its
conversation and only then contacts Hermes, so the turns persist in Q/A
order and the running turn is never cancelled.
"""
import asyncio
import json

import httpx
import pytest

from api.routes import hermes_proxy as hp
from api.services.conversation_store import ConversationStore
from tests.test_hermes_proxy import _isolated_task_store, _manual_request  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def hermes_store(tmp_path, monkeypatch):
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(hp, "get_store", lambda: store)
    return store


def _scripted_client(gen_factories, sends):
    """A fake httpx client whose Nth `send` streams the Nth generator."""
    class _Upstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        def __init__(self, factory):
            self._factory = factory

        async def aiter_raw(self):
            async for chunk in self._factory():
                yield chunk

        async def aclose(self):
            pass

    class _Client:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            sends.append(len(sends))
            return _Upstream(gen_factories[len(sends) - 1])

        async def aclose(self):
            pass

    return _Client


async def test_queued_turn_waits_for_the_running_turn_before_contacting_hermes(
    monkeypatch, hermes_store,
):
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    hold = asyncio.Event()
    sends: list[int] = []

    async def first_gen():
        yield b'data: {"type": "conversation_id", "conversation_id": "queue-conv-1"}\n\n'
        yield b'data: {"type": "content", "content": "first answer"}\n\n'
        await hold.wait()
        yield b'data: {"type": "done"}\n\n'

    async def second_gen():
        yield b'data: {"type": "conversation_id", "conversation_id": "queue-conv-1"}\n\n'
        yield b'data: {"type": "content", "content": "second answer"}\n\n'
        yield b'data: {"type": "done"}\n\n'

    client_cls = _scripted_client([first_gen, second_gen], sends)
    monkeypatch.setattr(hp, "_client", lambda: client_cls())
    endpoint = next(r for r in hp.router.routes if r.path.endswith("/ask/stream")).endpoint

    first = await endpoint(_manual_request({"question": "first question", "conversation_id": "queue-conv-1"}))
    first_iter = first.body_iterator
    await first_iter.__anext__()
    await first_iter.__anext__()
    await first_iter.aclose()
    first_turn = hp.get_turn_registry().get_by_conversation("queue-conv-1")
    assert first_turn is not None

    second = await endpoint(_manual_request({
        "question": "second question", "conversation_id": "queue-conv-1",
        "if_busy": "queue", "client_turn_id": "hermes-turn-2",
    }))
    assert second.status_code == 200
    second_iter = second.body_iterator
    queued_frame = await second_iter.__anext__()
    assert json.loads(queued_frame[len("data: "):]) == {
        "type": "queued", "conversation_id": "queue-conv-1",
        "position": 1, "client_turn_id": "hermes-turn-2",
    }
    await asyncio.sleep(0.05)
    assert sends == [0]  # Hermes not contacted for the queued turn yet
    assert not first_turn.task.done()
    assert hp.get_turn_registry().queued_count("queue-conv-1") == 1

    hold.set()
    rest = b"".join([
        chunk if isinstance(chunk, bytes) else chunk.encode()
        async for chunk in second_iter
    ])
    await first_turn.task
    assert sends == [0, 1]
    assert b"second answer" in rest

    messages = hermes_store.get_messages("queue-conv-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
        ("assistant", "second answer"),
    ]


async def test_queue_request_on_an_idle_conversation_relays_at_once(monkeypatch, hermes_store):
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")
    sends: list[int] = []

    async def only_gen():
        yield b'data: {"type": "conversation_id", "conversation_id": "idle-conv"}\n\n'
        yield b'data: {"type": "content", "content": "answer"}\n\n'
        yield b'data: {"type": "done"}\n\n'

    client_cls = _scripted_client([only_gen], sends)
    monkeypatch.setattr(hp, "_client", lambda: client_cls())
    endpoint = next(r for r in hp.router.routes if r.path.endswith("/ask/stream")).endpoint

    response = await endpoint(_manual_request({
        "question": "q", "conversation_id": "idle-conv", "if_busy": "queue",
    }))
    body = b"".join([c if isinstance(c, bytes) else c.encode() async for c in response.body_iterator])
    assert sends == [0]
    assert b'"queued"' not in body
    assert b"answer" in body
