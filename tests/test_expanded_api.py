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
