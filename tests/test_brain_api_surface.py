from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter, _mcp_capability
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class BrainApiSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.os.create_organization("Org", "org_surface")
        self.os.create_organization_workspace("org_surface", "Allowed", "client", "ws_surface")
        self.os.create_organization_workspace("org_surface", "Other", "client", "ws_other")
        self.os.create_person("org_surface", "Owner", "owner@surface.test", role="owner", person_id="person_surface")
        self.os.add_person_to_workspace("org_surface", "ws_surface", "person_surface", "admin")
        self.os.create_actor("ws_surface", "Actor", "admin", "actor_surface")
        self.token, self.identity = issue_identity(self.os, "org_surface", "person_surface", "ws_surface", "actor_surface")
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5); self.os.close()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload); headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse(); data = json.loads(response.read()); conn.close()
        return response.status, data

    def test_proposal_listing_is_workspace_scoped_and_identity_derived(self) -> None:
        status, body = self.request("GET", "/memory-proposals?workspace_id=ws_other&person_id=person_surface&organization_id=org_surface")
        self.assertEqual(status, 403)
        status, body = self.request("GET", "/memory-proposals?workspace_id=ws_surface&organization_id=org_surface")
        self.assertEqual(status, 200)
        self.assertEqual(body["proposals"], [])

    def test_legacy_review_is_deliberately_retired(self) -> None:
        status, body = self.request("POST", "/memory-proposals/review", {"workspace_id": "ws_surface", "person_id": "person_surface", "proposal_id": "missing", "action": "approve"})
        self.assertEqual(status, 404)
        self.assertIn("brain.promote", body["message"])

    def test_proposal_create_uses_bearer_attribution(self) -> None:
        status, item = self.request("POST", "/memory-proposals", {
            "workspace_id": "ws_surface", "proposer_type": "agent",
            "proposer_id": "spoof-person", "kind": "memory", "content": "remember this",
            "evidence": "operator supplied evidence",
        })
        self.assertEqual(status, 201)
        self.assertEqual(item["organization_id"], "org_surface")
        self.assertEqual(item["workspace_id"], "ws_surface")
        self.assertEqual(item["proposed_by_type"], "person")
        self.assertEqual(item["proposed_by_id"], "person_surface")

    def test_mcp_brain_mutations_have_write_capabilities(self) -> None:
        self.assertEqual(_mcp_capability("brain.propose"), "brain_propose")
        self.assertEqual(_mcp_capability("brain.promote"), "brain_promote")
        read_identity = AuthenticatedIdentity(self.identity.principal_id, self.identity.organization_id, self.identity.person_id, self.identity.auth_type, frozenset({"brain_read"}), self.identity.scopes, self.identity.workspace_id)
        router = McpToolRouter(self.os, read_identity)
        self.assertEqual(router.call("brain.propose", {"workspace_id": "ws_surface", "kind": "memory", "content": "x", "evidence": "x"})["error"], "AuthorizationError")
        self.assertEqual(router.call("brain.promote", {"workspace_id": "ws_surface", "proposal_id": "missing", "action": "approve"})["error"], "AuthorizationError")

    def test_mcp_brain_mutations_are_identity_native_end_to_end(self) -> None:
        router = McpToolRouter(self.os, self.identity)
        entity = self.os.brain_ops.create_entity("org_surface", "ws_surface", "person_surface", "Acme", "company")
        alias = router.call("brain.propose", {"workspace_id": "ws_surface", "kind": "alias",
            "candidate_entity_ids": [entity["id"]], "alias": "Acme Ltd", "score": .9,
            "rationale": "verified", "evidence": "registry"})
        self.assertEqual(alias.get("status"), "pending")
        promoted = router.call("brain.promote", {"workspace_id": "ws_surface", "proposal_id": alias["id"], "action": "approve"})
        self.assertTrue(promoted.get("resolved") or promoted.get("status") == "approved")
        fact = router.call("brain.propose", {"workspace_id": "ws_surface", "kind": "fact",
            "content": "Acme is active", "payload": {"subject": "Acme", "predicate": "status", "object": "active"},
            "evidence": "operator observation"})
        self.assertEqual(fact.get("status"), "pending")
        fact_result = router.call("brain.promote", {"workspace_id": "ws_surface", "proposal_id": fact["id"], "action": "reject"})
        self.assertEqual(fact_result.get("status"), "rejected")


if __name__ == "__main__":
    unittest.main()
