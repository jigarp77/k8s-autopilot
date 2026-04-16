"""
tests/unit/test_circuit_breaker.py
────────────────────────────────────
Unit tests for CircuitBreaker.
"""

import time
from unittest.mock import patch

import pytest

from autopilot.utils.circuit_breaker import CBState, CircuitBreaker


@pytest.fixture
def cb():
    return CircuitBreaker(failure_threshold=3, success_threshold=2, timeout_seconds=60, name="test")


class TestCircuitBreaker:
    def test_starts_closed(self, cb):
        assert cb.state == CBState.CLOSED
        assert cb.is_open() is False

    def test_opens_after_threshold_failures(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CBState.OPEN
        assert cb.is_open() is True

    def test_below_threshold_stays_closed(self, cb):
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CBState.CLOSED

    def test_transitions_to_half_open_after_timeout(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CBState.OPEN

        with patch("autopilot.utils.circuit_breaker.time") as mt:
            mt.monotonic.return_value = time.monotonic() + 61
            result = cb.is_open()
            assert cb.state == CBState.HALF_OPEN
            assert result is False

    def test_success_in_half_open_closes(self, cb):
        for _ in range(3):
            cb.record_failure()

        with patch("autopilot.utils.circuit_breaker.time") as mt:
            mt.monotonic.return_value = time.monotonic() + 61
            cb.is_open()

        cb.record_success()
        cb.record_success()
        assert cb.state == CBState.CLOSED

    def test_failure_in_half_open_reopens(self, cb):
        for _ in range(3):
            cb.record_failure()

        with patch("autopilot.utils.circuit_breaker.time") as mt:
            mt.monotonic.return_value = time.monotonic() + 61
            cb.is_open()

        cb.record_failure()
        assert cb.state == CBState.OPEN

    def test_success_in_closed_reduces_count(self, cb):
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failure_count == 1

    def test_total_opens_counter(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.stats()["total_opens"] == 1

    def test_stats_keys(self, cb):
        stats = cb.stats()
        assert "state" in stats
        assert "failure_count" in stats
        assert "total_opens" in stats
