"""
Tests for Gmail Integration.
P3.3 Acceptance Criteria:
- Can search emails by keyword
- Can filter by sender
- Can filter by date range
- Returns email subject, sender, date, snippet
- Can fetch full email body when needed
- Rate limiting prevents quota errors
- "Did Kevin email about the budget" returns relevant emails
- Empty results return empty list, not error
"""
import pytest

# All tests in this file use mocks (unit tests)
pytestmark = pytest.mark.unit
from datetime import datetime, timezone  # noqa: E402
from unittest.mock import patch, MagicMock  # noqa: E402
import base64  # noqa: E402

from googleapiclient.errors import HttpError  # noqa: E402

from api.services.gmail import (  # noqa: E402
    GmailService,
    EmailMessage,
    DraftMessage,
    build_gmail_query,
)
from api.services.google_auth import GoogleAccount  # noqa: E402


class TestEmailMessage:
    """Test EmailMessage dataclass."""

    def test_creates_message_with_required_fields(self):
        """Should create message with all required fields."""
        msg = EmailMessage(
            message_id="abc123",
            thread_id="thread1",
            subject="Budget Review",
            sender="kevin@example.com",
            sender_name="Kevin",
            date=datetime.now(timezone.utc),
            snippet="Here's the budget...",
            source_account="personal"
        )
        assert msg.message_id == "abc123"
        assert msg.subject == "Budget Review"

    def test_message_to_dict(self):
        """Should convert message to dict."""
        msg = EmailMessage(
            message_id="abc123",
            thread_id="thread1",
            subject="Budget Review",
            sender="kevin@example.com",
            sender_name="Kevin",
            date=datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc),
            snippet="Here's the budget...",
            source_account="personal"
        )
        data = msg.to_dict()
        assert data["message_id"] == "abc123"
        assert data["source"] == "gmail"


class TestBuildGmailQuery:
    """Test Gmail query builder."""

    def test_builds_simple_keyword_query(self):
        """Should build simple keyword query."""
        query = build_gmail_query(keywords="budget")
        assert "budget" in query

    def test_builds_from_query(self):
        """Should build from: query."""
        query = build_gmail_query(from_email="kevin@example.com")
        assert "from:kevin@example.com" in query

    def test_builds_date_range_query(self):
        """Should build date range query."""
        query = build_gmail_query(
            after=datetime(2026, 1, 1),
            before=datetime(2026, 1, 31)
        )
        assert "after:" in query
        assert "before:" in query

    def test_combines_multiple_filters(self):
        """Should combine multiple filters."""
        query = build_gmail_query(
            keywords="budget",
            from_email="kevin@example.com",
            after=datetime(2026, 1, 1)
        )
        assert "budget" in query
        assert "from:" in query
        assert "after:" in query


