"""
Slack integration service for LifeOS CRM.

Provides OAuth flow and conversation/user retrieval via Slack API.
Creates SourceEntity records for Slack users and interactions.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from api.services.source_entity import (
    SourceEntity,
    SourceEntityStore,
)

# Source type constant
SOURCE_SLACK = "slack"

logger = logging.getLogger(__name__)

# Slack OAuth configuration
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
SLACK_REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI", "http://localhost:8000/api/crm/slack/callback")

# Direct token configuration (preferred for personal use)
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "")
SLACK_TEAM_ID = os.getenv("SLACK_TEAM_ID", "")

# OAuth scopes required for CRM functionality
SLACK_SCOPES = [
    "users:read",           # Read user profiles
    "users:read.email",     # Read user emails
    "channels:read",        # List channels
    "channels:history",     # Read channel messages
    "groups:read",          # List private channels
    "groups:history",       # Read private channel messages
    "im:read",              # List DMs
    "im:history",           # Read DM messages
    "mpim:read",            # List group DMs
    "mpim:history",         # Read group DM messages
]

# Token storage path
SLACK_TOKEN_PATH = Path("data/slack_tokens.json")


@dataclass
class SlackUser:
    """Represents a Slack workspace user."""
    user_id: str
    username: str
    real_name: str
    display_name: str
    email: Optional[str] = None
    title: Optional[str] = None
    phone: Optional[str] = None
    image_url: Optional[str] = None
    is_bot: bool = False
    is_deleted: bool = False
    team_id: Optional[str] = None
    timezone: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dict for API response."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "real_name": self.real_name,
            "display_name": self.display_name,
            "email": self.email,
            "title": self.title,
            "phone": self.phone,
            "image_url": self.image_url,
            "is_bot": self.is_bot,
            "is_deleted": self.is_deleted,
            "team_id": self.team_id,
            "timezone": self.timezone,
        }


@dataclass
class SlackMessage:
    """Represents a Slack message."""
    ts: str  # Timestamp (unique ID)
    channel_id: str
    user_id: str
    text: str
    timestamp: datetime
    thread_ts: Optional[str] = None
    reply_count: int = 0
    reactions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dict for API response."""
        return {
            "ts": self.ts,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "text": self.text,
            "timestamp": self.timestamp.isoformat(),
            "thread_ts": self.thread_ts,
            "reply_count": self.reply_count,
            "reactions": self.reactions,
        }


