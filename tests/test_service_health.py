"""
Tests for the service health registry.

Tests:
- Singleton behavior
- Service state tracking (healthy/failed/degraded)
- Degradation event recording and cleanup
- Critical alert triggering
- Summary endpoint data
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from api.services.service_health import (
    ServiceHealthRegistry,
    ServiceStatus,
    Severity,
    ServiceState,
    DegradationEvent,
    get_service_health,
    record_degradation,
    mark_service_healthy,
    mark_service_failed,
)


@pytest.fixture
def fresh_registry():
    """Create a fresh registry for each test (bypass singleton)."""
    registry = object.__new__(ServiceHealthRegistry)
    registry._initialized = False
    registry.__init__()
    return registry


class TestServiceHealthRegistry:
    """Tests for ServiceHealthRegistry class."""

    def test_singleton_behavior(self):
        """Registry should be a singleton."""
        r1 = get_service_health()
        r2 = get_service_health()
        assert r1 is r2

    def test_mark_healthy(self, fresh_registry):
        """mark_healthy should update service state."""
        fresh_registry.mark_healthy("chromadb")

        state = fresh_registry.get_state("chromadb")
        assert state is not None
        assert state.status == ServiceStatus.HEALTHY
        assert state.consecutive_failures == 0
        assert state.last_healthy is not None

    def test_mark_failed(self, fresh_registry):
        """mark_failed should update service state and increment counters."""
        fresh_registry.mark_failed("chromadb", "Connection refused")

        state = fresh_registry.get_state("chromadb")
        assert state is not None
        assert state.status == ServiceStatus.UNAVAILABLE
        assert state.failure_count == 1
        assert state.consecutive_failures == 1
        assert state.last_error == "Connection refused"

    def test_consecutive_failures(self, fresh_registry):
        """Consecutive failures should accumulate."""
        fresh_registry.mark_failed("chromadb", "Error 1")
        fresh_registry.mark_failed("chromadb", "Error 2")
        fresh_registry.mark_failed("chromadb", "Error 3")

        state = fresh_registry.get_state("chromadb")
        assert state.failure_count == 3
        assert state.consecutive_failures == 3

    def test_healthy_resets_consecutive_failures(self, fresh_registry):
        """mark_healthy should reset consecutive failure count."""
        fresh_registry.mark_failed("chromadb", "Error 1")
        fresh_registry.mark_failed("chromadb", "Error 2")
        fresh_registry.mark_healthy("chromadb")

        state = fresh_registry.get_state("chromadb")
        assert state.status == ServiceStatus.HEALTHY
        assert state.consecutive_failures == 0
        # Total failure count should persist
        assert state.failure_count == 2

    def test_mark_degraded(self, fresh_registry):
        """mark_degraded should set fallback info."""
        fresh_registry.mark_degraded("ollama", "haiku_llm")

        state = fresh_registry.get_state("ollama")
        assert state.status == ServiceStatus.DEGRADED
        assert state.using_fallback is True
        assert state.fallback_name == "haiku_llm"

    def test_record_degradation(self, fresh_registry):
        """record_degradation should add event and mark service degraded."""
        fresh_registry.record_degradation(
            "ollama",
            "intent_classification",
            "haiku_llm",
            "Connection refused"
        )

        state = fresh_registry.get_state("ollama")
        assert state.status == ServiceStatus.DEGRADED
        assert state.using_fallback is True

        events = fresh_registry.get_degradation_events(hours=24)
        assert len(events) == 1
        assert events[0].service == "ollama"
        assert events[0].operation == "intent_classification"
        assert events[0].fallback_used == "haiku_llm"

    def test_degradation_event_cleanup(self, fresh_registry):
        """Old degradation events should be cleaned up."""
        # Add an old event (manually set timestamp)
        old_event = DegradationEvent(
            timestamp=datetime.now(timezone.utc) - timedelta(hours=25),
            service="ollama",
            operation="test",
            fallback_used="test_fallback",
        )
        fresh_registry._degradation_events.append(old_event)

        # Add a new event (triggers cleanup)
        fresh_registry.record_degradation("ollama", "new_op", "new_fallback", None)

        events = fresh_registry.get_degradation_events(hours=24)
        # Old event should be cleaned up, only new event remains
        assert len(events) == 1
        assert events[0].operation == "new_op"

    def test_get_critical_issues(self, fresh_registry):
        """get_critical_issues should return failed critical services."""
        # ChromaDB is critical
        fresh_registry.mark_failed("chromadb", "Connection timeout")
        # Ollama is warning (not critical)
        fresh_registry.mark_failed("ollama", "Not running")

        issues = fresh_registry.get_critical_issues()
        assert len(issues) == 1
        assert issues[0][0] == "chromadb"
        assert "Connection timeout" in issues[0][1]

    def test_get_summary(self, fresh_registry):
        """get_summary should return complete status info."""
        fresh_registry.mark_healthy("chromadb")
        fresh_registry.mark_failed("ollama", "Not running")
        fresh_registry.record_degradation("bm25_index", "search", "vector_only", None)

        summary = fresh_registry.get_summary()

        assert "overall_status" in summary
        assert "services" in summary
        assert "degradation_events" in summary
        assert "degradation_count_24h" in summary
        assert "critical_issues" in summary
        assert "checked_at" in summary

        # Check service statuses
        assert summary["services"]["chromadb"]["status"] == "healthy"
        assert summary["services"]["ollama"]["status"] == "unavailable"
        assert summary["services"]["bm25_index"]["status"] == "degraded"

        # Overall status should be degraded (has unavailable service)
        assert summary["overall_status"] == "degraded"

    def test_critical_alert_sent_on_transition(self, fresh_registry):
        """Critical failure should trigger alert after consecutive failures."""
        from datetime import timedelta
        # Bypass startup grace period
        fresh_registry._startup_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        with patch("api.services.notifications.send_alert") as mock_alert:
            # First two failures don't alert (need MIN_CONSECUTIVE_FAILURES_FOR_ALERT=3)
            fresh_registry.mark_failed("chromadb", "Connection refused", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Connection refused", Severity.CRITICAL)
            assert mock_alert.call_count == 0

            # Third failure triggers alert
            fresh_registry.mark_failed("chromadb", "Connection refused", Severity.CRITICAL)
            mock_alert.assert_called_once()
            call_args = mock_alert.call_args
            assert "CRITICAL" in call_args.kwargs["subject"]
            assert "chromadb" in call_args.kwargs["body"].lower()

    def test_critical_alert_not_repeated(self, fresh_registry):
        """Repeated critical failures should not spam alerts."""
        from datetime import timedelta
        # Bypass startup grace period
        fresh_registry._startup_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        with patch("api.services.notifications.send_alert") as mock_alert:
            # First 3 failures to trigger alert
            fresh_registry.mark_failed("chromadb", "Error 1", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 2", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 3", Severity.CRITICAL)
            assert mock_alert.call_count == 1

            # Subsequent failures should NOT alert (cooldown)
            fresh_registry.mark_failed("chromadb", "Error 4", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 5", Severity.CRITICAL)
            assert mock_alert.call_count == 1  # Still just 1

    def test_critical_alert_after_recovery(self, fresh_registry):
        """Alert should fire again after service recovers and fails again."""
        from datetime import timedelta
        # Bypass startup grace period
        fresh_registry._startup_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        with patch("api.services.notifications.send_alert") as mock_alert:
            # First set of failures to trigger alert
            fresh_registry.mark_failed("chromadb", "Error 1", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 2", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 3", Severity.CRITICAL)
            assert mock_alert.call_count == 1

            # Recovery resets consecutive failure count
            fresh_registry.mark_healthy("chromadb")

            # Clear the cooldown for testing (normally 5 min)
            fresh_registry._alert_cooldowns.clear()

            # New failures after recovery should alert after threshold
            fresh_registry.mark_failed("chromadb", "Error 4", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 5", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 6", Severity.CRITICAL)
            assert mock_alert.call_count == 2

    def test_startup_grace_period_suppresses_alert(self, fresh_registry):
        """Alerts should be suppressed during startup grace period."""
        # fresh_registry has _startup_time set to now, so we're in grace period
        with patch("api.services.notifications.send_alert") as mock_alert:
            # Even with enough consecutive failures, no alert during grace period
            fresh_registry.mark_failed("chromadb", "Error 1", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 2", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 3", Severity.CRITICAL)
            fresh_registry.mark_failed("chromadb", "Error 4", Severity.CRITICAL)
            mock_alert.assert_not_called()

    def test_warning_no_immediate_alert(self, fresh_registry):
        """Warning failure should not trigger immediate alert."""
        with patch("api.services.notifications.send_alert") as mock_alert:
            fresh_registry.mark_failed("ollama", "Not running", Severity.WARNING)

            mock_alert.assert_not_called()

    def test_unknown_service(self, fresh_registry):
        """Unknown service should be tracked without error."""
        fresh_registry.mark_healthy("custom_service")

        state = fresh_registry.get_state("custom_service")
        assert state is not None
        assert state.status == ServiceStatus.HEALTHY

    def test_clear_degradation_events(self, fresh_registry):
        """clear_degradation_events should remove all events."""
        fresh_registry.record_degradation("ollama", "op1", "fallback1", None)
        fresh_registry.record_degradation("ollama", "op2", "fallback2", None)

        count = fresh_registry.clear_degradation_events()
        assert count == 2

        events = fresh_registry.get_degradation_events(hours=24)
        assert len(events) == 0


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_record_degradation_function(self):
        """record_degradation function should work."""
        # Uses the singleton
        registry = get_service_health()
        initial_count = len(registry.get_degradation_events(hours=24))

        record_degradation("test_service", "test_op", "test_fallback", "test error")

        events = registry.get_degradation_events(hours=24)
        assert len(events) > initial_count

    def test_mark_service_healthy_function(self):
        """mark_service_healthy function should work."""
        mark_service_healthy("test_service2")

        registry = get_service_health()
        state = registry.get_state("test_service2")
        assert state.status == ServiceStatus.HEALTHY

    def test_mark_service_failed_function(self):
        """mark_service_failed function should work."""
        mark_service_failed("test_service3", "Test error", Severity.WARNING)

        registry = get_service_health()
        state = registry.get_state("test_service3")
        assert state.status == ServiceStatus.UNAVAILABLE
