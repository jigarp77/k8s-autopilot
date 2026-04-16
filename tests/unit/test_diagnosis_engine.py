"""
tests/unit/test_diagnosis_engine.py
────────────────────────────────────
Tests for DiagnosisEngine — mocks out the Anthropic client.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from operator.config import AnthropicConfig, OperatorConfig
from operator.engines.context_collector import PodContext
from operator.engines.diagnosis_engine import (
    Diagnosis, DiagnosisEngine, RemediationAction,
    Severity, TriggerCategory,
)


SAMPLE_RESPONSE = {
    "trigger_category": "CrashLoopBackOff",
    "root_cause": "Application exits immediately due to missing DATABASE_URL env var",
    "summary": "Pod is crash-looping because DATABASE_URL is not configured.",
    "severity": "high",
    "confidence": 0.92,
    "recommended_actions": [
        {
            "action": "no_action",
            "description": "Fix the missing env var and redeploy; restart won't help.",
            "confidence": 0.90,
            "is_safe": True,
            "parameters": {},
        }
    ],
    "runbook": "## CrashLoopBackOff — missing env var\n1. Check env vars\n2. Patch deployment",
}


@pytest.fixture
def config():
    cfg = OperatorConfig()
    cfg.anthropic = AnthropicConfig(api_key="sk-test", model="claude-opus-4-5")
    return cfg


@pytest.fixture
def engine(config):
    with patch("operator.engines.diagnosis_engine.anthropic.Anthropic") as MockClient:
        instance = MockClient.return_value
        msg = MagicMock()
        msg.content = [MagicMock(text=json.dumps(SAMPLE_RESPONSE))]
        msg.usage.input_tokens  = 1000
        msg.usage.output_tokens = 200
        instance.messages.create.return_value = msg
        eng = DiagnosisEngine(config)
        yield eng, instance


class TestDiagnosisEngine:

    @pytest.mark.asyncio
    async def test_diagnose_pod_returns_diagnosis(self, engine):
        eng, mock_client = engine
        ctx = PodContext(namespace="default", pod_name="my-pod")

        diag = await eng.diagnose_pod(ctx, "CrashLoopBackOff")

        assert isinstance(diag, Diagnosis)
        assert diag.trigger_category == TriggerCategory.CRASH_LOOP_BACK_OFF
        assert diag.severity         == Severity.HIGH
        assert diag.confidence       == pytest.approx(0.92)
        assert len(diag.recommended_actions) == 1

    @pytest.mark.asyncio
    async def test_top_action_is_first(self, engine):
        eng, _ = engine
        ctx = PodContext(namespace="default", pod_name="my-pod")
        diag = await eng.diagnose_pod(ctx, "CrashLoopBackOff")
        assert diag.top_action is not None
        assert diag.top_action.action == "no_action"

    @pytest.mark.asyncio
    async def test_tokens_recorded(self, engine):
        eng, _ = engine
        ctx = PodContext(namespace="default", pod_name="my-pod")
        diag = await eng.diagnose_pod(ctx, "CrashLoopBackOff")
        assert diag.tokens_used == 1200

    @pytest.mark.asyncio
    async def test_api_error_returns_fallback(self, config):
        import anthropic as _anthropic
        with patch("operator.engines.diagnosis_engine.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create.side_effect = _anthropic.APIError(
                message="rate limited", request=MagicMock(), body=None
            )
            eng = DiagnosisEngine(config)
            ctx = PodContext(namespace="default", pod_name="broken-pod")
            diag = await eng.diagnose_pod(ctx, "OOMKilled")

        assert diag.confidence   == 0.0
        assert diag.trigger_category == TriggerCategory.UNKNOWN

    @pytest.mark.asyncio
    async def test_malformed_json_returns_fallback(self, config):
        with patch("operator.engines.diagnosis_engine.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            msg = MagicMock()
            msg.content = [MagicMock(text="this is not json {{{")]
            msg.usage.input_tokens  = 100
            msg.usage.output_tokens = 10
            instance.messages.create.return_value = msg
            eng = DiagnosisEngine(config)
            ctx = PodContext(namespace="default", pod_name="bad-pod")
            diag = await eng.diagnose_pod(ctx, "CrashLoopBackOff")

        assert diag.confidence == 0.0
        assert "unparseable" in diag.root_cause.lower()

    @pytest.mark.asyncio
    async def test_context_text_in_prompt(self, engine):
        eng, mock_client = engine
        ctx = PodContext(namespace="prod", pod_name="api-server")
        ctx.restart_count = 10
        ctx.current_logs["app"] = "ERROR: connect ECONNREFUSED 127.0.0.1:5432"

        await eng.diagnose_pod(ctx, "CrashLoopBackOff")

        call_args = mock_client.messages.create.call_args
        user_content = call_args.kwargs["messages"][0]["content"]
        assert "api-server" in user_content
        assert "ECONNREFUSED" in user_content
