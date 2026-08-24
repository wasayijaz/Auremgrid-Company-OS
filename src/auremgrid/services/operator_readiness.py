from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.secrets import redact


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


class OperatorReadinessService:
    """Read-only operator evidence for supervised local production runs."""

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

    def supervised_operations_export(
        self,
        organization_id: str,
        person_id: str,
        workspace_id: str | None = None,
        *,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Return a bounded, redacted packet for agent and automation review."""

        self._authorize_operator(organization_id, person_id, workspace_id)
        bounded_limit = self._limit(limit)
        agent_workspace_clause, workspace_values = self._workspace_filter("ar.workspace_id", workspace_id)
        action_workspace_clause, action_workspace_values = self._workspace_filter("workspace_id", workspace_id)
        agent_runs = self._agent_runs(organization_id, agent_workspace_clause, workspace_values, bounded_limit)
        automation_runs = self._automation_runs(organization_id, workspace_id, bounded_limit)
        packet = {
            "_meta": {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "exported_at": _now(),
                "limit": bounded_limit,
                "redacted": True,
            },
            "scheduler": {
                "heartbeats": self._scheduler_heartbeats(organization_id, workspace_id),
                "controls": self._scheduler_controls(organization_id, workspace_id),
            },
            "agents": {
                "runs": agent_runs,
                "action_executions": self._agent_action_executions(
                    organization_id, action_workspace_clause, action_workspace_values, bounded_limit
                ),
                "status_counts": dict(Counter(str(run["status"]) for run in agent_runs)),
            },
            "automations": {
                "definitions": self._automations(organization_id, bounded_limit),
                "runs": automation_runs,
                "status_counts": dict(Counter(str(run["status"]) for run in automation_runs)),
            },
            "jobs": self._job_summary(organization_id, workspace_id),
            "circuit_breakers": self._circuit_breakers(organization_id, bounded_limit),
        }
        return redact(packet)

    def readiness_report(
        self,
        organization_id: str,
        person_id: str,
        workspace_id: str | None = None,
        *,
        limit: int = 25,
    ) -> dict[str, Any]:
        packet = self.supervised_operations_export(organization_id, person_id, workspace_id, limit=limit)
        checks: list[dict[str, Any]] = []
        heartbeats = packet["scheduler"]["heartbeats"]
        checks.append(
            {
                "id": "scheduler_heartbeat_present",
                "status": "pass" if heartbeats else "warn",
                "detail": "at least one scoped worker heartbeat exists" if heartbeats else "no scoped worker heartbeat has been recorded",
            }
        )
        degraded = [row for row in heartbeats if row.get("status") == "degraded"]
        checks.append(
            {
                "id": "scheduler_not_degraded",
                "status": "pass" if not degraded else "fail",
                "detail": f"{len(degraded)} degraded scheduler heartbeat(s)",
            }
        )
        action_counts = packet["agents"]["status_counts"]
        checks.append(
            {
                "id": "agent_runs_not_failed",
                "status": "pass" if int(action_counts.get("failed", 0)) == 0 else "warn",
                "detail": f"{action_counts.get('failed', 0)} failed recent agent run(s)",
            }
        )
        waiting = packet["automations"]["status_counts"].get("waiting_approval", 0)
        checks.append(
            {
                "id": "automation_training_checkpoints_visible",
                "status": "pass" if waiting else "warn",
                "detail": f"{waiting} automation run(s) waiting for approval in the export",
            }
        )
        return {
            "_meta": packet["_meta"],
            "status": "ready_with_warnings" if any(check["status"] == "warn" for check in checks) else "ready",
            "checks": checks,
            "export": packet,
        }

    def postgres_portability_assessment(self, *, finding_limit: int = 50) -> dict[str, Any]:
        """Assess known SQLite-specific assumptions without opening a Postgres connection."""

        from auremgrid.storage.migrations import MIGRATIONS
        from auremgrid.storage.sqlite import SCHEMA

        rules = (
            ("sqlite_pragma", "PRAGMA ", "blocker", "SQLite PRAGMA settings need Postgres connection/session equivalents."),
            ("sqlite_virtual_table_fts5", "CREATE VIRTUAL TABLE", "blocker", "SQLite FTS5 virtual tables need a Postgres full-text/vector design."),
            ("sqlite_trigger_raise", "RAISE(ABORT", "blocker", "SQLite trigger RAISE syntax must be rewritten as PL/pgSQL."),
            ("sqlite_randomblob", "randomblob(", "blocker", "SQLite randomblob ids in triggers need a Postgres-safe generator."),
            ("sqlite_begin_immediate", "BEGIN IMMEDIATE", "blocker", "SQLite immediate transactions need Postgres lock/transaction semantics."),
            ("sqlite_table_info", "PRAGMA table_info", "warning", "Migration replay checks use SQLite catalog APIs."),
        )
        scans: list[tuple[str, str]] = [("sqlite bootstrap schema", SCHEMA)]
        scans.extend((f"migration {migration.version}: {migration.name}", migration.sql) for migration in MIGRATIONS)
        findings: list[dict[str, Any]] = [
            {
                "id": "companyos_sqlite_store_runtime",
                "severity": "blocker",
                "source": "CompanyOS.__init__",
                "detail": "CompanyOS constructs SqliteStore directly, so Postgres is not yet selectable through the local runtime.",
            },
            {
                "id": "postgres_migration_runner_missing",
                "severity": "blocker",
                "source": "PostgresStore",
                "detail": "PostgresStore exposes a connection wrapper but does not run a Postgres dialect migration set.",
            },
        ]
        seen = {(finding["id"], finding["source"]) for finding in findings}
        for source, sql in scans:
            upper = sql.upper()
            for finding_id, marker, severity, detail in rules:
                if marker.upper() not in upper:
                    continue
                key = (finding_id, source)
                if key in seen:
                    continue
                findings.append({"id": finding_id, "severity": severity, "source": source, "detail": detail})
                seen.add(key)
        counts = Counter(str(item["severity"]) for item in findings)
        bounded = findings[: self._limit(finding_limit)]
        return {
            "status": "not_ready" if counts.get("blocker", 0) else "ready_for_adapter_tests",
            "target": "postgres",
            "source_schema_version": self.os.store.schema_version,
            "summary": {
                "blockers": int(counts.get("blocker", 0)),
                "warnings": int(counts.get("warning", 0)),
                "findings_total": len(findings),
                "findings_returned": len(bounded),
            },
            "findings": bounded,
            "required_work": [
                "Make storage backend selection explicit in CompanyOS and operator configuration.",
                "Create a Postgres dialect migration path for schema, triggers, FTS/search, ids, and catalog replay checks.",
                "Run parity tests for supervised agents, automations, jobs, approvals, and exports against the Postgres backend.",
            ],
            "opened_connections": False,
        }

    def _authorize_operator(self, organization_id: str, person_id: str, workspace_id: str | None) -> None:
        membership = self.os.company.org_membership(organization_id, person_id)
        if membership is None or membership.role not in {"owner", "admin"}:
            raise AuthorizationError("organization admin required")
        if workspace_id is not None:
            scope = self.os.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != organization_id:
                raise ValidationError("workspace must belong to organization")

    @staticmethod
    def _limit(value: int) -> int:
        if value < 1:
            raise ValidationError("limit must be positive")
        return min(int(value), 100)

    @staticmethod
    def _workspace_filter(column: str, workspace_id: str | None) -> tuple[str, list[Any]]:
        if workspace_id is None:
            return "1=1", []
        return f"{column}=?", [workspace_id]

    def _agent_runs(self, organization_id: str, workspace_clause: str, values: list[Any], limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"""SELECT ar.*,a.name AS agent_name,t.title AS task_title,t.status AS task_status,
                       (SELECT COUNT(*) FROM run_traces rt WHERE rt.run_id=ar.id) AS trace_count,
                       (SELECT COUNT(*) FROM tool_calls tc WHERE tc.run_id=ar.id) AS tool_call_count
                FROM agent_runs ar
                JOIN agents a ON a.id=ar.agent_id
                LEFT JOIN agent_tasks t ON t.id=ar.task_id
                WHERE ar.organization_id=? AND {workspace_clause}
                ORDER BY ar.started_at DESC, ar.id DESC
                LIMIT ?""",
            (organization_id, *values, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            output = self.conn.execute("SELECT kind,content,source_refs,created_at FROM run_outputs WHERE run_id=?", (item["id"],)).fetchone()
            error = self.conn.execute("SELECT kind,message,detail,retryable,created_at FROM run_errors WHERE run_id=?", (item["id"],)).fetchone()
            item["output"] = self._decoded_output(output)
            item["error"] = redact(_row(error)) if error else None
            item["traces"] = self._run_traces(item["id"], limit=10)
            item["tool_calls"] = self._run_tool_calls(item["id"], limit=10)
            result.append(item)
        return result

    def _run_traces(self, run_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id,run_id,sequence,kind,message,metadata,recorded_at
               FROM run_traces
               WHERE run_id=?
               ORDER BY sequence ASC
               LIMIT ?""",
            (run_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            item["metadata"] = _loads(item.get("metadata"), {})
            result.append(item)
        return result

    def _run_tool_calls(self, run_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id,run_id,tool_name,arguments,status,started_at,completed_at,result_preview,error
               FROM tool_calls
               WHERE run_id=?
               ORDER BY started_at ASC,id ASC
               LIMIT ?""",
            (run_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            item["arguments"] = _loads(item.get("arguments"), {})
            result.append(redact(item))
        return result

    def _agent_action_executions(self, organization_id: str, workspace_clause: str, values: list[Any], limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"""SELECT id,organization_id,workspace_id,agent_id,run_id,task_id,approval_request_id,
                       action,action_kind,idempotency_key,descriptor_hash,payload_hash,status,
                       result_json,error_json,created_at,completed_at
                FROM agent_action_executions
                WHERE organization_id=? AND {workspace_clause}
                ORDER BY created_at DESC,id DESC
                LIMIT ?""",
            (organization_id, *values, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            item["result"] = _loads(item.pop("result_json"), None)
            item["error"] = _loads(item.pop("error_json"), None)
            result.append(item)
        return result

    def _scheduler_heartbeats(self, organization_id: str, workspace_id: str | None) -> list[dict[str, Any]]:
        clause, values = self._workspace_filter("workspace_id", workspace_id)
        rows = self.conn.execute(
            f"""SELECT * FROM scheduler_heartbeats
                WHERE organization_id=? AND {clause}
                ORDER BY updated_at DESC, worker_id ASC""",
            (organization_id, *values),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            item["last_result"] = _loads(item.get("last_result"), None)
            result.append(item)
        return result

    def _scheduler_controls(self, organization_id: str, workspace_id: str | None) -> list[dict[str, Any]]:
        clause, values = self._workspace_filter("workspace_id", workspace_id)
        rows = self.conn.execute(
            f"""SELECT * FROM scheduler_controls
                WHERE organization_id=? AND {clause}
                ORDER BY updated_at DESC, scope_key ASC""",
            (organization_id, *values),
        ).fetchall()
        return [_row(row) for row in rows]

    def _automations(self, organization_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT a.*,
                      (SELECT COUNT(*) FROM automation_actions aa WHERE aa.automation_id=a.id) AS action_count,
                      (SELECT GROUP_CONCAT(type) FROM automation_triggers at WHERE at.automation_id=a.id) AS trigger_types
               FROM automations a
               WHERE a.organization_id=?
               ORDER BY a.created_at DESC,a.id DESC
               LIMIT ?""",
            (organization_id, limit),
        ).fetchall()
        return [_row(row) for row in rows]

    def _automation_runs(self, organization_id: str, workspace_id: str | None, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT ar.*,a.organization_id,a.name AS automation_name,a.approval_policy
                FROM automation_runs ar
                JOIN automations a ON a.id=ar.automation_id
                WHERE a.organization_id=?
                ORDER BY ar.started_at DESC, ar.id DESC
                LIMIT ?""",
            (organization_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = _row(row)
            item["trigger_payload"] = _loads(item.get("trigger_payload"), {})
            item["output"] = _loads(item.get("output"), {})
            if workspace_id is not None and item["trigger_payload"].get("workspace_id") != workspace_id:
                continue
            result.append(item)
        return result

    def _job_summary(self, organization_id: str, workspace_id: str | None) -> dict[str, Any]:
        clause, values = self._workspace_filter("workspace_id", workspace_id)
        rows = self.conn.execute(
            f"""SELECT type,status,COUNT(*) AS count
                FROM jobs
                WHERE organization_id=? AND {clause}
                GROUP BY type,status
                ORDER BY type,status""",
            (organization_id, *values),
        ).fetchall()
        return {"counts": [_row(row) for row in rows]}

    def _circuit_breakers(self, organization_id: str, limit: int) -> dict[str, Any]:
        policies = [
            _row(row)
            for row in self.conn.execute(
                """SELECT organization_id,task_class,max_runtime_ms,max_cost_amount,max_tokens,
                          breaker_threshold,breaker_window_seconds,breaker_open_seconds,
                          failure_count,breaker_open_until,updated_at
                   FROM intelligence_evaluation_policies
                   WHERE organization_id=?
                   ORDER BY updated_at DESC,task_class
                   LIMIT ?""",
                (organization_id, limit),
            ).fetchall()
        ]
        events = [
            _row(row)
            for row in self.conn.execute(
                """SELECT id,organization_id,task_class,event_type,evaluation_id,detail,created_at
                   FROM intelligence_evaluation_circuit_events
                   WHERE organization_id=?
                   ORDER BY created_at DESC,id DESC
                   LIMIT ?""",
                (organization_id, limit),
            ).fetchall()
        ]
        return {"policies": policies, "events": events}

    @staticmethod
    def _decoded_output(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        item = _row(row)
        item["source_refs"] = _loads(item.get("source_refs"), [])
        return redact(item)
