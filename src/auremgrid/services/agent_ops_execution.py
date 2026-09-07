"""Agent execution, task queues, run traces, tool calls, and reviews mixin."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import (
    AgentLevel,
    LEVEL_DEFINITIONS,
)
from auremgrid.services.reversible_actions import (
    ACTION_KINDS,
    supervised_action_catalog,
    validate_approved_action_descriptor,
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


class AgentOpsExecutionMixin:
    def enqueue_task(
        self,
        organization_id: str,
        requested_by_person_id: str,
        agent_id: str,
        title: str,
        instructions: str,
        workspace_id: str | None = None,
        priority: int = 50,
        intent_tags: Sequence[str] | None = None,
        selected_level: AgentLevel | str | None = None,
        override_reason: str = "",
        action_descriptor: dict[str, Any] | None = None,
        orchestrator_trace_id: str | None = None,
        approval_request_id: str | None = None,
        parent_task_id: str | None = None,
        delegation_depth: int | None = None,
    ) -> dict[str, Any]:
        if self.company.org_membership(organization_id, requested_by_person_id) is None:
            raise AuthorizationError("organization membership required")
        if workspace_id and workspace_id not in self.visible_workspace_ids(organization_id, requested_by_person_id):
            raise AuthorizationError("agent task workspace is not visible to caller")
        agent = self.conn.execute(
            "SELECT * FROM agents WHERE organization_id=? AND id=?",
            (organization_id, agent_id),
        ).fetchone()
        if agent is None:
            raise NotFoundError("agent not found")
        if workspace_id and (self.company.workspace_scope(workspace_id) is None or self.company.workspace_scope(workspace_id)["organization_id"] != organization_id):
            raise NotFoundError("workspace not found in organization")
        if workspace_id and workspace_id not in json.loads(agent["allowed_workspace_ids"]):
            raise AuthorizationError("agent cannot access workspace")
        if not title.strip() or not instructions.strip():
            raise ValidationError("agent task title and instructions are required")
        if priority < 0 or priority > 100:
            raise ValidationError("agent task priority must be between 0 and 100")
        if action_descriptor is not None:
            validate_approved_action_descriptor(
                self.conn,
                organization_id,
                workspace_id,
                requested_by_person_id,
                action_descriptor,
                approval_request_id,
                orchestrator_trace_id,
            )
        depth = self._validated_delegation_depth(organization_id, workspace_id, parent_task_id, delegation_depth)

        tags = self.validate_capability_tags(intent_tags or ("execute",))
        recommended = self.resolve_level(tags)
        selected = self._selected_level(recommended, selected_level, override_reason)
        agent_level = self._normalize_level(agent["level"])
        if selected not in LEVEL_DEFINITIONS[agent_level].can_handle:
            raise ValidationError("agent level cannot handle selected task level")

        now = _now().isoformat()
        task = {
            "id": self.new_id("agenttask"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "title": title,
            "instructions": instructions,
            "priority": priority,
            "status": "queued",
            "approval_request_id": approval_request_id,
            "intent_tags": _json(tags),
            "recommended_level": recommended.value,
            "selected_level": selected.value,
            "level_override_reason": override_reason.strip() if selected != recommended else None,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "action_descriptor_json": _json(action_descriptor) if action_descriptor is not None else None,
            "orchestrator_trace_id": orchestrator_trace_id,
            "parent_task_id": parent_task_id,
            "delegation_depth": depth,
        }
        self.conn.execute(
            """INSERT INTO agent_tasks(
                id,organization_id,workspace_id,agent_id,title,instructions,priority,status,
                approval_request_id,intent_tags,recommended_level,selected_level,level_override_reason,
                created_at,started_at,completed_at,action_descriptor_json,orchestrator_trace_id,
                parent_task_id,delegation_depth
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(task.values()),
        )
        if selected != recommended:
            self.conn.execute(
                """INSERT INTO agent_level_overrides(
                    id,organization_id,task_id,requested_by_person_id,recommended_level,selected_level,
                    intent_tags,reason,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    self.new_id("leveloverride"),
                    organization_id,
                    task["id"],
                    requested_by_person_id,
                    recommended.value,
                    selected.value,
                    _json(tags),
                    override_reason.strip(),
                    now,
                ),
            )
        self.conn.execute(
            "INSERT INTO agent_queue_items VALUES (?,?,?,?,?,?,?,?)",
            (self.new_id("queue"), organization_id, agent_id, task["id"], priority, "queued", now, None),
        )
        self.conn.commit()
        return task

    def start_run(self, organization_id: str, person_id: str, agent_id: str, task_id: str) -> dict[str, Any]:
        if self.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        task = self.conn.execute(
            "SELECT * FROM agent_tasks WHERE organization_id=? AND agent_id=? AND id=?",
            (organization_id, agent_id, task_id),
        ).fetchone()
        if task is None or task["status"] != "queued":
            raise ValidationError("queued agent task required")
        if task["action_descriptor_json"]:
            validate_approved_action_descriptor(
                self.conn,
                organization_id,
                task["workspace_id"],
                person_id,
                json.loads(task["action_descriptor_json"]),
                task["approval_request_id"],
                task["orchestrator_trace_id"],
            )
        if task["workspace_id"] is not None and task["workspace_id"] not in self.visible_workspace_ids(organization_id, person_id):
            raise AuthorizationError("agent task workspace is not visible to caller")
        now = _now().isoformat()
        run = {
            "id": self.new_id("run"),
            "organization_id": organization_id,
            "workspace_id": task["workspace_id"],
            "agent_id": agent_id,
            "task_id": task_id,
            "status": "running",
            "started_at": now,
            "completed_at": None,
            "runtime_ms": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": None,
            "error_id": None,
            "output_id": None,
            "delegation_depth": int(task["delegation_depth"]),
        }
        self.conn.execute(
            """INSERT INTO agent_runs(
                id,organization_id,workspace_id,agent_id,task_id,status,started_at,completed_at,
                runtime_ms,input_tokens,output_tokens,cost,error_id,output_id,delegation_depth
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(run.values()),
        )
        self.conn.execute("UPDATE agent_tasks SET status='running',started_at=? WHERE id=?", (now, task_id))
        self.conn.execute("UPDATE agents SET status='running',current_task_id=? WHERE id=?", (task_id, agent_id))
        self.conn.execute("UPDATE agent_queue_items SET status='claimed',claimed_at=? WHERE task_id=?", (now, task_id))
        self.conn.commit()
        if task["action_descriptor_json"]:
            self._enqueue_approved_action(run, person_id)
        return run

    def _enqueue_approved_action(self, run: dict[str, Any], person_id: str) -> None:
        descriptor = json.loads(self.conn.execute("SELECT action_descriptor_json FROM agent_tasks WHERE id=?", (run["task_id"],)).fetchone()[0])
        principal = self.conn.execute(
            "SELECT id FROM auth_principals WHERE organization_id=? AND person_id=? AND status='active' LIMIT 1",
            (run["organization_id"], person_id),
        ).fetchone()
        if principal is None:
            raise AuthorizationError("active principal required for approved action")
        from auremgrid.services.job_ops import JobOperations
        JobOperations(self.conn, self.new_id).enqueue_job(
            run["organization_id"], run["workspace_id"], principal["id"], "agent.run",
            {"run_id": run["id"], "agent_id": run["agent_id"], "action": descriptor},
            idempotency_key=f"agent-action:{run['task_id']}",
        )

    def claim_next_task(self, organization_id: str, person_id: str, agent_id: str) -> dict[str, Any] | None:
        """Claim the highest-priority queued task that remains inside the agent scope."""
        if self.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        agent = self.conn.execute(
            "SELECT * FROM agents WHERE organization_id=? AND id=?",
            (organization_id, agent_id),
        ).fetchone()
        if agent is None:
            raise NotFoundError("agent not found")
        allowed = set(json.loads(agent["allowed_workspace_ids"]))
        visible = self.visible_workspace_ids(organization_id, person_id)
        rows = self.conn.execute(
            """SELECT t.* FROM agent_queue_items q
               JOIN agent_tasks t ON t.id=q.task_id
               WHERE q.organization_id=? AND q.agent_id=? AND q.status='queued' AND t.status='queued'
               ORDER BY q.priority DESC,q.enqueued_at ASC,q.id ASC""",
            (organization_id, agent_id),
        ).fetchall()
        for task in rows:
            if task["workspace_id"] is None or task["workspace_id"] in allowed and task["workspace_id"] in visible:
                return self.start_run(organization_id, person_id, agent_id, task["id"])
        return None

    def report_action_descriptors(
        self,
        organization_id: str,
        person_id: str,
        workspace_id: str | None,
        capabilities: Any = None,
    ) -> list[dict[str, Any]]:
        if not self._can(capabilities, "workspace_write"):
            return []
        if self.company.org_membership(organization_id, person_id) is None:
            return []
        if workspace_id and workspace_id not in self.visible_workspace_ids(organization_id, person_id):
            return []
        types = ["client_weekly_report", "capacity_report", "workload_report"]
        return [
            {
                "id": f"generate-{report_type}",
                "action": "generate_report",
                "label": report_type.replace("_", " ").title(),
                "kind": "report.generate",
                "route": "/reports/generate",
                "method": "POST",
                "payload": {
                    "organization_id": organization_id,
                    "person_id": person_id,
                    "workspace_id": workspace_id,
                    "type": report_type,
                },
                "required_fields": [],
                "safe": True,
                "one_way": False,
                "requires_approval": False,
                "status": "available",
                "idempotency_scope": f"report:{organization_id}:{workspace_id or 'organization'}:{report_type}",
            }
            for report_type in types
        ]

    def agent_action_descriptors(
        self,
        organization_id: str,
        person_id: str,
        agent_id: str,
        workspace_id: str | None = None,
        capabilities: Any = None,
    ) -> list[dict[str, Any]]:
        if not self._can(capabilities, "agent_run"):
            return []
        if self.company.org_membership(organization_id, person_id) is None:
            return []
        visible = self.visible_workspace_ids(organization_id, person_id)
        if workspace_id and workspace_id not in visible:
            return []
        agent = self.conn.execute(
            "SELECT * FROM agents WHERE organization_id=? AND id=?",
            (organization_id, agent_id),
        ).fetchone()
        if agent is None:
            return []
        allowed = set(json.loads(agent["allowed_workspace_ids"] or "[]"))
        effective_workspace = workspace_id if workspace_id in allowed and workspace_id in visible else None
        if workspace_id and effective_workspace is None:
            return []
        base = {"organization_id": organization_id, "person_id": person_id, "agent_id": agent_id}
        return [
            {
                "id": "create-agent-task",
                "action": "create_agent_task",
                "label": "Queue task",
                "kind": "agent.task.create",
                "route": "/agents/tasks",
                "method": "POST",
                "payload": {**base, "workspace_id": effective_workspace, "priority": 50},
                "required_fields": ["title", "instructions"],
                "safe": True,
                "one_way": False,
                "requires_approval": False,
                "status": "available",
            },
            {
                "id": "claim-agent-task",
                "action": "claim_agent_task",
                "label": "Claim next task",
                "kind": "agent.task.claim",
                "route": "/agents/runs/claim",
                "method": "POST",
                "payload": base,
                "required_fields": [],
                "safe": True,
                "one_way": False,
                "requires_approval": False,
                "status": "available",
            },
        ]

    def record_tool_call(
        self,
        organization_id: str,
        agent_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result_preview: str = "",
        error: str | None = None,
    ) -> dict[str, Any]:
        self._run(organization_id, agent_id, run_id)
        agent = self.conn.execute("SELECT tools FROM agents WHERE id=?", (agent_id,)).fetchone()
        if tool_name not in json.loads(agent[0]):
            raise AuthorizationError("tool is not allowed for agent")
        run = self.conn.execute("SELECT workspace_id FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        argument_workspace = arguments.get("workspace_id") if isinstance(arguments, dict) else None
        if argument_workspace is not None and argument_workspace != run["workspace_id"]:
            raise AuthorizationError("tool call workspace is outside the run scope")
        now = _now().isoformat()
        item = {
            "id": self.new_id("toolcall"),
            "run_id": run_id,
            "tool_name": tool_name,
            "arguments": _json(arguments),
            "status": "failed" if error else "completed",
            "started_at": now,
            "completed_at": now,
            "result_preview": result_preview[:500],
            "error": error,
        }
        self.conn.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?)", tuple(item.values()))
        self.conn.commit()
        return item

    def record_trace(
        self,
        organization_id: str,
        agent_id: str,
        run_id: str,
        kind: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._run(organization_id, agent_id, run_id)
        if not kind.strip() or not message.strip():
            raise ValidationError("trace kind and message are required")
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM run_traces WHERE run_id=?", (run_id,)
        ).fetchone()
        item = {
            "id": self.new_id("trace"),
            "run_id": run_id,
            "sequence": int(row[0]),
            "kind": kind.strip(),
            "message": message.strip(),
            "metadata": _json(metadata or {}),
            "recorded_at": _now().isoformat(),
        }
        self.conn.execute("INSERT INTO run_traces VALUES (?,?,?,?,?,?,?)", tuple(item.values()))
        self.conn.commit()
        return item

    def request_review(
        self,
        organization_id: str,
        person_id: str,
        agent_id: str,
        run_id: str,
        query: str = "",
        runbook_id: str | None = None,
        profile_ids: Sequence[str] | None = None,
        capabilities: Any = None,
    ) -> dict[str, Any]:
        run = self._run(organization_id, agent_id, run_id)
        if run["status"] != "running":
            raise ValidationError("running agent run required for request review")
        workspace_id = run["workspace_id"]
        if workspace_id is None or workspace_id not in self.visible_workspace_ids(organization_id, person_id):
            raise AuthorizationError("agent review workspace is not visible to caller")
        task = self.conn.execute(
            "SELECT action_descriptor_json FROM agent_tasks WHERE id=?",
            (run["task_id"],),
        ).fetchone()
        if task is not None and task["action_descriptor_json"]:
            raise ValidationError("request review cannot attach to executable agent action tasks")
        if self.intelligence_orchestrator is None:
            raise ValidationError("intelligence orchestrator unavailable")
        result = self.intelligence_orchestrator.run(
            organization_id,
            workspace_id,
            person_id,
            actor_id=None,
            runbook_id=runbook_id,
            profile_ids=profile_ids,
            query=query or None,
            capabilities=capabilities,
            iterations=1,
        )
        trace_id = str(result["trace_id"])
        trace = self.record_trace(
            organization_id,
            agent_id,
            run_id,
            "request_review",
            "Read-only expert review requested",
            {"orchestrator_trace_id": trace_id, "status": result.get("status")},
        )
        self.conn.execute(
            "UPDATE agent_tasks SET orchestrator_trace_id=? WHERE id=? AND orchestrator_trace_id IS NULL",
            (trace_id, run["task_id"]),
        )
        self.conn.commit()
        return {"run_id": run_id, "trace_id": trace_id, "review": result, "run_trace": trace}

    def complete_run(
        self,
        organization_id: str,
        agent_id: str,
        run_id: str,
        content: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float | None = None,
        source_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        run = self._run(organization_id, agent_id, run_id)
        now = _now()
        started = datetime.fromisoformat(run["started_at"])
        runtime = int((now - started).total_seconds() * 1000)
        output_id = self.new_id("output")
        self.conn.execute(
            "INSERT INTO run_outputs VALUES (?,?,?,?,?,?)",
            (output_id, run_id, "text", content, _json(source_refs or []), now.isoformat()),
        )
        self.conn.execute(
            """UPDATE agent_runs SET status='completed',completed_at=?,runtime_ms=?,input_tokens=?,
            output_tokens=?,cost=?,output_id=? WHERE id=?""",
            (now.isoformat(), runtime, input_tokens, output_tokens, cost, output_id, run_id),
        )
        self.conn.execute("UPDATE agent_tasks SET status='completed',completed_at=? WHERE id=?", (now.isoformat(), run["task_id"]))
        self.conn.execute("UPDATE agents SET status='idle',current_task_id=NULL WHERE id=?", (agent_id,))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())

    def fail_run(
        self,
        organization_id: str,
        agent_id: str,
        run_id: str,
        kind: str,
        message: str,
        detail: str = "",
        retryable: bool = False,
    ) -> dict[str, Any]:
        run = self._run(organization_id, agent_id, run_id)
        now = _now().isoformat()
        error_id = self.new_id("runerror")
        self.conn.execute(
            "INSERT INTO run_errors VALUES (?,?,?,?,?,?,?)",
            (error_id, run_id, kind, message, detail, int(retryable), now),
        )
        self.conn.execute("UPDATE agent_runs SET status='failed',completed_at=?,error_id=? WHERE id=?", (now, error_id, run_id))
        self.conn.execute("UPDATE agent_tasks SET status='failed',completed_at=? WHERE id=?", (now, run["task_id"]))
        self.conn.execute("UPDATE agents SET status='error',current_task_id=NULL WHERE id=?", (agent_id,))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())

    def command_center(self, organization_id: str, person_id: str) -> dict[str, Any]:
        if self.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        visible = self.visible_workspace_ids(organization_id, person_id)
        agents = [self._redacted_agent(row, visible) for row in self.conn.execute("SELECT * FROM agents WHERE organization_id=? ORDER BY name", (organization_id,)).fetchall()]
        clause, scope_values = self._visible_workspace_clause("workspace_id", visible)
        runs = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM agent_runs WHERE organization_id=? AND {clause} ORDER BY started_at DESC LIMIT 25",
            (organization_id, *scope_values),
        ).fetchall()]
        return {
            "agents": agents,
            "recent_runs": runs,
            "supervised_action_catalog": self.supervised_action_catalog(organization_id, person_id),
            "running": sum(run["status"] == "running" for run in runs),
            "failed": sum(run["status"] == "failed" for run in runs),
            "token_cost": sum((run["cost"] or 0) for run in runs),
        }

    def supervised_action_catalog(self, organization_id: str, person_id: str) -> list[dict[str, Any]]:
        if self.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        return [dict(item) for item in supervised_action_catalog()]

    def list_runs(
        self,
        organization_id: str,
        person_id: str,
        workspace_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        visible = self.visible_workspace_ids(organization_id, person_id)
        if workspace_id is not None and workspace_id not in visible:
            raise AuthorizationError("workspace membership required")
        where = ["organization_id=?"]
        values: list[Any] = [organization_id]
        if workspace_id is not None:
            where.append("workspace_id=?")
            values.append(workspace_id)
        elif visible:
            marks = ",".join("?" for _ in visible)
            where.append(f"(workspace_id IS NULL OR workspace_id IN ({marks}))")
            values.extend(sorted(visible))
        else:
            where.append("workspace_id IS NULL")
        if agent_id is not None:
            where.append("agent_id=?")
            values.append(agent_id)
        rows = self.conn.execute(
            f"SELECT * FROM agent_runs WHERE {' AND '.join(where)} ORDER BY started_at DESC,id DESC",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def run_detail(
        self, organization_id: str, person_id: str, run_id: str
    ) -> dict[str, Any]:
        visible = self.list_runs(organization_id, person_id)
        run = next((item for item in visible if item["id"] == run_id), None)
        if run is None:
            raise NotFoundError("agent run not found")
        task = self.conn.execute("SELECT * FROM agent_tasks WHERE id=?", (run.get("task_id"),)).fetchone()
        output = self.conn.execute("SELECT * FROM run_outputs WHERE run_id=?", (run_id,)).fetchone()
        error = self.conn.execute("SELECT * FROM run_errors WHERE run_id=?", (run_id,)).fetchone()
        tools = self.conn.execute("SELECT * FROM tool_calls WHERE run_id=? ORDER BY started_at,id", (run_id,)).fetchall()
        traces = self.conn.execute("SELECT * FROM run_traces WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        action_executions = self._action_executions_for_run(run_id)
        return {
            "run": run,
            "task": dict(task) if task else None,
            "output": dict(output) if output else None,
            "error": dict(error) if error else None,
            "tool_calls": [dict(row) for row in tools],
            "traces": [dict(row) for row in traces],
            "action_executions": action_executions,
            "action_execution_boundary": self._action_execution_boundary(dict(task) if task else None, action_executions),
        }

    def _action_executions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id,organization_id,workspace_id,agent_id,run_id,task_id,approval_request_id,
                      action,action_kind,idempotency_key,descriptor_hash,payload_hash,status,
                      result_json,error_json,created_at,completed_at
               FROM agent_action_executions
               WHERE run_id=?
               ORDER BY created_at DESC,id DESC""",
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = _loads(item.pop("result_json"), None)
            item["error"] = _loads(item.pop("error_json"), None)
            item["replay_state"] = self._action_replay_state(item)
            result.append(item)
        return result

    @staticmethod
    def _action_replay_state(execution: dict[str, Any]) -> str:
        if execution["status"] == "succeeded":
            return "idempotent_replay_returns_recorded_result"
        if execution["status"] == "running":
            return "blocked_active_execution"
        if execution["status"] == "failed":
            return "blocked_failed_execution_requires_new_approval_or_idempotency_key"
        return "unknown"

    def _action_execution_boundary(self, task: dict[str, Any] | None, executions: list[dict[str, Any]]) -> dict[str, Any]:
        if task is None or not task.get("action_descriptor_json"):
            return {"status": "not_applicable", "requires_approval": False}
        if not executions:
            return {
                "status": "approved_action_not_started",
                "requires_approval": True,
                "replay_state": "not_started",
            }
        latest = executions[0]
        return {
            "status": latest["status"],
            "requires_approval": True,
            "action": latest["action"],
            "action_kind": latest["action_kind"],
            "idempotency_key": latest["idempotency_key"],
            "replay_state": latest["replay_state"],
            "operator_next_step": _action_operator_next_step(latest["status"]),
        }

    def _run(self, organization_id: str, agent_id: str, run_id: str) -> Any:
        row = self.conn.execute(
            "SELECT * FROM agent_runs WHERE organization_id=? AND agent_id=? AND id=?",
            (organization_id, agent_id, run_id),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise ValidationError("running agent run required")
        return row

