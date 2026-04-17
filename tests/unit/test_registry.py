"""
tests/unit/test_registry.py
─────────────────────────────
Tests for the remediation action registry (plugin pattern).
"""

import pytest

from autopilot.remediations.registry import ActionResult, RemediationRegistry


@pytest.fixture
def reg():
    return RemediationRegistry()


class TestRegistry:
    def test_starts_empty(self, reg):
        assert reg.list_actions() == []

    def test_register_adds_action(self, reg):
        @reg.register("test_action", description="A test action", safe_auto=True)
        async def my_action(k8s, namespace, name, **kwargs):
            return ActionResult(success=True, action="test_action", message="ok")

        assert reg.has("test_action")
        assert reg.get("test_action") is my_action

    def test_metadata_populated(self, reg):
        @reg.register("act_x", description="desc", safe_auto=False)
        async def act_x(k8s, namespace, name, **kwargs):
            return ActionResult(success=True, action="act_x", message="")

        actions = reg.list_actions()
        assert len(actions) == 1
        entry = actions[0]
        assert entry["key"] == "act_x"
        assert entry["description"] == "desc"
        assert entry["safe_auto"] is False
        assert entry["function"] == "act_x"

    def test_docstring_used_when_no_description(self, reg):
        @reg.register("act_docstring", safe_auto=True)
        async def act_docstring(k8s, namespace, name, **kwargs):
            """This is the docstring."""
            return ActionResult(success=True, action="act_docstring", message="")

        entry = reg.list_actions()[0]
        assert "This is the docstring" in entry["description"]

    def test_missing_action_returns_none(self, reg):
        assert reg.get("does_not_exist") is None
        assert reg.has("does_not_exist") is False

    def test_multiple_registrations(self, reg):
        @reg.register("a")
        async def _a(k8s, namespace, name, **kwargs):
            return ActionResult(success=True, action="a", message="")

        @reg.register("b")
        async def _b(k8s, namespace, name, **kwargs):
            return ActionResult(success=True, action="b", message="")

        assert reg.has("a")
        assert reg.has("b")
        assert len(reg.list_actions()) == 2

    def test_action_result_defaults(self):
        ar = ActionResult(success=True, message="ok")
        assert ar.output == {}
        assert ar.action == ""
        assert ar.dry_run is False

    @pytest.mark.asyncio
    async def test_registered_action_can_be_called(self, reg):
        @reg.register("callable_action")
        async def action(k8s, namespace, name, **kwargs):
            return ActionResult(
                success=True, action="callable_action", message=f"ran on {namespace}/{name}"
            )

        fn = reg.get("callable_action")
        result = await fn(k8s=None, namespace="ns", name="pod1")
        assert result.success
        assert "ns/pod1" in result.message
