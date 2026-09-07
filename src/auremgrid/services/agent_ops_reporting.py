"""Agent reporting and brief generation mixin."""
from __future__ import annotations

import json
from typing import Any

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.agent_ops_shared import (
    _json,
    _now,
)


class AgentOpsReportingMixin:
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

