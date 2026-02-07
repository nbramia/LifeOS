"""
Tests for api/services/conversation_store.py

Tests conversation persistence, message management, and helper functions.
"""
import json
import os
import sqlite3
import tempfile
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from api.services.conversation_store import (
    Conversation,
    Message,
    ConversationStore,
    generate_title,
    format_conversation_history,
    get_store,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_db():
    """Create a temporary database file for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store(temp_db):
    """Create a ConversationStore with a temporary database."""
    return ConversationStore(db_path=temp_db)


@pytest.fixture
def conversation(store):
    """Create a test conversation."""
    return store.create_conversation(title="Test Conversation")


@pytest.fixture
def conversation_with_messages(store, conversation):
    """Create a conversation with multiple messages."""
    store.add_message(conversation.id, "user", "Hello, how are you?")
    store.add_message(conversation.id, "assistant", "I'm doing well, thanks!")
    store.add_message(conversation.id, "user", "Great to hear!")
    return conversation


# =============================================================================
# Dataclass Tests
# =============================================================================

@pytest.mark.unit
class TestConversationDataclass:
    """Tests for the Conversation dataclass."""

    def test_create_conversation_dataclass(self):
        """Test creating a Conversation instance."""
        now = datetime.now()
        conv = Conversation(
            id="test-id",
            title="Test Title",
            created_at=now,
            updated_at=now,
            message_count=5
        )
        assert conv.id == "test-id"
        assert conv.title == "Test Title"
        assert conv.created_at == now
        assert conv.updated_at == now
        assert conv.message_count == 5

    def test_conversation_default_message_count(self):
        """Test that message_count defaults to 0."""
        now = datetime.now()
        conv = Conversation(
            id="test-id",
            title="Test",
            created_at=now,
            updated_at=now
        )
        assert conv.message_count == 0


@pytest.mark.unit
class TestMessageDataclass:
    """Tests for the Message dataclass."""

    def test_create_message_dataclass(self):
        """Test creating a Message instance."""
        now = datetime.now()
        msg = Message(
            id="msg-id",
            conversation_id="conv-id",
            role="user",
            content="Hello world",
            created_at=now,
            sources=[{"title": "doc1"}],
            routing={"handler": "general"}
        )
        assert msg.id == "msg-id"
        assert msg.conversation_id == "conv-id"
        assert msg.role == "user"
        assert msg.content == "Hello world"
        assert msg.created_at == now
        assert msg.sources == [{"title": "doc1"}]
        assert msg.routing == {"handler": "general"}

    def test_message_optional_fields_default_none(self):
        """Test that sources and routing default to None."""
        now = datetime.now()
        msg = Message(
            id="msg-id",
            conversation_id="conv-id",
            role="assistant",
            content="Test",
            created_at=now
        )
        assert msg.sources is None
        assert msg.routing is None


# =============================================================================
# ConversationStore Tests
# =============================================================================

@pytest.mark.unit
class TestConversationStoreInit:
    """Tests for ConversationStore initialization."""

    def test_init_creates_tables(self, temp_db):
        """Test that initialization creates required tables."""
        store = ConversationStore(db_path=temp_db)

        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        assert "conversations" in tables
        assert "messages" in tables

    def test_init_creates_index(self, temp_db):
        """Test that initialization creates the messages index."""
        store = ConversationStore(db_path=temp_db)

        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        indexes = [row[0] for row in cursor.fetchall()]
        conn.close()

        assert "idx_messages_conversation" in indexes

    def test_init_idempotent(self, temp_db):
        """Test that calling init multiple times doesn't cause errors."""
        store1 = ConversationStore(db_path=temp_db)
        store2 = ConversationStore(db_path=temp_db)

        # Should not raise any errors
        conv = store2.create_conversation(title="Test")
        assert conv.title == "Test"


@pytest.mark.unit
class TestCreateConversation:
    """Tests for create_conversation method."""

    def test_create_with_title(self, store):
        """Test creating a conversation with a title."""
        conv = store.create_conversation(title="My Conversation")

        assert conv.id is not None
        assert len(conv.id) == 36  # UUID length
        assert conv.title == "My Conversation"
        assert conv.message_count == 0
        assert isinstance(conv.created_at, datetime)
        assert isinstance(conv.updated_at, datetime)

    def test_create_without_title(self, store):
        """Test creating a conversation without a title uses default."""
        conv = store.create_conversation()
        assert conv.title == "New Conversation"

    def test_create_with_none_title(self, store):
        """Test creating a conversation with None title uses default."""
        conv = store.create_conversation(title=None)
        assert conv.title == "New Conversation"

    def test_create_persists_to_database(self, store, temp_db):
        """Test that created conversation is persisted."""
        conv = store.create_conversation(title="Persisted")

        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT id, title FROM conversations WHERE id = ?",
            (conv.id,)
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == conv.id
        assert row[1] == "Persisted"

    def test_create_multiple_conversations(self, store):
        """Test creating multiple conversations generates unique IDs."""
        conv1 = store.create_conversation(title="First")
        conv2 = store.create_conversation(title="Second")
        conv3 = store.create_conversation(title="Third")

        ids = {conv1.id, conv2.id, conv3.id}
        assert len(ids) == 3  # All unique


@pytest.mark.unit
class TestGetConversation:
    """Tests for get_conversation method."""

    def test_get_existing_conversation(self, store, conversation):
        """Test retrieving an existing conversation."""
        retrieved = store.get_conversation(conversation.id)

        assert retrieved is not None
        assert retrieved.id == conversation.id
        assert retrieved.title == conversation.title
        assert retrieved.message_count == 0

    def test_get_nonexistent_conversation(self, store):
        """Test retrieving a non-existent conversation returns None."""
        result = store.get_conversation("nonexistent-id")
        assert result is None

    def test_get_conversation_with_messages(self, store, conversation_with_messages):
        """Test that message_count is accurate."""
        retrieved = store.get_conversation(conversation_with_messages.id)
        assert retrieved.message_count == 3

    def test_get_conversation_timestamps(self, store, conversation):
        """Test that timestamps are properly parsed."""
        retrieved = store.get_conversation(conversation.id)

        assert isinstance(retrieved.created_at, datetime)
        assert isinstance(retrieved.updated_at, datetime)


@pytest.mark.unit
class TestListConversations:
    """Tests for list_conversations method."""

    def test_list_empty(self, store):
        """Test listing when no conversations exist."""
        result = store.list_conversations()
        assert result == []

    def test_list_multiple_conversations(self, store):
        """Test listing multiple conversations."""
        store.create_conversation(title="First")
        store.create_conversation(title="Second")
        store.create_conversation(title="Third")

        result = store.list_conversations()

        assert len(result) == 3
        titles = {c.title for c in result}
        assert titles == {"First", "Second", "Third"}

    def test_list_ordered_by_updated_at_desc(self, store):
        """Test that conversations are ordered by updated_at descending."""
        conv1 = store.create_conversation(title="Old")
        conv2 = store.create_conversation(title="Middle")
        conv3 = store.create_conversation(title="New")

        # Add a message to conv1 to make it the most recently updated
        store.add_message(conv1.id, "user", "Update me")

        result = store.list_conversations()

        # conv1 should be first (most recently updated)
        assert result[0].id == conv1.id
        assert result[0].title == "Old"

    def test_list_with_limit(self, store):
        """Test limiting the number of conversations returned."""
        for i in range(10):
            store.create_conversation(title=f"Conv {i}")

        result = store.list_conversations(limit=5)
        assert len(result) == 5

    def test_list_includes_message_counts(self, store):
        """Test that message counts are included."""
        conv = store.create_conversation(title="With Messages")
        store.add_message(conv.id, "user", "Hello")
        store.add_message(conv.id, "assistant", "Hi there")

        result = store.list_conversations()

        found = next((c for c in result if c.id == conv.id), None)
        assert found is not None
        assert found.message_count == 2


@pytest.mark.unit
class TestDeleteConversation:
    """Tests for delete_conversation method."""

    def test_delete_existing_conversation(self, store, conversation):
        """Test deleting an existing conversation."""
        result = store.delete_conversation(conversation.id)

        assert result is True
        assert store.get_conversation(conversation.id) is None

    def test_delete_nonexistent_conversation(self, store):
        """Test deleting a non-existent conversation returns False."""
        result = store.delete_conversation("nonexistent-id")
        assert result is False

    def test_delete_removes_messages(self, store, conversation_with_messages, temp_db):
        """Test that deleting a conversation removes its messages."""
        conv_id = conversation_with_messages.id

        # Verify messages exist before delete
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conv_id,)
        )
        count_before = cursor.fetchone()[0]
        conn.close()
        assert count_before == 3

        # Delete conversation
        store.delete_conversation(conv_id)

        # Verify messages are gone
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conv_id,)
        )
        count_after = cursor.fetchone()[0]
        conn.close()
        assert count_after == 0


