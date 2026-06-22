"""
Tests for Chat API endpoints with streaming support.
P2.1/P2.2 Acceptance Criteria:
- Streaming endpoint returns SSE format
- Sources are included in stream
- Save to vault creates proper note structure
- Empty requests return 400 errors
"""
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from api.main import app

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow


class TestAskStreamEndpoint:
    """Test the /api/ask/stream endpoint."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_stream_endpoint_exists(self, client):
        """Stream endpoint should exist and accept POST."""
        response = client.post("/api/ask/stream", json={"question": "test"})
        assert response.status_code != 404
        assert response.status_code != 405

    def test_stream_rejects_empty_question(self, client):
        """Should return 400 for empty question."""
        response = client.post("/api/ask/stream", json={"question": ""})
        assert response.status_code == 400

    def test_stream_rejects_whitespace_question(self, client):
        """Should return 400 for whitespace-only question."""
        response = client.post("/api/ask/stream", json={"question": "   "})
        assert response.status_code == 400

    def test_stream_returns_event_stream(self, client):
        """Response should be text/event-stream."""
        with patch('api.routes.chat.VectorStore') as mock_vs:
            mock_vs.return_value.search.return_value = []

            with patch('api.routes.chat.get_synthesizer') as mock_synth:
                async def mock_stream(*args, **kwargs):
                    yield "Test response"
                mock_synth.return_value.stream_response = mock_stream

                response = client.post(
                    "/api/ask/stream",
                    json={"question": "test question"}
                )

                assert response.headers.get("content-type", "").startswith("text/event-stream")

    def test_stream_includes_sources_event(self, client):
        """Stream should include sources in SSE format."""
        with patch('api.routes.chat.VectorStore') as mock_vs:
            mock_vs.return_value.search.return_value = [
                {
                    'content': 'Test content',
                    'metadata': {
                        'file_name': 'test.md',
                        'file_path': '/vault/test.md'
                    }
                }
            ]

            with patch('api.routes.chat.get_synthesizer') as mock_synth:
                async def mock_stream(*args, **kwargs):
                    yield "Response"
                mock_synth.return_value.stream_response = mock_stream

                response = client.post(
                    "/api/ask/stream",
                    json={"question": "test", "include_sources": True}
                )

                # Parse SSE response
                content = response.text
                assert "data:" in content

    def test_stream_claude_code_model_hands_off(self, client):
        """Selecting the 'claude_code' model routes the turn to the engine
        handoff (a claude_intent event) rather than running the agentic loop."""
        with patch('api.routes.chat.classify_action_intent', new_callable=AsyncMock) as mock_classify:
            response = client.post(
                "/api/ask/stream",
                json={"question": "refactor the parser", "model_override": "claude_code"},
            )
            body = response.text

        assert response.status_code == 200
        # Emits the engine-handoff intent with the full question as the task...
        assert '"type": "claude_intent"' in body
        assert '"engine": "claude_code"' in body
        assert "refactor the parser" in body
        # ...and short-circuits before classification and the agentic loop, so no
        # inline answer is synthesized.
        assert '"type": "content"' not in body
        mock_classify.assert_not_called()


class TestChatRequestValidation:
    """Test request validation for chat endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_stream_handles_missing_include_sources(self, client):
        """Should default include_sources to True."""
        with patch('api.routes.chat.VectorStore') as mock_vs:
            mock_vs.return_value.search.return_value = []

            with patch('api.routes.chat.get_synthesizer') as mock_synth:
                async def mock_stream(*args, **kwargs):
                    yield "Test"
                mock_synth.return_value.stream_response = mock_stream

                response = client.post(
                    "/api/ask/stream",
                    json={"question": "test"}
                )

                assert response.status_code == 200

# Unit tests for compose intent detection (no TestClient needed)
class TestComposeIntentDetection:
    """Test the compose intent detection helper function."""

    def test_detects_draft_email(self):
        """Should detect 'draft an email' requests."""
        from api.services.chat_helpers import detect_compose_intent

        assert detect_compose_intent("draft an email to John about the meeting")
        assert detect_compose_intent("Draft email to Sarah")
        assert detect_compose_intent("draft a message to the team")

    def test_detects_compose_email(self):
        """Should detect 'compose' requests."""
        from api.services.chat_helpers import detect_compose_intent

        assert detect_compose_intent("compose an email to Kevin")
        assert detect_compose_intent("compose email about the project")

    def test_detects_write_email(self):
        """Should detect 'write' requests."""
        from api.services.chat_helpers import detect_compose_intent

        assert detect_compose_intent("write an email to the team")
        assert detect_compose_intent("write email about budget")

    def test_detects_email_to_pattern(self):
        """Should detect 'email to' pattern."""
        from api.services.chat_helpers import detect_compose_intent

        assert detect_compose_intent("email to john@example.com about the project")
        assert detect_compose_intent("write to Sarah about the deadline")
        assert detect_compose_intent("draft to Kevin following up on our call")

    def test_does_not_detect_search_queries(self):
        """Should NOT detect search/retrieve queries as compose intent."""
        from api.services.chat_helpers import detect_compose_intent

        assert not detect_compose_intent("find my email about the meeting")
        assert not detect_compose_intent("search emails from John")
        assert not detect_compose_intent("what emails did I get yesterday")
        assert not detect_compose_intent("show me the email thread")

    def test_does_not_detect_unrelated_queries(self):
        """Should NOT detect unrelated queries."""
        from api.services.chat_helpers import detect_compose_intent

        assert not detect_compose_intent("what's on my calendar")
        assert not detect_compose_intent("tell me about Kevin")
        assert not detect_compose_intent("search my notes for project updates")


class TestHandoffEndpoint:
    """Test the /api/chat/handoff engine-handoff endpoint (#305b/c, web surface)."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_handoff_spawns_codex(self, client):
        with patch(
            "api.services.agent_worker.codex_spawn.spawn_codex_session",
            return_value={"ok": True, "session_id": "sess_codex_abc123"},
        ) as m:
            r = client.post("/api/chat/handoff", json={"engine": "codex", "task": "add the world cup games"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["engine"] == "codex"
        assert d["session_id"] == "sess_codex_abc123"
        assert m.called
        # The real task (not a directive phrase) was forwarded.
        assert m.call_args.args[1] == "add the world cup games"

    def test_handoff_spawns_claude_code(self, client):
        with patch(
            "api.services.agent_worker.claude_code_spawn.spawn_claude_code_session",
            return_value={"ok": True, "session_id": "sess_cc_xyz"},
        ) as m:
            r = client.post("/api/chat/handoff", json={"engine": "claude_code", "task": "refactor the parser"})
        assert r.status_code == 200
        assert r.json()["engine"] == "claude_code"
        assert m.called

    def test_handoff_rejects_unknown_engine(self, client):
        r = client.post("/api/chat/handoff", json={"engine": "gpt", "task": "x"})
        assert r.status_code == 400

    def test_handoff_rejects_empty_task(self, client):
        r = client.post("/api/chat/handoff", json={"engine": "codex", "task": "   "})
        assert r.status_code == 400

    def test_handoff_surfaces_spawn_failure(self, client):
        with patch(
            "api.services.agent_worker.codex_spawn.spawn_codex_session",
            return_value={"ok": False, "error": "no prompt"},
        ):
            r = client.post("/api/chat/handoff", json={"engine": "codex", "task": "do a thing"})
        assert r.status_code == 500
