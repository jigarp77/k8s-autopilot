"""
tests/unit/test_approval_engine.py
────────────────────────────────────
Tests for the Slack human-in-the-loop approval workflow.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from autopilot.config import OperatorConfig, SlackConfig
from autopilot.engines.approval_engine import ApprovalDecision, ApprovalEngine
from autopilot.engines.diagnosis_engine import (
    Diagnosis,
    RemediationAction,
    Severity,
    TriggerCategory,
)


def _make_diagnosis():
    action = RemediationAction(
        action="restart_pod", description="test", confidence=0.9, is_safe=False
    )
    return Diagnosis(
        trigger_category=TriggerCategory.CRASH_LOOP_BACK_OFF,
        root_cause="test cause",
        summary="summary",
        severity=Severity.HIGH,
        confidence=0.9,
        recommended_actions=[action],
    )


def _make_action():
    return RemediationAction(
        action="restart_pod", description="test", confidence=0.9, is_safe=False
    )


@pytest.fixture
def slack_enabled_config():
    cfg = OperatorConfig()
    cfg.slack = SlackConfig(token="xoxb-test", enabled=True, timeout_seconds=1, channel="#test")
    return cfg


@pytest.fixture
def slack_disabled_config():
    cfg = OperatorConfig()
    cfg.slack = SlackConfig(enabled=False)
    return cfg


class TestApprovalEngine:
    @pytest.mark.asyncio
    async def test_auto_rejects_when_slack_disabled(self, slack_disabled_config):
        slack = MagicMock()
        engine = ApprovalEngine(slack_disabled_config, slack)

        decision = await engine.request_approval(
            diagnosis=_make_diagnosis(),
            action=_make_action(),
            namespace="default",
            name="my-pod",
            trigger="CrashLoopBackOff",
        )
        assert decision == ApprovalDecision.REJECTED

    @pytest.mark.asyncio
    async def test_approval_flow_approved(self, slack_enabled_config):
        slack = MagicMock()
        slack.post_approval_request = AsyncMock(return_value="1234.5678")
        slack.update_approval_message = AsyncMock()

        engine = ApprovalEngine(slack_enabled_config, slack)

        # Kick off the approval, then immediately "approve" via webhook callback
        async def approve_soon():
            # wait for the approval to register in the pending dict
            for _ in range(20):
                if engine._pending:
                    break
                await asyncio.sleep(0.01)
            approval_id = next(iter(engine._pending.keys()))
            await engine.on_approval_response(approval_id, "approved", "jigar")

        results = await asyncio.gather(
            engine.request_approval(
                diagnosis=_make_diagnosis(),
                action=_make_action(),
                namespace="default",
                name="my-pod",
                trigger="CrashLoopBackOff",
            ),
            approve_soon(),
        )
        assert results[0] == ApprovalDecision.APPROVED

    @pytest.mark.asyncio
    async def test_approval_flow_rejected(self, slack_enabled_config):
        slack = MagicMock()
        slack.post_approval_request = AsyncMock(return_value="1234.5678")
        slack.update_approval_message = AsyncMock()

        engine = ApprovalEngine(slack_enabled_config, slack)

        async def reject_soon():
            for _ in range(20):
                if engine._pending:
                    break
                await asyncio.sleep(0.01)
            approval_id = next(iter(engine._pending.keys()))
            await engine.on_approval_response(approval_id, "rejected", "jigar")

        results = await asyncio.gather(
            engine.request_approval(
                diagnosis=_make_diagnosis(),
                action=_make_action(),
                namespace="default",
                name="my-pod",
                trigger="CrashLoopBackOff",
            ),
            reject_soon(),
        )
        assert results[0] == ApprovalDecision.REJECTED

    @pytest.mark.asyncio
    async def test_approval_times_out(self, slack_enabled_config):
        slack = MagicMock()
        slack.post_approval_request = AsyncMock(return_value="1234.5678")
        slack.update_approval_message = AsyncMock()

        engine = ApprovalEngine(slack_enabled_config, slack)

        # No one responds — should timeout (timeout_seconds=1)
        decision = await engine.request_approval(
            diagnosis=_make_diagnosis(),
            action=_make_action(),
            namespace="default",
            name="my-pod",
            trigger="CrashLoopBackOff",
        )
        assert decision == ApprovalDecision.TIMEOUT

    @pytest.mark.asyncio
    async def test_unknown_approval_id_returns_false(self, slack_enabled_config):
        slack = MagicMock()
        engine = ApprovalEngine(slack_enabled_config, slack)
        result = await engine.on_approval_response("nonexistent", "approved")
        assert result is False

    def test_pending_count_starts_zero(self, slack_enabled_config):
        slack = MagicMock()
        engine = ApprovalEngine(slack_enabled_config, slack)
        assert engine.pending_count() == 0
        assert engine.list_pending() == []
