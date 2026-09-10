"""
End-to-end flow tests for LifeOS.

These tests verify the complete request flow works, including:
- API configuration validation
- Error handling and user feedback
- Timeout handling
- Streaming response integrity

Run with server: pytest tests/test_e2e_flow.py -v
"""
import pytest
import httpx
import json
from unittest.mock import MagicMock
from unittest.mock import AsyncMock
from playwright.sync_api import expect

# Mark as integration tests (require server or mocking)
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.requires_server]


def _stream_events(candidate_base_url, question):
    """Collect one real candidate SSE response with an application timeout.

    The client timeout is deliberately generous; timeout behavior is induced
    by the instance-owned deterministic provider, then asserted from the
    route's events rather than inferred from an httpx exception.
    """
    events = []
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    with httpx.stream(
        "POST",
        f"{candidate_base_url}/api/ask/stream",
        json={"question": question, "include_sources": False},
        timeout=timeout,
    ) as response:
        assert response.status_code == 200, response.text
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class TestConfigurationValidation:
    """Tests that verify configuration errors are caught early."""

    def test_local_llm_url_defaults_configured(self):
        """Local LLM URL should have a sensible default."""
        from config.settings import Settings

        settings = Settings(_env_file=None)
        assert settings.local_llm_url  # should default to http://localhost:8080

    def test_synthesizer_uses_local_llm(self):
        """Synthesizer should use local LLM client."""
        from api.services.synthesizer import Synthesizer

        mock_response = MagicMock()
        mock_response.text = "Test response"

        mock_client = MagicMock()
        mock_client.create.return_value = mock_response

        synth = Synthesizer()
        synth._client = mock_client
        result = synth.synthesize("test prompt")
        assert result == "Test response"
        mock_client.create.assert_called_once()


class TestErrorHandling:
    """Tests that verify errors are properly surfaced to users."""

    def test_api_error_returns_user_friendly_message(self, candidate_base_url):
        """API errors should return helpful messages, not raw exceptions."""
        events = _stream_events(candidate_base_url, "[synthetic-backend-failure]")
        contents = [event.get("content", "") for event in events if event.get("type") == "content"]
        assert contents == ["Sorry, I encountered an error and could not complete this request."]
        assert "synthetic backend failure" not in "".join(contents)
        assert events[-1]["type"] == "done"
        conversation_ids = [
            event["conversation_id"]
            for event in events
            if event.get("type") == "conversation_id"
        ]
        assert len(conversation_ids) == 1
        stored = httpx.get(
            f"{candidate_base_url}/api/conversations/{conversation_ids[0]}",
            timeout=5.0,
        )
        assert stored.status_code == 200
        assistant_messages = [
            message["content"]
            for message in stored.json()["messages"]
            if message["role"] == "assistant"
        ]
        assert assistant_messages[-1] == contents[0]

    def test_timeout_returns_message_not_hang(self, candidate_base_url):
        """Timeouts should return a message, not leave the UI hanging."""
        events = _stream_events(candidate_base_url, "[synthetic-timeout]")
        contents = [event.get("content", "") for event in events if event.get("type") == "content"]
        assert contents == ["Sorry, I encountered an error and could not complete this request."]
        assert events[-1]["type"] == "done"


