"""
tests/unit/test_audit_logger.py
─────────────────────────────────
Tests for AuditLogger SQLite persistence.
"""

from datetime import UTC, datetime

import pytest

from autopilot.audit.logger import AuditLogger, AuditOutcome, AuditRecord


@pytest.fixture
def audit(tmp_path):
    db_path = tmp_path / "test_audit.db"
    return AuditLogger(str(db_path))


@pytest.fixture
def sample_record():
    return AuditRecord(
        resource_id="default/my-pod",
        namespace="default",
        name="my-pod",
        trigger="CrashLoopBackOff",
        action="restart_pod",
        outcome=AuditOutcome.EXECUTED,
        diagnosis={"root_cause": "missing env var"},
        action_output={"restarted": True},
        tokens_used=1500,
    )


class TestAuditLogger:
    @pytest.mark.asyncio
    async def test_log_returns_row_id(self, audit, sample_record):
        row_id = await audit.log(sample_record)
        assert row_id > 0

    @pytest.mark.asyncio
    async def test_log_persists_data(self, audit, sample_record):
        await audit.log(sample_record)
        rows = audit.query(namespace="default")
        assert len(rows) == 1
        assert rows[0]["name"] == "my-pod"
        assert rows[0]["trigger"] == "CrashLoopBackOff"
        assert rows[0]["outcome"] == "executed"
        assert rows[0]["tokens_used"] == 1500

    @pytest.mark.asyncio
    async def test_query_filter_by_namespace(self, audit, sample_record):
        await audit.log(sample_record)
        other = AuditRecord(
            resource_id="prod/other",
            namespace="prod",
            name="other",
            trigger="OOMKilled",
            action="no_action",
            outcome=AuditOutcome.NOTIFIED,
        )
        await audit.log(other)

        default_rows = audit.query(namespace="default")
        assert len(default_rows) == 1
        prod_rows = audit.query(namespace="prod")
        assert len(prod_rows) == 1

    @pytest.mark.asyncio
    async def test_query_filter_by_outcome(self, audit, sample_record):
        await audit.log(sample_record)
        failed = AuditRecord(
            resource_id="default/bad",
            namespace="default",
            name="bad",
            trigger="OOMKilled",
            action="restart_pod",
            outcome=AuditOutcome.FAILED,
        )
        await audit.log(failed)

        executed = audit.query(outcome="executed")
        assert len(executed) == 1
        failed_rows = audit.query(outcome="failed")
        assert len(failed_rows) == 1

    @pytest.mark.asyncio
    async def test_query_respects_limit(self, audit):
        for i in range(10):
            await audit.log(
                AuditRecord(
                    resource_id=f"default/pod-{i}",
                    namespace="default",
                    name=f"pod-{i}",
                    trigger="CrashLoopBackOff",
                    action="restart_pod",
                    outcome=AuditOutcome.EXECUTED,
                )
            )
        rows = audit.query(limit=5)
        assert len(rows) == 5

    @pytest.mark.asyncio
    async def test_summary_counts_outcomes(self, audit):
        for outcome, n in [
            (AuditOutcome.EXECUTED, 3),
            (AuditOutcome.FAILED, 2),
            (AuditOutcome.REJECTED, 1),
            (AuditOutcome.DRY_RUN, 4),
        ]:
            for i in range(n):
                await audit.log(
                    AuditRecord(
                        resource_id=f"ns/pod-{i}",
                        namespace="ns",
                        name=f"pod-{i}",
                        trigger="test",
                        action="test",
                        outcome=outcome,
                    )
                )

        summary = audit.summary()
        assert summary["total"] == 10
        assert summary["executed"] == 3
        assert summary["failed"] == 2
        assert summary["rejected"] == 1
        assert summary["dry_run"] == 4

    @pytest.mark.asyncio
    async def test_completed_at_stored(self, audit, sample_record):
        sample_record.completed_at = datetime.now(UTC)
        await audit.log(sample_record)
        rows = audit.query()
        assert rows[0]["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_diagnosis_serialised_as_json(self, audit):
        rec = AuditRecord(
            resource_id="default/p",
            namespace="default",
            name="p",
            trigger="t",
            action="a",
            outcome=AuditOutcome.EXECUTED,
            diagnosis={"nested": {"key": "value"}, "list": [1, 2, 3]},
        )
        await audit.log(rec)
        rows = audit.query()
        import json

        parsed = json.loads(rows[0]["diagnosis"])
        assert parsed["nested"]["key"] == "value"
        assert parsed["list"] == [1, 2, 3]
