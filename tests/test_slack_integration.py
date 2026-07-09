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


@patch("api.services.slack_integration.SLACK_USER_TOKEN", "")
class TestListChannelsMemberOnly:
    """Issue #439: member_only=True must enumerate via users.conversations
    (channels the authed user belongs to) instead of conversations.list
    (every channel in the workspace)."""

    def _client_with_capture(self):
        from api.services.slack_integration import SlackClient

        store = MagicMock()
        store.get_token.return_value = "xoxp-test-fake-token"
        client = SlackClient(token_store=store)

        response = MagicMock()
        response.json.return_value = {
            "ok": True,
            "channels": [
                {"id": "C1", "name": "general", "is_private": False},
                {"id": "D1", "user": "U123", "is_im": True},
            ],
            "response_metadata": {},
        }
        http = MagicMock()
        http.get.return_value = response
        client._http_client = http
        return client, http

    def test_member_only_uses_users_conversations(self):
        client, http = self._client_with_capture()

        channels = client.list_channels(member_only=True)

        url = http.get.call_args.args[0]
        assert url.endswith("/users.conversations")
        assert len(channels) == 2

    def test_default_uses_conversations_list(self):
        client, http = self._client_with_capture()

        client.list_channels()

        url = http.get.call_args.args[0]
        assert url.endswith("/conversations.list")

    def test_member_only_excludes_archived(self):
        client, http = self._client_with_capture()

        client.list_channels(member_only=True)

        params = http.get.call_args.kwargs["params"]
        assert params["exclude_archived"] == "true"

    def test_default_does_not_exclude_archived(self):
        client, http = self._client_with_capture()

        client.list_channels()

        params = http.get.call_args.kwargs["params"]
        assert "exclude_archived" not in params


@patch("api.services.slack_integration.SLACK_USER_TOKEN", "")
class TestGetAllChannelHistoryNoiseFiltering:
    """Channel membership/metadata system messages (channel_join,
    channel_topic, ...) must not be returned by the sync bulk-fetch path —
    each would otherwise become a searchable indexed document."""

    def _client_with_history(self, messages):
        from api.services.slack_integration import SlackClient

        store = MagicMock()
        store.get_token.return_value = "xoxp-test-fake-token"
        client = SlackClient(token_store=store)
        client._api_call = MagicMock(return_value={
            "ok": True,
            "messages": messages,
            "response_metadata": {},
        })
        return client

    def test_noise_subtypes_are_skipped(self):
        client = self._client_with_history([
            {"type": "message", "ts": "1700000003.000000",
             "user": "U1", "text": "Real discussion message"},
            {"type": "message", "ts": "1700000002.000000", "user": "U2",
             "subtype": "channel_join",
             "text": "<@U2> has joined the channel"},
            {"type": "message", "ts": "1700000001.000000", "user": "U1",
             "subtype": "channel_topic",
             "text": "<@U1> set the channel topic: standup notes"},
        ])

        messages = client.get_all_channel_history("C123")

        assert len(messages) == 1
        assert messages[0].text == "Real discussion message"

    def test_bot_messages_are_kept(self):
        client = self._client_with_history([
            {"type": "message", "ts": "1700000004.000000", "user": "",
             "subtype": "bot_message", "text": "Deploy finished: build 42"},
        ])

        messages = client.get_all_channel_history("C123")

        assert len(messages) == 1
        assert messages[0].text == "Deploy finished: build 42"


@patch("api.services.slack_integration.SLACK_USER_TOKEN", "")
class TestGetThreadReplies:
    """Issue #440: conversations.replies fetch — paginated, parent excluded."""

    def _client(self, pages):
        from api.services.slack_integration import SlackClient

        store = MagicMock()
        store.get_token.return_value = "xoxp-test-fake-token"
        client = SlackClient(token_store=store)
        client._api_call = MagicMock(side_effect=pages)
        return client

    def test_excludes_parent_and_parses_replies(self):
        client = self._client([{
            "ok": True,
            "messages": [
                {"type": "message", "ts": "1700000000.000100",
                 "thread_ts": "1700000000.000100", "user": "U1",
                 "text": "parent message", "reply_count": 2},
                {"type": "message", "ts": "1700000100.000200",
                 "thread_ts": "1700000000.000100", "user": "U2",
                 "text": "first reply"},
                {"type": "message", "ts": "1700000200.000300",
                 "thread_ts": "1700000000.000100", "user": "U1",
                 "text": "second reply"},
            ],
            "response_metadata": {},
        }])

        replies = client.get_thread_replies("C123", "1700000000.000100")

        assert [r.text for r in replies] == ["first reply", "second reply"]
        assert all(r.thread_ts == "1700000000.000100" for r in replies)

    def test_paginates_with_cursor(self):
        client = self._client([
            {"ok": True,
             "messages": [{"type": "message", "ts": "1700000100.000200",
                           "thread_ts": "1700000000.000100", "user": "U2",
                           "text": "reply page 1"}],
             "response_metadata": {"next_cursor": "cur2"}},
            {"ok": True,
             "messages": [{"type": "message", "ts": "1700000200.000300",
                           "thread_ts": "1700000000.000100", "user": "U1",
                           "text": "reply page 2"}],
             "response_metadata": {}},
        ])

        replies = client.get_thread_replies("C123", "1700000000.000100")

        assert [r.text for r in replies] == ["reply page 1", "reply page 2"]
        assert client._api_call.call_count == 2
        second_kwargs = client._api_call.call_args_list[1].kwargs
        assert second_kwargs["cursor"] == "cur2"

    def test_oldest_is_passed_through(self):
        from datetime import datetime, timezone

        client = self._client([{"ok": True, "messages": [], "response_metadata": {}}])
        oldest = datetime(2026, 7, 1, tzinfo=timezone.utc)

        client.get_thread_replies("C123", "1700000000.000100", oldest=oldest)

        kwargs = client._api_call.call_args.kwargs
        assert kwargs["oldest"] == str(oldest.timestamp())

    def test_history_parses_latest_reply(self):
        """get_all_channel_history must surface latest_reply so the sync can
        tell which threads have new activity."""
        client = self._client([{
            "ok": True,
            "messages": [
                {"type": "message", "ts": "1700000000.000100", "user": "U1",
                 "text": "parent", "reply_count": 3,
                 "latest_reply": "1700009999.000500"},
            ],
            "response_metadata": {},
        }])

        messages = client.get_all_channel_history("C123")

        assert messages[0].latest_reply == "1700009999.000500"
