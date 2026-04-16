"""
autopilot/utils/circuit_breaker.py
────────────────────────────────────
Circuit breaker to halt remediation when consecutive failures indicate a
broken remediation path (e.g. RBAC misconfiguration, API unavailable).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CBState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout_seconds: int = 600
    name: str = "default"

    _state: CBState = field(default=CBState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _total_opens: int = field(default=0, init=False)

    def is_open(self) -> bool:
        if self._state == CBState.CLOSED:
            return False

        if self._state == CBState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.timeout_seconds:
                logger.info("[CircuitBreaker:%s] Transitioning OPEN → HALF_OPEN", self.name)
                self._state = CBState.HALF_OPEN
                self._success_count = 0
                return False
            return True

        return False

    def record_success(self) -> None:
        if self._state == CBState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                logger.info(
                    "[CircuitBreaker:%s] Transitioning HALF_OPEN → CLOSED after %d successes",
                    self.name,
                    self._success_count,
                )
                self._state = CBState.CLOSED
                self._failure_count = 0
        elif self._state == CBState.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CBState.HALF_OPEN:
            logger.warning("[CircuitBreaker:%s] Failure during HALF_OPEN → re-opening", self.name)
            self._state = CBState.OPEN
            self._failure_count = self.failure_threshold
            self._total_opens += 1
            return

        if self._state == CBState.CLOSED and self._failure_count >= self.failure_threshold:
            logger.error(
                "[CircuitBreaker:%s] %d consecutive failures → OPEN (pausing for %ds)",
                self.name,
                self._failure_count,
                self.timeout_seconds,
            )
            self._state = CBState.OPEN
            self._total_opens += 1

    @property
    def state(self) -> CBState:
        return self._state

    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "total_opens": self._total_opens,
            "timeout_seconds": self.timeout_seconds,
        }
