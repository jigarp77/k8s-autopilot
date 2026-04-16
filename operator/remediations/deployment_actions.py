"""
operator/remediations/deployment_actions.py
─────────────────────────────────────────────
Deployment and node remediation actions.
"""

from __future__ import annotations

import logging

from operator.remediations.registry import ActionResult, registry
from operator.utils.k8s_client import K8sClient

logger = logging.getLogger(__name__)


# ── Deployment actions ────────────────────────────────────────────────────────

@registry.register(
    "rollback_deployment",
    description="Roll back deployment to the previous ReplicaSet revision",
    safe_auto=False,   # always require approval — rollbacks can break things
)
async def rollback_deployment(
    k8s:       K8sClient,
    namespace: str,
    name:      str,
    dry_run:   bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True, action="rollback_deployment", dry_run=True,
            message=f"[DRY-RUN] Would roll back deployment {namespace}/{name} to previous revision",
        )

    ok = await k8s.rollback_deployment(name, namespace)
    return ActionResult(
        success=ok, action="rollback_deployment",
        message=(
            f"Rolled back deployment {namespace}/{name} to previous revision"
            if ok else
            f"Failed to roll back deployment {namespace}/{name}"
        ),
    )


@registry.register(
    "restart_deployment",
    description="Trigger rolling restart of all pods in the deployment",
    safe_auto=False,
)
async def restart_deployment(
    k8s:       K8sClient,
    namespace: str,
    name:      str,
    dry_run:   bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True, action="restart_deployment", dry_run=True,
            message=f"[DRY-RUN] Would rolling-restart deployment {namespace}/{name}",
        )

    ok = await k8s.restart_deployment(name, namespace)
    return ActionResult(
        success=ok, action="restart_deployment",
        message=(
            f"Rolling restart triggered for deployment {namespace}/{name}"
            if ok else
            f"Failed to restart deployment {namespace}/{name}"
        ),
    )


@registry.register(
    "scale_deployment",
    description="Scale deployment to specified replica count",
    safe_auto=False,
)
async def scale_deployment(
    k8s:       K8sClient,
    namespace: str,
    name:      str,
    replicas:  int  = 1,
    dry_run:   bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True, action="scale_deployment", dry_run=True,
            message=f"[DRY-RUN] Would scale deployment {namespace}/{name} to {replicas} replicas",
        )

    ok = await k8s.scale_deployment(name, namespace, replicas)
    return ActionResult(
        success=ok, action="scale_deployment",
        message=(
            f"Scaled deployment {namespace}/{name} to {replicas} replicas"
            if ok else
            f"Failed to scale deployment {namespace}/{name}"
        ),
        output={"target_replicas": replicas},
    )


# ── Node actions ──────────────────────────────────────────────────────────────

@registry.register(
    "cordon_node",
    description="Mark node unschedulable so no new pods land on it",
    safe_auto=False,
)
async def cordon_node(
    k8s:       K8sClient,
    namespace: str,   # unused for nodes, kept for uniform signature
    name:      str,
    dry_run:   bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True, action="cordon_node", dry_run=True,
            message=f"[DRY-RUN] Would cordon node {name}",
        )

    ok = await k8s.cordon_node(name)
    return ActionResult(
        success=ok, action="cordon_node",
        message=f"Cordoned node {name}" if ok else f"Failed to cordon node {name}",
    )


@registry.register(
    "drain_node",
    description="Evict all non-DaemonSet pods from node (use after cordon)",
    safe_auto=False,   # draining is disruptive
)
async def drain_node(
    k8s:          K8sClient,
    namespace:    str,
    name:         str,
    grace_period: int  = 60,
    dry_run:      bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True, action="drain_node", dry_run=True,
            message=f"[DRY-RUN] Would drain node {name} (grace_period={grace_period}s)",
        )

    ok = await k8s.drain_node(name, grace_period=grace_period)
    return ActionResult(
        success=ok, action="drain_node",
        message=f"Drained node {name}" if ok else f"Failed to drain node {name}",
    )


@registry.register(
    "uncordon_node",
    description="Re-enable scheduling on a previously cordoned node",
    safe_auto=False,
)
async def uncordon_node(
    k8s:       K8sClient,
    namespace: str,
    name:      str,
    dry_run:   bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True, action="uncordon_node", dry_run=True,
            message=f"[DRY-RUN] Would uncordon node {name}",
        )

    ok = await k8s.uncordon_node(name)
    return ActionResult(
        success=ok, action="uncordon_node",
        message=f"Uncordoned node {name}" if ok else f"Failed to uncordon node {name}",
    )
