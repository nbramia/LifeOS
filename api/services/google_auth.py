"""
Google OAuth authentication service for LifeOS.

Handles OAuth 2.0 flow for both personal and work Google accounts
with separate credentials and token storage.
"""
import json
import logging
from enum import Enum
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# Scopes for personal account (read-write)
SCOPES_PERSONAL = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# Scopes for work account (read-only except gmail which needs modify for drafts)
SCOPES_WORK = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",  # Need modify for drafts
    "https://www.googleapis.com/auth/drive.readonly",
]


class GoogleAccount(Enum):
    """Google account types."""
    PERSONAL = "personal"
    WORK = "work"


class GoogleAuthService:
    """
    Google OAuth authentication service.

    Handles:
    - Loading credentials from file
    - Browser-based OAuth flow
    - Token storage and retrieval
    - Automatic token refresh
    - Re-authentication on token revocation
    """

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        account_type: GoogleAccount = GoogleAccount.PERSONAL
    ):
        """
        Initialize Google Auth service.

        Args:
            credentials_path: Path to OAuth credentials JSON file
            token_path: Path to store/load token JSON file
            account_type: Type of account (personal or work)
        """
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.account_type = account_type
        self.scopes = SCOPES_PERSONAL if account_type == GoogleAccount.PERSONAL else SCOPES_WORK
        self._credentials: Optional[Credentials] = None

    def get_credentials(self) -> Credentials:
        """
        Get valid Google credentials.

        Will:
        1. Load existing token if available
        2. Refresh token if expired
        3. Initiate OAuth flow if no valid token

        Returns:
            Valid Google credentials

        Raises:
            FileNotFoundError: If credentials file doesn't exist
        """
        # Check credentials file exists
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Google credentials file not found at {self.credentials_path}. "
                f"Please download OAuth credentials from Google Cloud Console."
            )

        # Try to load existing token
        if self.token_path.exists():
            try:
                self._credentials = Credentials.from_authorized_user_file(
                    str(self.token_path),
                    self.scopes
                )
            except Exception as e:
                logger.warning(f"Failed to load existing token: {e}")
                self._credentials = None

        # Check if we need to refresh or re-authenticate
        if self._credentials:
            if self._credentials.valid:
                return self._credentials

            if self._credentials.expired and self._credentials.refresh_token:
                try:
                    logger.info(f"Refreshing expired token for {self.account_type.value} account")
                    self._credentials.refresh(Request())
                    self._save_token(self._credentials)
                    return self._credentials
                except Exception as e:
                    logger.warning(f"Token refresh failed (may be revoked): {e}")
                    # Fall through to re-authenticate

        # Need to authenticate via browser
        logger.info(f"Initiating OAuth flow for {self.account_type.value} account")
        self._credentials = self._run_oauth_flow()
        self._save_token(self._credentials)
        return self._credentials

    def _run_oauth_flow(self) -> Credentials:
        """
        Run the browser-based OAuth flow.

        Returns:
            New credentials from OAuth flow
        """
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            self.scopes
        )

        # Run local server for OAuth callback
        credentials = flow.run_local_server(
            port=0,  # Use any available port
            prompt="consent",  # Always show consent screen
            access_type="offline"  # Get refresh token
        )

        return credentials

    def _save_token(self, credentials: Credentials) -> None:
        """
        Save credentials to token file.

        Args:
            credentials: Google credentials to save
        """
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json())
        logger.info(f"Saved token to {self.token_path}")

    def revoke_token(self) -> bool:
        """
        Revoke the current token and delete local token file.

        Returns:
            True if successful, False otherwise
        """
        if self.token_path.exists():
            self.token_path.unlink()
            logger.info(f"Deleted token file {self.token_path}")

        self._credentials = None
        return True

    @property
    def is_authenticated(self) -> bool:
        """Check if we have valid credentials."""
        if not self.token_path.exists():
            return False

        try:
            creds = Credentials.from_authorized_user_file(
                str(self.token_path),
                self.scopes
            )
            return creds.valid or (creds.expired and creds.refresh_token)
        except Exception:
            return False


# Singleton instances for each account
_auth_services: dict[GoogleAccount, GoogleAuthService] = {}


def get_google_auth(
    account_type: GoogleAccount,
    credentials_path: Optional[str] = None,
    token_path: Optional[str] = None
) -> GoogleAuthService:
    """
    Get or create Google auth service for an account type.

    Args:
        account_type: Personal or work account
        credentials_path: Override default credentials path
        token_path: Override default token path

    Returns:
        GoogleAuthService instance
    """
    if account_type not in _auth_services:
        # Default paths
        config_dir = Path("./config")
        if credentials_path is None:
            credentials_path = str(config_dir / f"credentials-{account_type.value}.json")
        if token_path is None:
            token_path = str(config_dir / f"token-{account_type.value}.json")

        _auth_services[account_type] = GoogleAuthService(
            credentials_path=credentials_path,
            token_path=token_path,
            account_type=account_type
        )

    return _auth_services[account_type]


def authenticate_all_accounts() -> dict[str, bool]:
    """
    Authenticate both personal and work accounts.

    This will open browser windows for any accounts that need authentication.

    Returns:
        Dict mapping account type to success status
    """
    results = {}

    for account_type in GoogleAccount:
        try:
            auth = get_google_auth(account_type)
            auth.get_credentials()
            results[account_type.value] = True
            logger.info(f"Successfully authenticated {account_type.value} account")
        except Exception as e:
            results[account_type.value] = False
            logger.error(f"Failed to authenticate {account_type.value} account: {e}")

    return results
