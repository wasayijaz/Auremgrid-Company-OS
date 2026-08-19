from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from auremgrid.api.mcp import McpToolRouter
from auremgrid.domain.errors import AuremgridError, AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from pathlib import Path


class CompanyOSRequestHandler(BaseHTTPRequestHandler):
    os: CompanyOS
    router: McpToolRouter

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/health":
                self._json(200, {"ok": True, "schema_version": self.os.store.schema_version})
                return
            if parsed.path in {"/", "/dashboard"}:
                self._html(200, _dashboard_html())
                return
            if parsed.path == "/search":
                bundle = self.os.search(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    _need(params, "query"),
                    as_of=_optional_dt(params.get("as_of")),
                    limit=_int(params.get("limit", "8"), "limit"),
                )
                self._json(200, bundle.to_dict())
                return
            if parsed.path == "/entity":
                self._json(
                    200,
                    self.os.entity(_need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "name")),
                )
                return
            if parsed.path == "/history":
                self._json(
                    200,
                    self.os.history(
                        _need(params, "workspace_id"),
                        _need(params, "actor_id"),
                        _need(params, "subject"),
                        predicate=params.get("predicate"),
                    ),
                )
                return
            if parsed.path == "/neighbors":
                self._json(
                    200,
                    self.os.neighbors(_need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "entity")),
                )
                return
            if parsed.path == "/sources":
                self._json(200, self.os.sources(_need(params, "workspace_id"), _need(params, "actor_id")))
                return
            if parsed.path == "/recent":
                self._json(
                    200,
                    self.os.recent(
                        _need(params, "workspace_id"),
                        _need(params, "actor_id"),
                        limit=int(params.get("limit", "5")),
                    ),
                )
                return
            if parsed.path == "/brief":
                self._json(
                    200,
                    self.os.account_brief(
                        _need(params, "workspace_id"),
                        _need(params, "actor_id"),
                        query=params.get("query"),
                    ).to_dict(),
                )
                return
            if parsed.path == "/work":
                items = self.os.list_work(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    open_only=params.get("open_only", "1") != "0",
                )
                self._json(200, {"work": [item.to_dict() for item in items]})
                return
            if parsed.path == "/organizations/workspaces":
                items = self.os.company.list_workspaces(_need(params, "organization_id"))
                self._json(200, {"workspaces": items})
                return
            if parsed.path == "/people":
                organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id")
                if self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
                items = self.os.company.list_people(organization_id)
                self._json(200, {"people": [item.to_dict() for item in items]})
                return
            if parsed.path == "/projects":
                items = self.os.list_projects(
                    _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                )
                self._json(200, {"projects": [item.to_dict() for item in items]})
                return
            if parsed.path == "/projects/get":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                self.os._require_person_access(organization_id,workspace_id,person_id);item=self.os.company.get_project(workspace_id,_need(params,"project_id"))
                if item is None:raise NotFoundError("project not found")
                self._json(200,item.to_dict());return
            if parsed.path == "/deliverables":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                self.os._require_person_access(organization_id,workspace_id,person_id);items=self.os.company.list_deliverables(workspace_id,params.get("project_id"))
                self._json(200,{"deliverables":[item.to_dict() for item in items]});return
            if parsed.path == "/reviews":
                organization_id, workspace_id, person_id = (
                    _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                )
                self.os._require_person_access(organization_id, workspace_id, person_id)
                items = self.os.company.list_reviews(workspace_id, params.get("status"))
                self._json(200, {"reviews": [item.to_dict() for item in items]})
                return
            if parsed.path == "/decisions":
                organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
                workspace_id = params.get("workspace_id")
                if workspace_id:
                    self.os._require_person_access(organization_id, workspace_id, person_id)
                elif self.os.company.org_membership(organization_id, person_id) is None:
                    raise AuthorizationError("person is not an organization member")
                items = self.os.company.list_decisions(organization_id, workspace_id)
                self._json(200, {"decisions": [item.to_dict() for item in items]})
                return
            if parsed.path == "/dashboard/data":
                self._json(200, self.os.dashboard.command(_need(params,"organization_id"),_need(params,"person_id")))
                return
            if parsed.path == "/dashboard/client":
                self._json(200, self.os.dashboard.client_hq(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")))
                return
            if parsed.path == "/dashboard/module":
                self._json(200,self.os.dashboard.module(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id"),_need(params,"module")));return
            if parsed.path in {"/signals","/risks","/opportunities","/meetings","/campaigns","/creative","/content"}:
                organization_id, workspace_id, person_id = _need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                self.os._require_person_access(organization_id,workspace_id,person_id)
                if parsed.path == "/signals": result=self.os.client_ops.list_signals(organization_id,workspace_id,person_id,params.get("status"))
                elif parsed.path == "/risks": result=self.os.client_ops.list_risks(organization_id,workspace_id,person_id,params.get("open_only","1")!="0")
                elif parsed.path == "/opportunities": result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM opportunities WHERE workspace_id=? ORDER BY created_at DESC",(workspace_id,)).fetchall()]
                elif parsed.path == "/meetings": result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC",(workspace_id,)).fetchall()]
                elif parsed.path == "/campaigns": result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()]
                elif parsed.path == "/creative": result=self.os.agency_ops.search_creative(organization_id,workspace_id,person_id,params.get("query",""),params.get("approval_state"),params.get("campaign_id"))
                else: result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()]
                self._json(200,{parsed.path[1:]:result}); return
            if parsed.path == "/finance":
                self._json(200,self.os.agency_ops.finance_status(_need(params,"organization_id"),_need(params,"person_id"),params.get("workspace_id"))); return
            if parsed.path == "/notifications":
                self._json(200,{"notifications":self.os.agency_ops.attention(_need(params,"organization_id"),_need(params,"person_id"),_int(params.get("limit",20),"limit"))}); return
            if parsed.path == "/agents":
                self._json(200,self.os.agent_ops.command_center(_need(params,"organization_id"),_need(params,"person_id"))); return
            if parsed.path in {"/approvals","/automations","/reports"}:
                organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id")
                if self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
                table={"/approvals":"approval_requests","/automations":"automations","/reports":"report_runs"}[parsed.path]
                rows=self.os.store.conn.execute(f"SELECT * FROM {table} WHERE organization_id=? ORDER BY rowid DESC",(organization_id,)).fetchall()
                self._json(200,{parsed.path[1:]:[dict(r) for r in rows]});return
            if parsed.path == "/integrations":
                organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id")
                if self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
                self._json(200,{"integrations":[dict(r) for r in self.os.store.conn.execute("SELECT * FROM integrations WHERE organization_id=? ORDER BY source",(organization_id,)).fetchall()]}); return
            if parsed.path == "/memory-proposals":
                organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id"); workspace_id=params.get("workspace_id")
                if workspace_id:self.os._require_person_access(organization_id,workspace_id,person_id)
                rows=self.os.store.conn.execute("SELECT * FROM memory_proposals WHERE organization_id=? AND (? IS NULL OR workspace_id=?) ORDER BY created_at DESC",(organization_id,workspace_id,workspace_id)).fetchall()
                self._json(200,{"proposals":[dict(r) for r in rows]}); return
            if parsed.path == "/knowledge-health":
                self._json(200,self.os.brain_ops.knowledge_health(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id"))); return
            if parsed.path == "/work/detail":
                self._json(200,self.os.work_ops.detail(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id"),_need(params,"work_item_id"))); return
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/tools/call":
                result = self.router.call(str(payload.get("name", "")), payload.get("arguments") or {})
                status = 400 if "error" in result else 200
                self._json(status, result)
                return
            if parsed.path == "/search":
                bundle = self.os.search(
                    _need(payload, "workspace_id"),
                    _need(payload, "actor_id"),
                    _need(payload, "query"),
                    as_of=_optional_dt(payload.get("as_of")),
                    limit=_int(payload.get("limit", 8), "limit"),
                )
                self._json(200, bundle.to_dict())
                return
            if parsed.path == "/remember":
                memory = self.os.remember(
                    _need(payload, "workspace_id"),
                    _need(payload, "actor_id"),
                    _need(payload, "content"),
                    kind=str(payload.get("kind", "preference")),
                )
                self._json(200, memory.to_dict())
                return
            if parsed.path == "/organizations":
                item = self.os.create_organization(_need(payload, "name"), _optional_str(payload.get("id")))
                self._json(201, item.to_dict())
                return
            if parsed.path == "/workspaces":
                item = self.os.create_organization_workspace(
                    _need(payload, "organization_id"), _need(payload, "name"),
                    str(payload.get("kind", "client")), _optional_str(payload.get("id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/people":
                item = self.os.create_person(
                    _need(payload, "organization_id"), _need(payload, "name"),
                    _optional_str(payload.get("email")), _optional_str(payload.get("title")),
                    _optional_str(payload.get("department")), _optional_str(payload.get("manager_id")),
                    str(payload.get("role", "member")), _optional_str(payload.get("id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/workspace-memberships":
                item = self.os.add_person_to_workspace(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), str(payload.get("role", "operator")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/projects":
                item = self.os.create_project(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "name"), str(payload.get("description", "")),
                    str(payload.get("priority", "normal")), _optional_str(payload.get("due_date")),
                    float(payload["budget"]) if payload.get("budget") is not None else None,
                    [str(value) for value in payload.get("tags", [])],
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/deliverables":
                item = self.os.create_deliverable(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "project_id"),
                    _need(payload, "title"), _need(payload, "type"), _optional_str(payload.get("work_item_id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/reviews":
                item = self.os.open_review(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "deliverable_id"),
                    str(payload.get("kind", "internal")), _optional_str(payload.get("reviewer_person_id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/reviews/decide":
                item = self.os.decide_review(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "review_id"), _need(payload, "decision"),
                )
                self._json(200, item.to_dict())
                return
            if parsed.path == "/decisions":
                item = self.os.create_decision(
                    _need(payload, "organization_id"), _need(payload, "person_id"),
                    _need(payload, "statement"), _need(payload, "rationale"),
                    _optional_str(payload.get("workspace_id")), _optional_str(payload.get("project_id")),
                    _optional_str(payload.get("source_id")), str(payload.get("evidence", "")),
                    [str(value) for value in payload.get("tags", [])],
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/signals":
                item=self.os.client_ops.create_signal(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"source_type"),_need(payload,"evidence"),_optional_str(payload.get("source_id")),float(payload.get("confidence",1)))
                self._json(201,item.to_dict()); return
            if parsed.path == "/signals/route":
                self._json(200,self.os.client_ops.route_signal(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"signal_id"),_need(payload,"destination"))); return
            if parsed.path == "/risks":
                item=self.os.client_ops.create_risk(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"severity"),float(payload.get("probability",0.5)),_need(payload,"impact"),_need(payload,"evidence"),_need(payload,"recommended_action"),_optional_str(payload.get("project_id")))
                self._json(201,item.to_dict()); return
            if parsed.path == "/opportunities":
                item=self.os.client_ops.create_opportunity(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"reason"),_need(payload,"evidence"),_need(payload,"recommendation"),float(payload["estimated_value"]) if payload.get("estimated_value") is not None else None)
                self._json(201,item.to_dict()); return
            if parsed.path == "/health/calculate":
                item=self.os.client_ops.calculate_health(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id")); self._json(201,item.to_dict()); return
            if parsed.path == "/campaigns":
                item=self.os.agency_ops.create_campaign(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"name"),_need(payload,"objective"),_need(payload,"platform"),_optional_str(payload.get("project_id")),float(payload["budget"]) if payload.get("budget") is not None else None,str(payload.get("currency","USD")),_optional_str(payload.get("start_date")),_optional_str(payload.get("end_date")))
                self._json(201,item); return
            if parsed.path == "/campaigns/metrics":
                item=self.os.agency_ops.record_campaign_metrics(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"campaign_id"),_need(payload,"source"),*[_optional_float(payload.get(k)) for k in ("spend","revenue","leads","impressions","clicks")]); self._json(201,item); return
            if parsed.path == "/creative":
                item=self.os.agency_ops.create_creative(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"title"),_need(payload,"format"),_optional_str(payload.get("project_id")),_optional_str(payload.get("campaign_id")),_optional_str(payload.get("platform")),_optional_str(payload.get("dimensions")),[str(x) for x in payload.get("style_tags",[])],_optional_str(payload.get("source_url"))); self._json(201,item); return
            if parsed.path == "/content":
                item=self.os.agency_ops.create_content(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"title"),_need(payload,"objective"),_need(payload,"audience"),str(payload.get("hook","")),str(payload.get("copy","")),_optional_str(payload.get("project_id")),_optional_str(payload.get("channel_id")),[str(x) for x in payload.get("references",[])],str(payload.get("brain_context",""))); self._json(201,item); return
            if parsed.path == "/content/advance":
                self._json(200,self.os.agency_ops.advance_content(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"content_id"),_need(payload,"to_stage"))); return
            if parsed.path == "/approvals":
                item=self.os.agency_ops.request_approval(_need(payload,"organization_id"),_need(payload,"requested_by_type"),_need(payload,"requested_by_id"),_need(payload,"requested_for"),_need(payload,"action_type"),payload.get("payload") or {},_need(payload,"reason"),str(payload.get("policy","human")),_optional_str(payload.get("workspace_id")),_optional_str(payload.get("approver_person_id"))); self._json(201,item); return
            if parsed.path == "/approvals/decide":
                self._json(200,self.os.agency_ops.decide_approval(_need(payload,"organization_id"),_need(payload,"approver_person_id"),_need(payload,"approval_id"),_bool(payload.get("approved"),"approved"),str(payload.get("comments","")))); return
            if parsed.path == "/integrations":
                self._json(200,self.os.agent_ops.upsert_integration(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"source"),payload.get("workspace_mappings") or {},[str(x) for x in payload.get("permissions",[])],str(payload.get("status","not_connected")))); return
            if parsed.path == "/reports/generate":
                self._json(201,self.os.agent_ops.generate_report(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"type"),_optional_str(payload.get("workspace_id")))); return
            if parsed.path == "/agents/seed":
                self._json(201,{"agents":self.os.agent_ops.seed_primary_agents(_need(payload,"organization_id"),_need(payload,"person_id"))});return
            if parsed.path == "/agents/tasks":
                self._json(201,self.os.agent_ops.enqueue_task(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"agent_id"),_need(payload,"title"),_need(payload,"instructions"),_optional_str(payload.get("workspace_id")),int(payload.get("priority",50))));return
            if parsed.path == "/agents/runs/start":
                self._json(201,self.os.agent_ops.start_run(_need(payload,"organization_id"),_need(payload,"agent_id"),_need(payload,"task_id")));return
            if parsed.path == "/agents/runs/complete":
                self._json(200,self.os.agent_ops.complete_run(_need(payload,"organization_id"),_need(payload,"agent_id"),_need(payload,"run_id"),str(payload.get("content","")),int(payload.get("input_tokens",0)),int(payload.get("output_tokens",0)),_optional_float(payload.get("cost")),[str(x) for x in payload.get("source_refs",[])]));return
            if parsed.path == "/automations":
                self._json(201,self.os.agent_ops.create_automation(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"name"),_need(payload,"trigger_type"),payload.get("conditions") or [],payload.get("actions") or [],str(payload.get("approval_policy","human"))));return
            if parsed.path == "/automations/trigger":
                self._json(200,{"runs":self.os.agent_ops.trigger_automations(_need(payload,"organization_id"),_need(payload,"trigger_type"),payload.get("payload") or {})});return
            if parsed.path == "/automations/execute-approved":
                self._json(200,self.os.agent_ops.execute_approved_automation_run(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"run_id")));return
            if parsed.path == "/automations/activate":
                self._json(200,self.os.agent_ops.activate_automation(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"automation_id")));return
            if parsed.path == "/integrations/sync/start":
                self._json(201,self.os.agent_ops.start_sync(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"integration_id")));return
            if parsed.path == "/integrations/sync/complete":
                self._json(200,self.os.agent_ops.complete_sync(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"sync_run_id"),int(payload.get("object_count",0)),_optional_str(payload.get("cursor_after")),_optional_str(payload.get("error"))));return
            if parsed.path == "/memory-proposals":
                self._json(201,self.os.brain_ops.create_proposal(_need(payload,"organization_id"),_optional_str(payload.get("workspace_id")),_need(payload,"proposer_type"),_need(payload,"proposer_id"),_need(payload,"kind"),_need(payload,"content"),payload.get("payload") or {},_need(payload,"evidence"),float(payload.get("confidence",0.5)),_optional_str(payload.get("source_id")))); return
            if parsed.path == "/memory-proposals/review":
                self._json(200,self.os.brain_ops.review_proposal(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"proposal_id"),_need(payload,"action"),payload.get("edited_payload"))); return
            if parsed.path == "/initiatives":
                self._json(201,self.os.create_initiative(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"project_id"),_need(payload,"name"),str(payload.get("description","")))); return
            if parsed.path == "/deliverables/version":
                item=self.os.add_deliverable_version(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"deliverable_id"),str(payload.get("notes","")),_optional_str(payload.get("file_url")));self._json(201,item.to_dict());return
            if parsed.path == "/reviews/comment":
                item=self.os.add_review_comment(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"review_id"),_need(payload,"body"),_optional_float(payload.get("timestamp_seconds")));self._json(201,item.to_dict());return
            if parsed.path == "/work/items":
                item=self.os.work_ops.create(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"title"),_need(payload,"request"),_need(payload,"requested_by"),_optional_str(payload.get("project_id")),_optional_str(payload.get("campaign_id")),_optional_str(payload.get("parent_id")),str(payload.get("priority","normal")),[str(x) for x in payload.get("tags",[])],_optional_float(payload.get("estimate_hours")),_optional_str(payload.get("deadline")),str(payload.get("brief","")),str(payload.get("brain_context","")),_optional_float(payload.get("financial_value")));self._json(201,item.to_dict());return
            if parsed.path == "/work/items/update":
                item=self.os.work_ops.update(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),payload.get("changes") or {});self._json(200,item.to_dict());return
            if parsed.path == "/work/dependencies":
                self._json(201,self.os.work_ops.add_dependency(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"depends_on_id"),str(payload.get("kind","blocks"))));return
            if parsed.path == "/work/comments":
                self._json(201,self.os.work_ops.add_comment(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"body")));return
            if parsed.path == "/work/time":
                self._json(201,self.os.work_ops.log_time(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_required_dt(payload.get("started_at"),"started_at"),_required_dt(payload.get("ended_at"),"ended_at"),str(payload.get("notes","")),bool(payload.get("billable",True))));return
            work_action = {
                "/work/capture": "capture_work",
                "/work/capture_work": "capture_work",
                "/work/assign": "assign_work",
                "/work/assign_work": "assign_work",
                "/work/start": "start_work",
                "/work/start_work": "start_work",
                "/work/dod": "mark_dod",
                "/work/mark-dod": "mark_dod",
                "/work/mark_dod": "mark_dod",
                "/work/submit-review": "submit_review",
                "/work/submit_review": "submit_review",
                "/work/close-review": "close_review",
                "/work/close_review": "close_review",
                "/work/ship": "ship_work",
                "/work/ship_work": "ship_work",
            }.get(parsed.path)
            if work_action:
                self._json(200, self._call_work_action(work_action, payload))
                return
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def _call_work_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = _need(payload, "workspace_id")
        actor_id = _need(payload, "actor_id")
        if action == "capture_work":
            item = self.os.capture_work(
                workspace_id,
                actor_id,
                _need(payload, "title"),
                _need(payload, "request"),
                _need(payload, "requested_by"),
                needed_by=_optional_str(payload.get("needed_by")),
                playbook_id=_optional_str(payload.get("playbook_id")),
                decision_maker=_optional_str(payload.get("decision_maker")),
            )
        elif action == "assign_work":
            item = self.os.assign_work(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                _need(payload, "assignee_id"),
                decision_maker=_optional_str(payload.get("decision_maker")),
            )
        elif action == "start_work":
            item = self.os.start_work(workspace_id, actor_id, _need(payload, "work_item_id"))
        elif action == "mark_dod":
            checks = payload.get("checks")
            if not isinstance(checks, dict):
                raise ValidationError("checks must be an object")
            item = self.os.mark_dod(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                {str(key): _bool(value, f"checks.{key}") for key, value in checks.items()},
            )
        elif action == "submit_review":
            item = self.os.submit_review(workspace_id, actor_id, _need(payload, "work_item_id"))
        elif action == "close_review":
            item = self.os.close_review(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                _bool(payload.get("approved"), "approved"),
                note=str(payload.get("note", "")),
            )
        elif action == "ship_work":
            item = self.os.ship_work(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                note=str(payload.get("note", "")),
            )
        else:
            raise ValidationError(f"unknown work action: {action}")
        return item.to_dict()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("request body must be a JSON object")
        return payload

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ValidationError):
            self._json(400, {"error": "validation_error", "message": str(exc)})
            return
        if isinstance(exc, AuthorizationError):
            self._json(403, {"error": "authorization_error", "message": str(exc)})
            return
        if isinstance(exc, NotFoundError):
            self._json(404, {"error": "not_found", "message": str(exc)})
            return
        if isinstance(exc, AuremgridError):
            self._json(400, {"error": "auremgrid_error", "message": str(exc)})
            return
        self._json(500, {"error": "internal_error", "message": str(exc)})


def _need(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not value:
        raise ValidationError(f"{key} is required")
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be an integer") from exc

def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValidationError(f"{key} must be a boolean")


def _optional_dt(value: Any) -> Any:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError("as_of must be an ISO datetime") from exc

def _required_dt(value: Any, key: str) -> Any:
    if not value: raise ValidationError(f"{key} is required")
    result=_optional_dt(value)
    return result


def serve(os: CompanyOS, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (CompanyOSRequestHandler,),
        {"os": os, "router": McpToolRouter(os)},
    )
    return ThreadingHTTPServer((host, port), handler)


def _dashboard_html() -> str:
    path = Path(__file__).with_name("dashboard.html")
    return path.read_text(encoding="utf-8")