@pytest.mark.unit
class TestAddMessage:
    """Tests for add_message method."""

    def test_add_user_message(self, store, conversation):
        """Test adding a user message."""
        msg = store.add_message(
            conversation.id,
            role="user",
            content="Hello, world!"
        )

        assert msg.id is not None
        assert len(msg.id) == 36
        assert msg.conversation_id == conversation.id
        assert msg.role == "user"
        assert msg.content == "Hello, world!"
        assert isinstance(msg.created_at, datetime)
        assert msg.sources is None
        assert msg.routing is None

    def test_add_assistant_message(self, store, conversation):
        """Test adding an assistant message."""
        msg = store.add_message(
            conversation.id,
            role="assistant",
            content="Hello! How can I help?"
        )

        assert msg.role == "assistant"
        assert msg.content == "Hello! How can I help?"

    def test_add_message_with_sources(self, store, conversation):
        """Test adding a message with sources."""
        sources = [
            {"title": "Document 1", "path": "/docs/1.md"},
            {"title": "Document 2", "path": "/docs/2.md"}
        ]

        msg = store.add_message(
            conversation.id,
            role="assistant",
            content="Based on the documents...",
            sources=sources
        )

        assert msg.sources == sources

    def test_add_message_with_routing(self, store, conversation):
        """Test adding a message with routing metadata."""
        routing = {"handler": "search", "confidence": 0.95}

        msg = store.add_message(
            conversation.id,
            role="user",
            content="Search for something",
            routing=routing
        )

        assert msg.routing == routing

    def test_add_message_updates_conversation_timestamp(self, store, conversation):
        """Test that adding a message updates the conversation's updated_at."""
        original_updated = store.get_conversation(conversation.id).updated_at

        # Small delay to ensure timestamp difference
        import time
        time.sleep(0.01)

        store.add_message(conversation.id, "user", "New message")

        updated_conv = store.get_conversation(conversation.id)
        assert updated_conv.updated_at > original_updated

    def test_add_message_persists_sources_as_json(self, store, conversation, temp_db):
        """Test that sources are stored as JSON in the database."""
        sources = [{"key": "value"}]
        msg = store.add_message(
            conversation.id,
            role="assistant",
            content="Test",
            sources=sources
        )

        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT sources FROM messages WHERE id = ?",
            (msg.id,)
        )
        stored_sources = cursor.fetchone()[0]
        conn.close()

        assert json.loads(stored_sources) == sources


