"""Tests for CircuitBreaker state transitions."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from shizzle_server.lib.circuit_breaker import CircuitBreaker, CircuitState


class TestCircuitBreakerStates:
    """Test circuit breaker state transitions."""

    def test_initial_state_is_closed(self, circuit_breaker: CircuitBreaker) -> None:
        """Circuit breaker starts in closed state."""
        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.is_closed()
        assert not circuit_breaker.is_open()

    def test_successful_calls_keep_circuit_closed(
        self, circuit_breaker: CircuitBreaker
    ) -> None:
        """Successful calls maintain closed state."""
        for _ in range(10):
            result = circuit_breaker.call(lambda: "success")
            assert result == "success"

        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.failures == 0

    def test_failures_open_circuit_at_threshold(
        self, circuit_breaker: CircuitBreaker
    ) -> None:
        """Circuit opens after reaching failure threshold."""
        # Threshold is 3 for test fixture
        for _ in range(3):
            with pytest.raises(ZeroDivisionError):
                circuit_breaker.call(lambda: 1 / 0)

        assert circuit_breaker.state == CircuitState.OPEN
        assert circuit_breaker.is_open()
        assert circuit_breaker.failures == 3

    def test_open_circuit_blocks_calls(self, circuit_breaker: CircuitBreaker) -> None:
        """Open circuit raises RuntimeError without calling function."""
        # Open the circuit
        for _ in range(3):
            with pytest.raises(ZeroDivisionError):
                circuit_breaker.call(lambda: 1 / 0)

        # Verify circuit is open
        assert circuit_breaker.is_open()

        # Call should raise RuntimeError without executing function
        call_count = 0

        def tracked_call() -> str:
            nonlocal call_count
            call_count += 1
            return "called"

        with pytest.raises(RuntimeError, match="is open"):
            circuit_breaker.call(tracked_call)

        assert call_count == 0  # Function was not called

    def test_circuit_transitions_to_half_open_after_timeout(
        self, circuit_breaker: CircuitBreaker
    ) -> None:
        """Circuit enters half-open state after timeout expires."""
        # Open the circuit
        for _ in range(3):
            with pytest.raises(ZeroDivisionError):
                circuit_breaker.call(lambda: 1 / 0)

        assert circuit_breaker.is_open()

        # Wait for timeout (1 second in test fixture)
        time.sleep(1.1)

        # Check should transition to half-open
        assert not circuit_breaker.is_open()
        assert circuit_breaker.state == CircuitState.HALF_OPEN

    def test_successful_call_in_half_open_closes_circuit(
        self, circuit_breaker: CircuitBreaker
    ) -> None:
        """Successful call in half-open state closes the circuit."""
        # Open the circuit
        for _ in range(3):
            with pytest.raises(ZeroDivisionError):
                circuit_breaker.call(lambda: 1 / 0)

        # Wait for timeout
        time.sleep(1.1)

        # Verify half-open
        circuit_breaker.is_open()  # Triggers transition
        assert circuit_breaker.state == CircuitState.HALF_OPEN

        # Successful call
        result = circuit_breaker.call(lambda: "recovered")
        assert result == "recovered"

        # Circuit should be closed
        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.failures == 0

    def test_failed_call_in_half_open_reopens_circuit(
        self, circuit_breaker: CircuitBreaker
    ) -> None:
        """Failed call in half-open state reopens the circuit."""
        # Open the circuit
        for _ in range(3):
            with pytest.raises(ZeroDivisionError):
                circuit_breaker.call(lambda: 1 / 0)

        # Wait for timeout
        time.sleep(1.1)

        # Trigger half-open
        circuit_breaker.is_open()
        assert circuit_breaker.state == CircuitState.HALF_OPEN

        # Failed call
        with pytest.raises(ValueError):
            circuit_breaker.call(lambda: int("not a number"))

        # Circuit should be open again
        assert circuit_breaker.state == CircuitState.OPEN
        assert circuit_breaker.failures == 4  # Original 3 + 1

    def test_reset_clears_state(self, circuit_breaker: CircuitBreaker) -> None:
        """Manual reset clears all state."""
        # Open the circuit
        for _ in range(3):
            with pytest.raises(ZeroDivisionError):
                circuit_breaker.call(lambda: 1 / 0)

        assert circuit_breaker.is_open()
        assert circuit_breaker.failures == 3

        # Reset
        circuit_breaker.reset()

        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.failures == 0
        assert not circuit_breaker.is_open()

    def test_get_status_returns_readable_string(
        self, circuit_breaker: CircuitBreaker
    ) -> None:
        """get_status returns human-readable status."""
        status = circuit_breaker.get_status()
        assert "closed" in status
        assert "0/3" in status

        # Cause some failures
        with contextlib.suppress(ZeroDivisionError):
            circuit_breaker.call(lambda: 1 / 0)

        status = circuit_breaker.get_status()
        assert "1/3" in status


class TestCircuitBreakerConfig:
    """Test circuit breaker configuration."""

    def test_custom_failure_threshold(self) -> None:
        """Circuit breaker respects custom failure threshold."""
        breaker = CircuitBreaker(failure_threshold=5, timeout_seconds=60)

        # 4 failures should not open
        for _ in range(4):
            with pytest.raises(ValueError):
                breaker.call(lambda: int("x"))

        assert breaker.state == CircuitState.CLOSED

        # 5th failure opens it
        with pytest.raises(ValueError):
            breaker.call(lambda: int("x"))

        assert breaker.state == CircuitState.OPEN

    def test_custom_timeout(self) -> None:
        """Circuit breaker respects custom timeout."""
        breaker = CircuitBreaker(failure_threshold=1, timeout_seconds=2)

        # Open circuit
        with pytest.raises(ValueError):
            breaker.call(lambda: int("x"))

        assert breaker.is_open()

        # Wait less than timeout
        time.sleep(0.5)
        assert breaker.is_open()

        # Wait for full timeout
        time.sleep(1.6)
        assert not breaker.is_open()  # Should be half-open now

    def test_named_circuit_breaker(self) -> None:
        """Circuit breaker name appears in repr and status."""
        breaker = CircuitBreaker(name="api-service")
        assert "api-service" in repr(breaker)


async def test_half_open_allows_only_one_async_recovery_probe() -> None:
    breaker = CircuitBreaker(failure_threshold=1, timeout_seconds=-1)

    async def fail() -> None:
        raise ValueError("down")

    with pytest.raises(ValueError):
        await breaker.call_async(fail)

    started = asyncio.Event()
    release = asyncio.Event()

    async def probe() -> str:
        started.set()
        await release.wait()
        return "recovered"

    task = asyncio.create_task(breaker.call_async(probe))
    await started.wait()
    with pytest.raises(RuntimeError, match="probe in flight"):
        await breaker.call_async(probe)
    release.set()
    assert await task == "recovered"
    assert breaker.state == CircuitState.CLOSED
