from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class RevenueOperations:
    """Agency sales, pacing, retainer economics, and internal report-pack ledger."""

    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any], company: Any) -> None:
        self.conn, self.new_id, self.authorize, self.company = conn, new_id, authorize, company

    def _event(self, org: str, workspace: str, entity_type: str, entity_id: str, action: str, actor: str, payload: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO sales_events VALUES (?,?,?,?,?,?,?,?,?)",
            (self.new_id("sales_event"), org, workspace, entity_type, entity_id, action, actor,
             json.dumps(payload or {}, separators=(",", ":")), _now()),
        )

    def create_prospect(self, organization_id: str, workspace_id: str, person_id: str, name: str,
                        company_name: str, contact_email: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if not name.strip() or not company_name.strip():
            raise ValidationError("prospect name and company name are required")
        now = _now(); item = {
            "id": self.new_id("prospect"), "organization_id": organization_id, "workspace_id": workspace_id,
            "name": name.strip(), "company_name": company_name.strip(), "contact_email": contact_email,
            "status": "new", "owner_person_id": person_id, "created_at": now, "updated_at": now,
        }
        self.conn.execute("INSERT INTO sales_prospects VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
        self._event(organization_id, workspace_id, "prospect", item["id"], "created", person_id)
        self.conn.commit(); return item

    def list_prospects(self, organization_id: str, workspace_id: str, person_id: str, status: str | None = None) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        sql = "SELECT * FROM sales_prospects WHERE organization_id=? AND workspace_id=?"; args: list[Any] = [organization_id, workspace_id]
        if status: sql += " AND status=?"; args.append(status)
        sql += " ORDER BY updated_at DESC,id"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def create_proposal(self, organization_id: str, workspace_id: str, person_id: str, prospect_id: str,
                        title: str, amount: float, currency: str = "USD", valid_until: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if amount < 0 or not title.strip(): raise ValidationError("proposal title and non-negative amount are required")
        prospect = self.conn.execute("SELECT * FROM sales_prospects WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, prospect_id)).fetchone()
        if prospect is None: raise NotFoundError("prospect not found")
        now = _now(); item = {"id": self.new_id("proposal"), "organization_id": organization_id, "workspace_id": workspace_id,
            "prospect_id": prospect_id, "title": title.strip(), "amount": amount, "currency": currency.strip() or "USD",
            "status": "draft", "valid_until": valid_until, "created_at": now, "updated_at": now}
        self.conn.execute("INSERT INTO sales_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
        self.conn.execute("UPDATE sales_prospects SET status='proposal',updated_at=? WHERE id=?", (now, prospect_id))
        self._event(organization_id, workspace_id, "proposal", item["id"], "created", person_id, {"prospect_id": prospect_id})
        self.conn.commit(); return item

    def list_proposals(self, organization_id: str, workspace_id: str, person_id: str, status: str | None = None) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        sql = "SELECT * FROM sales_proposals WHERE organization_id=? AND workspace_id=?"; args: list[Any] = [organization_id, workspace_id]
        if status: sql += " AND status=?"; args.append(status)
        sql += " ORDER BY updated_at DESC,id"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def convert_to_client(self, organization_id: str, workspace_id: str, person_id: str, proposal_id: str,
                          client_name: str, contract_kind: str = "retainer", billing_model: str = "monthly",
                          start_date: str | None = None, end_date: str | None = None,
                          idempotency_key: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if not idempotency_key: raise ValidationError("idempotency_key is required")
        prior = self.conn.execute("SELECT * FROM sales_conversions WHERE organization_id=? AND idempotency_key=?", (organization_id, idempotency_key)).fetchone()
        if prior: return dict(prior)
        proposal = self.conn.execute("SELECT * FROM sales_proposals WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, proposal_id)).fetchone()
        if proposal is None: raise NotFoundError("proposal not found")
        prospect = self.conn.execute("SELECT * FROM sales_prospects WHERE id=?", (proposal["prospect_id"],)).fetchone()
        if prospect is None: raise NotFoundError("prospect not found")
        if proposal["status"] not in {"won", "draft", "sent"}: raise ValidationError("proposal is not convertible")
        now = _now(); client_workspace_id = self.new_id("client_ws")
        self.conn.execute("INSERT INTO workspaces(id,name,created_at) VALUES (?,?,?)", (client_workspace_id, client_name.strip() or prospect["company_name"], now))
        self.conn.execute("INSERT INTO workspace_organization VALUES (?,?,?)", (client_workspace_id, organization_id, "client"))
        contract_id = self.new_id("contract")
        self.conn.execute("INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (contract_id, organization_id, client_workspace_id, contract_kind, billing_model, proposal["amount"], proposal["currency"], start_date or now[:10], end_date, end_date, "active", now))
        conversion_id = self.new_id("conversion")
        self.conn.execute("INSERT INTO sales_conversions VALUES (?,?,?,?,?,?,?,?,?)", (conversion_id, organization_id, workspace_id, proposal["prospect_id"], proposal_id, client_workspace_id, contract_id, idempotency_key, now))
        self.conn.execute("UPDATE sales_proposals SET status='won',updated_at=? WHERE id=?", (now, proposal_id))
        self.conn.execute("UPDATE sales_prospects SET status='converted',updated_at=? WHERE id=?", (now, proposal["prospect_id"]))
        self._event(organization_id, workspace_id, "proposal", proposal_id, "converted", person_id, {"client_workspace_id": client_workspace_id, "contract_id": contract_id})
        self.conn.commit()
        return {**dict(self.conn.execute("SELECT * FROM sales_conversions WHERE id=?", (conversion_id,)).fetchone()), "client_workspace": dict(self.conn.execute("SELECT * FROM workspaces WHERE id=?", (client_workspace_id,)).fetchone()), "contract": dict(self.conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone())}

    def campaign_budget_pacing(self, organization_id: str, workspace_id: str, person_id: str) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        rows = self.conn.execute("SELECT * FROM campaigns WHERE organization_id=? AND workspace_id=? ORDER BY id", (organization_id, workspace_id)).fetchall(); out=[]
        for campaign in rows:
            metric = self.conn.execute("SELECT * FROM campaign_metric_snapshots WHERE campaign_id=? ORDER BY captured_at DESC LIMIT 1", (campaign["id"],)).fetchone()
            if campaign["budget"] is None or metric is None or metric["spend"] is None:
                out.append({"campaign_id": campaign["id"], "status": "insufficient_data", "reason": "campaign budget or sourced spend is missing", "budget": campaign["budget"], "spend": metric["spend"] if metric else None}); continue
            ratio = metric["spend"] / campaign["budget"] if campaign["budget"] else None
            status = "over_paced" if ratio is not None and ratio > 1 else "on_pace"
            out.append({"campaign_id": campaign["id"], "status": status, "budget": campaign["budget"], "spend": metric["spend"], "pacing_ratio": round(ratio, 4), "source": metric["source"]})
        return out

    def retainer_read_model(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        contracts = self.conn.execute("SELECT * FROM contracts WHERE organization_id=? AND workspace_id=? ORDER BY start_date DESC", (organization_id, workspace_id)).fetchall()
        if not contracts: return {"workspace_id": workspace_id, "status": "no_contract", "contracts": []}
        result=[]
        for c in contracts:
            revenue = float(self.conn.execute("SELECT COALESCE(SUM(amount),0) FROM revenues WHERE organization_id=? AND workspace_id=?", (organization_id, workspace_id)).fetchone()[0])
            costs = float(self.conn.execute("SELECT COALESCE(SUM(amount),0) FROM costs WHERE organization_id=? AND workspace_id=?", (organization_id, workspace_id)).fetchone()[0])
            usage = self.conn.execute("SELECT COALESCE(SUM(used_hours),0) used_hours, COUNT(*) periods FROM scope_usage WHERE organization_id=? AND workspace_id=? AND contract_id=?", (organization_id, workspace_id, c["id"])).fetchone()
            included = self.conn.execute("SELECT COALESCE(SUM(included_hours),0) FROM scope_allowances WHERE contract_id=?", (c["id"],)).fetchone()[0]
            result.append({**dict(c), "recognized_revenue": revenue, "recorded_costs": costs, "profit": round(revenue-costs, 2), "margin": round((revenue-costs)/revenue, 4) if revenue else None, "used_hours": float(usage["used_hours"]), "included_hours": float(included), "utilization": round(float(usage["used_hours"])/included, 4) if included else None, "renewal_signal": "missing_end_date" if not c["end_date"] else "renewal_upcoming"})
        return {"workspace_id": workspace_id, "status": "ok", "contracts": result}

    def request_report_pack(self, organization_id: str, workspace_id: str, person_id: str, note: str, report_run_id: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if not note.strip(): raise ValidationError("report pack note is required")
        now = _now(); item=(self.new_id("report_pack"), organization_id, workspace_id, report_run_id, person_id, "pending", note.strip(), now, None, None)
        self.conn.execute("INSERT INTO report_pack_requests VALUES (?,?,?,?,?,?,?,?,?,?)", item)
        self.conn.execute("INSERT INTO report_pack_events VALUES (?,?,?,?,?,?,?,?)", (self.new_id("report_pack_event"),organization_id,workspace_id,item[0],"requested",person_id,note.strip(),now)); self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM report_pack_requests WHERE id=?", (item[0],)).fetchone())

    def decide_report_pack(self, organization_id: str, workspace_id: str, person_id: str, request_id: str, approved: bool, note: str = "") -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        row=self.conn.execute("SELECT * FROM report_pack_requests WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id,workspace_id,request_id)).fetchone()
        if row is None: raise NotFoundError("report pack request not found")
        if row["status"] != "pending": raise ValidationError("report pack request already decided")
        status="approved" if approved else "rejected"; now=_now()
        self.conn.execute("UPDATE report_pack_requests SET status=?,decided_at=?,decided_by_person_id=? WHERE id=?", (status,now,person_id,request_id))
        self.conn.execute("INSERT INTO report_pack_events VALUES (?,?,?,?,?,?,?,?)", (self.new_id("report_pack_event"),organization_id,workspace_id,request_id,status,person_id,note,now)); self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM report_pack_requests WHERE id=?", (request_id,)).fetchone())

    def deliver_report_pack_internal(self, organization_id: str, workspace_id: str, person_id: str, request_id: str, note: str = "") -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        row=self.conn.execute("SELECT * FROM report_pack_requests WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id,workspace_id,request_id)).fetchone()
        if row is None: raise NotFoundError("report pack request not found")
        if row["status"] != "approved": raise ValidationError("report pack must be approved before internal delivery")
        now=_now(); self.conn.execute("UPDATE report_pack_requests SET status='delivered_internal',decided_at=? WHERE id=?", (now,request_id)); self.conn.execute("INSERT INTO report_pack_events VALUES (?,?,?,?,?,?,?,?)", (self.new_id("report_pack_event"),organization_id,workspace_id,request_id,"delivered_internal",person_id,note,now)); self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM report_pack_requests WHERE id=?", (request_id,)).fetchone())

    def list_report_packs(self, organization_id: str, workspace_id: str, person_id: str) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        return [dict(r) for r in self.conn.execute("SELECT * FROM report_pack_requests WHERE organization_id=? AND workspace_id=? ORDER BY created_at DESC,id", (organization_id,workspace_id)).fetchall()]
