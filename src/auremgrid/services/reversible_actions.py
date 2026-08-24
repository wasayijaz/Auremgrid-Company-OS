from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _load_json(value: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError) as exc:
        raise ValidationError("approved action payload must be valid JSON") from exc
    if not isinstance(loaded, dict):
        raise ValidationError("approved action payload must be an object")
    return loaded


def _as_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise ValidationError(f"{field} must be finite")
    return result


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    return value.strip()


ACTION_KINDS: dict[str, str] = {
    "generate_report": "report.generate",
    "create_notification": "notification.create",
    "acknowledge_attention": "proactive_attention.acknowledge",
    "create_risk": "risk.create",
    "add_work_comment": "work.comment.create",
    "create_proposal": "brain.proposal.create",
}


_ALLOWED_PAYLOAD_KEYS: dict[str, set[str]] = {
    "generate_report": {"organization_id", "workspace_id", "person_id", "report_type", "type", "idempotency_key"},
    "create_notification": {
        "organization_id",
        "workspace_id",
        "person_id",
        "recipient_person_id",
        "reason",
        "source_type",
        "source_id",
        "severity",
        "urgency",
        "waiting_days",
        "actionable",
        "idempotency_key",
    },
    "acknowledge_attention": {
        "organization_id",
        "workspace_id",
        "person_id",
        "fingerprint",
        "reason",
        "idempotency_key",
    },
    "create_risk": {
        "organization_id",
        "workspace_id",
        "person_id",
        "type",
        "severity",
        "probability",
        "impact",
        "evidence",
        "recommended_action",
        "project_id",
        "idempotency_key",
    },
    "add_work_comment": {
        "organization_id",
        "workspace_id",
        "person_id",
        "work_item_id",
        "body",
        "idempotency_key",
    },
    "create_proposal": {
        "organization_id",
        "workspace_id",
        "person_id",
        "proposer_type",
        "kind",
        "content",
        "payload",
        "evidence",
        "confidence",
        "source_id",
        "idempotency_key",
    },
}


def _basic_descriptor(descriptor: dict[str, Any]) -> str:
    if not isinstance(descriptor, dict):
        raise ValidationError("agent action descriptor must be an object")
    if descriptor.get("safe") is not True or descriptor.get("one_way") is not False:
        raise ValidationError("agent action descriptor must be safe and reversible")
    action = descriptor.get("action")
    if action not in ACTION_KINDS:
        raise ValidationError("unsupported agent action descriptor")
    kind = descriptor.get("kind")
    if kind is not None and kind != ACTION_KINDS[action]:
        raise AuthorizationError("approved action kind does not match descriptor")
    payload = descriptor.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValidationError("agent action descriptor payload must be an object")
    unknown = set(payload) - _ALLOWED_PAYLOAD_KEYS[action]
    if unknown:
        raise ValidationError(f"unsupported action payload fields: {', '.join(sorted(unknown))}")
    return str(action)