class TestGmailService:
    """Test GmailService."""

    @pytest.fixture
    def mock_auth_service(self):
        """Create mock auth service."""
        mock = MagicMock()
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock.get_credentials.return_value = mock_creds
        return mock

    @pytest.fixture
    def gmail_service(self, mock_auth_service):
        """Create Gmail service with mock auth."""
        with patch('api.services.gmail.get_google_auth', return_value=mock_auth_service):
            with patch('api.services.gmail.build') as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                service = GmailService(account_type=GoogleAccount.PERSONAL)
                service._service = mock_service
                return service

    def test_searches_by_keyword(self, gmail_service):
        """Should search emails by keyword."""
        mock_messages = {
            "messages": [
                {"id": "msg1", "threadId": "thread1"}
            ]
        }
        mock_message_detail = {
            "id": "msg1",
            "threadId": "thread1",
            "snippet": "Budget review for Q1",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Budget Review"},
                    {"name": "From", "value": "Kevin <kevin@example.com>"},
                    {"name": "Date", "value": "Tue, 7 Jan 2026 10:00:00 -0800"},
                ]
            }
        }
        gmail_service._service.users().messages().list().execute.return_value = mock_messages
        gmail_service._service.users().messages().get().execute.return_value = mock_message_detail

        messages = gmail_service.search(keywords="budget")

        assert len(messages) >= 1
        gmail_service._service.users().messages().list.assert_called()

    def test_searches_by_sender(self, gmail_service):
        """Should filter by sender."""
        mock_messages = {"messages": [{"id": "msg1", "threadId": "thread1"}]}
        mock_detail = {
            "id": "msg1",
            "threadId": "thread1",
            "snippet": "Test email",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Test"},
                    {"name": "From", "value": "kevin@example.com"},
                    {"name": "Date", "value": "Tue, 7 Jan 2026 10:00:00 -0800"},
                ]
            }
        }
        gmail_service._service.users().messages().list().execute.return_value = mock_messages
        gmail_service._service.users().messages().get().execute.return_value = mock_detail

        gmail_service.search(from_email="kevin@example.com")

        # Should have called with from: in query
        call_args = gmail_service._service.users().messages().list.call_args
        assert "from:" in str(call_args)

    def test_returns_empty_list_for_no_results(self, gmail_service):
        """Should return empty list when no results."""
        gmail_service._service.users().messages().list().execute.return_value = {}

        messages = gmail_service.search(keywords="nonexistent12345")

        assert messages == []

    def test_fetches_email_body(self, gmail_service):
        """Should fetch full email body."""
        body_text = "This is the full email body content."
        encoded_body = base64.urlsafe_b64encode(body_text.encode()).decode()
        mock_detail = {
            "id": "msg1",
            "threadId": "thread1",
            "snippet": "This is the full...",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Test"},
                    {"name": "From", "value": "test@example.com"},
                    {"name": "Date", "value": "Tue, 7 Jan 2026 10:00:00 -0800"},
                ],
                "body": {"data": encoded_body}
            }
        }
        gmail_service._service.users().messages().get().execute.return_value = mock_detail

        message = gmail_service.get_message("msg1", include_body=True)

        assert message is not None
        assert message.body is not None

    def test_rate_limiting(self, gmail_service):
        """Should have rate limiting configured."""
        # Rate limit should be set
        assert hasattr(gmail_service, 'rate_limit_delay')
        assert gmail_service.rate_limit_delay >= 0


