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


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class CapacitySurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        self.token, self.identity = issue_identity(self.os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin")
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.os.close()

    def get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        conn.request("GET", path, headers={"Authorization": f"Bearer {token or self.token}"})
        response = conn.getresponse(); body = json.loads(response.read()); conn.close()
        return response.status, body

    def test_http_capacity_requires_week_and_derives_identity(self) -> None:
        status, body = self.get("/capacity?week_start=2026-08-17")
        self.assertEqual(status, 200)
        self.assertEqual(body["organization_id"], "org_demo")
        self.assertEqual(body["week_start"], "2026-08-17")
        self.assertIn("people", body)
        status, _ = self.get("/capacity")
        self.assertEqual(status, 400)

    def test_mcp_capacity_matches_board_and_rejects_cross_workspace(self) -> None:
        router = McpToolRouter(self.os, self.identity)
        result = router.call("people.capacity", {"week_start": "2026-08-17"})
        self.assertEqual(result["organization_id"], "org_demo")
        self.assertIn("accounts", result)
        self.assertEqual([item["workspace_id"] for item in result["accounts"]], ["ws_alpha"])
        self.assertNotIn("Client Beta", str(result))
        denied = router.call("people.capacity", {"week_start": "2026-08-17", "workspace_id": "ws_beta"})
        self.assertEqual(denied["error"], "AuthorizationError")

    def test_capacity_report_cites_canonical_inputs(self) -> None:
        report = self.os.agent_ops.generate_report("org_demo", "person_demo_owner", "capacity_report")
        self.assertTrue(report["citations"])
        self.assertNotIn("capacity_snapshots", {citation["table"] for citation in report["citations"]})


if __name__ == "__main__":
    unittest.main()
