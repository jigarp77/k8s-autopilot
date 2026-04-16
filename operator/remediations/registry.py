"""
operator/remediations/registry.py
────────────────────────────────────
Plugin-pattern action registry.

Every remediation action is a plain async function with the signature:

    async def action(k8s: K8sClient, namespace: str, name: str, **kwargs) -> ActionResult

Actions are registered with @registry.register("action_key") and looked up by
the remediation engine using the key returned by the diagnosis engine.

This pattern means new actions can be added without touching the engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    success:   bool
    message:   str
    action:    str = ""
    dry_run:   bool = False
    output:    dict = None   # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.output is None:
            self.output = {}


# Type alias for action functions
ActionFn = Callable[..., Awaitable[ActionResult]]


class RemediationRegistry:
    """Central registry of all available remediation actions."""

    def __init__(self) -> None:
        self._actions: dict[str, ActionFn] = {}
        self._metadata: dict[str, dict]    = {}

    def register(
        self,
        key:         str,
        description: str = "",
        safe_auto:   bool = False,
    ) -> Callable[[ActionFn], ActionFn]:
        """Decorator to register an action function."""
        def decorator(fn: ActionFn) -> ActionFn:
            self._actions[key] = fn
            self._metadata[key] = {
                "description": description or fn.__doc__ or "",
                "safe_auto":   safe_auto,
                "function":    fn.__name__,
            }
            logger.debug("Registered remediation action: %s", key)
            return fn
        return decorator

    def get(self, key: str) -> Optional[ActionFn]:
        return self._actions.get(key)

    def list_actions(self) -> list[dict]:
        return [
            {"key": k, **v}
            for k, v in self._metadata.items()
        ]

    def has(self, key: str) -> bool:
        return key in self._actions


# Module-level singleton used across the codebase
registry = RemediationRegistry()