def _canonical_payload(action: str, payload: dict[str, Any], organization_id: str, workspace_id: str | None, actor_person_id: str) -> dict[str, Any]:
    if payload.get("organization_id") not in {None, organization_id}:
        raise AuthorizationError("action organization is outside approved scope")
    if payload.get("workspace_id") not in {None, workspace_id}:
        raise AuthorizationError("action workspace is outside approved scope")
    if payload.get("person_id") not in {None, actor_person_id}:
        raise AuthorizationError("action actor is outside approved scope")

    if action == "generate_report":
        report_type = payload.get("report_type") or payload.get("type")
        if not isinstance(report_type, str) or not report_type.strip():
            raise ValidationError("report_type is required")
        result = {"report_type": report_type.strip(), "workspace_id": workspace_id}
    elif action == "create_notification":
        result = {
            "recipient_person_id": _required_text(payload, "recipient_person_id"),
            "reason": _required_text(payload, "reason"),
            "source_type": _required_text(payload, "source_type"),
            "source_id": payload.get("source_id"),
            "workspace_id": workspace_id,
            "severity": _as_float(payload.get("severity", 0.5), "severity"),
            "urgency": _as_float(payload.get("urgency", 0.5), "urgency"),
            "waiting_days": _as_float(payload.get("waiting_days", 0), "waiting_days"),
            "actionable": bool(payload.get("actionable", True)),
        }
        if not 0 <= result["severity"] <= 1 or not 0 <= result["urgency"] <= 1 or result["waiting_days"] < 0:
            raise ValidationError("notification severity, urgency, and waiting days are out of range")
    elif action == "acknowledge_attention":
        result = {
            "fingerprint": _required_text(payload, "fingerprint"),
            "reason": str(payload.get("reason", "approved agent acknowledgement")).strip() or "approved agent acknowledgement",
            "workspace_id": workspace_id,
        }
    elif action == "create_risk":
        probability = _as_float(payload.get("probability"), "probability")
        if not 0 <= probability <= 1:
            raise ValidationError("probability is out of range")
        result = {
            "type": _required_text(payload, "type"),
            "severity": _required_text(payload, "severity"),
            "probability": probability,
            "impact": _required_text(payload, "impact"),
            "evidence": _required_text(payload, "evidence"),
            "recommended_action": _required_text(payload, "recommended_action"),
            "project_id": payload.get("project_id"),
            "workspace_id": workspace_id,
        }
        if result["project_id"] is not None:
            result["project_id"] = _required_text(payload, "project_id")
    elif action == "add_work_comment":
        result = {
            "work_item_id": _required_text(payload, "work_item_id"),
            "body": _required_text(payload, "body"),
            "workspace_id": workspace_id,
        }
    elif action == "create_proposal":
        confidence = _as_float(payload.get("confidence"), "confidence")
        if not 0 <= confidence <= 1:
            raise ValidationError("confidence is out of range")
        structured_payload = payload.get("payload")
        if not isinstance(structured_payload, dict):
            raise ValidationError("proposal payload must be an object")
        source_id = payload.get("source_id")
        if source_id is not None:
            source_id = _required_text(payload, "source_id")
        result = {
            "proposer_type": _required_text(payload, "proposer_type"),
            "kind": _required_text(payload, "kind"),
            "content": _required_text(payload, "content"),
            "payload": structured_payload,
            "evidence": _required_text(payload, "evidence"),
            "confidence": confidence,
            "source_id": source_id,
            "workspace_id": workspace_id,
        }
    else:
        raise ValidationError("unsupported agent action descriptor")

    if "idempotency_key" in payload:
        result["idempotency_key"] = _required_text(payload, "idempotency_key")
    return result


def validate_approved_action_descriptor(
    conn: Any,
    organization_id: str,
    workspace_id: str | None,
    actor_person_id: str,
    descriptor: dict[str, Any],
    approval_request_id: str | None,
    orchestrator_trace_id: str | None = None,
) -> dict[str, Any]:
    action = _basic_descriptor(descriptor)
    if not approval_request_id:
        raise ValidationError("approved action descriptor required")
    approval = conn.execute(
        "SELECT * FROM approval_requests WHERE id=? AND organization_id=?",
        (approval_request_id, organization_id),
    ).fetchone()
    if approval is None or approval["status"] != "approved":
        raise AuthorizationError("same-scope approved action required")
    if approval["workspace_id"] != workspace_id or approval["action_type"] != ACTION_KINDS[action]:
        raise AuthorizationError("same-scope approved action required")
    approved_payload = _load_json(approval["payload"])
    descriptor_payload = descriptor.get("payload") or {}
    canonical_descriptor = _canonical_payload(action, descriptor_payload, organization_id, workspace_id, actor_person_id)
    canonical_approval = _canonical_payload(action, approved_payload, organization_id, workspace_id, actor_person_id)
    if canonical_descriptor != canonical_approval:
        raise AuthorizationError("approved action payload does not match descriptor")
    if action == "create_notification":
        recipient = conn.execute(
            "SELECT id FROM people WHERE organization_id=? AND id=? AND status='active'",
            (organization_id, canonical_descriptor["recipient_person_id"]),
        ).fetchone()
        if recipient is None:
            raise NotFoundError("notification recipient not found")
    if action == "create_risk" and canonical_descriptor.get("project_id"):
        project = conn.execute(
            "SELECT id FROM projects WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, canonical_descriptor["project_id"]),
        ).fetchone()
        if project is None:
            raise NotFoundError("project not found")
    if action == "add_work_comment":
        work_item = conn.execute(
            "SELECT id FROM work_items WHERE workspace_id=? AND id=?",
            (workspace_id, canonical_descriptor["work_item_id"]),
        ).fetchone()
        if work_item is None:
            raise NotFoundError("work item not found")
    if action == "create_proposal" and canonical_descriptor.get("source_id"):
        source = conn.execute(
            "SELECT id FROM sources WHERE workspace_id=? AND id=?",
            (workspace_id, canonical_descriptor["source_id"]),
        ).fetchone()
        if source is None:
            raise NotFoundError("proposal evidence not found")
    if action == "acknowledge_attention":
        lifecycle = conn.execute(
            """SELECT workspace_id FROM proactive_intelligence_attention_lifecycle
               WHERE organization_id=? AND person_id=? AND fingerprint=?""",
            (organization_id, actor_person_id, canonical_descriptor["fingerprint"]),
        ).fetchone()
        if lifecycle is None:
            raise NotFoundError("attention lifecycle item not found")
        if lifecycle["workspace_id"] != workspace_id:
            raise AuthorizationError("attention lifecycle item is outside approved scope")
    if orchestrator_trace_id:
        trace = conn.execute(
            "SELECT 1 FROM intelligence_orchestrator_runs WHERE trace_id=? AND organization_id=? AND workspace_id IS ?",
            (orchestrator_trace_id, organization_id, workspace_id),
        ).fetchone()
        if trace is None:
            raise ValidationError("orchestrator trace is not durable in scope")
    return {
        "action": action,
        "kind": ACTION_KINDS[action],
        "payload": canonical_descriptor,
        "descriptor_hash": _hash(descriptor),
        "payload_hash": _hash(canonical_descriptor),
    }


