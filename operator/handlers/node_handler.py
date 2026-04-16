"""
operator/handlers/node_handler.py
───────────────────────────────────
Kopf handlers for node-level failures.

Triggers:
  • NodeNotReady        — node condition Ready=False
  • NodeDiskPressure    — condition DiskPressure=True
  • NodeMemoryPressure  — condition MemoryPressure=True
  • NodePIDPressure     — condition PIDPressure=True
"""

from __future__ import annotations

import logging
from typing import Any

from operator.config import OperatorConfig
from operator.engines.context_collector import ContextCollector
from operator.engines.diagnosis_engine import DiagnosisEngine
from operator.engines.remediation_engine import RemediationEngine
from operator.integrations.slack import SlackClient
from operator.integrations.pagerduty import PagerDutyClient
from operator.integrations.prometheus import AutopilotMetrics

logger = logging.getLogger(__name__)

# Map node condition → trigger name
_CONDITION_TRIGGER = {
    "Ready":          ("NodeNotReady",       lambda status: status == "False"),
    "DiskPressure":   ("NodeDiskPressure",   lambda status: status == "True"),
    "MemoryPressure": ("NodeMemoryPressure", lambda status: status == "True"),
    "PIDPressure":    ("NodePIDPressure",    lambda status: status == "True"),
}


def _get_node_trigger(node: dict) -> tuple[str, str]:
    conditions = node.get("status", {}).get("conditions", [])
    for cond in conditions:
        ctype  = cond.get("type", "")
        status = cond.get("status", "")
        if ctype in _CONDITION_TRIGGER:
            trigger_name, predicate = _CONDITION_TRIGGER[ctype]
            if predicate(status):
                return trigger_name, cond.get("message", "")
    return "", ""


class NodeHandler:

    def __init__(
        self,
        config:     OperatorConfig,
        collector:  ContextCollector,
        diagnoser:  DiagnosisEngine,
        remediator: RemediationEngine,
        slack:      SlackClient,
        pagerduty:  PagerDutyClient,
        metrics:    AutopilotMetrics,
    ) -> None:
        self._config    = config
        self._collector = collector
        self._diagnoser = diagnoser
        self._remediator= remediator
        self._slack     = slack
        self._pd        = pagerduty
        self._metrics   = metrics

    async def on_node_event(self, event: dict, **kwargs: Any) -> None:
        node      = event.get("object", {})
        node_name = node.get("metadata", {}).get("name", "")

        trigger, reason = _get_node_trigger(node)
        if not trigger:
            return

        logger.info("Node trigger: %s trigger=%s reason=%s", node_name, trigger, reason)
        self._metrics.events_processed.labels(kind="node", trigger=trigger).inc()

        await self._process(node_name, trigger)

    async def _process(self, node_name: str, trigger: str) -> None:
        import time
        t0 = time.monotonic()

        ctx = await self._collector.collect_node_context(node_name)
        diagnosis = await self._diagnoser.diagnose_node(ctx, trigger)
        self._metrics.diagnosis_duration.observe(time.monotonic() - t0)

        await self._slack.post_incident_notification(
            diagnosis=diagnosis, namespace="", name=node_name, trigger=trigger,
        )

        from operator.engines.diagnosis_engine import Severity
        if diagnosis.severity in (Severity.CRITICAL, Severity.HIGH):
            await self._pd.trigger_incident(diagnosis, "nodes", node_name, trigger)

        result = await self._remediator.handle(
            diagnosis=diagnosis, namespace="", name=node_name,
            trigger=trigger, resource_kind="node",
        )
        logger.info("Node remediation: %s → %s", node_name, result.outcome.value)
