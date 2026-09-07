"""Automation triggers, approvals, actions execution, and reconciliation mixin."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.reversible_actions import (
    ACTION_KINDS,
    validate_reversible_action_descriptor,
)
from auremgrid.services.agent_ops_shared import (
    _action_operator_next_step,
    _json,
    _loads,
    _now,
    _optional_text,
    _stable_hash,
)


class AgentOpsAutomationsMixin:
    def create_automation(
        self,
        organization_id: str,
        person_id: str,
        name: str,
        trigger_type: str,
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        approval_policy: str = "human",
    ) -> dict[str, Any]:
        membership = self.company.org_membership(organization_id, person_id)
        if membership is None:
            raise AuthorizationError("organization membership required")
        if approval_policy not in {"auto", "human", "admin_only"}:
            raise ValidationError("invalid approval policy")
        automation = {
            "id": self.new_id("automation"),
            "organization_id": organization_id,
            "name": name,
            "description": "",
            "status": "training",
            "approval_policy": approval_policy,
            "created_by_person_id": person_id,
            "created_at": _now().isoformat(),
        }
        self.conn.execute("INSERT INTO automations VALUES (?,?,?,?,?,?,?,?)", tuple(automation.values()))
        self.conn.execute("INSERT INTO automation_triggers VALUES (?,?,?,?)", (self.new_id("trigger"), automation["id"], trigger_type, "{}"))
        for sequence, condition in enumerate(conditions):
            self.conn.execute(
                "INSERT INTO automation_conditions VALUES (?,?,?,?,?,?)",
                (self.new_id("condition"), automation["id"], condition["field"], condition["operator"], _json(condition["value"]), sequence),
            )
        for sequence, action in enumerate(actions):
            self.conn.execute(
                "INSERT INTO automation_actions VALUES (?,?,?,?,?,?)",
                (self.new_id("action"), automation["id"], action["type"], _json(action.get("config", {})), sequence, int(action.get("one_way", False))),
            )
        self.conn.commit()
        return automation

    def trigger_automations(self, organization_id: str, trigger_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT a.* FROM automations a JOIN automation_triggers t ON t.automation_id=a.id
            WHERE a.organization_id=? AND a.status IN ('training','active') AND t.type=?""",
            (organization_id, trigger_type),
        ).fetchall()
        results = []
        for automation in rows:
            conditions = self.conn.execute(
                "SELECT * FROM automation_conditions WHERE automation_id=? ORDER BY sequence",
                (automation["id"],),
            ).fetchall()
            if not all(self._condition(payload, condition) for condition in conditions):
                continue
            actions = self.conn.execute(
                "SELECT * FROM automation_actions WHERE automation_id=? ORDER BY sequence",
                (automation["id"],),
            ).fetchall()
            blocked_reason = None
            try:
                descriptors = self._automation_action_descriptors(organization_id, automation, actions, payload)
            except ValidationError as exc:
                descriptors = []
                blocked_reason = str(exc)
            if not descriptors and blocked_reason is None:
                continue
            workspace_id = self._automation_workspace_id(descriptors) if descriptors else _optional_text(payload.get("workspace_id"))
            fingerprint_subject = descriptors or [
                {"type": row["type"], "one_way": bool(row["one_way"]), "config": _loads(row["config"], {})}
                for row in actions
            ]
            fingerprint = self._automation_change_fingerprint(automation, trigger_type, payload, fingerprint_subject, workspace_id)
            existing = self._find_automation_run_by_fingerprint(automation["id"], fingerprint)
            if existing is not None:
                results.append(self._automation_run_response(existing, True))
                continue
            run_id = self.new_id("automationrun")
            now = _now().isoformat()
            approval_payload = {
                "trigger_type": trigger_type,
                "trigger_payload": payload,
                "actions": descriptors,
                "change_fingerprint": fingerprint,
            }
            if blocked_reason:
                approval_payload["blocked_reason"] = blocked_reason
            auto_execute = (
                automation["status"] == "active"
                and automation["approval_policy"] == "auto"
                and bool(descriptors)
                and blocked_reason is None
            )
            principal = None
            if auto_execute:
                principal = self.conn.execute(
                    "SELECT id FROM auth_principals WHERE organization_id=? AND person_id=? AND status='active' LIMIT 1",
                    (organization_id, automation["created_by_person_id"]),
                ).fetchone()
                if principal is None:
                    raise AuthorizationError("active principal required for automation execution")
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing = self._find_automation_run_by_fingerprint(automation["id"], fingerprint)
                if existing is not None:
                    self.conn.commit()
                    results.append(self._automation_run_response(existing, True))
                    continue
                approval = self._insert_automation_approval(
                    organization_id,
                    automation,
                    workspace_id,
                    approval_payload,
                    now,
                    auto_approved=auto_execute,
                )
                self.conn.execute(
                    """INSERT INTO automation_runs(
                        id,automation_id,trigger_type,trigger_payload,status,started_at,completed_at,
                        approval_request_id,output,workspace_id,job_id,change_fingerprint,action_descriptor_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        automation["id"],
                        trigger_type,
                        _json(payload),
                        "queued" if auto_execute else "waiting_approval",
                        now,
                        None,
                        approval["id"],
                        _json({"blocked_reason": blocked_reason} if blocked_reason else {}),
                        workspace_id,
                        None,
                        fingerprint,
                        _json(descriptors),
                    ),
                )
                job = None
                if auto_execute and principal is not None:
                    from auremgrid.services.job_ops import JobOperations

                    job_payload = {
                        "run_id": run_id,
                        "automation_id": automation["id"],
                        "actions": descriptors,
                        "change_fingerprint": fingerprint,
                    }

                    def attach_job(item: dict[str, Any]) -> None:
                        self.conn.execute("UPDATE automation_runs SET status='queued',job_id=? WHERE id=?", (item["id"], run_id))

                    job = JobOperations(self.conn, self.new_id).enqueue_job(
                        organization_id,
                        workspace_id,
                        principal["id"],
                        "automation.execute",
                        job_payload,
                        idempotency_key=f"automation:{organization_id}:{workspace_id or 'organization'}:{fingerprint}",
                        transaction_hook=attach_job,
                        manage_transaction=False,
                    )
                self.conn.commit()
                latest = self.conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
                response = self._automation_run_response(latest, False)
                if job is not None:
                    response["job_id"] = job["id"]
                results.append(response)
            except sqlite3.IntegrityError:
                self.conn.rollback()
                existing = self._find_automation_run_by_fingerprint(automation["id"], fingerprint)
                if existing is None:
                    raise
                results.append(self._automation_run_response(existing, True))
            except Exception:
                self.conn.rollback()
                raise
        return results

    def activate_automation(self, organization_id: str, person_id: str, automation_id: str) -> dict[str, Any]:
        membership = self.company.org_membership(organization_id, person_id)
        if membership is None or membership.role not in {"owner", "admin"}:
            raise AuthorizationError("organization admin required")
        automation = self.conn.execute(
            "SELECT * FROM automations WHERE organization_id=? AND id=?",
            (organization_id, automation_id),
        ).fetchone()
        if automation is None:
            raise NotFoundError("automation not found")
        approved = self.conn.execute(
            """SELECT 1 FROM automation_runs ar JOIN approval_requests ap ON ap.id=ar.approval_request_id
            WHERE ar.automation_id=? AND ap.status='approved' LIMIT 1""",
            (automation_id,),
        ).fetchone()
        if approved is None:
            raise ValidationError("automation needs an approved training run before activation")
        self.conn.execute("UPDATE automations SET status='active' WHERE id=?", (automation_id,))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone())

    def execute_approved_automation_run(self, organization_id: str, person_id: str, run_id: str) -> dict[str, Any]:
        membership = self.company.org_membership(organization_id, person_id)
        if membership is None or membership.role not in {"owner", "admin"}:
            raise AuthorizationError("organization admin required")
        run = self.conn.execute(
            """SELECT ar.*,a.created_by_person_id,a.organization_id
            FROM automation_runs ar JOIN automations a ON a.id=ar.automation_id
            WHERE a.organization_id=? AND ar.id=?""",
            (organization_id, run_id),
        ).fetchone()
        if run is None:
            raise NotFoundError("automation run not found")
        approval = self.conn.execute("SELECT status FROM approval_requests WHERE id=?", (run["approval_request_id"],)).fetchone()
        if approval is None or approval["status"] != "approved":
            raise AuthorizationError("approved automation run required")
        if run["status"] in {"queued", "running", "completed"} and run["job_id"]:
            return dict(run)
        if run["status"] != "waiting_approval":
            raise AuthorizationError("approved automation run required")
        principal = self.conn.execute(
            "SELECT id FROM auth_principals WHERE organization_id=? AND person_id=? AND status='active' LIMIT 1",
            (organization_id, person_id),
        ).fetchone()
        if principal is None:
            raise AuthorizationError("active principal required for automation execution")
        workspace_id = run["workspace_id"]
        actions = _loads(run["action_descriptor_json"], [])
        if not actions:
            raise ValidationError("automation run has no executable supervised actions")
        payload = {
            "run_id": run_id,
            "automation_id": run["automation_id"],
            "actions": actions,
            "change_fingerprint": run["change_fingerprint"],
        }
        from auremgrid.services.job_ops import JobOperations

        def attach_job(item: dict[str, Any]) -> None:
            self.conn.execute("UPDATE automation_runs SET status='queued',job_id=? WHERE id=?", (item["id"], run_id))

        job = JobOperations(self.conn, self.new_id).enqueue_job(
            organization_id,
            workspace_id,
            principal["id"],
            "automation.execute",
            payload,
            idempotency_key=f"automation:{organization_id}:{workspace_id or 'organization'}:{run['change_fingerprint']}",
            transaction_hook=attach_job,
        )
        latest = self.conn.execute("SELECT status,job_id FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        if latest is None or latest["job_id"] != job["id"] or latest["status"] == "waiting_approval":
            self.conn.execute("UPDATE automation_runs SET status='queued',job_id=? WHERE id=?", (job["id"], run_id))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone())

    def execute_automation_job(self, organization_id: str, identity: Any, job_payload: dict[str, Any]) -> dict[str, Any]:
        run = self.conn.execute(
            """SELECT ar.*,a.created_by_person_id,a.organization_id
               FROM automation_runs ar JOIN automations a ON a.id=ar.automation_id
               WHERE a.organization_id=? AND ar.id=? AND ar.automation_id=?""",
            (organization_id, job_payload.get("run_id"), job_payload.get("automation_id")),
        ).fetchone()
        if run is None or run["status"] not in {"queued", "running", "completed"}:
            raise AuthorizationError("automation run scope is invalid")
        if run["workspace_id"] != identity.workspace_id:
            raise AuthorizationError("automation job workspace is invalid")
        approval = self.conn.execute(
            "SELECT status FROM approval_requests WHERE id=? AND organization_id=?",
            (run["approval_request_id"], organization_id),
        ).fetchone()
        if approval is None or approval["status"] != "approved":
            raise AuthorizationError("approved automation run required")
        actions = _loads(run["action_descriptor_json"], [])
        if actions != job_payload.get("actions"):
            raise AuthorizationError("queued automation action does not match run descriptor")
        if run["change_fingerprint"] != job_payload.get("change_fingerprint"):
            raise AuthorizationError("queued automation fingerprint does not match run")
        if run["status"] == "completed":
            return {"run_id": run["id"], "status": "completed", "output": _loads(run["output"], [])}
        now = _now().isoformat()
        self._reconcile_stale_automation_action_executions(run, now)
        self.conn.execute("UPDATE automation_runs SET status='running' WHERE id=?", (run["id"],))
        self.conn.commit()
        try:
            output = self._execute_actions(organization_id, identity, run, actions, json.loads(run["trigger_payload"]))
        except Exception as exc:
            self.conn.execute(
                "UPDATE automation_runs SET status='failed',completed_at=?,output=? WHERE id=?",
                (now, _json({"error": {"type": exc.__class__.__name__, "message": str(exc)}}), run["id"]),
            )
            self.conn.commit()
            raise
        self.conn.execute(
            "UPDATE automation_runs SET status='completed',completed_at=?,output=? WHERE id=?",
            (_now().isoformat(), _json(output), run["id"]),
        )
        self.conn.commit()
        return {"run_id": run["id"], "status": "completed", "output": output}

    def _execute_actions(self, organization_id: str, identity: Any, automation: Any, actions: list[Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        output = []
        person_id = automation["created_by_person_id"]
        for descriptor in actions:
            validated = validate_reversible_action_descriptor(
                self.conn,
                organization_id,
                descriptor["payload"].get("workspace_id"),
                person_id,
                descriptor,
            )
            idempotency_key = str(validated["payload"].get("idempotency_key") or f"automation:{automation['id']}:{validated['payload_hash']}")
            execution = self._begin_automation_action_execution(automation, validated, idempotency_key)
            if execution["status"] == "succeeded":
                output.append(_loads(execution["result_json"], {}))
                continue
            try:
                result = self._execute_automation_canonical_action(organization_id, identity, automation, person_id, validated)
            except Exception as exc:
                self._finish_automation_action_execution(execution["id"], "failed", None, {"type": exc.__class__.__name__, "message": str(exc)})
                raise
            self._finish_automation_action_execution(execution["id"], "succeeded", result, None)
            self._record_automation_action_audit(organization_id, automation, person_id, validated, result)
            output.append(result)
        return output

    def _execute_automation_canonical_action(
        self,
        organization_id: str,
        identity: Any,
        automation: Any,
        person_id: str,
        validated: dict[str, Any],
    ) -> dict[str, Any]:
        payload = validated["payload"]
        action = validated["action"]
        workspace_id = payload.get("workspace_id")
        if action == "create_risk":
            if not workspace_id:
                raise ValidationError("risk automation requires workspace_id")
            risk = self.client_ops.create_risk(
                organization_id,
                workspace_id,
                person_id,
                payload["type"],
                payload["severity"],
                payload["probability"],
                payload["impact"],
                payload["evidence"],
                payload["recommended_action"],
                payload.get("project_id"),
            )
            result = risk.to_dict() if hasattr(risk, "to_dict") else dict(risk)
            return {"action": action, "kind": validated["kind"], "entity_type": "risk", "id": result["id"], "source_refs": [result["id"]], "result": result}
        if action == "create_notification":
            notice = self.approvals.create_notification(
                organization_id,
                payload["recipient_person_id"],
                payload["reason"],
                payload["source_type"],
                payload.get("source_id"),
                workspace_id,
                payload["severity"],
                payload["urgency"],
                payload["waiting_days"],
                payload["actionable"],
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "notification", "id": notice["id"], "source_refs": [notice["id"]], "result": notice}
        if self.os is None:
            raise ValidationError("automation execution service unavailable")
        if action == "acknowledge_attention":
            item = self.os.proactive_intelligence.update_attention_status(
                identity,
                payload["fingerprint"],
                "acknowledged",
                payload["reason"],
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "proactive_attention", "id": item["id"], "source_refs": [item["id"]], "result": item}
        if action == "add_work_comment":
            if not workspace_id:
                raise ValidationError("work comment automation requires workspace_id")
            item = self.os.work_ops.add_comment(
                organization_id,
                workspace_id,
                person_id,
                payload["work_item_id"],
                payload["body"],
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "work_comment", "id": item["id"], "source_refs": [item["id"]], "result": item}
        if action == "create_proposal":
            item = self.os.brain_ops.create_proposal(
                organization_id,
                workspace_id,
                payload["proposer_type"],
                identity,
                payload["kind"],
                payload["content"],
                payload["payload"],
                payload["evidence"],
                payload["confidence"],
                payload.get("source_id"),
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "memory_proposal", "id": item["id"], "source_refs": [item["id"]], "result": item}
        raise ValidationError(f"unsupported automation action: {validated['kind']}")

    def _begin_automation_action_execution(self, automation: Any, validated: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        existing = self.conn.execute(
            "SELECT * FROM automation_action_executions WHERE organization_id=? AND idempotency_key=?",
            (automation["organization_id"], idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["descriptor_hash"] != validated["descriptor_hash"] or existing["payload_hash"] != validated["payload_hash"]:
                raise AuthorizationError("idempotency key was already used for a different automation action")
            if existing["status"] == "succeeded":
                return dict(existing)
            if existing["status"] == "running":
                raise ValidationError("automation action execution is already running")
            raise ValidationError("automation action execution failed previously; create a new approved run or idempotency key")
        now = _now().isoformat()
        item = {
            "id": self.new_id("automationaction"),
            "organization_id": automation["organization_id"],
            "workspace_id": validated["payload"].get("workspace_id"),
            "automation_id": automation["automation_id"] if "automation_id" in automation.keys() else automation["id"],
            "run_id": automation["id"],
            "approval_request_id": automation["approval_request_id"],
            "action": validated["action"],
            "action_kind": validated["kind"],
            "idempotency_key": idempotency_key,
            "descriptor_hash": validated["descriptor_hash"],
            "payload_hash": validated["payload_hash"],
            "status": "running",
            "result_json": None,
            "error_json": None,
            "created_at": now,
            "completed_at": None,
        }
        self.conn.execute(
            """INSERT INTO automation_action_executions(
                id,organization_id,workspace_id,automation_id,run_id,approval_request_id,action,
                action_kind,idempotency_key,descriptor_hash,payload_hash,status,result_json,error_json,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        self.conn.commit()
        return item

    def _find_automation_run_by_fingerprint(self, automation_id: str, fingerprint: str) -> Any:
        return self.conn.execute(
            """SELECT * FROM automation_runs
               WHERE automation_id=? AND change_fingerprint=?""",
            (automation_id, fingerprint),
        ).fetchone()

    @staticmethod
    def _automation_run_response(row: Any, deduped: bool) -> dict[str, Any]:
        return {
            "run_id": row["id"],
            "status": row["status"],
            "approval_request_id": row["approval_request_id"],
            "job_id": row["job_id"],
            "output": _loads(row["output"], {}),
            "deduped": deduped,
        }

    def _insert_automation_approval(
        self,
        organization_id: str,
        automation: Any,
        workspace_id: str | None,
        payload: dict[str, Any],
        now: str,
        auto_approved: bool = False,
    ) -> dict[str, Any]:
        approver = self.company.org_membership(organization_id, automation["created_by_person_id"])
        if approver is None:
            raise AuthorizationError("automation approver must belong to organization")
        if workspace_id is not None:
            scope = self.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != organization_id:
                raise NotFoundError("workspace not found")
        item = {
            "id": self.new_id("approval"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "requested_by_type": "automation",
            "requested_by_id": automation["id"],
            "requested_for": "automation run",
            "action_type": "automation.execute",
            "payload": json.dumps(payload),
            "reason": "Approved active auto policy for allowlisted reversible action" if auto_approved else "Training mode or gated action",
            "approver_person_id": automation["created_by_person_id"],
            "policy": "auto" if auto_approved else "human",
            "status": "approved" if auto_approved else "pending",
            "approved_at": now if auto_approved else None,
            "rejected_at": None,
            "comments": "",
            "created_at": now,
        }
        self.conn.execute("INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
        return item

    def _reconcile_stale_automation_action_executions(self, run: Any, now: str) -> None:
        running = self.conn.execute(
            """SELECT id FROM automation_action_executions
               WHERE run_id=? AND status='running'
               ORDER BY created_at ASC""",
            (run["id"],),
        ).fetchall()
        if not running:
            return
        job = self.conn.execute("SELECT status,attempts,lease_expires_at FROM jobs WHERE id=?", (run["job_id"],)).fetchone()
        if job is not None and job["attempts"] <= 1 and job["status"] in {"leased", "running"} and (job["lease_expires_at"] is None or job["lease_expires_at"] > now):
            return
        error = _json(
            {
                "type": "LeaseRecovered",
                "message": "stale running automation action fenced after job lease recovery",
            }
        )
        self.conn.execute(
            """UPDATE automation_action_executions
               SET status='failed',error_json=?,completed_at=?
               WHERE run_id=? AND status='running'""",
            (error, now, run["id"]),
        )
        self.conn.commit()

    def _finish_automation_action_execution(self, execution_id: str, status: str, result: dict[str, Any] | None, error: dict[str, Any] | None) -> None:
        self.conn.execute(
            "UPDATE automation_action_executions SET status=?,result_json=?,error_json=?,completed_at=? WHERE id=?",
            (status, _json(result) if result is not None else None, _json(error) if error is not None else None, _now().isoformat(), execution_id),
        )
        self.conn.commit()

    def _record_automation_action_audit(
        self,
        organization_id: str,
        automation: Any,
        person_id: str,
        validated: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """INSERT INTO ledger_audit(
                id,organization_id,workspace_id,principal_type,principal_id,action,entity_type,entity_id,detail,recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                self.new_id("audit"),
                organization_id,
                validated["payload"].get("workspace_id"),
                "automation",
                automation["automation_id"] if "automation_id" in automation.keys() else automation["id"],
                "execute",
                "automation_action",
                result.get("id"),
                _json({"action": validated["action"], "kind": validated["kind"], "approved_by_person_id": person_id}),
                _now().isoformat(),
            ),
        )
        self.conn.commit()

    def _automation_action_descriptors(
        self,
        organization_id: str,
        automation: Any,
        actions: list[Any],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        descriptors = []
        for action in actions:
            if action["one_way"]:
                raise ValidationError("automation actions must be reversible local actions")
            config = json.loads(action["config"])
            workspace_id = config.get("workspace_id") or payload.get("workspace_id")
            if action["type"] == "risk.create":
                descriptor_payload = {
                    "workspace_id": workspace_id,
                    "type": config.get("type", "relationship"),
                    "severity": config.get("severity", "medium"),
                    "probability": float(config.get("probability", 0.5)),
                    "impact": config.get("impact", payload.get("reason", "Automation signal")),
                    "evidence": config.get("evidence", _json(payload)),
                    "recommended_action": config.get("recommended_action", "Account lead review"),
                    "project_id": config.get("project_id"),
                    "idempotency_key": config.get("idempotency_key"),
                }
                descriptor = {
                    "action": "create_risk",
                    "kind": ACTION_KINDS["create_risk"],
                    "safe": True,
                    "one_way": False,
                    "payload": {key: value for key, value in descriptor_payload.items() if value is not None},
                }
            elif action["type"] == "notification.create":
                descriptor_payload = {
                    "workspace_id": workspace_id,
                    "recipient_person_id": config.get("recipient_person_id") or automation["created_by_person_id"],
                    "reason": config.get("reason", payload.get("reason", "Automation signal")),
                    "source_type": "automation",
                    "source_id": automation["id"],
                    "severity": float(config.get("severity", 0.5)),
                    "urgency": float(config.get("urgency", 0.5)),
                    "waiting_days": float(config.get("waiting_days", 0)),
                    "actionable": bool(config.get("actionable", True)),
                    "idempotency_key": config.get("idempotency_key"),
                }
                descriptor = {
                    "action": "create_notification",
                    "kind": ACTION_KINDS["create_notification"],
                    "safe": True,
                    "one_way": False,
                    "payload": {key: value for key, value in descriptor_payload.items() if value is not None},
                }
            elif action["type"] == "proactive_attention.acknowledge":
                descriptor_payload = {
                    "workspace_id": workspace_id,
                    "fingerprint": config.get("fingerprint") or payload.get("fingerprint"),
                    "reason": config.get("reason", payload.get("reason", "Automation acknowledgement")),
                    "idempotency_key": config.get("idempotency_key"),
                }
                descriptor = {
                    "action": "acknowledge_attention",
                    "kind": ACTION_KINDS["acknowledge_attention"],
                    "safe": True,
                    "one_way": False,
                    "payload": {key: value for key, value in descriptor_payload.items() if value is not None},
                }
            elif action["type"] == "work.comment.create":
                descriptor_payload = {
                    "workspace_id": workspace_id,
                    "work_item_id": config.get("work_item_id") or payload.get("work_item_id"),
                    "body": config.get("body", payload.get("body", "Automation review note")),
                    "idempotency_key": config.get("idempotency_key"),
                }
                descriptor = {
                    "action": "add_work_comment",
                    "kind": ACTION_KINDS["add_work_comment"],
                    "safe": True,
                    "one_way": False,
                    "payload": {key: value for key, value in descriptor_payload.items() if value is not None},
                }
            elif action["type"] == "brain.proposal.create":
                descriptor_payload = {
                    "workspace_id": workspace_id,
                    "proposer_type": config.get("proposer_type", "automation"),
                    "kind": config.get("kind", "memory"),
                    "content": config.get("content", payload.get("content", "Automation proposal")),
                    "payload": config.get("payload", payload.get("proposal_payload", {})),
                    "evidence": config.get("evidence", payload.get("evidence", "Automation signal")),
                    "confidence": float(config.get("confidence", payload.get("confidence", 0.5))),
                    "source_id": config.get("source_id"),
                    "idempotency_key": config.get("idempotency_key"),
                }
                descriptor = {
                    "action": "create_proposal",
                    "kind": ACTION_KINDS["create_proposal"],
                    "safe": True,
                    "one_way": False,
                    "payload": {key: value for key, value in descriptor_payload.items() if value is not None},
                }
            else:
                raise ValidationError(f"unsupported automation action: {action['type']}")
            validate_reversible_action_descriptor(
                self.conn,
                organization_id,
                workspace_id,
                automation["created_by_person_id"],
                descriptor,
            )
            descriptors.append(descriptor)
        return descriptors

    @staticmethod
    def _automation_workspace_id(descriptors: list[dict[str, Any]]) -> str | None:
        workspace_ids = {descriptor["payload"].get("workspace_id") for descriptor in descriptors}
        if len(workspace_ids) > 1:
            raise ValidationError("automation run actions must share one workspace scope")
        return next(iter(workspace_ids))

    @staticmethod
    def _automation_change_fingerprint(automation: Any, trigger_type: str, payload: dict[str, Any], descriptors: list[dict[str, Any]], workspace_id: str | None) -> str:
        return _stable_hash(
            {
                "automation_id": automation["id"],
                "trigger_type": trigger_type,
                "trigger_payload": payload,
                "workspace_id": workspace_id,
                "actions": descriptors,
            }
        )

    @staticmethod
    def _condition(payload: dict[str, Any], row: Any) -> bool:
        actual = payload.get(row["field"])
        expected = json.loads(row["value"])
        op = row["operator"]
        if op == "eq":
            return actual == expected
        if op == "gt":
            return actual is not None and actual > expected
        if op == "gte":
            return actual is not None and actual >= expected
        if op == "lt":
            return actual is not None and actual < expected
        if op == "contains":
            return actual is not None and expected in actual
        return False