@dataclass
class SlackChannel:
    """Represents a Slack channel."""
    channel_id: str
    name: str
    is_private: bool = False
    is_im: bool = False
    is_mpim: bool = False
    member_count: int = 0
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dict for API response."""
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "is_private": self.is_private,
            "is_im": self.is_im,
            "is_mpim": self.is_mpim,
            "member_count": self.member_count,
            "members": self.members,
        }


class SlackTokenStore:
    """Manages Slack OAuth tokens."""

    def __init__(self, path: Path = SLACK_TOKEN_PATH):
        self.path = path
        self._tokens: dict[str, dict] = {}
        self._load()

    def _load(self):
        """Load tokens from disk."""
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self._tokens = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load Slack tokens: {e}")
                self._tokens = {}

    def _save(self):
        """Save tokens to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._tokens, f, indent=2)

    def get_token(self, workspace_id: str = "default") -> Optional[str]:
        """Get access token for workspace."""
        token_data = self._tokens.get(workspace_id)
        if token_data:
            return token_data.get("access_token")
        return None

    def set_token(
        self,
        access_token: str,
        workspace_id: str = "default",
        team_name: Optional[str] = None,
        authed_user_id: Optional[str] = None,
    ):
        """Store access token for workspace."""
        self._tokens[workspace_id] = {
            "access_token": access_token,
            "team_name": team_name,
            "authed_user_id": authed_user_id,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def remove_token(self, workspace_id: str = "default"):
        """Remove token for workspace."""
        if workspace_id in self._tokens:
            del self._tokens[workspace_id]
            self._save()

    def list_workspaces(self) -> list[dict]:
        """List all connected workspaces."""
        return [
            {"workspace_id": wid, "team_name": data.get("team_name")}
            for wid, data in self._tokens.items()
        ]


class SlackClient:
    """Slack API client for CRM integration."""

    BASE_URL = "https://slack.com/api"

    def __init__(self, token_store: Optional[SlackTokenStore] = None, token: Optional[str] = None):
        """
        Initialize Slack client.

        Args:
            token_store: Optional token store for OAuth-based tokens
            token: Optional direct token (overrides token store)
        """
        self.token_store = token_store or SlackTokenStore()
        self._direct_token = token
        self._http_client: Optional[httpx.Client] = None
        self._user_cache: dict[str, SlackUser] = {}

    @property
    def http_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=30.0)
        return self._http_client

    def close(self):
        """Close HTTP client."""
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def _get_token(self, workspace_id: str = "default") -> Optional[str]:
        """Get token from direct token, env var, or token store."""
        # Priority: direct token > env var > token store
        if self._direct_token:
            return self._direct_token
        if SLACK_USER_TOKEN:
            return SLACK_USER_TOKEN
        return self.token_store.get_token(workspace_id)

    def is_configured(self) -> bool:
        """Check if Slack is configured (either OAuth or direct token)."""
        return bool(SLACK_USER_TOKEN or (SLACK_CLIENT_ID and SLACK_CLIENT_SECRET))

    def is_connected(self, workspace_id: str = "default") -> bool:
        """Check if we have a valid token."""
        return self._get_token(workspace_id) is not None

    def get_oauth_url(self, state: Optional[str] = None) -> str:
        """Generate OAuth authorization URL."""
        params = {
            "client_id": SLACK_CLIENT_ID,
            "scope": ",".join(SLACK_SCOPES),
            "redirect_uri": SLACK_REDIRECT_URI,
        }
        if state:
            params["state"] = state
        return f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict:
        """
        Exchange OAuth code for access token.

        Returns dict with access_token, team info, etc.
        """
        response = self.http_client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": SLACK_CLIENT_ID,
                "client_secret": SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": SLACK_REDIRECT_URI,
            },
        )
        data = response.json()

        if not data.get("ok"):
            raise SlackAPIError(data.get("error", "Unknown OAuth error"))

        # Store the token
        access_token = data.get("access_token")
        team = data.get("team", {})
        authed_user = data.get("authed_user", {})

        self.token_store.set_token(
            access_token=access_token,
            workspace_id=team.get("id", "default"),
            team_name=team.get("name"),
            authed_user_id=authed_user.get("id"),
        )

        return data

    def _api_call(
        self,
        method: str,
        workspace_id: str = "default",
        max_retries: int = 3,
        **kwargs,
    ) -> dict:
        """
        Make authenticated Slack API call with rate limit handling.

        Args:
            method: Slack API method name
            workspace_id: Workspace ID
            max_retries: Maximum retries on rate limit
            **kwargs: API parameters

        Returns:
            API response dict
        """
        import time

        token = self._get_token(workspace_id)
        if not token:
            raise SlackAPIError(f"No token available for workspace {workspace_id}")

        retry_delay = 2  # Start with 2 seconds

        for attempt in range(max_retries + 1):
            response = self.http_client.post(
                f"{self.BASE_URL}/{method}",
                headers={"Authorization": f"Bearer {token}"},
                json=kwargs if kwargs else None,
            )
            data = response.json()

            if data.get("ok"):
                return data

            error = data.get("error", "Unknown API error")

            # Handle rate limiting with exponential backoff
            if error == "ratelimited":
                if attempt < max_retries:
                    retry_after = int(response.headers.get("Retry-After", retry_delay))
                    logger.warning(f"Rate limited, waiting {retry_after}s (attempt {attempt + 1}/{max_retries + 1})")
                    time.sleep(retry_after)
                    retry_delay *= 2  # Exponential backoff
                    continue
                else:
                    raise SlackAPIError("Rate limit exceeded after retries")

            # Handle auth errors
            if error == "token_revoked" or error == "invalid_auth":
                if not self._direct_token and not SLACK_USER_TOKEN:
                    self.token_store.remove_token(workspace_id)
            raise SlackAPIError(error)

        raise SlackAPIError("Max retries exceeded")

    def list_users(self, workspace_id: str = "default") -> list[SlackUser]:
        """List all users in workspace."""
        users = []
        cursor = None

        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor

            data = self._api_call("users.list", workspace_id, **params)

            for member in data.get("members", []):
                profile = member.get("profile", {})
                users.append(SlackUser(
                    user_id=member["id"],
                    username=member.get("name", ""),
                    real_name=profile.get("real_name", ""),
                    display_name=profile.get("display_name", ""),
                    email=profile.get("email"),
                    title=profile.get("title"),
                    phone=profile.get("phone"),
                    image_url=profile.get("image_192"),
                    is_bot=member.get("is_bot", False),
                    is_deleted=member.get("deleted", False),
                    team_id=member.get("team_id"),
                    timezone=member.get("tz"),
                ))

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return users

    def get_user(self, user_id: str, workspace_id: str = "default") -> Optional[SlackUser]:
        """Get single user by ID."""
        try:
            data = self._api_call("users.info", workspace_id, user=user_id)
            member = data.get("user", {})
            profile = member.get("profile", {})
            return SlackUser(
                user_id=member["id"],
                username=member.get("name", ""),
                real_name=profile.get("real_name", ""),
                display_name=profile.get("display_name", ""),
                email=profile.get("email"),
                title=profile.get("title"),
                phone=profile.get("phone"),
                image_url=profile.get("image_192"),
                is_bot=member.get("is_bot", False),
                is_deleted=member.get("deleted", False),
                team_id=member.get("team_id"),
                timezone=member.get("tz"),
            )
        except SlackAPIError:
            return None

    def list_channels(self, workspace_id: str = "default", include_dms: bool = True) -> list[SlackChannel]:
        """
        List all accessible channels with pagination.

        Args:
            workspace_id: Workspace ID
            include_dms: If True, include DMs (im, mpim). If False, only regular channels.

        Returns:
            List of SlackChannel objects
        """
        import time

        channels = []
        cursor = None

        # Determine types to fetch
        # Note: Slack API returns different results for different type combinations
        # We need to query DMs separately for best results
        types_list = ["public_channel", "private_channel"]
        if include_dms:
            types_list.extend(["im", "mpim"])

        token = self._get_token(workspace_id)
        if not token:
            raise SlackAPIError(f"No token available for workspace {workspace_id}")

        retry_delay = 2
        max_retries = 3

        while True:
            params = {
                "types": ",".join(types_list),
                "limit": 200,  # Max per request
            }
            if cursor:
                params["cursor"] = cursor

            # Use GET with params for conversations.list (more reliable)
            for attempt in range(max_retries + 1):
                response = self.http_client.get(
                    f"{self.BASE_URL}/conversations.list",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                data = response.json()

                if data.get("ok"):
                    break

                error = data.get("error", "Unknown API error")
                if error == "ratelimited" and attempt < max_retries:
                    retry_after = int(response.headers.get("Retry-After", retry_delay))
                    logger.warning(f"Rate limited on conversations.list, waiting {retry_after}s")
                    time.sleep(retry_after)
                    retry_delay *= 2
                    continue

                raise SlackAPIError(error)

            for channel in data.get("channels", []):
                channels.append(SlackChannel(
                    channel_id=channel["id"],
                    name=channel.get("name", channel.get("user", "DM")),
                    is_private=channel.get("is_private", False),
                    is_im=channel.get("is_im", False),
                    is_mpim=channel.get("is_mpim", False),
                    member_count=channel.get("num_members", 0),
                ))

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return channels

    def get_channel_history(
        self,
        channel_id: str,
        workspace_id: str = "default",
        limit: int = 100,
        oldest: Optional[datetime] = None,
        latest: Optional[datetime] = None,
    ) -> list[SlackMessage]:
        """Get message history for a channel (single page)."""
        params = {"channel": channel_id, "limit": limit}

        if oldest:
            params["oldest"] = str(oldest.timestamp())
        if latest:
            params["latest"] = str(latest.timestamp())

        data = self._api_call("conversations.history", workspace_id, **params)
        messages = []

        for msg in data.get("messages", []):
            if msg.get("type") != "message":
                continue

            ts = float(msg["ts"])
            messages.append(SlackMessage(
                ts=msg["ts"],
                channel_id=channel_id,
                user_id=msg.get("user", ""),
                text=msg.get("text", ""),
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                thread_ts=msg.get("thread_ts"),
                reply_count=msg.get("reply_count", 0),
                reactions=msg.get("reactions", []),
            ))

        return messages

    def get_all_channel_history(
        self,
        channel_id: str,
        workspace_id: str = "default",
        oldest: Optional[datetime] = None,
        latest: Optional[datetime] = None,
        max_messages: int = 10000,
    ) -> list[SlackMessage]:
        """
        Get full message history for a channel with pagination.

        Args:
            channel_id: Channel ID to fetch history for
            workspace_id: Workspace ID
            oldest: Only fetch messages after this time
            latest: Only fetch messages before this time
            max_messages: Maximum messages to fetch (safety limit)

        Returns:
            List of SlackMessage objects
        """
        all_messages = []
        cursor = None

        while len(all_messages) < max_messages:
            params = {"channel": channel_id, "limit": 200}

            if oldest:
                params["oldest"] = str(oldest.timestamp())
            if latest:
                params["latest"] = str(latest.timestamp())
            if cursor:
                params["cursor"] = cursor

            data = self._api_call("conversations.history", workspace_id, **params)

            for msg in data.get("messages", []):
                if msg.get("type") != "message":
                    continue

                ts = float(msg["ts"])
                all_messages.append(SlackMessage(
                    ts=msg["ts"],
                    channel_id=channel_id,
                    user_id=msg.get("user", ""),
                    text=msg.get("text", ""),
                    timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                    thread_ts=msg.get("thread_ts"),
                    reply_count=msg.get("reply_count", 0),
                    reactions=msg.get("reactions", []),
                ))

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return all_messages

    def get_user_cached(self, user_id: str, workspace_id: str = "default") -> Optional[SlackUser]:
        """Get user with caching to reduce API calls."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        user = self.get_user(user_id, workspace_id)
        if user:
            self._user_cache[user_id] = user
        return user


class SlackAPIError(Exception):
    """Error from Slack API."""
    pass


def create_slack_source_entity(
    slack_user: SlackUser,
    team_id: Optional[str] = None,
) -> SourceEntity:
    """
    Create a SourceEntity from a Slack user.

    Args:
        slack_user: SlackUser object
        team_id: Slack team/workspace ID

    Returns:
        SourceEntity ready for storage
    """
    source_id = f"{team_id or 'default'}:{slack_user.user_id}"

    return SourceEntity(
        source_type=SOURCE_SLACK,
        source_id=source_id,
        observed_name=slack_user.real_name or slack_user.display_name or slack_user.username,
        observed_email=slack_user.email,
        observed_phone=slack_user.phone,
        metadata={
            "username": slack_user.username,
            "display_name": slack_user.display_name,
            "title": slack_user.title,
            "image_url": slack_user.image_url,
            "is_bot": slack_user.is_bot,
            "team_id": team_id or slack_user.team_id,
            "timezone": slack_user.timezone,
        },
        observed_at=datetime.now(timezone.utc),
    )


def sync_slack_users(
    client: SlackClient,
    entity_store: SourceEntityStore,
    workspace_id: str = "default",
) -> dict:
    """
    Sync Slack users to SourceEntity store.

    Returns sync statistics.
    """
    stats = {
        "total": 0,
        "created": 0,
        "updated": 0,
        "skipped_bots": 0,
        "skipped_deleted": 0,
    }

    users = client.list_users(workspace_id)
    stats["total"] = len(users)

    for user in users:
        # Skip bots and deleted users
        if user.is_bot:
            stats["skipped_bots"] += 1
            continue
        if user.is_deleted:
            stats["skipped_deleted"] += 1
            continue

        source_entity = create_slack_source_entity(user, team_id=workspace_id)

        # Check if entity already exists
        existing = entity_store.get_by_source(SOURCE_SLACK, source_entity.source_id)
        if existing:
            # Update metadata
            existing.observed_name = source_entity.observed_name
            existing.observed_email = source_entity.observed_email
            existing.observed_phone = source_entity.observed_phone
            existing.metadata = source_entity.metadata
            existing.observed_at = source_entity.observed_at
            entity_store.update(existing)
            stats["updated"] += 1
        else:
            entity_store.add(source_entity)
            stats["created"] += 1

    return stats


# Singleton client instance
_slack_client: Optional[SlackClient] = None


def get_slack_client() -> SlackClient:
    """Get or create singleton Slack client."""
    global _slack_client
    if _slack_client is None:
        _slack_client = SlackClient()
    return _slack_client


def get_workspace_id() -> str:
    """Get the configured workspace ID from env or default."""
    return SLACK_TEAM_ID or "default"


def is_slack_enabled() -> bool:
    """Check if Slack integration is enabled and configured."""
    return bool(SLACK_USER_TOKEN)
