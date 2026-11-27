"""A local caller asks the operator to Approve or Deny on the primary bot.

`POST /api/approvals/external` opens a request and sends the Telegram
message; `GET /api/approvals/external/{token}` reads its state; the
operator's `ext:` button press decides it. Every request lives in a
temporary SQLite file, every Telegram send is captured, and the real
Telegram HTTP call is replaced with one that fails the test, so nothing
reaches the real data directory or Telegram.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import external_approvals as routes
from api.services import external_approval as ea

pytestmark = pytest.mark.unit

HASH = "ab" * 32
OTHER_HASH = "cd" * 32
SUMMARY = "Reply to the synthetic plumber thread with the Tuesday quote"
SOURCE = "text from +1 555 0100"
T0 = 1_900_000_000.0


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach Telegram")

    monkeypatch.setattr("api.services.telegram.httpx.post", refuse)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "external_approvals.db")


@pytest.fixture
def store(db_path, clock):
    instance = ea.ExternalApprovalStore(db_path, clock=clock)
    ea.reset_store(instance)
    yield instance
    ea.reset_store(None)


@pytest.fixture
def sent(monkeypatch):
    """Captured Telegram requests: (text, buttons)."""
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    def fake_send(text, buttons):
        calls.append((text, buttons))
        return 2000 + len(calls)

    monkeypatch.setattr(routes, "send_message_with_buttons", fake_send)
    return calls


@pytest.fixture
def app():
    from fastapi.exceptions import RequestValidationError

    from api.main import validation_exception_handler

    application = FastAPI()
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.include_router(routes.router)
    return application


@pytest.fixture
def local(app, store, sent):
    return TestClient(app, client=("127.0.0.1", 50000))


def body(**overrides):
    payload = {"summary": SUMMARY, "request_hash": HASH, "ttl_seconds": 600}
    payload.update(overrides)
    return payload


def open_request(client, **overrides) -> dict:
    resp = client.post("/api/approvals/external", json=body(**overrides))
    assert resp.status_code == 200, resp.text
    return resp.json()


def state(client, token: str) -> dict:
    resp = client.get(f"/api/approvals/external/{token}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Opening a request
# ---------------------------------------------------------------------------

def test_open_sends_one_message_with_buttons_and_returns_a_token(local, sent):
    result = open_request(local, source=SOURCE)
    token = result["token"]
    assert len(token) == 16 and all(c in "0123456789abcdef" for c in token)
    assert result["expires_at"] == datetime.fromtimestamp(T0 + 600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    assert len(sent) == 1
    text, buttons = sent[0]
    assert text.startswith("Chief of Staff asks:\n\n" + ea.SUMMARY_QUOTE + SUMMARY + "\n")
    assert f"From: {SOURCE}" in text
    assert f"Request: {HASH[:12]}" in text
    assert "Expires: " in text
    assert buttons == [("Approve", f"ext:y:{token}"), ("Deny", f"ext:n:{token}")]


def test_summary_lines_cannot_pass_for_the_request_details():
    planted = "Pay the synthetic invoice\nFrom: text from +1 555 0199\nRequest: 000000000000"
    text = ea.request_text(planted, "text from +1 555 0100", HASH, T0 + 600)
    lines = text.split("\n")
    assert [line for line in lines if line.startswith("From: ")] == ["From: text from +1 555 0100"]
    assert [line for line in lines if line.startswith("Request: ")] == [f"Request: {HASH[:12]}"]
    assert ea.SUMMARY_QUOTE + "From: text from +1 555 0199" in lines


def test_the_expiry_is_shown_in_the_operators_timezone(local, sent, monkeypatch):
    monkeypatch.setattr(ea.settings, "timezone", "America/New_York")
    open_request(local)
    local_expiry = datetime.fromtimestamp(T0 + 600, tz=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York"))
    assert f"Expires: {local_expiry.strftime('%b %d, %I:%M %p %Z')}" in sent[0][0]


def test_the_summary_is_shown_whole_and_without_control_characters(local, sent):
    summary = ("x" * 290) + "\x07‮\nend"
    open_request(local, summary=summary, source="email\nfrom someone@example.com")
    text = sent[0][0]
    assert ("x" * 290) + "\n" + ea.SUMMARY_QUOTE + "end" in text
    assert "\x07" not in text and "‮" not in text
    assert "From: emailfrom someone@example.com" in text


def test_the_default_ttl_is_three_hours(local, sent):
    resp = local.post("/api/approvals/external", json={"summary": SUMMARY, "request_hash": HASH})
    assert resp.status_code == 200
    expected = datetime.fromtimestamp(T0 + 10800, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    assert resp.json()["expires_at"] == expected
    assert state(local, resp.json()["token"])["expires_at"] == expected


@pytest.mark.parametrize("overrides", [
    {"summary": ""},
    {"summary": "   \x07  "},
    {"summary": "y" * 301},
    {"request_hash": HASH.upper()},
    {"request_hash": HASH[:63]},
    {"request_hash": HASH + "a"},
    {"ttl_seconds": 59},
    {"ttl_seconds": 86401},
    {"ttl_seconds": "600"},
    {"source": ""},
    {"source": "s" * 121},
])
def test_an_invalid_body_is_400_and_sends_nothing(local, sent, store, overrides):
    resp = local.post("/api/approvals/external", json=body(**overrides))
    assert resp.status_code == 400
    assert sent == []
    with ea.connect_closing(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_approvals").fetchone()[0] == 0


@pytest.mark.parametrize("ttl", [60, 86400])
def test_ttl_bounds_are_accepted(local, ttl):
    open_request(local, ttl_seconds=ttl)


def test_a_failed_send_is_503_and_the_request_is_deleted(app, store, monkeypatch):
    monkeypatch.setattr(routes, "send_message_with_buttons", lambda text, buttons: None)
    client = TestClient(app, client=("127.0.0.1", 50000))
    resp = client.post("/api/approvals/external", json=body())
    assert resp.status_code == 503
    with ea.connect_closing(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_approvals").fetchone()[0] == 0


def test_a_raising_send_is_503_and_the_request_is_deleted(app, store, monkeypatch):
    def boom(text, buttons):
        raise RuntimeError("network down")

    monkeypatch.setattr(routes, "send_message_with_buttons", boom)
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.post("/api/approvals/external", json=body()).status_code == 503
    with ea.connect_closing(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_approvals").fetchone()[0] == 0


def test_telegram_disabled_is_503(app, store, monkeypatch):
    from api.services import telegram

    monkeypatch.setattr(routes, "send_message_with_buttons", telegram.send_message_with_buttons)
    monkeypatch.setattr(telegram.settings, "telegram_bot_token", "")
    monkeypatch.setattr(telegram.settings, "telegram_chat_id", "")
    assert not telegram.settings.telegram_enabled
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.post("/api/approvals/external", json=body()).status_code == 503


# ---------------------------------------------------------------------------
# Reading a request
# ---------------------------------------------------------------------------

def test_get_returns_the_hash_and_source_it_was_opened_with(local):
    token = open_request(local, source=SOURCE)["token"]
    assert state(local, token) == {
        "state": "pending",
        "request_hash": HASH,
        "expires_at": datetime.fromtimestamp(T0 + 600, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": SOURCE,
    }


@pytest.mark.parametrize("token", ["0123456789abcdef", "nothex", "0123456789ABCDEF", "0" * 17])
def test_get_for_an_unknown_or_malformed_token_is_404(local, token):
    resp = local.get(f"/api/approvals/external/{token}")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "unknown or lost"}


def test_a_pending_request_past_expiry_reads_expired(local, clock):
    token = open_request(local)["token"]
    clock.now = T0 + 600
    assert state(local, token)["state"] == "expired"


# ---------------------------------------------------------------------------
# Who may call
# ---------------------------------------------------------------------------

REFUSED_HEADERS = [
    {"X-Forwarded-For": "100.64.0.9"},
    {"X-Forwarded-Host": "lifeos.example.ts.net"},
    {"X-Forwarded-Proto": "https"},
    {"Forwarded": "for=100.64.0.9"},
    {"X-Real-IP": "100.64.0.9"},
    {"Tailscale-User-Login": "someone@example.com"},
    {"Tailscale-Funnel-Request": "?1"},
    {"CF-Connecting-IP": "203.0.113.7"},
    {"Origin": "http://localhost:8000"},
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Mode": "cors"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
    {"X-LifeOS-Agent-Session": "synthetic-session"},
    {"X-LifeOS-Hermes-Turn": "synthetic-turn"},
    {"X-LifeOS-Connected-App": "synthetic-app"},
    {"X-LifeOS-Turn-ID": "synthetic-turn-id"},
    {"X-LifeOS-Scheduled-Fire": "1"},
]


@pytest.mark.parametrize("headers", REFUSED_HEADERS, ids=lambda h: next(iter(h)))
def test_loopback_with_a_proxy_browser_or_relay_header_is_403(app, store, sent, headers):
    client = TestClient(app, client=("127.0.0.1", 50000))
    resp = client.post("/api/approvals/external", json=body(), headers=headers)
    assert resp.status_code == 403
    assert sent == []
    token = open_request(TestClient(app, client=("127.0.0.1", 50000)))["token"]
    assert client.get(f"/api/approvals/external/{token}", headers=headers).status_code == 403


@pytest.mark.parametrize("peer", ["100.64.0.9", "192.168.1.20", "testclient", "10.0.0.1"])
def test_a_non_loopback_peer_is_403(app, store, sent, peer):
    token = open_request(TestClient(app, client=("127.0.0.1", 50000)))["token"]
    sent.clear()
    remote = TestClient(app, client=(peer, 40000))
    assert remote.post("/api/approvals/external", json=body()).status_code == 403
    assert remote.post("/api/approvals/external", json={"bad": 1}).status_code == 403
    assert remote.get(f"/api/approvals/external/{token}").status_code == 403
    assert sent == []


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_every_loopback_spelling_is_accepted(app, store, sent, peer):
    open_request(TestClient(app, client=(peer, 50000)))


def test_the_routes_are_registered_on_the_app():
    from api.main import app as main_app

    paths = {getattr(route, "path", "") for route in main_app.routes}
    assert {"/api/approvals/external", "/api/approvals/external/{token}"} <= paths


def test_no_mcp_tool_reaches_the_approval_routes():
    import mcp_server

    assert not [p for p in mcp_server.CURATED_ENDPOINTS if p.lower().startswith("/api/approvals")]


# ---------------------------------------------------------------------------
# Button presses
# ---------------------------------------------------------------------------

def _listener(tmp_path):
    from api.services.telegram import TelegramBotListener

    with patch.object(TelegramBotListener, "_STATE_FILE", tmp_path / "telegram_state.json"):
        listener = TelegramBotListener()
    listener._chat_id = "123"
    listener._is_primary = True
    return listener


def _press(data: str, *, chat_id: str = "123", from_id: str = "123", message_id: int = 2001) -> dict:
    return {"update_id": 7, "callback_query": {
        "id": "cb1", "data": data, "from": {"id": int(from_id)},
        "message": {"message_id": message_id, "chat": {"id": chat_id}, "text": "Chief of Staff asks:"},
    }}


async def _deliver(listener, update):
    with patch("api.services.telegram.settings") as mock_settings, \
         patch("api.services.telegram.answer_callback_query", new_callable=AsyncMock) as answer, \
         patch("api.services.telegram.edit_message_text", new_callable=AsyncMock) as edit, \
         patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as chat:
        mock_settings.telegram_chat_id = "123"
        await listener._handle_update(update)
    return answer, edit, chat


def _answer_text(answer) -> str:
    return answer.call_args.args[1]


def _edited_text(edit) -> str:
    return edit.call_args.args[2]


async def test_approve_moves_the_request_to_approved_and_edits_the_message(local, sent, tmp_path):
    token = open_request(local)["token"]
    answer, edit, chat = await _deliver(_listener(tmp_path), _press(sent[0][1][0][1]))
    assert state(local, token)["state"] == "approved"
    assert _answer_text(answer) == ea.OUTCOME_APPROVED
    assert edit.call_args.args[1] == 2001
    assert _edited_text(edit).endswith(ea.OUTCOME_APPROVED)
    chat.assert_not_called()


async def test_deny_moves_the_request_to_denied(local, sent, tmp_path):
    token = open_request(local)["token"]
    answer, edit, _ = await _deliver(_listener(tmp_path), _press(sent[0][1][1][1]))
    assert state(local, token)["state"] == "denied"
    assert _answer_text(answer) == ea.OUTCOME_DENIED
    assert _edited_text(edit).endswith(ea.OUTCOME_DENIED)


async def test_a_second_press_changes_nothing(local, sent, tmp_path):
    token = open_request(local)["token"]
    listener = _listener(tmp_path)
    await _deliver(listener, _press(sent[0][1][1][1]))
    answer, edit, _ = await _deliver(listener, _press(sent[0][1][0][1]))
    assert state(local, token)["state"] == "denied"
    assert _answer_text(answer) == ea.OUTCOME_ALREADY_DECIDED
    assert _edited_text(edit).endswith(ea.OUTCOME_ALREADY_DECIDED)


async def test_a_press_after_expiry_leaves_it_expired(local, sent, clock, tmp_path):
    token = open_request(local)["token"]
    clock.now = T0 + 600
    answer, edit, _ = await _deliver(_listener(tmp_path), _press(sent[0][1][0][1]))
    assert state(local, token)["state"] == "expired"
    assert _answer_text(answer) == "This request expired; nothing was approved."
    assert _edited_text(edit).endswith("This request expired; nothing was approved.")
    clock.now = T0
    answer, _, _ = await _deliver(_listener(tmp_path), _press(sent[0][1][0][1]))
    assert state(local, token)["state"] == "expired"
    assert _answer_text(answer) == ea.OUTCOME_EXPIRED


async def test_a_press_just_before_expiry_counts(local, sent, clock, tmp_path):
    token = open_request(local)["token"]
    clock.now = T0 + 599
    await _deliver(_listener(tmp_path), _press(sent[0][1][0][1]))
    assert state(local, token)["state"] == "approved"


async def test_a_press_on_another_message_changes_nothing(local, sent, tmp_path):
    token = open_request(local)["token"]
    answer, edit, _ = await _deliver(_listener(tmp_path), _press(sent[0][1][0][1], message_id=9999))
    assert state(local, token)["state"] == "pending"
    assert _answer_text(answer) == ea.OUTCOME_WRONG_MESSAGE
    edit.assert_awaited_once()


async def test_a_press_on_an_unknown_token_answers_lost(local, sent, tmp_path):
    answer, edit, _ = await _deliver(_listener(tmp_path), _press("ext:y:0123456789abcdef"))
    assert _answer_text(answer) == "This request was lost; ask again."
    assert _edited_text(edit).endswith("This request was lost; ask again.")


@pytest.mark.parametrize("who", [{"chat_id": "456"}, {"from_id": "456"}])
async def test_a_press_from_another_chat_or_user_changes_nothing(local, sent, tmp_path, who):
    token = open_request(local)["token"]
    answer, edit, _ = await _deliver(_listener(tmp_path), _press(sent[0][1][0][1], **who))
    assert state(local, token)["state"] == "pending"
    answer.assert_not_called()
    edit.assert_not_called()


async def test_one_request_cannot_decide_another(local, sent, tmp_path):
    first = open_request(local)["token"]
    second = open_request(local, request_hash=OTHER_HASH)["token"]
    # The second request's token on the first request's message.
    await _deliver(_listener(tmp_path), _press(f"ext:y:{second}", message_id=2001))
    assert state(local, first)["state"] == "pending"
    assert state(local, second)["state"] == "pending"
    assert state(local, second)["request_hash"] == OTHER_HASH


async def test_logs_carry_token_and_hash_but_never_the_summary_or_source(local, sent, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    token = open_request(local, source=SOURCE)["token"]
    await _deliver(_listener(tmp_path), _press(sent[0][1][0][1]))
    await _deliver(_listener(tmp_path), _press(sent[0][1][0][1]))
    messages = [r.getMessage() for r in caplog.records if r.name == ea.__name__]
    assert any("open" in m and token in m and HASH in m for m in messages)
    assert any("decision" in m and token in m and ea.OUTCOME_APPROVED in m for m in messages)
    assert any("refused" in m and token in m for m in messages)
    for record in caplog.records:
        text = record.getMessage()
        assert SUMMARY not in text and SOURCE not in text


# ---------------------------------------------------------------------------
# Persistence and single use
# ---------------------------------------------------------------------------

def _open_with_message(store, message_id=3001, ttl=600) -> str:
    token = store.open(HASH, SUMMARY, SOURCE, ttl)["token"]
    store.set_message_id(token, message_id)
    return token


def test_a_request_and_its_decision_survive_a_restart(db_path, clock):
    first = ea.ExternalApprovalStore(db_path, clock=clock)
    token = _open_with_message(first)
    restarted = ea.ExternalApprovalStore(db_path, clock=clock)
    assert restarted.get(token)["state"] == "pending"
    assert restarted.decide(token, approve=True, message_id=3001) == ea.OUTCOME_APPROVED
    again = ea.ExternalApprovalStore(db_path, clock=clock)
    assert again.get(token)["state"] == "approved"
    assert again.get(token)["expires_at"] == ea.iso_utc(T0 + 600)


def test_a_restart_does_not_extend_the_expiry(db_path, clock):
    token = _open_with_message(ea.ExternalApprovalStore(db_path, clock=clock))
    clock.now = T0 + 601
    restarted = ea.ExternalApprovalStore(db_path, clock=clock)
    assert restarted.decide(token, approve=True, message_id=3001) == ea.OUTCOME_EXPIRED
    assert restarted.get(token)["state"] == "expired"


def test_single_use_holds_across_two_store_instances(db_path, clock):
    one = ea.ExternalApprovalStore(db_path, clock=clock)
    two = ea.ExternalApprovalStore(db_path, clock=clock)
    token = _open_with_message(one)
    assert one.decide(token, approve=False, message_id=3001) == ea.OUTCOME_DENIED
    assert two.decide(token, approve=True, message_id=3001) == ea.OUTCOME_ALREADY_DECIDED
    assert one.get(token)["state"] == "denied"


def test_the_conditional_update_lets_only_one_press_win(db_path, clock, monkeypatch):
    """Two presses that both read the request as pending: the write itself
    admits only the first."""
    store = ea.ExternalApprovalStore(db_path, clock=clock)
    other = ea.ExternalApprovalStore(db_path, clock=clock)
    token = _open_with_message(store)
    real_row = ea.ExternalApprovalStore._row

    def stale_row(self, conn, tok):
        row = real_row(self, conn, tok)
        if self is store:
            # The other press lands between this press's read and its write.
            monkeypatch.setattr(ea.ExternalApprovalStore, "_row", real_row)
            other.decide(tok, approve=False, message_id=3001)
        return row

    monkeypatch.setattr(ea.ExternalApprovalStore, "_row", stale_row)
    assert store.decide(token, approve=True, message_id=3001) == ea.OUTCOME_ALREADY_DECIDED
    assert store.get(token)["state"] == "denied"


def test_retention_deletes_requests_a_week_past_expiry(db_path, clock):
    store = ea.ExternalApprovalStore(db_path, clock=clock)
    old = _open_with_message(store, ttl=600)
    recent = _open_with_message(store, message_id=3002, ttl=86400)
    clock.now = T0 + 600 + ea.RETENTION_SECONDS + 1
    store.open(HASH, SUMMARY, None, 600)
    assert store.get(old) is None
    assert store.get(recent)["state"] == "expired"
    assert store.decide(old, approve=True, message_id=3001) == ea.OUTCOME_LOST


def test_the_database_lives_next_to_the_other_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(ea.settings, "chroma_path", str(tmp_path / "data" / "chromadb"))
    assert ea.get_db_path() == str(tmp_path / "data" / "external_approvals.db")


# ---------------------------------------------------------------------------
# Schedules cannot reach the routes
# ---------------------------------------------------------------------------

APPROVAL_PATH_SPELLINGS = [
    "/api/approvals/external",
    "/api/approvals/external/0123456789abcdef",
    "/api/approvals",
    "//api/approvals/external",
    "/api//approvals/external",
    "/API/Approvals/External",
    "/api/approvals/../approvals/external",
    "/api/tasks/../approvals/external",
    "/api/%61pprovals/external",
    "/api%2Fapprovals%2Fexternal",
    "/api/%2561pprovals/external",
    "/api/./approvals/external",
    "/api\\approvals\\external",
    "/api/approvals/external?x=1",
    "/api/approvals%00/external",
]


@pytest.mark.parametrize("endpoint", APPROVAL_PATH_SPELLINGS)
async def test_a_scheduled_endpoint_never_calls_the_approval_routes(endpoint, monkeypatch):
    from api.services import scheduler_store

    class NoClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the scheduler made an HTTP call")

    monkeypatch.setattr(scheduler_store.httpx, "AsyncClient", NoClient)
    scheduler = scheduler_store.SchedulerScheduler.__new__(scheduler_store.SchedulerScheduler)
    for method in ("GET", "POST"):
        result = await scheduler._call_endpoint({"endpoint": endpoint, "method": method, "params": {}})
        assert result == "Schedules cannot call the approval routes."


@pytest.mark.parametrize("endpoint", ["/api/approvalsx", "/api/tasks", "/api/scheduler/approvals-report"])
def test_other_paths_are_not_approval_paths(endpoint):
    assert not ea.is_approval_path(endpoint)


async def test_a_scheduled_endpoint_call_carries_the_scheduled_fire_header(monkeypatch):
    from api.services import scheduler_store

    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"scheduler_message": "ok"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            seen["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(scheduler_store.httpx, "AsyncClient", FakeClient)
    scheduler = scheduler_store.SchedulerScheduler.__new__(scheduler_store.SchedulerScheduler)
    assert await scheduler._call_endpoint({"endpoint": "/api/tasks", "method": "GET"}) == "ok"
    assert seen["headers"] == {"X-LifeOS-Scheduled-Fire": "1"}
    assert ea.refusal_reason("127.0.0.1", seen["headers"]) is not None


@pytest.mark.parametrize("endpoint", APPROVAL_PATH_SPELLINGS[:9])
def test_saving_a_schedule_with_an_approval_endpoint_is_refused(endpoint):
    from api.services.scheduler_validation import ScheduleActionValidationError, validate_action_inputs

    with pytest.raises(ScheduleActionValidationError) as err:
        validate_action_inputs("endpoint", "", {"endpoint": endpoint, "method": "POST"})
    assert err.value.field == "endpoint_config.endpoint"
