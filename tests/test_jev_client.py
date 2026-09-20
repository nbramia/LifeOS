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


def test_ask_success_returns_answers_and_records_usage():
    def handler(request):
        assert request.url.path == "/v1/systemone"
        body = request.read()
        import json
        payload = json.loads(body)
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
    monkeypatch.setattr("api.services.jev_client.time.sleep", lambda s: None)

    def handler(request):
        return httpx.Response(429)

    client = _client(handler)
    with pytest.raises(JevError) as exc_info:
        client.ask("state", _QUESTIONS)
    assert "429" in str(exc_info.value)
    assert "state" not in str(exc_info.value)


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
