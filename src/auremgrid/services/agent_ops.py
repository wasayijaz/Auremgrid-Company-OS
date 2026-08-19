from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> datetime: return datetime.now(timezone.utc).replace(microsecond=0)


class AgentOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], company: Any, approvals: Any, client_ops: Any) -> None:
        self.conn,self.new_id,self.company,self.approvals,self.client_ops=conn,new_id,company,approvals,client_ops

    def seed_primary_agents(self, organization_id: str, owner_person_id: str) -> list[dict[str, Any]]:
        if self.company.org_membership(organization_id,owner_person_id) is None: raise AuthorizationError("organization membership required")
        definitions=(("Sol","advisor_reviewer","Review architecture and detect risk",[]),("Terra","builder","Implement deep product work",["domain.write","code.write"]),("Luna","executor","Execute operations and consistency work",["domain.write"]))
        agents=[]
        for name,role,description,writes in definitions:
            existing=self.conn.execute("SELECT * FROM agents WHERE organization_id=? AND name=?",(organization_id,name)).fetchone()
            if existing:
                agents.append(dict(existing));continue
            role_id=self.new_id("agentrole")
            self.conn.execute("INSERT INTO agent_roles VALUES (?,?,?,?,?,?)",(role_id,organization_id,role,description,json.dumps([]),json.dumps(writes)))
            item={"id":self.new_id("agent"),"organization_id":organization_id,"name":name,"role_id":role_id,"model":"unconfigured",
                "tools":json.dumps([]),"allowed_workspace_ids":json.dumps([]),"memory_access":"proposal_only","write_permissions":json.dumps(writes),
                "status":"idle","current_task_id":None,"created_at":_now().isoformat()}
            self.conn.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); agents.append(item)
        self.conn.commit(); return agents

    def configure_agent(self, organization_id: str, owner_person_id: str, agent_id: str, model: str,
        tools: list[str], allowed_workspace_ids: list[str], write_permissions: list[str]) -> dict[str, Any]:
        membership=self.company.org_membership(organization_id,owner_person_id)
        if membership is None or membership.role not in {"owner","admin"}: raise AuthorizationError("organization admin required")
        for workspace_id in allowed_workspace_ids:
            scope=self.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"]!=organization_id: raise ValidationError("agent workspace must belong to organization")
        self.conn.execute("UPDATE agents SET model=?,tools=?,allowed_workspace_ids=?,write_permissions=? WHERE organization_id=? AND id=?",
            (model,json.dumps(tools),json.dumps(allowed_workspace_ids),json.dumps(write_permissions),organization_id,agent_id)); self.conn.commit()
        row=self.conn.execute("SELECT * FROM agents WHERE organization_id=? AND id=?",(organization_id,agent_id)).fetchone()
        if row is None: raise NotFoundError("agent not found")
        return dict(row)

    def enqueue_task(self, organization_id: str, requested_by_person_id: str, agent_id: str, title: str,
        instructions: str, workspace_id: str | None = None, priority: int = 50) -> dict[str, Any]:
        if self.company.org_membership(organization_id,requested_by_person_id) is None: raise AuthorizationError("organization membership required")
        agent=self.conn.execute("SELECT * FROM agents WHERE organization_id=? AND id=?",(organization_id,agent_id)).fetchone()
        if agent is None: raise NotFoundError("agent not found")
        if workspace_id and workspace_id not in json.loads(agent["allowed_workspace_ids"]): raise AuthorizationError("agent cannot access workspace")
        now=_now().isoformat(); task={"id":self.new_id("agenttask"),"organization_id":organization_id,"workspace_id":workspace_id,
            "agent_id":agent_id,"title":title,"instructions":instructions,"priority":priority,"status":"queued","approval_request_id":None,
            "created_at":now,"started_at":None,"completed_at":None}
        self.conn.execute("INSERT INTO agent_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",tuple(task.values()))
        self.conn.execute("INSERT INTO agent_queue_items VALUES (?,?,?,?,?,?,?,?)",(self.new_id("queue"),organization_id,agent_id,task["id"],priority,"queued",now,None)); self.conn.commit(); return task

    def start_run(self, organization_id: str, agent_id: str, task_id: str) -> dict[str, Any]:
        task=self.conn.execute("SELECT * FROM agent_tasks WHERE organization_id=? AND agent_id=? AND id=?",(organization_id,agent_id,task_id)).fetchone()
        if task is None or task["status"]!="queued": raise ValidationError("queued agent task required")
        now=_now().isoformat(); run={"id":self.new_id("run"),"organization_id":organization_id,"workspace_id":task["workspace_id"],
            "agent_id":agent_id,"task_id":task_id,"status":"running","started_at":now,"completed_at":None,"runtime_ms":None,
            "input_tokens":0,"output_tokens":0,"cost":None,"error_id":None,"output_id":None}
        self.conn.execute("INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(run.values()))
        self.conn.execute("UPDATE agent_tasks SET status='running',started_at=? WHERE id=?",(now,task_id)); self.conn.execute("UPDATE agents SET status='running',current_task_id=? WHERE id=?",(task_id,agent_id)); self.conn.execute("UPDATE agent_queue_items SET status='claimed',claimed_at=? WHERE task_id=?",(now,task_id)); self.conn.commit(); return run

    def record_tool_call(self, organization_id: str, agent_id: str, run_id: str, tool_name: str,
        arguments: dict[str, Any], result_preview: str = "", error: str | None = None) -> dict[str, Any]:
        run=self._run(organization_id,agent_id,run_id); agent=self.conn.execute("SELECT tools FROM agents WHERE id=?",(agent_id,)).fetchone()
        if tool_name not in json.loads(agent[0]): raise AuthorizationError("tool is not allowed for agent")
        now=_now().isoformat(); item={"id":self.new_id("toolcall"),"run_id":run_id,"tool_name":tool_name,"arguments":json.dumps(arguments),
            "status":"failed" if error else "completed","started_at":now,"completed_at":now,"result_preview":result_preview[:500],"error":error}
        self.conn.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def complete_run(self, organization_id: str, agent_id: str, run_id: str, content: str,
        input_tokens: int = 0, output_tokens: int = 0, cost: float | None = None, source_refs: list[str] | None = None) -> dict[str, Any]:
        run=self._run(organization_id,agent_id,run_id); now=_now(); started=datetime.fromisoformat(run["started_at"]); runtime=int((now-started).total_seconds()*1000)
        output_id=self.new_id("output"); self.conn.execute("INSERT INTO run_outputs VALUES (?,?,?,?,?,?)",(output_id,run_id,"text",content,json.dumps(source_refs or []),now.isoformat()))
        self.conn.execute("UPDATE agent_runs SET status='completed',completed_at=?,runtime_ms=?,input_tokens=?,output_tokens=?,cost=?,output_id=? WHERE id=?",(now.isoformat(),runtime,input_tokens,output_tokens,cost,output_id,run_id))
        self.conn.execute("UPDATE agent_tasks SET status='completed',completed_at=? WHERE id=?",(now.isoformat(),run["task_id"])); self.conn.execute("UPDATE agents SET status='idle',current_task_id=NULL WHERE id=?",(agent_id,)); self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM agent_runs WHERE id=?",(run_id,)).fetchone())

    def fail_run(self, organization_id: str, agent_id: str, run_id: str, kind: str, message: str, detail: str = "", retryable: bool = False) -> dict[str, Any]:
        run=self._run(organization_id,agent_id,run_id); now=_now().isoformat(); error_id=self.new_id("runerror")
        self.conn.execute("INSERT INTO run_errors VALUES (?,?,?,?,?,?,?)",(error_id,run_id,kind,message,detail,int(retryable),now)); self.conn.execute("UPDATE agent_runs SET status='failed',completed_at=?,error_id=? WHERE id=?",(now,error_id,run_id)); self.conn.execute("UPDATE agent_tasks SET status='failed',completed_at=? WHERE id=?",(now,run["task_id"])); self.conn.execute("UPDATE agents SET status='error',current_task_id=NULL WHERE id=?",(agent_id,)); self.conn.commit(); return dict(self.conn.execute("SELECT * FROM agent_runs WHERE id=?",(run_id,)).fetchone())

    def command_center(self, organization_id: str, person_id: str) -> dict[str, Any]:
        if self.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
        agents=[dict(row) for row in self.conn.execute("SELECT * FROM agents WHERE organization_id=? ORDER BY name",(organization_id,)).fetchall()]
        runs=[dict(row) for row in self.conn.execute("SELECT * FROM agent_runs WHERE organization_id=? ORDER BY started_at DESC LIMIT 25",(organization_id,)).fetchall()]
        return {"agents":agents,"recent_runs":runs,"running":sum(r["status"]=="running" for r in runs),"failed":sum(r["status"]=="failed" for r in runs),"token_cost":sum((r["cost"] or 0) for r in runs)}

    def _run(self, organization_id: str, agent_id: str, run_id: str) -> Any:
        row=self.conn.execute("SELECT * FROM agent_runs WHERE organization_id=? AND agent_id=? AND id=?",(organization_id,agent_id,run_id)).fetchone()
        if row is None or row["status"]!="running": raise ValidationError("running agent run required")
        return row

    def create_automation(self, organization_id: str, person_id: str, name: str, trigger_type: str,
        conditions: list[dict[str, Any]], actions: list[dict[str, Any]], approval_policy: str = "human") -> dict[str, Any]:
        membership=self.company.org_membership(organization_id,person_id)
        if membership is None: raise AuthorizationError("organization membership required")
        if approval_policy not in {"auto","human","admin_only"}: raise ValidationError("invalid approval policy")
        automation={"id":self.new_id("automation"),"organization_id":organization_id,"name":name,"description":"","status":"training",
            "approval_policy":approval_policy,"created_by_person_id":person_id,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO automations VALUES (?,?,?,?,?,?,?,?)",tuple(automation.values())); self.conn.execute("INSERT INTO automation_triggers VALUES (?,?,?,?)",(self.new_id("trigger"),automation["id"],trigger_type,"{}"))
        for sequence,condition in enumerate(conditions): self.conn.execute("INSERT INTO automation_conditions VALUES (?,?,?,?,?,?)",(self.new_id("condition"),automation["id"],condition["field"],condition["operator"],json.dumps(condition["value"]),sequence))
        for sequence,action in enumerate(actions): self.conn.execute("INSERT INTO automation_actions VALUES (?,?,?,?,?,?)",(self.new_id("action"),automation["id"],action["type"],json.dumps(action.get("config",{})),sequence,int(action.get("one_way",False))))
        self.conn.commit(); return automation

    def trigger_automations(self, organization_id: str, trigger_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows=self.conn.execute("""SELECT a.* FROM automations a JOIN automation_triggers t ON t.automation_id=a.id
            WHERE a.organization_id=? AND a.status IN ('training','active') AND t.type=?""",(organization_id,trigger_type)).fetchall(); results=[]
        for automation in rows:
            conditions=self.conn.execute("SELECT * FROM automation_conditions WHERE automation_id=? ORDER BY sequence",(automation["id"],)).fetchall()
            if not all(self._condition(payload,c) for c in conditions): continue
            actions=self.conn.execute("SELECT * FROM automation_actions WHERE automation_id=? ORDER BY sequence",(automation["id"],)).fetchall()
            needs_approval=automation["status"]=="training" or automation["approval_policy"]!="auto" or any(a["one_way"] for a in actions)
            run_id=self.new_id("automationrun"); now=_now().isoformat(); approval_id=None; status="waiting_approval" if needs_approval else "completed"
            if needs_approval:
                approval=self.approvals.request_approval(organization_id,"automation",automation["id"],"automation run","automation.execute",payload,"Training mode or gated action",approver_person_id=automation["created_by_person_id"])
                approval_id=approval["id"]
            output=self._execute_actions(organization_id,automation,actions,payload) if status=="completed" else {}
            self.conn.execute("INSERT INTO automation_runs VALUES (?,?,?,?,?,?,?,?,?)",(run_id,automation["id"],trigger_type,json.dumps(payload),status,now,now if status=="completed" else None,approval_id,json.dumps(output))); results.append({"run_id":run_id,"status":status,"approval_request_id":approval_id,"output":output})
        self.conn.commit(); return results

    def activate_automation(self, organization_id: str, person_id: str, automation_id: str) -> dict[str,Any]:
        membership=self.company.org_membership(organization_id,person_id)
        if membership is None or membership.role not in {"owner","admin"}: raise AuthorizationError("organization admin required")
        automation=self.conn.execute("SELECT * FROM automations WHERE organization_id=? AND id=?",(organization_id,automation_id)).fetchone()
        if automation is None:raise NotFoundError("automation not found")
        approved=self.conn.execute("""SELECT 1 FROM automation_runs ar JOIN approval_requests ap ON ap.id=ar.approval_request_id
            WHERE ar.automation_id=? AND ap.status='approved' LIMIT 1""",(automation_id,)).fetchone()
        if approved is None:raise ValidationError("automation needs an approved training run before activation")
        self.conn.execute("UPDATE automations SET status='active' WHERE id=?",(automation_id,));self.conn.commit();return dict(self.conn.execute("SELECT * FROM automations WHERE id=?",(automation_id,)).fetchone())

    def execute_approved_automation_run(self, organization_id: str, person_id: str, run_id: str) -> dict[str,Any]:
        run=self.conn.execute("""SELECT ar.*,a.created_by_person_id,a.organization_id FROM automation_runs ar JOIN automations a ON a.id=ar.automation_id
            WHERE a.organization_id=? AND ar.id=?""",(organization_id,run_id)).fetchone()
        if run is None:raise NotFoundError("automation run not found")
        approval=self.conn.execute("SELECT status FROM approval_requests WHERE id=?",(run["approval_request_id"],)).fetchone()
        if run["status"]!="waiting_approval" or approval is None or approval["status"]!="approved":raise AuthorizationError("approved automation run required")
        actions=self.conn.execute("SELECT * FROM automation_actions WHERE automation_id=? ORDER BY sequence",(run["automation_id"],)).fetchall();payload=json.loads(run["trigger_payload"])
        output=self._execute_actions(organization_id,run,actions,payload);now=_now().isoformat()
        self.conn.execute("UPDATE automation_runs SET status='completed',completed_at=?,output=? WHERE id=?",(now,json.dumps(output),run_id));self.conn.commit();return dict(self.conn.execute("SELECT * FROM automation_runs WHERE id=?",(run_id,)).fetchone())

    def _execute_actions(self, organization_id: str, automation: Any, actions: list[Any], payload: dict[str,Any]) -> list[dict[str,Any]]:
        output=[];person_id=automation["created_by_person_id"]
        for action in actions:
            config=json.loads(action["config"]);workspace_id=config.get("workspace_id") or payload.get("workspace_id")
            if action["type"]=="risk.create":
                if not workspace_id:raise ValidationError("risk automation requires workspace_id")
                risk=self.client_ops.create_risk(organization_id,workspace_id,person_id,config.get("type","relationship"),config.get("severity","medium"),float(config.get("probability",.5)),config.get("impact",payload.get("reason","Automation signal")),json.dumps(payload),config.get("recommended_action","Account lead review"));output.append({"type":"risk","id":risk.id})
            elif action["type"]=="notification.create":
                recipient=config.get("recipient_person_id") or person_id
                notice=self.approvals.create_notification(organization_id,recipient,config.get("reason",payload.get("reason","Automation signal")),"automation",automation["id"],workspace_id,float(config.get("severity",.5)),float(config.get("urgency",.5)));output.append({"type":"notification","id":notice["id"]})
            else:raise ValidationError(f"unsupported automation action: {action['type']}")
        return output

    @staticmethod
    def _condition(payload: dict[str, Any], row: Any) -> bool:
        actual=payload.get(row["field"]); expected=json.loads(row["value"]); op=row["operator"]
        if op == "eq": return actual == expected
        if op == "gt": return actual is not None and actual > expected
        if op == "gte": return actual is not None and actual >= expected
        if op == "lt": return actual is not None and actual < expected
        if op == "contains": return actual is not None and expected in actual
        return False

    def generate_report(self, organization_id: str, person_id: str, type: str, workspace_id: str | None = None) -> dict[str, Any]:
        if self.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
        allowed={"daily_owner_brief","weekly_agency_brief","client_weekly_report","campaign_report","workload_report","capacity_report","revenue_report","churn_risk_report","creative_performance_report"}
        if type not in allowed: raise ValidationError("unsupported report type")
        payload:dict[str,Any]={"type":type}; citations=[]
        if type=="churn_risk_report":
            sql="SELECT id,workspace_id,severity,evidence,recommended_action FROM risks WHERE organization_id=? AND type='churn' AND status='open'"; rows=self.conn.execute(sql,(organization_id,)).fetchall(); payload["risks"]=[dict(r) for r in rows]; citations=[{"table":"risks","id":r["id"]} for r in rows]
        elif type=="capacity_report":
            rows=self.conn.execute("SELECT * FROM capacity_snapshots WHERE organization_id=? ORDER BY calculated_at DESC",(organization_id,)).fetchall(); payload["capacity"]=[dict(r) for r in rows]; citations=[{"table":"capacity_snapshots","id":r["id"]} for r in rows]
        elif type=="revenue_report": payload=self.approvals.finance_status(organization_id,person_id,workspace_id); citations=[{"table":"finance_connections","organization_id":organization_id}]
        elif type in {"daily_owner_brief","weekly_agency_brief"}:
            payload.update({"clients":self.conn.execute("SELECT COUNT(*) FROM workspace_organization WHERE organization_id=? AND kind='client'",(organization_id,)).fetchone()[0],
                "open_work":self.conn.execute("SELECT COUNT(*) FROM work_items wi JOIN workspace_organization wo ON wo.workspace_id=wi.workspace_id WHERE wo.organization_id=? AND wi.status!='shipped'",(organization_id,)).fetchone()[0],
                "open_risks":self.conn.execute("SELECT COUNT(*) FROM risks WHERE organization_id=? AND status='open'",(organization_id,)).fetchone()[0],
                "open_reviews":self.conn.execute("SELECT COUNT(*) FROM reviews WHERE organization_id=? AND status='open'",(organization_id,)).fetchone()[0]})
            citations=[{"table":name,"organization_id":organization_id} for name in ("workspace_organization","work_items","risks","reviews")]
        elif type=="client_weekly_report":
            if not workspace_id: raise ValidationError("client weekly report requires workspace_id")
            workspace=self.company.workspace_scope(workspace_id)
            if workspace is None or workspace["organization_id"]!=organization_id: raise AuthorizationError("workspace not available")
            work=[dict(r) for r in self.conn.execute("SELECT id,title,status,updated_at FROM work_items WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()]
            risks=[dict(r) for r in self.conn.execute("SELECT id,type,severity,status,evidence FROM risks WHERE workspace_id=?",(workspace_id,)).fetchall()]
            decisions=[dict(r) for r in self.conn.execute("SELECT id,statement,rationale,effective_from FROM decisions WHERE workspace_id=?",(workspace_id,)).fetchall()]
            payload.update({"work":work,"risks":risks,"decisions":decisions});citations=[{"table":"work_items","id":r["id"]} for r in work]+[{"table":"risks","id":r["id"]} for r in risks]+[{"table":"decisions","id":r["id"]} for r in decisions]
        elif type=="campaign_report":
            sql="""SELECT c.id,c.name,c.platform,c.status,m.id metric_id,m.captured_at,m.spend,m.revenue,m.leads,m.ctr,m.cvr,m.roas,m.source
                FROM campaigns c LEFT JOIN campaign_metric_snapshots m ON m.id=(SELECT id FROM campaign_metric_snapshots WHERE campaign_id=c.id ORDER BY captured_at DESC LIMIT 1)
                WHERE c.organization_id=?"""+(" AND c.workspace_id=?" if workspace_id else "")
            values=[organization_id]+([workspace_id] if workspace_id else []);rows=self.conn.execute(sql,values).fetchall();payload["campaigns"]=[dict(r) for r in rows];citations=[{"table":"campaigns","id":r["id"],"metric_id":r["metric_id"]} for r in rows]
        elif type=="workload_report":
            rows=self.conn.execute("""SELECT p.id,p.name,COUNT(w.id) open_work,COALESCE(SUM(w.estimate_hours),0) estimated_hours
                FROM people p LEFT JOIN work_items w ON w.assignee_person_id=p.id AND w.status!='shipped'
                WHERE p.organization_id=? GROUP BY p.id,p.name ORDER BY estimated_hours DESC""",(organization_id,)).fetchall();payload["people"]=[dict(r) for r in rows];citations=[{"table":"people","id":r["id"]} for r in rows]
        elif type=="creative_performance_report":
            rows=self.conn.execute("""SELECT ca.id,ca.title,ca.approval_state,cp.captured_at,cp.ctr,cp.cvr,cp.roas,cp.source
                FROM creative_assets ca LEFT JOIN creative_performance cp ON cp.id=(SELECT id FROM creative_performance WHERE asset_id=ca.id ORDER BY captured_at DESC LIMIT 1)
                WHERE ca.organization_id=?"""+(" AND ca.workspace_id=?" if workspace_id else ""),[organization_id]+([workspace_id] if workspace_id else [])).fetchall();payload["creative"]=[dict(r) for r in rows];citations=[{"table":"creative_assets","id":r["id"]} for r in rows]
        if not citations:
            citations=[{"table":"canonical_ledger","organization_id":organization_id,"workspace_id":workspace_id,"result":"no matching records"}]
        item={"id":self.new_id("report"),"organization_id":organization_id,"workspace_id":workspace_id,"type":type,
            "requested_by_person_id":person_id,"status":"completed","payload":json.dumps(payload),"citations":json.dumps(citations),"generated_at":_now().isoformat()}
        self.conn.execute("INSERT INTO report_runs VALUES (?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return {**item,"payload":payload,"citations":citations}
