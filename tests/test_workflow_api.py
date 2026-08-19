from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class WorkflowApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.os.create_organization("Auremgrid", "org_workflow")
        self.os.create_organization_workspace("org_workflow", "Delivery", "client", "ws_workflow")
        self.os.create_organization_workspace("org_workflow", "Restricted", "client", "ws_restricted")
        self.os.create_person("org_workflow", "Workflow Owner", role="owner", person_id="person_workflow")
        self.os.add_person_to_workspace("org_workflow", "ws_workflow", "person_workflow", "admin")
        self.token, self.identity = issue_identity(
            self.os, "org_workflow", "person_workflow", "ws_workflow"
        )
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.os.close()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None: headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_rest_catalog_create_run_and_summary(self) -> None:
        query = "organization_id=org_workflow&person_id=person_workflow"
        status, catalog = self.request("GET", f"/workflows/templates?{query}")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["templates"]), 8)

        status, run = self.request(
            "POST",
            "/workflows/runs",
            {
                "organization_id": "org_workflow",
                "workspace_id": "ws_workflow",
                "person_id": "person_workflow",
                "template_id": "campaign_launch",
                "idempotency_key": "campaign-launch-1",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(run["definition_key"], "campaign_launch")

        run_query = f"organization_id=org_workflow&workspace_id=ws_workflow&person_id=person_workflow&run_id={run['id']}"
        status, summary = self.request("GET", f"/workflows/runs/get?{run_query}")
        self.assertEqual(status, 200)
        self.assertEqual(summary["progress"]["total"], 3)
        self.assertEqual(summary["run"]["template_snapshot"]["key"], "campaign_launch")

    def test_rest_workflow_scope_does_not_leak(self) -> None:
        status, body = self.request(
            "GET",
            "/workflows/runs?organization_id=org_workflow&workspace_id=ws_restricted&person_id=person_workflow",
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "authorization_error")

    def test_mcp_workflow_tools_share_the_same_engine(self) -> None:
        router = McpToolRouter(self.os, self.identity)
        common = {
            "organization_id": "org_workflow",
            "workspace_id": "ws_workflow",
            "person_id": "person_workflow",
        }
        created = router.call(
            "workflows.runs.create",
            {**common, "template_id": "client_request", "idempotency_key": "request-1"},
        )
        self.assertNotIn("error", created)
        fetched = router.call("workflows.runs.get", {**common, "run_id": created["id"]})
        self.assertEqual(fetched["run"]["id"], created["id"])
        names = {tool["name"] for tool in router.list_tools()}
        self.assertIn("workflows.handoffs.acknowledge", names)


if __name__ == "__main__":
    unittest.main()
