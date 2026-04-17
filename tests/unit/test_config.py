"""
tests/unit/test_config.py
───────────────────────────
Tests for OperatorConfig loading and validation.
"""

import os
from unittest.mock import patch

import pytest

from autopilot.config import (
    AnthropicConfig,
    LogLevel,
    OperatorConfig,
    OperatorMode,
    PagerDutyConfig,
    SlackConfig,
    TriggerRule,
)


class TestSubConfigs:
    def test_slack_from_env_disabled_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = SlackConfig.from_env()
            assert cfg.enabled is False
            assert cfg.token == ""

    def test_slack_from_env_enabled_with_token(self):
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-abc"}, clear=True):
            cfg = SlackConfig.from_env()
            assert cfg.enabled is True
            assert cfg.token == "xoxb-abc"
            assert cfg.channel == "#incidents"

    def test_slack_custom_channel(self):
        with patch.dict(
            os.environ,
            {"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "#alerts"},
            clear=True,
        ):
            cfg = SlackConfig.from_env()
            assert cfg.channel == "#alerts"

    def test_pagerduty_from_env(self):
        with patch.dict(os.environ, {"PD_API_KEY": "key-123"}, clear=True):
            cfg = PagerDutyConfig.from_env()
            assert cfg.enabled is True
            assert cfg.api_key == "key-123"

    def test_pagerduty_disabled_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = PagerDutyConfig.from_env()
            assert cfg.enabled is False

    def test_anthropic_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = AnthropicConfig.from_env()
            assert cfg.model == "claude-opus-4-5"
            assert cfg.api_key == ""


class TestTriggerRule:
    def test_from_dict_full(self):
        rule = TriggerRule.from_dict(
            {
                "trigger": "CrashLoopBackOff",
                "autoRemediate": True,
                "requireApproval": False,
                "maxPerHour": 5,
                "minRestartCount": 10,
                "notifyPagerDuty": True,
            }
        )
        assert rule.trigger == "CrashLoopBackOff"
        assert rule.auto_remediate is True
        assert rule.require_approval is False
        assert rule.max_per_hour == 5
        assert rule.min_restart_count == 10
        assert rule.notify_pd is True

    def test_from_dict_defaults(self):
        rule = TriggerRule.from_dict({"trigger": "OOMKilled"})
        assert rule.trigger == "OOMKilled"
        assert rule.require_approval is True
        assert rule.max_per_hour == 3


class TestOperatorConfig:
    def test_defaults(self):
        cfg = OperatorConfig()
        assert cfg.mode == OperatorMode.SUGGEST
        assert cfg.log_level == LogLevel.INFO
        assert cfg.target_namespaces == []
        assert "kube-system" in cfg.ignored_namespaces

    def test_is_namespace_watched_empty_target_watches_all(self):
        cfg = OperatorConfig()
        assert cfg.is_namespace_watched("production") is True
        assert cfg.is_namespace_watched("anything") is True

    def test_is_namespace_watched_respects_target(self):
        cfg = OperatorConfig(target_namespaces=["production", "staging"])
        assert cfg.is_namespace_watched("production") is True
        assert cfg.is_namespace_watched("staging") is True
        assert cfg.is_namespace_watched("random") is False

    def test_is_namespace_watched_respects_ignored(self):
        cfg = OperatorConfig()
        assert cfg.is_namespace_watched("kube-system") is False

    def test_ignored_beats_target(self):
        cfg = OperatorConfig(
            target_namespaces=["kube-system"],
            ignored_namespaces=["kube-system"],
        )
        assert cfg.is_namespace_watched("kube-system") is False

    def test_get_rule_returns_matching(self):
        cfg = OperatorConfig(
            default_rules=[
                TriggerRule(trigger="CrashLoopBackOff", max_per_hour=5),
                TriggerRule(trigger="OOMKilled", max_per_hour=2),
            ]
        )
        rule = cfg.get_rule("CrashLoopBackOff")
        assert rule is not None
        assert rule.max_per_hour == 5

    def test_get_rule_returns_none_for_missing(self):
        cfg = OperatorConfig()
        assert cfg.get_rule("NonExistent") is None

    def test_load_reads_env(self):
        with patch.dict(
            os.environ,
            {
                "AUTOPILOT_MODE": "auto",
                "LOG_LEVEL": "DEBUG",
                "TARGET_NAMESPACES": "prod,staging",
            },
            clear=True,
        ):
            cfg = OperatorConfig.load()
            assert cfg.mode == OperatorMode.AUTO
            assert cfg.log_level == LogLevel.DEBUG
            assert cfg.target_namespaces == ["prod", "staging"]

    def test_load_invalid_mode_raises(self):
        with (
            patch.dict(os.environ, {"AUTOPILOT_MODE": "invalid"}, clear=True),
            pytest.raises(ValueError),
        ):
            OperatorConfig.load()
