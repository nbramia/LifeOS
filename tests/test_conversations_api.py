"""
Tests for api/routes/conversations.py

Tests conversation CRUD endpoints and messaging functionality.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from fastapi.testclient import TestClient

from api.main import app
from api.services.conversation_store import Conversation, Message


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def client():
    """Create a test client for the API."""
    return TestClient(app)


@pytest.fixture
def mock_store():
    """Create a mock conversation store."""
    store = MagicMock()
    return store


@pytest.fixture
def sample_conversation():
    """Create a sample conversation."""
    now = datetime.now()
    return Conversation(
        id="conv-123",
        title="Test Conversation",
        created_at=now,
        updated_at=now,
        message_count=2
    )


@pytest.fixture
def sample_messages():
    """Create sample messages."""
    now = datetime.now()
    return [
        Message(
            id="msg-1",
            conversation_id="conv-123",
            role="user",
            content="Hello, how are you?",
            created_at=now,
            sources=None,
            routing=None
        ),
        Message(
            id="msg-2",
            conversation_id="conv-123",
            role="assistant",
            content="I'm doing well, thanks!",
            created_at=now,
            sources=[{"file_name": "test.md"}],
            routing={"sources": ["vault"]}
        ),
    ]


# =============================================================================
# GET /api/conversations Tests
# =============================================================================

@pytest.mark.unit
class TestListConversations:
    """Tests for GET /api/conversations endpoint."""

    def test_list_conversations_empty(self, client, mock_store):
        """Test listing when no conversations exist."""
        mock_store.list_conversations.return_value = []

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations")

        assert response.status_code == 200
        data = response.json()
        assert data["conversations"] == []

    def test_list_conversations_with_data(self, client, mock_store, sample_conversation):
        """Test listing conversations with data."""
        mock_store.list_conversations.return_value = [sample_conversation]

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations")

        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 1
        assert data["conversations"][0]["id"] == "conv-123"
        assert data["conversations"][0]["title"] == "Test Conversation"
        assert data["conversations"][0]["message_count"] == 2

    def test_list_conversations_multiple(self, client, mock_store):
        """Test listing multiple conversations."""
        now = datetime.now()
        conversations = [
            Conversation(id=f"conv-{i}", title=f"Conv {i}",
                        created_at=now, updated_at=now, message_count=i)
            for i in range(5)
        ]
        mock_store.list_conversations.return_value = conversations

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations")

        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 5

    def test_list_conversations_includes_timestamps(self, client, mock_store, sample_conversation):
        """Test that timestamps are included in ISO format."""
        mock_store.list_conversations.return_value = [sample_conversation]

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations")

        assert response.status_code == 200
        data = response.json()
        conv = data["conversations"][0]
        assert "created_at" in conv
        assert "updated_at" in conv
        # Should be ISO format strings
        assert "T" in conv["created_at"]


@pytest.mark.unit
class TestListConversationsBackendParam:
    """The optional `backend` query filter (#596)."""

    def test_omitted_backend_leaves_filter_unset(self, client, mock_store):
        # Preserves today's behavior exactly: no backend filter applied.
        mock_store.list_conversations.return_value = []
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            client.get("/api/conversations")
        _, kwargs = mock_store.list_conversations.call_args
        assert kwargs["backend"] is None

    def test_explicit_backend_forwarded_to_store(self, client, mock_store):
        mock_store.list_conversations.return_value = []
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations?backend=hermes")
        assert response.status_code == 200
        _, kwargs = mock_store.list_conversations.call_args
        assert kwargs["backend"] == "hermes"

    def test_response_includes_backend_tag(self, client, mock_store):
        now = datetime.now()
        conv = Conversation(
            id="conv-h", title="Hermes thread", created_at=now, updated_at=now,
            message_count=0, backend="hermes",
        )
        mock_store.list_conversations.return_value = [conv]
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations?backend=hermes")
        assert response.json()["conversations"][0]["backend"] == "hermes"


# =============================================================================
# POST /api/conversations Tests
# =============================================================================

@pytest.mark.unit
class TestCreateConversation:
    """Tests for POST /api/conversations endpoint."""

    def test_create_conversation_with_title(self, client, mock_store, sample_conversation):
        """Test creating a conversation with a title."""
        sample_conversation.title = "My Custom Title"
        sample_conversation.message_count = 0
        mock_store.create_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations",
                json={"title": "My Custom Title"}
            )

        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "My Custom Title"
        assert data["message_count"] == 0
        mock_store.create_conversation.assert_called_once_with(title="My Custom Title")

    def test_create_conversation_without_title(self, client, mock_store, sample_conversation):
        """Test creating a conversation without a title."""
        sample_conversation.title = "New Conversation"
        sample_conversation.message_count = 0
        mock_store.create_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations",
                json={}
            )

        assert response.status_code == 201
        mock_store.create_conversation.assert_called_once_with(title=None)

    def test_create_conversation_returns_id(self, client, mock_store, sample_conversation):
        """Test that created conversation returns an ID."""
        sample_conversation.message_count = 0
        mock_store.create_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post("/api/conversations", json={})

        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        assert data["id"] == "conv-123"


