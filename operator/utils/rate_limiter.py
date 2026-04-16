"""
operator/utils/rate_limiter.py
────────────────────────────────
Token-bucket rate limiter with per-resource and global limits.

Features:
  • Per-resource sliding window (e.g. max 3 remediations/hour on pod X)
  • Global sliding window (e.g. max 20 remediations/hour cluster-wide)
  • Per-resource cooldown periods (no action for N seconds after last remediation)
  • Thread-safe (asyncio-based, no locks needed)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    allowed: bool
    reason:  str = ""
    retry_after_seconds: int = 0


class ResourceRateLimiter:
    """
    Tracks remediation attempts per resource (namespace/name/trigger) and globally.
    Uses a deque-based sliding window so no background cleanup is needed.
    """

    def __init__(
        self,
        global_max_per_hour:       int = 20,
        per_resource_max_per_hour: int = 3,
        cooldown_seconds:          int = 300,
    ) -> None:
        self._global_max  = global_max_per_hour
        self._resource_max = per_resource_max_per_hour
        self._cooldown    = cooldown_seconds
        self._window      = 3600  # 1 hour in seconds

        # resource_key → deque of timestamps (floats)
        self._resource_windows: dict[str, deque] = defaultdict(deque)
        # resource_key → timestamp of last remediation
        self._last_remediation:  dict[str, float] = {}
        # global window
        self._global_window: deque = deque()

    def _resource_key(self, namespace: str, name: str, trigger: str) -> str:
        return f"{namespace}/{name}/{trigger}"

    def _prune(self, window: deque, cutoff: float) -> None:
        """Remove entries older than cutoff from the left of the deque."""
        while window and window[0] < cutoff:
            window.popleft()

    def check(
        self,
        namespace: str,
        name:      str,
        trigger:   str,
        override_resource_max: Optional[int] = None,
    ) -> RateLimitResult:
        """Check whether a remediation is allowed right now."""
        now     = time.monotonic()
        cutoff  = now - self._window
        key     = self._resource_key(namespace, name, trigger)

        # 1. Cooldown check
        last = self._last_remediation.get(key)
        if last and (now - last) < self._cooldown:
            wait = int(self._cooldown - (now - last))
            return RateLimitResult(
                allowed=False,
                reason=f"Cooldown active for {namespace}/{name} ({trigger}) — retry in {wait}s",
                retry_after_seconds=wait,
            )

        # 2. Per-resource window check
        res_window = self._resource_windows[key]
        self._prune(res_window, cutoff)
        resource_limit = override_resource_max or self._resource_max
        if len(res_window) >= resource_limit:
            oldest  = res_window[0]
            wait    = int(self._window - (now - oldest)) + 1
            return RateLimitResult(
                allowed=False,
                reason=(
                    f"Per-resource rate limit reached for {namespace}/{name} ({trigger}) — "
                    f"{len(res_window)}/{resource_limit} in the last hour. Retry in {wait}s"
                ),
                retry_after_seconds=wait,
            )

        # 3. Global window check
        self._prune(self._global_window, cutoff)
        if len(self._global_window) >= self._global_max:
            oldest = self._global_window[0]
            wait   = int(self._window - (now - oldest)) + 1
            return RateLimitResult(
                allowed=False,
                reason=(
                    f"Global rate limit reached — "
                    f"{len(self._global_window)}/{self._global_max} in the last hour. "
                    f"Retry in {wait}s"
                ),
                retry_after_seconds=wait,
            )

        return RateLimitResult(allowed=True)

    def record(self, namespace: str, name: str, trigger: str) -> None:
        """Record that a remediation was executed."""
        now = time.monotonic()
        key = self._resource_key(namespace, name, trigger)
        self._resource_windows[key].append(now)
        self._global_window.append(now)
        self._last_remediation[key] = now
        logger.debug(
            "Rate limit recorded: %s/%s (%s) | resource=%d global=%d",
            namespace, name, trigger,
            len(self._resource_windows[key]),
            len(self._global_window),
        )

    def stats(self) -> dict:
        now    = time.monotonic()
        cutoff = now - self._window
        self._prune(self._global_window, cutoff)
        return {
            "global_count_last_hour":    len(self._global_window),
            "global_limit_per_hour":     self._global_max,
            "tracked_resources":         len(self._resource_windows),
            "cooldown_seconds":          self._cooldown,
        }
