"""
Resilience utilities for LifeOS.

Provides:
- Retry logic for transient failures
- Graceful degradation for external services
- Error wrapping and user-friendly messages
"""
import asyncio
import functools
import logging
from typing import Callable, TypeVar, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    base_delay: float = 1.0  # seconds
    max_delay: float = 30.0  # seconds
    exponential_base: float = 2.0
    retryable_exceptions: tuple = (Exception,)


DEFAULT_RETRY_CONFIG = RetryConfig()


class ServiceUnavailableError(Exception):
    """Raised when an external service is unavailable."""

    def __init__(self, service: str, message: str, partial_result: Any = None):
        self.service = service
        self.message = message
        self.partial_result = partial_result
        super().__init__(f"{service}: {message}")


class PartialResultError(Exception):
    """Raised when operation succeeded partially."""

    def __init__(self, message: str, result: Any, errors: list[str]):
        self.message = message
        self.result = result
        self.errors = errors
        super().__init__(message)


def retry_async(
    config: Optional[RetryConfig] = None,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
):
    """
    Decorator for async functions with retry logic.

    Args:
        config: Retry configuration
        on_retry: Optional callback on each retry (retry_num, exception)
    """
    cfg = config or DEFAULT_RETRY_CONFIG

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(cfg.max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except cfg.retryable_exceptions as e:
                    last_exception = e

                    if attempt < cfg.max_retries:
                        delay = min(
                            cfg.base_delay * (cfg.exponential_base ** attempt),
                            cfg.max_delay
                        )
                        logger.warning(
                            f"Retry {attempt + 1}/{cfg.max_retries} for {func.__name__}: {e}. "
                            f"Waiting {delay:.1f}s..."
                        )

                        if on_retry:
                            on_retry(attempt + 1, e)

                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"All {cfg.max_retries} retries exhausted for {func.__name__}: {e}"
                        )

            raise last_exception

        return wrapper
    return decorator


def retry_sync(
    config: Optional[RetryConfig] = None,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
):
    """
    Decorator for sync functions with retry logic.
    """
    cfg = config or DEFAULT_RETRY_CONFIG

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(cfg.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except cfg.retryable_exceptions as e:
                    last_exception = e

                    if attempt < cfg.max_retries:
                        delay = min(
                            cfg.base_delay * (cfg.exponential_base ** attempt),
                            cfg.max_delay
                        )
                        logger.warning(
                            f"Retry {attempt + 1}/{cfg.max_retries} for {func.__name__}: {e}. "
                            f"Waiting {delay:.1f}s..."
                        )

                        if on_retry:
                            on_retry(attempt + 1, e)

                        import time
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"All {cfg.max_retries} retries exhausted for {func.__name__}: {e}"
                        )

            raise last_exception

        return wrapper
    return decorator


def graceful_degradation(
    service_name: str,
    fallback_value: Any = None,
    log_level: int = logging.WARNING,
):
    """
    Decorator for graceful degradation when external service fails.

    Args:
        service_name: Name of the service (for logging)
        fallback_value: Value to return on failure
        log_level: Log level for failures
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.log(
                    log_level,
                    f"{service_name} unavailable: {e}. Using fallback."
                )
                return fallback_value

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.log(
                    log_level,
                    f"{service_name} unavailable: {e}. Using fallback."
                )
                return fallback_value

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def user_friendly_error(error: Exception) -> str:
    """
    Convert exception to user-friendly error message.

    Args:
        error: The exception to convert

    Returns:
        User-friendly error message
    """
    # Check for custom errors first
    if isinstance(error, ServiceUnavailableError):
        return f"{error.service} is currently unavailable. {error.message}"

    error_type = type(error).__name__
    error_str = str(error).lower()

    # Network errors
    if "timeout" in error_str or "timed out" in error_str:
        return "The request timed out. Please try again."

    if "connection" in error_str or "network" in error_str:
        return "Unable to connect. Please check your internet connection."

    # Auth errors
    if "unauthorized" in error_str or "401" in error_str:
        return "Authentication failed. Please re-authenticate."

    if "forbidden" in error_str or "403" in error_str:
        return "Access denied. You don't have permission for this action."

    # Rate limiting
    if "rate limit" in error_str or "429" in error_str or "quota" in error_str:
        return "Too many requests. Please wait a moment and try again."

    # Not found
    if "not found" in error_str or "404" in error_str:
        return "The requested resource was not found."

    # Server errors
    if "500" in error_str or "internal server error" in error_str:
        return "The service encountered an error. Please try again later."

    if "502" in error_str or "503" in error_str or "504" in error_str:
        return "The service is temporarily unavailable. Please try again later."

    # Default
    return f"An error occurred: {error_type}. Please try again."


def is_retryable_status(status_code: int) -> bool:
    """
    Check if HTTP status code is retryable.

    Args:
        status_code: HTTP status code

    Returns:
        True if the error is transient and retryable
    """
    # 5xx server errors (except 501 Not Implemented)
    if status_code >= 500 and status_code != 501:
        return True

    # 429 Too Many Requests
    if status_code == 429:
        return True

    # 408 Request Timeout
    if status_code == 408:
        return True

    return False


# Pre-configured retry configs for common use cases
GOOGLE_API_RETRY = RetryConfig(
    max_retries=3,
    base_delay=1.0,
    max_delay=10.0,
    retryable_exceptions=(
        ConnectionError,
        TimeoutError,
    ),
)

CLAUDE_API_RETRY = RetryConfig(
    max_retries=2,
    base_delay=2.0,
    max_delay=15.0,
    retryable_exceptions=(
        ConnectionError,
        TimeoutError,
    ),
)
