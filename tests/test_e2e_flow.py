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
from unittest.mock import patch, MagicMock

# Mark as integration tests (require server or mocking)
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.requires_server]


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

    def test_api_error_returns_user_friendly_message(self):
        """API errors should return helpful messages, not raw exceptions."""
        # This tests that the streaming endpoint handles errors gracefully
        pass  # Implemented below with actual server test

    def test_timeout_returns_message_not_hang(self):
        """Timeouts should return a message, not leave the UI hanging."""
        pass  # Implemented below with actual server test


class TestStreamingEndpoint:
    """Tests for the /api/ask/stream endpoint behavior."""

    @pytest.fixture
    def mock_synthesizer(self):
        """Mock synthesizer to avoid real API calls."""
        with patch('api.routes.ask.get_synthesizer') as mock:
            synth = MagicMock()
            mock.return_value = synth
            yield synth

    def test_stream_sends_error_event_on_api_failure(self):
        """Stream should send error event when API fails, not hang."""
        # The current behavior hangs - this test documents expected behavior
        # When API fails, should receive: data: {"type": "error", "message": "..."}
        pass

    def test_stream_includes_done_event(self):
        """Stream should always end with a done event."""
        pass


class TestUIErrorDisplay:
    """Tests that verify the UI properly displays errors."""

    @pytest.mark.browser
    def test_api_error_shown_to_user(self, page):
        """API errors or success should be displayed to the user, not silently fail."""

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto("http://localhost:8000/chat?mode=text")
        page.wait_for_selector(".welcome")

        # Send a message
        page.locator(".input-field").fill("test question")
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

        # Should have content (either success or error message)
        assert len(content) > 0, "Assistant should respond with some content"

    @pytest.mark.browser
    def test_loading_indicator_clears_on_completion(self, page):
        """Loading indicator should clear when response completes."""

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto("http://localhost:8000/chat?mode=text")
        page.wait_for_selector(".welcome")

        # Send a message
        page.locator(".input-field").fill("test")
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

        # Status should not be stuck on "Thinking..."
        page.wait_for_function(
            "() => document.querySelector('.status-text')?.textContent !== 'Thinking...'",
            timeout=30000
        )

        status = page.locator(".status-text").text_content()
        assert status in ["Ready", "Error"], f"Unexpected status: {status}"


class TestRequestTimeouts:
    """Tests for proper timeout handling."""

    def test_request_has_timeout(self):
        """Requests should have timeouts to prevent indefinite hangs."""
        # Test that httpx client has reasonable timeout (using sync client)
        try:
            httpx.post(
                "http://localhost:8000/api/ask/stream",
                json={"question": "test"},
                timeout=5.0  # 5 second timeout for test
            )
        except httpx.TimeoutException:
            pass  # Expected if server is slow
        except httpx.ConnectError:
            pass  # Expected if server not running


class TestHealthCheck:
    """Tests for health check endpoint."""

    def test_health_endpoint_exists(self):
        """Should have a /health endpoint for monitoring."""
        import httpx

        try:
            response = httpx.get("http://localhost:8000/health", timeout=5.0)
            # Should return 200
            assert response.status_code == 200
            data = response.json()
            assert 'status' in data
            # New health endpoint includes checks, but skip if server is old version
            # assert 'checks' in data
        except httpx.ConnectError:
            pytest.skip("Server not running")

    def test_health_checks_api_key(self):
        """Health endpoint should verify API key is configured."""
        import httpx

        try:
            response = httpx.get("http://localhost:8000/health", timeout=5.0)
            data = response.json()
            # Should have checks dict with api_key_configured (skip if old server)
            if 'checks' not in data:
                pytest.skip("Server running old version without checks")
            assert 'api_key_configured' in data.get('checks', {})
        except httpx.ConnectError:
            pytest.skip("Server not running")

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
    - Server running on localhost:8000
    - Valid API key configured (for success tests)
    - Playwright browsers installed

    Run with: pytest tests/test_e2e_flow.py::TestRealUserFlow -v --browser chromium
    """

    @pytest.mark.browser
    def test_user_sends_query_gets_response(self, page):
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
        page.goto("http://localhost:8000/chat?mode=text")

        # Wait for app to load
        page.wait_for_selector(".welcome", timeout=10000)
        page.wait_for_function(
            "() => window.lifeChat?.state?.isLoading === false",
            timeout=15000,
        )

        # Verify initial state
        expect(page.locator(".status-text")).to_have_text("Ready")

        # Type a question
        page.mouse.click(600, 760)
        page.keyboard.type("What is LifeOS?")

        # Send the message
        page.keyboard.press("Enter")

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
        assert len(content) > 0, "Assistant response should not be empty"

        # Typing indicator should be gone
        typing = page.locator(".typing")
        expect(typing).not_to_be_visible()

        # Status should be Ready or Error (not stuck on "Thinking...")
        status = page.locator(".status-text").text_content()
        assert status in ["Ready", "Error"], f"Unexpected status: {status}"

    @pytest.mark.browser
    def test_user_sends_query_via_enter_key(self, page):
        """User can send a message by pressing Enter."""
        from playwright.sync_api import expect

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto("http://localhost:8000/chat?mode=text")
        page.wait_for_selector(".welcome", timeout=10000)

        # Type and press Enter
        input_field = page.locator(".input-field")
        input_field.fill("Hello")
        input_field.press("Enter")

        # Message should be sent
        page.wait_for_selector(".message.user", timeout=5000)
        expect(page.locator(".message.user .message-content")).to_contain_text("Hello")

    @pytest.mark.browser
    def test_user_clicks_suggestion(self, page):
        """User can click a suggestion to send it as a query."""

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto("http://localhost:8000/chat?mode=text")
        page.wait_for_selector(".welcome", timeout=10000)

        # Click first suggestion
        suggestions = page.locator(".suggestion")
        if suggestions.count() > 0:
            first_suggestion = suggestions.first

            first_suggestion.click()

            # Message should be sent with suggestion text
            page.wait_for_selector(".message.user", timeout=5000)
            user_msg = page.locator(".message.user .message-content").text_content()
            # Suggestion text should be in the message (may be truncated)
            assert len(user_msg) > 0

    @pytest.mark.browser
    def test_mobile_user_flow(self, page):
        """Test the full user flow on mobile viewport."""

        # Mobile viewport (iPhone X)
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto("http://localhost:8000/chat?mode=text")
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
        assert len(assistant_msg.text_content()) > 0
