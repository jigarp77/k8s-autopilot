"""
operator/integrations/pagerduty.py
─────────────────────────────────────
PagerDuty Events API v2 integration.
Creates incidents for critical/high severity diagnoses.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from operator.config import PagerDutyConfig
from operator.engines.diagnosis_engine import Diagnosis, Severity

logger = logging.getLogger(__name__)

_PD_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
_SEVERITY_MAP  = {
    Severity.CRITICAL: "critical",
    Severity.HIGH:     "error",
    Severity.MEDIUM:   "warning",
    Severity.LOW:      "info",
}


class PagerDutyClient:

    def __init__(self, config: PagerDutyConfig) -> None:
        self._config = config

    async def trigger_incident(
        self,
        diagnosis:  Diagnosis,
        namespace:  str,
        name:       str,
        trigger:    str,
    ) -> Optional[str]:
        """Create a PagerDuty incident. Returns the dedup_key on success."""
        if not self._config.enabled:
            return None

        dedup_key = f"autopilot-{namespace}-{name}-{trigger}"
        severity  = _SEVERITY_MAP.get(diagnosis.severity, "error")

        payload = {
            "routing_key":  self._config.api_key,
            "event_action": "trigger",
            "dedup_key":    dedup_key,
            "payload": {
                "summary":   f"K8s Autopilot: {trigger} on {namespace}/{name}",
                "severity":  severity,
                "source":    f"k8s-autopilot/{namespace}/{name}",
                "component": name,
                "group":     namespace,
                "class":     trigger,
                "custom_details": {
                    "root_cause":       diagnosis.root_cause,
                    "confidence":       diagnosis.confidence,
                    "trigger_category": diagnosis.trigger_category.value,
                    "recommended_action": diagnosis.top_action.action if diagnosis.top_action else "none",
                },
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _PD_EVENTS_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status in (200, 202):
                        data = await resp.json()
                        logger.info(
                            "PagerDuty incident triggered for %s/%s (key=%s)",
                            namespace, name, dedup_key,
                        )
                        return data.get("dedup_key", dedup_key)
                    else:
                        text = await resp.text()
                        logger.error("PagerDuty API error %d: %s", resp.status, text[:200])
                        return None
        except Exception as exc:
            logger.error("PagerDuty request failed: %s", exc)
            return None

    async def resolve_incident(self, dedup_key: str) -> bool:
        """Resolve a previously triggered incident."""
        if not self._config.enabled:
            return False

        payload = {
            "routing_key":  self._config.api_key,
            "event_action": "resolve",
            "dedup_key":    dedup_key,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _PD_EVENTS_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    return resp.status in (200, 202)
        except Exception as exc:
            logger.error("PagerDuty resolve failed: %s", exc)
            return False
