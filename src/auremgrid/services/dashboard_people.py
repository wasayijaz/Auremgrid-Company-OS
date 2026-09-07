from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError

from .dashboard_shared import _json_list


class DashboardPeopleMixin:
    def person_detail(self, organization_id: str, viewer_person_id: str, target_person_id: str, workspace_id: str | None = None, week_start: str | None = None) -> dict[str, Any]:
        viewer_membership = self.os.company.org_membership(organization_id, viewer_person_id)
        if viewer_membership is None:
            raise AuthorizationError("organization membership required")
        if viewer_membership.role == "client":
            raise AuthorizationError("people directory requires agency membership")
        person = self.conn.execute("SELECT * FROM people WHERE organization_id=? AND id=?", (organization_id, target_person_id)).fetchone()
        if person is None: raise NotFoundError("person not found")
        if workspace_id:
            self.os._require_person_access(organization_id, workspace_id, viewer_person_id)
            if not self.conn.execute("SELECT 1 FROM workspace_memberships WHERE workspace_id=? AND person_id=?", (workspace_id, target_person_id)).fetchone(): raise NotFoundError("person not found in workspace")
        suffix = " AND workspace_id=?" if workspace_id else ""
        scope = (organization_id, target_person_id, *((workspace_id,) if workspace_id else ()))
        memberships = [dict(row) for row in self.conn.execute("""SELECT wm.workspace_id,w.name,wm.role FROM workspace_memberships wm JOIN workspaces w ON w.id=wm.workspace_id JOIN workspace_organization wo ON wo.workspace_id=w.id WHERE wo.organization_id=? AND wm.person_id=? ORDER BY w.name""", (organization_id, target_person_id)).fetchall()]
        projects = [dict(row) for row in self.conn.execute("SELECT id,workspace_id,name,status,priority,due_date,health,progress FROM projects WHERE organization_id=? AND owner_person_id=?" + suffix + " ORDER BY due_date,name", scope).fetchall()]
        work = [dict(row) for row in self.conn.execute("""SELECT wi.id,wi.workspace_id,wi.project_id,wi.title,wi.status,wi.needed_by,wi.estimate_hours,wi.actual_effort_hours FROM work_items wi JOIN workspace_organization wo ON wo.workspace_id=wi.workspace_id WHERE wo.organization_id=? AND wi.assignee_person_id=?""" + (" AND wi.workspace_id=?" if workspace_id else "") + " ORDER BY wi.needed_by,wi.title", scope).fetchall()]
        reviews = [dict(row) for row in self.conn.execute("""SELECT rv.id,rv.workspace_id,rv.status,rv.decision,rv.opened_at,rv.closed_at,d.title AS deliverable_title FROM reviews rv JOIN deliverables d ON d.id=rv.deliverable_id WHERE rv.organization_id=? AND rv.reviewer_person_id=?""" + (" AND rv.workspace_id=?" if workspace_id else "") + " ORDER BY rv.opened_at DESC", scope).fetchall()]
        skills = [dict(row) for row in self.conn.execute("SELECT s.name,s.category,ps.level FROM person_skills ps JOIN skills s ON s.id=ps.skill_id WHERE ps.person_id=? ORDER BY s.name", (target_person_id,)).fetchall()]
        leave = [dict(row) for row in self.conn.execute("SELECT start_date,end_date,hours,status FROM leave_records WHERE organization_id=? AND person_id=? ORDER BY start_date DESC", (organization_id, target_person_id)).fetchall()]
        week = week_start or datetime.now(timezone.utc).date().isoformat()
        try:
            board = self.os.capacity.weekly_board(organization_id, viewer_person_id, week, workspace_id)
            capacity = next((row for row in board.get("people", []) if str(row.get("person_id")) == str(target_person_id)), None)
        except (AuthorizationError, ValidationError): capacity = None
        return {"person": dict(person), "memberships": memberships, "projects": projects, "work": work, "reviews": reviews, "skills": skills, "leave": leave, "capacity": capacity, "capacity_status": "sourced" if capacity else "unknown", "deadlines": [row for row in work if row.get("needed_by")]}

    def agent_detail(
        self,
        organization_id: str,
        person_id: str,
        agent_id: str,
        capabilities: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if self.os.company.org_membership(organization_id, person_id) is None: raise AuthorizationError("organization membership required")
        visible = self.os.agent_ops.visible_workspace_ids(organization_id, person_id)
        agent = self.conn.execute("SELECT * FROM agents WHERE organization_id=? AND id=?", (organization_id, agent_id)).fetchone()
        if agent is None: raise NotFoundError("agent not found")
        try:
            allowed = [str(item) for item in _json_list(agent["allowed_workspace_ids"])]
        except (TypeError, ValueError):
            allowed = []
        # An agent explicitly scoped only to workspaces the viewer cannot access is
        # indistinguishable from a missing agent; do not leak its existence or metadata.
        if allowed and not (set(allowed) & visible):
            raise NotFoundError("agent not found")
        role = self.conn.execute("SELECT * FROM agent_roles WHERE id=? AND organization_id=?", (agent["role_id"], organization_id)).fetchone()
        scope_clause, scope_values = self.os.agent_ops._visible_workspace_clause("workspace_id", visible)
        tasks = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM agent_tasks WHERE organization_id=? AND agent_id=? AND {scope_clause} ORDER BY created_at DESC LIMIT 25",
            (organization_id, agent_id, *scope_values),
        ).fetchall()]
        # Queue items carry their scope through the task, so filter via the task join.
        queue = [dict(row) for row in self.conn.execute(
            f"SELECT q.* FROM agent_queue_items q JOIN agent_tasks t ON t.id=q.task_id WHERE q.organization_id=? AND q.agent_id=? AND {scope_clause.replace('workspace_id', 't.workspace_id')} ORDER BY q.enqueued_at DESC LIMIT 25",
            (organization_id, agent_id, *scope_values),
        ).fetchall()]
        runs = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM agent_runs WHERE organization_id=? AND agent_id=? AND {scope_clause} ORDER BY started_at DESC LIMIT 25",
            (organization_id, agent_id, *scope_values),
        ).fetchall()]
        completed = sum(row.get("status") == "completed" for row in runs); failed = sum(row.get("status") == "failed" for row in runs); finished = completed + failed
        scoped_allowed = [item for item in allowed if item in visible]
        action_workspace = scoped_allowed[0] if scoped_allowed else None
        return {"agent": {**self.os.agent_ops._redacted_agent(agent, visible), "capability": {"role": role["name"] if role else "Unknown", "description": role["description"] if role else "Unknown"}, "tools": _json_list(agent["tools"]), "write_permissions": _json_list(agent["write_permissions"]), "allowed_workspace_ids": scoped_allowed}, "current_task": next((row for row in tasks if row.get("status") in {"running", "queued"}), None), "tasks": tasks, "queue": queue, "runs": runs, "quality": {"completed": completed, "failed": failed, "success_rate": completed / finished if finished else None}, "cost": {"total": sum(float(row.get("cost") or 0) for row in runs), "currency": "USD", "status": "sourced" if runs else "unknown"}, "budget": {"status": "not_configured", "amount": None, "currency": "USD"}, "allowed_actions": self.os.agent_ops.agent_action_descriptors(organization_id, person_id, agent_id, action_workspace, capabilities)}
