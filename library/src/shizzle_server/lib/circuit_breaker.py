"""
Circuit Breaker pattern implementation for Karaoke Agent.

Prevents cascading failures by tracking errors and temporarily
blocking calls to failing services.

States:
- CLOSED: Normal operation, calls pass through
- OPEN: Service is failing, calls are blocked
- HALF-OPEN: Testing if service recovered

Usage:
    breaker = CircuitBreaker(failure_threshold=5, timeout_seconds=60)

    try:
        result = breaker.call(lambda: external_service_call())
    except RuntimeError as e:
        # Circuit is open or call failed
        pass
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)


T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""

    failure_threshold: int = 5
    """Number of consecutive failures before opening circuit."""

    timeout_seconds: int = 60
    """Seconds before attempting recovery (half-open state)."""

    success_threshold: int = 1
    """Number of successes in half-open before closing circuit."""


class CircuitBreaker(Generic[T]):
    """
    Circuit breaker pattern to prevent cascading failures.

    When a service fails repeatedly, the circuit "opens" and blocks
    further calls for a timeout period. After the timeout, it enters
    "half-open" state where a single call is allowed through to test
    if the service has recovered.

    Example:
        # Create breaker with defaults
        api_breaker = CircuitBreaker()

        # Or with custom settings
        api_breaker = CircuitBreaker(
            failure_threshold=5,
            timeout_seconds=60
        )

        # Wrap external calls
        def make_api_call():
            return api_breaker.call(lambda: requests.get(url))

        # Check state before attempting call
        if not api_breaker.is_open():
            result = api_breaker.call(lambda: service.call())
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        timeout_seconds: int = 60,
        success_threshold: int = 1,
        name: str = "default",
    ) -> None:
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            timeout_seconds: Time before attempting recovery (half-open)
            success_threshold: Successes needed in half-open to close circuit
            name: Optional name for logging
        """
        self.config = CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            timeout_seconds=timeout_seconds,
            success_threshold=success_threshold,
        )
        self.name = name

        self._failures = 0
        self._successes_in_half_open = 0
        self._last_failure_time: float | None = None
        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def failures(self) -> int:
        """Get current failure count."""
        return self._failures

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state with logging."""
        if new_state != self._state:
            logger.info(f"Circuit breaker '{self.name}': {self._state.value} -> {new_state.value}")
            self._state = new_state

    def _should_attempt_reset(self) -> bool:
        """Check if timeout has elapsed and we should try half-open."""
        if self._state != CircuitState.OPEN:
            return False
        if self._last_failure_time is None:
            return True
        elapsed = time.time() - self._last_failure_time
        return elapsed > self.config.timeout_seconds

    def _before_call(self) -> None:
        """Reject open circuits or move them to half-open after the timeout."""
        if self._state != CircuitState.OPEN:
            return
        if self._should_attempt_reset():
            self._transition_to(CircuitState.HALF_OPEN)
            logger.debug(f"Circuit breaker '{self.name}' entering half-open state")
            return
        raise RuntimeError(
            f"Circuit breaker '{self.name}' is open (failed {self._failures} times)"
        )

    def _record_success(self) -> None:
        """Close a recovered half-open circuit after enough successes."""
        if self._state != CircuitState.HALF_OPEN:
            return
        self._successes_in_half_open += 1
        if self._successes_in_half_open >= self.config.success_threshold:
            self._transition_to(CircuitState.CLOSED)
            self._failures = 0
            self._successes_in_half_open = 0
            logger.info(f"Circuit breaker '{self.name}' closed - service recovered")

    def call(self, func: Callable[[], T], *args: Any, **kwargs: Any) -> T:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Positional arguments for func (deprecated, pass via lambda)
            **kwargs: Keyword arguments for func (deprecated, pass via lambda)

        Returns:
            Result of func()

        Raises:
            RuntimeError: If circuit is open
            Any exception: If func raises and circuit stays closed/half-open
        """
        self._before_call()
        try:
            result = func(*args, **kwargs) if args or kwargs else func()
            self._record_success()
            return result
        except Exception:
            self._record_failure()
            raise

    async def call_async(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Execute an awaitable factory with circuit breaker protection."""
        self._before_call()
        try:
            result = await factory()
            self._record_success()
            return result
        except Exception:
            self._record_failure()
            raise

    def _record_failure(self) -> None:
        """Record a failure and potentially open the circuit."""
        self._failures += 1
        self._last_failure_time = time.time()
        self._successes_in_half_open = 0

        if self._failures >= self.config.failure_threshold:
            self._transition_to(CircuitState.OPEN)
            logger.error(
                f"Circuit breaker '{self.name}' opened after {self._failures} failures"
            )

    def is_open(self) -> bool:
        """
        Check if circuit is open (blocking calls).

        Also handles automatic transition to half-open if timeout elapsed.

        Returns:
            True if circuit is open and blocking calls
        """
        if self._state == CircuitState.OPEN and self._should_attempt_reset():
            self._transition_to(CircuitState.HALF_OPEN)
        return self._state == CircuitState.OPEN

    def is_closed(self) -> bool:
        """Check if circuit is closed (allowing calls)."""
        return self._state == CircuitState.CLOSED

    def get_status(self) -> str:
        """Get human-readable status string."""
        return f"{self._state.value} (failures: {self._failures}/{self.config.failure_threshold})"

    def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        self._transition_to(CircuitState.CLOSED)
        self._failures = 0
        self._successes_in_half_open = 0
        self._last_failure_time = None
        logger.info(f"Circuit breaker '{self.name}' manually reset")

    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"CircuitBreaker(name='{self.name}', state={self._state.value}, "
            f"failures={self._failures}/{self.config.failure_threshold})"
        )
