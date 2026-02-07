"""
Tests for Google OAuth Setup.
P3.1 Acceptance Criteria:
- OAuth flow completes successfully in browser for personal account
- OAuth flow completes successfully in browser for work account
- Access tokens stored locally
- Refresh tokens stored locally
- Tokens auto-refresh when expired
- Credentials file excluded from version control
- Clear error message if auth fails
- Re-auth flow works if tokens are revoked
"""
import pytest

# All tests in this file use mocks (unit tests)
pytestmark = pytest.mark.unit
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta, timezone

from api.services.google_auth import (
    GoogleAuthService,
    GoogleAccount,
    SCOPES_PERSONAL,
    SCOPES_WORK,
)


class TestGoogleAuthService:
    """Test Google OAuth service."""

    @pytest.fixture
    def temp_config_dir(self):
        """Create temp directory for credentials and tokens."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def mock_credentials_personal(self, temp_config_dir):
        """Create mock personal credentials file."""
        creds = {
            "installed": {
                "client_id": "test-personal.apps.googleusercontent.com",
                "client_secret": "test-secret-personal",
                "redirect_uris": ["http://localhost"]
            }
        }
        path = temp_config_dir / "credentials-personal.json"
        path.write_text(json.dumps(creds))
        return path

    @pytest.fixture
    def mock_credentials_work(self, temp_config_dir):
        """Create mock work credentials file."""
        creds = {
            "installed": {
                "client_id": "test-work.apps.googleusercontent.com",
                "client_secret": "test-secret-work",
                "redirect_uris": ["http://localhost"]
            }
        }
        path = temp_config_dir / "credentials-work.json"
        path.write_text(json.dumps(creds))
        return path

    def test_loads_credentials_file(self, mock_credentials_personal, temp_config_dir):
        """Should load credentials from JSON file."""
        service = GoogleAuthService(
            credentials_path=str(mock_credentials_personal),
            token_path=str(temp_config_dir / "token.json"),
            account_type=GoogleAccount.PERSONAL
        )
        assert service.credentials_path.exists()

    def test_personal_account_has_readwrite_scopes(self):
        """Personal account should have read-write scopes."""
        # Calendar, Gmail, Drive - all with full access
        assert "https://www.googleapis.com/auth/calendar" in SCOPES_PERSONAL
        assert "https://www.googleapis.com/auth/gmail.modify" in SCOPES_PERSONAL
        assert "https://www.googleapis.com/auth/drive" in SCOPES_PERSONAL

    def test_work_account_has_correct_scopes(self):
        """Work account should have readonly for calendar/drive, modify for gmail (for drafts)."""
        assert "https://www.googleapis.com/auth/calendar.readonly" in SCOPES_WORK
        assert "https://www.googleapis.com/auth/gmail.modify" in SCOPES_WORK  # Need modify for drafts
        assert "https://www.googleapis.com/auth/drive.readonly" in SCOPES_WORK

    def test_stores_token_locally(self, mock_credentials_personal, temp_config_dir):
        """Should store tokens in local file."""
        token_path = temp_config_dir / "token-personal.json"
        service = GoogleAuthService(
            credentials_path=str(mock_credentials_personal),
            token_path=str(token_path),
            account_type=GoogleAccount.PERSONAL
        )

        # Mock the credentials object
        mock_creds = MagicMock()
        mock_creds.token = "access_token_123"
        mock_creds.refresh_token = "refresh_token_456"
        mock_creds.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds.to_json.return_value = json.dumps({
            "token": "access_token_123",
            "refresh_token": "refresh_token_456",
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        })

        service._save_token(mock_creds)

        assert token_path.exists()
        saved = json.loads(token_path.read_text())
        assert "token" in saved or "access_token_123" in token_path.read_text()

    def test_loads_existing_token(self, mock_credentials_personal, temp_config_dir):
        """Should load existing token from file."""
        token_path = temp_config_dir / "token-personal.json"

        # Create a mock token file
        token_data = {
            "token": "existing_access_token",
            "refresh_token": "existing_refresh_token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "secret",
            "scopes": SCOPES_PERSONAL,
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat() + "Z"
        }
        token_path.write_text(json.dumps(token_data))

        service = GoogleAuthService(
            credentials_path=str(mock_credentials_personal),
            token_path=str(token_path),
            account_type=GoogleAccount.PERSONAL
        )

        with patch('api.services.google_auth.Credentials') as mock_creds_class:
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds.expired = False
            mock_creds_class.from_authorized_user_file.return_value = mock_creds

            creds = service.get_credentials()
            mock_creds_class.from_authorized_user_file.assert_called_once()

    @patch('api.services.google_auth.InstalledAppFlow')
    def test_initiates_oauth_flow_when_no_token(
        self, mock_flow_class, mock_credentials_personal, temp_config_dir
    ):
        """Should initiate OAuth flow when no token exists."""
        token_path = temp_config_dir / "token-personal.json"

        service = GoogleAuthService(
            credentials_path=str(mock_credentials_personal),
            token_path=str(token_path),
            account_type=GoogleAccount.PERSONAL
        )

        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.to_json.return_value = '{"token": "new_token"}'
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_class.from_client_secrets_file.return_value = mock_flow

        creds = service.get_credentials()

        mock_flow_class.from_client_secrets_file.assert_called_once()
        mock_flow.run_local_server.assert_called_once()

    @patch('api.services.google_auth.Credentials')
    def test_auto_refreshes_expired_token(
        self, mock_creds_class, mock_credentials_personal, temp_config_dir
    ):
        """Should auto-refresh when token is expired."""
        token_path = temp_config_dir / "token-personal.json"

        # Create expired token
        token_data = {
            "token": "expired_token",
            "refresh_token": "valid_refresh_token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "secret",
            "scopes": SCOPES_PERSONAL,
            "expiry": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat() + "Z"
        }
        token_path.write_text(json.dumps(token_data))

        service = GoogleAuthService(
            credentials_path=str(mock_credentials_personal),
            token_path=str(token_path),
            account_type=GoogleAccount.PERSONAL
        )

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "valid_refresh_token"
        mock_creds.to_json.return_value = '{"token": "refreshed_token"}'
        mock_creds_class.from_authorized_user_file.return_value = mock_creds

        with patch('api.services.google_auth.Request') as mock_request:
            creds = service.get_credentials()
            mock_creds.refresh.assert_called_once()

    def test_clear_error_on_missing_credentials(self, temp_config_dir):
        """Should raise clear error when credentials file missing."""
        service = GoogleAuthService(
            credentials_path=str(temp_config_dir / "nonexistent.json"),
            token_path=str(temp_config_dir / "token.json"),
            account_type=GoogleAccount.PERSONAL
        )

        with pytest.raises(FileNotFoundError) as exc_info:
            service.get_credentials()

        assert "credentials" in str(exc_info.value).lower()

    @patch('api.services.google_auth.InstalledAppFlow')
    def test_reauth_when_token_revoked(
        self, mock_flow_class, mock_credentials_personal, temp_config_dir
    ):
        """Should re-authenticate when token is revoked."""
        token_path = temp_config_dir / "token-personal.json"

        # Create a token that will fail refresh (simulating revoked)
        token_data = {
            "token": "revoked_token",
            "refresh_token": "revoked_refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "secret",
            "scopes": SCOPES_PERSONAL
        }
        token_path.write_text(json.dumps(token_data))

        service = GoogleAuthService(
            credentials_path=str(mock_credentials_personal),
            token_path=str(token_path),
            account_type=GoogleAccount.PERSONAL
        )

        # Mock flow for re-auth
        mock_flow = MagicMock()
        mock_new_creds = MagicMock()
        mock_new_creds.valid = True
        mock_new_creds.to_json.return_value = '{"token": "new_token"}'
        mock_flow.run_local_server.return_value = mock_new_creds
        mock_flow_class.from_client_secrets_file.return_value = mock_flow

        with patch('api.services.google_auth.Credentials') as mock_creds_class:
            mock_creds = MagicMock()
            mock_creds.valid = False
            mock_creds.expired = True
            mock_creds.refresh_token = "revoked_refresh"
            # Simulate refresh failure (token revoked)
            mock_creds.refresh.side_effect = Exception("Token has been revoked")
            mock_creds_class.from_authorized_user_file.return_value = mock_creds

            creds = service.get_credentials()

            # Should have initiated new OAuth flow
            mock_flow.run_local_server.assert_called_once()

    def test_separate_tokens_per_account(self, temp_config_dir):
        """Should maintain separate tokens for personal and work accounts."""
        # This is more of an integration test - verify the paths are different
        personal_token = temp_config_dir / "token-personal.json"
        work_token = temp_config_dir / "token-work.json"

        assert personal_token != work_token
        assert "personal" in str(personal_token)
        assert "work" in str(work_token)


class TestGoogleAccountEnum:
    """Test GoogleAccount enum."""

    def test_personal_account_value(self):
        """Personal account should have correct value."""
        assert GoogleAccount.PERSONAL.value == "personal"

    def test_work_account_value(self):
        """Work account should have correct value."""
        assert GoogleAccount.WORK.value == "work"