# =============================================================================
# GET /api/conversations/{id} Tests
# =============================================================================

@pytest.mark.unit
class TestGetConversation:
    """Tests for GET /api/conversations/{id} endpoint."""

    def test_get_conversation_exists(
        self, client, mock_store, sample_conversation, sample_messages
    ):
        """Test getting an existing conversation."""
        mock_store.get_conversation.return_value = sample_conversation
        mock_store.get_messages.return_value = sample_messages

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations/conv-123")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "conv-123"
        assert data["title"] == "Test Conversation"
        assert len(data["messages"]) == 2

    def test_get_conversation_not_found(self, client, mock_store):
        """Test getting a non-existent conversation."""
        mock_store.get_conversation.return_value = None

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_conversation_includes_messages(
        self, client, mock_store, sample_conversation, sample_messages
    ):
        """Test that messages are included in response."""
        mock_store.get_conversation.return_value = sample_conversation
        mock_store.get_messages.return_value = sample_messages

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations/conv-123")

        assert response.status_code == 200
        data = response.json()
        messages = data["messages"]

        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello, how are you?"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["sources"] is not None

    def test_get_conversation_message_metadata(
        self, client, mock_store, sample_conversation, sample_messages
    ):
        """Test that message metadata (sources, routing) is included."""
        mock_store.get_conversation.return_value = sample_conversation
        mock_store.get_messages.return_value = sample_messages

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations/conv-123")

        assert response.status_code == 200
        data = response.json()
        assistant_msg = data["messages"][1]

        assert assistant_msg["sources"] == [{"file_name": "test.md"}]
        assert assistant_msg["routing"] == {"sources": ["vault"]}


# =============================================================================
# DELETE /api/conversations/{id} Tests
# =============================================================================

@pytest.mark.unit
class TestDeleteConversation:
    """Tests for DELETE /api/conversations/{id} endpoint."""

    def test_delete_conversation_exists(self, client, mock_store):
        """Test deleting an existing conversation."""
        mock_store.delete_conversation.return_value = True

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.delete("/api/conversations/conv-123")

        assert response.status_code == 204
        mock_store.delete_conversation.assert_called_once_with("conv-123")

    def test_delete_conversation_not_found(self, client, mock_store):
        """Test deleting a non-existent conversation."""
        mock_store.delete_conversation.return_value = False

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.delete("/api/conversations/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


# =============================================================================
# POST /api/conversations/{id}/ask Tests
# =============================================================================

@pytest.mark.unit
class TestAskInConversation:
    """Tests for POST /api/conversations/{id}/ask endpoint."""

    def test_ask_conversation_not_found(self, client, mock_store):
        """Test asking in a non-existent conversation."""
        mock_store.get_conversation.return_value = None

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations/nonexistent/ask",
                json={"question": "Hello?"}
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_ask_empty_question(self, client, mock_store, sample_conversation):
        """Test asking with empty question."""
        mock_store.get_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations/conv-123/ask",
                json={"question": ""}
            )

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_ask_whitespace_question(self, client, mock_store, sample_conversation):
        """Test asking with whitespace-only question."""
        mock_store.get_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations/conv-123/ask",
                json={"question": "   "}
            )

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_ask_missing_question(self, client, mock_store, sample_conversation):
        """Test asking without question field."""
        mock_store.get_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations/conv-123/ask",
                json={}
            )

        # FastAPI validation error
        assert response.status_code in (400, 422)


