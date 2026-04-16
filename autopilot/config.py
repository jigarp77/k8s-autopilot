"""
autopilot/config.py
────────────────────
Centralised configuration for K8s Autopilot.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class OperatorMode(StrEnum):
    DRY_RUN = "dry-run"
    SUGGEST = "suggest"
    AUTO = "auto"
    APPROVAL = "approval"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class SlackConfig:
    token: str = ""
    channel: str = "#incidents"
    approval_channel: str = "#sre-approvals"
    webhook_url: str = ""
    timeout_seconds: int = 300
    enabled: bool = False

    @classmethod
    def from_env(cls) -> SlackConfig:
        return cls(
            token=os.environ.get("SLACK_BOT_TOKEN", ""),
            channel=os.environ.get("SLACK_CHANNEL", "#incidents"),
            approval_channel=os.environ.get("SLACK_APPROVAL_CHANNEL", "#sre-approvals"),
            webhook_url=os.environ.get("SLACK_WEBHOOK_URL", ""),
            timeout_seconds=int(os.environ.get("SLACK_APPROVAL_TIMEOUT", "300")),
            enabled=bool(os.environ.get("SLACK_BOT_TOKEN")),
        )


@dataclass
class PagerDutyConfig:
    api_key: str = ""
    service_id: str = ""
    escalation_policy: str = ""
    severity_threshold: str = "high"
    enabled: bool = False

    @classmethod
    def from_env(cls) -> PagerDutyConfig:
        return cls(
            api_key=os.environ.get("PD_API_KEY", ""),
            service_id=os.environ.get("PD_SERVICE_ID", ""),
            escalation_policy=os.environ.get("PD_ESCALATION_POLICY", ""),
            enabled=bool(os.environ.get("PD_API_KEY")),
        )


@dataclass
class PrometheusConfig:
    enabled: bool = True
    port: int = 8000
    path: str = "/metrics"

    @classmethod
    def from_env(cls) -> PrometheusConfig:
        return cls(
            enabled=os.environ.get("PROMETHEUS_ENABLED", "true").lower() == "true",
            port=int(os.environ.get("PROMETHEUS_PORT", "8000")),
        )


@dataclass
class AnthropicConfig:
    api_key: str = ""
    model: str = "claude-opus-4-5"
    max_tokens: int = 2048
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> AnthropicConfig:
        return cls(
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5"),
        )


@dataclass
class RateLimitConfig:
    global_max_per_hour: int = 20
    per_resource_max_per_hour: int = 3
    cooldown_seconds: int = 300
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout: int = 600


@dataclass
class TriggerRule:
    """A single remediation rule from the AutopilotPolicy CRD."""

    trigger: str = ""
    auto_remediate: bool = False
    require_approval: bool = True
    max_per_hour: int = 3
    notify_pd: bool = False
    min_restart_count: int = 3
    cooldown_after_action: int = 300

    @classmethod
    def from_dict(cls, d: dict) -> TriggerRule:
        return cls(
            trigger=d.get("trigger", ""),
            auto_remediate=d.get("autoRemediate", False),
            require_approval=d.get("requireApproval", True),
            max_per_hour=d.get("maxPerHour", 3),
            notify_pd=d.get("notifyPagerDuty", False),
            min_restart_count=d.get("minRestartCount", 3),
        )


@dataclass
class OperatorConfig:
    mode: OperatorMode = OperatorMode.SUGGEST
    log_level: LogLevel = LogLevel.INFO
    target_namespaces: list[str] = field(default_factory=list)
    ignored_namespaces: list[str] = field(default_factory=lambda: ["kube-system", "kube-public"])
    watched_labels: dict[str, str] = field(default_factory=dict)

    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    pagerduty: PagerDutyConfig = field(default_factory=PagerDutyConfig)
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)

    default_rules: list[TriggerRule] = field(default_factory=list)

    log_lines: int = 200
    metrics_window_min: int = 15
    include_node_info: bool = True
    include_events: bool = True

    audit_db_path: str = "/data/autopilot-audit.db"

    @classmethod
    def load(cls) -> OperatorConfig:
        cfg = cls(
            mode=OperatorMode(os.environ.get("AUTOPILOT_MODE", "suggest")),
            log_level=LogLevel(os.environ.get("LOG_LEVEL", "INFO")),
            target_namespaces=[
                ns.strip()
                for ns in os.environ.get("TARGET_NAMESPACES", "").split(",")
                if ns.strip()
            ],
            anthropic=AnthropicConfig.from_env(),
            slack=SlackConfig.from_env(),
            pagerduty=PagerDutyConfig.from_env(),
            prometheus=PrometheusConfig.from_env(),
        )

        config_file = Path(os.environ.get("CONFIG_FILE", "/etc/autopilot/config.yaml"))
        if config_file.exists():
            cfg = cls._overlay_yaml(cfg, config_file)

        cfg._validate()
        logger.info(
            "Config loaded: mode=%s namespaces=%s slack=%s pd=%s",
            cfg.mode,
            cfg.target_namespaces or "all",
            cfg.slack.enabled,
            cfg.pagerduty.enabled,
        )
        return cfg

    @classmethod
    def _overlay_yaml(cls, cfg: OperatorConfig, path: Path) -> OperatorConfig:
        try:
            data = yaml.safe_load(path.read_text()) or {}
            if "mode" in data:
                cfg.mode = OperatorMode(data["mode"])
            if "targetNamespaces" in data:
                cfg.target_namespaces = data["targetNamespaces"]
            if "ignoredNamespaces" in data:
                cfg.ignored_namespaces = data["ignoredNamespaces"]
            if "logLines" in data:
                cfg.log_lines = int(data["logLines"])
            rl = data.get("rateLimit", {})
            if rl:
                cfg.rate_limit = RateLimitConfig(
                    global_max_per_hour=rl.get("globalMaxPerHour", 20),
                    per_resource_max_per_hour=rl.get("perResourceMaxPerHour", 3),
                    cooldown_seconds=rl.get("cooldownSeconds", 300),
                )
            rules = data.get("defaultRules", [])
            if rules:
                cfg.default_rules = [TriggerRule.from_dict(r) for r in rules]
        except Exception as exc:
            logger.warning("Failed to parse config file %s: %s", path, exc)
        return cfg

    def _validate(self) -> None:
        if not self.anthropic.api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set — AI diagnosis will fail. "
                "Set the env var or mount a K8s Secret."
            )
        if self.mode == OperatorMode.AUTO and not self.slack.enabled:
            logger.warning(
                "Mode is 'auto' but Slack is not configured — "
                "remediation notifications will be silent."
            )

    def is_namespace_watched(self, namespace: str) -> bool:
        if namespace in self.ignored_namespaces:
            return False
        if not self.target_namespaces:
            return True
        return namespace in self.target_namespaces

    def get_rule(self, trigger: str) -> TriggerRule | None:
        for rule in self.default_rules:
            if rule.trigger == trigger:
                return rule
        return None
