"""
Memory monitoring utility for graceful shutdown of long-running processes.

Usage:
    from api.utils.memory_monitor import MemoryMonitor, check_memory

    # Simple check
    if check_memory().should_stop:
        save_state_and_exit()

    # Or use the monitor for tracking over time
    monitor = MemoryMonitor(critical_threshold=85.0)
    for item in items:
        status = monitor.check()
        if status.should_stop:
            logger.warning(f"Stopping: {status.reason}")
            break
        process(item)
"""
import logging
from dataclasses import dataclass
from typing import Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class MemoryStatus:
    """Current memory status."""
    percent_used: float
    available_gb: float
    total_gb: float
    should_stop: bool
    reason: str = ""

    def __str__(self) -> str:
        return f"Memory: {self.percent_used:.1f}% used, {self.available_gb:.1f}GB available"


class MemoryMonitor:
    """
    Monitor system memory and signal when to gracefully shut down.

    Thresholds:
    - warning_threshold (80%): Log warnings
    - critical_threshold (90%): Signal to stop
    - growth_rate_threshold (5%): Warn if memory grows this much between checks

    Features:
    - Tracks consecutive warnings and stops after 3+
    - Detects rapid memory growth
    - Periodic status logging (every N checks)
    """

    def __init__(
        self,
        warning_threshold: float = 80.0,
        critical_threshold: float = 90.0,
        growth_rate_threshold: float = 5.0,
        consecutive_warnings_to_stop: int = 3,
        log_interval: int = 10,
    ):
        """
        Initialize memory monitor.

        Args:
            warning_threshold: Log warnings above this % (default 80)
            critical_threshold: Signal stop above this % (default 90)
            growth_rate_threshold: Warn if memory grows this much between checks (default 5)
            consecutive_warnings_to_stop: Stop after this many consecutive warnings (default 3)
            log_interval: Log status every N checks (default 10)
        """
        self.warning_threshold = warning_threshold
        self.critical_threshold = critical_threshold
        self.growth_rate_threshold = growth_rate_threshold
        self.consecutive_warnings_to_stop = consecutive_warnings_to_stop
        self.log_interval = log_interval

        self.last_percent: Optional[float] = None
        self.checks = 0
        self.warnings = 0

    def check(self) -> MemoryStatus:
        """Check current memory status and return recommendation."""
        if not PSUTIL_AVAILABLE:
            return MemoryStatus(
                percent_used=0,
                available_gb=0,
                total_gb=0,
                should_stop=False,
                reason="psutil not available - memory monitoring disabled"
            )

        mem = psutil.virtual_memory()
        percent = mem.percent
        available_gb = mem.available / (1024 ** 3)
        total_gb = mem.total / (1024 ** 3)

        self.checks += 1
        should_stop = False
        reason = ""

        # Check absolute threshold
        if percent >= self.critical_threshold:
            should_stop = True
            reason = f"Memory critical: {percent:.1f}% used (threshold: {self.critical_threshold}%)"
            logger.error(f"MEMORY CRITICAL: {percent:.1f}% used, {available_gb:.1f}GB available")
        elif percent >= self.warning_threshold:
            self.warnings += 1
            logger.warning(f"Memory high: {percent:.1f}% used, {available_gb:.1f}GB available")
            if self.warnings >= self.consecutive_warnings_to_stop:
                should_stop = True
                reason = f"Memory sustained high: {percent:.1f}% for {self.warnings} checks"
        else:
            self.warnings = 0  # Reset warning counter

        # Check growth rate
        if self.last_percent is not None:
            growth = percent - self.last_percent
            if growth > self.growth_rate_threshold:
                logger.warning(f"Memory growing fast: +{growth:.1f}% since last check")
                if percent > 75:  # Only concern if already moderately high
                    should_stop = True
                    reason = f"Memory growing rapidly: +{growth:.1f}% (now at {percent:.1f}%)"

        self.last_percent = percent

        # Log periodic status
        if self.log_interval > 0 and self.checks % self.log_interval == 0:
            logger.info(f"Memory status: {percent:.1f}% used, {available_gb:.1f}GB available")

        return MemoryStatus(
            percent_used=percent,
            available_gb=available_gb,
            total_gb=total_gb,
            should_stop=should_stop,
            reason=reason
        )

    def get_summary(self) -> str:
        """Get summary of memory monitoring."""
        if not PSUTIL_AVAILABLE:
            return "Memory monitoring disabled (psutil not installed)"
        mem = psutil.virtual_memory()
        return f"Final memory: {mem.percent:.1f}% used, {mem.available / (1024**3):.1f}GB available ({self.checks} checks)"

    def reset(self):
        """Reset monitoring state."""
        self.last_percent = None
        self.checks = 0
        self.warnings = 0


# Convenience function for simple one-off checks
def check_memory(critical_threshold: float = 90.0) -> MemoryStatus:
    """
    Simple one-off memory check.

    Args:
        critical_threshold: Return should_stop=True if above this %

    Returns:
        MemoryStatus with current state
    """
    if not PSUTIL_AVAILABLE:
        return MemoryStatus(
            percent_used=0,
            available_gb=0,
            total_gb=0,
            should_stop=False,
            reason="psutil not available"
        )

    mem = psutil.virtual_memory()
    percent = mem.percent
    available_gb = mem.available / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)

    should_stop = percent >= critical_threshold
    reason = f"Memory at {percent:.1f}%" if should_stop else ""

    return MemoryStatus(
        percent_used=percent,
        available_gb=available_gb,
        total_gb=total_gb,
        should_stop=should_stop,
        reason=reason
    )


def get_memory_info() -> dict:
    """Get current memory info as a dict (useful for logging/APIs)."""
    if not PSUTIL_AVAILABLE:
        return {"error": "psutil not available"}

    mem = psutil.virtual_memory()
    return {
        "percent_used": mem.percent,
        "available_gb": round(mem.available / (1024 ** 3), 2),
        "total_gb": round(mem.total / (1024 ** 3), 2),
        "used_gb": round(mem.used / (1024 ** 3), 2),
    }
