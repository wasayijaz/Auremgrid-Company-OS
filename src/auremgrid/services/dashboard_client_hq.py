from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .dashboard_shared import _json_list


class DashboardClientHQMixin:
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
        caller_membership = self.os.company.workspace_membership(workspace_id, identity.person_id)
        portal_reports = (
            self.os.report_delivery.portal_list(identity, organization_id, workspace_id)
            if caller_membership is not None and caller_membership.role == "client"
            else self.os.report_delivery.staff_list(identity, organization_id, workspace_id)
        )
        insights = {
            "performance": [dict(r) for r in self.conn.execute(
                """SELECT * FROM performance_insights WHERE organization_id=? AND workspace_id=?
                   ORDER BY created_at DESC,id LIMIT 50""", (organization_id, workspace_id)
            ).fetchall()],
            "forecasts": [dict(r) for r in self.conn.execute(
                """SELECT * FROM forecasts WHERE organization_id=? AND workspace_id=?
                   ORDER BY created_at DESC,id LIMIT 50""", (organization_id, workspace_id)
            ).fetchall()],
            "opportunities": [dict(r) for r in self.conn.execute(
                """SELECT * FROM opportunities WHERE organization_id=? AND workspace_id=?
                   ORDER BY created_at DESC,id LIMIT 50""", (organization_id, workspace_id)
            ).fetchall()],
            "campaign_anomalies": [dict(r) for r in self.conn.execute(
                """SELECT anomaly.*,campaign.workspace_id FROM campaign_anomalies anomaly
                   JOIN campaigns campaign ON campaign.id=anomaly.campaign_id
                   WHERE campaign.organization_id=? AND campaign.workspace_id=?
                   ORDER BY anomaly.detected_at DESC,anomaly.id LIMIT 50""", (organization_id, workspace_id)
            ).fetchall()],
        }

        roster = self.os.client_ops.get_client_roster(organization_id, workspace_id, person_id)
        people_by_id = {str(item["id"]): item for item in workspace_people}
        agent_ids = {str(role.get("agent_id")) for role in (roster or {}).get("roles", []) if role.get("principal_type") == "agent" and role.get("agent_id")}
        agents_by_id = {str(row["id"]): dict(row) for row in self.conn.execute("SELECT id,name,status FROM agents WHERE organization_id=? AND id IN ({})".format(",".join("?" for _ in agent_ids) or "NULL"), (organization_id, *sorted(agent_ids))).fetchall()} if agent_ids else {}

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

        def principal_ref(role: dict[str, Any]) -> dict[str, Any] | None:
            if role.get("principal_type") == "agent":
                agent = agents_by_id.get(str(role.get("agent_id")))
                return {"id": agent["id"], "agent_id": agent["id"], "name": agent["name"], "status": agent["status"], "principal_type": "agent"} if agent else None
            return person_ref(role.get("person_id"))

        slots: dict[str, Any] = {
            "dri": None, "backup": None, "account": {"lead": None, "executive": None},
            "cadence": None, "escalation": None, "wings": {},
        }
        if roster:
            for role in roster.get("roles", []):
                role_key, ref = role.get("role_key"), principal_ref(role)
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
            "people": workspace_people + contacts, "activity": activity, "insights": insights,
            "portal_reports": portal_reports,
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
