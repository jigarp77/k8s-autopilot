"""
operator/integrations/prometheus.py
──────────────────────────────────────
Prometheus metrics exposition for K8s Autopilot.

Exposes metrics at :8000/metrics (configurable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        Counter, Gauge, Histogram, Summary,
        start_http_server, REGISTRY,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    logger.warning("prometheus-client not installed — metrics disabled")


class _NoOpMetric:
    """Stub when prometheus_client is not installed."""
    def labels(self, **kwargs) -> "_NoOpMetric": return self
    def inc(self, amount: float = 1) -> None: pass
    def dec(self, amount: float = 1) -> None: pass
    def set(self, value: float) -> None: pass
    def observe(self, value: float) -> None: pass


@dataclass
class AutopilotMetrics:
    """All Prometheus metrics for the operator."""

    remediations_total:    object = field(default_factory=_NoOpMetric)
    events_processed:      object = field(default_factory=_NoOpMetric)
    diagnosis_duration:    object = field(default_factory=_NoOpMetric)
    rate_limited_total:    object = field(default_factory=_NoOpMetric)
    circuit_open_total:    object = field(default_factory=_NoOpMetric)
    approval_pending:      object = field(default_factory=_NoOpMetric)
    ai_tokens_used:        object = field(default_factory=_NoOpMetric)

    @classmethod
    def create(cls, port: int = 8000, enabled: bool = True) -> "AutopilotMetrics":
        if not enabled or not _PROMETHEUS_AVAILABLE:
            return cls()

        metrics = cls(
            remediations_total = Counter(
                "autopilot_remediations_total",
                "Total remediation actions executed",
                ["action", "trigger", "outcome"],
            ),
            events_processed = Counter(
                "autopilot_events_processed_total",
                "Total K8s events processed by the operator",
                ["kind", "trigger"],
            ),
            diagnosis_duration = Histogram(
                "autopilot_diagnosis_duration_seconds",
                "Time taken for AI diagnosis",
                buckets=[0.5, 1, 2, 5, 10, 20, 30],
            ),
            rate_limited_total = Counter(
                "autopilot_rate_limited_total",
                "Remediations blocked by rate limiter",
            ),
            circuit_open_total = Counter(
                "autopilot_circuit_open_total",
                "Remediations blocked by open circuit breaker",
            ),
            approval_pending = Gauge(
                "autopilot_approval_pending",
                "Number of remediation approvals awaiting human decision",
            ),
            ai_tokens_used = Counter(
                "autopilot_ai_tokens_total",
                "Total AI tokens consumed for diagnoses",
            ),
        )

        try:
            start_http_server(port)
            logger.info("Prometheus metrics server started on :%d", port)
        except Exception as exc:
            logger.warning("Could not start Prometheus server: %s", exc)

        return metrics