# =============================================================================
# Response Model Tests
# =============================================================================

@pytest.mark.unit
class TestResponseModels:
    """Tests for response model structure."""

    def test_conversation_response_structure(self, client, mock_store, sample_conversation):
        """Test ConversationResponse has all required fields."""
        sample_conversation.message_count = 0
        mock_store.create_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post("/api/conversations", json={})

        assert response.status_code == 201
        data = response.json()

        required_fields = ["id", "title", "created_at", "updated_at", "message_count"]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    def test_conversation_detail_response_structure(
        self, client, mock_store, sample_conversation, sample_messages
    ):
        """Test ConversationDetailResponse has all required fields."""
        mock_store.get_conversation.return_value = sample_conversation
        mock_store.get_messages.return_value = sample_messages

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations/conv-123")

        assert response.status_code == 200
        data = response.json()

        required_fields = ["id", "title", "created_at", "updated_at", "messages"]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

        # Check message structure
        msg = data["messages"][0]
        msg_fields = ["id", "role", "content", "created_at"]
        for field in msg_fields:
            assert field in msg, f"Missing message field: {field}"


# =============================================================================
# Edge Cases
# =============================================================================

@pytest.mark.unit
class TestConversationEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_conversation_with_special_characters_in_title(
        self, client, mock_store, sample_conversation
    ):
        """Test conversation with special characters in title."""
        sample_conversation.title = "Test: <script>alert('xss')</script>"
        sample_conversation.message_count = 0
        mock_store.create_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations",
                json={"title": "Test: <script>alert('xss')</script>"}
            )

        assert response.status_code == 201
        # Title should be stored as-is (escaping is frontend concern)
        data = response.json()
        assert "<script>" in data["title"]

    def test_conversation_with_unicode_title(
        self, client, mock_store, sample_conversation
    ):
        """Test conversation with unicode characters."""
        sample_conversation.title = "日本語のタイトル 🎉"
        sample_conversation.message_count = 0
        mock_store.create_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations",
                json={"title": "日本語のタイトル 🎉"}
            )

        assert response.status_code == 201
        data = response.json()
        assert "日本語" in data["title"]

    def test_conversation_with_very_long_title(
        self, client, mock_store, sample_conversation
    ):
        """Test conversation with very long title."""
        long_title = "A" * 1000
        sample_conversation.title = long_title
        sample_conversation.message_count = 0
        mock_store.create_conversation.return_value = sample_conversation

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations",
                json={"title": long_title}
            )

        assert response.status_code == 201

    def test_get_conversation_with_many_messages(
        self, client, mock_store, sample_conversation
    ):
        """Test getting conversation with many messages."""
        now = datetime.now()
        many_messages = [
            Message(
                id=f"msg-{i}",
                conversation_id="conv-123",
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}",
                created_at=now
            )
            for i in range(100)
        ]
        sample_conversation.message_count = 100
        mock_store.get_conversation.return_value = sample_conversation
        mock_store.get_messages.return_value = many_messages

        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations/conv-123")

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 100


# =============================================================================
# POST /api/conversations/{id}/answer Tests (#403 web/voice parity)
# =============================================================================

