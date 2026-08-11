"""
Tests for Google credential-refresh failure attribution (#540).

Regression context: a DNS failure reaching oauth2.googleapis.com during a
routine token refresh was caught by a bare `except Exception`, logged as
"Token refresh failed (may be revoked)", and then reported to a headless
operator as an expired/revoked token that needed an interactive re-auth run.
The token was valid the whole time — the network was down. This is the
top-line error on a sync job that fails most nights, so it's the first thing
an operator reads, and it pointed at the wrong fix (and would have cost an
interactive browser session on a headless box for nothing).

These tests pin the fix: a transport/DNS failure during refresh must be
reported as a connectivity problem (token untested, no re-auth suggested); a
genuine rejection by the identity provider must still say re-auth is needed
and name the script; an unrecognised failure must say the refresh failed
without asserting the token is invalid. The valid-credential path must be
unchanged, and no raw exception text (which can carry tokens) may reach the
raised message.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth import exceptions as google_auth_exceptions

from api.services.google_auth import (
    GoogleAccount,
    GoogleAuthService,
    _classify_refresh_failure,
    _headless_auth_failure_message,
    _REFRESH_FAIL_CONNECTIVITY,
    _REFRESH_FAIL_REJECTED,
    _REFRESH_FAIL_UNKNOWN,
)

pytestmark = pytest.mark.unit

# A synthetic refresh-token-shaped string. If this ever showed up in a raised
# message it would mean raw exception text leaked into operator-facing output.
_SYNTHETIC_SECRET = "1//synthetic-refresh-token-should-never-leak"


@pytest.fixture
def temp_config_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def credentials_file(temp_config_dir):
    """A synthetic OAuth client-secret file — never a real credential."""
    creds = {
        "installed": {
            "client_id": "synthetic-test.apps.googleusercontent.com",
            "client_secret": "synthetic-secret",
            "redirect_uris": ["http://localhost"],
        }
    }
    path = temp_config_dir / "credentials-personal.json"
    path.write_text(json.dumps(creds))
    return path


def _service(credentials_file, temp_config_dir, account=GoogleAccount.PERSONAL):
    """Build a service whose token file exists — get_credentials() only loads
    (and potentially refreshes) a token when `token_path.exists()`, so an
    absent file would skip the refresh attempt these tests exist to cover.
    The content is never actually parsed: `Credentials` itself is mocked.
    """
    token_path = temp_config_dir / f"token-{account.value}.json"
    token_path.write_text(json.dumps({"token": "synthetic-placeholder"}))
    return GoogleAuthService(
        credentials_path=str(credentials_file),
        token_path=str(token_path),
        account_type=account,
    )


def _expired_creds(refresh_side_effect):
    """Stand-in for an expired-but-refreshable Credentials object."""
    creds = MagicMock()
    creds.valid = False
    creds.expired = True
    creds.refresh_token = _SYNTHETIC_SECRET
    creds.refresh.side_effect = refresh_side_effect
    return creds


class TestClassifyRefreshFailure:
    """The classifier reads only the exception's type, never its text."""

    def test_transport_error_is_connectivity(self):
        exc = google_auth_exceptions.TransportError(_SYNTHETIC_SECRET)
        assert _classify_refresh_failure(exc) == _REFRESH_FAIL_CONNECTIVITY

    def test_transport_error_wrapping_dns_failure_is_connectivity(self):
        """A name-resolution failure never reaches Google. google-auth wraps every
        requests.exceptions.RequestException — a DNS-caused ConnectionError
        included — into the same TransportError type, so it lands in the same
        connectivity bucket as a plain connection-refused or timeout."""
        dns_cause = ConnectionError(
            "Synthetic NameResolutionError: nodename nor servname provided"
        )
        exc = google_auth_exceptions.TransportError(dns_cause)
        assert _classify_refresh_failure(exc) == _REFRESH_FAIL_CONNECTIVITY

    def test_refresh_error_is_rejected(self):
        """RefreshError is what the token endpoint itself raises on a non-200
        response — a genuine rejection, not a network problem."""
        exc = google_auth_exceptions.RefreshError(
            "invalid_grant: Token has been expired or revoked."
        )
        assert _classify_refresh_failure(exc) == _REFRESH_FAIL_REJECTED

    def test_unrecognised_exception_is_unknown(self):
        assert (
            _classify_refresh_failure(ValueError("synthetic odd failure"))
            == _REFRESH_FAIL_UNKNOWN
        )

    def test_refresh_error_with_a_chained_transport_cause_is_still_rejected(self):
        """Documents, rather than guards against, a scenario verified not to
        occur in the installed google-auth (2.48.0): every RefreshError raise
        site in google.oauth2._client originates from parsing the token
        endpoint's response body, never from catching a transport exception,
        so a network failure cannot surface disguised as a RefreshError. This
        pins what the classifier does today if that ever changed upstream —
        isinstance(TransportError) is checked first, but a RefreshError is not
        a TransportError subclass, so a __cause__ chain is never consulted and
        the exception classifies by its own type regardless of what caused
        it."""
        exc = google_auth_exceptions.RefreshError("invalid_grant")
        try:
            raise google_auth_exceptions.TransportError("synthetic transport failure")
        except google_auth_exceptions.TransportError as transport_exc:
            exc.__cause__ = transport_exc

        assert _classify_refresh_failure(exc) == _REFRESH_FAIL_REJECTED