@pytest.mark.unit
class TestGetMessages:
    """Tests for get_messages method."""

    def test_get_messages_empty(self, store, conversation):
        """Test getting messages from an empty conversation."""
        messages = store.get_messages(conversation.id)
        assert messages == []

    def test_get_messages_chronological_order(self, store, conversation):
        """Test that messages are returned in chronological order."""
        store.add_message(conversation.id, "user", "First")
        store.add_message(conversation.id, "assistant", "Second")
        store.add_message(conversation.id, "user", "Third")

        messages = store.get_messages(conversation.id)

        assert len(messages) == 3
        assert messages[0].content == "First"
        assert messages[1].content == "Second"
        assert messages[2].content == "Third"

    def test_get_messages_with_limit(self, store, conversation):
        """Test getting last N messages with limit."""
        for i in range(10):
            store.add_message(conversation.id, "user", f"Message {i}")

        messages = store.get_messages(conversation.id, limit=3)

        assert len(messages) == 3
        # Should be the last 3 messages
        assert messages[0].content == "Message 7"
        assert messages[1].content == "Message 8"
        assert messages[2].content == "Message 9"

    def test_get_messages_preserves_sources(self, store, conversation):
        """Test that sources are properly deserialized."""
        sources = [{"doc": "test.md", "score": 0.9}]
        store.add_message(
            conversation.id,
            role="assistant",
            content="Answer",
            sources=sources
        )

        messages = store.get_messages(conversation.id)

        assert messages[0].sources == sources

    def test_get_messages_preserves_routing(self, store, conversation):
        """Test that routing is properly deserialized."""
        routing = {"handler": "crm", "person_id": 123}
        store.add_message(
            conversation.id,
            role="user",
            content="Question",
            routing=routing
        )

        messages = store.get_messages(conversation.id)

        assert messages[0].routing == routing

    def test_get_messages_nonexistent_conversation(self, store):
        """Test getting messages for a non-existent conversation."""
        messages = store.get_messages("nonexistent-id")
        assert messages == []


