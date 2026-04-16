"""
operator/engines/diagnosis_engine.py
───────────────────────────────────────
Sends gathered context to Claude and returns a structured diagnosis:

  • Root cause classification
  • Confidence score
  • Recommended remediation actions (ordered by preference)
  • Human-readable summary for Slack notification
  • Generated runbook snippet
  • Severity level
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import anthropic

from operator.config import OperatorConfig
from operator.engines.context_collector import PodContext, NodeContext

logger = logging.getLogger(__name__)


# ── Output types ──────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"


class TriggerCategory(str, Enum):
    CRASH_LOOP_BACK_OFF = "CrashLoopBackOff"
    OOM_KILLED          = "OOMKilled"
    IMAGE_PULL_ERROR    = "ImagePullError"
    PENDING_SCHEDULING  = "PendingScheduling"
    LIVENESS_PROBE_FAIL = "LivenessProbeFailure"
    NODE_NOT_READY      = "NodeNotReady"
    NODE_DISK_PRESSURE  = "NodeDiskPressure"
    NODE_MEMORY_PRESSURE= "NodeMemoryPressure"
    DEPLOYMENT_STALLED  = "DeploymentStalled"
    CONFIG_ERROR        = "ConfigurationError"
    DEPENDENCY_FAILURE  = "DependencyFailure"
    UNKNOWN             = "Unknown"


@dataclass
class RemediationAction:
    action:       str    # registry key, e.g. "restart_pod", "scale_deployment"
    description:  str    # human-readable explanation
    confidence:   float  # 0.0–1.0
    is_safe:      bool   # whether this is a safe auto-action
    parameters:   dict   = field(default_factory=dict)


@dataclass
class Diagnosis:
    trigger_category:   TriggerCategory
    root_cause:         str
    summary:            str          # 1–2 sentences for Slack
    severity:           Severity
    confidence:         float        # 0.0–1.0
    recommended_actions: list[RemediationAction] = field(default_factory=list)
    runbook:            str  = ""
    affected_resource:  str  = ""
    namespace:          str  = ""
    raw_response:       str  = ""    # full Claude response for audit
    tokens_used:        int  = 0

    @property
    def top_action(self) -> Optional[RemediationAction]:
        return self.recommended_actions[0] if self.recommended_actions else None

    def to_dict(self) -> dict:
        return {
            "trigger_category":  self.trigger_category.value,
            "root_cause":        self.root_cause,
            "summary":           self.summary,
            "severity":          self.severity.value,
            "confidence":        self.confidence,
            "recommended_actions": [
                {
                    "action":      a.action,
                    "description": a.description,
                    "confidence":  a.confidence,
                    "is_safe":     a.is_safe,
                    "parameters":  a.parameters,
                }
                for a in self.recommended_actions
            ],
            "runbook": self.runbook,
        }


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert Kubernetes SRE with deep knowledge of container orchestration, \
Linux systems, application debugging, and production incident response.

You will receive context about a failing Kubernetes resource (pod, node, or deployment) \
including container logs, K8s events, resource configuration, and node status.

Your job is to:
1. Identify the precise root cause
2. Assess severity and confidence
3. Recommend specific, executable remediation actions in priority order
4. Generate a short runbook for this class of failure

Return ONLY a valid JSON object matching this schema — no markdown, no preamble:
{
  "trigger_category": "<one of: CrashLoopBackOff|OOMKilled|ImagePullError|PendingScheduling|LivenessProbeFailure|NodeNotReady|NodeDiskPressure|NodeMemoryPressure|DeploymentStalled|ConfigurationError|DependencyFailure|Unknown>",
  "root_cause": "<precise technical root cause — 1-2 sentences>",
  "summary": "<1-2 sentence plain-English summary for on-call engineer>",
  "severity": "<critical|high|medium|low>",
  "confidence": <0.0 to 1.0>,
  "recommended_actions": [
    {
      "action": "<registry_key: restart_pod|delete_pod|scale_deployment|rollback_deployment|restart_deployment|cordon_node|drain_node|increase_memory_limit|no_action>",
      "description": "<why this action and what it will do>",
      "confidence": <0.0 to 1.0>,
      "is_safe": <true if this is safe to automate, false if human review needed>,
      "parameters": {}
    }
  ],
  "runbook": "<markdown runbook: cause, diagnosis steps, remediation steps, prevention>"
}

Severity guidelines:
  critical: Service down / users impacted now, needs immediate action
  high:     Service degraded, likely to worsen, act within 15 minutes
  medium:   Service affected but stable, act within 1 hour
  low:      Advisory, act during business hours

Safe auto-actions (is_safe=true): restart_pod, delete_pod (only if in CrashLoop), scale_deployment (only to reduce replicas for OOM)
Always require approval (is_safe=false): rollback_deployment, cordon_node, drain_node, increase_memory_limit
"""


