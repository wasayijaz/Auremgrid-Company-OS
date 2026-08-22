from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity


class _ExistingDashboardService:
    def __init__(self, os: Any) -> None: self.os=os; self.conn=os.store.conn

    def command(self, organization_id: str, person_id: str) -> dict[str, Any]:
        if self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
        workspaces=self.conn.execute("""SELECT w.id,w.name,wo.kind,wm.role FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
            JOIN workspace_memberships wm ON wm.workspace_id=w.id WHERE wo.organization_id=? AND wm.person_id=?""",(organization_id,person_id)).fetchall()
        ids=[row["id"] for row in workspaces]; placeholders=",".join("?" for _ in ids) or "NULL"
        def count(table: str, clause: str="1=1") -> int:
            if not ids:return 0
            return int(self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IN ({placeholders}) AND {clause}",ids).fetchone()[0])
        active_clients=sum(row["kind"]=="client" for row in workspaces); open_work=count("work_items","status!='shipped'")
        overdue=count("work_items","status!='shipped' AND needed_by IS NOT NULL AND needed_by < date('now')")
        review=count("reviews","status='open'"); risks=count("risks","status='open'")
        active_workflows=count("workflow_runs","status NOT IN ('completed','cancelled')")
        agents=[dict(r) for r in self.conn.execute("SELECT * FROM agents WHERE organization_id=? ORDER BY name",(organization_id,)).fetchall()]
        automation_count=self.conn.execute("SELECT COUNT(*) FROM automation_runs ar JOIN automations a ON a.id=ar.automation_id WHERE a.organization_id=? AND ar.started_at>=date('now')",(organization_id,)).fetchone()[0]
        finance=self.os.agency_ops.finance_status(organization_id,person_id)
        attention=[{**item,"client":None,"severity":"ranked","evidence":item["reason"],"owner":person_id,"next_action":"Open source record"}
            for item in self.os.agency_ops.attention(organization_id,person_id,10)]
        if ids:
            overdue_rows=self.conn.execute(f"""SELECT wi.id,wi.title,wi.needed_by,wi.assignee_person_id,w.name client
                FROM work_items wi JOIN workspaces w ON w.id=wi.workspace_id
                WHERE wi.workspace_id IN ({placeholders}) AND wi.status!='shipped' AND wi.needed_by IS NOT NULL
                AND wi.needed_by < date('now') ORDER BY wi.needed_by""",ids).fetchall()
            attention.extend({"id":row["id"],"priority":.92,"reason":f"{row['title']} is overdue","source_type":"work_item",
                "source_id":row["id"],"client":row["client"],"severity":"high","evidence":f"Due {row['needed_by']}",
                "owner":row["assignee_person_id"],"next_action":"Replan or complete the work"} for row in overdue_rows)
            risk_rows=self.conn.execute(f"""SELECT r.*,w.name client FROM risks r JOIN workspaces w ON w.id=r.workspace_id
                WHERE r.workspace_id IN ({placeholders}) AND r.status='open' ORDER BY CASE r.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC""",ids).fetchall()
            attention.extend({"id":row["id"],"priority":.98 if row["severity"]=="critical" else .88,"reason":row["impact"],
                "source_type":"risk","source_id":row["id"],"client":row["client"],"severity":row["severity"],
                "evidence":row["evidence"],"owner":row["owner_person_id"],"next_action":row["recommended_action"]} for row in risk_rows)
            stalled=self.conn.execute(f"""SELECT rv.id,rv.opened_at,w.name client FROM reviews rv JOIN workspaces w ON w.id=rv.workspace_id
                WHERE rv.workspace_id IN ({placeholders}) AND rv.status='open' AND rv.opened_at < datetime('now','-48 hours')""",ids).fetchall()
            attention.extend({"id":row["id"],"priority":.84,"reason":"Review has been open over 48 hours","source_type":"review",
                "source_id":row["id"],"client":row["client"],"severity":"medium","evidence":f"Opened {row['opened_at']}",
                "owner":None,"next_action":"Assign a reviewer or close the review"} for row in stalled)
        attention=sorted(attention,key=lambda item:item["priority"],reverse=True)[:3]
        clients=[]
        for ws in workspaces:
            if ws["kind"]!="client":continue
            health=self.conn.execute("SELECT * FROM client_health_snapshots WHERE workspace_id=? ORDER BY calculated_at DESC LIMIT 1",(ws["id"],)).fetchone()
            latest=self.conn.execute("SELECT occurred_at FROM touchpoints WHERE workspace_id=? ORDER BY occurred_at DESC LIMIT 1",(ws["id"],)).fetchone()
            clients.append({"id":ws["id"],"name":ws["name"],"role":ws["role"],"health":health["overall"] if health else None,
                "health_trend":health["trend"] if health else None,"open_work":self.conn.execute("SELECT COUNT(*) FROM work_items WHERE workspace_id=? AND status!='shipped'",(ws["id"],)).fetchone()[0],
                "reviews":self.conn.execute("SELECT COUNT(*) FROM reviews WHERE workspace_id=? AND status='open'",(ws["id"],)).fetchone()[0],
                "risks":self.conn.execute("SELECT COUNT(*) FROM risks WHERE workspace_id=? AND status='open'",(ws["id"],)).fetchone()[0],
                "last_touch":latest[0] if latest else None})
        pulse=[]
        for row in self.conn.execute("SELECT workspace_id,action,target,detail,recorded_at FROM audit_events WHERE workspace_id IN ("+placeholders+") ORDER BY recorded_at DESC LIMIT 12",ids).fetchall() if ids else []:
            pulse.append(dict(row))
        return {"generated_at":datetime.now(timezone.utc).isoformat(),"metrics":{"active_clients":active_clients,"mrr":finance.get("mrr") if finance["status"]=="connected" else None,
            "finance_status":finance["status"],"open_work":open_work,"overdue_work":overdue,"in_review":review,"active_workflows":active_workflows,"agents_running":sum(a["status"]=="running" for a in agents),
            "automations_today":automation_count,"open_risks":risks},"attention":attention,"clients":clients,"agents":agents,"pulse":pulse,
            "workspaces":[dict(row) for row in workspaces],
            "identity": self._identity_view(organization_id, person_id),
            "ledger_health": self._ledger_health(organization_id, person_id),
            "capability_summary": self._capability_summary(organization_id, person_id),
            "modules": self._capability_modules(organization_id, person_id, ids),}

    def _identity_view(self, organization_id: str, person_id: str) -> dict[str, Any]:
        organization = self.conn.execute(
            "SELECT id,name,created_at FROM organizations WHERE id=?", (organization_id,)
        ).fetchone()
        person = self.conn.execute(
            """SELECT p.id,p.name,p.email,p.title,p.department,p.status,om.role AS organization_role
               FROM people p JOIN organization_memberships om ON om.person_id=p.id AND om.organization_id=p.organization_id
               WHERE p.organization_id=? AND p.id=?""", (organization_id, person_id)
        ).fetchone()
        return {
            "organization": dict(organization) if organization else {"id": organization_id, "name": organization_id},
            "person": dict(person) if person else {"id": person_id},
        }

    def _ledger_health(self, organization_id: str, person_id: str) -> dict[str, Any]:
        def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
            row = self.conn.execute(sql, params).fetchone()
            return int(row[0] or 0) if row else 0
        finance = self.os.agency_ops.finance_status(organization_id, person_id)
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()
        integrity_status = str(integrity[0]) if integrity else "unknown"
        graph_health = getattr(self.os, "graph_health", {}) or {}
        status = "healthy" if integrity_status == "ok" and graph_health.get("status", "healthy") in {"healthy", "ready"} else "degraded"
        return {
            "status": status,
            "integrity": integrity_status,
            "schema_version": self.os.store.schema_version,
            "audit_events": scalar("SELECT COUNT(*) FROM ledger_audit WHERE organization_id=?", (organization_id,)),
            "recent_audit_events": scalar(
                "SELECT COUNT(*) FROM ledger_audit WHERE organization_id=? AND recorded_at>=datetime('now','-24 hours')", (organization_id,)
            ),
            "finance": {"status": finance.get("status", "unavailable")},
        }

    def _capability_summary(self, organization_id: str, person_id: str) -> dict[str, Any]:
        return {
            "feedback_patterns": int(self.conn.execute("SELECT COUNT(*) FROM feedback_patterns WHERE organization_id=?", (organization_id,)).fetchone()[0]),
            "performance_insights": int(self.conn.execute("SELECT COUNT(*) FROM performance_insights WHERE organization_id=?", (organization_id,)).fetchone()[0]),
            "forecasts": int(self.conn.execute("SELECT COUNT(*) FROM forecasts WHERE organization_id=?", (organization_id,)).fetchone()[0]),
            "retention_policies": int(self.conn.execute("SELECT COUNT(*) FROM retention_policies WHERE organization_id=?", (organization_id,)).fetchone()[0]),
        }

    def _capability_modules(self, organization_id: str, person_id: str, workspace_ids: list[str]) -> dict[str, Any]:
        # The command payload is organization-scoped; workspace-specific modules
        # are intentionally left empty until an explicit workspace is selected.
        if not workspace_ids:
            return {"feedback": [], "performance": [], "forecasts": [], "retention": []}
        return {
            "feedback": [],
            "performance": [],
            "forecasts": self.os.forecasts.list_forecasts(organization_id, person_id),
            "retention": self.os.retention.list_policies(organization_id, person_id),
        }

    def settings(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """Return authenticated, canonical settings and system health for the operator."""
        if identity.organization_id != organization_id:
            raise AuthorizationError("dashboard scope denied")
        identity.require("workspace_read")
        if workspace_id:
            self.os._require_person_access(organization_id, workspace_id, identity.person_id, write=False)
        person = self.conn.execute(
            """SELECT p.id,p.name,p.email,p.title,p.department,p.status,om.role AS organization_role
               FROM people p JOIN organization_memberships om ON om.person_id=p.id AND om.organization_id=p.organization_id
               WHERE p.organization_id=? AND p.id=?""", (organization_id, identity.person_id)
        ).fetchone()
        memberships = [dict(row) for row in self.conn.execute(
            """SELECT w.id,w.name,wo.kind,wm.role FROM workspaces w
               JOIN workspace_organization wo ON wo.workspace_id=w.id
               JOIN workspace_memberships wm ON wm.workspace_id=w.id
               WHERE wo.organization_id=? AND wm.person_id=? ORDER BY w.name""", (organization_id, identity.person_id)
        ).fetchall()]
        pending_approvals = [dict(row) for row in self.conn.execute(
            """SELECT id,workspace_id,requested_for,action_type,reason,approver_person_id,status,created_at
               FROM approval_requests WHERE organization_id=? AND status='pending' ORDER BY created_at DESC LIMIT 20""", (organization_id,)
        ).fetchall()]
        integrations = self.os.integrations.list(identity)
        return {
            "identity": {"organization": dict(self.conn.execute("SELECT id,name,created_at FROM organizations WHERE id=?", (organization_id,)).fetchone() or {"id": organization_id, "name": organization_id}), "person": dict(person) if person else {"id": identity.person_id}},
            "workspace": next((item for item in memberships if item["id"] == workspace_id), None) if workspace_id else None,
            "workspaces": memberships,
            "permissions": {"capabilities": sorted(identity.capabilities), "scopes": sorted(identity.scopes)},
            "approvals": {"pending": pending_approvals, "pending_count": len(pending_approvals)},
            "integrations": integrations,
            "health": self._ledger_health(organization_id, identity.person_id),
        }

    def client_hq(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str, person_id: str,
    ) -> dict[str, Any]:
        """Return the client operating view, scoped by the authenticated identity.

        The original payload is retained; the operational additions are derived from
        the canonical roster, meeting-responsibility ledger, and workflow board.
        """
        self._authorize(identity, organization_id, workspace_id, person_id, "workspace_read")
        workspace = self.conn.execute(
            """SELECT w.* FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
               WHERE w.id=? AND wo.organization_id=?""", (workspace_id, organization_id)
        ).fetchone()
        if workspace is None:
            raise AuthorizationError("dashboard scope denied")
        health = self.conn.execute(
            """SELECT * FROM client_health_snapshots
               WHERE organization_id=? AND workspace_id=? ORDER BY calculated_at DESC LIMIT 1""",
            (organization_id, workspace_id),
        ).fetchone()
        work = [w.to_dict() for w in self.os.store.list_work_items(workspace_id)]
        projects = [p.to_dict() for p in self.os.company.list_projects(workspace_id)]
        reviews = [r.to_dict() for r in self.os.company.list_reviews(workspace_id)]
        risks = [dict(r) for r in self.conn.execute(
            "SELECT * FROM risks WHERE organization_id=? AND workspace_id=? AND status='open' ORDER BY detected_at DESC",
            (organization_id, workspace_id),
        ).fetchall()]
        decisions = [d.to_dict() for d in self.os.company.list_decisions(organization_id, workspace_id)]
        campaigns = [dict(r) for r in self.conn.execute(
            "SELECT * FROM campaigns WHERE organization_id=? AND workspace_id=? ORDER BY updated_at DESC",
            (organization_id, workspace_id),
        ).fetchall()]
        content = [dict(r) for r in self.conn.execute(
            "SELECT * FROM content_items WHERE organization_id=? AND workspace_id=? ORDER BY updated_at DESC",
            (organization_id, workspace_id),
        ).fetchall()]
        creative = [dict(r) for r in self.conn.execute(
            "SELECT * FROM creative_assets WHERE organization_id=? AND workspace_id=? ORDER BY created_at DESC",
            (organization_id, workspace_id),
        ).fetchall()]
        workflows = [dict(r) for r in self.conn.execute(
            """SELECT id,definition_key,definition_name,definition_version,status,due_at,updated_at
               FROM workflow_runs WHERE organization_id=? AND workspace_id=? ORDER BY updated_at DESC""",
            (organization_id, workspace_id),
        ).fetchall()]
        files = [dict(r) for r in self.conn.execute(
            """SELECT wf.id,wf.title,wf.url,wf.source,wf.created_at
               FROM work_files wf JOIN work_items wi ON wi.id=wf.work_item_id
               JOIN workspace_organization wo ON wo.workspace_id=wi.workspace_id
               WHERE wi.workspace_id=? AND wo.organization_id=?
               UNION ALL
               SELECT df.id,df.title,df.url,df.kind,df.created_at
               FROM deliverable_files df JOIN deliverables d ON d.id=df.deliverable_id
               WHERE d.workspace_id=? AND d.organization_id=?""",
            (workspace_id, organization_id, workspace_id, organization_id),
        ).fetchall()]
        meetings = [dict(r) for r in self.conn.execute(
            "SELECT * FROM meetings WHERE organization_id=? AND workspace_id=? ORDER BY occurred_at DESC",
            (organization_id, workspace_id),
        ).fetchall()]
        messages = [dict(r) for r in self.conn.execute(
            """SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
               WHERE c.organization_id=? AND c.workspace_id=? ORDER BY sent_at DESC""",
            (organization_id, workspace_id),
        ).fetchall()]
        workspace_people = [dict(r) for r in self.conn.execute(
            """SELECT p.id,p.name,p.title,p.department,wm.role FROM workspace_memberships wm
               JOIN people p ON p.id=wm.person_id
               WHERE p.organization_id=? AND wm.workspace_id=? ORDER BY p.name,p.id""",
            (organization_id, workspace_id),
        ).fetchall()]
        contacts = [dict(r) for r in self.conn.execute(
            "SELECT id,name,role,influence,decision_power,last_contact_at FROM contacts WHERE organization_id=? AND workspace_id=?",
            (organization_id, workspace_id),
        ).fetchall()]
        activity = [dict(r) for r in self.conn.execute(
            """SELECT action,entity_type,entity_id,detail,recorded_at FROM ledger_audit
               WHERE organization_id=? AND workspace_id=? ORDER BY recorded_at DESC LIMIT 50""",
            (organization_id, workspace_id),
        ).fetchall()]

        roster = self.os.client_ops.get_client_roster(organization_id, workspace_id, person_id)
        people_by_id = {str(item["id"]): item for item in workspace_people}

        def person_ref(person_id: str | None) -> dict[str, Any] | None:
            if not person_id:
                return None
            item = people_by_id.get(str(person_id))
            # Roster triggers guarantee workspace membership; keeping this guard
            # prevents a malformed legacy row from disclosing another workspace.
            if item is None:
                return None
            return {
                "id": item["id"], "person_id": item["id"], "name": item["name"],
                "title": item["title"], "department": item["department"], "role": item["role"],
            }

        slots: dict[str, Any] = {
            "dri": None, "backup": None, "account": {"lead": None, "executive": None},
            "cadence": None, "escalation": None, "wings": {},
        }
        if roster:
            for role in roster.get("roles", []):
                role_key, ref = role.get("role_key"), person_ref(role.get("person_id"))
                if role_key == "client_success_dri": slots["dri"] = ref
                elif role_key == "client_success_backup": slots["backup"] = ref
                elif role_key == "account_lead": slots["account"]["lead"] = ref
                elif role_key == "account_executive": slots["account"]["executive"] = ref
                elif role_key == "cadence_owner": slots["cadence"] = ref
                elif role_key == "escalation_owner": slots["escalation"] = ref
                elif role_key in {"wing_lead", "wing_executive"}:
                    wing = role.get("wing") or ""
                    wing_slots = slots["wings"].setdefault(wing, {"lead": None, "executive": None})
                    wing_slots["lead" if role_key == "wing_lead" else "executive"] = ref
        # Keep canonical role names available alongside the concise dashboard
        # slot names; both point at the same read-only person references.
        slots.update({
            "client_success_dri": slots["dri"],
            "client_success_backup": slots["backup"],
            "account_lead": slots["account"]["lead"],
            "account_executive": slots["account"]["executive"],
            "cadence_owner": slots["cadence"],
            "escalation_owner": slots["escalation"],
        })
        account_team = {**slots, "slots": dict(slots)}

        meeting_responsibilities = []
        for meeting in meetings:
            responsibility = self.os.client_ops.get_meeting_responsibilities(
                organization_id, workspace_id, person_id, meeting["id"]
            )
            meeting_responsibilities.append({
                "meeting_id": meeting["id"], "meeting": meeting,
                "responsibility": responsibility, **responsibility,
            })
        # Also expose a compact id-keyed form for consumers that do not need the
        # full meeting row; both are read-only projections of the same canonical data.
        meeting_responsibility_map = {
            item["meeting"]["id"]: item["responsibility"] for item in meeting_responsibilities
        }

        work_statuses = Counter(str(item.get("status", "unknown")) for item in work)
        estimated_remaining = sum(
            max(0.0, float(item.get("estimate_hours") or 0.0) - float(item.get("actual_effort_hours") or 0.0))
            for item in work if item.get("status") != "shipped"
        )
        unanswered_important = sum(
            bool(item.get("requires_reply")) and item.get("replied_at") is None and bool(item.get("important"))
            for item in messages
        )
        workflow_states = Counter(str(item.get("status", "unknown")) for item in workflows)
        active_work = [item for item in work if item.get("status") != "shipped"]
        today = datetime.now(timezone.utc).date().isoformat()
        summary = {
            "work_statuses": dict(sorted(work_statuses.items())),
            "work_status_counts": dict(sorted(work_statuses.items())),
            "estimated_remaining_hours": estimated_remaining,
            "estimated_remaining": estimated_remaining,
            "open_risks": sum(1 for item in risks if item.get("status") == "open"),
            "unanswered_important_messages": unanswered_important,
            "unanswered_important": unanswered_important,
            "workflow_states": dict(sorted(workflow_states.items())),
            "workflow_state_counts": dict(sorted(workflow_states.items())),
            "open_work": len(active_work),
            "overdue_work": sum(
                bool(item.get("deadline") or item.get("needed_by"))
                and str(item.get("deadline") or item.get("needed_by")) < today
                for item in active_work
            ),
            "blocked_work": sum(item.get("status") == "blocked" for item in active_work),
            "work_in_review": sum(
                item.get("status") in {"review", "client_review"} for item in active_work
            ),
        }

        workload_rows: dict[str, dict[str, Any]] = {}
        for item in work:
            if item.get("status") == "shipped":
                continue
            assignee = item.get("assignee_person_id") or item.get("assignee_id")
            key = str(assignee) if assignee else "unassigned"
            row = workload_rows.setdefault(key, {
                "person": person_ref(str(assignee)) if assignee else None,
                "person_id": assignee,
                "work_items": 0,
                "estimated_remaining_hours": 0.0,
            })
            row["work_items"] += 1
            row["estimated_remaining_hours"] += max(0.0, float(item.get("estimate_hours") or 0.0) - float(item.get("actual_effort_hours") or 0.0))
        workload = sorted(workload_rows.values(), key=lambda row: (row["person"] is None, str(row["person_id"] or "")))
        workload_by_person = {str(row["person_id"] or "unassigned"): row for row in workload}
        workflow_board = self.workflow_board(identity, organization_id, workspace_id, person_id)
        workflow_stages = workflow_board.get("stages", [])
        summary.update({
            "active_workflow_runs": sum(
                run.get("status") not in {"completed", "cancelled"}
                for run in workflow_board.get("runs", [])
            ),
            "ready_workflow_stages": sum(
                bool((stage.get("readiness") or {}).get("ready")) for stage in workflow_stages
            ),
            "blocked_workflow_stages": sum(stage.get("status") == "blocked" for stage in workflow_stages),
            "waiting_approval_stages": sum(
                stage.get("status") in {"waiting_approval", "approval_pending"}
                for stage in workflow_stages
            ),
            "overdue_workflow_stages": sum(
                bool((stage.get("due") or {}).get("overdue")) for stage in workflow_stages
            ),
        })
        return {
            "workspace": dict(workspace), "health": dict(health) if health else None,
            "projects": projects, "work": work, "reviews": reviews, "risks": risks,
            "decisions": decisions, "campaigns": campaigns, "content": content, "creative": creative,
            "workflows": workflows, "files": files, "meetings": meetings, "messages": messages,
            "people": workspace_people + contacts, "activity": activity,
            "finance": (
                self.os.agency_ops.finance_status(organization_id, person_id, workspace_id)
                if identity.can("finance_read") else {"status": "not_authorized"}
            ),
            "brain": (
                self.os.store.get_client_brain(workspace_id).to_dict()
                if identity.can("brain_read") and self.os.store.get_client_brain(workspace_id)
                else ({"status": "not_authorized"} if not identity.can("brain_read") else None)
            ),
            "current_roster": roster, "account_team": account_team,
            "meeting_responsibilities": meeting_responsibilities,
            "meeting_responsibility_map": meeting_responsibility_map,
            "summary": summary, "workload": workload,
            "workload_by_person": workload_by_person,
            "workflow_board": workflow_board, "readiness": workflow_board,
        }

    def review_center(self, organization_id: str, person_id: str) -> dict[str, Any]:
        """Cross-workspace review queue for the caller's own accessible workspaces.

        This never trusts a caller-supplied workspace_id: membership rows are
        the only source of which workspaces the caller may see review rows
        from, matching the read-access pattern used by command().
        """

        if self.os.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        workspace_rows = self.conn.execute(
            """SELECT w.id,w.name,wm.role FROM workspaces w
               JOIN workspace_organization wo ON wo.workspace_id=w.id
               JOIN workspace_memberships wm ON wm.workspace_id=w.id
               WHERE wo.organization_id=? AND wm.person_id=?""",
            (organization_id, person_id),
        ).fetchall()
        ids = [row["id"] for row in workspace_rows]
        names = {row["id"]: row["name"] for row in workspace_rows}
        if not ids:
            return {
                "waiting_for_me": [], "waiting_for_team": [], "waiting_for_client": [],
                "revision_requested": [], "stalled": [], "approved_today": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""SELECT rv.id,rv.workspace_id,rv.deliverable_id,rv.version,rv.kind,rv.status,
                       rv.reviewer_person_id,rv.opened_at,rv.closed_at,rv.decision,
                       d.title AS deliverable_title, d.type AS deliverable_type
                FROM reviews rv
                JOIN deliverables d ON d.id=rv.deliverable_id
                WHERE rv.workspace_id IN ({placeholders})
                ORDER BY rv.opened_at ASC""",
            ids,
        ).fetchall()
        now = datetime.now(timezone.utc)

        def row_view(row: Any) -> dict[str, Any]:
            return {
                "id": row["id"], "workspace_id": row["workspace_id"], "client": names.get(row["workspace_id"]),
                "deliverable_id": row["deliverable_id"], "deliverable_title": row["deliverable_title"],
                "deliverable_type": row["deliverable_type"], "version": row["version"], "kind": row["kind"],
                "status": row["status"], "reviewer_person_id": row["reviewer_person_id"],
                "opened_at": row["opened_at"], "closed_at": row["closed_at"], "decision": row["decision"],
            }

        waiting_for_me: list[dict[str, Any]] = []
        waiting_for_team: list[dict[str, Any]] = []
        waiting_for_client: list[dict[str, Any]] = []
        revision_requested: list[dict[str, Any]] = []
        stalled: list[dict[str, Any]] = []
        approved_today: list[dict[str, Any]] = []
        for row in rows:
            view = row_view(row)
            if row["status"] == "open":
                opened_at = row["opened_at"]
                try:
                    stalled_hours = (now - datetime.fromisoformat(opened_at.replace("Z", "+00:00"))).total_seconds() / 3600
                except (TypeError, ValueError):
                    stalled_hours = 0.0
                view["stalled_hours"] = round(stalled_hours, 1)
                if stalled_hours >= 48:
                    stalled.append(view)
                if row["kind"] == "client":
                    waiting_for_client.append(view)
                elif row["reviewer_person_id"] == person_id:
                    waiting_for_me.append(view)
                else:
                    waiting_for_team.append(view)
            elif row["status"] == "revision_requested":
                revision_requested.append(view)
            elif row["status"] == "approved" and row["closed_at"] and row["closed_at"][:10] == now.date().isoformat():
                approved_today.append(view)
        return {
            "waiting_for_me": waiting_for_me, "waiting_for_team": waiting_for_team,
            "waiting_for_client": waiting_for_client, "revision_requested": revision_requested,
            "stalled": stalled, "approved_today": approved_today,
            "generated_at": now.isoformat(),
        }

    def module(self, organization_id: str, workspace_id: str, person_id: str, module: str) -> dict[str,Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        if module in {"Feedback", "Performance Insights", "Forecasts", "Retention"}:
            if module == "Feedback":
                items = self.os.feedback.list_patterns(organization_id, workspace_id, person_id)
                return {"module": module, "source_table": "feedback_patterns", "items": items,
                        "allowed_actions": [{"action": "promote_pattern", "route": "/feedback/patterns/promote"}]}
            if module == "Performance Insights":
                items = self.os.performance.list_insights(organization_id, workspace_id, person_id)
                return {"module": module, "source_table": "performance_insights", "items": items,
                        "allowed_actions": [{"action": "generate_insights", "route": "/insights/performance/generate"}]}
            if module == "Forecasts":
                return {"module": module, "source_table": "forecasts", "items": self.os.forecasts.list_forecasts(organization_id, person_id),
                        "allowed_actions": [{"action": "generate_forecasts", "route": "/forecasts/generate"}]}
            return {"module": module, "source_table": "retention_policies", "items": self.os.retention.list_policies(organization_id, person_id),
                    "allowed_actions": [{"action": "create_policy", "route": "/retention/policies"}]}
        queries={
            "Campaigns":("campaigns","SELECT id,name,platform,status,budget,updated_at FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Content":("content_items","SELECT id,title,stage,objective,publish_at,updated_at FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Creative":("creative_assets","SELECT id,title,format,platform,approval_state,revision_count,created_at FROM creative_assets WHERE workspace_id=? ORDER BY created_at DESC"),
            "Meetings":("meetings","SELECT id,title,occurred_at,summary,sentiment,source FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC"),
            "Automations":("automations","SELECT id,name,status,approval_policy,created_at FROM automations WHERE organization_id=? ORDER BY created_at DESC"),
            "Reports":("report_runs","SELECT id,type,status,generated_at FROM report_runs WHERE organization_id=? ORDER BY generated_at DESC"),
            "Integrations":("integrations","SELECT id,source,status,last_sync_at,last_error,object_count,health FROM integrations WHERE organization_id=? ORDER BY source"),
            "Workflows":("workflow_runs","SELECT id,definition_name,definition_version,status,due_at,updated_at FROM workflow_runs WHERE workspace_id=? ORDER BY updated_at DESC"),
        }
        if module not in queries:return {"module":module,"items":[]}
        table,sql=queries[module];scope=organization_id if module in {"Automations","Reports","Integrations"} else workspace_id
        return {"module":module,"source_table":table,"items":[dict(row) for row in self.conn.execute(sql,(scope,)).fetchall()]}

_TERMINAL = {"completed", "cancelled"}


class DashboardService(_ExistingDashboardService):
    """Compose dashboard views from canonical scoped state without shadow writes."""

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

    def brain(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        self._authorize(identity, organization_id, workspace_id, person_id, "brain_read")
        actor_id = self.os.auth.actor_for_identity(identity, workspace_id)
        actor = self.os._require_actor(workspace_id, actor_id)
        moment = _moment(as_of)
        sources = self.os.store.allowed_sources(workspace_id, actor, as_of=as_of)
        source_ids = {source.id for source in sources}
        facts = self.os.store.list_facts(workspace_id, source_ids, as_of=moment, include_superseded=True)
        states = self._fact_states(workspace_id, (fact.id for fact in facts), moment)

        truth_rows: list[dict[str, Any]] = []
        conflict_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            state = states.get(fact.id, "inferred")
            item = fact.to_dict()
            item["state"] = state
            if fact.conflict_group:
                conflict_rows[fact.conflict_group].append(item)
            if state not in {"stale", "conflicted", "proposed"} and not fact.superseded_by:
                truth_rows.append(item)

        all_conflict_ids: dict[str,set[str]] = defaultdict(set)
        if conflict_rows:
            group_ids=sorted(conflict_rows); marks=','.join('?' for _ in group_ids)
            for row in self.conn.execute(
                f"SELECT id,conflict_group FROM facts WHERE workspace_id=? AND conflict_group IN ({marks}) AND superseded_by IS NULL",
                (workspace_id,*group_ids),
            ).fetchall():
                all_conflict_ids[str(row["conflict_group"])].add(str(row["id"]))
        conflicts = []
        for group_id, alternatives in sorted(conflict_rows.items()):
            if all_conflict_ids[group_id] != {str(item["id"]) for item in alternatives}:
                # A partial ACL must not reveal that additional alternatives,
                # or even the conflict group itself, exist.
                continue
            live = [item for item in alternatives if item["state"] != "stale"]
            winner = next((item["id"] for item in alternatives if item["state"] == "verified"), None)
            actions = []
            if as_of is None and identity.can("brain_promote") and winner is None:
                actions = [
                    _action(
                        "resolve_conflict", "/brain/conflicts/resolve",
                        {"workspace_id":workspace_id,"conflict_group":group_id,"winner_fact_id":item["id"]},
                    )
                    for item in alternatives
                ]
            conflicts.append(
                {
                    "id": group_id,
                    "state": "resolved" if winner and len(live) == 1 else "conflicted",
                    "winner_fact_id": winner,
                    "alternatives": sorted(alternatives, key=lambda item: (item["recorded_at"], item["id"])),
                    "allowed_actions": actions,
                }
            )

        entities = self._entities(organization_id, workspace_id, moment, source_ids)
        proposals = self._proposal_rows(
            identity,organization_id,workspace_id,person_id,moment,as_of,source_ids,
            self._visible_proposal_entity_ids(organization_id,workspace_id,moment,source_ids),
        )
        workspace = self.os.store.get_workspace(workspace_id)
        graph = self.os.store.graph_generation_state(workspace_id)
        semantic = dict(getattr(self.os, "embedding_health", {}) or {})
        graph_runtime = dict(getattr(self.os, "graph_health", {}) or {})
        semantic_fallback = bool(semantic.get("fallback_used", False))
        graph_runtime_status = _health_status(graph_runtime.get("status"))
        graph_status = (
            "degraded" if graph_runtime_status == "degraded"
            else "healthy" if graph.get("active_status") == "active"
            else "building" if graph.get("building_generation") is not None
            else "unavailable"
        )
        health = {
            "semantic": {
                "status": _health_status(semantic.get("status")),
                "provider": _safe_scalar(semantic.get("provider")),
                "model": _safe_scalar(semantic.get("model")),
                "version": _safe_scalar(semantic.get("version")),
                "mode": "deterministic_fallback" if semantic_fallback else "configured_provider",
                "fallback_used": semantic_fallback,
            },
            "graph": {
                "status": graph_status,
                "provider": _safe_scalar(getattr(self.os.graph, "name", None)),
                "active_generation": graph.get("active_generation"),
                "building": graph.get("building_generation") is not None,
                "serving_stale_generation": bool(
                    graph_runtime_status == "degraded" and graph.get("active_generation")
                ),
            },
        }
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": moment.isoformat(),
            "workspace": {"id": workspace_id, "name": workspace.name if workspace else ""},
            "summary": {
                "sources": len(sources),
                "current_truths": len(truth_rows),
                "conflict_groups": len(conflicts),
                "pending_proposals": sum(item["status"] == "pending" for item in proposals),
                "entities": len(entities),
            },
            "proposals": proposals,
            "conflicts": conflicts,
            "current_truths": sorted(truth_rows, key=lambda item: (item["subject"], item["predicate"], item["id"])),
            "entities": entities,
            "health": health,
        }

    def _proposal_rows(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str,
        person_id: str, moment: datetime, as_of: datetime | None, allowed_source_ids: set[str],
        visible_entity_ids: set[str],
    ) -> list[dict[str, Any]]:
        cutoff=moment.isoformat(); historical=as_of is not None
        visible_documents,visible_facts,visible_relations = self._visible_evidence_ids(
            workspace_id,allowed_source_ids,cutoff
        )
        rows: list[dict[str, Any]]=[]
        for proposal in self.os.brain_ops.list_memory_proposals(
            organization_id,workspace_id,person_id,as_of=as_of
        ):
            if proposal.get("source_id") and str(proposal["source_id"]) not in allowed_source_ids:
                continue
            kind=str(proposal["kind"]); status=str(proposal["status"])
            actions=[]
            if not historical and status=="pending" and kind=="fact" and identity.can("brain_promote"):
                actions=[
                    _action("approve","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"approve"}),
                    _action("reject","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"reject"}),
                ]
            rows.append({
                "id":proposal["id"],"family":"knowledge","kind":kind,"status":status,
                "content":proposal["content"],"structured_payload":_json_object(proposal.get("structured_payload")),
                "confidence":proposal["confidence"],"evidence":proposal["evidence"],
                "created_at":proposal["created_at"],"reviewed_at":proposal.get("reviewed_at"),
                "allowed_actions":actions,
            })

        resolution_rows=self.conn.execute("""SELECT p.*,d.action AS decision_action,
                d.reviewer_person_id AS decision_reviewer,d.created_at AS decision_at
            FROM entity_resolution_proposals p
            LEFT JOIN entity_resolution_decisions d ON d.proposal_id=p.id AND d.created_at<=?
            WHERE p.organization_id=? AND p.workspace_id=? AND p.created_at<=?
            ORDER BY p.created_at DESC,p.id DESC""",(cutoff,organization_id,workspace_id,cutoff)).fetchall()
        for raw in resolution_rows:
            proposal=dict(raw); candidates={str(item) for item in _json_list(proposal["candidate_entity_ids"])}
            if not candidates or not candidates.issubset(visible_entity_ids):
                continue
            if proposal.get("evidence_source_id") and str(proposal["evidence_source_id"]) not in allowed_source_ids:
                continue
            refs=_json_object(proposal.get("evidence_refs"))
            if not _refs_visible(refs,allowed_source_ids,visible_documents,visible_facts,visible_relations):
                continue
            action=proposal.get("decision_action")
            status="approved" if action=="approve" else "rejected" if action=="reject" else "pending"
            actions=[]
            if not historical and status=="pending" and identity.can("brain_promote"):
                actions=[
                    _action("approve","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"approve"}),
                    _action("reject","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"reject"}),
                ]
            rows.append({
                "id":proposal["id"],"family":"entity_resolution","kind":proposal["kind"],"status":status,
                "content":proposal["alias"] or proposal["rationale"],
                "structured_payload":{
                    "alias":proposal["alias"],"source_entity_id":proposal["source_entity_id"],
                    "target_entity_id":proposal["target_entity_id"],"candidate_entity_ids":sorted(candidates),
                },
                "confidence":proposal["score"],"evidence":proposal["evidence"],
                "created_at":proposal["created_at"],"reviewed_at":proposal.get("decision_at"),
                "allowed_actions":actions,
            })
        return sorted(rows,key=lambda item:(item["created_at"],item["id"]),reverse=True)

    def _visible_proposal_entity_ids(
        self, organization_id: str, workspace_id: str, moment: datetime,
        allowed_source_ids: set[str],
    ) -> set[str]:
        rows=self.conn.execute("""SELECT e.id,a.source_id FROM entities e
            JOIN entity_aliases a ON a.entity_id=e.id
            WHERE e.organization_id=? AND e.workspace_id=? AND e.created_at<=? AND a.created_at<=?
              AND a.status='approved'""",
            (organization_id,workspace_id,moment.isoformat(),moment.isoformat()),
        ).fetchall()
        return {
            str(row["id"]) for row in rows
            if row["source_id"] is None or str(row["source_id"]) in allowed_source_ids
        }

    def _visible_evidence_ids(
        self, workspace_id: str, allowed_source_ids: set[str], cutoff: str,
    ) -> tuple[set[str],set[str],set[str]]:
        if not allowed_source_ids:
            return set(),set(),set()
        marks=','.join('?' for _ in allowed_source_ids); values=(workspace_id,*sorted(allowed_source_ids),cutoff)
        documents={str(row["id"]) for row in self.conn.execute(
            f"SELECT id FROM documents WHERE workspace_id=? AND source_id IN ({marks}) AND observed_at<=?",values
        ).fetchall()}
        facts={str(row["id"]) for row in self.conn.execute(
            f"SELECT id FROM facts WHERE workspace_id=? AND source_id IN ({marks}) AND observed_at<=?",values
        ).fetchall()}
        relations={str(row["id"]) for row in self.conn.execute(
            f"SELECT id FROM relations WHERE workspace_id=? AND source_id IN ({marks}) AND observed_at<=?",values
        ).fetchall()}
        return documents,facts,relations

    def workflow_board(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        self._authorize(identity, organization_id, workspace_id, person_id, "workspace_read")
        moment = _moment(as_of)
        cutoff = moment.isoformat()
        historical = as_of is not None
        run_rows = self.conn.execute(
            """SELECT * FROM workflow_runs
               WHERE organization_id=? AND workspace_id=? AND created_at<=?
               ORDER BY COALESCE(due_at,created_at),created_at,id""",
            (organization_id, workspace_id, cutoff),
        ).fetchall()
        if not run_rows:
            return self._workflow_response(moment, historical, [], [])
        run_ids = [str(row["id"]) for row in run_rows]
        marks = ",".join("?" for _ in run_ids)
        stages = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_stage_runs WHERE run_id IN ({marks}) AND created_at<=? ORDER BY run_id,sequence,stage_key",
            (*run_ids, cutoff),
        ).fetchall()]
        stage_ids = [str(row["id"]) for row in stages]
        stage_marks = ",".join("?" for _ in stage_ids) or "NULL"
        history = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_transition_history WHERE run_id IN ({marks}) AND created_at<=? ORDER BY created_at,rowid",
            (*run_ids, cutoff),
        ).fetchall()]
        dependencies = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_stage_dependencies WHERE run_id IN ({marks}) AND created_at<=?",
            (*run_ids, cutoff),
        ).fetchall()]
        evidence = [dict(row) for row in self.conn.execute(
            f"SELECT stage_run_id,kind,COUNT(*) AS count FROM workflow_evidence WHERE stage_run_id IN ({stage_marks}) AND created_at<=? GROUP BY stage_run_id,kind",
            (*stage_ids, cutoff),
        ).fetchall()] if stage_ids else []
        approvals = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_approval_decisions WHERE stage_run_id IN ({stage_marks}) AND created_at<=? ORDER BY created_at,rowid",
            (*stage_ids, cutoff),
        ).fetchall()] if stage_ids else []
        handoffs = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_handoff_acknowledgements WHERE run_id IN ({marks}) AND created_at<=? ORDER BY created_at,rowid",
            (*run_ids, cutoff),
        ).fetchall()]
        handoff_contracts: dict[tuple[str,str],str] = {}
        for run in run_rows:
            snapshot=_json_object(run["template_snapshot"])
            for item in snapshot.get("stages",[]) if isinstance(snapshot.get("stages"),list) else []:
                if isinstance(item,dict) and item.get("key"):
                    handoff_contracts[(str(run["id"]),str(item["key"]))]=str(item.get("handoff_contract") or "")

        history_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        history_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in history:
            history_by_run[str(event["run_id"])].append(event)
            if event["stage_run_id"]:
                history_by_stage[str(event["stage_run_id"])].append(event)
        evidence_by_stage: dict[str, Counter[str]] = defaultdict(Counter)
        for row in evidence:
            evidence_by_stage[str(row["stage_run_id"])][str(row["kind"])] = int(row["count"])
        approval_by_stage = {str(row["stage_run_id"]): row for row in approvals}
        request_id_by_stage: dict[str, str] = {}
        for event in history:
            if event["stage_run_id"] and event["action"] == "request_approval":
                metadata = _json_object(event["metadata"])
                if metadata.get("approval_request_id"):
                    request_id_by_stage[str(event["stage_run_id"])] = str(metadata["approval_request_id"])
        request_ids = sorted(set(request_id_by_stage.values()))
        requests_by_id: dict[str, dict[str, Any]] = {}
        if request_ids:
            request_marks = ",".join("?" for _ in request_ids)
            requests_by_id = {str(row["id"]): dict(row) for row in self.conn.execute(
                f"SELECT id,approver_person_id,status,created_at FROM approval_requests "
                f"WHERE organization_id=? AND workspace_id=? AND id IN ({request_marks}) AND created_at<=?",
                (organization_id, workspace_id, *request_ids, cutoff),
            ).fetchall()}
        request_by_stage: dict[str,dict[str,Any]] = {}
        request_rows=self.conn.execute("""SELECT id,requested_for,approver_person_id,status,created_at
            FROM approval_requests WHERE organization_id=? AND workspace_id=? AND action_type='workflow_stage_approval'
              AND created_at<=? ORDER BY created_at,id""",(organization_id,workspace_id,cutoff)).fetchall()
        stage_id_by_contract_key={
            f"workflow:{stage['run_id']}:{stage['stage_key']}":str(stage["id"]) for stage in stages
        }
        for raw in request_rows:
            stage_id=stage_id_by_contract_key.get(str(raw["requested_for"]))
            if stage_id: request_by_stage[stage_id]=dict(raw)
        ack_by_pair = {(str(row["from_stage_run_id"]), str(row["to_stage_run_id"])): row for row in handoffs}
        dependencies_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for dependency in dependencies:
            dependencies_by_stage[str(dependency["stage_run_id"])].append(dependency)

        stage_by_id: dict[str, dict[str, Any]] = {}
        for stage in stages:
            events = history_by_stage.get(str(stage["id"]), [])
            if historical:
                stage["status"] = str(events[-1]["to_status"] or "pending") if events else "pending"
                stage["version"] = 1 + sum(event["from_status"] != event["to_status"] for event in events)
                stage["blocked_reason"] = next(
                    (event["reason"] for event in reversed(events) if event["to_status"] == "blocked"), None
                ) if stage["status"] == "blocked" else None
            stage["required_evidence"] = _json_list(stage["required_evidence"])
            stage["requires_approval"] = bool(stage["requires_approval"])
            stage["handoff_contract"] = handoff_contracts.get((str(stage["run_id"]),str(stage["stage_key"])),"")
            stage_by_id[str(stage["id"])] = stage

        rendered_stages: list[dict[str, Any]] = []
        for stage in stages:
            stage_id = str(stage["id"])
            deps = dependencies_by_stage.get(stage_id, [])
            dependency_view = []
            missing_handoffs: list[dict[str,Any]] = []
            dependencies_clear = True
            handoffs_clear = True
            for dependency in deps:
                source = stage_by_id[str(dependency["depends_on_stage_run_id"])]
                completed = source["status"] == "completed"
                requires_handoff = bool(source["handoff_to_wing"] or source["handoff_to_role"] or source["handoff_to_person_id"])
                ack = ack_by_pair.get((str(source["id"]), stage_id))
                acknowledged = bool(ack and int(ack["source_stage_version"]) == int(source["version"]))
                dependencies_clear = dependencies_clear and completed
                handoffs_clear = handoffs_clear and (not requires_handoff or acknowledged)
                if requires_handoff and completed and not acknowledged:
                    missing_handoffs.append({
                        "from_stage_id":source["stage_key"],"to_stage_id":stage["stage_key"],
                        "artifact_contract":source["handoff_contract"],
                    })
                dependency_view.append({
                    "stage_run_id": source["id"], "stage_key": source["stage_key"], "kind": dependency["kind"],
                    "status": source["status"], "handoff_required": requires_handoff,
                    "handoff_acknowledged": acknowledged,"handoff_contract":source["handoff_contract"],
                })
            counts = evidence_by_stage.get(stage_id, Counter())
            missing = [kind for kind in stage["required_evidence"] if counts[kind] == 0]
            approval = approval_by_stage.get(stage_id)
            request = request_by_stage.get(stage_id) or requests_by_id.get(request_id_by_stage.get(stage_id, ""))
            approval_view = None if approval is None else {
                "decision": approval["decision"], "approval_request_id": approval["approval_request_id"],
                "approver_person_id": approval["approver_person_id"], "reason": approval["reason"],
                "created_at": approval["created_at"],
            }
            request_view = None if request is None else {
                "id": request["id"], "approver_person_id": request["approver_person_id"],
                "status": (
                    "unknown" if historical and approval_view is None
                    else approval_view["decision"] if historical and approval_view is not None
                    else request["status"]
                ),
            }
            ready = stage["status"] in {"pending", "blocked"} and dependencies_clear and handoffs_clear
            actions = [] if historical else self._stage_actions(
                identity,workspace_id,stage,ready,not missing,approval_view,request_view,
                missing_handoffs
            )
            rendered_stages.append({
                "id": stage_id, "run_id": stage["run_id"], "stage_key": stage["stage_key"], "name": stage["name"],
                "sequence": stage["sequence"], "status": stage["status"],
                "assignee": {"wing": stage["assignee_wing"], "role": stage["assignee_role"], "person_id": stage["assignee_person_id"]},
                "readiness": {"ready": ready, "dependencies_clear": dependencies_clear, "handoffs_clear": handoffs_clear},
                "dependencies": dependency_view,
                "evidence": {"total": sum(counts.values()), "by_kind": dict(sorted(counts.items())), "required": stage["required_evidence"], "missing": missing},
                "approval": {"required": stage["requires_approval"], "request": request_view, "latest": approval_view},
                "handoff": {"to_wing": stage["handoff_to_wing"], "to_role": stage["handoff_to_role"], "to_person_id": stage["handoff_to_person_id"],"contract":stage["handoff_contract"]},
                "blocker": stage["blocked_reason"],
                "due": {"at": stage["due_at"], "overdue": bool(stage["due_at"] and stage["due_at"] < cutoff and stage["status"] not in _TERMINAL)},
                "version": stage["version"], "allowed_actions": actions,
            })

        stages_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for stage in rendered_stages:
            stages_by_run[str(stage["run_id"])].append(stage)
        runs = []
        for raw in run_rows:
            run = dict(raw); children = stages_by_run.get(str(run["id"]), [])
            if historical:
                run["status"] = _derived_run_status(children, history_by_run.get(str(run["id"]), []))
            counts = Counter(stage["status"] for stage in children)
            actions: list[dict[str,Any]] = []
            if not historical and identity.can("workflow_run") and run["status"] not in _TERMINAL:
                actions.append(_action(
                    "cancel_run","/workflows/runs/cancel",
                    {"workspace_id":workspace_id,"run_id":run["id"],"expected_version":run["version"]},
                    ["reason"],
                ))
            runs.append({
                "id": run["id"], "definition_key": run["definition_key"], "definition_name": run["definition_name"],
                "definition_version": run["definition_version"], "status": run["status"],
                "due": {"at": run["due_at"], "escalation_at": run["escalation_at"], "overdue": bool(run["due_at"] and run["due_at"] < cutoff and run["status"] not in _TERMINAL)},
                "blocker": run["blocked_reason"] if not historical else next((stage["blocker"] for stage in children if stage["blocker"]), None),
                "progress": {"completed": counts["completed"], "total": len(children), "status_counts": dict(sorted(counts.items()))},
                "allowed_actions": actions,
            })
        return self._workflow_response(moment, historical, runs, rendered_stages)

    def _fact_states(self, workspace_id: str, fact_ids: Iterable[str], moment: datetime) -> dict[str, str]:
        ids = list(fact_ids)
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        cutoff = moment.isoformat()
        rows = self.conn.execute(
            f"""SELECT subject_id,state FROM knowledge_state_events
                WHERE workspace_id=? AND subject_type='fact' AND subject_id IN ({marks})
                  AND effective_from<=? AND (effective_until IS NULL OR effective_until>?)
                ORDER BY effective_from DESC,event_sequence DESC,recorded_at DESC,id DESC""",
            (workspace_id, *ids, cutoff, cutoff),
        ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(str(row["subject_id"]), str(row["state"]))
        return result

    def _entities(
        self, organization_id: str, workspace_id: str, moment: datetime,
        allowed_source_ids: set[str],
    ) -> list[dict[str, Any]]:
        cutoff = moment.isoformat()
        entities = [dict(row) for row in self.conn.execute(
            "SELECT * FROM entities WHERE organization_id=? AND workspace_id=? AND created_at<=? ORDER BY canonical_name,id",
            (organization_id, workspace_id, cutoff),
        ).fetchall()]
        if not entities:
            return []
        ids = [str(row["id"]) for row in entities]; marks = ",".join("?" for _ in ids)
        merges = self.conn.execute(
            f"SELECT source_entity_id,target_entity_id FROM entity_merge_history WHERE organization_id=? AND source_entity_id IN ({marks}) AND merged_at<=? ORDER BY merged_at,id",
            (organization_id, *ids, cutoff),
        ).fetchall()
        redirect = {str(row["source_entity_id"]): str(row["target_entity_id"]) for row in merges}
        aliases = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM entity_aliases WHERE entity_id IN ({marks}) AND created_at<=? AND status='approved' ORDER BY created_at,id",
            (*ids, cutoff),
        ).fetchall()]
        alias_ids = [str(row["id"]) for row in aliases]
        lifecycle: dict[str, str] = {}
        if alias_ids:
            alias_marks = ",".join("?" for _ in alias_ids)
            rows = self.conn.execute(
                f"SELECT alias_id,state FROM entity_alias_state_events WHERE organization_id=? AND workspace_id=? AND alias_id IN ({alias_marks}) AND created_at<=? ORDER BY created_at DESC,id DESC",
                (organization_id, workspace_id, *alias_ids, cutoff),
            ).fetchall()
            for row in rows: lifecycle.setdefault(str(row["alias_id"]), str(row["state"]))
        by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for alias in aliases:
            if alias["source_id"] is not None and str(alias["source_id"]) not in allowed_source_ids:
                continue
            owner = str(alias["entity_id"])
            target = _redirect(owner, redirect)
            state = lifecycle.get(str(alias["id"]), "active")
            if state == "retired" and target == owner:
                continue
            by_entity[target].append({
                "id": alias["id"], "alias": alias["alias"], "confidence": alias["confidence"],
                "state": state, "origin_entity_id": owner,
            })
        result = []
        for entity in entities:
            if str(entity["id"]) in redirect:
                continue
            if not by_entity.get(str(entity["id"])):
                continue
            result.append({
                "id": entity["id"], "canonical_name": entity["canonical_name"], "type": entity["type"],
                "aliases": by_entity.get(str(entity["id"]), []),
            })
        return result

    @staticmethod
    def _stage_actions(
        identity: AuthenticatedIdentity, workspace_id: str, stage: dict[str, Any],
        ready: bool, evidence_clear: bool, approval: dict[str, Any] | None,
        request: dict[str, Any] | None, missing_handoffs: list[dict[str,Any]],
    ) -> list[dict[str,Any]]:
        actions: list[dict[str,Any]] = []
        status = stage["status"]
        base={"workspace_id":workspace_id,"run_id":stage["run_id"],"stage_id":stage["stage_key"]}
        if identity.can("workflow_run"):
            if ready:
                actions.append(_action("start_stage","/workflows/stages/start",{**base,"expected_version":stage["version"]}))
            if status not in _TERMINAL:
                actions.append(_action("submit_evidence","/workflows/evidence",base,["kind","one_of:uri,text,object_type"] ))
            if status in {"pending", "in_progress", "waiting_approval"}:
                actions.append(_action("block_stage","/workflows/stages/block",{**base,"expected_version":stage["version"]},["reason"]))
            approval_clear = not stage["requires_approval"] or bool(approval and approval["decision"] == "approve")
            if status in {"in_progress", "waiting_approval"} and evidence_clear and approval_clear:
                actions.append(_action("complete_stage","/workflows/stages/complete",{**base,"expected_version":stage["version"]}))
        if identity.can("workflow_gate"):
            if (
                stage["requires_approval"] and status == "in_progress" and evidence_clear
                and request is not None and request["status"]=="pending"
            ):
                actions.append(_action(
                    "request_approval","/workflows/approvals/request",
                    {**base,"approval_request_id":request["id"],"expected_version":stage["version"]},["reason"],
                ))
            for handoff in missing_handoffs:
                if handoff["artifact_contract"]:
                    actions.append(_action(
                        "acknowledge_handoff","/workflows/handoffs/acknowledge",
                        {"workspace_id":workspace_id,"run_id":stage["run_id"],**handoff},
                    ))
        if (
            identity.can("approval_decide") and status == "waiting_approval" and request is not None
            and request["approver_person_id"] == identity.person_id and request["status"] in {"approved","rejected"}
        ):
            decision="approve" if request["status"]=="approved" else "request_changes"
            actions.append(_action(
                "decide_approval","/workflows/approvals/decide",
                {**base,"approval_request_id":request["id"],"decision":decision},["reason"],
            ))
        return actions

    def _authorize(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str,
        person_id: str, capability: str,
    ) -> None:
        if (
            identity.organization_id != organization_id
            or identity.person_id != person_id
            or identity.workspace_id not in {None, workspace_id}
        ):
            raise AuthorizationError("dashboard scope denied")
        identity.require(capability)
        self.os._require_person_access(organization_id, workspace_id, person_id, write=False)

    @staticmethod
    def _workflow_response(moment: datetime, historical: bool, runs: list[dict[str, Any]], stages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(), "as_of": moment.isoformat(),
            "historical": historical,
            "summary": {"runs": len(runs), "active_runs": sum(run["status"] not in _TERMINAL for run in runs), "stages": len(stages)},
            "runs": runs, "stages": stages,
        }


def _moment(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    return result.astimezone(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict): return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list): return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _redirect(entity_id: str, redirects: dict[str, str]) -> str:
    seen: set[str] = set(); current = entity_id
    while current in redirects and current not in seen:
        seen.add(current); current = redirects[current]
    return current


def _derived_run_status(stages: list[dict[str, Any]], history: list[dict[str, Any]]) -> str:
    explicit = [event for event in history if event["stage_run_id"] is None and event["to_status"] in _TERMINAL]
    if explicit: return str(explicit[-1]["to_status"])
    statuses = [stage["status"] for stage in stages]
    if statuses and all(status == "completed" for status in statuses): return "completed"
    if statuses and all(status == "cancelled" for status in statuses): return "cancelled"
    if "waiting_approval" in statuses: return "waiting_approval"
    if "blocked" in statuses and not any(status == "in_progress" for status in statuses): return "blocked"
    if any(status in {"in_progress", "completed", "blocked"} for status in statuses): return "in_progress"
    return "pending"


def _health_status(value: Any) -> str:
    return str(value) if value in {"healthy", "configured", "building", "degraded", "unavailable"} else "unavailable"


def _safe_scalar(value: Any) -> str | None:
    return str(value)[:120] if isinstance(value, (str, int, float)) else None


def _action(
    action: str, route: str, payload: dict[str,Any], required_fields: list[str] | None = None,
) -> dict[str,Any]:
    return {
        "action":action,"method":"POST","route":route,"payload":payload,
        "required_fields":required_fields or [],
    }


def _refs_visible(
    refs: dict[str,Any], sources: set[str], documents: set[str], facts: set[str], relations: set[str],
) -> bool:
    allowed={"sources":sources,"documents":documents,"facts":facts,"relations":relations}
    for kind,values in refs.items():
        if kind not in allowed or not isinstance(values,list):
            return False
        if not {str(value) for value in values}.issubset(allowed[kind]):
            return False
    return True