@pytest.mark.unit
class TestUpdateTitle:
    """Tests for update_title method."""

    def test_update_title_success(self, store, conversation):
        """Test successfully updating a conversation title."""
        result = store.update_title(conversation.id, "New Title")

        assert result is True
        updated = store.get_conversation(conversation.id)
        assert updated.title == "New Title"

    def test_update_title_nonexistent(self, store):
        """Test updating title for non-existent conversation."""
        result = store.update_title("nonexistent-id", "New Title")
        assert result is False

    def test_update_title_updates_timestamp(self, store, conversation):
        """Test that updating title updates the timestamp."""
        original = store.get_conversation(conversation.id)

        import time
        time.sleep(0.01)

        store.update_title(conversation.id, "Updated Title")

        updated = store.get_conversation(conversation.id)
        assert updated.updated_at > original.updated_at


# =============================================================================
# Helper Function Tests
# =============================================================================

@pytest.mark.unit
class TestGenerateTitle:
    """Tests for generate_title function."""

    def test_short_question(self):
        """Test title generation from short question."""
        title = generate_title("What is Python?")
        assert title == "What is Python"  # Question mark removed

    def test_long_question_truncated(self):
        """Test that long questions are truncated."""
        question = "What is the best way to implement a microservices architecture in Python?"
        title = generate_title(question, max_length=30)

        assert len(title) <= 30
        assert not title.endswith(' ')  # No trailing space

    def test_truncate_at_word_boundary(self):
        """Test that truncation happens at word boundary."""
        question = "This is a very long question about something important"
        title = generate_title(question, max_length=25)

        # Should not cut a word in half
        assert title == "This is a very long"

    def test_strips_whitespace(self):
        """Test that whitespace is stripped."""
        title = generate_title("  Hello world  ")
        assert title == "Hello world"

    def test_exact_length_not_truncated(self):
        """Test that titles at exact max_length are not truncated."""
        question = "Exactly fifty characters long right here exactly?"
        title = generate_title(question, max_length=50)
        # Question mark stripped, result is 49 chars
        assert title == "Exactly fifty characters long right here exactly"

    def test_multiple_question_marks(self):
        """Test handling multiple question marks."""
        title = generate_title("What is this???")
        assert title == "What is this"