@pytest.mark.unit
class TestAnswerInConversation:
    """Tests for POST /api/conversations/{id}/answer — answering a spawned
    orchestrating-persona session's [CLARIFY]/[GOAL] from web/voice."""

    def _linked_conversation(self):
        now = datetime.now()
        return Conversation(
            id="conv-403", title="Doctor session", created_at=now, updated_at=now,
            message_count=1, persona_id="doctor", agent_session_id="sess_abc",
        )

    def test_answer_deposits_via_session_keyed_path(self, client, mock_store):
        """Happy path: deposits onto the spawned session's open question and
        echoes the answer into the thread."""
        mock_store.get_conversation.return_value = self._linked_conversation()
        fake_session_store = MagicMock()
        fake_session_store.deposit_answer_by_session_id.return_value = True

        with patch("api.routes.conversations.get_store", return_value=mock_store), \
             patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=fake_session_store):
            response = client.post(
                "/api/conversations/conv-403/answer",
                json={"answer": "the lifeos repo"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["session_id"] == "sess_abc"
        fake_session_store.deposit_answer_by_session_id.assert_called_once_with(
            "sess_abc", "the lifeos repo"
        )
        # The answer is echoed into the conversation thread.
        mock_store.add_message.assert_called_once()

    def test_answer_reaches_session_from_a_hermes_tagged_conversation(self, client, mock_store):
        """#596: a session spawned from a Hermes-selected turn links to its
        conversation exactly as on lifeos (test_persona_api.py's
        test_doctor_spawn_diverted_from_hermes_tags_conversation_hermes) — this
        endpoint doesn't branch on `backend` at all, so the answer path must
        work unchanged regardless of which backend tagged the thread."""
        now = datetime.now()
        hermes_conv = Conversation(
            id="conv-403", title="Doctor session", created_at=now, updated_at=now,
            message_count=1, persona_id="doctor", agent_session_id="sess_abc",
            backend="hermes",
        )
        mock_store.get_conversation.return_value = hermes_conv
        fake_session_store = MagicMock()
        fake_session_store.deposit_answer_by_session_id.return_value = True

        with patch("api.routes.conversations.get_store", return_value=mock_store), \
             patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=fake_session_store):
            response = client.post(
                "/api/conversations/conv-403/answer",
                json={"answer": "the lifeos repo"},
            )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        fake_session_store.deposit_answer_by_session_id.assert_called_once_with(
            "sess_abc", "the lifeos repo"
        )

    def test_answer_empty_is_400(self, client, mock_store):
        mock_store.get_conversation.return_value = self._linked_conversation()
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations/conv-403/answer", json={"answer": "   "}
            )
        assert response.status_code == 400

    def test_answer_conversation_not_found_is_404(self, client, mock_store):
        mock_store.get_conversation.return_value = None
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations/missing/answer", json={"answer": "hi"}
            )
        assert response.status_code == 404

    def test_answer_no_spawned_session_is_409(self, client, mock_store, sample_conversation):
        # sample_conversation has agent_session_id=None (no spawned session).
        mock_store.get_conversation.return_value = sample_conversation
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.post(
                "/api/conversations/conv-123/answer", json={"answer": "hi"}
            )
        assert response.status_code == 409
        assert "no spawned session" in response.json()["detail"].lower()

    def test_answer_no_open_question_is_409(self, client, mock_store):
        """Session exists but has no open question (never asked, already
        answered, or timed out) → 409, idempotent, never crashes."""
        mock_store.get_conversation.return_value = self._linked_conversation()
        fake_session_store = MagicMock()
        fake_session_store.deposit_answer_by_session_id.return_value = False
        with patch("api.routes.conversations.get_store", return_value=mock_store), \
             patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=fake_session_store):
            response = client.post(
                "/api/conversations/conv-403/answer", json={"answer": "hi"}
            )
        assert response.status_code == 409
        assert "no open question" in response.json()["detail"].lower()
        # Nothing echoed into the thread when there was nothing to answer.
        mock_store.add_message.assert_not_called()


