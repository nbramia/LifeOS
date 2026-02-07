"""Tests for Slack integration service."""
import tempfile
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from api.services.slack_integration import (
    SlackUser,
    SlackMessage,
    SlackChannel,
    SlackTokenStore,
    SlackClient,
    SlackAPIError,
    create_slack_source_entity,
    SOURCE_SLACK,
)


class TestSlackUser:
    """Tests for SlackUser dataclass."""

    def test_create_user(self):
        """Test basic user creation."""
        user = SlackUser(
            user_id="U12345",
            username="jdoe",
            real_name="John Doe",
            display_name="John",
            email="john@example.com",
        )

        assert user.user_id == "U12345"
        assert user.username == "jdoe"
        assert user.real_name == "John Doe"
        assert user.email == "john@example.com"

    def test_to_dict(self):
        """Test serialization to dict."""
        user = SlackUser(
            user_id="U12345",
            username="jdoe",
            real_name="John Doe",
            display_name="John",
            email="john@example.com",
            title="Engineer",
        )

        data = user.to_dict()
        assert data["user_id"] == "U12345"
        assert data["email"] == "john@example.com"
        assert data["title"] == "Engineer"


class TestSlackMessage:
    """Tests for SlackMessage dataclass."""

    def test_create_message(self):
        """Test message creation."""
        now = datetime.now(timezone.utc)
        msg = SlackMessage(
            ts="1234567890.123456",
            channel_id="C12345",
            user_id="U12345",
            text="Hello world",
            timestamp=now,
        )

        assert msg.ts == "1234567890.123456"
        assert msg.channel_id == "C12345"
        assert msg.text == "Hello world"

    def test_to_dict(self):
        """Test serialization."""
        now = datetime.now(timezone.utc)
        msg = SlackMessage(
            ts="1234567890.123456",
            channel_id="C12345",
            user_id="U12345",
            text="Hello world",
            timestamp=now,
            reply_count=5,
        )

        data = msg.to_dict()
        assert data["ts"] == "1234567890.123456"
        assert data["reply_count"] == 5


class TestSlackChannel:
    """Tests for SlackChannel dataclass."""

    def test_create_channel(self):
        """Test channel creation."""
        channel = SlackChannel(
            channel_id="C12345",
            name="general",
            is_private=False,
            member_count=50,
        )

        assert channel.channel_id == "C12345"
        assert channel.name == "general"
        assert channel.member_count == 50

    def test_to_dict(self):
        """Test serialization."""
        channel = SlackChannel(
            channel_id="C12345",
            name="general",
            is_private=True,
        )

        data = channel.to_dict()
        assert data["channel_id"] == "C12345"
        assert data["is_private"] is True