def _build_user_prompt(context_text: str, trigger: str, resource_id: str) -> str:
    return f"""\
FAILING RESOURCE: {resource_id}
TRIGGER: {trigger}

{context_text}

Analyse the above context and return your structured JSON diagnosis.
"""


# ── Engine ────────────────────────────────────────────────────────────────────

class DiagnosisEngine:

    def __init__(self, config: OperatorConfig) -> None:
        self._config = config
        self._client = anthropic.Anthropic(api_key=config.anthropic.api_key)

    async def diagnose_pod(
        self, context: PodContext, trigger: str
    ) -> Diagnosis:
        resource_id = f"{context.namespace}/{context.pod_name}"
        context_text = context.to_text()
        return await self._diagnose(context_text, trigger, resource_id, context.namespace)

    async def diagnose_node(
        self, context: NodeContext, trigger: str
    ) -> Diagnosis:
        resource_id  = f"node/{context.node_name}"
        context_text = context.to_text()
        return await self._diagnose(context_text, trigger, resource_id, "")

    async def _diagnose(
        self,
        context_text: str,
        trigger:      str,
        resource_id:  str,
        namespace:    str,
    ) -> Diagnosis:
        logger.info("Running AI diagnosis for %s (trigger=%s)", resource_id, trigger)

        user_prompt = _build_user_prompt(context_text, trigger, resource_id)

        # Cap context to avoid exceeding model limits
        if len(user_prompt) > 100_000:
            logger.warning(
                "Context for %s is %d chars — truncating to 100k", resource_id, len(user_prompt)
            )
            user_prompt = user_prompt[:100_000] + "\n\n[CONTEXT TRUNCATED]"

        try:
            response = self._client.messages.create(
                model      = self._config.anthropic.model,
                max_tokens = self._config.anthropic.max_tokens,
                system     = _SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": user_prompt}],
            )

            raw_text   = response.content[0].text
            tokens_in  = response.usage.input_tokens
            tokens_out = response.usage.output_tokens

            logger.debug(
                "Claude response for %s: %d input + %d output tokens",
                resource_id, tokens_in, tokens_out,
            )

            return self._parse_response(
                raw_text    = raw_text,
                resource_id = resource_id,
                namespace   = namespace,
                tokens      = tokens_in + tokens_out,
            )

        except anthropic.APIError as exc:
            logger.error("Anthropic API error for %s: %s", resource_id, exc)
            return self._fallback_diagnosis(trigger, resource_id, namespace, str(exc))

    def _parse_response(
        self,
        raw_text:    str,
        resource_id: str,
        namespace:   str,
        tokens:      int,
    ) -> Diagnosis:
        try:
            clean = (
                raw_text.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            data = json.loads(clean)

            actions = [
                RemediationAction(
                    action      = a["action"],
                    description = a["description"],
                    confidence  = float(a.get("confidence", 0.5)),
                    is_safe     = bool(a.get("is_safe", False)),
                    parameters  = a.get("parameters", {}),
                )
                for a in data.get("recommended_actions", [])
            ]

            return Diagnosis(
                trigger_category    = TriggerCategory(data.get("trigger_category", "Unknown")),
                root_cause          = data.get("root_cause", "Unknown"),
                summary             = data.get("summary", ""),
                severity            = Severity(data.get("severity", "medium")),
                confidence          = float(data.get("confidence", 0.5)),
                recommended_actions = actions,
                runbook             = data.get("runbook", ""),
                affected_resource   = resource_id,
                namespace           = namespace,
                raw_response        = raw_text,
                tokens_used         = tokens,
            )

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error("Failed to parse Claude response: %s\nRaw: %s", exc, raw_text[:500])
            return Diagnosis(
                trigger_category  = TriggerCategory.UNKNOWN,
                root_cause        = "AI returned unparseable response — manual investigation required",
                summary           = "Could not auto-diagnose. Please investigate manually.",
                severity          = Severity.MEDIUM,
                confidence        = 0.0,
                affected_resource = resource_id,
                namespace         = namespace,
                raw_response      = raw_text,
                tokens_used       = tokens,
            )

    @staticmethod
    def _fallback_diagnosis(
        trigger: str, resource_id: str, namespace: str, error: str
    ) -> Diagnosis:
        """Return a safe default diagnosis when the API call fails."""
        return Diagnosis(
            trigger_category  = TriggerCategory.UNKNOWN,
            root_cause        = f"AI diagnosis unavailable ({error})",
            summary           = f"Trigger={trigger} on {resource_id}. Manual investigation required.",
            severity          = Severity.HIGH,
            confidence        = 0.0,
            affected_resource = resource_id,
            namespace         = namespace,
        )
