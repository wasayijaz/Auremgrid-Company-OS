from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .dashboard_shared import _safe_scalar


class DashboardCommandMixin:
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
        agents=self._agent_dashboard_rows(organization_id, person_id)
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
        surface_by_source = {
            "work_item": "Work",
            "review": "Review",
            "risk": "Review",
            "approval": "Review",
            "workflow": "Workflows",
            "proposal": "Brain",
        }
        cosmo_queue = [
            {
                **item,
                "surface": surface_by_source.get(str(item.get("source_type")), "Command"),
                "action_kind": "open_surface",
            }
            for item in attention
        ]
        clients=[]
        for ws in workspaces:
            if ws["kind"]!="client":continue
            health=self.conn.execute("SELECT * FROM client_health_snapshots WHERE workspace_id=? ORDER BY calculated_at DESC LIMIT 1",(ws["id"],)).fetchone()
            latest=self.conn.execute("SELECT occurred_at FROM touchpoints WHERE workspace_id=? ORDER BY occurred_at DESC LIMIT 1",(ws["id"],)).fetchone()
            owner = self._client_owner(organization_id, ws["id"])
            scope = self._client_scope_usage(organization_id, ws["id"])
            client_finance = self.os.agency_ops.finance_status(organization_id, person_id, ws["id"])
            open_work_count = int(self.conn.execute("SELECT COUNT(*) FROM work_items WHERE workspace_id=? AND status!='shipped'",(ws["id"],)).fetchone()[0])
            review_count = int(self.conn.execute("SELECT COUNT(*) FROM reviews WHERE workspace_id=? AND status='open'",(ws["id"],)).fetchone()[0])
            risk_count = int(self.conn.execute("SELECT COUNT(*) FROM risks WHERE workspace_id=? AND status='open'",(ws["id"],)).fetchone()[0])
            critical_risks = int(self.conn.execute("SELECT COUNT(*) FROM risks WHERE workspace_id=? AND status='open' AND severity IN ('critical','high')",(ws["id"],)).fetchone()[0])
            attention_state = "high" if critical_risks or (scope["percentage"] is not None and scope["percentage"] > 110) else "medium" if risk_count or review_count else "low"
            clients.append({"id":ws["id"],"name":ws["name"],"role":ws["role"],"owner":owner,"attention":attention_state,
                "health":health["overall"] if health else None,"health_trend":health["trend"] if health else None,
                "open_work":open_work_count,"reviews":review_count,"risks":risk_count,"scope":scope,
                "last_touch":latest[0] if latest else None,
                "finance":{"status":client_finance["status"],"recognized_revenue":client_finance.get("recognized_revenue"),"currency":None,"source":client_finance.get("source")}})
        pulse=[]
        for row in self.conn.execute("SELECT workspace_id,action,target,detail,recorded_at FROM audit_events WHERE workspace_id IN ("+placeholders+") ORDER BY recorded_at DESC LIMIT 12",ids).fetchall() if ids else []:
            pulse.append(dict(row))
        return {"generated_at":datetime.now(timezone.utc).isoformat(),"metrics":{"active_clients":active_clients,"mrr":finance.get("mrr") if finance["status"]=="connected" else None,
            "finance_status":finance["status"],"open_work":open_work,"overdue_work":overdue,"in_review":review,"active_workflows":active_workflows,"agents_running":sum(a["status"]=="running" for a in agents),
            "automations_today":automation_count,"open_risks":risks},"attention":attention,"clients":clients,"agents":agents,"pulse":pulse,
            "workspaces":[dict(row) for row in workspaces],
            "onboarding": self.os.onboarding.latest_status(organization_id, ids),
            "identity": self._identity_view(organization_id, person_id),
            "ledger_health": self._ledger_health(organization_id, person_id),
            "capability_summary": self._capability_summary(organization_id, person_id),
            "cosmo": {
                "name": "Cosmo",
                "mode": "evidence_grounded",
                "queue": cosmo_queue,
                "writes_require_canonical_routes": True,
            },
            "modules": self._capability_modules(organization_id, person_id, ids),
            "agency_map": self._agency_map(organization_id, ids, clients),
            "trends": self._command_trends(ids),}

    def _client_owner(self, organization_id: str, workspace_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT rr.person_id,p.name,p.title,rr.role_key
               FROM client_account_rosters roster
               JOIN client_account_roster_roles rr ON rr.roster_id=roster.id
               JOIN people p ON p.id=rr.person_id AND p.organization_id=rr.organization_id
               WHERE roster.organization_id=? AND roster.workspace_id=?
                 AND rr.role_key IN ('client_success_dri','account_lead','account_executive')
               ORDER BY roster.version DESC,
                 CASE rr.role_key WHEN 'client_success_dri' THEN 0 WHEN 'account_lead' THEN 1 ELSE 2 END,
                 rr.id LIMIT 1""",
            (organization_id, workspace_id),
        ).fetchone()
        return dict(row) if row else None

    def _client_scope_usage(self, organization_id: str, workspace_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT SUM(COALESCE(a.included_hours,0)) AS included_hours,
                      SUM(COALESCE(latest.used_hours,0)) AS used_hours,
                      SUM(COALESCE(a.included_quantity,0)) AS included_quantity,
                      SUM(COALESCE(latest.delivered_quantity,0)+COALESCE(latest.in_review_quantity,0)+COALESCE(latest.requested_quantity,0)) AS used_quantity
               FROM contracts c JOIN scope_allowances a ON a.contract_id=c.id
               LEFT JOIN scope_usage latest ON latest.id=(
                 SELECT u.id FROM scope_usage u WHERE u.allowance_id=a.id
                 ORDER BY u.calculated_at DESC,u.id DESC LIMIT 1)
               WHERE c.organization_id=? AND c.workspace_id=? AND c.status='active'""",
            (organization_id, workspace_id),
        ).fetchone()
        included_hours = float(row["included_hours"] or 0) if row else 0.0
        used_hours = float(row["used_hours"] or 0) if row else 0.0
        included_quantity = float(row["included_quantity"] or 0) if row else 0.0
        used_quantity = float(row["used_quantity"] or 0) if row else 0.0
        denominator = included_hours or included_quantity
        numerator = used_hours if included_hours else used_quantity
        return {
            "status": "recorded" if denominator else "unknown",
            "percentage": round(numerator / denominator * 100, 1) if denominator else None,
            "included_hours": included_hours or None,
            "used_hours": used_hours if included_hours else None,
            "included_quantity": included_quantity or None,
            "used_quantity": used_quantity if included_quantity else None,
        }

    def _agent_dashboard_rows(self, organization_id: str, person_id: str) -> list[dict[str, Any]]:
        visible = self.os.agent_ops.visible_workspace_ids(organization_id, person_id)
        scope_clause, scope_values = self.os.agent_ops._visible_workspace_clause("workspace_id", visible)
        agents = [self.os.agent_ops._redacted_agent(row, visible) for row in self.conn.execute(
            "SELECT * FROM agents WHERE organization_id=? ORDER BY name,id", (organization_id,)
        ).fetchall()]
        for agent in agents:
            agent_id = agent["id"]
            tasks = self.conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status IN ('queued','pending') THEN 1 ELSE 0 END) AS queued,
                          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                   FROM agent_tasks WHERE organization_id=? AND agent_id=? AND """ + scope_clause,
                (organization_id, agent_id, *scope_values),
            ).fetchone()
            runs = self.conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                          SUM(CASE WHEN status IN ('failed','error') THEN 1 ELSE 0 END) AS failed,
                          SUM(COALESCE(cost,0)) AS cost,
                          SUM(input_tokens+output_tokens) AS tokens,
                          AVG(runtime_ms) AS average_runtime_ms,
                          MAX(started_at) AS last_run_at
                   FROM agent_runs WHERE organization_id=? AND agent_id=? AND """ + scope_clause,
                (organization_id, agent_id, *scope_values),
            ).fetchone()
            total_runs = int(runs["total"] or 0)
            completed_runs = int(runs["completed"] or 0)
            agent["runtime"] = {
                "tasks_total": int(tasks["total"] or 0),
                "queue_count": int(tasks["queued"] or 0),
                "completed_tasks": int(tasks["completed"] or 0),
                "runs_total": total_runs,
                "completed_runs": completed_runs,
                "failed_runs": int(runs["failed"] or 0),
                "quality_rate": round(completed_runs / total_runs, 3) if total_runs else None,
                "cost": float(runs["cost"] or 0) if total_runs else None,
                "tokens": int(runs["tokens"] or 0) if total_runs else None,
                "average_runtime_ms": round(float(runs["average_runtime_ms"]), 1) if runs["average_runtime_ms"] is not None else None,
                "last_run_at": runs["last_run_at"],
                "budget": None,
                "budget_status": "not_configured",
            }
        return agents

    def _agency_map(self, organization_id: str, workspace_ids: list[str], clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not workspace_ids:
            return []
        marks = ",".join("?" for _ in workspace_ids)
        nodes = [{
            "id": client["id"], "kind": "client", "workspace_id": client["id"], "label": client["name"],
            "state": client["attention"], "health": client["health"], "source": "workspaces",
        } for client in clients]
        definitions = (
            ("project", "projects", "id,name,status", "name"),
            ("campaign", "campaigns", "id,name,status", "name"),
            ("workflow", "workflow_runs", "id,definition_name AS name,status", "name"),
        )
        for kind, table, columns, label_key in definitions:
            rows = self.conn.execute(
                f"SELECT {columns},workspace_id FROM {table} WHERE workspace_id IN ({marks}) ORDER BY workspace_id,id",
                workspace_ids,
            ).fetchall()
            nodes.extend({
                "id": row["id"], "kind": kind, "workspace_id": row["workspace_id"],
                "label": row[label_key] or row["id"], "state": row["status"], "health": None, "source": table,
            } for row in rows)
        return nodes

    def _command_trends(self, workspace_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not workspace_ids:
            return {"work_created": [], "reviews_opened": [], "campaign_metrics": []}
        marks = ",".join("?" for _ in workspace_ids)
        def series(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in self.conn.execute(sql, workspace_ids).fetchall()]
        return {
            "work_created": series(f"SELECT substr(created_at,1,10) AS date,COUNT(*) AS value FROM work_items WHERE workspace_id IN ({marks}) GROUP BY substr(created_at,1,10) ORDER BY date"),
            "reviews_opened": series(f"SELECT substr(opened_at,1,10) AS date,COUNT(*) AS value FROM reviews WHERE workspace_id IN ({marks}) GROUP BY substr(opened_at,1,10) ORDER BY date"),
            "campaign_metrics": series(f"""SELECT substr(m.captured_at,1,10) AS date,COUNT(*) AS value
                FROM campaign_metric_snapshots m JOIN campaigns c ON c.id=m.campaign_id
                WHERE c.workspace_id IN ({marks}) GROUP BY substr(m.captured_at,1,10) ORDER BY date"""),
        }
