from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


FIXTURES=Path(__file__).resolve().parents[1]/"fixtures"


class ExpandedApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os=CompanyOS(":memory:"); self.os.seed_demo(FIXTURES)
        self.token,self.identity=issue_identity(self.os,"org_demo","person_demo_owner","ws_alpha","act_alpha_admin")
        self.server=serve(self.os,"127.0.0.1",0); self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.host,self.port=self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown();self.server.server_close();self.os.close()

    def get(self,path:str,token:str|None=None)->tuple[int,dict]:
        c=HTTPConnection(self.host,self.port,timeout=5);c.request("GET",path,headers={"Authorization":f"Bearer {token or self.token}"});r=c.getresponse();body=json.loads(r.read());c.close();return r.status,body

    def post(self,path:str,payload:dict)->tuple[int,dict]:
        c=HTTPConnection(self.host,self.port,timeout=5);c.request("POST",path,json.dumps(payload),{"Content-Type":"application/json","Authorization":f"Bearer {self.token}"});r=c.getresponse();body=json.loads(r.read());c.close();return r.status,body

    def test_dashboard_payload_is_real_and_finance_is_not_connected(self) -> None:
        status,body=self.get("/dashboard/data?organization_id=org_demo&person_id=person_demo_owner")
        self.assertEqual(status,200);self.assertEqual(body["metrics"]["active_clients"],2)
        self.assertEqual(body["metrics"]["finance_status"],"not_connected");self.assertIsNone(body["metrics"]["mrr"])
        self.assertGreaterEqual(len(body["attention"]),1);self.assertLessEqual(len(body["attention"]),3)
        self.assertTrue(all({"client","reason","severity","evidence","owner","next_action"} <= set(item) for item in body["attention"]))

    def test_signal_and_health_rest_endpoints(self) -> None:
        common={"organization_id":"org_demo","workspace_id":"ws_alpha","person_id":"person_demo_owner"}
        status,signal=self.post("/signals",{**common,"type":"risk","source_type":"manual","evidence":"Client is concerned"})
        self.assertEqual(status,201)
        status,routed=self.post("/signals/route",{**common,"signal_id":signal["id"],"destination":"risk"})
        self.assertEqual(status,200);self.assertIn("risk_id",routed)
        status,health=self.post("/health/calculate",common)
        self.assertEqual(status,201);self.assertIn("open risks",health["explanation"])
        status,explained=self.get("/health/explain?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner")
        self.assertEqual(status,200);self.assertIn("components",explained)

    def test_client_lifecycle_and_scope_read_models_are_available_over_http(self) -> None:
        common={"organization_id":"org_demo","workspace_id":"ws_alpha","person_id":"person_demo_owner"}
        status,risk=self.post("/risks",{**common,"type":"delivery","severity":"high","impact":"Delay","evidence":"Late milestone","recommended_action":"Recover"})
        self.assertEqual(status,201)
        status,resolved=self.post("/risks/resolve",{**common,"risk_id":risk["id"],"resolution":"Recovered"})
        self.assertEqual(status,200);self.assertEqual(resolved["status"],"resolved")
        status,detail=self.get(f"/risks/detail?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner&risk_id={risk['id']}")
        self.assertEqual(status,200);self.assertEqual(len(detail["events"]),2)
        status,opportunity=self.post("/opportunities",{**common,"type":"upsell","reason":"Demand","evidence":"Requests","recommendation":"Propose"})
        self.assertEqual(status,201)
        status,advanced=self.post("/opportunities/advance",{**common,"opportunity_id":opportunity["id"],"to_status":"qualified","note":"Validated"})
        self.assertEqual(status,200);self.assertEqual(advanced["status"],"qualified")
        status,scope=self.get("/scope/status?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner")
        self.assertEqual(status,200);self.assertIn(scope["status"],{"no_contract","no_allowances","no_usage","unknown","recorded","over_scope"})

    def test_scope_contract_allowance_and_usage_writes_are_authenticated_and_scoped(self) -> None:
        common={"organization_id":"org_demo","workspace_id":"ws_alpha","person_id":"person_demo_owner"}
        status,contract=self.post("/contracts",{**common,"kind":"retainer","billing_model":"monthly","start_date":"2026-08-01","value":5000})
        self.assertEqual(status,201);self.assertEqual(contract["status"],"active")
        status,allowance=self.post("/scope/allowances",{**common,"contract_id":contract["id"],"service_category":"content","period":"2026-08","included_quantity":10})
        self.assertEqual(status,201);self.assertEqual(allowance["contract_id"],contract["id"])
        status,usage=self.post("/scope/usage",{**common,"contract_id":contract["id"],"allowance_id":allowance["id"],"period_start":"2026-08-01","delivered":12})
        self.assertEqual(status,201);self.assertEqual(usage["usage_percent"],120.0)
        status,error=self.post("/scope/usage",{**common,"contract_id":contract["id"],"allowance_id":allowance["id"],"period_start":"2026-08-01"})
        self.assertEqual(status,400);self.assertIn("delivered is required",error["message"])
        status,error=self.post("/contracts",{**common,"person_id":"person_not_the_token_owner","kind":"retainer","billing_model":"monthly","start_date":"2026-08-01"})
        self.assertEqual(status,403);self.assertEqual(error["error"],"authorization_error")

    def test_campaign_creative_finance_and_agent_run_completion_routes(self) -> None:
        common={"organization_id":"org_demo","workspace_id":"ws_alpha","person_id":"person_demo_owner"}
        status,campaign=self.post("/campaigns",{**common,"name":"Lifecycle","objective":"Leads","platform":"meta"})
        self.assertEqual(status,201)
        status,scheduled=self.post("/campaigns/transition",{**common,"campaign_id":campaign["id"],"to_status":"scheduled","note":"Ready"})
        self.assertEqual(status,200);self.assertEqual(scheduled["status"],"scheduled")
        status,campaign_detail=self.get(f"/campaigns/detail?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner&campaign_id={campaign['id']}")
        self.assertEqual(status,200);self.assertEqual(campaign_detail["events"][0]["to_status"],"scheduled")
        status,creative=self.post("/creative",{**common,"title":"Launch visual","format":"image","campaign_id":campaign["id"]})
        self.assertEqual(status,201)
        status,version=self.post("/creative/versions",{**common,"asset_id":creative["id"],"file_url":"https://example.test/v1.png","notes":"First cut"})
        self.assertEqual(status,201);self.assertEqual(version["version"],1)
        status,connection=self.post("/finance/connect",{"organization_id":"org_demo","person_id":"person_demo_owner","provider":"accounting-test"})
        self.assertEqual(status,200);self.assertEqual(connection["status"],"connected")
        status,cost=self.post("/finance/costs",{**common,"amount":125,"category":"labor","incurred_at":"2026-08-01","source":"timesheets"})
        self.assertEqual(status,201);self.assertEqual(cost["amount"],125)
        status,revenue=self.post("/finance/revenue",{**common,"amount":5000,"recognized_at":"2026-08-01","source":"accounting-test"})
        self.assertEqual(status,201);self.assertEqual(revenue["amount"],5000)
        status,invoice=self.post("/finance/invoices",{**common,"amount":1200,"issued_at":"2026-08-01","due_at":"2026-08-15","source":"accounting-test"})
        self.assertEqual(status,201);self.assertEqual(invoice["status"],"issued")
        status,finance=self.get("/finance?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner")
        self.assertEqual(status,200);self.assertEqual(finance["recognized_revenue"],5000);self.assertEqual(finance["outstanding_revenue"],1200)

        luna=next(item for item in self.os.agent_ops.seed_primary_agents("org_demo","person_demo_owner") if item["name"]=="Luna")
        self.os.agent_ops.configure_agent("org_demo","person_demo_owner",luna["id"],"local",["work.list"],["ws_alpha"],["domain.write"])
        status,task=self.post("/agents/tasks",{**common,"agent_id":luna["id"],"title":"Inspect","instructions":"List work"})
        self.assertEqual(status,201)
        status,claimed=self.post("/agents/runs/claim",{"organization_id":"org_demo","agent_id":luna["id"]})
        self.assertEqual(status,200);run=claimed["run"];self.assertEqual(run["task_id"],task["id"])
        status,_=self.post("/agents/runs/trace",{"organization_id":"org_demo","agent_id":luna["id"],"run_id":run["id"],"kind":"plan","message":"Inspect"})
        self.assertEqual(status,201)
        self.os.agent_ops.complete_run("org_demo",luna["id"],run["id"],"Done")
        status,runs=self.get("/agents/runs?organization_id=org_demo&person_id=person_demo_owner&workspace_id=ws_alpha")
        self.assertEqual(status,200);self.assertEqual(runs["runs"][0]["id"],run["id"])
        status,run_detail=self.get(f"/agents/runs/detail?organization_id=org_demo&person_id=person_demo_owner&run_id={run['id']}")
        self.assertEqual(status,200);self.assertEqual(run_detail["traces"][0]["kind"],"plan")

    def test_proactive_worker_status_never_claims_unstarted_work(self) -> None:
        status,body=self.get("/dashboard/intelligence/refresh-status?organization_id=org_demo&person_id=person_demo_owner&snapshot_type=executive")
        self.assertEqual(status,200)
        self.assertIn(body["status"],{"no_snapshot","queued","running","failed","stale","ready"})
        self.assertIn("worker_command",body)

    def test_provider_import_sync_without_transport_is_not_connected_over_http(self) -> None:
        status,result=self.post("/provider-imports/sync",{"provider":"stripe_accounting","account_id":"acct","resource":"invoices","workspace_mappings":{"acct":"ws_alpha"}})
        self.assertEqual(status,200)
        self.assertEqual(result["status"],"not_connected")
        status,body=self.get("/provider-imports/status")
        self.assertEqual(status,200)
        self.assertEqual(body["imports"][0]["status"],"not_connected")

    def test_operator_pause_and_health_are_workspace_scoped_over_http(self) -> None:
        status,alpha=self.post("/operator/pause",{"workspace_id":"ws_alpha","worker_id":"shared-worker"})
        self.assertEqual(status,200)
        self.assertTrue(alpha["paused"])
        status,alpha_health=self.get("/operator/health?workspace_id=ws_alpha&worker_id=shared-worker")
        self.assertEqual(status,200)
        self.assertTrue(alpha_health["paused"])
        status,beta_health=self.get("/operator/health?workspace_id=ws_beta&worker_id=shared-worker")
        self.assertEqual(status,200)
        self.assertFalse(beta_health["paused"])

    def test_expanded_mcp_names_share_permissioned_domains(self) -> None:
        router=McpToolRouter(self.os,self.identity); common={"organization_id":"org_demo","person_id":"person_demo_owner"}
        advertised={tool["name"] for tool in router.list_tools()}
        self.assertTrue({"brain.search","clients.list","projects.list","work.create","decisions.create","meetings.list","campaigns.performance","people.capacity","risks.list","agents.runs","reports.generate"}<=advertised)
        clients=router.call("clients.list",common);self.assertEqual(len(clients["clients"]),2)
        health=router.call("clients.health",{**common,"workspace_id":"ws_alpha"});self.assertIn("overall",health)
        brain=router.call("brain.search",{"workspace_id":"ws_alpha","actor_id":"act_alpha_admin","query":"consultation price"});self.assertFalse(brain["unknown"])
        report=router.call("reports.generate",{**common,"type":"capacity_report"});self.assertEqual(report["status"],"completed")

    def test_every_required_report_type_generates_with_citations(self) -> None:
        types=("daily_owner_brief","weekly_agency_brief","client_weekly_report","campaign_report","workload_report","capacity_report","revenue_report","churn_risk_report","creative_performance_report")
        for report_type in types:
            report=self.os.agent_ops.generate_report("org_demo","person_demo_owner",report_type,"ws_alpha" if report_type in {"client_weekly_report","campaign_report","creative_performance_report"} else None)
            self.assertEqual(report["status"],"completed");self.assertTrue(report["citations"])

    def test_client_hq_and_module_payloads_cover_operating_tabs(self) -> None:
        status,hq=self.get("/dashboard/client?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner")
        self.assertEqual(status,200);self.assertTrue({"brain","work","projects","campaigns","content","creative","files","meetings","messages","people","decisions","finance","risks","activity"}<=set(hq))
        status,module=self.get("/dashboard/module?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner&module=Campaigns")
        self.assertEqual(status,200);self.assertEqual(module["source_table"],"campaigns")

    def test_dashboard_work_rows_open_through_backend_detail_contract(self) -> None:
        status,hq=self.get("/dashboard/client?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner")
        self.assertEqual(status,200);self.assertGreater(len(hq["work"]),0)
        row=hq["work"][0]
        for key in ("id","title","status","priority"):
            self.assertIn(key,row)
        status,detail=self.get(f"/work/detail?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner&work_item_id={row['id']}")
        self.assertEqual(status,200)
        self.assertEqual(detail["work_item"]["id"],row["id"])
        self.assertIn("comments",detail);self.assertIn("files",detail);self.assertIn("versions",detail)
        self.assertIn("allowed_transitions",detail);self.assertIn("version",detail)

    def test_work_transition_endpoint_enforces_version_idempotency_and_workspace_scope(self) -> None:
        status,created=self.post("/work/items",{
            "organization_id":"org_demo",
            "workspace_id":"ws_alpha",
            "person_id":"person_demo_owner",
            "title":"Lifecycle API",
            "request":"Move this through the canonical route",
            "requested_by":"Client",
        })
        self.assertEqual(status,201)
        status,detail=self.get(f"/work/detail?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner&work_item_id={created['id']}")
        self.assertEqual(status,200)
        status,moved=self.post("/work/items/transition",{
            "organization_id":"org_demo",
            "workspace_id":"ws_alpha",
            "person_id":"person_demo_owner",
            "work_item_id":created["id"],
            "to_status":"assigned",
            "reason":"Accepted",
            "expected_version":detail["version"],
            "idempotency_key":"api-transition-1",
        })
        self.assertEqual(status,200)
        self.assertEqual(moved["work_item"]["status"],"assigned")
        status,replay=self.post("/work/items/transition",{
            "organization_id":"org_demo",
            "workspace_id":"ws_alpha",
            "person_id":"person_demo_owner",
            "work_item_id":created["id"],
            "to_status":"assigned",
            "reason":"Accepted",
            "expected_version":detail["version"],
            "idempotency_key":"api-transition-1",
        })
        self.assertEqual(status,200)
        self.assertEqual(replay,moved)
        status,mismatch=self.post("/work/items/transition",{
            "organization_id":"org_demo",
            "workspace_id":"ws_alpha",
            "person_id":"person_demo_owner",
            "work_item_id":created["id"],
            "to_status":"in_progress",
            "reason":"Different",
            "expected_version":moved["version"],
            "idempotency_key":"api-transition-1",
        })
        self.assertEqual(status,400)
        self.assertEqual(mismatch["error"],"validation_error")
        status,scoped=self.post("/work/items/transition",{
            "organization_id":"org_demo",
            "workspace_id":"ws_beta",
            "person_id":"person_demo_owner",
            "work_item_id":created["id"],
            "to_status":"in_progress",
            "expected_version":moved["version"],
            "idempotency_key":"api-transition-cross-scope",
        })
        self.assertIn(status,{403,404})

    def test_cross_workspace_rest_lookup_returns_no_records(self) -> None:
        outsider=self.os.create_person("org_demo","Prime only")
        self.os.add_person_to_workspace("org_demo","ws_alpha",outsider.id,"viewer")
        outsider_token,_=issue_identity(self.os,"org_demo",outsider.id,"ws_alpha")
        status,body=self.get(f"/campaigns?organization_id=org_demo&workspace_id=ws_beta&person_id={outsider.id}",outsider_token)
        self.assertEqual(status,403);self.assertEqual(body["error"],"authorization_error")

    def test_initiative_http_endpoint_creates_project_child(self) -> None:
        project=self.os.create_project("org_demo","ws_alpha","person_demo_owner","Initiative parent")
        status,body=self.post("/initiatives",{
            "organization_id":"org_demo",
            "workspace_id":"ws_alpha",
            "person_id":"person_demo_owner",
            "project_id":project.id,
            "name":"  Launch initiative  ",
        })
        self.assertEqual(status,201)
        self.assertEqual(body["project_id"],project.id)
        self.assertEqual(body["name"],"Launch initiative")


if __name__=="__main__": unittest.main()
