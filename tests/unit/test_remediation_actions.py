"""
tests/unit/test_remediation_actions.py
────────────────────────────────────────
Tests for the registered remediation actions (pod + deployment + node).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from autopilot.remediations.deployment_actions import (
    cordon_node,
    drain_node,
    restart_deployment,
    rollback_deployment,
    scale_deployment,
    uncordon_node,
)
from autopilot.remediations.pod_actions import (
    delete_pod,
    force_delete_pod,
    no_action,
    restart_pod,
)


def _mock_pod_with_owner():
    pod = MagicMock()
    pod.metadata.owner_references = [MagicMock(kind="Deployment", name="my-app")]
    return pod


def _mock_pod_without_owner():
    pod = MagicMock()
    pod.metadata.owner_references = []
    return pod


@pytest.fixture
def k8s():
    return AsyncMock()


class TestPodActions:
    @pytest.mark.asyncio
    async def test_restart_pod_dry_run(self, k8s):
        k8s.get_pod.return_value = _mock_pod_with_owner()
        result = await restart_pod(k8s, "default", "my-pod", dry_run=True)
        assert result.success
        assert result.dry_run
        assert "DRY-RUN" in result.message
        k8s.delete_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_pod_not_found(self, k8s):
        k8s.get_pod.return_value = None
        result = await restart_pod(k8s, "default", "gone", dry_run=False)
        assert result.success is False
        assert "not found" in result.message

    @pytest.mark.asyncio
    async def test_restart_pod_unowned_refused(self, k8s):
        k8s.get_pod.return_value = _mock_pod_without_owner()
        result = await restart_pod(k8s, "default", "raw-pod", dry_run=False)
        assert result.success is False
        assert "no owner" in result.message.lower()

    @pytest.mark.asyncio
    async def test_delete_pod_dry_run(self, k8s):
        result = await delete_pod(k8s, "default", "my-pod", dry_run=True)
        assert result.success
        assert result.dry_run
        k8s.delete_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_pod_real(self, k8s):
        k8s.delete_pod.return_value = True
        result = await delete_pod(k8s, "default", "my-pod", dry_run=False)
        assert result.success
        k8s.delete_pod.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_delete_pod_dry_run(self, k8s):
        result = await force_delete_pod(k8s, "default", "stuck", dry_run=True)
        assert result.success
        assert result.dry_run

    @pytest.mark.asyncio
    async def test_no_action_always_succeeds(self, k8s):
        result = await no_action(k8s, "default", "pod", dry_run=False)
        assert result.success
        assert result.action == "no_action"


class TestDeploymentActions:
    @pytest.mark.asyncio
    async def test_rollback_dry_run(self, k8s):
        result = await rollback_deployment(k8s, "default", "app", dry_run=True)
        assert result.success
        assert result.dry_run
        k8s.rollback_deployment.assert_not_called()

    @pytest.mark.asyncio
    async def test_rollback_real(self, k8s):
        k8s.rollback_deployment.return_value = True
        result = await rollback_deployment(k8s, "default", "app", dry_run=False)
        assert result.success
        k8s.rollback_deployment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rollback_failure_propagates(self, k8s):
        k8s.rollback_deployment.return_value = False
        result = await rollback_deployment(k8s, "default", "app", dry_run=False)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_restart_deployment_dry_run(self, k8s):
        result = await restart_deployment(k8s, "default", "app", dry_run=True)
        assert result.success
        assert result.dry_run

    @pytest.mark.asyncio
    async def test_restart_deployment_real(self, k8s):
        k8s.restart_deployment.return_value = True
        result = await restart_deployment(k8s, "default", "app", dry_run=False)
        assert result.success

    @pytest.mark.asyncio
    async def test_scale_deployment_dry_run(self, k8s):
        result = await scale_deployment(k8s, "default", "app", replicas=3, dry_run=True)
        assert result.success
        assert result.dry_run
        assert "3 replicas" in result.message

    @pytest.mark.asyncio
    async def test_scale_deployment_real(self, k8s):
        k8s.scale_deployment.return_value = True
        result = await scale_deployment(k8s, "default", "app", replicas=5, dry_run=False)
        assert result.success
        assert result.output["target_replicas"] == 5
        k8s.scale_deployment.assert_awaited_once_with("app", "default", 5)


class TestNodeActions:
    @pytest.mark.asyncio
    async def test_cordon_dry_run(self, k8s):
        result = await cordon_node(k8s, "", "node-1", dry_run=True)
        assert result.success
        assert result.dry_run
        k8s.cordon_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_cordon_real(self, k8s):
        k8s.cordon_node.return_value = True
        result = await cordon_node(k8s, "", "node-1", dry_run=False)
        assert result.success

    @pytest.mark.asyncio
    async def test_uncordon_real(self, k8s):
        k8s.uncordon_node.return_value = True
        result = await uncordon_node(k8s, "", "node-1", dry_run=False)
        assert result.success

    @pytest.mark.asyncio
    async def test_drain_dry_run(self, k8s):
        result = await drain_node(k8s, "", "node-1", grace_period=30, dry_run=True)
        assert result.success
        assert result.dry_run
        assert "30" in result.message

    @pytest.mark.asyncio
    async def test_drain_real(self, k8s):
        k8s.drain_node.return_value = True
        result = await drain_node(k8s, "", "node-1", dry_run=False)
        assert result.success
        k8s.drain_node.assert_awaited_once()
