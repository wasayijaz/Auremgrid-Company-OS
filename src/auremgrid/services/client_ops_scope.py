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


class ClientOperationsScopeMixin:
    def create_contract(self, organization_id: str, workspace_id: str, person_id: str, kind: str,
        billing_model: str, start_date: str, value: float | None = None, currency: str = "USD",
        end_date: str | None = None, renewal_date: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        contract={"id":self.new_id("contract"),"organization_id":organization_id,"workspace_id":workspace_id,"kind":kind,
            "billing_model":billing_model,"value":value,"currency":currency,"start_date":start_date,"end_date":end_date,
            "renewal_date":renewal_date,"status":"active","created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",tuple(contract.values())); self.conn.commit()
        return contract

    def add_scope_allowance(self, organization_id: str, workspace_id: str, person_id: str, contract_id: str,
        service_category: str, period: str, included_quantity: float | None = None,
        included_hours: float | None = None, revision_limit: int | None = None) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        contract=self.conn.execute("SELECT id FROM contracts WHERE organization_id=? AND workspace_id=? AND id=?",(organization_id,workspace_id,contract_id)).fetchone()
        if not contract: raise NotFoundError("contract not found")
        item={"id":self.new_id("allowance"),"contract_id":contract_id,"service_category":service_category,"period":period,
            "included_quantity":included_quantity,"included_hours":included_hours,"revision_limit":revision_limit}
        self.conn.execute("INSERT INTO scope_allowances VALUES (?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def record_scope_usage(self, organization_id: str, workspace_id: str, person_id: str, contract_id: str,
        allowance_id: str, period_start: str, delivered: float, in_review: float = 0, requested: float = 0,
        used_hours: float = 0) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if any(value < 0 for value in (delivered, in_review, requested, used_hours)):
            raise ValidationError("scope usage values cannot be negative")
        allowance=self.conn.execute("""SELECT a.*,c.organization_id,c.workspace_id FROM scope_allowances a
            JOIN contracts c ON c.id=a.contract_id
            WHERE a.contract_id=? AND a.id=? AND c.organization_id=? AND c.workspace_id=?""",(contract_id,allowance_id,organization_id,workspace_id)).fetchone()
        if allowance is None: raise NotFoundError("scope allowance not found")
        item={"id":self.new_id("usage"),"organization_id":organization_id,"workspace_id":workspace_id,"contract_id":contract_id,
            "allowance_id":allowance_id,"period_start":period_start,"delivered_quantity":delivered,"in_review_quantity":in_review,
            "requested_quantity":requested,"used_hours":used_hours,"calculated_at":_now().isoformat()}
        self.conn.execute("INSERT INTO scope_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit()
        usage_for_score = {**item, "included_quantity": allowance["included_quantity"], "included_hours": allowance["included_hours"]}
        percentage = _usage_totals(usage_for_score)["percentage"]
        if percentage is not None and percentage>100:
            self.create_risk(organization_id,workspace_id,person_id,"scope","high",min(percentage/200,1),
                f"Scope usage is {percentage:.0f}%",json.dumps(item),"Review change order or retainer expansion")
            self.create_opportunity(organization_id,workspace_id,person_id,"scope_expansion",f"Scope usage is {percentage:.0f}%",
                json.dumps(item),"Propose expanded allowance")
        return {**item,"usage_percent":percentage}

    def scope_status(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        contracts = [dict(row) for row in self.conn.execute(
            "SELECT * FROM contracts WHERE organization_id=? AND workspace_id=? ORDER BY start_date DESC,created_at DESC,id",
            (organization_id, workspace_id),
        ).fetchall()]
        if not contracts:
            return {
                "status": "no_contract",
                "workspace_id": workspace_id,
                "contracts": [],
                "allowances": [],
                "summary": {"allowances": 0, "over_scope": 0, "unknown": 0, "recorded": 0},
            }
        allowances: list[dict[str, Any]] = []
        for contract in contracts:
            rows = self.conn.execute(
                "SELECT * FROM scope_allowances WHERE contract_id=? ORDER BY service_category,period,id",
                (contract["id"],),
            ).fetchall()
            for allowance in rows:
                history_rows = self.conn.execute(
                    """SELECT u.*,a.included_quantity,a.included_hours,a.revision_limit
                       FROM scope_usage u JOIN scope_allowances a ON a.id=u.allowance_id
                       WHERE u.organization_id=? AND u.workspace_id=? AND u.allowance_id=?
                       ORDER BY u.period_start DESC,u.calculated_at DESC,u.id DESC""",
                    (organization_id, workspace_id, allowance["id"]),
                ).fetchall()
                history = []
                for usage in history_rows:
                    totals = _usage_totals(usage)
                    history.append({**dict(usage), **totals, "status": self._scope_usage_state(totals["percentage"])})
                latest = history[0] if history else None
                linked = self._scope_generated_links(organization_id, workspace_id, allowance["id"], latest["id"] if latest else None)
                allowance_status = latest["status"] if latest else "no_usage"
                allowances.append({
                    **dict(allowance),
                    "contract_id": contract["id"],
                    "contract_status": contract["status"],
                    "status": allowance_status,
                    "latest_usage": latest,
                    "period_history": history,
                    "generated": linked,
                })
        status_counts = {
            "allowances": len(allowances),
            "over_scope": sum(item["status"] == "over_scope" for item in allowances),
            "unknown": sum(item["status"] == "unknown" for item in allowances),
            "recorded": sum(item["status"] == "recorded" for item in allowances),
            "no_usage": sum(item["status"] == "no_usage" for item in allowances),
        }
        status = (
            "no_allowances" if not allowances else
            "over_scope" if status_counts["over_scope"] else
            "no_usage" if status_counts["no_usage"] == len(allowances) else
            "unknown" if status_counts["unknown"] or status_counts["no_usage"] else
            "recorded"
        )
        return {
            "status": status,
            "workspace_id": workspace_id,
            "contracts": contracts,
            "allowances": allowances,
            "summary": status_counts,
        }
