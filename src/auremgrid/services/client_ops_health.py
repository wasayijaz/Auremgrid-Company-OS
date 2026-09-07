from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.client_ops import (
    ClientAccountRoster,
    ClientAccountRosterRole,
    ClientHealthSnapshot,
    Conversation,
    Meeting,
    MeetingResponsibilities,
    Message,
    Opportunity,
    Risk,
    Signal,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError

from .client_ops_shared import (
    OPPORTUNITY_ACTIVE_STATUSES,
    OPPORTUNITY_TERMINAL_STATUSES,
    WING_ROLES,
    _json,
    _load_json_object,
    _norm_role,
    _norm_wing,
    _now,
    _parse_dt,
    _usage_totals,
)


class ClientOperationsHealthMixin:
    def explain_health(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        overdue = [dict(row) for row in self.conn.execute(
            """SELECT id,title,status,COALESCE(deadline,needed_by) AS due_at,assignee_person_id
               FROM work_items
               WHERE workspace_id=? AND status!='shipped'
                 AND COALESCE(deadline,needed_by) IS NOT NULL
                 AND COALESCE(deadline,needed_by) < date('now')
               ORDER BY due_at,id""",
            (workspace_id,),
        ).fetchall()]
        unanswered = self.unanswered_messages(organization_id, workspace_id, person_id)
        open_risks = [dict(row) for row in self.conn.execute(
            "SELECT * FROM risks WHERE organization_id=? AND workspace_id=? AND status='open' ORDER BY detected_at DESC,id",
            (organization_id, workspace_id),
        ).fetchall()]
        scope = self.scope_status(organization_id, workspace_id, person_id)
        reasons: list[str] = []
        delivery = 100.0
        communication = 100.0
        relationship = 100.0
        scope_score = 100.0
        if overdue:
            delivery = max(0, 100 - len(overdue) * 12)
            reasons.append(f"{len(overdue)} overdue work items")
        if unanswered:
            communication = max(0, 100 - len(unanswered) * 10)
            reasons.append(f"{len(unanswered)} unanswered client messages")
        if open_risks:
            relationship = max(0, 100 - len(open_risks) * 8)
            reasons.append(f"{len(open_risks)} open risks")
        latest_percentages = [
            item["latest_usage"]["percentage"] for item in scope.get("allowances", [])
            if item.get("latest_usage") and item["latest_usage"].get("percentage") is not None
        ]
        scope_percentage = max(latest_percentages) if latest_percentages else None
        if scope_percentage is not None and scope_percentage > 100:
            scope_score = max(0, 100 - (scope_percentage - 100))
            reasons.append(f"scope usage {scope_percentage:.0f}%")
        overall = round((delivery + communication + scope_score + relationship) / 4, 1)
        previous = self.conn.execute(
            "SELECT * FROM client_health_snapshots WHERE workspace_id=? ORDER BY calculated_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        previous_score = float(previous["overall"]) if previous else None
        trend = "stable" if previous_score is None or previous_score == overall else ("up" if overall > previous_score else "down")
        components = {
            "delivery": {"score": delivery, "evidence_refs": [{"table": "work_items", "id": row["id"]} for row in overdue]},
            "communication": {"score": communication, "evidence_refs": [{"table": "messages", "id": row["id"]} for row in unanswered]},
            "relationship": {"score": relationship, "evidence_refs": [{"table": "risks", "id": row["id"]} for row in open_risks]},
            "scope": {
                "score": scope_score,
                "status": scope["status"],
                "percentage": scope_percentage,
                "evidence_refs": [
                    {"table": "scope_usage", "id": item["latest_usage"]["id"]}
                    for item in scope.get("allowances", []) if item.get("latest_usage")
                ],
            },
            "performance": {"score": None, "status": "unknown", "evidence_refs": []},
            "finance": {"score": None, "status": "unknown", "evidence_refs": []},
        }
        return {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "overall": overall,
            "relationship": relationship,
            "delivery": delivery,
            "performance": None,
            "finance": None,
            "communication": communication,
            "scope": scope_score,
            "sentiment": None,
            "components": components,
            "evidence": {
                "overdue_work": overdue,
                "unanswered_messages": unanswered,
                "open_risks": open_risks,
                "scope": scope,
            },
            "contributing_signals": reasons,
            "explanation": "; ".join(reasons) or "No negative operational signals",
            "previous_score": previous_score,
            "trend": trend,
            "latest_snapshot": dict(previous) if previous else None,
        }

    def calculate_health(self, organization_id: str, workspace_id: str, person_id: str) -> ClientHealthSnapshot:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        explained = self.explain_health(organization_id, workspace_id, person_id)
        item=ClientHealthSnapshot(self.new_id("health"),organization_id,workspace_id,explained["overall"],explained["relationship"],explained["delivery"],None,None,
            explained["communication"],explained["scope"],None,tuple(explained["contributing_signals"]),explained["explanation"],explained["previous_score"],explained["trend"],_now())
        self.conn.execute("INSERT INTO client_health_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.organization_id,item.workspace_id,item.overall,item.relationship,item.delivery,item.performance,item.finance,
            item.communication,item.scope,item.sentiment,json.dumps(item.contributing_signals),item.explanation,item.previous_score,item.trend,item.calculated_at.isoformat()))
        self.conn.commit(); return item
