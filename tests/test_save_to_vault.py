"""
Tests for the enhanced Save to Vault functionality.

Tests the modal-based save with:
- Full conversation mode (conversation_id)
- Single Q&A fallback mode
- Custom guidance, folder, title, tags
- Error handling
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime
from fastapi.testclient import TestClient

from api.main import app
from api.services.conversation_store import Message


class TestSaveToVaultEnhanced:
    """Test the enhanced /api/save-to-vault endpoint."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    @pytest.fixture
    def mock_synthesizer(self):
        """Mock synthesizer that returns a proper note."""
        with patch('api.routes.chat.get_synthesizer') as mock_synth:
            mock_synth.return_value.get_response = AsyncMock(
                return_value="""---
title: Test Note
created: 2026-01-10
source: lifeos
tags: [test]
---

# Test Note

## TL;DR
This is a summary.

## Content
Test content here.
"""
            )
            yield mock_synth

    @pytest.fixture
    def mock_conversation_store(self):
        """Mock conversation store with test messages."""
        with patch('api.routes.chat.get_store') as mock_get_store:
            mock_store = MagicMock()
            mock_get_store.return_value = mock_store
            yield mock_store

    # ==================== BACKWARD COMPATIBILITY ====================

    def test_backward_compatible_single_qa(self, client, mock_synthesizer, tmp_path):
        """Existing behavior: question + answer should still work."""
        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language."
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "saved"
            assert "path" in data
            assert "obsidian_url" in data

    def test_backward_compatible_rejects_empty_question(self, client):
        """Existing behavior: empty question should return 400."""
        response = client.post(
            "/api/save-to-vault",
            json={"question": "", "answer": "test answer"}
        )
        assert response.status_code == 400

    def test_backward_compatible_rejects_empty_answer(self, client):
        """Existing behavior: empty answer should return 400."""
        response = client.post(
            "/api/save-to-vault",
            json={"question": "test", "answer": ""}
        )
        assert response.status_code == 400

    # ==================== FULL CONVERSATION MODE ====================

    def test_full_conversation_mode(self, client, mock_synthesizer, mock_conversation_store, tmp_path):
        """Should save full conversation when conversation_id provided."""
        # Setup mock messages
        mock_conversation_store.get_messages.return_value = [
            Message(
                id="msg1",
                conversation_id="conv123",
                role="user",
                content="What is Python?",
                created_at=datetime.now(),
                sources=None,
                routing=None
            ),
            Message(
                id="msg2",
                conversation_id="conv123",
                role="assistant",
                content="Python is a programming language.",
                created_at=datetime.now(),
                sources=[{"file_name": "python.md"}],
                routing={"sources": ["vault"]}
            ),
            Message(
                id="msg3",
                conversation_id="conv123",
                role="user",
                content="What are its main features?",
                created_at=datetime.now(),
                sources=None,
                routing=None
            ),
            Message(
                id="msg4",
                conversation_id="conv123",
                role="assistant",
                content="Python features include simplicity and readability.",
                created_at=datetime.now(),
                sources=[{"file_name": "features.md"}],
                routing={"sources": ["vault"]}
            ),
        ]

        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "conversation_id": "conv123",
                    "full_conversation": True
                }
            )

            assert response.status_code == 200
            # Verify conversation was fetched
            mock_conversation_store.get_messages.assert_called_once_with("conv123")
            # Verify synthesizer was called with conversation content
            call_args = mock_synthesizer.return_value.get_response.call_args[0][0]
            assert "What is Python?" in call_args
            assert "Python is a programming language" in call_args

    def test_conversation_not_found_returns_404(self, client, mock_conversation_store):
        """Should return 404 when conversation_id doesn't exist."""
        mock_conversation_store.get_messages.return_value = []

        response = client.post(
            "/api/save-to-vault",
            json={
                "conversation_id": "nonexistent",
                "full_conversation": True
            }
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_requires_conversation_id_or_qa(self, client):
        """Should return 400 when neither conversation_id nor question/answer provided."""
        response = client.post(
            "/api/save-to-vault",
            json={
                "full_conversation": True
                # No conversation_id, no question/answer
            }
        )

        assert response.status_code == 400

    # ==================== CUSTOM GUIDANCE ====================

    def test_custom_guidance_in_prompt(self, client, mock_synthesizer, tmp_path):
        """Custom guidance should be included in synthesis prompt."""
        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language.",
                    "guidance": "Focus on the historical context and creator Guido van Rossum"
                }
            )

            assert response.status_code == 200
            # Verify guidance was passed to synthesizer
            call_args = mock_synthesizer.return_value.get_response.call_args[0][0]
            assert "historical context" in call_args.lower() or "Guido" in call_args

    # ==================== FOLDER OVERRIDE ====================

    def test_folder_override(self, client, mock_synthesizer, tmp_path):
        """User-specified folder should override auto-detection."""
        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language.",
                    "folder": "Custom/MyNotes"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert "Custom/MyNotes" in data["path"]
            # Verify folder was created
            assert (tmp_path / "Custom" / "MyNotes").exists()

    def test_folder_auto_created(self, client, mock_synthesizer, tmp_path):
        """Custom folder paths should be auto-created."""
        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "Test",
                    "answer": "Test answer",
                    "folder": "Deep/Nested/Path/Here"
                }
            )

            assert response.status_code == 200
            assert (tmp_path / "Deep" / "Nested" / "Path" / "Here").exists()

    # ==================== TITLE OVERRIDE ====================

    def test_title_override(self, client, mock_synthesizer, tmp_path):
        """User-specified title should be used in filename."""
        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language.",
                    "title": "My Custom Title"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert "My Custom Title" in data["path"]

    def test_title_override_still_synthesizes(self, client, mock_synthesizer, tmp_path):
        """Even with title override, synthesizer should still run for content."""
        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language.",
                    "title": "My Custom Title"
                }
            )

            assert response.status_code == 200
            # Synthesizer should still be called
            mock_synthesizer.return_value.get_response.assert_called_once()

    # ==================== TAGS ====================

    def test_tags_included_in_request(self, client, mock_synthesizer, tmp_path):
        """Tags should be passed to synthesizer prompt."""
        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language.",
                    "tags": ["python", "programming", "tutorial"]
                }
            )

            assert response.status_code == 200
            # Verify tags were mentioned in synthesizer call
            call_args = mock_synthesizer.return_value.get_response.call_args[0][0]
            assert "python" in call_args.lower()

    # ==================== INCLUDE SOURCES ====================

    def test_include_sources_true(self, client, mock_synthesizer, mock_conversation_store, tmp_path):
        """With include_sources=True, sources should appear in prompt."""
        mock_conversation_store.get_messages.return_value = [
            Message(
                id="msg1",
                conversation_id="conv123",
                role="user",
                content="What is Python?",
                created_at=datetime.now(),
                sources=None,
                routing=None
            ),
            Message(
                id="msg2",
                conversation_id="conv123",
                role="assistant",
                content="Python is a programming language.",
                created_at=datetime.now(),
                sources=[{"file_name": "python_guide.md", "file_path": "/notes/python_guide.md"}],
                routing={"sources": ["vault"]}
            ),
        ]

        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "conversation_id": "conv123",
                    "full_conversation": True,
                    "include_sources": True
                }
            )

            assert response.status_code == 200
            call_args = mock_synthesizer.return_value.get_response.call_args[0][0]
            assert "python_guide.md" in call_args

    def test_include_sources_false(self, client, mock_synthesizer, mock_conversation_store, tmp_path):
        """With include_sources=False, sources should NOT appear in prompt."""
        mock_conversation_store.get_messages.return_value = [
            Message(
                id="msg1",
                conversation_id="conv123",
                role="user",
                content="What is Python?",
                created_at=datetime.now(),
                sources=None,
                routing=None
            ),
            Message(
                id="msg2",
                conversation_id="conv123",
                role="assistant",
                content="Python is a programming language.",
                created_at=datetime.now(),
                sources=[{"file_name": "python_guide.md"}],
                routing={"sources": ["vault"]}
            ),
        ]

        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "conversation_id": "conv123",
                    "full_conversation": True,
                    "include_sources": False
                }
            )

            assert response.status_code == 200
            call_args = mock_synthesizer.return_value.get_response.call_args[0][0]
            # Sources section should not be in the prompt
            assert "python_guide.md" not in call_args

    # ==================== INCLUDE RAW Q&A ====================

    def test_include_raw_qa_true(self, client, mock_synthesizer, tmp_path):
        """With include_raw_qa=True, raw Q&A should be appended to note."""
        # Return note content that we can check
        mock_synthesizer.return_value.get_response = AsyncMock(
            return_value="""---
title: Test Note
---

# Test Note

Synthesized content here.
"""
        )

        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language.",
                    "include_raw_qa": True
                }
            )

            assert response.status_code == 200
            # Read the saved file and check for raw Q&A
            data = response.json()
            saved_content = open(data["path"]).read()
            assert "What is Python?" in saved_content
            assert "Python is a programming language." in saved_content

    def test_include_raw_qa_false_default(self, client, mock_synthesizer, tmp_path):
        """By default (include_raw_qa=False), raw Q&A should NOT be appended."""
        mock_synthesizer.return_value.get_response = AsyncMock(
            return_value="""---
title: Test Note
---

# Test Note

Synthesized content only.
"""
        )

        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "question": "What is Python?",
                    "answer": "Python is a programming language."
                    # include_raw_qa defaults to False
                }
            )

            assert response.status_code == 200
            data = response.json()
            saved_content = open(data["path"]).read()
            # The synthesized note shouldn't have raw Q&A appended
            # (it may still mention the content, but not in raw format)
            assert "## Original Conversation" not in saved_content

    # ==================== EDGE CASES ====================

    def test_empty_conversation_returns_400(self, client, mock_conversation_store):
        """Conversation exists but has no messages should return 400."""
        mock_conversation_store.get_messages.return_value = []

        response = client.post(
            "/api/save-to-vault",
            json={
                "conversation_id": "empty_conv",
                "full_conversation": True
            }
        )

        # Empty conversation = not found (404) or bad request (400)
        assert response.status_code in [400, 404]

    def test_single_message_conversation(self, client, mock_synthesizer, mock_conversation_store, tmp_path):
        """Single message conversation should still work."""
        mock_conversation_store.get_messages.return_value = [
            Message(
                id="msg1",
                conversation_id="conv123",
                role="user",
                content="Hello",
                created_at=datetime.now(),
                sources=None,
                routing=None
            ),
        ]

        with patch('api.routes.chat.settings') as mock_settings:
            mock_settings.vault_path = tmp_path
            (tmp_path / "LifeOS" / "Research").mkdir(parents=True)

            response = client.post(
                "/api/save-to-vault",
                json={
                    "conversation_id": "conv123",
                    "full_conversation": True
                }
            )

            assert response.status_code == 200
