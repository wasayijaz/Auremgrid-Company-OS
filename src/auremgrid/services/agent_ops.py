from __future__ import annotations

import json
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


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


class AgentOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], company: Any, approvals: Any, client_ops: Any) -> None:
        self.conn, self.new_id, self.company, self.approvals, self.client_ops = conn, new_id, company, approvals, client_ops

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
    ) -> dict[str, Any]:
        if self.company.org_membership(organization_id, requested_by_person_id) is None:
            raise AuthorizationError("organization membership required")
        agent = self.conn.execute(
            "SELECT * FROM agents WHERE organization_id=? AND id=?",
            (organization_id, agent_id),
        ).fetchone()
        if agent is None:
            raise NotFoundError("agent not found")
        if workspace_id and workspace_id not in json.loads(agent["allowed_workspace_ids"]):
            raise AuthorizationError("agent cannot access workspace")

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
            "approval_request_id": None,
            "intent_tags": _json(tags),
            "recommended_level": recommended.value,
            "selected_level": selected.value,
            "level_override_reason": override_reason.strip() if selected != recommended else None,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
        }
        self.conn.execute(
            """INSERT INTO agent_tasks(
                id,organization_id,workspace_id,agent_id,title,instructions,priority,status,
                approval_request_id,intent_tags,recommended_level,selected_level,level_override_reason,
                created_at,started_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    def start_run(self, organization_id: str, agent_id: str, task_id: str) -> dict[str, Any]:
        task = self.conn.execute(
            "SELECT * FROM agent_tasks WHERE organization_id=? AND agent_id=? AND id=?",
            (organization_id, agent_id, task_id),
        ).fetchone()
        if task is None or task["status"] != "queued":
            raise ValidationError("queued agent task required")
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
        }
        self.conn.execute("INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(run.values()))
        self.conn.execute("UPDATE agent_tasks SET status='running',started_at=? WHERE id=?", (now, task_id))
        self.conn.execute("UPDATE agents SET status='running',current_task_id=? WHERE id=?", (task_id, agent_id))
        self.conn.execute("UPDATE agent_queue_items SET status='claimed',claimed_at=? WHERE task_id=?", (now, task_id))
        self.conn.commit()
        return run

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
        agents = [dict(row) for row in self.conn.execute("SELECT * FROM agents WHERE organization_id=? ORDER BY name", (organization_id,)).fetchall()]
        runs = [dict(row) for row in self.conn.execute("SELECT * FROM agent_runs WHERE organization_id=? ORDER BY started_at DESC LIMIT 25", (organization_id,)).fetchall()]
        return {
            "agents": agents,
            "recent_runs": runs,
            "running": sum(run["status"] == "running" for run in runs),
            "failed": sum(run["status"] == "failed" for run in runs),
            "token_cost": sum((run["cost"] or 0) for run in runs),
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
            needs_approval = automation["status"] == "training" or automation["approval_policy"] != "auto" or any(action["one_way"] for action in actions)
            run_id = self.new_id("automationrun")
            now = _now().isoformat()
            approval_id = None
            status = "waiting_approval" if needs_approval else "completed"
            if needs_approval:
                approval = self.approvals.request_approval(
                    organization_id,
                    "automation",
                    automation["id"],
                    "automation run",
                    "automation.execute",
                    payload,
                    "Training mode or gated action",
                    approver_person_id=automation["created_by_person_id"],
                )
                approval_id = approval["id"]
            output = self._execute_actions(organization_id, automation, actions, payload) if status == "completed" else {}
            self.conn.execute(
                "INSERT INTO automation_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, automation["id"], trigger_type, _json(payload), status, now, now if status == "completed" else None, approval_id, _json(output)),
            )
            results.append({"run_id": run_id, "status": status, "approval_request_id": approval_id, "output": output})
        self.conn.commit()
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
        run = self.conn.execute(
            """SELECT ar.*,a.created_by_person_id,a.organization_id
            FROM automation_runs ar JOIN automations a ON a.id=ar.automation_id
            WHERE a.organization_id=? AND ar.id=?""",
            (organization_id, run_id),
        ).fetchone()
        if run is None:
            raise NotFoundError("automation run not found")
        approval = self.conn.execute("SELECT status FROM approval_requests WHERE id=?", (run["approval_request_id"],)).fetchone()
        if run["status"] != "waiting_approval" or approval is None or approval["status"] != "approved":
            raise AuthorizationError("approved automation run required")
        actions = self.conn.execute("SELECT * FROM automation_actions WHERE automation_id=? ORDER BY sequence", (run["automation_id"],)).fetchall()
        payload = json.loads(run["trigger_payload"])
        output = self._execute_actions(organization_id, run, actions, payload)
        now = _now().isoformat()
        self.conn.execute("UPDATE automation_runs SET status='completed',completed_at=?,output=? WHERE id=?", (now, _json(output), run_id))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone())

    def _execute_actions(self, organization_id: str, automation: Any, actions: list[Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        output = []
        person_id = automation["created_by_person_id"]
        for action in actions:
            config = json.loads(action["config"])
            workspace_id = config.get("workspace_id") or payload.get("workspace_id")
            if action["type"] == "risk.create":
                if not workspace_id:
                    raise ValidationError("risk automation requires workspace_id")
                risk = self.client_ops.create_risk(
                    organization_id,
                    workspace_id,
                    person_id,
                    config.get("type", "relationship"),
                    config.get("severity", "medium"),
                    float(config.get("probability", 0.5)),
                    config.get("impact", payload.get("reason", "Automation signal")),
                    _json(payload),
                    config.get("recommended_action", "Account lead review"),
                )
                output.append({"type": "risk", "id": risk.id})
            elif action["type"] == "notification.create":
                recipient = config.get("recipient_person_id") or person_id
                notice = self.approvals.create_notification(
                    organization_id,
                    recipient,
                    config.get("reason", payload.get("reason", "Automation signal")),
                    "automation",
                    automation["id"],
                    workspace_id,
                    float(config.get("severity", 0.5)),
                    float(config.get("urgency", 0.5)),
                )
                output.append({"type": "notification", "id": notice["id"]})
            else:
                raise ValidationError(f"unsupported automation action: {action['type']}")
        return output

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
            rows = self.conn.execute(
                "SELECT * FROM capacity_snapshots WHERE organization_id=? ORDER BY calculated_at DESC",
                (organization_id,),
            ).fetchall()
            payload["capacity"] = [dict(row) for row in rows]
            citations = [{"table": "capacity_snapshots", "id": row["id"]} for row in rows]
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