class TestGetMessagesBatch:
    """
    Batched message fetching.

    Fetching one at a time cost ~42 min per nightly run on a busy mailbox,
    dominated by the per-call rate-limit sleep (#552).
    """

    @pytest.fixture
    def mock_auth_service(self):
        mock = MagicMock()
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock.get_credentials.return_value = mock_creds
        return mock

    @pytest.fixture
    def gmail_service(self, mock_auth_service):
        with patch('api.services.gmail.get_google_auth', return_value=mock_auth_service):
            with patch('api.services.gmail.build') as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                service = GmailService(account_type=GoogleAccount.PERSONAL)
                service._service = mock_service
                return service

    @staticmethod
    def _raw(message_id):
        return {
            "id": message_id,
            "threadId": f"thread_{message_id}",
            "snippet": "Quarterly planning notes",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": f"Subject {message_id}"},
                    {"name": "From", "value": "Dana Reyes <dana@example.com>"},
                    {"name": "Date", "value": "Tue, 7 Jan 2026 10:00:00 -0800"},
                ]
            },
        }

    def _install_batch(self, gmail_service, fail_ids=()):
        """Make new_batch_http_request() drive callbacks like the real client."""
        added = []

        class FakeBatch:
            def add(self, request, request_id=None, callback=None):
                added.append((request_id, callback))

            def execute(_self):
                for request_id, callback in added:
                    if request_id in fail_ids:
                        callback(request_id, None, Exception("boom"))
                    else:
                        callback(request_id, TestGetMessagesBatch._raw(request_id), None)
                added.clear()

        gmail_service._service.new_batch_http_request.side_effect = lambda: FakeBatch()
        return added

    def test_returns_all_requested_messages(self, gmail_service):
        """Every id resolves to a parsed message keyed by its id."""
        self._install_batch(gmail_service)
        ids = [f"msg{i}" for i in range(5)]

        result = gmail_service.get_messages_batch(ids)

        assert set(result) == set(ids)
        assert result["msg0"].sender == "dana@example.com"
        assert result["msg3"].subject == "Subject msg3"

    def test_uses_one_round_trip_per_batch_size(self, gmail_service):
        """120 messages at batch_size=50 is 3 round-trips, not 120."""
        self._install_batch(gmail_service)
        ids = [f"msg{i}" for i in range(120)]

        gmail_service.get_messages_batch(ids, batch_size=50)

        assert gmail_service._service.new_batch_http_request.call_count == 3

    def test_failed_message_falls_back_to_individual_fetch(self, gmail_service):
        """A per-message failure retries individually rather than being dropped."""
        self._install_batch(gmail_service, fail_ids={"msg1"})
        gmail_service._service.users().messages().get().execute.return_value = self._raw("msg1")

        result = gmail_service.get_messages_batch(["msg0", "msg1", "msg2"])

        assert set(result) == {"msg0", "msg1", "msg2"}

    def test_whole_batch_failure_falls_back(self, gmail_service):
        """If execute() raises, the chunk is retried individually, not lost."""
        class ExplodingBatch:
            def add(self, *a, **kw):
                pass

            def execute(self):
                raise Exception("transport died")

        gmail_service._service.new_batch_http_request.side_effect = lambda: ExplodingBatch()
        gmail_service._service.users().messages().get().execute.return_value = self._raw("msg0")

        result = gmail_service.get_messages_batch(["msg0"])

        assert "msg0" in result

    def test_empty_input_makes_no_requests(self, gmail_service):
        """No ids means no round-trips."""
        self._install_batch(gmail_service)

        assert gmail_service.get_messages_batch([]) == {}
        assert gmail_service._service.new_batch_http_request.call_count == 0

    @staticmethod
    def _quota_error():
        """An HttpError shaped like Gmail's quota-exhaustion response."""
        resp = MagicMock()
        resp.status = 403
        return HttpError(
            resp,
            b'{"error": {"message": "Quota exceeded for quota metric '
            b'\'Queries\'", "errors": [{"reason": "rateLimitExceeded"}]}}',
        )

    def test_rate_limited_messages_retry_as_a_batch_not_individually(self, gmail_service):
        """
        Quota errors back off and re-run the chunk.

        Falling back to individual fetches here is a thundering herd: the quota
        is already gone, so thousands of single requests make it worse. This is
        not hypothetical — it exhausted the live quota during development.
        """
        calls = {"n": 0}

        class FlakyBatch:
            def __init__(self):
                self.items = []

            def add(self, request, request_id=None, callback=None):
                self.items.append((request_id, callback))

            def execute(_self):
                calls["n"] += 1
                first_attempt = calls["n"] == 1
                for request_id, callback in _self.items:
                    if first_attempt:
                        callback(request_id, None, TestGetMessagesBatch._quota_error())
                    else:
                        callback(request_id, TestGetMessagesBatch._raw(request_id), None)

        gmail_service._service.new_batch_http_request.side_effect = lambda: FlakyBatch()

        with patch('api.services.gmail.time.sleep'):
            result = gmail_service.get_messages_batch(["msg0", "msg1"])

        assert set(result) == {"msg0", "msg1"}
        assert calls["n"] == 2, "should have retried as a batch"
        # The individual endpoint must not have been used for quota errors.
        gmail_service._service.users().messages().get().execute.assert_not_called()

    def test_gives_up_after_max_retries(self, gmail_service):
        """Persistent quota exhaustion stops rather than looping forever."""
        class AlwaysLimited:
            def add(self, request, request_id=None, callback=None):
                self._id, self._cb = request_id, callback

            def execute(_self):
                _self._cb(_self._id, None, TestGetMessagesBatch._quota_error())

        gmail_service._service.new_batch_http_request.side_effect = lambda: AlwaysLimited()

        with patch('api.services.gmail.time.sleep'):
            result = gmail_service.get_messages_batch(["msg0"], max_retries=2)

        assert result == {}
        assert gmail_service._service.new_batch_http_request.call_count == 3

    def test_non_quota_error_still_falls_back_individually(self, gmail_service):
        """A one-off failure is worth an individual retry; quota exhaustion isn't."""
        self._install_batch(gmail_service, fail_ids={"msg1"})
        gmail_service._service.users().messages().get().execute.return_value = self._raw("msg1")

        with patch('api.services.gmail.time.sleep'):
            result = gmail_service.get_messages_batch(["msg0", "msg1"])

        assert set(result) == {"msg0", "msg1"}

    def test_batches_are_paced(self, gmail_service):
        """Chunks are spaced out — the per-call sleep was what kept us under quota."""
        self._install_batch(gmail_service)

        with patch('api.services.gmail.time.sleep') as mock_sleep:
            gmail_service.get_messages_batch(
                [f"msg{i}" for i in range(4)], batch_size=2, seconds_per_message=0.5,
            )

        # Two chunks, each paced to batch_size * seconds_per_message = 1.0s.
        assert mock_sleep.call_count == 2
        assert all(0 < c.args[0] <= 1.0 for c in mock_sleep.call_args_list)

    def test_rate_limit_detection_ignores_plain_permission_errors(self, gmail_service):
        """A 403 that isn't about quota is a real error, not a signal to back off."""
        resp = MagicMock()
        resp.status = 403
        denied = HttpError(resp, b'{"error": {"message": "Insufficient permission"}}')

        assert gmail_service._is_rate_limit_error(denied) is False
        assert gmail_service._is_rate_limit_error(self._quota_error()) is True


