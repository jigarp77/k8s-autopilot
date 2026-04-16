"""
tests/unit/test_rate_limiter.py
─────────────────────────────────
Unit tests for ResourceRateLimiter.
"""

from unittest.mock import patch

import pytest

from autopilot.utils.rate_limiter import ResourceRateLimiter


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

    def test_per_resource_limit_reached(self):
        # Use cooldown=0 so repeated checks aren't blocked by cooldown
        lim = ResourceRateLimiter(
            global_max_per_hour=10, per_resource_max_per_hour=2, cooldown_seconds=0
        )
        ns, name, trigger = "default", "pod-x", "OOMKilled"
        for _ in range(2):
            result = lim.check(ns, name, trigger)
            assert result.allowed
            lim.record(ns, name, trigger)
        result = lim.check(ns, name, trigger)
        assert result.allowed is False

    def test_different_resources_independent(self, limiter):
        for _ in range(2):
            limiter.check("default", "pod-a", "CrashLoopBackOff")
            limiter.record("default", "pod-a", "CrashLoopBackOff")
        assert limiter.check("default", "pod-a", "CrashLoopBackOff").allowed is False
        assert limiter.check("default", "pod-b", "CrashLoopBackOff").allowed is True

    def test_different_triggers_independent(self, limiter):
        for _ in range(2):
            limiter.check("default", "pod-a", "CrashLoopBackOff")
            limiter.record("default", "pod-a", "CrashLoopBackOff")
        assert limiter.check("default", "pod-a", "CrashLoopBackOff").allowed is False
        assert limiter.check("default", "pod-a", "OOMKilled").allowed is True

    def test_global_limit_reached(self, limiter):
        for i in range(5):
            result = limiter.check("ns", f"pod-{i}", "CrashLoopBackOff")
            assert result.allowed
            limiter.record("ns", f"pod-{i}", "CrashLoopBackOff")
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
        with patch("autopilot.utils.rate_limiter.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            limiter.record("default", "pod-x", "CrashLoopBackOff")

            mock_time.monotonic.return_value = 1005.0
            result = limiter.check("default", "pod-x", "CrashLoopBackOff")
            assert result.allowed is False

            mock_time.monotonic.return_value = 1011.0
            result = limiter.check("default", "pod-x", "CrashLoopBackOff")
            assert result.allowed is True

    def test_stats_returns_expected_keys(self, limiter):
        stats = limiter.stats()
        assert "global_count_last_hour" in stats
        assert "global_limit_per_hour" in stats
        assert "tracked_resources" in stats
