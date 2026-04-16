"""
tests/unit/test_rate_limiter.py
─────────────────────────────────
Unit tests for ResourceRateLimiter.
"""

import time
import pytest
from unittest.mock import patch

from operator.utils.rate_limiter import ResourceRateLimiter


@pytest.fixture
def limiter():
    return ResourceRateLimiter(
        global_max_per_hour=5,
        per_resource_max_per_hour=2,
        cooldown_seconds=10,
    )


class TestRateLimiter:

    def test_first_request_allowed(self, limiter):
        result = limiter.check("default", "my-pod", "CrashLoopBackOff")
        assert result.allowed is True

    def test_records_after_check(self, limiter):
        limiter.check("default", "my-pod", "CrashLoopBackOff")
        limiter.record("default", "my-pod", "CrashLoopBackOff")
        stats = limiter.stats()
        assert stats["global_count_last_hour"] == 1

    def test_per_resource_limit_reached(self, limiter):
        ns, name, trigger = "default", "pod-x", "OOMKilled"
        # First two allowed
        for _ in range(2):
            result = limiter.check(ns, name, trigger)
            assert result.allowed
            limiter.record(ns, name, trigger)
        # Third blocked
        result = limiter.check(ns, name, trigger)
        assert result.allowed is False
        assert "rate limit" in result.reason.lower()

    def test_different_resources_independent(self, limiter):
        # Fill up pod-a
        for _ in range(2):
            limiter.check("default", "pod-a", "CrashLoopBackOff")
            limiter.record("default", "pod-a", "CrashLoopBackOff")
        assert limiter.check("default", "pod-a", "CrashLoopBackOff").allowed is False
        # pod-b should still be allowed
        assert limiter.check("default", "pod-b", "CrashLoopBackOff").allowed is True

    def test_different_triggers_independent(self, limiter):
        # Fill up CrashLoopBackOff limit for pod-a
        for _ in range(2):
            limiter.check("default", "pod-a", "CrashLoopBackOff")
            limiter.record("default", "pod-a", "CrashLoopBackOff")
        assert limiter.check("default", "pod-a", "CrashLoopBackOff").allowed is False
        # Different trigger on same pod should be allowed
        assert limiter.check("default", "pod-a", "OOMKilled").allowed is True

    def test_global_limit_reached(self, limiter):
        # Fill global limit (5)
        for i in range(5):
            result = limiter.check("ns", f"pod-{i}", "CrashLoopBackOff")
            assert result.allowed
            limiter.record("ns", f"pod-{i}", "CrashLoopBackOff")
        # Next one blocked by global limit
        result = limiter.check("ns", "pod-new", "CrashLoopBackOff")
        assert result.allowed is False
        assert "global" in result.reason.lower()

    def test_cooldown_blocks_repeat(self, limiter):
        limiter.record("default", "pod-x", "CrashLoopBackOff")
        result = limiter.check("default", "pod-x", "CrashLoopBackOff")
        assert result.allowed is False
        assert "cooldown" in result.reason.lower()
        assert result.retry_after_seconds > 0

    def test_cooldown_expires(self, limiter):
        with patch("operator.utils.rate_limiter.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            limiter.record("default", "pod-x", "CrashLoopBackOff")

            # During cooldown — blocked
            mock_time.monotonic.return_value = 1005.0
            result = limiter.check("default", "pod-x", "CrashLoopBackOff")
            assert result.allowed is False

            # After cooldown — allowed
            mock_time.monotonic.return_value = 1011.0
            result = limiter.check("default", "pod-x", "CrashLoopBackOff")
            assert result.allowed is True

    def test_override_resource_max(self, limiter):
        ns, name, trigger = "default", "pod-x", "CrashLoopBackOff"
        # Allow 4 with override (default is 2)
        for _ in range(4):
            result = limiter.check(ns, name, trigger, override_resource_max=4)
            assert result.allowed
            limiter.record(ns, name, trigger)
        result = limiter.check(ns, name, trigger, override_resource_max=4)
        # Cooldown kicks in before resource limit since we record each time
        assert result.allowed is False

    def test_stats_returns_expected_keys(self, limiter):
        stats = limiter.stats()
        assert "global_count_last_hour" in stats
        assert "global_limit_per_hour" in stats
        assert "tracked_resources" in stats
