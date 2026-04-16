"""
autopilot/engines/context_collector.py
────────────────────────────────────────
Collects all available context about a failing resource before diagnosis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kubernetes_asyncio import client

from autopilot.utils.k8s_client import K8sClient

logger = logging.getLogger(__name__)


@dataclass
class PodContext:
    """All context gathered for a failing pod."""

    namespace: str
    pod_name: str
    node_name: str = ""
    phase: str = ""
    reason: str = ""
    message: str = ""
    restart_count: int = 0
    containers: list[dict] = field(default_factory=list)
    init_containers: list[dict] = field(default_factory=list)
    current_logs: dict[str, str] = field(default_factory=dict)
    previous_logs: dict[str, str] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    resource_requests: dict = field(default_factory=dict)
    resource_limits: dict = field(default_factory=dict)
    node_conditions: list[dict] = field(default_factory=list)
    node_allocatable: dict = field(default_factory=dict)
    node_capacity: dict = field(default_factory=dict)
    namespace_quotas: dict = field(default_factory=dict)
    owner_kind: str = ""
    owner_name: str = ""
    labels: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)

    def to_text(self) -> str:
        lines = [
            f"=== POD CONTEXT: {self.namespace}/{self.pod_name} ===",
            f"Phase       : {self.phase}",
            f"Reason      : {self.reason}",
            f"Message     : {self.message}",
            f"Node        : {self.node_name}",
            (
                f"Owner       : {self.owner_kind}/{self.owner_name}"
                if self.owner_name
                else "Owner: none"
            ),
            f"Restarts    : {self.restart_count}",
            "",
        ]

        if self.containers:
            lines.append("--- Containers ---")
            for c in self.containers:
                lines.append(
                    f"  {c['name']}: state={c.get('state')} "
                    f"restarts={c.get('restarts', 0)} image={c.get('image', 'unknown')}"
                )
                if c.get("last_state"):
                    lines.append(f"    last_state: {c['last_state']}")
                if c.get("exit_code") is not None:
                    lines.append(
                        f"    exit_code: {c['exit_code']} reason: {c.get('exit_reason', '')}"
                    )
            lines.append("")

        if self.resource_requests or self.resource_limits:
            lines.append("--- Resource config ---")
            lines.append(f"  requests : {self.resource_requests}")
            lines.append(f"  limits   : {self.resource_limits}")
            lines.append("")

        if self.node_allocatable:
            lines.append("--- Node allocatable ---")
            for k, v in self.node_allocatable.items():
                lines.append(f"  {k}: {v}")
            lines.append("")

        if self.node_conditions:
            lines.append("--- Node conditions ---")
            for cond in self.node_conditions:
                lines.append(
                    f"  {cond['type']}: {cond['status']} "
                    f"({cond.get('reason', '')} — {cond.get('message', '')})"
                )
            lines.append("")

        if self.events:
            lines.append("--- K8s Events (most recent first) ---")
            for evt in self.events[-20:]:
                lines.append(
                    f"  [{evt.get('type', '?')}] {evt.get('reason', '?')}: "
                    f"{evt.get('message', '?')}  (count={evt.get('count', 1)})"
                )
            lines.append("")

        for container, logs in self.current_logs.items():
            lines.append(f"--- Current logs: {container} ---")
            lines.append(logs[-8000:] if logs else "(no logs)")
            lines.append("")

        for container, logs in self.previous_logs.items():
            lines.append(f"--- Previous (crashed) logs: {container} ---")
            lines.append(logs[-4000:] if logs else "(no previous logs)")
            lines.append("")

        if self.namespace_quotas:
            lines.append("--- Namespace resource quotas ---")
            for qname, qdata in self.namespace_quotas.items():
                lines.append(
                    f"  {qname}: hard={qdata.get('hard', {})} used={qdata.get('used', {})}"
                )
            lines.append("")

        return "\n".join(lines)


@dataclass
class NodeContext:
    node_name: str
    conditions: list[dict] = field(default_factory=list)
    allocatable: dict = field(default_factory=dict)
    capacity: dict = field(default_factory=dict)
    taints: list[str] = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    pods_on_node: int = 0
    events: list[dict] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [
            f"=== NODE CONTEXT: {self.node_name} ===",
            f"Pods on node : {self.pods_on_node}",
        ]
        if self.conditions:
            lines.append("--- Conditions ---")
            for c in self.conditions:
                lines.append(f"  {c['type']}: {c['status']} — {c.get('message', '')}")
        if self.capacity:
            lines.append("--- Capacity ---")
            for k, v in self.capacity.items():
                lines.append(f"  {k}: {v}")
        if self.taints:
            lines.append("--- Taints ---")
            for t in self.taints:
                lines.append(f"  {t}")
        if self.events:
            lines.append("--- Node events ---")
            for e in self.events[-10:]:
                lines.append(f"  [{e.get('type')}] {e.get('reason')}: {e.get('message')}")
        return "\n".join(lines)


class ContextCollector:
    """
    Gathers rich context from the K8s API for a failing resource.
    """

    def __init__(self, k8s: K8sClient, log_lines: int = 200) -> None:
        self._k8s = k8s
        self._log_lines = log_lines

    async def collect_pod_context(self, namespace: str, pod_name: str) -> PodContext:
        ctx = PodContext(namespace=namespace, pod_name=pod_name)

        pod = await self._k8s.get_pod(pod_name, namespace)
        if not pod:
            logger.warning("Pod %s/%s not found — context will be partial", namespace, pod_name)
            return ctx

        ctx.phase = pod.status.phase or ""
        ctx.node_name = pod.spec.node_name or ""
        ctx.labels = dict(pod.metadata.labels or {})
        ctx.annotations = dict(pod.metadata.annotations or {})

        for ref in pod.metadata.owner_references or []:
            ctx.owner_kind = ref.kind
            ctx.owner_name = ref.name
            break

        for cs in pod.status.container_statuses or []:
            container_spec = next((c for c in pod.spec.containers if c.name == cs.name), None)
            state_str = self._state_string(cs.state)
            last_state_str = self._state_string(cs.last_state) if cs.last_state else ""
            exit_code = None
            exit_reason = ""
            if cs.state and cs.state.terminated:
                exit_code = cs.state.terminated.exit_code
                exit_reason = cs.state.terminated.reason or ""
            elif cs.last_state and cs.last_state.terminated:
                exit_code = cs.last_state.terminated.exit_code
                exit_reason = cs.last_state.terminated.reason or ""

            ctx.containers.append(
                {
                    "name": cs.name,
                    "state": state_str,
                    "last_state": last_state_str,
                    "restarts": cs.restart_count or 0,
                    "image": cs.image,
                    "exit_code": exit_code,
                    "exit_reason": exit_reason,
                }
            )
            ctx.restart_count = max(ctx.restart_count, cs.restart_count or 0)

            if container_spec and container_spec.resources:
                ctx.resource_requests = {
                    k: str(v) for k, v in (container_spec.resources.requests or {}).items()
                }
                ctx.resource_limits = {
                    k: str(v) for k, v in (container_spec.resources.limits or {}).items()
                }

        if pod.status.conditions:
            for cond in pod.status.conditions:
                if cond.type == "PodScheduled" and cond.reason:
                    ctx.reason = cond.reason
                    ctx.message = cond.message or ""

        for c in pod.spec.containers:
            ctx.current_logs[c.name] = await self._k8s.get_pod_logs(
                pod_name, namespace, container=c.name, tail_lines=self._log_lines
            )
            if ctx.restart_count > 0:
                ctx.previous_logs[c.name] = await self._k8s.get_pod_logs(
                    pod_name,
                    namespace,
                    container=c.name,
                    tail_lines=100,
                    previous=True,
                )

        ctx.events = await self._k8s.get_pod_events(pod_name, namespace)

        if ctx.node_name:
            node = await self._k8s.get_node(ctx.node_name)
            if node:
                ctx.node_conditions = [
                    {
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason or "",
                        "message": c.message or "",
                    }
                    for c in (node.status.conditions or [])
                ]
                ctx.node_allocatable = {
                    k: str(v) for k, v in (node.status.allocatable or {}).items()
                }
                ctx.node_capacity = {k: str(v) for k, v in (node.status.capacity or {}).items()}

        ctx.namespace_quotas = await self._k8s.get_namespace_resource_quota(namespace)

        logger.debug(
            "Context collected for %s/%s: containers=%d events=%d",
            namespace,
            pod_name,
            len(ctx.containers),
            len(ctx.events),
        )
        return ctx

    async def collect_node_context(self, node_name: str) -> NodeContext:
        ctx = NodeContext(node_name=node_name)
        node = await self._k8s.get_node(node_name)
        if not node:
            return ctx

        ctx.conditions = [
            {
                "type": c.type,
                "status": c.status,
                "reason": c.reason or "",
                "message": c.message or "",
            }
            for c in (node.status.conditions or [])
        ]
        ctx.allocatable = {k: str(v) for k, v in (node.status.allocatable or {}).items()}
        ctx.capacity = {k: str(v) for k, v in (node.status.capacity or {}).items()}
        ctx.labels = dict(node.metadata.labels or {})
        ctx.taints = [f"{t.key}={t.value}:{t.effect}" for t in (node.spec.taints or [])]

        all_pods = await self._k8s.list_pods(namespace="")
        ctx.pods_on_node = sum(1 for p in all_pods if p.spec.node_name == node_name)

        return ctx

    @staticmethod
    def _state_string(state: client.V1ContainerState | None) -> str:
        if not state:
            return "unknown"
        if state.running:
            return f"Running (started={state.running.started_at})"
        if state.waiting:
            return f"Waiting reason={state.waiting.reason} msg={state.waiting.message}"
        if state.terminated:
            t = state.terminated
            return f"Terminated exit={t.exit_code} reason={t.reason} signal={t.signal}"
        return "unknown"
