"""
autopilot/main.py
──────────────────
K8s Autopilot operator entrypoint.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any

import kopf
from aiohttp import web

# Import all action modules so their @registry.register decorators fire
import autopilot.remediations.deployment_actions  # noqa: F401
import autopilot.remediations.pod_actions  # noqa: F401
from autopilot.audit.logger import AuditLogger
from autopilot.config import OperatorConfig
from autopilot.engines.approval_engine import ApprovalEngine
from autopilot.engines.context_collector import ContextCollector
from autopilot.engines.diagnosis_engine import DiagnosisEngine
from autopilot.engines.remediation_engine import RemediationEngine
from autopilot.handlers.node_handler import NodeHandler
from autopilot.handlers.pod_handler import PodHandler
from autopilot.integrations.pagerduty import PagerDutyClient
from autopilot.integrations.prometheus import AutopilotMetrics
from autopilot.integrations.slack import SlackClient
from autopilot.utils.k8s_client import init_k8s_client

logger = logging.getLogger(__name__)


# Globals set in startup
_pod_handler: PodHandler | None = None
_node_handler: NodeHandler | None = None


# ── Kopf lifecycle ─────────────────────────────────────────────────────────────


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **_: Any) -> None:
    global _pod_handler, _node_handler

    cfg = OperatorConfig.load()

    log_level = getattr(logging, cfg.log_level.value, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    settings.persistence.finalizer = "autopilot.k8s.io/kopf-finalizer"
    settings.posting.enabled = False
    settings.watching.connect_timeout = 10
    settings.watching.reconnect_backoff = 5

    k8s = await init_k8s_client()

    metrics = AutopilotMetrics.create(port=cfg.prometheus.port, enabled=cfg.prometheus.enabled)
    slack = SlackClient(cfg.slack)
    pagerduty = PagerDutyClient(cfg.pagerduty)
    audit = AuditLogger(cfg.audit_db_path)

    collector = ContextCollector(k8s, log_lines=cfg.log_lines)
    diagnoser = DiagnosisEngine(cfg)
    approval = ApprovalEngine(cfg, slack)
    remediator = RemediationEngine(cfg, k8s, approval, audit, metrics)

    _pod_handler = PodHandler(
        config=cfg,
        collector=collector,
        diagnoser=diagnoser,
        remediator=remediator,
        slack=slack,
        pagerduty=pagerduty,
        metrics=metrics,
    )
    _node_handler = NodeHandler(
        config=cfg,
        collector=collector,
        diagnoser=diagnoser,
        remediator=remediator,
        slack=slack,
        pagerduty=pagerduty,
        metrics=metrics,
    )

    asyncio.ensure_future(_start_webhook_server(approval, port=8080))

    logger.info(
        "K8s Autopilot started — mode=%s namespaces=%s",
        cfg.mode.value,
        cfg.target_namespaces or "all",
    )


@kopf.on.cleanup()
async def cleanup(**_: Any) -> None:
    logger.info("K8s Autopilot shutting down")


# ── Kopf event handlers ────────────────────────────────────────────────────────


@kopf.on.event("", "v1", "pods")
async def on_pod_event(event: dict, **kwargs: Any) -> None:
    if _pod_handler and event.get("type") in ("MODIFIED", "ADDED"):
        await _pod_handler.on_pod_event(event, **kwargs)


@kopf.on.event("", "v1", "nodes")
async def on_node_event(event: dict, **kwargs: Any) -> None:
    if _node_handler and event.get("type") == "MODIFIED":
        await _node_handler.on_node_event(event, **kwargs)


# ── Slack interactive webhook ─────────────────────────────────────────────────


async def _slack_webhook_handler(
    request: web.Request,
    approval: ApprovalEngine,
) -> web.Response:
    """
    Receives Slack interactive component payloads (button clicks).
    """
    try:
        data = await request.post()
        payload = _json.loads(data.get("payload", "{}"))

        actions = payload.get("actions", [])
        user = payload.get("user", {}).get("name", "unknown")

        for action in actions:
            value = action.get("value", "")
            if "|" not in value:
                continue
            decision, approval_id = value.split("|", 1)
            await approval.on_approval_response(
                approval_id=approval_id.strip(),
                decision=decision.strip(),
                user_name=user,
            )

        return web.Response(text="OK", status=200)
    except Exception as exc:
        logger.error("Webhook handler error: %s", exc)
        return web.Response(text="Error", status=500)


async def _health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "k8s-autopilot"})


async def _start_webhook_server(approval: ApprovalEngine, port: int = 8080) -> None:
    app = web.Application()
    app.router.add_post("/slack/actions", lambda r: _slack_webhook_handler(r, approval))
    app.router.add_get("/healthz", _health_handler)
    app.router.add_get("/readyz", _health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Webhook server started on :%d", port)


def main() -> None:
    kopf.run(
        clusterwide=True,
        priority=100,
        standalone=True,
    )


if __name__ == "__main__":
    main()
