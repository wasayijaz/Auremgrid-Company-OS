from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError


class DashboardService:
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
            "finance_status":finance["status"],"open_work":open_work,"overdue_work":overdue,"in_review":review,"agents_running":sum(a["status"]=="running" for a in agents),
            "automations_today":automation_count,"open_risks":risks},"attention":attention,"clients":clients,"agents":agents,"pulse":pulse,
            "workspaces":[dict(row) for row in workspaces]}

    def client_hq(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        workspace=self.conn.execute("SELECT * FROM workspaces WHERE id=?",(workspace_id,)).fetchone(); health=self.conn.execute("SELECT * FROM client_health_snapshots WHERE workspace_id=? ORDER BY calculated_at DESC LIMIT 1",(workspace_id,)).fetchone()
        return {"workspace":dict(workspace),"health":dict(health) if health else None,
            "projects":[p.to_dict() for p in self.os.company.list_projects(workspace_id)],
            "work":[w.to_dict() for w in self.os.store.list_work_items(workspace_id)],
            "reviews":[r.to_dict() for r in self.os.company.list_reviews(workspace_id)],
            "risks":self.os.client_ops.list_risks(organization_id,workspace_id,person_id),
            "decisions":[d.to_dict() for d in self.os.company.list_decisions(organization_id,workspace_id)],
            "campaigns":[dict(r) for r in self.conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()],
            "content":[dict(r) for r in self.conn.execute("SELECT * FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()],
            "creative":[dict(r) for r in self.conn.execute("SELECT * FROM creative_assets WHERE workspace_id=? ORDER BY created_at DESC",(workspace_id,)).fetchall()],
            "files":[dict(r) for r in self.conn.execute("""SELECT wf.id,wf.title,wf.url,wf.source,wf.created_at FROM work_files wf JOIN work_items wi ON wi.id=wf.work_item_id WHERE wi.workspace_id=?
                UNION ALL SELECT df.id,df.title,df.url,df.kind,df.created_at FROM deliverable_files df JOIN deliverables d ON d.id=df.deliverable_id WHERE d.workspace_id=?""",(workspace_id,workspace_id)).fetchall()],
            "meetings":[dict(r) for r in self.conn.execute("SELECT * FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC",(workspace_id,)).fetchall()],
            "messages":[dict(r) for r in self.conn.execute("SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.workspace_id=? ORDER BY sent_at DESC",(workspace_id,)).fetchall()],
            "people":[dict(r) for r in self.conn.execute("""SELECT p.id,p.name,p.title,p.department,wm.role FROM workspace_memberships wm JOIN people p ON p.id=wm.person_id WHERE wm.workspace_id=?""",(workspace_id,)).fetchall()]+[dict(r) for r in self.conn.execute("SELECT id,name,role,influence,decision_power,last_contact_at FROM contacts WHERE workspace_id=?",(workspace_id,)).fetchall()],
            "activity":[dict(r) for r in self.conn.execute("SELECT action,entity_type,entity_id,detail,recorded_at FROM ledger_audit WHERE workspace_id=? ORDER BY recorded_at DESC LIMIT 50",(workspace_id,)).fetchall()],
            "finance":self.os.agency_ops.finance_status(organization_id,person_id,workspace_id),
            "brain":self.os.store.get_client_brain(workspace_id).to_dict() if self.os.store.get_client_brain(workspace_id) else None}

    def module(self, organization_id: str, workspace_id: str, person_id: str, module: str) -> dict[str,Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        queries={
            "Campaigns":("campaigns","SELECT id,name,platform,status,budget,updated_at FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Content":("content_items","SELECT id,title,stage,objective,publish_at,updated_at FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Creative":("creative_assets","SELECT id,title,format,platform,approval_state,revision_count,created_at FROM creative_assets WHERE workspace_id=? ORDER BY created_at DESC"),
            "Meetings":("meetings","SELECT id,title,occurred_at,summary,sentiment,source FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC"),
            "Automations":("automations","SELECT id,name,status,approval_policy,created_at FROM automations WHERE organization_id=? ORDER BY created_at DESC"),
            "Reports":("report_runs","SELECT id,type,status,generated_at FROM report_runs WHERE organization_id=? ORDER BY generated_at DESC"),
            "Integrations":("integrations","SELECT id,source,status,last_sync_at,last_error,object_count,health FROM integrations WHERE organization_id=? ORDER BY source"),
        }
        if module not in queries:return {"module":module,"items":[]}
        table,sql=queries[module];scope=organization_id if module in {"Automations","Reports","Integrations"} else workspace_id
        return {"module":module,"source_table":table,"items":[dict(row) for row in self.conn.execute(sql,(scope,)).fetchall()]}
