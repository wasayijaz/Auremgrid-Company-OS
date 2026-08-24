from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter, _mcp_capability
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class IntelligenceContractSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.create_organization("Auremgrid", "org_intel_surface")
        self.os.create_organization_workspace("org_intel_surface", "Allowed", "client", "ws_intel_allowed")
        self.os.create_organization_workspace("org_intel_surface", "Denied", "client", "ws_intel_denied")
        self.os.create_person(
            "org_intel_surface", "Owner", "owner@intel.test", role="owner", person_id="person_intel_owner"
        )
        self.os.add_person_to_workspace("org_intel_surface", "ws_intel_allowed", "person_intel_owner", "admin")
        self.os.create_actor("ws_intel_allowed", "Bound actor", "admin", "actor_intel_allowed")
        self.token, self.identity = issue_identity(
            self.os, "org_intel_surface", "person_intel_owner", "ws_intel_allowed", "actor_intel_allowed"
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
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def scoped_path(self, route: str, **extra: object) -> str:
        params = {
            "organization_id": "org_intel_surface",
            "workspace_id": "ws_intel_allowed",
            "person_id": "person_intel_owner",
            **extra,
        }
        return route + "?" + "&".join(f"{key}={value}" for key, value in params.items())

    def test_rest_exposes_profiles_runbooks_and_scoped_result(self) -> None:
        status, profiles = self.request("GET", self.scoped_path("/dashboard/intelligence/profiles"))
        self.assertEqual(status, 200)
        self.assertEqual(len(profiles["profiles"]), 13)
        first_profile = profiles["profiles"][0]
        self.assertTrue({"id", "version", "name", "capability_level", "allowed_tool_refs"} <= first_profile.keys())

        status, profile = self.request(
            "GET", self.scoped_path("/dashboard/intelligence/profiles/get", profile_id=first_profile["id"])
        )
        self.assertEqual(status, 200)
        self.assertEqual(profile["profile"]["id"], first_profile["id"])

        status, runbooks = self.request("GET", self.scoped_path("/dashboard/intelligence/runbooks"))
        self.assertEqual(status, 200)
        self.assertEqual(len(runbooks["runbooks"]), 12)
        first_runbook = runbooks["runbooks"][0]
        self.assertTrue({"id", "version", "name", "profile_ids", "output_contract"} <= first_runbook.keys())

        status, runbook = self.request(
            "GET", self.scoped_path("/dashboard/intelligence/runbooks/get", runbook_id=first_runbook["id"])
        )
        self.assertEqual(status, 200)
        self.assertEqual(runbook["runbook"]["id"], first_runbook["id"])

        status, run = self.request(
            "POST",
            "/dashboard/intelligence/orchestrator/run",
            {
                "organization_id": "org_intel_surface",
                "workspace_id": "ws_intel_allowed",
                "person_id": "person_intel_owner",
                "runbook_id": first_runbook["id"],
                "iterations": 1,
            },
        )
        self.assertEqual(status, 200)
        result = run["result"]
        self.assertTrue(result["trace_id"].startswith("inteltrace_"))
        self.assertIn(result["status"], {"ready", "degraded"})
        self.assertIn("runbook_route", result)
        self.assertIn("profiles", result)
        self.assertIn("trace", result)

        status, fetched = self.request(
            "GET", self.scoped_path("/dashboard/intelligence/orchestrator/result", trace_id=result["trace_id"])
        )
        self.assertEqual(status, 200)
        self.assertEqual(fetched["result"]["trace_id"], result["trace_id"])

        status, latest = self.request("GET", self.scoped_path("/dashboard/intelligence/orchestrator/latest"))
        self.assertEqual(status, 200)
        self.assertEqual(latest["result"]["trace_id"], result["trace_id"])
        self.assertEqual(
            {action["action"] for action in latest["result"]["action_descriptors"]},
            {"challenge", "save_insight", "execute_approved_plan"},
        )
        self.assertTrue(all(action["safe"] is False and action["disabled"] is True for action in latest["result"]["action_descriptors"]))

    def test_rest_denies_cross_workspace_contract_and_result_reads(self) -> None:
        status, denied = self.request(
            "GET",
            "/dashboard/intelligence/profiles?organization_id=org_intel_surface&workspace_id=ws_intel_denied&person_id=person_intel_owner",
        )
        self.assertEqual(status, 403)
        self.assertNotIn("cosmo_", json.dumps(denied))

        status, run = self.request(
            "POST",
            "/dashboard/intelligence/orchestrator/run",
            {
                "organization_id": "org_intel_surface",
                "workspace_id": "ws_intel_allowed",
                "person_id": "person_intel_owner",
                "query": "workspace review",
            },
        )
        self.assertEqual(status, 200)
        trace_id = run["result"]["trace_id"]
        status, denied_result = self.request(
            "GET",
            f"/dashboard/intelligence/orchestrator/result?organization_id=org_intel_surface&workspace_id=ws_intel_denied&person_id=person_intel_owner&trace_id={trace_id}",
        )
        self.assertEqual(status, 403)
        self.assertNotIn(trace_id, json.dumps(denied_result))

    def test_mcp_tools_are_brain_read_and_identity_scoped(self) -> None:
        for name in (
            "intelligence.profiles.list",
            "intelligence.profiles.get",
            "intelligence.runbooks.list",
            "intelligence.runbooks.get",
            "intelligence.orchestrator.run",
            "intelligence.orchestrator.result",
        ):
            self.assertEqual(_mcp_capability(name), "brain_read")

        router = McpToolRouter(self.os, self.identity)
        tool_names = {tool["name"] for tool in router.list_tools()}
        self.assertIn("intelligence.profiles.list", tool_names)
        profiles = router.call("intelligence.profiles.list", {"workspace_id": "ws_intel_allowed"})
        self.assertEqual(len(profiles["profiles"]), 13)
        runbooks = router.call("intelligence.runbooks.list", {"workspace_id": "ws_intel_allowed"})
        self.assertEqual(len(runbooks["runbooks"]), 12)
        run = router.call(
            "intelligence.orchestrator.run",
            {"workspace_id": "ws_intel_allowed", "runbook_id": runbooks["runbooks"][0]["id"], "iterations": 1},
        )
        trace_id = run["result"]["trace_id"]
        fetched = router.call(
            "intelligence.orchestrator.result",
            {"workspace_id": "ws_intel_allowed", "trace_id": trace_id},
        )
        self.assertEqual(fetched["result"]["trace_id"], trace_id)

        forged = router.call("intelligence.profiles.list", {"workspace_id": "ws_intel_denied"})
        self.assertEqual(forged["error"], "AuthorizationError")
        forged_person = router.call(
            "intelligence.runbooks.list",
            {"workspace_id": "ws_intel_allowed", "person_id": "someone_else"},
        )
        self.assertEqual(forged_person["error"], "AuthorizationError")


if __name__ == "__main__":
    unittest.main()