@pytest.mark.unit
class TestPendingQuestionSurfacing:
    """GET /api/conversations/{id} surfaces an open [CLARIFY]/[GOAL]."""

    def test_pending_question_surfaced_when_open(self, client, mock_store, sample_messages):
        now = datetime.now()
        conv = Conversation(
            id="conv-403", title="Doctor", created_at=now, updated_at=now,
            message_count=1, persona_id="doctor", agent_session_id="sess_abc",
        )
        mock_store.get_conversation.return_value = conv
        mock_store.get_messages.return_value = sample_messages
        fake_session_store = MagicMock()
        fake_session_store.get_open_question_by_session_id.return_value = {
            "question": "Which repo?", "kind": "clarification",
        }
        with patch("api.routes.conversations.get_store", return_value=mock_store), \
             patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=fake_session_store):
            response = client.get("/api/conversations/conv-403")

        assert response.status_code == 200
        pq = response.json()["pending_question"]
        assert pq["session_id"] == "sess_abc"
        assert pq["question"] == "Which repo?"
        assert pq["kind"] == "clarification"

    def test_no_pending_question_when_unlinked(
        self, client, mock_store, sample_conversation, sample_messages
    ):
        """A conversation with no spawned session never touches SessionStore
        and reports pending_question=null (existing behavior unchanged)."""
        mock_store.get_conversation.return_value = sample_conversation
        mock_store.get_messages.return_value = sample_messages
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            response = client.get("/api/conversations/conv-123")
        assert response.status_code == 200
        assert response.json()["pending_question"] is None
        # No linked session → not active, so the client's result poll won't run.
        assert response.json()["agent_session_active"] is False


@pytest.mark.unit
class TestAgentSessionActive:
    """GET /api/conversations/{id} reports whether the spawned session is still
    running (#311) so the client's result-streaming poll can terminate."""

    def _linked_conversation(self):
        now = datetime.now()
        return Conversation(
            id="conv-403", title="Doctor", created_at=now, updated_at=now,
            message_count=1, persona_id="doctor", agent_session_id="sess_abc",
        )

    def _get(self, client, mock_store, fake_session_store):
        mock_store.get_conversation.return_value = self._linked_conversation()
        mock_store.get_messages.return_value = []
        with patch("api.routes.conversations.get_store", return_value=mock_store), \
             patch("api.services.agent_worker.session_store.SessionStore",
                   return_value=fake_session_store):
            return client.get("/api/conversations/conv-403")

    def test_active_when_session_running(self, client, mock_store):
        """A linked session with a non-terminal status reports active=True."""
        fake_session_store = MagicMock()
        fake_session_store.get_open_question_by_session_id.return_value = None
        fake_session_store.get_by_session_id.return_value = MagicMock(status="running")
        response = self._get(client, mock_store, fake_session_store)
        assert response.status_code == 200
        assert response.json()["agent_session_active"] is True

    def test_inactive_when_session_terminal(self, client, mock_store):
        """A linked session that reached a terminal status reports active=False
        so the client stops polling."""
        fake_session_store = MagicMock()
        fake_session_store.get_open_question_by_session_id.return_value = None
        fake_session_store.get_by_session_id.return_value = MagicMock(status="completed")
        response = self._get(client, mock_store, fake_session_store)
        assert response.status_code == 200
        assert response.json()["agent_session_active"] is False

    def test_inactive_when_session_row_missing(self, client, mock_store):
        """A linked session id with no row (never created / pruned) is treated
        as not-active — the client must not poll forever for a ghost."""
        fake_session_store = MagicMock()
        fake_session_store.get_open_question_by_session_id.return_value = None
        fake_session_store.get_by_session_id.return_value = None
        response = self._get(client, mock_store, fake_session_store)
        assert response.status_code == 200
        assert response.json()["agent_session_active"] is False
