"""
operator/engines/approval_engine.py
──────────────────────────────────────
Human-in-the-loop Slack approval workflow.

Flow:
  1. Post an interactive Block Kit message to #sre-approvals
  2. Store pending approval in an in-memory dict keyed by approval_id
  3. The Slack webhook endpoint (served by a small aiohttp server in main.py)
     calls on_approval_response() with the action payload
  4. The waiting coroutine is unblocked via asyncio.Event
  5. If nobody responds within timeout_seconds: auto-reject

Block Kit message includes:
  • Severity badge + root cause
  • Recommended action description
  • Approve / Reject buttons that POST back to our webhook
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from operator.config import OperatorConfig
from operator.engines.diagnosis_engine import Diagnosis, RemediationAction
from operator.integrations.slack import SlackClient

logger = logging.getLogger(__name__)


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT  = "timeout"


@dataclass
class PendingApproval:
    approval_id: str
    namespace:   str
    name:        str
    trigger:     str
    action:      str
    created_at:  datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event:       asyncio.Event = field(default_factory=asyncio.Event)
    decision:    Optional[ApprovalDecision] = None
    decided_by:  str = ""
    ts:          str = ""   # Slack message timestamp (for updating the message)


class ApprovalEngine:

    def __init__(self, config: OperatorConfig, slack: SlackClient) -> None:
        self._config   = config
        self._slack    = slack
        self._pending:  dict[str, PendingApproval] = {}

    async def request_approval(
        self,
        diagnosis:  Diagnosis,
        action:     RemediationAction,
        namespace:  str,
        name:       str,
        trigger:    str,
    ) -> ApprovalDecision:
        if not self._config.slack.enabled:
            logger.warning(
                "Approval required for %s/%s but Slack not configured — auto-rejecting",
                namespace, name,
            )
            return ApprovalDecision.REJECTED

        approval_id = str(uuid.uuid4())[:8]
        pending     = PendingApproval(
            approval_id = approval_id,
            namespace   = namespace,
            name        = name,
            trigger     = trigger,
            action      = action.action,
        )
        self._pending[approval_id] = pending

        # Post Slack approval request
        ts = await self._slack.post_approval_request(
            approval_id = approval_id,
            diagnosis   = diagnosis,
            action      = action,
            namespace   = namespace,
            name        = name,
        )
        pending.ts = ts or ""

        logger.info(
            "Approval request sent for %s/%s (trigger=%s action=%s id=%s) — waiting %ds",
            namespace, name, trigger, action.action, approval_id,
            self._config.slack.timeout_seconds,
        )

        # Wait for decision or timeout
        try:
            await asyncio.wait_for(
                pending.event.wait(),
                timeout=self._config.slack.timeout_seconds,
            )
            decision = pending.decision or ApprovalDecision.REJECTED
        except asyncio.TimeoutError:
            decision = ApprovalDecision.TIMEOUT
            logger.warning(
                "Approval timed out for %s/%s (id=%s)", namespace, name, approval_id
            )

        # Update Slack message to show outcome
        await self._slack.update_approval_message(
            ts          = pending.ts,
            approval_id = approval_id,
            decision    = decision,
            decided_by  = pending.decided_by,
        )

        del self._pending[approval_id]
        return decision

    async def on_approval_response(
        self,
        approval_id: str,
        decision:    str,
        user_name:   str = "",
    ) -> bool:
        """
        Called by the Slack webhook handler when an engineer clicks
        Approve or Reject in Slack.
        """
        pending = self._pending.get(approval_id)
        if not pending:
            logger.warning("Unknown approval ID: %s", approval_id)
            return False

        pending.decision   = ApprovalDecision(decision)
        pending.decided_by = user_name
        pending.event.set()

        logger.info(
            "Approval %s for %s/%s (trigger=%s): %s by %s",
            approval_id, pending.namespace, pending.name,
            pending.trigger, decision, user_name,
        )
        return True

    def pending_count(self) -> int:
        return len(self._pending)

    def list_pending(self) -> list[dict]:
        return [
            {
                "id":        p.approval_id,
                "resource":  f"{p.namespace}/{p.name}",
                "trigger":   p.trigger,
                "action":    p.action,
                "age_seconds": (datetime.now(timezone.utc) - p.created_at).seconds,
            }
            for p in self._pending.values()
        ]