class TestHeadlessFailureMessage:
    """The raised message names a cause and a remedy per category, never raw
    exception text, and always names the account."""

    def test_connectivity_names_account_and_does_not_suggest_reauth(self):
        msg = _headless_auth_failure_message("personal", _REFRESH_FAIL_CONNECTIVITY)
        assert "personal" in msg
        assert "authenticate_google.py" not in msg
        assert "expired" not in msg.lower()
        assert "revoked" not in msg.lower()

    def test_rejected_names_account_and_the_script(self):
        msg = _headless_auth_failure_message("work", _REFRESH_FAIL_REJECTED)
        assert "work" in msg
        assert "authenticate_google.py" in msg

    def test_unknown_does_not_assert_invalid_and_names_no_script(self):
        msg = _headless_auth_failure_message("work2", _REFRESH_FAIL_UNKNOWN)
        assert "work2" in msg
        assert "expired" not in msg.lower()
        assert "revoked" not in msg.lower()
        assert "authenticate_google.py" not in msg

    def test_no_refresh_attempted_keeps_original_wording(self):
        """None means no refresh was attempted at all (no token, or an expired
        token with no refresh_token) — a different situation than a refresh
        that ran and failed, so it keeps the pre-existing remedy."""
        msg = _headless_auth_failure_message("personal", None)
        assert "personal" in msg
        assert "authenticate_google.py" in msg


class TestGetCredentialsHeadlessAttribution:
    """End-to-end through GoogleAuthService.get_credentials() in a headless
    context, for each failure class in turn."""

    def _run(self, credentials_file, temp_config_dir, refresh_exc, account=GoogleAccount.PERSONAL, monkeypatch=None):
        service = _service(credentials_file, temp_config_dir, account)
        monkeypatch.setenv("LIFEOS_HEADLESS", "true")
        with patch("api.services.google_auth.Credentials") as mock_creds_class:
            mock_creds_class.from_authorized_user_file.return_value = _expired_creds(refresh_exc)
            with pytest.raises(RuntimeError) as exc_info:
                service.get_credentials()
        return str(exc_info.value)

    def test_dns_failure_reports_connectivity_not_revocation(
        self, credentials_file, temp_config_dir, monkeypatch
    ):
        dns_cause = ConnectionError("Synthetic NameResolutionError for oauth2.googleapis.com")
        exc = google_auth_exceptions.TransportError(dns_cause)
        msg = self._run(credentials_file, temp_config_dir, exc, monkeypatch=monkeypatch)

        assert "personal" in msg
        assert "expired" not in msg.lower()
        assert "revoked" not in msg.lower()
        assert "authenticate_google.py" not in msg
        assert _SYNTHETIC_SECRET not in msg

    def test_generic_transport_failure_reports_connectivity(
        self, credentials_file, temp_config_dir, monkeypatch
    ):
        """A plain connection-refused/timeout — not DNS-specific — lands in the
        same connectivity bucket with the same remedy."""
        exc = google_auth_exceptions.TransportError("Synthetic connection timed out")
        msg = self._run(credentials_file, temp_config_dir, exc, monkeypatch=monkeypatch)

        assert "personal" in msg
        assert "expired" not in msg.lower()
        assert "revoked" not in msg.lower()
        assert "authenticate_google.py" not in msg

    def test_provider_rejection_reports_reauth(
        self, credentials_file, temp_config_dir, monkeypatch
    ):
        exc = google_auth_exceptions.RefreshError(
            "invalid_grant: Token has been expired or revoked."
        )
        msg = self._run(
            credentials_file, temp_config_dir, exc,
            account=GoogleAccount.WORK, monkeypatch=monkeypatch,
        )

        assert "work" in msg
        assert "authenticate_google.py" in msg
        assert _SYNTHETIC_SECRET not in msg

    def test_unexpected_failure_does_not_assert_revocation(
        self, credentials_file, temp_config_dir, monkeypatch
    ):
        exc = ValueError("synthetic unrecognised failure")
        msg = self._run(credentials_file, temp_config_dir, exc, monkeypatch=monkeypatch)

        assert "personal" in msg
        assert "expired" not in msg.lower()
        assert "revoked" not in msg.lower()
        assert _SYNTHETIC_SECRET not in msg

    def test_account_name_distinguishes_a_two_account_setup(
        self, credentials_file, temp_config_dir, monkeypatch
    ):
        """The account must appear in every message so a two-account setup
        identifies which one failed."""
        exc = google_auth_exceptions.TransportError("Synthetic DNS failure")
        personal_msg = self._run(credentials_file, temp_config_dir, exc, monkeypatch=monkeypatch)

        work_creds_file = temp_config_dir / "credentials-work.json"
        work_creds_file.write_text(credentials_file.read_text())
        work_msg = self._run(
            work_creds_file, temp_config_dir, exc,
            account=GoogleAccount.WORK, monkeypatch=monkeypatch,
        )

        assert "personal" in personal_msg and "work account" not in personal_msg
        assert "work account" in work_msg


