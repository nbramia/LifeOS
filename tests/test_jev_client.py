"""Tests for the Jev typed-judgment client (api/services/jev_client.py).

Every HTTP call is mocked via httpx.MockTransport — no test contacts the
real TypeSafe API.
"""
import httpx
import pytest

from api.services.jev_client import JevClient, JevError, jev_configured

pytestmark = pytest.mark.unit

_QUESTIONS = {"disposition": {"type": "choice", "instructions": "pick one", "criteria": {}}}


def _client(handler, **kwargs):
    return JevClient(api_key="test-key", transport=httpx.MockTransport(handler), **kwargs)


def _async_recorder(sleeps):
    """An async replacement for asyncio.sleep that records the delay instead
    of actually waiting."""
    async def _sleep(seconds):
        sleeps.append(seconds)
    return _sleep


def test_ask_success_returns_answers_and_records_usage():
    """Also the mutation-check witness for _body(): hardcoding
    `"state": None` there instead of forwarding the passed-in state would
    still return the (unrelated) mocked answers, but this test would catch
    it via the posted-payload assertion below."""
    def handler(request):
        assert request.url.path == "/v1/systemone"
        body = request.read()
        import json
        payload = json.loads(body)
        assert payload["state"] == "some state"
        assert payload["questions"] == _QUESTIONS
        return httpx.Response(
            200,
            json={
                "answers": {"disposition": {"choice": "task", "confidence": 0.9}},
                "usage": {"input_tokens": 123},
                "model": "jev-1.13.0",
            },
        )

    client = _client(handler)
    answers = client.ask("some state", _QUESTIONS)
    assert answers == {"disposition": {"choice": "task", "confidence": 0.9}}
    assert client.last_usage == 123
    assert client.last_model == "jev-1.13.0"


def test_ask_sends_authorization_header():
    """A request missing/wrong Authorization must fail — this is the
    mutation-check witness: deleting the header from _headers() breaks it."""
    def handler(request):
        auth = request.headers.get("authorization")
        if auth != "Bearer test-key":
            return httpx.Response(401, json={"error": "missing auth"})
        return httpx.Response(200, json={"answers": {"a": {"choice": "x"}}})

    client = _client(handler)
    answers = client.ask("state", _QUESTIONS)
    assert answers == {"a": {"choice": "x"}}


def test_ask_retries_429_honoring_retry_after_then_succeeds(monkeypatch):
    """This is the mutation-check witness for the retry loop: if the retry
    loop is deleted (first non-2xx immediately raises), this test fails
    because it would see a JevError instead of the successful answers."""
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"answers": {"a": {"choice": "ok"}}})

    sleeps = []
    monkeypatch.setattr("api.services.jev_client.time.sleep", lambda s: sleeps.append(s))

    client = _client(handler)
    answers = client.ask("state", _QUESTIONS)
    assert answers == {"a": {"choice": "ok"}}
    assert calls["count"] == 2
    assert sleeps == [0.0]


def test_ask_exhausts_429_retries_raises_jev_error(monkeypatch):
    """Mutation-check witness for _MAX_ATTEMPTS: setting it to 1 would make
    the handler-call and sleep counts below fail (1 call, 0 sleeps instead
    of 3 and 2)."""
    calls = {"count": 0}
    sleeps = []
    monkeypatch.setattr("api.services.jev_client.time.sleep", lambda s: sleeps.append(s))

    def handler(request):
        calls["count"] += 1
        return httpx.Response(429)

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        client.ask("state", _QUESTIONS)
    assert "429" in str(exc_info.value)
    assert "state" not in str(exc_info.value)
    assert calls["count"] == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_aask_retries_429_honoring_retry_after_then_succeeds(monkeypatch):
    """Async counterpart to test_ask_retries_429_honoring_retry_after_then_succeeds."""
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"answers": {"a": {"choice": "ok"}}})

    sleeps = []
    monkeypatch.setattr("api.services.jev_client.asyncio.sleep", _async_recorder(sleeps))

    client = _client(handler)
    answers = await client.aask("state", _QUESTIONS)
    assert answers == {"a": {"choice": "ok"}}
    assert calls["count"] == 2
    assert sleeps == [0.0]


