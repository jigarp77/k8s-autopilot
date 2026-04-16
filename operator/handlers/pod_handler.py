"""
operator/handlers/pod_handler.py
──────────────────────────────────
Kopf event handlers for pod failures.

Triggers detected:
  • CrashLoopBackOff      — restart count > threshold
  • OOMKilled             — container terminated with OOMKilled reason
  • ImagePullBackOff      — image pull failure
  • ErrImagePull
  • Pending (Unschedulable)— pod stuck in Pending
  • LivenessProbeFailure  — container killed by liveness probe
  • ContainerCreating (stuck)
"""

from __future__ import annotations

import logging
from typing import Any

import kopf

from operator.engines.context_collector import ContextCollector
from operator.engines.diagnosis_engine import DiagnosisEngine
from operator.engines.remediation_engine import RemediationEngine
from operator.engines.approval_engine import ApprovalEngine
from operator.integrations.slack import SlackClient
from operator.integrations.pagerduty import PagerDutyClient
from operator.integrations.prometheus import AutopilotMetrics
from operator.config import OperatorConfig

logger = logging.getLogger(__name__)


# Minimum restart count before acting on CrashLoopBackOff
_MIN_CRASH_RESTARTS = 3


def _get_crash_trigger(pod: dict) -> tuple[str, str]:
    """
    Examine pod status and return (trigger_name, reason).
    Returns ("", "") if no actionable trigger found.
    """
    status = pod.get("status", {})
    phase  = status.get("phase", "")

    # Check container statuses
    for cs in status.get("containerStatuses", []) + status.get("initContainerStatuses", []):
        waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
        reason  = waiting.get("reason", "")

        if reason == "CrashLoopBackOff":
            restart_count = cs.get("restartCount", 0)
            return ("CrashLoopBackOff", f"Container {cs['name']} in CrashLoopBackOff (restarts={restart_count})")

        if reason in ("ImagePullBackOff", "ErrImagePull"):
            return ("ImagePullError", f"Container {cs['name']}: {reason} — {waiting.get('message', '')}")

        terminated = (cs.get("lastState", {}) or {}).get("terminated", {}) or {}
        if terminated.get("reason") == "OOMKilled":
            return ("OOMKilled", f"Container {cs['name']} OOM killed")

        if terminated.get("exitCode") == 137:
            return ("OOMKilled", f"Container {cs['name']} killed (exit 137, likely OOM)")

    # Pod stuck in Pending
    if phase == "Pending":
        for cond in status.get("conditions", []):
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                return ("PendingScheduling", cond.get("message", "Pod unschedulable"))

    # Liveness probe failure (recorded in events, not status — handled via event handler)

    return "", ""


class PodHandler:
    """Registered with kopf in main.py."""

    def __init__(
        self,
        config:    OperatorConfig,
        collector: ContextCollector,
        diagnoser: DiagnosisEngine,
        remediator: RemediationEngine,
        slack:     SlackClient,
        pagerduty: PagerDutyClient,
        metrics:   AutopilotMetrics,
    ) -> None:
        self._config    = config
        self._collector = collector
        self._diagnoser = diagnoser
        self._remediator= remediator
        self._slack     = slack
        self._pd        = pagerduty
        self._metrics   = metrics

    async def on_pod_event(self, event: dict, **kwargs: Any) -> None:
        """Called by kopf for every pod event."""
        pod       = event.get("object", {})
        meta      = pod.get("metadata", {})
        namespace = meta.get("namespace", "")
        name      = meta.get("name", "")

        if not self._config.is_namespace_watched(namespace):
            return

        trigger, reason = _get_crash_trigger(pod)
        if not trigger:
            return

        # Minimum restart count guard for CrashLoopBackOff
        if trigger == "CrashLoopBackOff":
            for cs in pod.get("status", {}).get("containerStatuses", []):
                if cs.get("restartCount", 0) < _MIN_CRASH_RESTARTS:
                    logger.debug(
                        "Ignoring CrashLoopBackOff for %s/%s — restarts < %d",
                        namespace, name, _MIN_CRASH_RESTARTS,
                    )
                    return

        logger.info(
            "Pod trigger detected: %s/%s trigger=%s reason=%s",
            namespace, name, trigger, reason,
        )
        self._metrics.events_processed.labels(kind="pod", trigger=trigger).inc()

        await self._process(namespace, name, trigger)

    async def _process(self, namespace: str, name: str, trigger: str) -> None:
        import time
        t0 = time.monotonic()

        # 1. Collect context
        try:
            ctx = await self._collector.collect_pod_context(namespace, name)
        except Exception as exc:
            logger.error("Context collection failed for %s/%s: %s", namespace, name, exc)
            return

        # 2. Diagnose
        try:
            diagnosis = await self._diagnoser.diagnose_pod(ctx, trigger)
            self._metrics.diagnosis_duration.observe(time.monotonic() - t0)
            self._metrics.ai_tokens_used.inc(diagnosis.tokens_used)
        except Exception as exc:
            logger.error("Diagnosis failed for %s/%s: %s", namespace, name, exc)
            return

        logger.info(
            "Diagnosis for %s/%s: category=%s severity=%s confidence=%.0f%% action=%s",
            namespace, name,
            diagnosis.trigger_category.value,
            diagnosis.severity.value,
            diagnosis.confidence * 100,
            diagnosis.top_action.action if diagnosis.top_action else "none",
        )

        # 3. Notify Slack
        await self._slack.post_incident_notification(
            diagnosis   = diagnosis,
            namespace   = namespace,
            name        = name,
            trigger     = trigger,
        )

        # 4. PagerDuty for critical/high
        from operator.engines.diagnosis_engine import Severity
        if diagnosis.severity in (Severity.CRITICAL, Severity.HIGH):
            await self._pd.trigger_incident(diagnosis, namespace, name, trigger)

        # 5. Remediate
        result = await self._remediator.handle(
            diagnosis     = diagnosis,
            namespace     = namespace,
            name          = name,
            trigger       = trigger,
            resource_kind = "pod",
        )

        logger.info(
            "Remediation outcome for %s/%s: %s — %s",
            namespace, name, result.outcome.value, result.message,
        )
