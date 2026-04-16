"""
operator/main.py
─────────────────
K8s Autopilot operator entrypoint.

Responsibilities:
  1. Load config from environment / ConfigMap
  2. Initialise all engines and integrations
  3. Register kopf handlers for pods, nodes, deployments
  4. Serve the Slack webhook endpoint (aiohttp) for approval responses
  5. Start Prometheus metrics server

Run:
    python -m operator.main
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

import kopf
from aiohttp import web

from operator.audit.logger import AuditLogger
from operator.config import OperatorConfig
from operator.engines.approval_engine import ApprovalEngine
from operator.engines.context_collector import ContextCollector
from operator.engines.diagnosis_engine import DiagnosisEngine
from operator.engines.remediation_engine import RemediationEngine
from operator.handlers.node_handler import NodeHandler
from operator.handlers.pod_handler import PodHandler
from operator.integrations.pagerduty import PagerDutyClient
from operator.integrations.prometheus import AutopilotMetrics
from operator.integrations.slack import SlackClient
from operator.utils.k8s_client import init_k8s_client

# Import all action modules so their @registry.register decorators fire
import operator.remediations.pod_actions          # noqa: F401
import operator.remediations.deployment_actions   # noqa: F401

logger = logging.getLogger(__name__)


# ── Global references (set in startup, used in kopf handlers) ─────────────────
_pod_handler:  PodHandler  | None = None
_node_handler: NodeHandler | None = None


# ── Kopf lifecycle ─────────────────────────────────────────────────────────────

@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **_: Any) -> None:
    global _pod_handler, _node_handler

    cfg = OperatorConfig.load()

    # Logging
    log_level = getattr(logging, cfg.log_level.value, logging.INFO)
    logging.basicConfig(
        level   = log_level,
        format  = "%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt = "%Y-%m-%dT%H:%M:%S",
    )
    kopf.configure(verbose=cfg.log_level.value == "DEBUG")

    # Kopf settings
    settings.persistence.finalizer               = "autopilot.k8s.io/kopf-finalizer"
    settings.posting.enabled                     = False   # don't post kopf events to K8s
    settings.watching.connect_timeout            = 10
    settings.watching.reconnect_backoff          = 5

    # Initialise K8s client
    k8s = await init_k8s_client()

    # Initialise integrations
    metrics   = AutopilotMetrics.create(port=cfg.prometheus.port, enabled=cfg.prometheus.enabled)
    slack     = SlackClient(cfg.slack)
    pagerduty = PagerDutyClient(cfg.pagerduty)
    audit     = AuditLogger(cfg.audit_db_path)

    # Initialise engines
    collector  = ContextCollector(k8s, log_lines=cfg.log_lines)
    diagnoser  = DiagnosisEngine(cfg)
    approval   = ApprovalEngine(cfg, slack)
    remediator = RemediationEngine(cfg, k8s, approval, audit, metrics)

    # Build handlers
    _pod_handler = PodHandler(
        config=cfg, collector=collector, diagnoser=diagnoser,
        remediator=remediator, slack=slack, pagerduty=pagerduty, metrics=metrics,
    )
    _node_handler = NodeHandler(
        config=cfg, collector=collector, diagnoser=diagnoser,
        remediator=remediator, slack=slack, pagerduty=pagerduty, metrics=metrics,
    )

    # Start Slack webhook server
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
    Slack sends a POST with Content-Type: application/x-www-form-urlencoded
    and a 'payload' field containing JSON.
    """
    import json as _json
    try:
        data    = await request.post()
        payload = _json.loads(data.get("payload", "{}"))

        actions = payload.get("actions", [])
        user    = payload.get("user", {}).get("name", "unknown")

        for action in actions:
            value = action.get("value", "")
            if "|" not in value:
                continue
            decision, approval_id = value.split("|", 1)
            await approval.on_approval_response(
                approval_id = approval_id.strip(),
                decision    = decision.strip(),
                user_name   = user,
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


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    kopf.run(
        clusterwide = True,
        priority    = 100,
        standalone  = True,
    )


if __name__ == "__main__":
    main()
