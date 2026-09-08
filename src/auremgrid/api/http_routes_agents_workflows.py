"""Agents, automations, and workflows route mixin."""
from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import (
    _need,
    _optional_float,
    _optional_int,
    _optional_str,
    _optional_str_list,
    _optional_string_sequence,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.agent_execution import AGENT_THINK_JOB_TYPE, SQLiteAgentExecutionStore, thinking_surface_enabled


class HttpRoutesAgentsWorkflowsMixin:
    def _get_agents_workflows(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/agents":
            self._json(200, self.os.agent_ops.command_center(_need(params, "organization_id"), _need(params, "person_id")))
            return True
        if parsed.path == "/agents/detail":
            assert identity is not None
            self._json(200, self.os.dashboard.agent_detail(
                _need(params, "organization_id"), _need(params, "person_id"), _need(params, "agent_id"),
                identity.capabilities,
            ))
            return True
        if parsed.path == "/agents/runs":
            self._json(200, {"runs": self.os.agent_ops.list_runs(
                _need(params, "organization_id"), _need(params, "person_id"),
                _optional_str(params.get("workspace_id")), _optional_str(params.get("agent_id")),
            )})
            return True
        if parsed.path == "/agents/runs/detail":
            self._json(200, self.os.agent_ops.run_detail(
                _need(params, "organization_id"), _need(params, "person_id"), _need(params, "run_id")
            ))
            return True
        if parsed.path in {"/agents/runs/thinking", "/agents/jobs/thinking"}:
            assert identity is not None
            organization_id = identity.organization_id
            workspace_id = _optional_str(params.get("workspace_id"))
            if workspace_id:
                self.os.auth.scope_identity(identity, workspace_id)
            job = self.os.jobs.get_job(organization_id, workspace_id, _need(params, "job_id"))
            if job.get("type") != AGENT_THINK_JOB_TYPE:
                raise NotFoundError("thinking job not found")
            store = SQLiteAgentExecutionStore(self.os.store.conn)
            read = store.thinking_read_model(
                organization_id, workspace_id, str((job.get("payload") or {}).get("run_id") or "")
            ) if thinking_surface_enabled(self.os) and store.available() else None
            self._json(200, {"available": bool(read), "thinking": read, "attempts": (read or {}).get("attempts", [])})
            return True
        if parsed.path == "/workflows/templates":
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            if self.os.company.org_membership(organization_id, person_id) is None:
                raise AuthorizationError("organization membership required")
            templates = self.os.workflow_catalog.for_wing(params["wing"]) if params.get("wing") else self.os.workflow_catalog.all()
            self._json(200, {"templates": [item.to_dict() for item in templates]})
            return True
        if parsed.path == "/workflows/runs":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            self.os._require_person_access(organization_id, workspace_id, person_id)
            rows = self.os.store.conn.execute("""SELECT id,definition_key,definition_name,definition_version,status,due_at,
                escalation_at,created_at,updated_at FROM workflow_runs WHERE organization_id=? AND workspace_id=? ORDER BY updated_at DESC""",
                (organization_id, workspace_id)).fetchall()
            self._json(200, {"runs": [dict(row) for row in rows]})
            return True
        if parsed.path == "/workflows/runs/get":
            self._json(200, self.os.workflow_ops.summary(_need(params, "organization_id"), _need(params, "workspace_id"),
                _need(params, "person_id"), _need(params, "run_id")))
            return True
        if parsed.path == "/workflows/escalations":
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            if self.os.company.org_membership(organization_id, person_id) is None:
                raise AuthorizationError("organization membership required")
            self._json(200, self.os.workflow_ops.overdue_escalations(_need(params, "organization_id"), _need(params, "workspace_id"),
                _need(params, "person_id"), params.get("as_of")))
            return True
        return False

    def _post_agents_workflows(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/agents/seed":
            self._json(201, {"agents": self.os.agent_ops.seed_primary_agents(_need(payload, "organization_id"), _need(payload, "person_id"))})
            return True
        if parsed.path == "/agents/tasks":
            self._json(201, self.os.agent_ops.enqueue_task(
                _need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "agent_id"),
                _need(payload, "title"), _need(payload, "instructions"), _optional_str(payload.get("workspace_id")),
                int(payload.get("priority", 50)), _optional_str_list(payload.get("intent_tags")),
                _optional_str(payload.get("selected_level")), _optional_str(payload.get("override_reason")) or "",
            ))
            return True
        if parsed.path == "/agents/runs/start":
            self._json(201, self.os.agent_ops.start_run(_need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "agent_id"), _need(payload, "task_id")))
            return True
        if parsed.path == "/agents/runs/claim":
            item = self.os.agent_ops.claim_next_task(
                _need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "agent_id")
            )
            self._json(200, {"run": item})
            return True
        if parsed.path == "/agents/runs/trace":
            self._json(201, self.os.agent_ops.record_trace(
                _need(payload, "organization_id"), _need(payload, "agent_id"), _need(payload, "run_id"),
                _need(payload, "kind"), _need(payload, "message"), payload.get("metadata") or {},
            ))
            return True
        if parsed.path == "/agents/runs/request-review":
            scoped = self.os.auth.scope_identity(identity, _need(payload, "workspace_id"))
            self._json(201, self.os.agent_ops.request_review(
                scoped.organization_id, scoped.person_id, _need(payload, "agent_id"), _need(payload, "run_id"),
                str(payload.get("query") or ""), _optional_str(payload.get("runbook_id")),
                _optional_string_sequence(payload.get("profile_ids"), "profile_ids"), scoped.capabilities,
            ))
            return True
        if parsed.path == "/agents/runs/tool-call":
            self._json(201, self.os.agent_ops.record_tool_call(
                _need(payload, "organization_id"), _need(payload, "agent_id"), _need(payload, "run_id"),
                _need(payload, "tool_name"), payload.get("arguments") or {},
                str(payload.get("result_preview", "")), _optional_str(payload.get("error")),
            ))
            return True
        if parsed.path == "/agents/runs/complete":
            self._json(200, self.os.agent_ops.complete_run(
                _need(payload, "organization_id"), _need(payload, "agent_id"), _need(payload, "run_id"),
                str(payload.get("content", "")), int(payload.get("input_tokens", 0)), int(payload.get("output_tokens", 0)),
                _optional_float(payload.get("cost")), [str(x) for x in payload.get("source_refs", [])],
            ))
            return True
        if parsed.path == "/automations":
            self._json(201, self.os.agent_ops.create_automation(
                _need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "name"),
                _need(payload, "trigger_type"), payload.get("conditions") or [], payload.get("actions") or [],
                str(payload.get("approval_policy", "human")),
            ))
            return True
        if parsed.path == "/automations/trigger":
            self._json(200, {"runs": self.os.agent_ops.trigger_automations(_need(payload, "organization_id"), _need(payload, "trigger_type"), payload.get("payload") or {})})
            return True
        if parsed.path == "/automations/execute-approved":
            self._json(200, self.os.agent_ops.execute_approved_automation_run(_need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "run_id")))
            return True
        if parsed.path == "/automations/activate":
            self._json(200, self.os.agent_ops.activate_automation(_need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "automation_id")))
            return True
        if parsed.path == "/workflows/runs":
            template = self.os.workflow_catalog.get(_need(payload, "template_id"))
            item = self.os.workflow_ops.create_run(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                template, _optional_str(payload.get("due_at")), _optional_int(payload.get("sla_minutes")),
                _optional_str(payload.get("idempotency_key")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/workflows/stages/start":
            item = self.os.workflow_ops.start_stage(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "stage_id"), _optional_int(payload.get("expected_version")),
                _optional_str(payload.get("idempotency_key")),
            )
            self._json(200, item)
            return True
        if parsed.path == "/workflows/evidence":
            item = self.os.workflow_ops.submit_evidence(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "stage_id"), _need(payload, "kind"),
                _optional_str(payload.get("uri")), _optional_str(payload.get("text")), payload.get("metadata") or {},
                _optional_str(payload.get("object_type")), _optional_str(payload.get("object_id")),
                _optional_str(payload.get("locator")), _optional_str(payload.get("content_hash")),
                _optional_str(payload.get("idempotency_key")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/workflows/approvals/request":
            item = self.os.workflow_ops.request_approval(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "stage_id"), _need(payload, "reason"),
                _optional_str(payload.get("approval_request_id")), _optional_int(payload.get("expected_version")),
                _optional_str(payload.get("idempotency_key")),
            )
            self._json(200, item)
            return True
        if parsed.path == "/workflows/approvals/decide":
            item = self.os.workflow_ops.decide_approval(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "stage_id"), _need(payload, "decision"),
                _need(payload, "reason"), _optional_str(payload.get("approval_request_id")),
                _optional_str(payload.get("idempotency_key")),
            )
            self._json(200, item)
            return True
        if parsed.path == "/workflows/handoffs/acknowledge":
            item = self.os.workflow_ops.acknowledge_handoff(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "from_stage_id"), _need(payload, "to_stage_id"),
                _need(payload, "artifact_contract"), str(payload.get("reason", "")),
                _optional_str(payload.get("idempotency_key")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/workflows/stages/complete":
            item = self.os.workflow_ops.complete_stage(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "stage_id"), str(payload.get("reason", "")),
                _optional_int(payload.get("expected_version")), _optional_str(payload.get("idempotency_key")),
            )
            self._json(200, item)
            return True
        if parsed.path == "/workflows/stages/block":
            item = self.os.workflow_ops.block_stage(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "stage_id"), _need(payload, "reason"),
                _optional_int(payload.get("expected_version")),
            )
            self._json(200, item)
            return True
        if parsed.path == "/workflows/runs/cancel":
            item = self.os.workflow_ops.cancel_run(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "run_id"), _need(payload, "reason"), _optional_int(payload.get("expected_version")),
            )
            self._json(200, item)
            return True
        return False
