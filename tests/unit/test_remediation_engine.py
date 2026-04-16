"""
tests/unit/test_remediation_engine.py
───────────────────────────────────────
Tests for RemediationEngine orchestration logic.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from operator.config import OperatorConfig, OperatorMode, RateLimitConfig
from operator.engines.approval_engine import ApprovalDecision
from operator.engines.diagnosis_engine import (
    Diagnosis, RemediationAction, Severity, TriggerCategory,
)
from operator.engines.remediation_engine import RemediationEngine, RemediationOutcome
from operator.remediations.registry import ActionResult


def _make_diagnosis(action_key="restart_pod", is_safe=True, severity="high"):
    action = RemediationAction(
        action=action_key, description="test action",
        confidence=0.9, is_safe=is_safe,
    )
    return Diagnosis(
        trigger_category=TriggerCategory.CRASH_LOOP_BACK_OFF,
        root_cause="test", summary="test",
        severity=Severity(severity), confidence=0.9,
        recommended_actions=[action],
    )


@pytest.fixture
def deps():
    config              = OperatorConfig()
    config.mode         = OperatorMode.AUTO
    config.rate_limit   = RateLimitConfig(
        global_max_per_hour=100, per_resource_max_per_hour=10, cooldown_seconds=0
    )
    k8s        = MagicMock()
    approval   = AsyncMock()
    audit      = AsyncMock()
    metrics    = MagicMock()
    metrics.remediations_total = MagicMock()
    metrics.remediations_total.labels.return_value = MagicMock()
    metrics.circuit_open_total = MagicMock()
    metrics.rate_limited_total = MagicMock()
    return config, k8s, approval, audit, metrics


def _make_engine(deps):
    config, k8s, approval, audit, metrics = deps
    return RemediationEngine(config, k8s, approval, audit, metrics)


class TestRemediationEngine:

    @pytest.mark.asyncio
    async def test_dry_run_mode_skips_action(self, deps):
        config, k8s, approval, audit, metrics = deps
        config.mode = OperatorMode.DRY_RUN
        eng = _make_engine(deps)
        diag = _make_diagnosis()

        result = await eng.handle(diag, "default", "pod-x", "CrashLoopBackOff")

        assert result.outcome == RemediationOutcome.DRY_RUN
        audit.log.assert_called_once()

    @pytest.mark.asyncio
    async def test_suggest_mode_skips_action(self, deps):
        config, *_ = deps
        config.mode = OperatorMode.SUGGEST
        eng = _make_engine(deps)
        result = await eng.handle(_make_diagnosis(), "default", "pod-x", "CrashLoopBackOff")
        assert result.outcome == RemediationOutcome.SKIPPED_POLICY

    @pytest.mark.asyncio
    async def test_auto_mode_executes_safe_action(self, deps):
        config, k8s, approval, audit, metrics = deps
        config.mode = OperatorMode.AUTO
        eng = _make_engine(deps)

        # Patch the registry action
        mock_action_result = ActionResult(success=True, action="restart_pod", message="Restarted")
        with patch("operator.engines.remediation_engine.registry") as mock_reg:
            mock_fn = AsyncMock(return_value=mock_action_result)
            mock_reg.get.return_value = mock_fn
            mock_reg.has.return_value = True

            result = await eng.handle(_make_diagnosis("restart_pod", is_safe=True), "default", "pod-x", "CrashLoopBackOff")

        assert result.outcome == RemediationOutcome.EXECUTED
        audit.log.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsafe_action_requires_approval(self, deps):
        config, k8s, approval, audit, metrics = deps
        config.mode = OperatorMode.AUTO
        approval.request_approval = AsyncMock(return_value=ApprovalDecision.APPROVED)
        eng = _make_engine(deps)

        mock_action_result = ActionResult(success=True, action="rollback_deployment", message="Rolled back")
        with patch("operator.engines.remediation_engine.registry") as mock_reg:
            mock_reg.get.return_value = AsyncMock(return_value=mock_action_result)
            result = await eng.handle(
                _make_diagnosis("rollback_deployment", is_safe=False),
                "default", "my-deploy", "DeploymentStalled",
            )

        approval.request_approval.assert_called_once()
        assert result.outcome == RemediationOutcome.EXECUTED

    @pytest.mark.asyncio
    async def test_rejected_approval_returns_rejected(self, deps):
        config, k8s, approval, audit, metrics = deps
        config.mode = OperatorMode.AUTO
        approval.request_approval = AsyncMock(return_value=ApprovalDecision.REJECTED)
        eng = _make_engine(deps)

        result = await eng.handle(
            _make_diagnosis("rollback_deployment", is_safe=False),
            "default", "my-deploy", "DeploymentStalled",
        )
        assert result.outcome == RemediationOutcome.REJECTED

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_when_open(self, deps):
        config, *_ = deps
        eng = _make_engine(deps)

        # Force open the circuit breaker
        for _ in range(config.rate_limit.circuit_breaker_threshold):
            eng._circuit_breaker.record_failure()

        result = await eng.handle(_make_diagnosis(), "default", "pod-x", "CrashLoopBackOff")
        assert result.outcome == RemediationOutcome.SKIPPED_CB

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_excess(self, deps):
        config, *_ = deps
        config.rate_limit = RateLimitConfig(
            global_max_per_hour=1,
            per_resource_max_per_hour=1,
            cooldown_seconds=0,
        )
        eng = _make_engine(deps)

        mock_result = ActionResult(success=True, action="restart_pod", message="ok")
        with patch("operator.engines.remediation_engine.registry") as mock_reg:
            mock_reg.get.return_value = AsyncMock(return_value=mock_result)
            # First: allowed
            await eng.handle(_make_diagnosis(), "default", "pod-x", "CrashLoopBackOff")
            # Second: rate limited
            result = await eng.handle(_make_diagnosis(), "default", "pod-x", "CrashLoopBackOff")

        assert result.outcome == RemediationOutcome.SKIPPED_RATE

    @pytest.mark.asyncio
    async def test_unknown_action_key_returns_failed(self, deps):
        eng = _make_engine(deps)
        diag = _make_diagnosis("non_existent_action", is_safe=True)

        with patch("operator.engines.remediation_engine.registry") as mock_reg:
            mock_reg.get.return_value = None
            result = await eng.handle(diag, "default", "pod-x", "CrashLoopBackOff")

        assert result.outcome == RemediationOutcome.FAILED
