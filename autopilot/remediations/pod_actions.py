"""
autopilot/remediations/pod_actions.py
───────────────────────────────────────
All pod-related remediation actions registered in the global registry.
"""

from __future__ import annotations

import asyncio
import logging

from autopilot.remediations.registry import ActionResult, registry
from autopilot.utils.k8s_client import K8sClient

logger = logging.getLogger(__name__)


@registry.register(
    "restart_pod",
    description="Delete the failing pod so its controller creates a fresh one",
    safe_auto=True,
)
async def restart_pod(
    k8s: K8sClient,
    namespace: str,
    name: str,
    dry_run: bool = False,
    **kwargs,
) -> ActionResult:
    """
    Delete the pod with a 0-second grace period.
    If the pod is owned by a Deployment/StatefulSet/DaemonSet, the controller
    will immediately schedule a replacement.
    """
    pod = await k8s.get_pod(name, namespace)
    if not pod:
        return ActionResult(
            success=False,
            action="restart_pod",
            dry_run=dry_run,
            message=f"Pod {namespace}/{name} not found — may have already been replaced",
        )

    owner_refs = pod.metadata.owner_references or []
    if not owner_refs:
        return ActionResult(
            success=False,
            action="restart_pod",
            dry_run=dry_run,
            message=(
                f"Pod {namespace}/{name} has no owner — "
                "deleting an unmanaged pod would lose it permanently"
            ),
        )

    if dry_run:
        return ActionResult(
            success=True,
            action="restart_pod",
            dry_run=True,
            message=f"[DRY-RUN] Would delete pod {namespace}/{name} to trigger controller restart",
        )

    ok = await k8s.delete_pod(name, namespace, grace_period=0)
    if not ok:
        return ActionResult(
            success=False,
            action="restart_pod",
            message=f"Failed to delete pod {namespace}/{name}",
        )

    await asyncio.sleep(10)
    new_pod = await k8s.get_pod(name, namespace)
    status = "replacement appeared" if new_pod else "replacement not yet visible"

    return ActionResult(
        success=True,
        action="restart_pod",
        message=f"Deleted pod {namespace}/{name} — {status}",
        output={"replacement_found": bool(new_pod)},
    )


@registry.register(
    "delete_pod",
    description="Hard delete the pod (graceful) — use for stuck Terminating pods",
    safe_auto=True,
)
async def delete_pod(
    k8s: K8sClient,
    namespace: str,
    name: str,
    grace_period: int = 30,
    dry_run: bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True,
            action="delete_pod",
            dry_run=True,
            message=f"[DRY-RUN] Would delete pod {namespace}/{name} with grace={grace_period}s",
        )

    ok = await k8s.delete_pod(name, namespace, grace_period=grace_period)
    return ActionResult(
        success=ok,
        action="delete_pod",
        message=(
            f"Deleted pod {namespace}/{name} (grace_period={grace_period}s)"
            if ok
            else f"Failed to delete pod {namespace}/{name}"
        ),
    )


@registry.register(
    "force_delete_pod",
    description="Force-delete a stuck Terminating pod (grace_period=0)",
    safe_auto=False,
)
async def force_delete_pod(
    k8s: K8sClient,
    namespace: str,
    name: str,
    dry_run: bool = False,
    **kwargs,
) -> ActionResult:
    if dry_run:
        return ActionResult(
            success=True,
            action="force_delete_pod",
            dry_run=True,
            message=f"[DRY-RUN] Would force-delete pod {namespace}/{name}",
        )

    ok = await k8s.delete_pod(name, namespace, grace_period=0)
    return ActionResult(
        success=ok,
        action="force_delete_pod",
        message=(
            f"Force-deleted pod {namespace}/{name}"
            if ok
            else f"Failed to force-delete pod {namespace}/{name}"
        ),
    )


@registry.register(
    "no_action",
    description="Take no automated action — notify only",
    safe_auto=True,
)
async def no_action(
    k8s: K8sClient,
    namespace: str,
    name: str,
    dry_run: bool = False,
    **kwargs,
) -> ActionResult:
    return ActionResult(
        success=True,
        action="no_action",
        message=f"No automated action taken for {namespace}/{name} — notification sent only",
    )