class TestStreamingEndpoint:
    """Tests for the /api/ask/stream endpoint behavior."""

    def test_stream_sends_error_event_on_api_failure(self, candidate_base_url):
        """Stream should send error event when API fails, not hang."""
        events = _stream_events(candidate_base_url, "[synthetic-backend-failure]")
        errors = [event for event in events if event.get("type") == "error"]
        assert errors, "backend failure must produce an SSE error event"
        assert errors[0]["message"] == "The request could not be completed."
        assert events[-1]["type"] == "done"

    def test_stream_includes_done_event(self, candidate_base_url):
        """Stream should always end with a done event."""
        events = _stream_events(candidate_base_url, "synthetic success")
        assert events, "candidate stream returned no SSE events"
        assert events[-1]["type"] == "done"
        content = "".join(event.get("content", "") for event in events if event.get("type") == "content")
        assert content == "synthetic deterministic test response"

    def test_outer_stream_error_is_sanitized_and_marked_truncated(self, monkeypatch):
        """A genuine outer failure stays error-only and persists truncation."""
        from fastapi.testclient import TestClient
        import api.routes.chat as chat_route
        import api.services.agent_loop as agent_loop_module
        from api.services.conversation_store import get_store
        from api.services.chat_turns import TRUNCATION_MARKER

        async def failing_loop(**_kwargs):
            yield {"type": "text", "content": "synthetic partial response"}
            raise RuntimeError("synthetic provider detail must stay server-side")

        monkeypatch.setattr(agent_loop_module, "run_agent_loop", failing_loop)
        monkeypatch.setattr(chat_route, "classify_action_intent", AsyncMock(return_value=None))
        response = TestClient(__import__("api.main", fromlist=["app"]).app).post(
            "/api/ask/stream", json={"question": "synthetic outer failure"}
        )
        assert response.status_code == 200
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        errors = [event for event in events if event.get("type") == "error"]
        assert errors and errors[0]["message"] == "The request could not be completed."
        assert "synthetic provider detail" not in response.text
        assert not any(event.get("type") == "done" for event in events)
        conversation_id = next(
            event["conversation_id"] for event in events
            if event.get("type") == "conversation_id"
        )
        messages = get_store().get_messages(conversation_id)
        assistant = [message for message in messages if message.role == "assistant"][-1]
        assert assistant.content.endswith(TRUNCATION_MARKER)
        assert assistant.routing["truncated"] is True

    def test_handled_stream_error_persists_all_partial_content(self, monkeypatch):
        """A handled provider failure persists the exact content already streamed."""
        from fastapi.testclient import TestClient
        import api.routes.chat as chat_route
        import api.services.agent_loop as agent_loop_module
        from api.services.agent_loop import AgentResult
        from api.services.conversation_store import get_store

        async def handled_failure_loop(**_kwargs):
            yield {"type": "text", "content": "Useful streamed result."}
            yield {
                "type": "result",
                "result": AgentResult(
                    full_text="stale round accumulator",
                    error_message="The request could not be completed.",
                ),
            }

        monkeypatch.setattr(agent_loop_module, "run_agent_loop", handled_failure_loop)
        monkeypatch.setattr(chat_route, "classify_action_intent", AsyncMock(return_value=None))
        response = TestClient(__import__("api.main", fromlist=["app"]).app).post(
            "/api/ask/stream", json={"question": "synthetic handled failure"}
        )
        assert response.status_code == 200
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert [event["type"] for event in events if event["type"] in {"error", "done"}] == [
            "error", "done"
        ]
        error = next(event for event in events if event.get("type") == "error")
        assert error["message"] == "The request could not be completed."
        conversation_id = next(
            event["conversation_id"] for event in events
            if event.get("type") == "conversation_id"
        )
        messages = get_store().get_messages(conversation_id)
        assistant = [message for message in messages if message.role == "assistant"][-1]
        assert assistant.content == "Useful streamed result."


class TestUIErrorDisplay:
    """Tests that verify the UI properly displays errors."""

    @pytest.mark.browser
    def test_api_error_shown_to_user(self, page, candidate_base_url):
        """API errors or success should be displayed to the user, not silently fail."""

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{candidate_base_url}/chat")
        page.wait_for_selector(".welcome")

        # Send a message
        page.locator(".input-field").fill("[synthetic-backend-failure]")
        page.locator(".send-btn").click()

        # Wait for response (should show either success or error, not hang forever)
        page.wait_for_selector(".message.assistant", timeout=30000)

        # Wait for streaming to complete by checking for status change
        page.wait_for_function(
            "() => !document.querySelector('.typing') && document.querySelector('.status-text')?.textContent !== 'Thinking...'",
            timeout=60000
        )

        # Should show some message to user (success or error)
        assistant_msg = page.locator(".message.assistant .message-content")
        content = assistant_msg.text_content()

        assert content == "Sorry, I encountered an error and could not complete this request."
        expect(page.locator(".send-btn")).to_be_enabled()
        expect(page.locator(".stop-btn")).not_to_have_class("visible")
        expect(page.locator(".status-text")).to_have_text("Error")

    @pytest.mark.browser
    def test_loading_indicator_clears_on_completion(self, page, candidate_base_url):
        """Loading indicator should clear when response completes."""

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{candidate_base_url}/chat")
        page.wait_for_selector(".welcome")

        # Send a message
        page.locator(".input-field").fill("synthetic success")
        page.locator(".send-btn").click()

        # Wait for typing indicator to appear (may not appear for fast responses)
        try:
            page.wait_for_selector(".typing", timeout=5000)
        except Exception:
            pass  # Fast response, no typing indicator

        # Wait for response to complete - use longer timeout for streaming
        page.wait_for_function(
            "() => !document.querySelector('.typing') && document.querySelector('.message.assistant')",
            timeout=60000
        )

        expect(page.locator(".message.assistant .message-content")).to_have_text(
            "synthetic deterministic test response"
        )
        # Status should not be stuck on "Thinking..."
        page.wait_for_function(
            "() => document.querySelector('.status-text')?.textContent !== 'Thinking...'",
            timeout=30000
        )

        status = page.locator(".status-text").text_content()
        assert status == "Ready", f"Unexpected status: {status}"
        expect(page.locator(".send-btn")).to_be_enabled()
        expect(page.locator(".stop-btn")).not_to_have_class("visible")

    @pytest.mark.parametrize(
        "viewport",
        [{"width": 1280, "height": 800}, {"width": 390, "height": 844}],
        ids=["desktop", "mobile"],
    )
    @pytest.mark.browser
    def test_error_allows_successful_followup(self, page, candidate_base_url, viewport):
        """An error must release the composer for a subsequent turn."""
        page.set_viewport_size(viewport)
        page.goto(f"{candidate_base_url}/chat")
        page.wait_for_selector(".welcome")

        page.locator(".input-field").fill("[synthetic-backend-failure]")
        page.locator(".send-btn").click()
        page.wait_for_function(
            "() => !document.querySelector('.typing') && document.querySelector('.status-text')?.textContent !== 'Thinking...'",
            timeout=60000,
        )
        expect(page.locator(".message.assistant .message-content").first).to_have_text(
            "Sorry, I encountered an error and could not complete this request."
        )
        expect(page.locator(".send-btn")).to_be_enabled()
        expect(page.locator(".stop-btn")).not_to_have_class("visible")
        expect(page.locator(".status-text")).to_have_text("Error")

        page.locator(".input-field").fill("synthetic success")
        page.locator(".send-btn").click()
        expect(page.locator(".message.assistant .message-content").last).to_have_text(
            "synthetic deterministic test response", timeout=60000
        )
        expect(page.locator(".send-btn")).to_be_enabled()
        expect(page.locator(".stop-btn")).not_to_have_class("visible")
        expect(page.locator(".status-text")).to_have_text("Ready")