class TestGmailAPI:
    """Test Gmail API endpoint."""

    @pytest.fixture
    def mock_gmail_service(self):
        """Create mock Gmail service."""
        mock = MagicMock()
        mock.search.return_value = [
            EmailMessage(
                message_id="1",
                thread_id="t1",
                subject="Budget Review",
                sender="kevin@example.com",
                sender_name="Kevin",
                date=datetime(2026, 1, 7, tzinfo=timezone.utc),
                snippet="Here's the budget...",
                source_account="personal",
            )
        ]
        return mock

    def test_search_endpoint_returns_results(self, mock_gmail_service):
        """Should return search results."""
        from fastapi.testclient import TestClient
        from api.main import app

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.get("/api/gmail/search?q=budget")

            assert response.status_code == 200
            data = response.json()
            assert "messages" in data


class TestDraftMessage:
    """Test DraftMessage dataclass."""

    def test_creates_draft_with_required_fields(self):
        """Should create draft with required fields."""
        draft = DraftMessage(
            draft_id="draft123",
            message_id="msg123",
            subject="Test Subject",
            to="recipient@example.com",
        )
        assert draft.draft_id == "draft123"
        assert draft.subject == "Test Subject"
        assert draft.to == "recipient@example.com"

    def test_draft_to_dict(self):
        """Should convert draft to dict."""
        draft = DraftMessage(
            draft_id="draft123",
            message_id="msg123",
            subject="Test Subject",
            to="recipient@example.com",
            body="Email body here",
            cc="cc@example.com",
            source_account="personal",
        )
        data = draft.to_dict()
        assert data["draft_id"] == "draft123"
        assert data["subject"] == "Test Subject"
        assert data["cc"] == "cc@example.com"
        assert data["source_account"] == "personal"


class TestGmailServiceDraft:
    """Test GmailService draft functionality."""

    @pytest.fixture
    def mock_auth_service(self):
        """Create mock auth service."""
        mock = MagicMock()
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock.get_credentials.return_value = mock_creds
        return mock

    @pytest.fixture
    def gmail_service(self, mock_auth_service):
        """Create Gmail service with mock auth."""
        with patch('api.services.gmail.get_google_auth', return_value=mock_auth_service):
            with patch('api.services.gmail.build') as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                service = GmailService(account_type=GoogleAccount.PERSONAL)
                service._service = mock_service
                return service

    def test_creates_draft(self, gmail_service):
        """Should create a draft email."""
        # Mock the drafts().create() response
        mock_response = {
            "id": "draft123",
            "message": {"id": "msg123"}
        }
        gmail_service._service.users().drafts().create().execute.return_value = mock_response

        draft = gmail_service.create_draft(
            to="recipient@example.com",
            subject="Test Subject",
            body="Test body content",
        )

        assert draft is not None
        assert draft.draft_id == "draft123"
        assert draft.message_id == "msg123"
        assert draft.subject == "Test Subject"
        assert draft.to == "recipient@example.com"
        gmail_service._service.users().drafts().create.assert_called()

    def test_creates_draft_with_cc_bcc(self, gmail_service):
        """Should create draft with CC and BCC."""
        mock_response = {
            "id": "draft456",
            "message": {"id": "msg456"}
        }
        gmail_service._service.users().drafts().create().execute.return_value = mock_response

        draft = gmail_service.create_draft(
            to="recipient@example.com",
            subject="Test Subject",
            body="Test body",
            cc="cc@example.com",
            bcc="bcc@example.com",
        )

        assert draft is not None
        assert draft.cc == "cc@example.com"
        assert draft.bcc == "bcc@example.com"

    def test_create_draft_handles_error(self, gmail_service):
        """Should return None on error."""
        gmail_service._service.users().drafts().create().execute.side_effect = Exception("API Error")

        draft = gmail_service.create_draft(
            to="recipient@example.com",
            subject="Test",
            body="Body",
        )

        assert draft is None

    def test_sends_draft(self, gmail_service):
        """Should send an existing draft by ID and return the message ID."""
        gmail_service._service.users().drafts().send().execute.return_value = {"id": "sent-msg-1"}

        message_id = gmail_service.send_draft("draft123")

        assert message_id == "sent-msg-1"
        gmail_service._service.users().drafts().send.assert_called()

    def test_send_draft_handles_error(self, gmail_service):
        """Should return None when the send fails."""
        gmail_service._service.users().drafts().send().execute.side_effect = Exception("API Error")

        message_id = gmail_service.send_draft("draft123")

        assert message_id is None


