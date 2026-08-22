from __future__ import annotations

import unittest
import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.demo_agency import ORG_ID, seed_realistic_agency_demo
from auremgrid.services.brain import CompanyOS
from auremgrid.domain.errors import AuthorizationError
from auremgrid.api.http import serve
from tests.auth_support import issue_identity


class OperatingDetailSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        seed_realistic_agency_demo(self.os)

    def tearDown(self) -> None:
        self.os.close()

    def test_people_detail_is_canonical_and_scoped(self) -> None:
        view = self.os.dashboard.person_detail(ORG_ID, "person_realistic_owner", "person_realistic_strategist", "ws_prime_clinics", "2026-08-24")
        self.assertEqual(view["person"]["id"], "person_realistic_strategist")
        self.assertEqual(len(view["projects"]), 1)
        self.assertIn("work", view)
        self.assertIn("reviews", view)
        self.assertIn("skills", view)
        self.assertIn("leave", view)
        self.assertIn(view["capacity_status"], {"sourced", "unknown"})
        with self.assertRaises(AuthorizationError):
            self.os.dashboard.person_detail(ORG_ID, "missing-viewer", "person_realistic_strategist", "ws_prime_clinics")

    def test_people_detail_org_wide_memberships_and_client_denial(self) -> None:
        view = self.os.dashboard.person_detail(ORG_ID, "person_realistic_owner", "person_realistic_operator")
        self.assertGreaterEqual(len(view["memberships"]), 3)
        self.assertTrue({row["workspace_id"] for row in view["memberships"]}.issuperset({"ws_prime_clinics", "ws_base_ryder", "ws_evolve"}))
        self.assertGreaterEqual(len(view["projects"]), 3)
        with self.assertRaises(AuthorizationError):
            self.os.dashboard.person_detail(ORG_ID, "person_realistic_client_prime", "person_realistic_operator")

    def test_agent_detail_and_performance_are_real_payloads(self) -> None:
        agents = self.os.agent_ops.seed_primary_agents(ORG_ID, "person_realistic_owner")
        agent_id = agents[0]["id"]
        detail = self.os.dashboard.agent_detail(ORG_ID, "person_realistic_owner", agent_id)
        self.assertIn("tools", detail["agent"])
        self.assertIn("queue", detail)
        self.assertIn("quality", detail)
        performance = self.os.dashboard.performance_surface(ORG_ID, "ws_prime_clinics", "person_realistic_owner")
        self.assertEqual(len(performance["campaigns"]), 1)
        self.assertGreaterEqual(len(performance["creative_comparison"]), 2)
        self.assertTrue(performance["insights"])

    def test_ui_and_http_contracts_expose_authenticated_detail_routes(self) -> None:
        root = Path(__file__).parents[1]
        js = "\n".join(path.read_text(encoding="utf-8") for path in (root / "src/auremgrid/api/dashboard/js").glob("*.js"))
        http = (root / "src/auremgrid/api/http.py").read_text(encoding="utf-8")
        for route in ("/people/detail", "/agents/detail", "/dashboard/performance"):
            self.assertIn(route, js)
            self.assertIn(route, http)
        for marker in ("openPersonInspector407", "openAgentInspector407", "creative_comparison", "evidence_status", "data-person-id", "data-agent-id", "target_person_id"):
            self.assertIn(marker, js)
        people_inspector = js[js.index("openPersonInspector407"):js.index("async function renderPeople407")]
        self.assertNotIn("workspace_id=${encodeURIComponent(workspace)}", people_inspector)
        self.assertIn("Client memberships", people_inspector)
        agents_renderer = js[js.index("function renderAgents407"):js.index("function ensureWorkSideInspector")]
        self.assertNotIn("scalarRows(agent", agents_renderer)
        self.assertNotIn("organization id", agents_renderer.lower())
        self.assertIn("metric-strip", js)
        self.assertIn("toFixed(2)", js)
        self.assertIn("n.toFixed(1)", js)
        self.assertNotIn("(n*100).toFixed(1)", js)
        self.assertIn("Math.round(n*100)", js)
        self.assertIn("Canonical campaign record; performance is shown below.", js)

    def test_authenticated_http_person_detail_separates_viewer_and_target(self) -> None:
        token, _ = issue_identity(self.os, ORG_ID, "person_realistic_owner", "ws_prime_clinics", "act_ws_prime_clinics")
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            def get(query: str, bearer: str = token) -> tuple[int, dict]:
                conn = HTTPConnection(host, port, timeout=5)
                conn.request("GET", query, headers={"Authorization": f"Bearer {bearer}"})
                response = conn.getresponse(); body = json.loads(response.read()); conn.close()
                return response.status, body
            status, body = get("/people/detail?organization_id=org_realistic_agency_demo&person_id=person_realistic_owner&target_person_id=person_realistic_strategist&workspace_id=ws_prime_clinics")
            self.assertEqual(status, 200, body)
            self.assertEqual(body["person"]["id"], "person_realistic_strategist")
            spoof_status, _ = get("/people/detail?organization_id=org_realistic_agency_demo&person_id=person_realistic_strategist&target_person_id=person_realistic_owner&workspace_id=ws_prime_clinics")
            self.assertIn(spoof_status, {401, 403})
            cross_status, _ = get("/people/detail?organization_id=org_unknown&person_id=person_realistic_owner&target_person_id=person_realistic_strategist&workspace_id=ws_prime_clinics")
            self.assertIn(cross_status, {401, 403})
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
