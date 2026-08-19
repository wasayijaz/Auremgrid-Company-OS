from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from auremgrid.domain.errors import NotFoundError, ValidationError


TERMINAL_STATUSES = {"completed", "cancelled"}


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class WorkflowRepository:
    def __init__(self, conn: sqlite3.Connection, new_id: Callable[[str], str]) -> None:
        self.conn = conn
        self.new_id = new_id

    def get_idempotency(self, organization_id: str, key: str, operation: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM workflow_idempotency_keys WHERE organization_id=? AND key=? AND operation=?",
            (organization_id, key, operation),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["response"] = json.loads(result["response"])
        return result

    def save_idempotency(
        self,
        organization_id: str,
        key: str,
        operation: str,
        result_type: str,
        result_id: str,
        response: dict[str, Any],
        now: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO workflow_idempotency_keys(
                organization_id, key, operation, result_type, result_id, response, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (organization_id, key, operation, result_type, result_id, json.dumps(response, sort_keys=True), now),
        )

    def definition_by_key(self, organization_id: str, key: str) -> dict[str, Any] | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM workflow_definitions WHERE organization_id=? AND key=?",
                (organization_id, key),
            ).fetchone()
        )

    def definition_version(self, definition_id: str, version: str) -> dict[str, Any] | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM workflow_definition_versions WHERE definition_id=? AND version=?",
                (definition_id, version),
            ).fetchone()
        )

    def save_definition_version(
        self,
        organization_id: str,
        key: str,
        name: str,
        version: str,
        snapshot: dict[str, Any],
        created_by_person_id: str,
        now: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        definition = self.definition_by_key(organization_id, key)
        if definition is None:
            definition_id = self.new_id("wdef")
            self.conn.execute(
                """
                INSERT INTO workflow_definitions(id, organization_id, key, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (definition_id, organization_id, key, name, now, now),
            )
            definition = self.definition_by_key(organization_id, key)
        else:
            self.conn.execute(
                "UPDATE workflow_definitions SET name=?, updated_at=? WHERE id=?",
                (name, now, definition["id"]),
            )
            definition = self.definition_by_key(organization_id, key)

        assert definition is not None
        existing_version = self.definition_version(definition["id"], version)
        snapshot_text = json.dumps(snapshot, sort_keys=True)
        if existing_version is not None:
            if existing_version["snapshot"] != snapshot_text:
                raise ValidationError("workflow definition version already exists with a different snapshot")
            return definition, existing_version

        version_id = self.new_id("wdefver")
        self.conn.execute(
            """
            INSERT INTO workflow_definition_versions(
                id, definition_id, version, snapshot, created_by_person_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (version_id, definition["id"], version, snapshot_text, created_by_person_id, now),
        )
        for stage in snapshot["stages"]:
            self.conn.execute(
                """
                INSERT INTO workflow_definition_steps(
                    id, definition_version_id, step_key, name, sequence, assignee_wing, assignee_role,
                    required_evidence, requires_approval, handoff_contract, on_reject_step_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.new_id("wdefstep"),
                    version_id,
                    stage["key"],
                    stage["name"],
                    stage["sequence"],
                    stage["assignee_wing"],
                    stage["assignee_role"],
                    json.dumps(stage["required_evidence"]),
                    int(stage["requires_approval"]),
                    stage["handoff_contract"],
                    stage["on_reject_stage_key"],
                ),
            )
        for edge in snapshot["edges"]:
            self.conn.execute(
                """
                INSERT INTO workflow_definition_edges(
                    id, definition_version_id, from_step_key, to_step_key, kind
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (self.new_id("wdefedge"), version_id, edge["from"], edge["to"], edge["kind"]),
            )
        version_row = self.definition_version(definition["id"], version)
        assert version_row is not None
        return definition, version_row

    def create_run(
        self,
        run: dict[str, Any],
        stages: list[dict[str, Any]],
        dependencies: list[dict[str, Any]],
        history: dict[str, Any],
    ) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO workflow_runs(
                id, organization_id, workspace_id, definition_id, definition_version_id,
                definition_key, definition_version, definition_name, template_snapshot, status,
                created_by_person_id, idempotency_key, due_at, sla_minutes, escalation_at,
                blocked_reason, created_at, updated_at, started_at, completed_at, cancelled_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["id"],
                run["organization_id"],
                run["workspace_id"],
                run["definition_id"],
                run["definition_version_id"],
                run["definition_key"],
                run["definition_version"],
                run["definition_name"],
                json.dumps(run["template_snapshot"], sort_keys=True),
                run["status"],
                run["created_by_person_id"],
                run["idempotency_key"],
                run["due_at"],
                run["sla_minutes"],
                run["escalation_at"],
                run["blocked_reason"],
                run["created_at"],
                run["updated_at"],
                run["started_at"],
                run["completed_at"],
                run["cancelled_at"],
                run["version"],
            ),
        )
        for stage in stages:
            self.conn.execute(
                """
                INSERT INTO workflow_stage_runs(
                    id, run_id, stage_key, name, sequence, status, assignee_wing, assignee_role,
                    assignee_person_id, required_evidence, requires_approval, handoff_to_wing,
                    handoff_to_role, handoff_to_person_id, on_reject_stage_key, due_at, blocked_reason, created_at,
                    updated_at, started_at, completed_at, cancelled_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage["id"],
                    stage["run_id"],
                    stage["stage_key"],
                    stage["name"],
                    stage["sequence"],
                    stage["status"],
                    stage["assignee_wing"],
                    stage["assignee_role"],
                    stage["assignee_person_id"],
                    json.dumps(stage["required_evidence"]),
                    int(stage["requires_approval"]),
                    stage["handoff_to_wing"],
                    stage["handoff_to_role"],
                    stage["handoff_to_person_id"],
                    stage["on_reject_stage_key"],
                    stage["due_at"],
                    stage["blocked_reason"],
                    stage["created_at"],
                    stage["updated_at"],
                    stage["started_at"],
                    stage["completed_at"],
                    stage["cancelled_at"],
                    stage["version"],
                ),
            )
        for dependency in dependencies:
            self.conn.execute(
                """
                INSERT INTO workflow_stage_dependencies(
                    run_id, stage_run_id, depends_on_stage_run_id, kind, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    dependency["run_id"],
                    dependency["stage_run_id"],
                    dependency["depends_on_stage_run_id"],
                    dependency["kind"],
                    dependency["created_at"],
                ),
            )
        self.record_history(history)
        return self.get_run(run["id"])

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("workflow run not found")
        result = dict(row)
        result["template_snapshot"] = json.loads(result["template_snapshot"])
        return result

    def get_stage(self, stage_run_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM workflow_stage_runs WHERE id=?", (stage_run_id,)).fetchone()
        if row is None:
            raise NotFoundError("workflow stage run not found")
        result = dict(row)
        result["required_evidence"] = json.loads(result["required_evidence"])
        result["requires_approval"] = bool(result["requires_approval"])
        return result

    def get_stage_by_key(self, run_id: str, stage_key: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM workflow_stage_runs WHERE run_id=? AND stage_key=?",
            (run_id, stage_key),
        ).fetchone()
        if row is None:
            raise NotFoundError("workflow stage run not found")
        return self.get_stage(row["id"])

    def list_stages(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM workflow_stage_runs WHERE run_id=? ORDER BY sequence, stage_key",
            (run_id,),
        ).fetchall()
        stages = []
        for row in rows:
            stage = dict(row)
            stage["required_evidence"] = json.loads(stage["required_evidence"])
            stage["requires_approval"] = bool(stage["requires_approval"])
            stages.append(stage)
        return stages

    def dependencies_for_stage(self, stage_run_id: str) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT d.*, s.status AS dependency_status, s.handoff_to_wing, s.handoff_to_role,
                       s.handoff_to_person_id, s.assignee_wing AS dependency_wing,
                       s.assignee_role AS dependency_role, s.assignee_person_id AS dependency_person_id
                FROM workflow_stage_dependencies d
                JOIN workflow_stage_runs s ON s.id=d.depends_on_stage_run_id
                WHERE d.stage_run_id=?
                """,
                (stage_run_id,),
            ).fetchall()
        )

    def update_stage_status(
        self,
        stage_run_id: str,
        from_status: str,
        to_status: str,
        now: str,
        expected_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        assignments = ["status=?", "updated_at=?", "version=version+1"]
        values: list[Any] = [to_status, now]
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(value)
        values.extend([stage_run_id, from_status, expected_version])
        cursor = self.conn.execute(
            f"""
            UPDATE workflow_stage_runs SET {', '.join(assignments)}
            WHERE id=? AND status=? AND version=?
            """,
            values,
        )
        if cursor.rowcount != 1:
            raise ValidationError("workflow stage changed concurrently")
        return self.get_stage(stage_run_id)

    def update_run_status(
        self,
        run_id: str,
        from_status: str,
        to_status: str,
        now: str,
        expected_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        assignments = ["status=?", "updated_at=?", "version=version+1"]
        values: list[Any] = [to_status, now]
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(value)
        values.extend([run_id, from_status, expected_version])
        cursor = self.conn.execute(
            f"UPDATE workflow_runs SET {', '.join(assignments)} WHERE id=? AND status=? AND version=?",
            values,
        )
        if cursor.rowcount != 1:
            raise ValidationError("workflow run changed concurrently")
        return self.get_run(run_id)

    def add_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO workflow_evidence(
                id, run_id, stage_run_id, kind, uri, text, metadata, object_type, object_id,
                locator, content_hash, submitted_by_person_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence["id"],
                evidence["run_id"],
                evidence["stage_run_id"],
                evidence["kind"],
                evidence["uri"],
                evidence["text"],
                json.dumps(evidence["metadata"], sort_keys=True),
                evidence["object_type"],
                evidence["object_id"],
                evidence["locator"],
                evidence["content_hash"],
                evidence["submitted_by_person_id"],
                evidence["created_at"],
            ),
        )
        return self.get_evidence(evidence["id"])

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM workflow_evidence WHERE id=?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError("workflow evidence not found")
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def list_evidence(self, stage_run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM workflow_evidence WHERE stage_run_id=? ORDER BY created_at",
            (stage_run_id,),
        ).fetchall()
        evidence = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            evidence.append(item)
        return evidence

    def add_approval_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO workflow_approval_decisions(
                id, run_id, stage_run_id, approval_request_id, decision, approver_person_id, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision["id"],
                decision["run_id"],
                decision["stage_run_id"],
                decision["approval_request_id"],
                decision["decision"],
                decision["approver_person_id"],
                decision["reason"],
                decision["created_at"],
            ),
        )
        return dict(
            self.conn.execute(
                "SELECT * FROM workflow_approval_decisions WHERE id=?",
                (decision["id"],),
            ).fetchone()
        )

    def latest_approval_decision(self, stage_run_id: str) -> dict[str, Any] | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM workflow_approval_decisions WHERE stage_run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (stage_run_id,),
            ).fetchone()
        )

    def approval_request_exists(self, approval_request_id: str) -> bool:
        row = self.conn.execute("SELECT id FROM approval_requests WHERE id=?", (approval_request_id,)).fetchone()
        return row is not None

    def get_approval_request(self, approval_request_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_request_id,)).fetchone()
        if row is None:
            raise NotFoundError("approval request not found")
        return dict(row)

    def add_handoff_ack(self, acknowledgement: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO workflow_handoff_acknowledgements(
                id, run_id, from_stage_run_id, to_stage_run_id, acknowledged_by_person_id,
                from_wing, from_role, from_person_id, source_stage_version, to_wing, to_role, to_person_id,
                artifact_contract, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                acknowledgement["id"],
                acknowledgement["run_id"],
                acknowledgement["from_stage_run_id"],
                acknowledgement["to_stage_run_id"],
                acknowledgement["acknowledged_by_person_id"],
                acknowledgement["from_wing"],
                acknowledgement["from_role"],
                acknowledgement["from_person_id"],
                acknowledgement["source_stage_version"],
                acknowledgement["to_wing"],
                acknowledgement["to_role"],
                acknowledgement["to_person_id"],
                acknowledgement["artifact_contract"],
                acknowledgement["reason"],
                acknowledgement["created_at"],
            ),
        )
        return dict(
            self.conn.execute(
                "SELECT * FROM workflow_handoff_acknowledgements WHERE id=?",
                (acknowledgement["id"],),
            ).fetchone()
        )

    def has_handoff_ack(self, from_stage_run_id: str, to_stage_run_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT h.id FROM workflow_handoff_acknowledgements h
            JOIN workflow_stage_runs source ON source.id=h.from_stage_run_id
            WHERE h.from_stage_run_id=? AND h.to_stage_run_id=?
              AND source.completed_at IS NOT NULL AND h.source_stage_version=source.version
            LIMIT 1
            """,
            (from_stage_run_id, to_stage_run_id),
        ).fetchone()
        return row is not None

    def record_history(self, item: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO workflow_transition_history(
                id, run_id, stage_run_id, actor_person_id, action, from_status, to_status,
                reason, metadata, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["run_id"],
                item["stage_run_id"],
                item["actor_person_id"],
                item["action"],
                item["from_status"],
                item["to_status"],
                item["reason"],
                json.dumps(item["metadata"], sort_keys=True),
                item["idempotency_key"],
                item["created_at"],
            ),
        )
        return dict(
            self.conn.execute(
                "SELECT * FROM workflow_transition_history WHERE id=?",
                (item["id"],),
            ).fetchone()
        )

    def history(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM workflow_transition_history WHERE run_id=? ORDER BY created_at, rowid",
            (run_id,),
        ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            history.append(item)
        return history

    def overdue(self, organization_id: str, workspace_id: str | None, as_of: str) -> dict[str, list[dict[str, Any]]]:
        run_values: list[Any] = [organization_id, as_of, as_of]
        run_where = "organization_id=? AND status NOT IN ('completed','cancelled') AND (due_at < ? OR escalation_at < ?)"
        stage_values: list[Any] = [organization_id, as_of]
        stage_where = """
            r.organization_id=? AND s.status NOT IN ('completed','cancelled')
            AND (s.due_at < ? OR (s.due_at IS NULL AND r.escalation_at < ?))
        """
        stage_values.append(as_of)
        if workspace_id is not None:
            run_where += " AND workspace_id=?"
            run_values.append(workspace_id)
            stage_where += " AND r.workspace_id=?"
            stage_values.append(workspace_id)
        runs = rows_to_dicts(
            self.conn.execute(
                f"SELECT * FROM workflow_runs WHERE {run_where} ORDER BY COALESCE(escalation_at, due_at), due_at",
                run_values,
            ).fetchall()
        )
        stages = rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT s.*, r.organization_id, r.workspace_id
                FROM workflow_stage_runs s
                JOIN workflow_runs r ON r.id=s.run_id
                WHERE {stage_where}
                ORDER BY COALESCE(s.due_at, r.escalation_at), s.sequence
                """,
                stage_values,
            ).fetchall()
        )
        return {"runs": runs, "stages": stages}
