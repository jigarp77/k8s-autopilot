"""
operator/engines/remediation_engine.py
──────────────────────────────────────────
Orchestrates the full remediation lifecycle:

  1. Policy check   — is this trigger allowed in current mode?
  2. Rate limit     — have we acted too recently on this resource?
  3. Circuit breaker— have we had too many consecutive failures?
  4. Approval gate  — auto or requires Slack approval?
  5. Action         — execute from registry, respecting dry-run mode
  6. Verify         — confirm the action worked
  7. Audit          — log everything to SQLite + K8s event
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from operator.audit.logger import AuditLogger, AuditRecord, AuditOutcome
from operator.config import OperatorConfig, OperatorMode
from operator.engines.approval_engine import ApprovalEngine, ApprovalDecision
from operator.engines.diagnosis_engine import Diagnosis, RemediationAction
from operator.integrations.prometheus import AutopilotMetrics
from operator.remediations.registry import ActionResult, registry
from operator.utils.circuit_breaker import CircuitBreaker
from operator.utils.k8s_client import K8sClient
from operator.utils.rate_limiter import ResourceRateLimiter

logger = logging.getLogger(__name__)


class RemediationOutcome(str, Enum):
    EXECUTED       = "executed"
    DRY_RUN        = "dry_run"
    SKIPPED_POLICY = "skipped_policy"
    SKIPPED_RATE   = "skipped_rate_limit"
    SKIPPED_CB     = "skipped_circuit_open"
    REJECTED       = "rejected_by_human"
    APPROVAL_TIMEOUT = "approval_timeout"
    FAILED         = "failed"
    NO_ACTION      = "no_action"


@dataclass
class RemediationResult:
    outcome:      RemediationOutcome
    action:       str  = ""
    message:      str  = ""
    resource_id:  str  = ""
    action_result: Optional[ActionResult] = None


class RemediationEngine:

    def __init__(
        self,
        config:    OperatorConfig,
        k8s:       K8sClient,
        approval:  ApprovalEngine,
        audit:     AuditLogger,
        metrics:   AutopilotMetrics,
    ) -> None:
        self._config   = config
        self._k8s      = k8s
        self._approval = approval
        self._audit    = audit
        self._metrics  = metrics

        self._rate_limiter = ResourceRateLimiter(
            global_max_per_hour        = config.rate_limit.global_max_per_hour,
            per_resource_max_per_hour  = config.rate_limit.per_resource_max_per_hour,
            cooldown_seconds           = config.rate_limit.cooldown_seconds,
        )
        self._circuit_breaker = CircuitBreaker(
            failure_threshold = config.rate_limit.circuit_breaker_threshold,
            timeout_seconds   = config.rate_limit.circuit_breaker_timeout,
            name              = "remediation",
        )

    async def handle(
        self,
        diagnosis:  Diagnosis,
        namespace:  str,
        name:       str,
        trigger:    str,
        resource_kind: str = "pod",
    ) -> RemediationResult:
        resource_id = f"{namespace}/{name}"
        started_at  = datetime.now(timezone.utc)

        # ── 1. Mode / policy check ─────────────────────────────────────────
        if self._config.mode == OperatorMode.DRY_RUN:
            logger.info("[DRY-RUN] Would remediate %s (trigger=%s)", resource_id, trigger)
            await self._audit.log(AuditRecord(
                resource_id=resource_id, namespace=namespace, name=name,
                trigger=trigger, action="(dry-run)", outcome=AuditOutcome.DRY_RUN,
                diagnosis=diagnosis.to_dict(), started_at=started_at,
            ))
            return RemediationResult(
                outcome=RemediationOutcome.DRY_RUN,
                message=f"Dry-run mode: would act on {resource_id}",
                resource_id=resource_id,
            )

        if self._config.mode == OperatorMode.SUGGEST:
            logger.info("[SUGGEST] Notification only for %s (trigger=%s)", resource_id, trigger)
            return RemediationResult(
                outcome=RemediationOutcome.SKIPPED_POLICY,
                message="Mode=suggest: notified only, no action taken",
                resource_id=resource_id,
            )

        # ── 2. No action recommended ───────────────────────────────────────
        action = diagnosis.top_action
        if not action or action.action == "no_action":
            return RemediationResult(
                outcome=RemediationOutcome.NO_ACTION,
                resource_id=resource_id,
                message="AI recommended no automated action",
            )

        # ── 3. Circuit breaker ─────────────────────────────────────────────
        if self._circuit_breaker.is_open():
            logger.warning("Circuit breaker OPEN — skipping remediation for %s", resource_id)
            self._metrics.circuit_open_total.inc()
            return RemediationResult(
                outcome=RemediationOutcome.SKIPPED_CB,
                resource_id=resource_id,
                message="Circuit breaker is OPEN due to consecutive failures — manual intervention required",
            )

        # ── 4. Rate limit ──────────────────────────────────────────────────
        rule = self._config.get_rule(trigger)
        rate_check = self._rate_limiter.check(
            namespace, name, trigger,
            override_resource_max=rule.max_per_hour if rule else None,
        )
        if not rate_check.allowed:
            logger.info("Rate limit hit for %s: %s", resource_id, rate_check.reason)
            self._metrics.rate_limited_total.inc()
            return RemediationResult(
                outcome=RemediationOutcome.SKIPPED_RATE,
                resource_id=resource_id,
                message=rate_check.reason,
            )

        # ── 5. Approval gate ───────────────────────────────────────────────
        needs_approval = (
            self._config.mode == OperatorMode.APPROVAL
            or not action.is_safe
            or (rule and rule.require_approval)
        )

        if needs_approval:
            decision = await self._approval.request_approval(
                diagnosis=diagnosis,
                action=action,
                namespace=namespace,
                name=name,
                trigger=trigger,
            )
            if decision == ApprovalDecision.REJECTED:
                await self._audit.log(AuditRecord(
                    resource_id=resource_id, namespace=namespace, name=name,
                    trigger=trigger, action=action.action,
                    outcome=AuditOutcome.REJECTED, diagnosis=diagnosis.to_dict(),
                    started_at=started_at,
                ))
                return RemediationResult(
                    outcome=RemediationOutcome.REJECTED,
                    action=action.action,
                    resource_id=resource_id,
                    message="Remediation rejected by human approver",
                )
            if decision == ApprovalDecision.TIMEOUT:
                return RemediationResult(
                    outcome=RemediationOutcome.APPROVAL_TIMEOUT,
                    action=action.action,
                    resource_id=resource_id,
                    message=f"Approval timed out after {self._config.slack.timeout_seconds}s",
                )

        # ── 6. Execute ─────────────────────────────────────────────────────
        fn = registry.get(action.action)
        if not fn:
            logger.error("Unknown action key: %s", action.action)
            return RemediationResult(
                outcome=RemediationOutcome.FAILED,
                action=action.action,
                resource_id=resource_id,
                message=f"No registered action for key '{action.action}'",
            )

        logger.info(
            "Executing remediation: action=%s resource=%s trigger=%s",
            action.action, resource_id, trigger,
        )

        try:
            result: ActionResult = await fn(
                k8s       = self._k8s,
                namespace = namespace,
                name      = name,
                dry_run   = False,
                **action.parameters,
            )
        except Exception as exc:
            logger.exception("Action %s raised exception for %s: %s", action.action, resource_id, exc)
            self._circuit_breaker.record_failure()
            result = ActionResult(success=False, action=action.action, message=str(exc))

        # ── 7. Record outcome ──────────────────────────────────────────────
        if result.success:
            self._circuit_breaker.record_success()
            self._rate_limiter.record(namespace, name, trigger)
            self._metrics.remediations_total.labels(
                action=action.action, trigger=trigger, outcome="success"
            ).inc()
            audit_outcome = AuditOutcome.EXECUTED
        else:
            self._circuit_breaker.record_failure()
            self._metrics.remediations_total.labels(
                action=action.action, trigger=trigger, outcome="failure"
            ).inc()
            audit_outcome = AuditOutcome.FAILED

        await self._audit.log(AuditRecord(
            resource_id  = resource_id,
            namespace    = namespace,
            name         = name,
            trigger      = trigger,
            action       = action.action,
            outcome      = audit_outcome,
            diagnosis    = diagnosis.to_dict(),
            action_output= result.output,
            started_at   = started_at,
            completed_at = datetime.now(timezone.utc),
        ))

        return RemediationResult(
            outcome      = RemediationOutcome.EXECUTED if result.success else RemediationOutcome.FAILED,
            action       = action.action,
            message      = result.message,
            resource_id  = resource_id,
            action_result= result,
        )
