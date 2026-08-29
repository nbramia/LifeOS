"""
Tests for Chat API endpoints with streaming support.
P2.1/P2.2 Acceptance Criteria:
- Streaming endpoint returns SSE format
- Sources are included in stream
- Save to vault creates proper note structure
- Empty requests return 400 errors
"""
import json

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
        assert "Question cannot be empty" in str(response.json()["detail"])

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


class TestBackendTaggingField:
    """AskStreamRequest.backend (#596): tags a newly created conversation's
    sidebar-filtering label ONLY — never routing, model selection, or persona
    resolution. Omitted reproduces today's tagging ("lifeos") exactly."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @staticmethod
    def _ask(client, extra_body=None):
        body = {"question": "hello there"}
        if extra_body:
            body.update(extra_body)
        with patch('api.routes.chat.VectorStore') as mock_vs, \
                patch('api.routes.chat.get_synthesizer') as mock_synth:
            mock_vs.return_value.search.return_value = []

            async def mock_stream(*args, **kwargs):
                yield "Test response"
            mock_synth.return_value.stream_response = mock_stream

            response = client.post("/api/ask/stream", json=body)
        return response

    @staticmethod
    def _conversation_id(response_text):
        import re
        m = re.search(r'"conversation_id": "([^"]+)"', response_text)
        assert m, f"no conversation_id event in: {response_text!r}"
        return m.group(1)

    def _conversation(self, tmp_path, conv_id):
        from api.services.conversation_store import ConversationStore
        store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
        conv = store.get_conversation(conv_id)
        assert conv is not None
        return conv

    def test_omitted_backend_tags_conversation_lifeos(self, client, tmp_path):
        response = self._ask(client)
        assert response.status_code == 200
        conv = self._conversation(tmp_path, self._conversation_id(response.text))
        assert conv.backend == "lifeos"

    def test_explicit_backend_tags_conversation(self, client, tmp_path):
        response = self._ask(client, {"backend": "hermes"})
        assert response.status_code == 200
        conv = self._conversation(tmp_path, self._conversation_id(response.text))
        assert conv.backend == "hermes"

    @staticmethod
    def _events(response_text, *, drop_types=("conversation_id", "perf_trace")):
        """Parsed SSE `data:` events, dropping per-request-instance noise
        (the minted conversation id, and perf-trace timings that are never
        deterministic) so two otherwise-equivalent turns compare equal."""
        events = []
        for line in response_text.split("\n"):
            if not line.startswith("data: "):
                continue
            data = json.loads(line[len("data: "):])
            if data.get("type") not in drop_types:
                events.append(data)
        return events

    def test_backend_field_does_not_alter_response_or_persona(self, client, tmp_path):
        """Same question, same persona (default primary) — only the `backend`
        field differs. The response's content/routing/persona resolution must
        be identical; only the tag on the (different) created conversation
        should differ."""
        plain = self._ask(client)
        tagged = self._ask(client, {"backend": "hermes"})

        assert self._events(plain.text) == self._events(tagged.text)

        plain_conv = self._conversation(tmp_path, self._conversation_id(plain.text))
        tagged_conv = self._conversation(tmp_path, self._conversation_id(tagged.text))
        assert plain_conv.persona_id == tagged_conv.persona_id == "primary"
        assert plain_conv.backend == "lifeos"
        assert tagged_conv.backend == "hermes"


class TestConversationTitlingSeam:
    """Verifies `/api/ask/stream` invokes the shared post-turn titling seam
    (api/services/conversation_titler.py) once the turn finishes — the
    seam's own behavior (not-before-2nd-message, sanitization,
    failure-safety) is unit-tested directly in test_conversation_titler.py;
    this only pins that the native chat path calls it, with the turn's own
    conversation id, alongside (not instead of) the pre-existing
    first-message truncation title (`generate_title()`)."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_schedule_retitle_called_with_the_turns_conversation_id(self, client):
        with patch('api.routes.chat.VectorStore') as mock_vs, \
                patch('api.routes.chat.get_synthesizer') as mock_synth, \
                patch('api.routes.chat.schedule_retitle') as mock_retitle:
            mock_vs.return_value.search.return_value = []

            async def mock_stream(*args, **kwargs):
                yield "Test response"
            mock_synth.return_value.stream_response = mock_stream

            response = client.post("/api/ask/stream", json={"question": "test question"})

        assert response.status_code == 200
        import re
        m = re.search(r'"conversation_id": "([^"]+)"', response.text)
        assert m, f"no conversation_id event in: {response.text!r}"
        conv_id = m.group(1)

        mock_retitle.assert_called_once_with(conv_id)


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


