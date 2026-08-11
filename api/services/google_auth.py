"""
Google OAuth authentication service for LifeOS.

Handles OAuth 2.0 flow for both personal and work Google accounts
with separate credentials and token storage.
"""
import logging
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# Scopes for personal account (read-write)
SCOPES_PERSONAL = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    # Read+write to Google Sheets — used by the fitness Sheet mirror (#321) to
    # write rows. Google has no per-file write scope, so this grants write to
    # ALL spreadsheets the account can reach (acceptable for a single-user,
    # self-hosted deployment). Bumped from spreadsheets.readonly; requires
    # re-running the OAuth flow to re-consent.
    "https://www.googleapis.com/auth/spreadsheets",
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
    WORK2 = "work2"


def resolve_account(account: str) -> GoogleAccount:
    """Resolve account string to GoogleAccount enum."""
    try:
        return GoogleAccount(account)
    except ValueError:
        return GoogleAccount.PERSONAL


def get_configured_accounts() -> list[GoogleAccount]:
    """Return list of accounts that have credentials configured."""
    accounts = [GoogleAccount.PERSONAL]
    config_dir = Path("./config")
    for account in [GoogleAccount.WORK, GoogleAccount.WORK2]:
        if (config_dir / f"credentials-{account.value}.json").exists():
            accounts.append(account)
    return accounts


# Refresh-failure categories, mirroring _google_failure_kind/_failure_causes in
# agent_tools.py: classify what actually happened before naming a remedy,
# because a connectivity failure and a genuine rejection call for opposite
# actions — retry vs. re-authenticate — and naming the wrong one sends the
# operator to fix something that isn't broken (and costs an interactive
# browser session on a headless box for nothing).
_REFRESH_FAIL_CONNECTIVITY = "connectivity"
_REFRESH_FAIL_REJECTED = "rejected"
_REFRESH_FAIL_UNKNOWN = "unknown"

_AUTHENTICATE_SCRIPT = "~/.venvs/lifeos/bin/python scripts/authenticate_google.py"


def _classify_refresh_failure(exc: Exception) -> str:
    """Classify a `Credentials.refresh()` exception before naming a remedy.

    Never derives anything from the exception's text — it can carry tokens —
    only its type is inspected.

    - `google.auth.exceptions.TransportError` is what
      `google.auth.transport.requests.Request.__call__` raises for *any*
      `requests.exceptions.RequestException` it catches — connection refused,
      timeout, and DNS/name-resolution failure alike (`requests`/`urllib3`
      raise a `ConnectionError` wrapping a `NameResolutionError` for the
      latter; none of these are HTTP-level distinctions). None of those cases
      ever reached Google, so the credentials were never evaluated.
    - `google.auth.exceptions.RefreshError` is what
      `google.oauth2._client._handle_error_response` raises when the token
      endpoint itself returns a non-200 response — a genuine rejection by
      the identity provider (e.g. a revoked or expired refresh token).
    - Anything else is unrecognised; say the refresh failed without
      asserting which of the above happened.
    """
    if isinstance(exc, google_auth_exceptions.TransportError):
        return _REFRESH_FAIL_CONNECTIVITY
    if isinstance(exc, google_auth_exceptions.RefreshError):
        return _REFRESH_FAIL_REJECTED
    return _REFRESH_FAIL_UNKNOWN


def _headless_auth_failure_message(account: str, refresh_failure_kind: Optional[str]) -> str:
    """Name the failing account and the remedy the failure actually establishes.

    `refresh_failure_kind` is None when no refresh was attempted (no stored
    token, or an expired token with no refresh_token) — that case keeps its
    original wording since it isn't the misattribution this classifies.
    """
    if refresh_failure_kind == _REFRESH_FAIL_CONNECTIVITY:
        return (
            f"Could not reach Google to refresh the OAuth token for the "
            f"{account} account — this looks like a network or DNS problem, "
            f"not a credentials problem. The token itself is untested, not "
            f"known to be invalid. Re-authenticating will not help; retry "
            f"once connectivity is restored."
        )
    if refresh_failure_kind == _REFRESH_FAIL_REJECTED:
        return (
            f"Google rejected the OAuth token refresh for the {account} "
            f"account and it cannot be refreshed in a headless environment. "
            f"Run interactively: {_AUTHENTICATE_SCRIPT}"
        )
    if refresh_failure_kind == _REFRESH_FAIL_UNKNOWN:
        return (
            f"Token refresh for the {account} account failed and cannot be "
            f"retried in a headless environment. The cause is not "
            f"established — this may or may not be a credentials problem."
        )
    return (
        f"Google OAuth token for {account} account is expired/revoked "
        f"and cannot be refreshed in a headless environment. "
        f"Run interactively: {_AUTHENTICATE_SCRIPT}"
    )


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
        self.scopes = SCOPES_PERSONAL if account_type == GoogleAccount.PERSONAL else SCOPES_WORK  # WORK2 uses work scopes
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
        refresh_failure_kind: Optional[str] = None
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
                    refresh_failure_kind = _classify_refresh_failure(e)
                    if refresh_failure_kind == _REFRESH_FAIL_CONNECTIVITY:
                        logger.warning(
                            f"Token refresh for {self.account_type.value} account could not "
                            f"reach Google (connectivity failure, token not evaluated): {e}"
                        )
                    elif refresh_failure_kind == _REFRESH_FAIL_REJECTED:
                        logger.warning(
                            f"Token refresh for {self.account_type.value} account was rejected "
                            f"by Google: {e}"
                        )
                    else:
                        logger.warning(
                            f"Token refresh for {self.account_type.value} account failed: {e}"
                        )
                    # Fall through to re-authenticate

        # Need to authenticate via browser — fail fast if headless
        if os.environ.get("LIFEOS_HEADLESS", "").lower() in ("1", "true", "yes") or not sys.stdin.isatty():
            raise RuntimeError(
                _headless_auth_failure_message(self.account_type.value, refresh_failure_kind)
            )

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
    Authenticate all configured accounts (personal, work, work2).

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