@pytest.mark.unit
class TestFormatConversationHistory:
    """Tests for format_conversation_history function."""

    def test_empty_messages(self):
        """Test formatting empty message list."""
        result = format_conversation_history([])
        assert result == ""

    def test_format_single_message(self):
        """Test formatting a single message."""
        messages = [
            Message(
                id="1",
                conversation_id="conv",
                role="user",
                content="Hello",
                created_at=datetime.now()
            )
        ]

        result = format_conversation_history(messages)
        assert result == "User: Hello"

    def test_format_multiple_messages(self):
        """Test formatting multiple messages."""
        now = datetime.now()
        messages = [
            Message(id="1", conversation_id="conv", role="user",
                    content="Hi", created_at=now),
            Message(id="2", conversation_id="conv", role="assistant",
                    content="Hello!", created_at=now),
            Message(id="3", conversation_id="conv", role="user",
                    content="How are you?", created_at=now),
        ]

        result = format_conversation_history(messages)

        assert "User: Hi" in result
        assert "Assistant: Hello!" in result
        assert "User: How are you?" in result
        # Messages separated by double newlines
        assert "\n\n" in result

    def test_format_respects_max_tokens(self):
        """Test that formatting respects token limit."""
        now = datetime.now()
        # Create messages with known length
        messages = [
            Message(id="1", conversation_id="conv", role="user",
                    content="A" * 1000, created_at=now),
            Message(id="2", conversation_id="conv", role="assistant",
                    content="B" * 1000, created_at=now),
            Message(id="3", conversation_id="conv", role="user",
                    content="C" * 1000, created_at=now),
        ]

        # max_tokens=100 means max_chars=400
        result = format_conversation_history(messages, max_tokens=100)

        # Should not include all messages
        assert len(result) <= 400 + 100  # Some buffer for labels

    def test_role_labels(self):
        """Test that correct role labels are used."""
        now = datetime.now()
        messages = [
            Message(id="1", conversation_id="conv", role="user",
                    content="Question", created_at=now),
            Message(id="2", conversation_id="conv", role="assistant",
                    content="Answer", created_at=now),
        ]

        result = format_conversation_history(messages)

        assert result.startswith("User:")
        assert "Assistant:" in result


# =============================================================================
# Singleton Tests
# =============================================================================

@pytest.mark.unit
class TestGetStore:
    """Tests for get_store singleton function."""

    def test_get_store_returns_instance(self):
        """Test that get_store returns a ConversationStore."""
        # Reset singleton for test
        import api.services.conversation_store as module
        module._store_instance = None

        store = get_store()
        assert isinstance(store, ConversationStore)

    def test_get_store_returns_same_instance(self):
        """Test that get_store returns the same instance."""
        # Reset singleton for test
        import api.services.conversation_store as module
        module._store_instance = None

        store1 = get_store()
        store2 = get_store()

        assert store1 is store2


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.unit
class TestConversationWorkflow:
    """Integration tests for typical conversation workflows."""

    def test_complete_conversation_workflow(self, store):
        """Test a complete conversation workflow."""
        # Create conversation
        conv = store.create_conversation(title="Help with Python")
        assert conv.id is not None

        # Add messages
        store.add_message(conv.id, "user", "How do I read a file in Python?")
        store.add_message(
            conv.id,
            "assistant",
            "You can use the open() function with a context manager.",
            sources=[{"doc": "python_basics.md"}]
        )
        store.add_message(conv.id, "user", "Thanks!")

        # Verify conversation state
        retrieved = store.get_conversation(conv.id)
        assert retrieved.message_count == 3

        # Get messages
        messages = store.get_messages(conv.id)
        assert len(messages) == 3
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert messages[1].sources is not None

        # Update title
        store.update_title(conv.id, "Python File I/O")
        updated = store.get_conversation(conv.id)
        assert updated.title == "Python File I/O"

        # Delete
        result = store.delete_conversation(conv.id)
        assert result is True
        assert store.get_conversation(conv.id) is None

    def test_multiple_conversations_isolation(self, store):
        """Test that multiple conversations are isolated."""
        conv1 = store.create_conversation(title="Conversation 1")
        conv2 = store.create_conversation(title="Conversation 2")

        store.add_message(conv1.id, "user", "Message in conv1")
        store.add_message(conv2.id, "user", "Message in conv2")
        store.add_message(conv2.id, "user", "Another in conv2")

        messages1 = store.get_messages(conv1.id)
        messages2 = store.get_messages(conv2.id)

        assert len(messages1) == 1
        assert len(messages2) == 2

        # Delete conv1 doesn't affect conv2
        store.delete_conversation(conv1.id)

        messages2_after = store.get_messages(conv2.id)
        assert len(messages2_after) == 2
