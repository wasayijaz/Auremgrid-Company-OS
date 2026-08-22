from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


CONTENT_STAGES = ("idea","research","brief","script","design","review","approved","scheduled","published","measured")


class AgencyOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any], company: Any) -> None:
        self.conn, self.new_id, self.authorize, self.company = conn, new_id, authorize, company

    def create_campaign(self, organization_id: str, workspace_id: str, person_id: str, name: str,
        objective: str, platform: str, project_id: str | None = None, budget: float | None = None,
        currency: str = "USD", start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if project_id and self.company.get_project(workspace_id,project_id) is None: raise NotFoundError("project not found")
        if not name.strip() or not objective.strip() or not platform.strip(): raise ValidationError("campaign name, objective, and platform are required")
        if budget is not None and budget < 0: raise ValidationError("campaign budget cannot be negative")
        now=_now().isoformat(); item={"id":self.new_id("campaign"),"organization_id":organization_id,"workspace_id":workspace_id,
            "project_id":project_id,"name":name,"objective":objective,"platform":platform,"budget":budget,"currency":currency,
            "start_date":start_date,"end_date":end_date,"status":"draft","owner_person_id":person_id,"created_at":now,"updated_at":now}
        self.conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def record_campaign_metrics(self, organization_id: str, workspace_id: str, person_id: str, campaign_id: str,
        source: str, spend: float | None = None, revenue: float | None = None, leads: float | None = None,
        impressions: float | None = None, clicks: float | None = None) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not self.conn.execute("SELECT id FROM campaigns WHERE organization_id=? AND workspace_id=? AND id=?",(organization_id,workspace_id,campaign_id)).fetchone(): raise NotFoundError("campaign not found")
        if any(value is not None and value < 0 for value in (spend, revenue, leads, impressions, clicks)): raise ValidationError("campaign metrics cannot be negative")
        def ratio(a: float | None,b: float | None,m: float=1.0) -> float | None: return round(a/b*m,4) if a is not None and b else None
        item={"id":self.new_id("metric"),"organization_id":organization_id,"workspace_id":workspace_id,"campaign_id":campaign_id,
            "captured_at":_now().isoformat(),"spend":spend,"revenue":revenue,"leads":leads,"impressions":impressions,"clicks":clicks,
            "cpl":ratio(spend,leads),"cac":None,"ctr":ratio(clicks,impressions,100),"cvr":ratio(leads,clicks,100),"roas":ratio(revenue,spend),"source":source}
        self.conn.execute("INSERT INTO campaign_metric_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def campaign_performance(self, organization_id: str, workspace_id: str, person_id: str, campaign_id: str) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id)
        campaign=self.conn.execute("SELECT * FROM campaigns WHERE organization_id=? AND workspace_id=? AND id=?",(organization_id,workspace_id,campaign_id)).fetchone()
        if campaign is None: raise NotFoundError("campaign not found")
        metric=self.conn.execute("SELECT * FROM campaign_metric_snapshots WHERE campaign_id=? ORDER BY captured_at DESC LIMIT 1",(campaign_id,)).fetchone()
        return {"campaign":dict(campaign),"metrics":dict(metric) if metric else {"status":"not_connected"}}

    def create_creative(self, organization_id: str, workspace_id: str, person_id: str, title: str,
        format: str, project_id: str | None = None, campaign_id: str | None = None, platform: str | None = None,
        dimensions: str | None = None, style_tags: list[str] | None = None, source_url: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if project_id and self.company.get_project(workspace_id, project_id) is None: raise NotFoundError("project not found")
        if campaign_id and not self.conn.execute("SELECT id FROM campaigns WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, campaign_id)).fetchone(): raise NotFoundError("campaign not found")
        if not title.strip() or not format.strip(): raise ValidationError("creative title and format are required")
        item={"id":self.new_id("creative"),"organization_id":organization_id,"workspace_id":workspace_id,"project_id":project_id,
            "campaign_id":campaign_id,"title":title,"platform":platform,"format":format,"dimensions":dimensions,"creator_person_id":person_id,
            "reviewer_person_id":None,"approval_state":"draft","source_url":source_url,"final_url":None,"thumbnail_url":None,
            "revision_count":0,"style_tags":json.dumps(style_tags or []),"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO creative_assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def search_creative(self, organization_id: str, workspace_id: str, person_id: str, query: str = "",
        approval_state: str | None = None, campaign_id: str | None = None) -> list[dict[str, Any]]:
        self.authorize(organization_id,workspace_id,person_id); sql="SELECT * FROM creative_assets WHERE organization_id=? AND workspace_id=?"; values=[organization_id,workspace_id]
        if query: sql+=" AND (lower(title) LIKE ? OR lower(style_tags) LIKE ?)"; values.extend([f"%{query.lower()}%"]*2)
        if approval_state: sql+=" AND approval_state=?"; values.append(approval_state)
        if campaign_id: sql+=" AND campaign_id=?"; values.append(campaign_id)
        return [dict(row) for row in self.conn.execute(sql+" ORDER BY created_at DESC",values).fetchall()]

    def record_creative_performance(self, organization_id: str, workspace_id: str, person_id: str,
        asset_id: str, source: str, campaign_id: str | None = None, impressions: float | None = None,
        clicks: float | None = None, conversions: float | None = None, spend: float | None = None,
        revenue: float | None = None) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not self.conn.execute("SELECT id FROM creative_assets WHERE organization_id=? AND workspace_id=? AND id=?",(organization_id,workspace_id,asset_id)).fetchone():raise NotFoundError("creative asset not found")
        if campaign_id and not self.conn.execute("SELECT id FROM campaigns WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, campaign_id)).fetchone(): raise NotFoundError("campaign not found")
        if any(value is not None and value < 0 for value in (impressions, clicks, conversions, spend, revenue)): raise ValidationError("creative metrics cannot be negative")
        ratio=lambda a,b,m=1:round(a/b*m,4) if a is not None and b else None
        item={"id":self.new_id("creativeperf"),"asset_id":asset_id,"campaign_id":campaign_id,"captured_at":_now().isoformat(),
            "impressions":impressions,"clicks":clicks,"conversions":conversions,"spend":spend,"revenue":revenue,
            "ctr":ratio(clicks,impressions,100),"cvr":ratio(conversions,clicks,100),"roas":ratio(revenue,spend),"source":source}
        self.conn.execute("INSERT INTO creative_performance VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def create_content(self, organization_id: str, workspace_id: str, person_id: str, title: str,
        objective: str, audience: str, hook: str = "", copy: str = "", project_id: str | None = None,
        channel_id: str | None = None, references: list[str] | None = None, brain_context: str = "") -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if project_id and self.company.get_project(workspace_id, project_id) is None: raise NotFoundError("project not found")
        if channel_id and not self.conn.execute("SELECT id FROM content_channels WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, channel_id)).fetchone(): raise NotFoundError("content channel not found")
        if not title.strip() or not objective.strip() or not audience.strip(): raise ValidationError("content title, objective, and audience are required")
        now=_now().isoformat()
        item={"id":self.new_id("content"),"organization_id":organization_id,"workspace_id":workspace_id,"project_id":project_id,
            "channel_id":channel_id,"title":title,"stage":"idea","objective":objective,"audience":audience,"hook":hook,"copy":copy,
            "creative_asset_id":None,"references_json":json.dumps(references or []),"brain_context":brain_context,"publish_at":None,
            "published_at":None,"parent_content_id":None,"owner_person_id":person_id,"created_at":now,"updated_at":now}
        self.conn.execute("INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def advance_content(self, organization_id: str, workspace_id: str, person_id: str, content_id: str, to_stage: str) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        row=self.conn.execute("SELECT * FROM content_items WHERE organization_id=? AND workspace_id=? AND id=?",(organization_id,workspace_id,content_id)).fetchone()
        if row is None: raise NotFoundError("content item not found")
        current=CONTENT_STAGES.index(row["stage"])
        if to_stage not in CONTENT_STAGES or CONTENT_STAGES.index(to_stage)!=current+1: raise ValidationError("content must advance one stage at a time")
        published=_now().isoformat() if to_stage=="published" else row["published_at"]
        self.conn.execute("UPDATE content_items SET stage=?,published_at=?,updated_at=? WHERE id=?",(to_stage,published,_now().isoformat(),content_id)); self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM content_items WHERE id=?",(content_id,)).fetchone())

    def record_content_performance(self, organization_id: str, workspace_id: str, person_id: str,
        content_item_id: str, source: str, impressions: float | None = None, engagements: float | None = None,
        clicks: float | None = None, conversions: float | None = None) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not self.conn.execute("SELECT id FROM content_items WHERE workspace_id=? AND id=?",(workspace_id,content_item_id)).fetchone():raise NotFoundError("content item not found")
        item={"id":self.new_id("contentperf"),"content_item_id":content_item_id,"captured_at":_now().isoformat(),
            "impressions":impressions,"engagements":engagements,"clicks":clicks,"conversions":conversions,"source":source}
        self.conn.execute("INSERT INTO content_performance VALUES (?,?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def set_availability(self, organization_id: str, person_id: str, week_start: str, available_hours: float) -> dict[str, Any]:
        if self.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("person is not an organization member")
        item={"id":self.new_id("availability"),"organization_id":organization_id,"person_id":person_id,"week_start":week_start,"available_hours":available_hours}
        self.conn.execute("""INSERT INTO availability VALUES (?,?,?,?,?) ON CONFLICT(person_id,week_start)
        DO UPDATE SET available_hours=excluded.available_hours""",tuple(item.values())); self.conn.commit(); return item

    def create_skill(self, organization_id: str, person_id: str, name: str, category: str) -> dict[str,Any]:
        membership=self.company.org_membership(organization_id,person_id)
        if membership is None or membership.role not in {"owner","admin"}: raise AuthorizationError("organization admin required")
        item={"id":self.new_id("skill"),"organization_id":organization_id,"name":name,"category":category}
        self.conn.execute("INSERT INTO skills VALUES (?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def assign_skill(self, organization_id: str, requester_person_id: str, person_id: str, skill_id: str, level: int) -> None:
        membership=self.company.org_membership(organization_id,requester_person_id)
        if membership is None or membership.role not in {"owner","admin"}: raise AuthorizationError("organization admin required")
        if not 1<=level<=5: raise ValidationError("skill level must be 1 to 5")
        if self.company.get_person(organization_id,person_id) is None or not self.conn.execute("SELECT id FROM skills WHERE organization_id=? AND id=?",(organization_id,skill_id)).fetchone(): raise NotFoundError("person or skill not found")
        self.conn.execute("INSERT OR REPLACE INTO person_skills VALUES (?,?,?)",(person_id,skill_id,level));self.conn.commit()

    def record_leave(self, organization_id: str, requester_person_id: str, person_id: str,
        start_date: str, end_date: str, hours: float, status: str = "approved") -> dict[str,Any]:
        membership=self.company.org_membership(organization_id,requester_person_id)
        if membership is None or membership.role not in {"owner","admin"}: raise AuthorizationError("organization admin required")
        item={"id":self.new_id("leave"),"organization_id":organization_id,"person_id":person_id,"start_date":start_date,"end_date":end_date,"hours":hours,"status":status}
        self.conn.execute("INSERT INTO leave_records VALUES (?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def people_directory(self, organization_id: str, requester_person_id: str) -> list[dict[str,Any]]:
        if self.company.org_membership(organization_id,requester_person_id) is None: raise AuthorizationError("organization membership required")
        result=[]
        for person in self.company.list_people(organization_id):
            skills=[dict(r) for r in self.conn.execute("SELECT s.name,s.category,ps.level FROM person_skills ps JOIN skills s ON s.id=ps.skill_id WHERE ps.person_id=?",(person.id,)).fetchall()]
            clients=[dict(r) for r in self.conn.execute("SELECT w.id,w.name,wm.role FROM workspace_memberships wm JOIN workspaces w ON w.id=wm.workspace_id JOIN workspace_organization wo ON wo.workspace_id=w.id WHERE wm.person_id=? AND wo.kind='client'",(person.id,)).fetchall()]
            work=self.conn.execute("SELECT COUNT(*) FROM work_items WHERE assignee_person_id=? AND status!='shipped'",(person.id,)).fetchone()[0]
            result.append({**person.to_dict(),"skills":skills,"active_clients":clients,"open_work":work})
        return result

    def calculate_capacity(self, organization_id: str, requester_person_id: str, person_id: str, week_start: str,
        estimated_assigned_hours: float, booked_hours: float) -> dict[str, Any]:
        requester=self.company.org_membership(organization_id,requester_person_id)
        if requester is None: raise AuthorizationError("person is not an organization member")
        row=self.conn.execute("SELECT available_hours FROM availability WHERE person_id=? AND week_start=?",(person_id,week_start)).fetchone()
        available=float(row[0]) if row else 0.0; remaining=available-max(estimated_assigned_hours,booked_hours)
        item={"id":self.new_id("capacity"),"organization_id":organization_id,"person_id":person_id,"week_start":week_start,
            "available_hours":available,"estimated_assigned_hours":estimated_assigned_hours,"booked_hours":booked_hours,
            "remaining_hours":remaining,"overloaded":int(remaining<0),"calculated_at":_now().isoformat()}
        self.conn.execute("INSERT INTO capacity_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return {**item,"overloaded":bool(item["overloaded"])}

    def finance_status(self, organization_id: str, requester_person_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        if self.company.org_membership(organization_id,requester_person_id) is None: raise AuthorizationError("person is not an organization member")
        connection=self.conn.execute("SELECT * FROM finance_connections WHERE organization_id=?",(organization_id,)).fetchone()
        if connection is None or connection["status"]!="connected":
            return {"status":"not_connected","mrr":None,"outstanding_revenue":None,"client_margin":None}
        where="organization_id=?"; values=[organization_id]
        if workspace_id: where+=" AND workspace_id=?"; values.append(workspace_id)
        revenue=self.conn.execute(f"SELECT COALESCE(SUM(amount),0) FROM revenues WHERE {where}",values).fetchone()[0]
        outstanding=self.conn.execute(f"SELECT COALESCE(SUM(amount),0) FROM invoices WHERE {where} AND status IN ('issued','overdue')",values).fetchone()[0]
        return {"status":"connected","recognized_revenue":revenue,"outstanding_revenue":outstanding,"source":connection["provider"]}

    def connect_finance(self, organization_id: str, person_id: str, provider: str) -> dict[str,Any]:
        membership=self.company.org_membership(organization_id,person_id)
        if membership is None or membership.role not in {"owner","admin"}: raise AuthorizationError("organization admin required")
        self.conn.execute("INSERT INTO finance_connections VALUES (?,?,?,?,?) ON CONFLICT(organization_id) DO UPDATE SET status='connected',provider=excluded.provider,last_error=NULL",(organization_id,"connected",provider,_now().isoformat(),None));self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM finance_connections WHERE organization_id=?",(organization_id,)).fetchone())

    def record_invoice(self, organization_id: str, workspace_id: str, person_id: str, amount: float,
        issued_at: str, due_at: str, source: str, currency: str = "USD", external_id: str | None = None,
        status: str = "issued") -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        connection=self.conn.execute("SELECT status FROM finance_connections WHERE organization_id=?",(organization_id,)).fetchone()
        if connection is None or connection[0]!="connected": raise ValidationError("finance is not connected")
        item={"id":self.new_id("invoice"),"organization_id":organization_id,"workspace_id":workspace_id,"external_id":external_id,
            "amount":amount,"currency":currency,"status":status,"issued_at":issued_at,"due_at":due_at,"paid_at":None,"source":source}
        self.conn.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def record_revenue(self, organization_id: str, workspace_id: str | None, person_id: str, amount: float,
        recognized_at: str, source: str, kind: str = "retainer", currency: str = "USD", project_id: str | None = None) -> dict[str,Any]:
        if workspace_id:self.authorize(organization_id,workspace_id,person_id,write=True)
        elif self.company.org_membership(organization_id,person_id) is None:raise AuthorizationError("organization membership required")
        connection=self.conn.execute("SELECT status FROM finance_connections WHERE organization_id=?",(organization_id,)).fetchone()
        if connection is None or connection[0]!="connected": raise ValidationError("finance is not connected")
        item={"id":self.new_id("revenue"),"organization_id":organization_id,"workspace_id":workspace_id,"project_id":project_id,
            "amount":amount,"currency":currency,"kind":kind,"recognized_at":recognized_at,"source":source}
        self.conn.execute("INSERT INTO revenues VALUES (?,?,?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def request_approval(self, organization_id: str, requested_by_type: str, requested_by_id: str,
        requested_for: str, action_type: str, payload: dict[str, Any], reason: str, policy: str = "human",
        workspace_id: str | None = None, approver_person_id: str | None = None) -> dict[str, Any]:
        if policy not in {"auto","human","admin_only"}: raise ValidationError("invalid approval policy")
        if policy != "auto" and not approver_person_id: raise ValidationError("human approval requires an approver")
        requester = self.company.get_person(organization_id, requested_by_id) if requested_by_type == "person" else None
        if requested_by_type == "person" and requester is None: raise NotFoundError("requesting person not found")
        if approver_person_id is not None:
            approver = self.company.org_membership(organization_id, approver_person_id)
            if approver is None or approver.role not in {"owner", "admin"}: raise AuthorizationError("approver must be an organization admin")
        if workspace_id:
            scope = self.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != organization_id: raise NotFoundError("workspace not found")
        now=_now().isoformat(); status="approved" if policy=="auto" else "pending"
        item={"id":self.new_id("approval"),"organization_id":organization_id,"workspace_id":workspace_id,
            "requested_by_type":requested_by_type,"requested_by_id":requested_by_id,"requested_for":requested_for,
            "action_type":action_type,"payload":json.dumps(payload),"reason":reason,"approver_person_id":approver_person_id,
            "policy":policy,"status":status,"approved_at":now if status=="approved" else None,"rejected_at":None,
            "comments":"","created_at":now}
        self.conn.execute("INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def decide_approval(self, organization_id: str, approver_person_id: str, approval_id: str, approved: bool, comments: str = "") -> dict[str, Any]:
        approver = self.company.org_membership(organization_id, approver_person_id)
        if approver is None or approver.role not in {"owner", "admin"}: raise AuthorizationError("organization admin required")
        row=self.conn.execute("SELECT * FROM approval_requests WHERE organization_id=? AND id=?",(organization_id,approval_id)).fetchone()
        if row is None: raise NotFoundError("approval not found")
        if row["status"]!="pending" or row["approver_person_id"]!=approver_person_id: raise AuthorizationError("approval cannot be decided by this person")
        now=_now().isoformat(); status="approved" if approved else "rejected"
        self.conn.execute("UPDATE approval_requests SET status=?,approved_at=?,rejected_at=?,comments=? WHERE id=?",
            (status,now if approved else None,None if approved else now,comments,approval_id)); self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM approval_requests WHERE id=?",(approval_id,)).fetchone())

    def create_notification(self, organization_id: str, recipient_person_id: str, reason: str,
        source_type: str, source_id: str | None = None, workspace_id: str | None = None,
        severity: float = 0.5, urgency: float = 0.5, waiting_days: float = 0, actionable: bool = True) -> dict[str, Any]:
        priority=round(min(1.0,severity*.45+urgency*.4+min(waiting_days/30,1)*.15),4)
        item={"id":self.new_id("notification"),"organization_id":organization_id,"recipient_person_id":recipient_person_id,
            "priority":priority,"reason":reason,"source_type":source_type,"source_id":source_id,"workspace_id":workspace_id,
            "actionable":int(actionable),"created_at":_now().isoformat(),"read_at":None,"resolved_at":None}
        self.conn.execute("INSERT INTO notifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def attention(self, organization_id: str, person_id: str, limit: int = 3) -> list[dict[str, Any]]:
        if self.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("person is not an organization member")
        rows=self.conn.execute("SELECT * FROM notifications WHERE organization_id=? AND recipient_person_id=? AND resolved_at IS NULL ORDER BY priority DESC,created_at LIMIT ?",(organization_id,person_id,limit)).fetchall()
        return [dict(row) for row in rows]