class TestSlackTokenStore:
    """Tests for SlackTokenStore."""

    @pytest.fixture
    def temp_token_path(self):
        """Create a temporary path for token storage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "slack_tokens.json"

    def test_set_and_get_token(self, temp_token_path):
        """Test storing and retrieving tokens."""
        store = SlackTokenStore(path=temp_token_path)
        store.set_token("xoxb-test-token", workspace_id="T12345", team_name="Test Workspace")

        assert store.get_token("T12345") == "xoxb-test-token"
        assert store.get_token("nonexistent") is None

    def test_remove_token(self, temp_token_path):
        """Test removing tokens."""
        store = SlackTokenStore(path=temp_token_path)
        store.set_token("xoxb-test-token", workspace_id="T12345")

        store.remove_token("T12345")
        assert store.get_token("T12345") is None

    def test_list_workspaces(self, temp_token_path):
        """Test listing connected workspaces."""
        store = SlackTokenStore(path=temp_token_path)
        store.set_token("token1", workspace_id="T1", team_name="Workspace 1")
        store.set_token("token2", workspace_id="T2", team_name="Workspace 2")

        workspaces = store.list_workspaces()
        assert len(workspaces) == 2
        assert any(w["workspace_id"] == "T1" for w in workspaces)
        assert any(w["workspace_id"] == "T2" for w in workspaces)

    def test_persistence(self, temp_token_path):
        """Test that tokens persist across instances."""
        store1 = SlackTokenStore(path=temp_token_path)
        store1.set_token("xoxb-test-token", workspace_id="T12345")

        # Create new instance - should load from disk
        store2 = SlackTokenStore(path=temp_token_path)
        assert store2.get_token("T12345") == "xoxb-test-token"


@patch("api.services.slack_integration.SLACK_USER_TOKEN", "")
class TestSlackClient:
    """Tests for SlackClient."""

    @pytest.fixture
    def mock_token_store(self):
        """Create a mock token store."""
        store = MagicMock()
        store.get_token.return_value = "xoxb-test-token"
        return store

    @pytest.fixture
    def client(self, mock_token_store):
        """Create a SlackClient with mock token store."""
        return SlackClient(token_store=mock_token_store)

    def test_is_connected(self, client, mock_token_store):
        """Test connection check."""
        assert client.is_connected("default") is True

        mock_token_store.get_token.return_value = None
        assert client.is_connected("default") is False

    @patch.object(SlackClient, "is_configured", return_value=True)
    def test_get_oauth_url(self, mock_configured, client):
        """Test OAuth URL generation."""
        with patch("api.services.slack_integration.SLACK_CLIENT_ID", "test-client-id"):
            url = client.get_oauth_url(state="test-state")
            assert "client_id=test-client-id" in url
            assert "state=test-state" in url

    def test_api_call_no_token(self, client, mock_token_store):
        """Test API call fails without token."""
        mock_token_store.get_token.return_value = None

        with pytest.raises(SlackAPIError) as exc_info:
            client._api_call("test.method", workspace_id="default")

        assert "No token available" in str(exc_info.value)


class TestCreateSlackSourceEntity:
    """Tests for create_slack_source_entity factory function."""

    def test_basic_user(self):
        """Test creating entity from basic user."""
        user = SlackUser(
            user_id="U12345",
            username="jdoe",
            real_name="John Doe",
            display_name="John",
            email="john@example.com",
        )

        entity = create_slack_source_entity(user, team_id="T12345")

        assert entity.source_type == SOURCE_SLACK
        assert entity.source_id == "T12345:U12345"
        assert entity.observed_name == "John Doe"
        assert entity.observed_email == "john@example.com"

    def test_user_with_phone(self):
        """Test creating entity with phone number."""
        user = SlackUser(
            user_id="U12345",
            username="jdoe",
            real_name="John Doe",
            display_name="John",
            phone="+1-555-0100",
        )

        entity = create_slack_source_entity(user)

        assert entity.observed_phone == "+1-555-0100"

    def test_metadata_fields(self):
        """Test that metadata includes Slack-specific fields."""
        user = SlackUser(
            user_id="U12345",
            username="jdoe",
            real_name="John Doe",
            display_name="John D",
            title="Senior Engineer",
            image_url="https://example.com/image.png",
            timezone="America/New_York",
        )

        entity = create_slack_source_entity(user, team_id="T12345")

        assert entity.metadata["username"] == "jdoe"
        assert entity.metadata["display_name"] == "John D"
        assert entity.metadata["title"] == "Senior Engineer"
        assert entity.metadata["image_url"] == "https://example.com/image.png"
        assert entity.metadata["timezone"] == "America/New_York"

    def test_fallback_name(self):
        """Test name fallback when real_name is empty."""
        user = SlackUser(
            user_id="U12345",
            username="jdoe",
            real_name="",
            display_name="John D",
        )

        entity = create_slack_source_entity(user)
        assert entity.observed_name == "John D"

        # Further fallback to username
        user2 = SlackUser(
            user_id="U12345",
            username="jdoe",
            real_name="",
            display_name="",
        )

        entity2 = create_slack_source_entity(user2)
        assert entity2.observed_name == "jdoe"