class TestRequestTimeouts:
    """Tests for proper timeout handling."""

    def test_request_timeout_is_application_observable(self, candidate_base_url):
        """Requests should have timeouts to prevent indefinite hangs."""
        events = _stream_events(candidate_base_url, "[synthetic-timeout]")
        assert events[-1]["type"] == "done"
        assert any(
            event.get("type") == "content"
            and event.get("content") == "Sorry, I encountered an error and could not complete this request."
            for event in events
        )


class TestHealthCheck:
    """Tests for health check endpoint."""

    def test_health_endpoint_exists(self, candidate_base_url):
        """Should have a /health endpoint for monitoring."""
        response = httpx.get(f"{candidate_base_url}/health", timeout=5.0)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in {"healthy", "degraded"}
        assert "checks" in data

    def test_health_checks_api_key(self, candidate_base_url):
        """Health endpoint should verify API key is configured."""
        response = httpx.get(f"{candidate_base_url}/health", timeout=5.0)
        assert response.status_code == 200
        data = response.json()
        assert "api_key_configured" in data["checks"]

    def test_health_returns_degraded_without_api_key(self):
        """Health should return degraded status if the Anthropic API key is missing.

        `api_key_configured` reflects whether ANTHROPIC_API_KEY is actually
        set (#697) — it used to check `local_llm_url`, which has a
        non-empty default and so could never report false.
        """
        # This is a unit test of the health logic
        from fastapi.testclient import TestClient
        from unittest.mock import patch, MagicMock

        from api.main import app
        client = TestClient(app)

        # Mock settings to have no Anthropic API key
        mock_settings = MagicMock()
        mock_settings.anthropic_api_key = ""

        with patch('config.settings.settings', mock_settings):
            response = client.get("/health")
            data = response.json()
            assert data['status'] == 'degraded'
            assert not data['checks']['api_key_configured']

    def test_health_reports_scheduler_watcher_liveness_distinct_from_reminder_scheduler(self):
        """#766: the scheduler file watcher's liveness is a separate field
        from reminder_scheduler (the delivery thread) — a watcher that's
        down must be visible even when delivery is fine, and vice versa."""
        from fastapi.testclient import TestClient
        from unittest.mock import patch, MagicMock

        from api.main import app
        import api.main as main
        client = TestClient(app)

        alive_scheduler = MagicMock()
        alive_scheduler.is_alive.return_value = True
        dead_watcher = MagicMock()
        dead_watcher.is_alive.return_value = False

        with patch.object(main, "_reminder_scheduler", alive_scheduler), \
             patch.object(main, "_scheduler_watcher", dead_watcher):
            response = client.get("/health")
            data = response.json()
            assert data['checks']['reminder_scheduler'] is True
            assert data['checks']['scheduler_watcher'] is False
            assert data['status'] == 'degraded'

        alive_watcher = MagicMock()
        alive_watcher.is_alive.return_value = True

        with patch.object(main, "_reminder_scheduler", alive_scheduler), \
             patch.object(main, "_scheduler_watcher", alive_watcher):
            response = client.get("/health")
            data = response.json()
            assert data['checks']['scheduler_watcher'] is True

    def test_health_scheduler_watcher_false_when_never_started(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch

        from api.main import app
        import api.main as main
        client = TestClient(app)

        with patch.object(main, "_scheduler_watcher", None):
            response = client.get("/health")
            data = response.json()
            assert data['checks']['scheduler_watcher'] is False


class TestRealUserFlow:
    """
    End-to-end tests that simulate real user interactions.

    These tests require:
    - An owned candidate instance (``candidate_base_url`` fixture)
    - Valid API key configured (for success tests)
    - Playwright browsers installed

    Run with: pytest tests/test_e2e_flow.py::TestRealUserFlow -v --browser chromium
    """

    @pytest.mark.browser
    def test_user_sends_query_gets_response(self, page, candidate_base_url):
        """
        Simulate a real user sending a query and receiving a response.

        This is the primary e2e test that validates the full flow:
        1. User loads the page
        2. User types a question
        3. User clicks send (or presses Enter)
        4. User sees their message appear
        5. User sees typing indicator
        6. User receives a response (success or error)
        7. Typing indicator clears
        8. Status returns to Ready or Error
        """
        from playwright.sync_api import expect

        # Setup
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{candidate_base_url}/chat")

        # Wait for app to load
        page.wait_for_selector(".welcome", timeout=10000)

        # Verify initial state
        expect(page.locator(".status-text")).to_have_text("Ready")

        # Type a question
        input_field = page.locator(".input-field")
        input_field.fill("What is LifeOS?")

        # Send the message
        page.locator(".send-btn").click()

        # User message should appear immediately
        page.wait_for_selector(".message.user", timeout=5000)
        user_msg = page.locator(".message.user .message-content")
        expect(user_msg).to_contain_text("What is LifeOS?")

        # Wait for assistant response element to appear
        page.wait_for_selector(".message.assistant", timeout=30000)

        # Wait for streaming to complete (status changes from Thinking to Ready/Error)
        page.wait_for_function(
            "() => document.querySelector('.status-text')?.textContent !== 'Thinking...'",
            timeout=60000
        )

        # Response should have content after streaming completes
        assistant_msg = page.locator(".message.assistant .message-content")
        content = assistant_msg.text_content()
        assert content == "synthetic deterministic test response"

        # Typing indicator should be gone
        typing = page.locator(".typing")
        expect(typing).not_to_be_visible()

        # Status should be Ready or Error (not stuck on "Thinking...")
        status = page.locator(".status-text").text_content()
        assert status in ["Ready", "Error"], f"Unexpected status: {status}"

    @pytest.mark.browser
    def test_user_sends_query_via_enter_key(self, page, candidate_base_url):
        """User can send a message by pressing Enter."""
        from playwright.sync_api import expect

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{candidate_base_url}/chat")
        page.wait_for_selector(".welcome", timeout=10000)

        # Type and press Enter
        input_field = page.locator(".input-field")
        input_field.fill("Hello")
        input_field.press("Enter")

        # Message should be sent
        page.wait_for_selector(".message.user", timeout=5000)
        expect(page.locator(".message.user .message-content")).to_contain_text("Hello")

    @pytest.mark.browser
    def test_user_clicks_suggestion(self, page, candidate_base_url):
        """User can click a suggestion to send it as a query."""

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{candidate_base_url}/chat")
        page.wait_for_selector(".welcome", timeout=10000)

        # Click first suggestion
        suggestions = page.locator(".suggestion")
        assert suggestions.count() > 0, "candidate chat should expose a suggestion"
        suggestions.first.click()

        # Message should be sent with suggestion text
        page.wait_for_selector(".message.user", timeout=5000)
        user_msg = page.locator(".message.user .message-content").text_content()
        assert user_msg

    @pytest.mark.browser
    def test_mobile_user_flow(self, page, candidate_base_url):
        """Test the full user flow on mobile viewport."""

        # Mobile viewport (iPhone X)
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(f"{candidate_base_url}/chat")
        page.wait_for_selector(".welcome", timeout=10000)

        # Sidebar should be hidden
        sidebar = page.locator(".sidebar")
        box = sidebar.bounding_box()
        assert box["x"] < 0, "Sidebar should be off-screen on mobile"

        # Send a message
        input_field = page.locator(".input-field")
        input_field.fill("Test on mobile")
        page.locator(".send-btn").click()

        # Should work the same as desktop
        page.wait_for_selector(".message.user", timeout=5000)
        page.wait_for_selector(".message.assistant", timeout=30000)

        # Wait for streaming to complete
        page.wait_for_function(
            "() => document.querySelector('.status-text')?.textContent !== 'Thinking...'",
            timeout=60000
        )

        # Message should be visible
        assistant_msg = page.locator(".message.assistant .message-content")
        assert assistant_msg.text_content() == "synthetic deterministic test response"
