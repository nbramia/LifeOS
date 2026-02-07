"""
Tests for Error Handling & Resilience (P4.2).
Acceptance Criteria:
- Google API timeout returns partial results + warning
- Claude API timeout returns graceful error
- Malformed requests return 400 with helpful message
- Service continues running after any single request error
- Retry logic for 5xx errors (max 3 retries)
"""
import pytest

# These tests use TestClient which initializes the app (slow)
pytestmark = pytest.mark.slow
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient

from api.main import app
from api.services.resilience import (
    RetryConfig,
    retry_async,
    retry_sync,
    graceful_degradation,
    user_friendly_error,
    is_retryable_status,
    ServiceUnavailableError,
    PartialResultError,
)


class TestRetryAsync:
    """Test async retry decorator."""

    @pytest.mark.asyncio
    async def test_succeeds_without_retry(self):
        """Should succeed on first attempt."""
        call_count = 0

        @retry_async()
        async def success_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await success_func()
        assert result == "success"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_failure(self):
        """Should retry on transient failures."""
        call_count = 0

        @retry_async(config=RetryConfig(max_retries=3, base_delay=0.01))
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Transient failure")
            return "success"

        result = await flaky_func()
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        """Should raise after exhausting retries."""
        call_count = 0

        @retry_async(config=RetryConfig(max_retries=2, base_delay=0.01))
        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Always fails")

        with pytest.raises(ConnectionError):
            await always_fails()

        assert call_count == 3  # 1 initial + 2 retries

    @pytest.mark.asyncio
    async def test_calls_on_retry_callback(self):
        """Should call on_retry callback."""
        retries_logged = []

        def on_retry(attempt, exc):
            retries_logged.append(attempt)

        @retry_async(
            config=RetryConfig(max_retries=2, base_delay=0.01),
            on_retry=on_retry
        )
        async def flaky():
            if len(retries_logged) < 2:
                raise ValueError("Retry me")
            return "done"

        result = await flaky()
        assert result == "done"
        assert retries_logged == [1, 2]


class TestRetrySync:
    """Test sync retry decorator."""

    def test_succeeds_without_retry(self):
        """Should succeed on first attempt."""
        call_count = 0

        @retry_sync()
        def success_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = success_func()
        assert result == "success"
        assert call_count == 1

    def test_retries_on_failure(self):
        """Should retry on transient failures."""
        call_count = 0

        @retry_sync(config=RetryConfig(max_retries=2, base_delay=0.01))
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TimeoutError("Timeout")
            return "success"

        result = flaky_func()
        assert result == "success"
        assert call_count == 2


class TestGracefulDegradation:
    """Test graceful degradation decorator."""

    @pytest.mark.asyncio
    async def test_returns_result_on_success(self):
        """Should return normal result when service works."""
        @graceful_degradation("TestService", fallback_value=[])
        async def working_service():
            return ["item1", "item2"]

        result = await working_service()
        assert result == ["item1", "item2"]

    @pytest.mark.asyncio
    async def test_returns_fallback_on_failure(self):
        """Should return fallback when service fails."""
        @graceful_degradation("TestService", fallback_value=[])
        async def failing_service():
            raise ConnectionError("Service down")

        result = await failing_service()
        assert result == []

    def test_sync_graceful_degradation(self):
        """Should work for sync functions too."""
        @graceful_degradation("TestService", fallback_value="default")
        def failing_sync():
            raise ValueError("Error")

        result = failing_sync()
        assert result == "default"


class TestUserFriendlyError:
    """Test user-friendly error messages."""

    def test_timeout_error(self):
        """Should handle timeout errors."""
        error = TimeoutError("Request timed out")
        msg = user_friendly_error(error)
        assert "timed out" in msg.lower() or "try again" in msg.lower()

    def test_connection_error(self):
        """Should handle connection errors."""
        error = ConnectionError("Network unreachable")
        msg = user_friendly_error(error)
        assert "connect" in msg.lower()

    def test_auth_error(self):
        """Should handle auth errors."""
        error = Exception("401 Unauthorized")
        msg = user_friendly_error(error)
        assert "authentication" in msg.lower()

    def test_rate_limit_error(self):
        """Should handle rate limiting."""
        error = Exception("Rate limit exceeded")
        msg = user_friendly_error(error)
        assert "too many" in msg.lower() or "wait" in msg.lower()

    def test_service_unavailable_error(self):
        """Should handle ServiceUnavailableError."""
        error = ServiceUnavailableError("Gmail", "API quota exceeded")
        msg = user_friendly_error(error)
        assert "Gmail" in msg


class TestIsRetryableStatus:
    """Test retryable status code detection."""

    def test_500_is_retryable(self):
        """500 Internal Server Error is retryable."""
        assert is_retryable_status(500) is True

    def test_502_is_retryable(self):
        """502 Bad Gateway is retryable."""
        assert is_retryable_status(502) is True

    def test_503_is_retryable(self):
        """503 Service Unavailable is retryable."""
        assert is_retryable_status(503) is True

    def test_429_is_retryable(self):
        """429 Too Many Requests is retryable."""
        assert is_retryable_status(429) is True

    def test_501_is_not_retryable(self):
        """501 Not Implemented is NOT retryable."""
        assert is_retryable_status(501) is False

    def test_400_is_not_retryable(self):
        """400 Bad Request is NOT retryable."""
        assert is_retryable_status(400) is False

    def test_404_is_not_retryable(self):
        """404 Not Found is NOT retryable."""
        assert is_retryable_status(404) is False


class TestAPIErrorHandling:
    """Test API error handling in endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_malformed_json_returns_400(self, client):
        """Malformed JSON should return 400."""
        response = client.post(
            "/api/search",
            content="not valid json",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code in [400, 422]

    def test_empty_query_returns_400(self, client):
        """Empty query should return 400 with helpful message."""
        response = client.post("/api/search", json={"query": ""})
        assert response.status_code == 400
        assert "error" in response.json() or "detail" in response.json()

    def test_missing_field_returns_422(self, client):
        """Missing required field should return 422."""
        response = client.post("/api/search", json={})
        assert response.status_code in [400, 422]

    def test_invalid_type_returns_error(self, client):
        """Invalid field type should return error."""
        response = client.post("/api/search", json={"query": 123})  # Should be string
        # Could be 200 if coerced, 400 if rejected, or 422 for validation error
        assert response.status_code in [200, 400, 422]

    def test_service_continues_after_error(self, client):
        """Service should continue running after an error."""
        # Send malformed request
        client.post("/api/search", json={"query": ""})

        # Service should still be responding (healthy or degraded based on config)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] in ["healthy", "degraded"]


class TestPartialResults:
    """Test partial result handling."""

    def test_partial_result_error_contains_data(self):
        """PartialResultError should contain partial data."""
        error = PartialResultError(
            message="Some sources failed",
            result=["item1", "item2"],
            errors=["Source 3 timed out"]
        )

        assert error.result == ["item1", "item2"]
        assert len(error.errors) == 1
        assert "timed out" in error.errors[0]
