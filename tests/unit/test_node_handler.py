"""
tests/unit/test_node_handler.py
─────────────────────────────────
Tests for node trigger detection logic.
"""

from autopilot.handlers.node_handler import _get_node_trigger


class TestGetNodeTrigger:
    def _node(self, conditions):
        return {"status": {"conditions": conditions}}

    def test_not_ready_detected(self):
        node = self._node(
            [
                {"type": "Ready", "status": "False", "message": "kubelet stopped"},
            ]
        )
        trigger, reason = _get_node_trigger(node)
        assert trigger == "NodeNotReady"
        assert "kubelet stopped" in reason

    def test_ready_true_no_trigger(self):
        node = self._node(
            [
                {"type": "Ready", "status": "True", "message": "kubelet is ready"},
            ]
        )
        trigger, _ = _get_node_trigger(node)
        assert trigger == ""

    def test_disk_pressure_detected(self):
        node = self._node(
            [
                {"type": "Ready", "status": "True"},
                {"type": "DiskPressure", "status": "True", "message": "disk full"},
            ]
        )
        trigger, reason = _get_node_trigger(node)
        assert trigger == "NodeDiskPressure"
        assert "disk full" in reason

    def test_memory_pressure_detected(self):
        node = self._node(
            [
                {"type": "Ready", "status": "True"},
                {"type": "MemoryPressure", "status": "True", "message": "oom"},
            ]
        )
        trigger, _ = _get_node_trigger(node)
        assert trigger == "NodeMemoryPressure"

    def test_pid_pressure_detected(self):
        node = self._node(
            [
                {"type": "PIDPressure", "status": "True", "message": "too many processes"},
            ]
        )
        trigger, _ = _get_node_trigger(node)
        assert trigger == "NodePIDPressure"

    def test_empty_conditions_no_trigger(self):
        trigger, _ = _get_node_trigger(self._node([]))
        assert trigger == ""

    def test_first_matching_condition_wins(self):
        """Ready is checked first, then DiskPressure."""
        node = self._node(
            [
                {"type": "Ready", "status": "False", "message": "not ready"},
                {"type": "DiskPressure", "status": "True", "message": "disk"},
            ]
        )
        trigger, _ = _get_node_trigger(node)
        assert trigger == "NodeNotReady"

    def test_unknown_condition_ignored(self):
        node = self._node(
            [
                {"type": "SomeRandomCondition", "status": "True"},
            ]
        )
        trigger, _ = _get_node_trigger(node)
        assert trigger == ""
