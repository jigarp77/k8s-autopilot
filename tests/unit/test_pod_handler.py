"""
tests/unit/test_pod_handler.py
────────────────────────────────
Tests for pod trigger detection logic.
"""

from autopilot.handlers.pod_handler import _get_crash_trigger


class TestGetCrashTrigger:
    def _pod(self, container_statuses=None, conditions=None, phase="Running"):
        return {
            "status": {
                "phase": phase,
                "containerStatuses": container_statuses or [],
                "conditions": conditions or [],
            }
        }

    def _waiting_cs(self, name, reason, message=""):
        return {
            "name": name,
            "restartCount": 5,
            "state": {"waiting": {"reason": reason, "message": message}},
            "lastState": {},
        }

    def _terminated_cs(self, name, exit_code, reason=""):
        return {
            "name": name,
            "restartCount": 1,
            "state": {},
            "lastState": {"terminated": {"exitCode": exit_code, "reason": reason}},
        }

    def test_crash_loop_backoff_detected(self):
        pod = self._pod(container_statuses=[self._waiting_cs("app", "CrashLoopBackOff")])
        trigger, reason = _get_crash_trigger(pod)
        assert trigger == "CrashLoopBackOff"
        assert "CrashLoopBackOff" in reason

    def test_image_pull_backoff_detected(self):
        pod = self._pod(container_statuses=[self._waiting_cs("app", "ImagePullBackOff")])
        trigger, _ = _get_crash_trigger(pod)
        assert trigger == "ImagePullError"

    def test_err_image_pull_detected(self):
        pod = self._pod(container_statuses=[self._waiting_cs("app", "ErrImagePull")])
        trigger, _ = _get_crash_trigger(pod)
        assert trigger == "ImagePullError"

    def test_oom_killed_detected(self):
        pod = self._pod(container_statuses=[self._terminated_cs("app", 137, reason="OOMKilled")])
        trigger, _ = _get_crash_trigger(pod)
        assert trigger == "OOMKilled"

    def test_exit_137_detected_as_oom(self):
        pod = self._pod(container_statuses=[self._terminated_cs("app", 137)])
        trigger, _ = _get_crash_trigger(pod)
        assert trigger == "OOMKilled"

    def test_pending_unschedulable_detected(self):
        pod = self._pod(
            phase="Pending",
            conditions=[
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "message": "0/3 nodes available",
                }
            ],
        )
        trigger, reason = _get_crash_trigger(pod)
        assert trigger == "PendingScheduling"
        assert "nodes available" in reason

    def test_running_pod_no_trigger(self):
        pod = self._pod(
            container_statuses=[
                {
                    "name": "app",
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2024-01-01T00:00:00Z"}},
                    "lastState": {},
                }
            ]
        )
        trigger, _ = _get_crash_trigger(pod)
        assert trigger == ""

    def test_empty_pod_no_trigger(self):
        trigger, _ = _get_crash_trigger({})
        assert trigger == ""

    def test_first_container_triggers(self):
        """Multiple containers — first failing one wins."""
        pod = self._pod(
            container_statuses=[
                {
                    "name": "sidecar",
                    "restartCount": 0,
                    "state": {"running": {}},
                    "lastState": {},
                },
                self._waiting_cs("app", "CrashLoopBackOff"),
            ]
        )
        trigger, _ = _get_crash_trigger(pod)
        assert trigger == "CrashLoopBackOff"
