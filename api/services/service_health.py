"""
Service Health Registry for LifeOS.

Tracks availability and degradation of external services:
- ChromaDB (vector store)
- Ollama (local LLM)
- Google APIs (Calendar, Gmail)
- Telegram (notifications)
- Embedding model (sentence-transformers)
- Vault filesystem (Obsidian)
- Backup storage (NVMe)

Provides:
- Real-time service status tracking
- Degradation event recording (when fallbacks are used)
- Severity-based alerting (CRITICAL = immediate, WARNING = batched)
- /health/services endpoint data

Usage:
    from api.services.service_health import get_service_health, record_degradation

    # Record when a fallback is used
    record_degradation("ollama", "intent_classification", "haiku_llm", "Connection refused")

    # Mark service state changes
    mark_service_healthy("chromadb")
    mark_service_failed("chromadb", "Connection timeout", Severity.CRITICAL)

    # Get current status
    registry = get_service_health()
    summary = registry.get_summary()
"""
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ServiceStatus(str, Enum):
    """Service availability status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    """Alert severity levels."""
    CRITICAL = "critical"  # Immediate alert
    WARNING = "warning"    # Batched nightly
    INFO = "info"          # Log only


# Service configuration: name -> (default severity, description)
SERVICE_CONFIG = {
    "chromadb": (Severity.CRITICAL, "Vector store (ChromaDB)"),
    "ollama": (Severity.WARNING, "Local LLM for query routing"),
    "google_calendar": (Severity.WARNING, "Google Calendar API"),
    "google_gmail": (Severity.WARNING, "Gmail API"),
    "telegram": (Severity.INFO, "Telegram bot notifications"),
    "embedding_model": (Severity.CRITICAL, "Sentence transformers model"),
    "vault_filesystem": (Severity.CRITICAL, "Obsidian vault filesystem"),
    "backup_storage": (Severity.WARNING, "NVMe backup drive"),
    "bm25_index": (Severity.WARNING, "BM25 keyword search index"),
}


@dataclass
class ServiceState:
    """Current state of a service."""
    status: ServiceStatus = ServiceStatus.UNKNOWN
    last_check: Optional[datetime] = None
    last_healthy: Optional[datetime] = None
    last_failed: Optional[datetime] = None
    failure_count: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    using_fallback: bool = False
    fallback_name: Optional[str] = None


@dataclass
class DegradationEvent:
    """Record of a degradation event (fallback used)."""
    timestamp: datetime
    service: str
    operation: str
    fallback_used: str
    original_error: Optional[str] = None


# Minimum time between critical alerts for the same service (prevents spam)
CRITICAL_ALERT_COOLDOWN_MINUTES = 5


class ServiceHealthRegistry:
    """
    Registry tracking health of all external services.

    Thread-safe singleton that maintains:
    - Current status of each service
    - Recent degradation events (last 24h)
    - Critical issues requiring attention

    Alert rate limiting:
    - CRITICAL alerts only sent on state transition (healthy → failed)
    - 5-minute cooldown between alerts for the same service (handles flapping)
    """

    _instance: Optional["ServiceHealthRegistry"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ServiceHealthRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._states: dict[str, ServiceState] = {}
        self._degradation_events: list[DegradationEvent] = []
        self._alert_cooldowns: dict[str, datetime] = {}  # service -> last alert time
        self._state_lock = threading.Lock()
        self._event_lock = threading.Lock()

        # Initialize states for known services
        for service in SERVICE_CONFIG:
            self._states[service] = ServiceState()

        self._initialized = True

    def mark_healthy(self, service: str) -> None:
        """
        Mark a service as healthy.

        Resets consecutive failure count and updates timestamps.
        """
        with self._state_lock:
            if service not in self._states:
                self._states[service] = ServiceState()

            state = self._states[service]
            now = datetime.now(timezone.utc)

            # Only log transition if coming from non-healthy state
            was_unhealthy = state.status in (ServiceStatus.UNAVAILABLE, ServiceStatus.DEGRADED)

            state.status = ServiceStatus.HEALTHY
            state.last_check = now
            state.last_healthy = now
            state.consecutive_failures = 0
            state.using_fallback = False
            state.fallback_name = None

            if was_unhealthy:
                logger.info(f"Service recovered: {service}")

    def mark_failed(
        self,
        service: str,
        error: str,
        severity: Optional[Severity] = None,
    ) -> None:
        """
        Mark a service as unavailable.

        Increments failure counts and optionally triggers immediate alert
        for CRITICAL severity (rate-limited to prevent spam).

        Alert conditions:
        - Only on state transition (healthy/unknown → failed)
        - Respects cooldown period (5 min) to handle flapping services
        """
        should_alert = False
        now = datetime.now(timezone.utc)

        with self._state_lock:
            if service not in self._states:
                self._states[service] = ServiceState()

            state = self._states[service]

            # Check if this is a state transition (healthy/unknown → failed)
            was_healthy = state.status in (ServiceStatus.HEALTHY, ServiceStatus.UNKNOWN)

            state.status = ServiceStatus.UNAVAILABLE
            state.last_check = now
            state.last_failed = now
            state.failure_count += 1
            state.consecutive_failures += 1
            state.last_error = error[:500] if error else None  # Truncate long errors

            # Get default severity if not specified
            if severity is None:
                severity = SERVICE_CONFIG.get(service, (Severity.WARNING, ""))[0]

            # Log the failure
            if was_healthy or state.consecutive_failures == 1:
                logger.warning(f"Service failed: {service} - {error[:100]}")

            # Determine if we should send an alert (rate limiting)
            if severity == Severity.CRITICAL and was_healthy:
                # Check cooldown
                last_alert = self._alert_cooldowns.get(service)
                cooldown = timedelta(minutes=CRITICAL_ALERT_COOLDOWN_MINUTES)

                if last_alert is None or (now - last_alert) > cooldown:
                    should_alert = True
                    self._alert_cooldowns[service] = now

        # Send alert outside lock
        if should_alert:
            self._send_critical_alert(service, error)

    def mark_degraded(
        self,
        service: str,
        fallback_name: str,
    ) -> None:
        """Mark a service as degraded (using fallback)."""
        with self._state_lock:
            if service not in self._states:
                self._states[service] = ServiceState()

            state = self._states[service]
            state.status = ServiceStatus.DEGRADED
            state.last_check = datetime.now(timezone.utc)
            state.using_fallback = True
            state.fallback_name = fallback_name

    def record_degradation(
        self,
        service: str,
        operation: str,
        fallback_used: str,
        original_error: Optional[str] = None,
    ) -> None:
        """
        Record a degradation event (fallback was used).

        These are collected for the nightly health report.
        """
        event = DegradationEvent(
            timestamp=datetime.now(timezone.utc),
            service=service,
            operation=operation,
            fallback_used=fallback_used,
            original_error=original_error[:200] if original_error else None,
        )

        with self._event_lock:
            self._degradation_events.append(event)
            # Auto-cleanup old events (> 24h)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            self._degradation_events = [
                e for e in self._degradation_events if e.timestamp > cutoff
            ]

        # Also mark the service as degraded
        self.mark_degraded(service, fallback_used)

        logger.info(
            f"Degradation: {service}/{operation} -> {fallback_used}"
            + (f" (error: {original_error[:50]})" if original_error else "")
        )

    def get_state(self, service: str) -> Optional[ServiceState]:
        """Get current state of a service."""
        with self._state_lock:
            return self._states.get(service)

    def get_degradation_events(self, hours: int = 24) -> list[DegradationEvent]:
        """Get degradation events from the last N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self._event_lock:
            return [e for e in self._degradation_events if e.timestamp > cutoff]

    def clear_degradation_events(self) -> int:
        """Clear degradation events (call after including in nightly report)."""
        with self._event_lock:
            count = len(self._degradation_events)
            self._degradation_events.clear()
            return count

    def get_critical_issues(self) -> list[tuple[str, str]]:
        """Get list of critical issues requiring attention."""
        issues = []
        with self._state_lock:
            for service, state in self._states.items():
                severity = SERVICE_CONFIG.get(service, (Severity.WARNING, ""))[0]
                if severity == Severity.CRITICAL and state.status == ServiceStatus.UNAVAILABLE:
                    issues.append((service, state.last_error or "Unknown error"))
        return issues

    def get_summary(self) -> dict:
        """
        Get summary of all service health for /health/services endpoint.

        Returns dict with:
        - overall_status: healthy/degraded/critical
        - services: dict of service -> status info
        - degradation_events: recent fallback usage
        - critical_issues: services needing immediate attention
        """
        with self._state_lock:
            services = {}
            for service, config in SERVICE_CONFIG.items():
                state = self._states.get(service, ServiceState())
                severity, description = config

                services[service] = {
                    "status": state.status.value,
                    "description": description,
                    "severity": severity.value,
                    "last_check": state.last_check.isoformat() if state.last_check else None,
                    "last_error": state.last_error,
                    "failure_count": state.failure_count,
                    "consecutive_failures": state.consecutive_failures,
                    "using_fallback": state.using_fallback,
                    "fallback_name": state.fallback_name,
                }

        # Get degradation events
        events = self.get_degradation_events(hours=24)
        event_summaries = [
            {
                "timestamp": e.timestamp.isoformat(),
                "service": e.service,
                "operation": e.operation,
                "fallback": e.fallback_used,
            }
            for e in events[-20:]  # Last 20 events
        ]

        # Determine overall status
        critical_issues = self.get_critical_issues()

        with self._state_lock:
            has_degraded = any(
                s.status == ServiceStatus.DEGRADED for s in self._states.values()
            )
            has_unavailable = any(
                s.status == ServiceStatus.UNAVAILABLE for s in self._states.values()
            )

        if critical_issues:
            overall = "critical"
        elif has_unavailable or has_degraded:
            overall = "degraded"
        else:
            overall = "healthy"

        return {
            "overall_status": overall,
            "services": services,
            "degradation_events": event_summaries,
            "degradation_count_24h": len(events),
            "critical_issues": [
                {"service": svc, "error": err} for svc, err in critical_issues
            ],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def _send_critical_alert(self, service: str, error: str) -> None:
        """Send immediate alert for critical service failure."""
        try:
            from api.services.notifications import send_alert

            description = SERVICE_CONFIG.get(service, (None, service))[1]
            send_alert(
                subject=f"CRITICAL: {description} unavailable",
                body=f"Service '{service}' has failed.\n\nError: {error}\n\nThis is a critical service that may impact core functionality.",
            )
        except Exception as e:
            logger.error(f"Failed to send critical alert for {service}: {e}")


# Module-level singleton accessor
_registry: Optional[ServiceHealthRegistry] = None


def get_service_health() -> ServiceHealthRegistry:
    """Get the service health registry singleton."""
    global _registry
    if _registry is None:
        _registry = ServiceHealthRegistry()
    return _registry


# Convenience functions for common operations

def record_degradation(
    service: str,
    operation: str,
    fallback_used: str,
    original_error: Optional[str] = None,
) -> None:
    """Record a degradation event (convenience wrapper)."""
    get_service_health().record_degradation(service, operation, fallback_used, original_error)


def mark_service_healthy(service: str) -> None:
    """Mark a service as healthy (convenience wrapper)."""
    get_service_health().mark_healthy(service)


def mark_service_failed(
    service: str,
    error: str,
    severity: Optional[Severity] = None,
) -> None:
    """Mark a service as failed (convenience wrapper)."""
    get_service_health().mark_failed(service, error, severity)