class TestGmailDraftAPI:
    """Test Gmail draft API endpoint."""

    @pytest.fixture
    def mock_gmail_service(self, tmp_path, monkeypatch):
        """Create mock Gmail service."""
        from api.services import gmail_draft_ledger as ledger_mod
        from api.services.gmail_draft_ledger import GmailDraftLedger

        # Patch the singleton itself so both the create-side route and the
        # shared check_send_gate() helper (called from the send route) see
        # the same throwaway ledger instead of the real one on disk.
        ledger = GmailDraftLedger(str(tmp_path / "gmail_draft_ledger.db"))
        monkeypatch.setattr(ledger_mod, "_draft_ledger", ledger)
        mock = MagicMock()
        mock.create_draft.return_value = DraftMessage(
            draft_id="draft123",
            message_id="msg123",
            subject="Test Subject",
            to="recipient@example.com",
            body="Test body",
            source_account="personal",
        )
        return mock

    def test_create_draft_endpoint(self, mock_gmail_service):
        """Should create draft via API endpoint."""
        from fastapi.testclient import TestClient
        from api.main import app

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post(
                "/api/gmail/drafts",
                json={
                    "to": "recipient@example.com",
                    "subject": "Test Subject",
                    "body": "Test body content",
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data["draft_id"] == "draft123"
            assert data["subject"] == "Test Subject"
            assert "gmail_url" in data
            assert "drafts" in data["gmail_url"]

    def test_create_draft_requires_to(self, mock_gmail_service):
        """Should require 'to' field."""
        from fastapi.testclient import TestClient
        from api.main import app

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post(
                "/api/gmail/drafts",
                json={
                    "to": "",
                    "subject": "Test",
                    "body": "Body",
                }
            )

            assert response.status_code == 400

    def test_create_draft_requires_subject(self, mock_gmail_service):
        """Should require 'subject' field."""
        from fastapi.testclient import TestClient
        from api.main import app

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post(
                "/api/gmail/drafts",
                json={
                    "to": "test@example.com",
                    "subject": "",
                    "body": "Body",
                }
            )

            assert response.status_code == 400

    def test_create_draft_requires_body(self, mock_gmail_service):
        """Should require 'body' field."""
        from fastapi.testclient import TestClient
        from api.main import app

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post(
                "/api/gmail/drafts",
                json={
                    "to": "test@example.com",
                    "subject": "Test",
                    "body": "",
                }
            )

            assert response.status_code == 400

    def test_create_draft_with_account_selection(self, mock_gmail_service):
        """Should accept account parameter."""
        from fastapi.testclient import TestClient
        from api.main import app

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post(
                "/api/gmail/drafts?account=work",
                json={
                    "to": "recipient@example.com",
                    "subject": "Work email",
                    "body": "Body content",
                }
            )

            assert response.status_code == 200

    def test_send_draft_endpoint(self, mock_gmail_service):
        """Should send a draft via the send endpoint."""
        from fastapi.testclient import TestClient
        from api.main import app

        mock_gmail_service.send_draft.return_value = "sent-msg-1"

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post(
                "/api/gmail/send",
                json={"draft_id": "draft123"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["message_id"] == "sent-msg-1"
            mock_gmail_service.send_draft.assert_called_once_with("draft123")

    def test_send_draft_requires_draft_id(self, mock_gmail_service):
        """Should reject a send with a missing/blank draft_id."""
        from fastapi.testclient import TestClient
        from api.main import app

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post("/api/gmail/send", json={"draft_id": "   "})

            assert response.status_code == 400

    def test_send_draft_failure_returns_500(self, mock_gmail_service):
        """Should return 500 when the underlying send fails."""
        from fastapi.testclient import TestClient
        from api.main import app

        mock_gmail_service.send_draft.return_value = None

        with patch('api.routes.gmail.get_gmail_service', return_value=mock_gmail_service):
            client = TestClient(app)
            response = client.post("/api/gmail/send", json={"draft_id": "draft123"})

            assert response.status_code == 500