@pytest.mark.asyncio
async def test_aask_exhausts_429_retries_raises_jev_error(monkeypatch):
    calls = {"count": 0}
    sleeps = []
    monkeypatch.setattr("api.services.jev_client.asyncio.sleep", _async_recorder(sleeps))

    def handler(request):
        calls["count"] += 1
        return httpx.Response(429)

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        await client.aask("state", _QUESTIONS)
    assert "429" in str(exc_info.value)
    assert "state" not in str(exc_info.value)
    assert calls["count"] == 3
    assert len(sleeps) == 2


def test_ask_401_raises_jev_error_without_state_in_message():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid key"})

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        client.ask("super secret personal state", _QUESTIONS)
    message = str(exc_info.value)
    assert "401" in message
    assert "super secret personal state" not in message


def test_ask_missing_answers_field_raises_jev_error():
    def handler(request):
        return httpx.Response(200, json={"not_answers": {}})

    client = _client(handler)
    with pytest.raises(JevError):
        client.ask("state", _QUESTIONS)


def test_ask_empty_api_key_raises_before_any_request():
    """The mutation-check witness for the empty-key guard: without it, an
    empty api_key produces an `Authorization: Bearer ` header, which httpx
    itself rejects as an illegal header value, so a MockTransport that
    asserts it's never called catches either failure mode as long as no
    successful response comes back."""
    def handler(request):
        raise AssertionError("must not send a request with no API key")

    client = JevClient(api_key="", transport=httpx.MockTransport(handler))
    with pytest.raises(JevError, match="not configured"):
        client.ask("state", _QUESTIONS)


@pytest.mark.asyncio
async def test_aask_empty_api_key_raises_before_any_request():
    def handler(request):
        raise AssertionError("must not send a request with no API key")

    client = JevClient(api_key="", transport=httpx.MockTransport(handler))
    with pytest.raises(JevError, match="not configured"):
        await client.aask("state", _QUESTIONS)


def test_ask_transport_error_raises_jev_error():
    """Mutation-check witness for the httpx.HTTPError wrapping: remove the
    try/except around client.post and this test fails with a raw
    httpx.ConnectTimeout instead of JevError. Also asserts `from None`:
    a chained `__cause__` could carry the original exception's message
    (and thus request data) into a traceback even when the JevError's own
    message is sanitized."""
    def handler(request):
        raise httpx.ConnectTimeout("connection timed out")

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        client.ask("state", _QUESTIONS)
    message = str(exc_info.value)
    assert "ConnectTimeout" in message
    assert "connection timed out" not in message
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_aask_transport_error_raises_jev_error():
    def handler(request):
        raise httpx.ConnectTimeout("connection timed out")

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        await client.aask("state", _QUESTIONS)
    message = str(exc_info.value)
    assert "ConnectTimeout" in message
    assert "connection timed out" not in message
    assert exc_info.value.__cause__ is None


def test_ask_unserializable_state_raises_jev_error_before_sending():
    """A `state` that json can't serialize must fail before the MockTransport
    handler is ever invoked, wrapped as JevError with no chained cause."""
    def handler(request):
        raise AssertionError("must not send a request with an unserializable state")

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        client.ask(object(), _QUESTIONS)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_aask_unserializable_state_raises_jev_error_before_sending():
    def handler(request):
        raise AssertionError("must not send a request with an unserializable state")

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        await client.aask(object(), _QUESTIONS)
    assert exc_info.value.__cause__ is None


def test_jev_configured_false_with_empty_key(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    assert jev_configured() is False


def test_jev_configured_true_with_key(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "typesafe_api_key", "sk-123")
    assert jev_configured() is True


@pytest.mark.asyncio
async def test_aask_success():
    def handler(request):
        return httpx.Response(200, json={"answers": {"a": {"choice": "y"}}, "usage": {"input_tokens": 5}})

    client = _client(handler)
    answers = await client.aask("state", _QUESTIONS)
    assert answers == {"a": {"choice": "y"}}
    assert client.last_usage == 5