class ReversibleActionExecutor:
    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

    def execute(self, organization_id: str, identity: AuthenticatedIdentity, job_payload: dict[str, Any]) -> dict[str, Any]:
        run = self.conn.execute(
            """SELECT r.*,t.approval_request_id,t.workspace_id AS task_workspace,
                      t.action_descriptor_json,t.orchestrator_trace_id
               FROM agent_runs r
               JOIN agent_tasks t ON t.id=r.task_id
               WHERE r.id=? AND r.agent_id=? AND r.organization_id=?""",
            (job_payload.get("run_id"), job_payload.get("agent_id"), organization_id),
        ).fetchone()
        if run is None or run["status"] != "running" or run["task_workspace"] != identity.workspace_id:
            raise AuthorizationError("agent run scope is invalid")
        descriptor = job_payload.get("action") or {}
        stored_descriptor = _load_json(run["action_descriptor_json"])
        if descriptor != stored_descriptor:
            raise AuthorizationError("queued action does not match approved task descriptor")
        validated = validate_approved_action_descriptor(
            self.conn,
            organization_id,
            run["task_workspace"],
            identity.person_id,
            descriptor,
            run["approval_request_id"],
            run["orchestrator_trace_id"],
        )
        idempotency_key = str(validated["payload"].get("idempotency_key") or f"agent-action:{run['task_id']}:{validated['action']}")
        execution = self._begin_execution(run, validated, idempotency_key)
        if execution["status"] == "succeeded":
            return _load_json(execution["result_json"])
        self.os.agent_ops.record_trace(
            organization_id,
            run["agent_id"],
            run["id"],
            "action",
            f"Executing approved action {validated['kind']}",
            {"execution_id": execution["id"], "idempotency_key": idempotency_key},
        )
        try:
            result = self._execute_canonical(organization_id, identity, run, validated)
        except Exception as exc:
            self._finish_execution(execution["id"], "failed", None, {"type": exc.__class__.__name__, "message": str(exc)})
            try:
                self.os.agent_ops.record_trace(
                    organization_id,
                    run["agent_id"],
                    run["id"],
                    "action",
                    f"Approved action {validated['kind']} failed",
                    {"execution_id": execution["id"], "error": str(exc)},
                )
            except Exception:
                pass
            raise
        self._finish_execution(execution["id"], "succeeded", result, None)
        self._record_audit(organization_id, run["task_workspace"], identity.person_id, validated, result)
        self.os.agent_ops.record_trace(
            organization_id,
            run["agent_id"],
            run["id"],
            "action",
            f"Approved action {validated['kind']} completed",
            {"execution_id": execution["id"], "result_id": result.get("id")},
        )
        return result

    def _begin_execution(self, run: Any, validated: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        existing = self.conn.execute(
            "SELECT * FROM agent_action_executions WHERE organization_id=? AND idempotency_key=?",
            (run["organization_id"], idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["descriptor_hash"] != validated["descriptor_hash"] or existing["payload_hash"] != validated["payload_hash"]:
                raise AuthorizationError("idempotency key was already used for a different approved action")
            if existing["status"] == "succeeded":
                return dict(existing)
            if existing["status"] == "running":
                raise ValidationError("approved action execution is already running")
            now = _now().isoformat()
            self.conn.execute(
                "UPDATE agent_action_executions SET status='running',error_json=NULL,completed_at=NULL WHERE id=?",
                (existing["id"],),
            )
            self.conn.commit()
            return dict(self.conn.execute("SELECT * FROM agent_action_executions WHERE id=?", (existing["id"],)).fetchone())
        now = _now().isoformat()
        item = {
            "id": self.os.jobs.new_id("agentaction"),
            "organization_id": run["organization_id"],
            "workspace_id": run["task_workspace"],
            "agent_id": run["agent_id"],
            "run_id": run["id"],
            "task_id": run["task_id"],
            "approval_request_id": run["approval_request_id"],
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
            """INSERT INTO agent_action_executions(
                id,organization_id,workspace_id,agent_id,run_id,task_id,approval_request_id,action,
                action_kind,idempotency_key,descriptor_hash,payload_hash,status,result_json,error_json,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        self.conn.commit()
        return item

    def _finish_execution(self, execution_id: str, status: str, result: dict[str, Any] | None, error: dict[str, Any] | None) -> None:
        self.conn.execute(
            "UPDATE agent_action_executions SET status=?,result_json=?,error_json=?,completed_at=? WHERE id=?",
            (status, _json(result) if result is not None else None, _json(error) if error is not None else None, _now().isoformat(), execution_id),
        )
        self.conn.commit()

    def _execute_canonical(self, organization_id: str, identity: AuthenticatedIdentity, run: Any, validated: dict[str, Any]) -> dict[str, Any]:
        payload = validated["payload"]
        action = validated["action"]
        if action == "generate_report":
            report = self.os.agent_ops.generate_report(
                organization_id,
                identity.person_id,
                str(payload["report_type"]),
                run["task_workspace"],
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "report", "id": report.get("id"), "source_refs": [str(report.get("id", ""))], "result": report}
        if action == "create_notification":
            notice = self.os.agency_ops.create_notification(
                organization_id,
                payload["recipient_person_id"],
                payload["reason"],
                payload["source_type"],
                payload.get("source_id"),
                run["task_workspace"],
                payload["severity"],
                payload["urgency"],
                payload["waiting_days"],
                payload["actionable"],
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "notification", "id": notice["id"], "source_refs": [notice["id"]], "result": notice}
        if action == "acknowledge_attention":
            item = self.os.proactive_intelligence.update_attention_status(
                identity,
                payload["fingerprint"],
                "acknowledged",
                payload["reason"],
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "proactive_attention", "id": item["id"], "source_refs": [item["id"]], "result": item}
        if action == "create_risk":
            item = self.os.client_ops.create_risk(
                organization_id,
                run["task_workspace"],
                identity.person_id,
                payload["type"],
                payload["severity"],
                payload["probability"],
                payload["impact"],
                payload["evidence"],
                payload["recommended_action"],
                payload.get("project_id"),
            )
            result = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            return {"action": action, "kind": validated["kind"], "entity_type": "risk", "id": result["id"], "source_refs": [result["id"]], "result": result}
        if action == "add_work_comment":
            item = self.os.work_ops.add_comment(
                organization_id,
                run["task_workspace"],
                identity.person_id,
                payload["work_item_id"],
                payload["body"],
            )
            return {"action": action, "kind": validated["kind"], "entity_type": "work_comment", "id": item["id"], "source_refs": [item["id"]], "result": item}
        if action == "create_proposal":
            item = self.os.brain_ops.create_proposal(
                organization_id,
                run["task_workspace"],
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
        raise ValidationError("unsupported agent action descriptor")

    def _record_audit(
        self,
        organization_id: str,
        workspace_id: str | None,
        person_id: str,
        validated: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """INSERT INTO ledger_audit(
                id,organization_id,workspace_id,principal_type,principal_id,action,entity_type,entity_id,detail,recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                self.os.jobs.new_id("audit"),
                organization_id,
                workspace_id,
                "person",
                person_id,
                "execute",
                "agent_action",
                result.get("id"),
                _json({"action": validated["action"], "kind": validated["kind"]}),
                _now().isoformat(),
            ),
        )
        self.conn.commit()
