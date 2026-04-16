"""
operator/integrations/slack.py
────────────────────────────────
Slack integration using the official slack-sdk.

Capabilities:
  • Incident notification (rich Block Kit message)
  • Approval request with interactive buttons
  • Approval outcome update (replaces buttons with result)
  • Runbook posting as a thread reply
"""

from __future__ import annotations

import logging
from typing import Optional

from operator.config import SlackConfig
from operator.engines.diagnosis_engine import Diagnosis, RemediationAction, Severity

logger = logging.getLogger(__name__)

# Map severity → Slack colour sidebar
_SEVERITY_COLOUR = {
    Severity.CRITICAL: "#E53E3E",
    Severity.HIGH:     "#DD6B20",
    Severity.MEDIUM:   "#D69E2E",
    Severity.LOW:      "#38A169",
}

_SEVERITY_EMOJI = {
    Severity.CRITICAL: ":rotating_light:",
    Severity.HIGH:     ":warning:",
    Severity.MEDIUM:   ":large_yellow_circle:",
    Severity.LOW:      ":large_green_circle:",
}


class SlackClient:

    def __init__(self, config: SlackConfig) -> None:
        self._config = config
        self._client = None

        if config.enabled and config.token:
            try:
                from slack_sdk.web.async_client import AsyncWebClient
                self._client = AsyncWebClient(token=config.token)
            except ImportError:
                logger.warning("slack-sdk not installed — Slack integration disabled")

    async def post_incident_notification(
        self,
        diagnosis:  Diagnosis,
        namespace:  str,
        name:       str,
        trigger:    str,
        action_taken: str = "",
    ) -> Optional[str]:
        """Post a rich incident notification. Returns message timestamp."""
        if not self._client:
            return None

        sev    = diagnosis.severity
        emoji  = _SEVERITY_EMOJI.get(sev, ":bell:")
        colour = _SEVERITY_COLOUR.get(sev, "#718096")

        action_text = (
            f"*Action taken:* `{action_taken}`"
            if action_taken and action_taken != "no_action"
            else "*Action:* Notification only — no automated action"
        )

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} K8s Incident — {namespace}/{name}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Trigger*\n`{trigger}`"},
                    {"type": "mrkdwn", "text": f"*Severity*\n`{sev.value.upper()}`"},
                    {"type": "mrkdwn", "text": f"*Namespace*\n`{namespace}`"},
                    {"type": "mrkdwn", "text": f"*Confidence*\n`{int(diagnosis.confidence * 100)}%`"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root cause*\n{diagnosis.root_cause}",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": action_text},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"K8s Autopilot • Category: `{diagnosis.trigger_category.value}`",
                    }
                ],
            },
        ]

        try:
            resp = await self._client.chat_postMessage(
                channel     = self._config.channel,
                text        = f"{emoji} K8s incident on {namespace}/{name}: {diagnosis.root_cause}",
                attachments = [{"color": colour, "blocks": blocks}],
            )
            ts = resp.get("ts")
            logger.info("Incident notification posted to %s (ts=%s)", self._config.channel, ts)

            # Post runbook as thread reply if available
            if diagnosis.runbook and ts:
                await self._post_runbook_thread(ts, diagnosis.runbook)

            return ts
        except Exception as exc:
            logger.error("Slack notification failed: %s", exc)
            return None

    async def post_approval_request(
        self,
        approval_id: str,
        diagnosis:   Diagnosis,
        action:      RemediationAction,
        namespace:   str,
        name:        str,
    ) -> Optional[str]:
        """Post an interactive approval request with Approve/Reject buttons."""
        if not self._client:
            return None

        sev   = diagnosis.severity
        emoji = _SEVERITY_EMOJI.get(sev, ":bell:")

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} Remediation approval required",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Resource*\n`{namespace}/{name}`"},
                    {"type": "mrkdwn", "text": f"*Severity*\n`{sev.value.upper()}`"},
                    {"type": "mrkdwn", "text": f"*Trigger*\n`{diagnosis.trigger_category.value}`"},
                    {"type": "mrkdwn", "text": f"*Confidence*\n`{int(diagnosis.confidence * 100)}%`"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root cause*\n{diagnosis.root_cause}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Proposed action*\n`{action.action}`\n"
                        f"_{action.description}_"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "Approve"},
                        "style":     "primary",
                        "action_id": f"approve_{approval_id}",
                        "value":     f"approve|{approval_id}",
                    },
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "Reject"},
                        "style":     "danger",
                        "action_id": f"reject_{approval_id}",
                        "value":     f"reject|{approval_id}",
                    },
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Approval ID: `{approval_id}` • Auto-rejects in {self._config.timeout_seconds}s",
                    }
                ],
            },
        ]

        try:
            resp = await self._client.chat_postMessage(
                channel = self._config.approval_channel,
                text    = f"Remediation approval needed for {namespace}/{name}",
                blocks  = blocks,
            )
            ts = resp.get("ts")
            logger.info("Approval request posted (id=%s ts=%s)", approval_id, ts)
            return ts
        except Exception as exc:
            logger.error("Failed to post approval request: %s", exc)
            return None

    async def update_approval_message(
        self,
        ts:          str,
        approval_id: str,
        decision:    object,
        decided_by:  str = "",
    ) -> None:
        """Replace the interactive buttons with the final decision."""
        if not self._client or not ts:
            return

        decision_str = str(decision).replace("ApprovalDecision.", "").lower()
        emoji        = ":white_check_mark:" if "approved" in decision_str else ":x:"
        decided_text = f" by {decided_by}" if decided_by else ""

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *{decision_str.upper()}*{decided_text} — Approval `{approval_id}`",
                },
            }
        ]

        try:
            await self._client.chat_update(
                channel = self._config.approval_channel,
                ts      = ts,
                blocks  = blocks,
                text    = f"Approval {decision_str}",
            )
        except Exception as exc:
            logger.warning("Failed to update approval message: %s", exc)

    async def _post_runbook_thread(self, parent_ts: str, runbook: str) -> None:
        try:
            await self._client.chat_postMessage(
                channel   = self._config.channel,
                thread_ts = parent_ts,
                text      = f"*Generated runbook:*\n```{runbook[:2000]}```",
                mrkdwn    = True,
            )
        except Exception as exc:
            logger.warning("Failed to post runbook thread: %s", exc)

    @property
    def enabled(self) -> bool:
        return self._client is not None