class TestLogLinesDoNotLeakExceptionText:
    """The refresh-failure log lines must never interpolate str(e) — an OAuth
    refresh exception is precisely where a token or response payload can
    appear, and log_redaction.py only filters `bot\\d+:...` patterns (the
    Telegram-token leak it was built for), not an arbitrary OAuth payload.
    The classification and the exception's type name are the diagnostic
    signal; the payload never was.
    """

    def _run_and_capture(self, credentials_file, temp_config_dir, refresh_exc, caplog, monkeypatch):
        service = _service(credentials_file, temp_config_dir)
        monkeypatch.setenv("LIFEOS_HEADLESS", "true")
        with patch("api.services.google_auth.Credentials") as mock_creds_class:
            mock_creds_class.from_authorized_user_file.return_value = _expired_creds(refresh_exc)
            with caplog.at_level("WARNING", logger="api.services.google_auth"):
                with pytest.raises(RuntimeError):
                    service.get_credentials()
        return caplog.records

    def test_connectivity_failure_log_omits_exception_text(
        self, credentials_file, temp_config_dir, monkeypatch, caplog
    ):
        exc = google_auth_exceptions.TransportError(_SYNTHETIC_SECRET)
        records = self._run_and_capture(credentials_file, temp_config_dir, exc, caplog, monkeypatch)
        joined = "\n".join(r.getMessage() for r in records)

        assert _SYNTHETIC_SECRET not in joined
        assert "TransportError" in joined
        assert all(r.exc_info is None for r in records)

    def test_rejected_failure_log_omits_exception_text(
        self, credentials_file, temp_config_dir, monkeypatch, caplog
    ):
        exc = google_auth_exceptions.RefreshError(_SYNTHETIC_SECRET)
        records = self._run_and_capture(credentials_file, temp_config_dir, exc, caplog, monkeypatch)
        joined = "\n".join(r.getMessage() for r in records)

        assert _SYNTHETIC_SECRET not in joined
        assert "RefreshError" in joined
        assert all(r.exc_info is None for r in records)

    def test_unknown_failure_log_omits_exception_text(
        self, credentials_file, temp_config_dir, monkeypatch, caplog
    ):
        exc = ValueError(_SYNTHETIC_SECRET)
        records = self._run_and_capture(credentials_file, temp_config_dir, exc, caplog, monkeypatch)
        joined = "\n".join(r.getMessage() for r in records)

        assert _SYNTHETIC_SECRET not in joined
        assert "ValueError" in joined
        assert all(r.exc_info is None for r in records)


class TestValidCredentialPathUnchanged:
    """A valid, unexpired token must be returned untouched — no classification,
    no logging about refresh failures, no exception raised."""

    def test_valid_credentials_returned_without_refresh_attempt(
        self, credentials_file, temp_config_dir
    ):
        service = _service(credentials_file, temp_config_dir)

        with patch("api.services.google_auth.Credentials") as mock_creds_class:
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds.expired = False
            mock_creds_class.from_authorized_user_file.return_value = mock_creds

            result = service.get_credentials()

        assert result is mock_creds
        mock_creds.refresh.assert_not_called()

    def test_valid_credentials_returned_even_when_headless(
        self, credentials_file, temp_config_dir, monkeypatch
    ):
        monkeypatch.setenv("LIFEOS_HEADLESS", "true")
        service = _service(credentials_file, temp_config_dir)

        with patch("api.services.google_auth.Credentials") as mock_creds_class:
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds.expired = False
            mock_creds_class.from_authorized_user_file.return_value = mock_creds

            result = service.get_credentials()

        assert result is mock_creds

    def test_successful_refresh_returns_credentials_without_raising(
        self, credentials_file, temp_config_dir, monkeypatch
    ):
        """A refresh that succeeds outright must behave exactly as before —
        including in a headless context, since success never reaches the
        classifier."""
        monkeypatch.setenv("LIFEOS_HEADLESS", "true")
        service = _service(credentials_file, temp_config_dir)

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = _SYNTHETIC_SECRET
        mock_creds.to_json.return_value = json.dumps({"token": "synthetic-refreshed-token"})

        def _refresh(request):
            mock_creds.valid = True

        mock_creds.refresh.side_effect = _refresh

        with patch("api.services.google_auth.Credentials") as mock_creds_class:
            mock_creds_class.from_authorized_user_file.return_value = mock_creds
            result = service.get_credentials()

        assert result is mock_creds
        mock_creds.refresh.assert_called_once()
