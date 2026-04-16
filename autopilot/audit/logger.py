"""
autopilot/audit/logger.py
──────────────────────────
Persistent audit trail backed by SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class AuditOutcome(StrEnum):
    EXECUTED = "executed"
    DRY_RUN = "dry_run"
    REJECTED = "rejected"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOTIFIED = "notified"


@dataclass
class AuditRecord:
    resource_id: str
    namespace: str
    name: str
    trigger: str
    action: str
    outcome: AuditOutcome
    diagnosis: dict = field(default_factory=dict)
    action_output: dict = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    decided_by: str = ""
    tokens_used: int = 0


class AuditLogger:
    _DDL = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        resource_id   TEXT NOT NULL,
        namespace     TEXT NOT NULL,
        name          TEXT NOT NULL,
        trigger       TEXT NOT NULL,
        action        TEXT NOT NULL,
        outcome       TEXT NOT NULL,
        diagnosis     TEXT,
        action_output TEXT,
        started_at    TEXT NOT NULL,
        completed_at  TEXT,
        decided_by    TEXT,
        tokens_used   INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_resource   ON audit_log(resource_id);
    CREATE INDEX IF NOT EXISTS idx_namespace  ON audit_log(namespace);
    CREATE INDEX IF NOT EXISTS idx_started_at ON audit_log(started_at);
    CREATE INDEX IF NOT EXISTS idx_outcome    ON audit_log(outcome);
    """

    def __init__(self, db_path: str = "/data/autopilot-audit.db") -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()
        logger.info("Audit logger initialised: %s", db_path)

    async def log(self, record: AuditRecord) -> int:
        try:
            cur = self._conn.execute(
                """
                INSERT INTO audit_log
                    (resource_id, namespace, name, trigger, action, outcome,
                     diagnosis, action_output, started_at, completed_at,
                     decided_by, tokens_used)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.resource_id,
                    record.namespace,
                    record.name,
                    record.trigger,
                    record.action,
                    record.outcome.value,
                    json.dumps(record.diagnosis),
                    json.dumps(record.action_output),
                    record.started_at.isoformat(),
                    record.completed_at.isoformat() if record.completed_at else None,
                    record.decided_by,
                    record.tokens_used,
                ),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as exc:
            logger.error("Audit log write failed: %s", exc)
            return -1

    def query(
        self,
        namespace: str | None = None,
        name: str | None = None,
        trigger: str | None = None,
        outcome: str | None = None,
        since_hours: int = 24,
        limit: int = 100,
    ) -> list[dict]:
        clauses = ["started_at >= datetime('now', ?)"]
        params: list = [f"-{since_hours} hours"]

        if namespace:
            clauses.append("namespace = ?")
            params.append(namespace)
        if name:
            clauses.append("name = ?")
            params.append(name)
        if trigger:
            clauses.append("trigger = ?")
            params.append(trigger)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)

        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM audit_log WHERE {where} ORDER BY started_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()

        cols = [d[0] for d in self._conn.execute("PRAGMA table_info(audit_log)").fetchall()]
        return [dict(zip(cols, r, strict=False)) for r in rows]

    def summary(self, since_hours: int = 24) -> dict:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                             AS total,
                SUM(CASE WHEN outcome='executed' THEN 1 ELSE 0 END)  AS executed,
                SUM(CASE WHEN outcome='failed'   THEN 1 ELSE 0 END)  AS failed,
                SUM(CASE WHEN outcome='rejected' THEN 1 ELSE 0 END)  AS rejected,
                SUM(CASE WHEN outcome='dry_run'  THEN 1 ELSE 0 END)  AS dry_run,
                SUM(tokens_used)                                     AS tokens_used
            FROM audit_log
            WHERE started_at >= datetime('now', ?)
            """,
            [f"-{since_hours} hours"],
        ).fetchone()
        return {
            "total": row[0],
            "executed": row[1],
            "failed": row[2],
            "rejected": row[3],
            "dry_run": row[4],
            "tokens_used": row[5],
            "since_hours": since_hours,
        }
