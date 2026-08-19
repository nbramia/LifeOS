"""
Tests for the Gmail draft send gate at the HTTP and MCP surfaces.

The invariant belongs at the send endpoint: every caller that can create a
LifeOS draft and then send by draft_id must pass through the same safety check.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from api.services import gmail_draft_ledger as ledger_mod
from api.services.gmail import DraftMessage
from api.services.gmail_draft_ledger import GmailDraftLedger

pytestmark = pytest.mark.unit


TURN_HEADER = "X-LifeOS-Turn-ID"


@pytest.fixture
def mock_gmail_service():
    mock = MagicMock()
    mock.create_draft.return_value = DraftMessage(
        draft_id="draft123",
        message_id="msg123",
        subject="Test Subject",
        to="recipient@example.com",
        body="Test body",
        source_account="personal",
    )
    mock.send_draft.return_value = "sent-msg-1"
    return mock


@pytest.fixture
def draft_ledger(tmp_path, monkeypatch):
    # Patch the singleton itself (not just the name `api/routes/gmail.py`
    # imports) — the shared check_send_gate() helper looks the ledger up via
    # its own module-level get_gmail_draft_ledger(), so the create-side route
    # and the send-side gate must resolve to the SAME instance.
    ledger = GmailDraftLedger(str(tmp_path / "gmail_draft_ledger.db"))
    monkeypatch.setattr(ledger_mod, "_draft_ledger", ledger)
    monkeypatch.setattr(
        ledger_mod.settings,
        "gmail_draft_send_cooldown_seconds",
        300,
    )
    return ledger


@pytest.fixture
def client(mock_gmail_service, draft_ledger):
    from api.main import app

    with patch("api.routes.gmail.get_gmail_service", return_value=mock_gmail_service):
        yield TestClient(app)


def _create_draft(client: TestClient, headers: dict | None = None) -> httpx.Response:
    return client.post(
        "/api/gmail/drafts",
        json={
            "to": "recipient@example.com",
            "subject": "Test Subject",
            "body": "Test body",
        },
        headers=headers or {},
    )


def test_send_refuses_lifeos_draft_inside_cooldown_without_turn_id(
    client,
    mock_gmail_service,
):
    response = _create_draft(client)
    assert response.status_code == 200

    send = client.post("/api/gmail/send", json={"draft_id": "draft123"})

    assert send.status_code == 409
    assert "confirmation" in send.json()["detail"]
    mock_gmail_service.send_draft.assert_not_called()


def test_send_refuses_same_turn_id_regardless_of_elapsed_time(
    client,
    draft_ledger,
    mock_gmail_service,
):
    response = _create_draft(client, headers={TURN_HEADER: "turn-1"})
    assert response.status_code == 200

    old_created_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    draft_ledger.record_created(
        account="personal",
        draft_id="draft123",
        created_at=old_created_at,
        turn_id="turn-1",
    )

    send = client.post(
        "/api/gmail/send",
        json={"draft_id": "draft123"},
        headers={TURN_HEADER: "turn-1"},
    )

    assert send.status_code == 409
    assert "confirmation" in send.json()["detail"]
    mock_gmail_service.send_draft.assert_not_called()


def test_send_allows_different_turn_id(
    client,
    mock_gmail_service,
):
    response = _create_draft(client, headers={TURN_HEADER: "turn-1"})
    assert response.status_code == 200

    send = client.post(
        "/api/gmail/send",
        json={"draft_id": "draft123"},
        headers={TURN_HEADER: "turn-2"},
    )

    assert send.status_code == 200
    assert send.json()["message_id"] == "sent-msg-1"
    mock_gmail_service.send_draft.assert_called_once_with("draft123")


def test_send_allows_past_window_without_turn_id(
    client,
    draft_ledger,
    mock_gmail_service,
):
    old_created_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    draft_ledger.record_created(
        account="personal",
        draft_id="old-draft",
        created_at=old_created_at,
    )

    send = client.post("/api/gmail/send", json={"draft_id": "old-draft"})

    assert send.status_code == 200
    assert send.json()["message_id"] == "sent-msg-1"
    mock_gmail_service.send_draft.assert_called_once_with("old-draft")


def test_send_allows_unknown_draft(
    client,
    mock_gmail_service,
):
    send = client.post("/api/gmail/send", json={"draft_id": "handwritten-draft"})

    assert send.status_code == 200
    assert send.json()["message_id"] == "sent-msg-1"
    mock_gmail_service.send_draft.assert_called_once_with("handwritten-draft")


def test_send_fails_closed_when_ledger_read_raises(
    mock_gmail_service,
    monkeypatch,
    caplog,
):
    from api.main import app

    class BrokenLedger:
        def get_entry(self, account: str, draft_id: str):
            raise RuntimeError("ledger read failed")

    monkeypatch.setattr(ledger_mod, "_draft_ledger", BrokenLedger())
    with patch("api.routes.gmail.get_gmail_service", return_value=mock_gmail_service):
        client = TestClient(app)
        with caplog.at_level("ERROR"):
            send = client.post("/api/gmail/send", json={"draft_id": "draft123"})

    assert send.status_code == 409
    assert "safety ledger" in send.json()["detail"]
    assert "ledger read failed" in caplog.text
    mock_gmail_service.send_draft.assert_not_called()


def test_draft_ledger_prunes_and_initializes_idempotently(tmp_path):
    db_path = str(tmp_path / "gmail_draft_ledger.db")
    ledger = GmailDraftLedger(db_path)
    GmailDraftLedger(db_path)

    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    ledger.record_created(
        account="personal",
        draft_id="old",
        created_at=now - timedelta(seconds=301),
    )
    ledger.record_created(
        account="personal",
        draft_id="fresh",
        created_at=now - timedelta(seconds=299),
    )

    assert ledger.prune(window_seconds=300, now=now) == 1
    assert ledger.get_entry("personal", "old") is None
    assert ledger.get_entry("personal", "fresh") is not None


async def test_in_process_tool_send_is_gated_by_shared_ledger_for_http_created_draft(
    draft_ledger,
):
    """A draft created via the HTTP route must also be refused by the
    in-process send_email_draft tool in a brand new turn.

    Before the fix, send_email_draft called GmailService.send_draft()
    directly with no ledger check at all — only the in-memory per-turn set
    stood in the way, and a fresh turn's set is empty. This is exactly the
    cross-caller bypass #588 was filed about, just relocated to the
    in-process tool instead of the HTTP route.
    """
    from unittest.mock import MagicMock

    from api.services.agent_tools import _tool_send_email_draft, begin_email_send_turn

    draft_ledger.record_created(account="personal", draft_id="http-draft-1")

    mock = MagicMock()
    mock.send_draft.return_value = "sent-1"
    begin_email_send_turn()  # a brand new turn — no memory of "http-draft-1"
    with patch("api.services.gmail.GmailService", return_value=mock):
        result = await _tool_send_email_draft({"draft_id": "http-draft-1"})

    assert result.startswith("Error")
    assert "confirmation" in result
    mock.send_draft.assert_not_called()


async def test_in_process_tool_create_records_to_shared_ledger_for_http_send_gate(
    draft_ledger,
):
    """A draft created via the in-process create_email_draft tool must be
    visible to the HTTP /api/gmail/send route's gate.

    Before the fix, create_email_draft never wrote to the shared ledger, so
    the HTTP route saw an unrelated hand-composed draft and sent it freely —
    the other half of the same cross-caller bypass (#588).
    """
    from unittest.mock import MagicMock

    from api.main import app
    from api.services.agent_tools import _tool_create_email_draft, begin_email_send_turn

    mock = MagicMock()
    mock.create_draft.return_value = DraftMessage(
        draft_id="in-process-draft-1",
        message_id="m1",
        subject="test",
        to="recipient@example.com",
        body="test body",
        source_account="personal",
    )
    begin_email_send_turn()
    with patch("api.services.gmail.GmailService", return_value=mock):
        create_result = await _tool_create_email_draft(
            {"to": "recipient@example.com", "subject": "test", "body": "test body"}
        )
    assert "in-process-draft-1" in create_result

    mock_http_gmail = MagicMock()
    mock_http_gmail.send_draft.return_value = "sent-http-1"
    with patch("api.routes.gmail.get_gmail_service", return_value=mock_http_gmail):
        client = TestClient(app)
        send = client.post("/api/gmail/send", json={"draft_id": "in-process-draft-1"})

    assert send.status_code == 409
    mock_http_gmail.send_draft.assert_not_called()


def test_prune_never_evicts_turn_tagged_row_by_age(tmp_path):
    """Turn-tagged entries must survive prune() regardless of age.

    The same-turn-id guarantee promises a refusal "regardless of elapsed
    time" (#588's acceptance criteria). A naive time-based prune would delete
    a turn-tagged row once it aged past the cooldown window, silently
    reopening the exact bypass this row exists to close: draft A created
    with turn id t1, pruned away 300s later, then a send of A with turn id
    t1 finds no entry and sends.
    """
    db_path = str(tmp_path / "gmail_draft_ledger.db")
    ledger = GmailDraftLedger(db_path)

    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    ledger.record_created(
        account="personal",
        draft_id="turn-tagged-old",
        created_at=now - timedelta(seconds=600),
        turn_id="t1",
    )

    ledger.prune(window_seconds=300, now=now)

    entry = ledger.get_entry("personal", "turn-tagged-old")
    assert entry is not None
    assert entry.turn_id == "t1"

    with pytest.raises(ledger_mod.GmailSendGateBlocked):
        ledger_mod.check_send_gate(
            account="personal", draft_id="turn-tagged-old", turn_id="t1"
        )


def test_prune_caps_turn_tagged_rows_by_count_oldest_first(tmp_path):
    """Turn-tagged rows are bounded by count, not age, so the ledger still
    can't grow without bound even though it never time-expires them."""
    db_path = str(tmp_path / "gmail_draft_ledger.db")
    ledger = GmailDraftLedger(db_path)

    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        # turn-tagged-0 is the oldest (created furthest in the past),
        # turn-tagged-4 the newest.
        ledger.record_created(
            account="personal",
            draft_id=f"turn-tagged-{i}",
            created_at=now - timedelta(seconds=4 - i),
            turn_id=f"t{i}",
        )

    ledger.prune(window_seconds=300, now=now, max_turn_tagged_rows=3)

    # The three most-recently-created rows (t4, t3, t2) survive; the two
    # oldest (t0, t1) were evicted first.
    assert ledger.get_entry("personal", "turn-tagged-0") is None
    assert ledger.get_entry("personal", "turn-tagged-1") is None
    assert ledger.get_entry("personal", "turn-tagged-2") is not None
    assert ledger.get_entry("personal", "turn-tagged-3") is not None
    assert ledger.get_entry("personal", "turn-tagged-4") is not None


def test_ledger_fails_closed_when_db_file_lost_but_marker_survives(
    tmp_path, monkeypatch
):
    """A missing ledger must fail closed, not silently fail open.

    GmailDraftLedger.__init__ recreates the DB with CREATE TABLE IF NOT
    EXISTS whenever the file is absent — including when the file was
    deleted out from under an existing deployment. Without a way to tell
    that apart from a genuine first run, a lost ledger looks identical to
    an empty one and every previously-tracked draft becomes "unknown" (and
    unknown drafts always send). The marker file survives the .db file
    going missing, so its presence is the signal that data was lost.
    """
    db_path = tmp_path / "gmail_draft_ledger.db"
    GmailDraftLedger(str(db_path))  # first run: creates the db + its marker
    db_path.unlink()  # simulate the ledger file vanishing

    lost_ledger = GmailDraftLedger(str(db_path))
    assert lost_ledger.freshly_initialized_at is not None
    assert lost_ledger.is_within_fresh_grace_period(window_seconds=300)

    monkeypatch.setattr(ledger_mod, "_draft_ledger", lost_ledger)
    monkeypatch.setattr(ledger_mod.settings, "gmail_draft_send_cooldown_seconds", 300)

    with pytest.raises(ledger_mod.GmailSendGateBlocked) as exc_info:
        ledger_mod.check_send_gate(
            account="personal", draft_id="never-seen-before", turn_id=None
        )
    assert exc_info.value.unavailable is True


def test_route_fails_closed_when_ledger_shows_data_loss(
    tmp_path, monkeypatch, mock_gmail_service, caplog
):
    """End-to-end: /api/gmail/send refuses a send when the ledger shows
    evidence of data loss, even for a draft_id it has never seen."""
    from api.main import app

    db_path = tmp_path / "gmail_draft_ledger.db"
    GmailDraftLedger(str(db_path))
    db_path.unlink()
    lost_ledger = GmailDraftLedger(str(db_path))

    monkeypatch.setattr(ledger_mod, "_draft_ledger", lost_ledger)
    monkeypatch.setattr(ledger_mod.settings, "gmail_draft_send_cooldown_seconds", 300)

    with patch("api.routes.gmail.get_gmail_service", return_value=mock_gmail_service):
        client = TestClient(app)
        with caplog.at_level("ERROR"):
            send = client.post("/api/gmail/send", json={"draft_id": "handwritten-draft"})

    assert send.status_code == 409
    assert "safety ledger" in send.json()["detail"]
    mock_gmail_service.send_draft.assert_not_called()


def test_fresh_install_with_no_prior_marker_is_not_restricted(tmp_path):
    """A genuine first run (no marker, because nothing ever ran here before)
    must not be treated as data loss — otherwise a brand-new deployment
    would refuse every send for one full cooldown window after every
    restart, forever, which is not what "fail closed" is supposed to cost."""
    db_path = tmp_path / "gmail_draft_ledger.db"
    ledger = GmailDraftLedger(str(db_path))

    assert ledger.freshly_initialized_at is None
    assert not ledger.is_within_fresh_grace_period(window_seconds=300)


def test_grace_period_expires_after_cooldown_window():
    """The fail-closed window after detected data loss is bounded: any draft
    that could have been silently lost was created before the ledger was
    recreated, so once one full cooldown window elapses, it would no longer
    be blocked even if it had been tracked perfectly."""
    ledger = GmailDraftLedger.__new__(GmailDraftLedger)
    ledger.freshly_initialized_at = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

    just_inside = datetime(2026, 8, 19, 12, 4, 59, tzinfo=timezone.utc)
    just_outside = datetime(2026, 8, 19, 12, 5, 1, tzinfo=timezone.utc)
    assert ledger.is_within_fresh_grace_period(window_seconds=300, now=just_inside)
    assert not ledger.is_within_fresh_grace_period(window_seconds=300, now=just_outside)


def test_mcp_tool_surface_refuses_draft_then_send_same_turn(
    mock_gmail_service,
    draft_ledger,
):
    import mcp_server
    from api.main import app

    class ClientAdapter:
        def __init__(self, test_client: TestClient):
            self.test_client = test_client

        def post(self, url: str, json: dict | None = None, headers: dict | None = None):
            path = url.removeprefix(mcp_server.API_BASE)
            return self.test_client.post(path, json=json, headers=headers)

    with patch("api.routes.gmail.get_gmail_service", return_value=mock_gmail_service):
        api_client = TestClient(app)
        server = mcp_server.LifeOSMCPServer.__new__(mcp_server.LifeOSMCPServer)
        server.client = ClientAdapter(api_client)
        server._result_cache = None

        draft = server._call_api(
            "lifeos_gmail_draft",
            {
                "to": "recipient@example.com",
                "subject": "Test Subject",
                "body": "Test body",
                "turn_id": "turn-1",
            },
        )
        send = server._call_api(
            "lifeos_gmail_send",
            {"draft_id": draft["draft_id"], "turn_id": "turn-1"},
        )

    assert "API error 409" in send["error"]
    mock_gmail_service.send_draft.assert_not_called()
