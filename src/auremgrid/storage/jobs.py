from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from auremgrid.domain.errors import NotFoundError, ValidationError


JOB_TERMINAL_STATUSES = {"succeeded", "failed", "dead_letter", "cancelled"}
OUTBOX_TERMINAL_STATUSES = {"published"}


def _decode_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    item["result"] = json.loads(item["result"]) if item["result"] is not None else None
    item["error"] = json.loads(item["error"]) if item["error"] is not None else None
    return item


def _decode_outbox(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


class JobRepository:
    def __init__(self, conn: sqlite3.Connection, new_id: Callable[[str], str]) -> None:
        self.conn = conn
        self.new_id = new_id
        self.conn.execute("PRAGMA busy_timeout = 5000")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass

    def find_job_by_idempotency(self, organization_id: str, idempotency_key: str) -> dict[str, Any] | None:
        return _decode_job(
            self.conn.execute(
                "SELECT * FROM jobs WHERE organization_id=? AND idempotency_key=?",
                (organization_id, idempotency_key),
            ).fetchone()
        )

    def insert_job(self, job: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO jobs(
                id, organization_id, workspace_id, principal_id, type, payload, status, priority, attempts,
                max_attempts, available_at, lease_owner, lease_expires_at, progress,
                idempotency_key, payload_hash, result, error, created_at, updated_at,
                started_at, completed_at, cancelled_at, lease_token, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job["organization_id"],
                job["workspace_id"],
                job["principal_id"],
                job["type"],
                json.dumps(job["payload"], sort_keys=True),
                job["status"],
                job["priority"],
                job["attempts"],
                job["max_attempts"],
                job["available_at"],
                job["lease_owner"],
                job["lease_expires_at"],
                job["progress"],
                job["idempotency_key"],
                job["payload_hash"],
                json.dumps(job["result"], sort_keys=True) if job["result"] is not None else None,
                json.dumps(job["error"], sort_keys=True) if job["error"] is not None else None,
                job["created_at"],
                job["updated_at"],
                job["started_at"],
                job["completed_at"],
                job["cancelled_at"],
                job["lease_token"],
                job["version"],
            ),
        )
        return self.get_job(job["id"])

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError("job not found")
        item = _decode_job(row)
        assert item is not None
        return item

    def list_jobs(
        self,
        organization_id: str,
        workspace_id: str | None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        values: list[Any] = [organization_id]
        where = "organization_id=?"
        if workspace_id is None:
            where += " AND workspace_id IS NULL"
        else:
            where += " AND workspace_id=?"
            values.append(workspace_id)
        if status is not None:
            where += " AND status=?"
            values.append(status)
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC, id DESC",
            values,
        ).fetchall()
        return [item for row in rows if (item := _decode_job(row)) is not None]

    def find_claimable_job(self, organization_id: str, workspace_id: str | None, now: str) -> dict[str, Any] | None:
        values: list[Any] = [organization_id, now, now]
        where = """
            organization_id=?
            AND (
                (status IN ('queued','retry_wait') AND available_at <= ?)
                OR (status IN ('leased','running') AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
            )
        """
        if workspace_id is None:
            where += " AND workspace_id IS NULL"
        else:
            where += " AND workspace_id=?"
            values.append(workspace_id)
        row = self.conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {where}
            ORDER BY priority DESC, available_at ASC, created_at ASC
            LIMIT 1
            """,
            values,
        ).fetchone()
        return _decode_job(row)

    def claim_job(
        self,
        job: dict[str, Any],
        lease_owner: str,
        lease_token: str,
        lease_expires_at: str,
        now: str,
    ) -> dict[str, Any] | None:
        cursor = self.conn.execute(
            """
            UPDATE jobs
            SET status='leased', lease_owner=?, lease_token=?, lease_expires_at=?, attempts=attempts+1,
                started_at=COALESCE(started_at, ?), updated_at=?, version=version+1
            WHERE id=? AND version=? AND (
                (status IN ('queued','retry_wait') AND available_at <= ?)
                OR (status IN ('leased','running') AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
            )
            """,
            (lease_owner, lease_token, lease_expires_at, now, now, job["id"], job["version"], now, now),
        )
        if cursor.rowcount != 1:
            return None
        return self.get_job(job["id"])

    def update_job_status(
        self,
        job_id: str,
        from_statuses: set[str],
        expected_version: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        assignments = ["version=version+1"]
        values: list[Any] = []
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            if key in {"payload", "result", "error"}:
                values.append(json.dumps(value, sort_keys=True) if value is not None else None)
            else:
                values.append(value)
        placeholders = ",".join("?" for _ in from_statuses)
        values.extend([job_id, expected_version, *sorted(from_statuses)])
        cursor = self.conn.execute(
            f"""
            UPDATE jobs SET {', '.join(assignments)}
            WHERE id=? AND version=? AND status IN ({placeholders})
            """,
            values,
        )
        if cursor.rowcount != 1:
            raise ValidationError("job changed concurrently or is not in a valid state")
        return self.get_job(job_id)

    def insert_job_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO job_events(
                id, job_id, organization_id, workspace_id, actor, event_type,
                from_status, to_status, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                event["job_id"],
                event["organization_id"],
                event["workspace_id"],
                event["actor"],
                event["event_type"],
                event["from_status"],
                event["to_status"],
                json.dumps(event["detail"], sort_keys=True),
                event["created_at"],
            ),
        )
        return dict(
            self.conn.execute("SELECT * FROM job_events WHERE id=?", (event["id"],)).fetchone()
        )

    def list_job_events(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY created_at, rowid",
            (job_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def find_outbox_by_idempotency(self, organization_id: str, idempotency_key: str) -> dict[str, Any] | None:
        return _decode_outbox(
            self.conn.execute(
                "SELECT * FROM outbox_events WHERE organization_id=? AND idempotency_key=?",
                (organization_id, idempotency_key),
            ).fetchone()
        )

    def insert_outbox(self, event: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO outbox_events(
                id, organization_id, workspace_id, aggregate_type, aggregate_id, event_type,
                payload, payload_hash, idempotency_key, status, attempts, max_attempts, next_attempt_at,
                lease_owner, lease_expires_at, lease_token, published_at, last_error, created_at, updated_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                event["organization_id"],
                event["workspace_id"],
                event["aggregate_type"],
                event["aggregate_id"],
                event["event_type"],
                json.dumps(event["payload"], sort_keys=True),
                event["payload_hash"],
                event["idempotency_key"],
                event["status"],
                event["attempts"],
                event["max_attempts"],
                event["next_attempt_at"],
                event["lease_owner"],
                event["lease_expires_at"],
                event["lease_token"],
                event["published_at"],
                event["last_error"],
                event["created_at"],
                event["updated_at"],
                event["version"],
            ),
        )
        return self.get_outbox(event["id"])

    def get_outbox(self, event_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM outbox_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError("outbox event not found")
        item = _decode_outbox(row)
        assert item is not None
        return item

    def find_claimable_outbox(
        self,
        organization_id: str,
        workspace_id: str | None,
        now: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        values: list[Any] = [organization_id, now, now]
        where = """
            organization_id=?
            AND status IN ('pending','failed')
            AND attempts < max_attempts
            AND next_attempt_at <= ?
            AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
        """
        if workspace_id is None:
            where += " AND workspace_id IS NULL"
        else:
            where += " AND workspace_id=?"
            values.append(workspace_id)
        rows = self.conn.execute(
            f"SELECT * FROM outbox_events WHERE {where} ORDER BY created_at ASC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [item for row in rows if (item := _decode_outbox(row)) is not None]

    def claim_outbox(
        self,
        event: dict[str, Any],
        lease_owner: str,
        lease_token: str,
        lease_expires_at: str,
        now: str,
    ) -> dict[str, Any] | None:
        cursor = self.conn.execute(
            """
            UPDATE outbox_events
            SET status='pending', attempts=attempts+1, lease_owner=?, lease_token=?, lease_expires_at=?,
                updated_at=?, version=version+1
            WHERE id=? AND version=? AND status IN ('pending','failed')
              AND attempts < max_attempts
              AND next_attempt_at <= ?
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """,
            (lease_owner, lease_token, lease_expires_at, now, event["id"], event["version"], now, now),
        )
        if cursor.rowcount != 1:
            return None
        return self.get_outbox(event["id"])

    def update_outbox(
        self,
        event_id: str,
        owner: str,
        expected_version: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        assignments = ["version=version+1"]
        values: list[Any] = []
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            values.append(json.dumps(value, sort_keys=True) if key == "payload" else value)
        values.extend([event_id, owner, expected_version])
        cursor = self.conn.execute(
            f"""
            UPDATE outbox_events SET {', '.join(assignments)}
            WHERE id=? AND lease_owner=? AND version=?
            """,
            values,
        )
        if cursor.rowcount != 1:
            raise ValidationError("outbox event changed concurrently or is not leased by this owner")
        return self.get_outbox(event_id)
