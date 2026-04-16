"""
autopilot/utils/k8s_client.py
─────────────────────────────
Thin async wrapper around the official kubernetes-asyncio client.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)


class K8sClient:
    """Async Kubernetes client wrapper. Initialise once and share globally."""

    def __init__(self) -> None:
        self._core: client.CoreV1Api | None = None
        self._apps: client.AppsV1Api | None = None
        self._batch: client.BatchV1Api | None = None

    async def initialise(self) -> None:
        try:
            config.load_incluster_config()
            logger.info("Using in-cluster Kubernetes config")
        except Exception:
            await config.load_kube_config()
            logger.info("Using kubeconfig (local dev mode)")

        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._batch = client.BatchV1Api()

    # ── Pod operations ────────────────────────────────────────────────────────

    async def get_pod(self, name: str, namespace: str) -> client.V1Pod | None:
        try:
            return await self._core.read_namespaced_pod(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def list_pods(self, namespace: str, label_selector: str = "") -> list[client.V1Pod]:
        if namespace:
            resp = await self._core.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )
        else:
            resp = await self._core.list_pod_for_all_namespaces(label_selector=label_selector)
        return resp.items

    async def delete_pod(self, name: str, namespace: str, grace_period: int = 30) -> bool:
        try:
            await self._core.delete_namespaced_pod(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=grace_period),
            )
            logger.info("Deleted pod %s/%s", namespace, name)
            return True
        except ApiException as e:
            logger.error("Failed to delete pod %s/%s: %s", namespace, name, e)
            return False

    async def get_pod_logs(
        self,
        name: str,
        namespace: str,
        container: str | None = None,
        tail_lines: int = 200,
        previous: bool = False,
    ) -> str:
        try:
            logs = await self._core.read_namespaced_pod_log(
                name=name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
                previous=previous,
                timestamps=True,
            )
            return logs or ""
        except ApiException as e:
            logger.warning("Could not retrieve logs for %s/%s: %s", namespace, name, e)
            return f"(log retrieval failed: {e.reason})"

    async def get_pod_events(self, name: str, namespace: str) -> list[dict]:
        field_selector = f"involvedObject.name={name},involvedObject.namespace={namespace}"
        resp = await self._core.list_namespaced_event(
            namespace=namespace, field_selector=field_selector
        )
        return [
            {
                "reason": e.reason,
                "message": e.message,
                "count": e.count,
                "type": e.type,
                "last_seen": str(e.last_timestamp or e.event_time or ""),
            }
            for e in sorted(
                resp.items,
                key=lambda x: x.last_timestamp or x.event_time or datetime.min.replace(tzinfo=UTC),
            )
        ]

    # ── Node operations ───────────────────────────────────────────────────────

    async def get_node(self, name: str) -> client.V1Node | None:
        try:
            return await self._core.read_node(name=name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def list_nodes(self) -> list[client.V1Node]:
        resp = await self._core.list_node()
        return resp.items

    async def cordon_node(self, name: str) -> bool:
        try:
            node = await self._core.read_node(name=name)
            node.spec.unschedulable = True
            await self._core.patch_node(name=name, body=node)
            logger.info("Cordoned node %s", name)
            return True
        except ApiException as e:
            logger.error("Failed to cordon node %s: %s", name, e)
            return False

    async def uncordon_node(self, name: str) -> bool:
        try:
            node = await self._core.read_node(name=name)
            node.spec.unschedulable = False
            await self._core.patch_node(name=name, body=node)
            logger.info("Uncordoned node %s", name)
            return True
        except ApiException as e:
            logger.error("Failed to uncordon node %s: %s", name, e)
            return False

    async def drain_node(self, name: str, grace_period: int = 60) -> bool:
        try:
            pods = await self.list_pods(namespace="")
            for pod in pods:
                if pod.spec.node_name != name:
                    continue
                if any(ref.kind == "DaemonSet" for ref in (pod.metadata.owner_references or [])):
                    continue
                await self.delete_pod(pod.metadata.name, pod.metadata.namespace, grace_period)
            logger.info("Drained node %s", name)
            return True
        except Exception as e:
            logger.error("Failed to drain node %s: %s", name, e)
            return False

    # ── Deployment operations ─────────────────────────────────────────────────

    async def get_deployment(self, name: str, namespace: str) -> client.V1Deployment | None:
        try:
            return await self._apps.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def scale_deployment(self, name: str, namespace: str, replicas: int) -> bool:
        try:
            body = {"spec": {"replicas": replicas}}
            await self._apps.patch_namespaced_deployment_scale(
                name=name, namespace=namespace, body=body
            )
            logger.info("Scaled deployment %s/%s to %d replicas", namespace, name, replicas)
            return True
        except ApiException as e:
            logger.error("Failed to scale deployment %s/%s: %s", namespace, name, e)
            return False

    async def rollback_deployment(self, name: str, namespace: str) -> bool:
        try:
            deploy = await self.get_deployment(name, namespace)
            if not deploy:
                return False

            rs_list = await self._apps.list_namespaced_replica_set(namespace=namespace)
            owned_rs = [
                rs
                for rs in rs_list.items
                if any(ref.name == name for ref in (rs.metadata.owner_references or []))
            ]
            owned_rs.sort(
                key=lambda rs: int(
                    rs.metadata.annotations.get("deployment.kubernetes.io/revision", "0")
                ),
                reverse=True,
            )

            if len(owned_rs) < 2:
                logger.warning("No previous revision found for %s/%s", namespace, name)
                return False

            prev_revision = owned_rs[1].metadata.annotations.get(
                "deployment.kubernetes.io/revision", "0"
            )
            patch = {
                "spec": {"template": owned_rs[1].spec.template.to_dict()},
            }
            await self._apps.patch_namespaced_deployment(name=name, namespace=namespace, body=patch)
            logger.info(
                "Rolled back deployment %s/%s to revision %s", namespace, name, prev_revision
            )
            return True
        except Exception as e:
            logger.error("Failed to rollback deployment %s/%s: %s", namespace, name, e)
            return False

    async def restart_deployment(self, name: str, namespace: str) -> bool:
        try:
            now = datetime.now(UTC).isoformat()
            patch = {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                    }
                }
            }
            await self._apps.patch_namespaced_deployment(name=name, namespace=namespace, body=patch)
            logger.info("Restarted deployment %s/%s", namespace, name)
            return True
        except ApiException as e:
            logger.error("Failed to restart deployment %s/%s: %s", namespace, name, e)
            return False

    # ── General helpers ────────────────────────────────────────────────────────

    async def get_namespace_resource_quota(self, namespace: str) -> dict:
        try:
            resp = await self._core.list_namespaced_resource_quota(namespace=namespace)
            quotas = {}
            for rq in resp.items:
                quotas[rq.metadata.name] = {
                    "hard": rq.spec.hard or {},
                    "used": rq.status.used or {},
                }
            return quotas
        except Exception:
            return {}

    async def get_node_for_pod(self, pod: client.V1Pod) -> client.V1Node | None:
        if not pod.spec.node_name:
            return None
        return await self.get_node(pod.spec.node_name)

    async def wait_for_pod_ready(self, name: str, namespace: str, timeout: int = 120) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            pod = await self.get_pod(name, namespace)
            if pod and pod.status.phase == "Running":
                conditions = pod.status.conditions or []
                if any(c.type == "Ready" and c.status == "True" for c in conditions):
                    return True
            await asyncio.sleep(5)
        return False


# Module-level singleton
_client: K8sClient | None = None


def get_k8s_client() -> K8sClient:
    if _client is None:
        raise RuntimeError("K8sClient not initialised — call init_k8s_client() first")
    return _client


async def init_k8s_client() -> K8sClient:
    global _client
    _client = K8sClient()
    await _client.initialise()
    return _client