class TestAskStreamRemoteModelOverride:
    """model_override="remote" (#654): dispatches to the configured paid
    OpenAI-compatible provider only when it's actually configured -- the
    picker hides the option otherwise, but a raw API caller could still send
    it, and an unconfigured pick must fall back to auto rather than being
    sent to Anthropic as a model literally named "remote" (which would 404)."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @staticmethod
    def _fake_loop(captured):
        from types import SimpleNamespace

        async def fake_loop(**kwargs):
            captured.clear()
            captured.update(kwargs)
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                unpriced=False, model="m", tool_calls_log=[], full_text="ok")}
        return fake_loop

    @staticmethod
    async def _fake_classify(*a, **k):
        return None

    def test_configured_remote_pick_dispatches_force_remote(self, client, monkeypatch):
        import api.services.agent_loop as agent_loop_mod
        from config.settings import settings

        captured = {}
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", self._fake_loop(captured))
        monkeypatch.setattr("api.routes.chat.classify_action_intent", self._fake_classify)
        monkeypatch.setattr(settings, "remote_llm_base_url", "http://fake-remote", raising=False)
        monkeypatch.setattr(settings, "remote_llm_model", "accounts/fireworks/models/x", raising=False)
        monkeypatch.setattr(settings, "remote_llm_api_key", "fw_key", raising=False)

        r = client.post("/api/ask/stream", json={"question": "hi", "model_override": "remote"})

        assert r.status_code == 200
        assert captured.get("force_remote") is True
        assert captured.get("force_local") is False

    def test_unconfigured_remote_pick_falls_back_to_auto(self, client, monkeypatch):
        """Not the picker's normal path (it hides the option), but a direct
        API call naming an unconfigured provider must not be mistaken for an
        Anthropic model id."""
        import api.services.agent_loop as agent_loop_mod
        from config.settings import settings

        captured = {}
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", self._fake_loop(captured))
        monkeypatch.setattr("api.routes.chat.classify_action_intent", self._fake_classify)
        monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
        monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
        monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

        r = client.post("/api/ask/stream", json={"question": "hi", "model_override": "remote"})

        assert r.status_code == 200
        assert captured.get("force_remote") is False
        assert captured.get("force_local") is False
        assert captured.get("model") != "remote"


class TestChatConfigRemoteFields:
    """GET /api/chat/config (#654): reports whether the remote provider is
    configured, so the picker can hide the option on a fresh clone."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_unconfigured_reports_unavailable(self, client, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
        monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
        monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

        r = client.get("/api/chat/config")

        assert r.status_code == 200
        assert r.json()["remote_model_available"] is False
        assert r.json()["remote_model_label"] == ""

    def test_configured_reports_available_with_label(self, client, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "remote_llm_base_url", "http://fake-remote", raising=False)
        monkeypatch.setattr(settings, "remote_llm_model", "accounts/fireworks/models/x", raising=False)
        monkeypatch.setattr(settings, "remote_llm_api_key", "fw_key", raising=False)
        monkeypatch.setattr(settings, "remote_llm_label", "Fireworks", raising=False)

        r = client.get("/api/chat/config")

        assert r.status_code == 200
        assert r.json()["remote_model_available"] is True
        assert r.json()["remote_model_label"] == "Fireworks"
