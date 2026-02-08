"""
Notification service for LifeOS.

Sends alerts via email when sync operations fail or other important events occur.

Configure LIFEOS_ALERT_EMAIL in .env to receive notifications.

Failure tracking:
- Processors (Granola, Omi, Calendar) record failures via record_failure()
- Nightly sync checks get_recent_failures() and includes them in the batch email
- Failures older than 24 hours are automatically cleaned up
"""
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# In-memory failure tracker
# Format: [(timestamp, source_name, error_message), ...]
_failure_log: list[tuple[datetime, str, str]] = []
_failure_lock = threading.Lock()

# How long to keep failures (for batching into nightly report)
_FAILURE_RETENTION_HOURS = 24


def record_failure(source: str, error: str, severity: str = "warning") -> None:
    """
    Record a processor failure for inclusion in the nightly batch email.

    Args:
        source: Name of the failing processor (e.g., "Granola processor", "Omi processor")
        error: Error message
        severity: "critical" for immediate alert, "warning" for nightly batch (default)
    """
    with _failure_lock:
        now = datetime.now(timezone.utc)
        _failure_log.append((now, source, error))
        logger.info(f"Recorded failure for nightly report: {source}")
        # Clean up old failures
        cutoff = now - timedelta(hours=_FAILURE_RETENTION_HOURS)
        _failure_log[:] = [(ts, src, err) for ts, src, err in _failure_log if ts > cutoff]

    # Send immediate alert for critical failures
    if severity == "critical":
        send_alert(
            subject=f"CRITICAL: {source}",
            body=f"A critical failure occurred.\n\nSource: {source}\nError: {error}",
        )


def get_recent_failures(hours: int = 24) -> list[tuple[datetime, str, str]]:
    """
    Get failures from the last N hours.

    Args:
        hours: How far back to look (default: 24)

    Returns:
        List of (timestamp, source, error) tuples
    """
    with _failure_lock:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [(ts, src, err) for ts, src, err in _failure_log if ts > cutoff]


def clear_failures() -> int:
    """
    Clear the failure log (call after sending nightly report).

    Returns:
        Number of failures cleared
    """
    with _failure_lock:
        count = len(_failure_log)
        _failure_log.clear()
        return count


def send_alert(
    subject: str,
    body: str,
    to: Optional[str] = None,
    include_timestamp: bool = True,
) -> bool:
    """
    Send an alert email.

    Args:
        subject: Email subject (will be prefixed with "[LifeOS Alert]")
        body: Email body
        to: Recipient email (defaults to LIFEOS_ALERT_EMAIL from settings)
        include_timestamp: Add timestamp to body

    Returns:
        True if sent successfully, False otherwise
    """
    from api.services.gmail import get_gmail_service
    from api.services.google_auth import GoogleAccount

    to = to or settings.alert_email
    if not to:
        logger.warning("No alert email configured (set LIFEOS_ALERT_EMAIL in .env)")
        return False
    full_subject = f"[LifeOS Alert] {subject}"

    if include_timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        body = f"{body}\n\n---\nTimestamp: {timestamp}"

    try:
        gmail = get_gmail_service(GoogleAccount.PERSONAL)
        message_id = gmail.send_email(
            to=to,
            subject=full_subject,
            body=body,
        )
        email_sent = message_id is not None
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")
        email_sent = False

    # Also send via Telegram if configured
    telegram_sent = False
    try:
        from api.services.telegram import send_message
        telegram_text = f"*{full_subject}*\n\n{body[:4000]}"
        telegram_sent = send_message(telegram_text)
        if not telegram_sent:
            logger.warning("Telegram alert not sent (disabled or failed)")
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")

    return email_sent


def send_sync_failure_alert(
    sync_name: str,
    error: str,
    context: Optional[dict] = None,
) -> bool:
    """
    Send an alert for a sync operation failure.

    Args:
        sync_name: Name of the sync that failed (e.g., "iMessage sync", "Vault reindex")
        error: Error message
        context: Optional additional context

    Returns:
        True if sent successfully
    """
    subject = f"{sync_name} failed"

    body_lines = [
        f"The {sync_name} operation failed.",
        "",
        f"Error: {error}",
    ]

    if context:
        body_lines.append("")
        body_lines.append("Context:")
        for key, value in context.items():
            body_lines.append(f"  {key}: {value}")

    body_lines.extend([
        "",
        "Check the LifeOS server logs for more details.",
    ])

    return send_alert(subject, "\n".join(body_lines))


def send_sync_success_summary(
    results: dict,
    include_details: bool = False,
) -> bool:
    """
    Send a summary of successful nightly sync (optional, disabled by default).

    Args:
        results: Dict of sync results
        include_details: Whether to include detailed results

    Returns:
        True if sent successfully
    """
    subject = "Nightly sync completed"

    body_lines = ["LifeOS nightly sync completed successfully.", ""]

    if include_details and results:
        body_lines.append("Results:")
        for sync_name, result in results.items():
            if isinstance(result, dict):
                body_lines.append(f"  {sync_name}: {result}")
            else:
                body_lines.append(f"  {sync_name}: {result}")

    return send_alert(subject, "\n".join(body_lines))
