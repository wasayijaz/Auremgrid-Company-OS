from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import (
    AGENT_LEVEL_ORDER,
    CAPABILITY_LEVELS,
    LEVEL_DEFINITIONS,
    AgentLevel,
    effective_capability_tags,
    normalize_agent_level,
)
from auremgrid.services.reversible_actions import (
    ACTION_KINDS,
    supervised_action_catalog,
    validate_approved_action_descriptor,
    validate_reversible_action_descriptor,
)


MAX_AGENT_DELEGATION_DEPTH = 3


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _action_operator_next_step(status: str) -> str:
    if status == "succeeded":
        return "No action needed; identical replays return the recorded local result."
    if status == "running":
        return "Wait for the active fenced execution to finish before retrying."
    if status == "failed":
        return "Review the recorded error and create a new approved task or idempotency key before retrying."
    return "Review execution status before taking another action."


class AgentOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], company: Any, approvals: Any, client_ops: Any, capacity: Any | None = None) -> None:
        self.conn, self.new_id, self.company, self.approvals, self.client_ops, self.capacity = conn, new_id, company, approvals, client_ops, capacity
        self.intelligence_orchestrator: Any | None = None

    @staticmethod
    def _can(capabilities: Any, capability: str) -> bool:
        return capabilities is None or capability in set(capabilities)

    def visible_workspace_ids(self, organization_id: str, person_id: str) -> set[str]:
        """Return only workspaces in *organization_id* that this person belongs to.

        Workspace memberships are organization-scoped through ``workspace_organization``;
        joining that table here prevents a person who belongs to workspaces in another
        organization from accidentally widening an agent response.
        """
        rows = self.conn.execute(
            """SELECT wm.workspace_id
               FROM workspace_memberships wm
               JOIN workspace_organization wo ON wo.workspace_id=wm.workspace_id
               WHERE wm.person_id=? AND wo.organization_id=?""",
            (person_id, organization_id),
        ).fetchall()
        return {str(row[0]) for row in rows}

    @staticmethod
    def _visible_workspace_clause(column: str, visible: set[str]) -> tuple[str, list[Any]]:
        if not visible:
            return f"{column} IS NULL", []
        marks = ",".join("?" for _ in visible)
        return f"({column} IS NULL OR {column} IN ({marks}))", sorted(visible)

    @staticmethod
    def _redacted_agent(row: Any, visible: set[str]) -> dict[str, Any]:
        agent = dict(row)
        try:
            allowed = json.loads(agent.get("allowed_workspace_ids") or "[]")
        except (TypeError, ValueError):
            allowed = []
        agent["allowed_workspace_ids"] = _json([str(item) for item in allowed if str(item) in visible])
        return agent

    def seed_primary_agents(self, organization_id: str, owner_person_id: str) -> list[dict[str, Any]]:
        if self.company.org_membership(organization_id, owner_person_id) is None:
            raise AuthorizationError("organization membership required")
        definitions = (
            ("Sol", "strategic_reviewer", "Review strategy, architecture, and risk", [], AgentLevel.L3_REASON),
            ("Terra", "builder", "Implement and verify deep product work", ["domain.write", "code.write"], AgentLevel.L2_BUILD),
            ("Luna", "operator", "Execute operations and consistency work", ["domain.write"], AgentLevel.L1_OPERATE),
        )
        agents = []
        for name, role, description, writes, level in definitions:
            existing = self.conn.execute(
                "SELECT * FROM agents WHERE organization_id=? AND name=?",
                (organization_id, name),
            ).fetchone()
            if existing:
                capability_tags = _json(list(effective_capability_tags(level)))
                if existing["level"] != level.value or existing["capability_tags"] != capability_tags:
                    self.conn.execute(
                        "UPDATE agents SET level=?,capability_tags=? WHERE organization_id=? AND id=?",
                        (level.value, capability_tags, organization_id, existing["id"]),
                    )
                    existing = self.conn.execute(
                        "SELECT * FROM agents WHERE organization_id=? AND id=?",
                        (organization_id, existing["id"]),
                    ).fetchone()
                agents.append(dict(existing))
                continue
            role_id = self.new_id("agentrole")
            self.conn.execute(
                """INSERT INTO agent_roles(
                    id,organization_id,name,description,default_tools,default_write_permissions
                ) VALUES (?,?,?,?,?,?)""",
                (role_id, organization_id, role, description, _json([]), _json(writes)),
            )
            item = {
                "id": self.new_id("agent"),
                "organization_id": organization_id,
                "name": name,
                "role_id": role_id,
                "model": "unconfigured",
                "tools": _json([]),
                "allowed_workspace_ids": _json([]),
                "memory_access": "proposal_only",
                "write_permissions": _json(writes),
                "level": level.value,
                "capability_tags": _json(list(effective_capability_tags(level))),
                "status": "idle",
                "current_task_id": None,
                "created_at": _now().isoformat(),
            }
            self.conn.execute(
                """INSERT INTO agents(
                    id,organization_id,name,role_id,model,tools,allowed_workspace_ids,memory_access,
                    write_permissions,level,capability_tags,status,current_task_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(item.values()),
            )
            agents.append(item)
        self.conn.commit()
        return agents

    def configure_agent(
        self,
        organization_id: str,
        owner_person_id: str,
        agent_id: str,
        model: str,
        tools: list[str],
        allowed_workspace_ids: list[str],
        write_permissions: list[str],
    ) -> dict[str, Any]:
        membership = self.company.org_membership(organization_id, owner_person_id)
        if membership is None or membership.role not in {"owner", "admin"}:
            raise AuthorizationError("organization admin required")
        for workspace_id in allowed_workspace_ids:
            scope = self.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != organization_id:
                raise ValidationError("agent workspace must belong to organization")
        self.conn.execute(
            """UPDATE agents SET model=?,tools=?,allowed_workspace_ids=?,write_permissions=?
            WHERE organization_id=? AND id=?""",
            (
                model,
                _json(tools),
                _json(allowed_workspace_ids),
                _json(write_permissions),
                organization_id,
                agent_id,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM agents WHERE organization_id=? AND id=?",
            (organization_id, agent_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("agent not found")
        return dict(row)

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

    def resolve_level(self, intent_tags: Sequence[str]) -> AgentLevel:
        required_tags = set(self.validate_capability_tags(intent_tags))
        for level in AGENT_LEVEL_ORDER:
            if required_tags.issubset(effective_capability_tags(level)):
                return level
        return AgentLevel.L3_REASON

    def validate_capability_tags(self, intent_tags: Sequence[str]) -> tuple[str, ...]:
        tags = tuple(dict.fromkeys(str(tag).strip() for tag in intent_tags if str(tag).strip()))
        if not tags:
            raise ValidationError("at least one capability tag is required")
        unknown = sorted(tag for tag in tags if tag not in CAPABILITY_LEVELS)
        if unknown:
            raise ValidationError(f"unknown capability tags: {', '.join(unknown)}")
        return tags

    def _selected_level(
        self,
        recommended: AgentLevel,
        selected_level: AgentLevel | str | None,
        override_reason: str,
    ) -> AgentLevel:
        if selected_level is None:
            if override_reason.strip():
                raise ValidationError("level override reason requires a selected level")
            return recommended
        selected = self._normalize_level(selected_level)
        if selected == recommended:
            if override_reason.strip():
                raise ValidationError("level override reason requires a different selected level")
            return selected
        if recommended not in LEVEL_DEFINITIONS[selected].can_handle:
            raise ValidationError("selected level cannot de-escalate below recommended level")
        if not override_reason.strip():
            raise ValidationError("level override reason is required")
        return selected

    @staticmethod
    def _normalize_level(value: AgentLevel | str) -> AgentLevel:
        try:
            return normalize_agent_level(value)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _validated_delegation_depth(
        self,
        organization_id: str,
        workspace_id: str | None,
        parent_task_id: str | None,
        delegation_depth: int | None,
    ) -> int:
        def parsed(value: int | None) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError("delegation depth must be an integer") from exc

        if parent_task_id is None:
            depth = 0 if delegation_depth is None else parsed(delegation_depth)
            if depth != 0:
                raise ValidationError("root agent tasks must use delegation depth 0")
        else:
            parent = self.conn.execute(
                "SELECT organization_id,workspace_id,delegation_depth FROM agent_tasks WHERE organization_id=? AND id=?",
                (organization_id, parent_task_id),
            ).fetchone()
            if parent is None:
                raise NotFoundError("parent agent task not found")
            if parent["workspace_id"] != workspace_id:
                raise AuthorizationError("delegated agent task must stay in the parent workspace")
            depth = int(parent["delegation_depth"]) + 1
            requested_depth = parsed(delegation_depth)
            if requested_depth is not None and requested_depth != depth:
                raise ValidationError("delegation depth must be parent depth plus one")
        if depth < 0 or depth > MAX_AGENT_DELEGATION_DEPTH:
            raise ValidationError("agent delegation depth exceeds configured bound")
        return depth

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
            output = self._execute_actions(organization_id, run, actions, json.loads(run["trigger_payload"]))
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

    def _execute_actions(self, organization_id: str, automation: Any, actions: list[Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
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
                result = self._execute_automation_canonical_action(organization_id, automation, person_id, validated)
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

    def generate_report(self, organization_id: str, person_id: str, type: str, workspace_id: str | None = None) -> dict[str, Any]:
        if self.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        if workspace_id and workspace_id not in self.visible_workspace_ids(organization_id, person_id):
            raise AuthorizationError("report workspace is not visible to caller")
        allowed = {
            "daily_owner_brief",
            "weekly_agency_brief",
            "client_weekly_report",
            "campaign_report",
            "workload_report",
            "capacity_report",
            "revenue_report",
            "churn_risk_report",
            "creative_performance_report",
        }
        if type not in allowed:
            raise ValidationError("unsupported report type")
        payload: dict[str, Any] = {"type": type}
        citations = []
        if type == "churn_risk_report":
            rows = self.conn.execute(
                "SELECT id,workspace_id,severity,evidence,recommended_action FROM risks WHERE organization_id=? AND type='churn' AND status='open'",
                (organization_id,),
            ).fetchall()
            payload["risks"] = [dict(row) for row in rows]
            citations = [{"table": "risks", "id": row["id"]} for row in rows]
        elif type == "capacity_report":
            if self.capacity is None:
                raise ValidationError("capacity service unavailable")
            board = self.capacity.weekly_board(organization_id, person_id, None, workspace_id)
            payload["capacity"] = board
            citations = [
                {"table": "availability", "organization_id": organization_id},
                {"table": "leave_records", "organization_id": organization_id},
                {"table": "work_items", "organization_id": organization_id},
                {"table": "work_versions", "organization_id": organization_id},
                {"table": "time_entries", "organization_id": organization_id},
                {"table": "workflow_runs", "organization_id": organization_id},
                {"table": "workflow_stage_runs", "organization_id": organization_id},
                {"table": "workflow_transition_history", "organization_id": organization_id},
                {"table": "client_account_rosters", "organization_id": organization_id},
                {"table": "client_account_roster_roles", "organization_id": organization_id},
            ]
        elif type == "revenue_report":
            payload = self.approvals.finance_status(organization_id, person_id, workspace_id)
            citations = [{"table": "finance_connections", "organization_id": organization_id}]
        elif type in {"daily_owner_brief", "weekly_agency_brief"}:
            payload.update(
                {
                    "clients": self.conn.execute(
                        "SELECT COUNT(*) FROM workspace_organization WHERE organization_id=? AND kind='client'",
                        (organization_id,),
                    ).fetchone()[0],
                    "open_work": self.conn.execute(
                        """SELECT COUNT(*) FROM work_items wi JOIN workspace_organization wo ON wo.workspace_id=wi.workspace_id
                        WHERE wo.organization_id=? AND wi.status!='shipped'""",
                        (organization_id,),
                    ).fetchone()[0],
                    "open_risks": self.conn.execute(
                        "SELECT COUNT(*) FROM risks WHERE organization_id=? AND status='open'",
                        (organization_id,),
                    ).fetchone()[0],
                    "open_reviews": self.conn.execute(
                        "SELECT COUNT(*) FROM reviews WHERE organization_id=? AND status='open'",
                        (organization_id,),
                    ).fetchone()[0],
                }
            )
            citations = [{"table": name, "organization_id": organization_id} for name in ("workspace_organization", "work_items", "risks", "reviews")]
        elif type == "client_weekly_report":
            if not workspace_id:
                raise ValidationError("client weekly report requires workspace_id")
            workspace = self.company.workspace_scope(workspace_id)
            if workspace is None or workspace["organization_id"] != organization_id:
                raise AuthorizationError("workspace not available")
            work = [dict(row) for row in self.conn.execute("SELECT id,title,status,updated_at FROM work_items WHERE workspace_id=? ORDER BY updated_at DESC", (workspace_id,)).fetchall()]
            risks = [dict(row) for row in self.conn.execute("SELECT id,type,severity,status,evidence FROM risks WHERE workspace_id=?", (workspace_id,)).fetchall()]
            decisions = [dict(row) for row in self.conn.execute("SELECT id,statement,rationale,effective_from FROM decisions WHERE workspace_id=?", (workspace_id,)).fetchall()]
            payload.update({"work": work, "risks": risks, "decisions": decisions})
            citations = [{"table": "work_items", "id": row["id"]} for row in work]
            citations += [{"table": "risks", "id": row["id"]} for row in risks]
            citations += [{"table": "decisions", "id": row["id"]} for row in decisions]
        elif type == "campaign_report":
            sql = """SELECT c.id,c.name,c.platform,c.status,m.id metric_id,m.captured_at,m.spend,m.revenue,m.leads,m.ctr,m.cvr,m.roas,m.source
                FROM campaigns c LEFT JOIN campaign_metric_snapshots m ON m.id=(SELECT id FROM campaign_metric_snapshots WHERE campaign_id=c.id ORDER BY captured_at DESC LIMIT 1)
                WHERE c.organization_id=?""" + (" AND c.workspace_id=?" if workspace_id else "")
            values = [organization_id] + ([workspace_id] if workspace_id else [])
            rows = self.conn.execute(sql, values).fetchall()
            payload["campaigns"] = [dict(row) for row in rows]
            citations = [{"table": "campaigns", "id": row["id"], "metric_id": row["metric_id"]} for row in rows]
        elif type == "workload_report":
            rows = self.conn.execute(
                """SELECT p.id,p.name,COUNT(w.id) open_work,COALESCE(SUM(w.estimate_hours),0) estimated_hours
                FROM people p LEFT JOIN work_items w ON w.assignee_person_id=p.id AND w.status!='shipped'
                WHERE p.organization_id=? GROUP BY p.id,p.name ORDER BY estimated_hours DESC""",
                (organization_id,),
            ).fetchall()
            payload["people"] = [dict(row) for row in rows]
            citations = [{"table": "people", "id": row["id"]} for row in rows]
        elif type == "creative_performance_report":
            rows = self.conn.execute(
                """SELECT ca.id,ca.title,ca.approval_state,cp.captured_at,cp.ctr,cp.cvr,cp.roas,cp.source
                FROM creative_assets ca LEFT JOIN creative_performance cp ON cp.id=(SELECT id FROM creative_performance WHERE asset_id=ca.id ORDER BY captured_at DESC LIMIT 1)
                WHERE ca.organization_id=?""" + (" AND ca.workspace_id=?" if workspace_id else ""),
                [organization_id] + ([workspace_id] if workspace_id else []),
            ).fetchall()
            payload["creative"] = [dict(row) for row in rows]
            citations = [{"table": "creative_assets", "id": row["id"]} for row in rows]
        if not citations:
            citations = [{"table": "canonical_ledger", "organization_id": organization_id, "workspace_id": workspace_id, "result": "no matching records"}]
        item = {
            "id": self.new_id("report"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "type": type,
            "requested_by_person_id": person_id,
            "status": "completed",
            "payload": _json(payload),
            "citations": _json(citations),
            "generated_at": _now().isoformat(),
        }
        self.conn.execute("INSERT INTO report_runs VALUES (?,?,?,?,?,?,?,?,?)", tuple(item.values()))
        self.conn.commit()
        return {**item, "payload": payload, "citations": citations}
