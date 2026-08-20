from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter, _mcp_capability
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class ClientRosterSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.os.create_organization("Org", "org_roster")
        self.os.create_organization_workspace("org_roster", "Allowed", "client", "ws_roster")
        self.os.create_organization_workspace("org_roster", "Other", "client", "ws_other_roster")
        self.os.create_person("org_roster", "Owner", "owner@roster.test", role="owner", person_id="person_roster")
        self.os.create_person("org_roster", "Backup", "backup@roster.test", role="member", person_id="person_backup")
        self.os.add_person_to_workspace("org_roster", "ws_roster", "person_roster", "admin")
        self.os.add_person_to_workspace("org_roster", "ws_roster", "person_backup", "operator")
        self.token, self.identity = issue_identity(self.os, "org_roster", "person_roster", "ws_roster")
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5); self.os.close()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Authorization": f"Bearer {self.token}"}; body = None
        if payload is not None:
            body = json.dumps(payload); headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers); response = conn.getresponse()
        result = json.loads(response.read()); conn.close(); return response.status, result

    def test_rest_roster_is_identity_scoped_and_write_gated(self) -> None:
        roles = [{"role_key": "client_success_dri", "person_id": "person_roster"}, {"role_key": "client_success_backup", "person_id": "person_backup"}]
        status, _ = self.request("POST", "/clients/roster", {"workspace_id": "ws_roster", "roles": roles})
        self.assertIn(status, (200, 201))
        status, body = self.request("GET", "/clients/roster?workspace_id=ws_other_roster")
        self.assertEqual(status, 403); self.assertNotIn("roles", body)

    def test_mcp_parity_and_capabilities(self) -> None:
        self.assertEqual(_mcp_capability("clients.roster.get"), "workspace_read")
        self.assertEqual(_mcp_capability("clients.roster.create"), "people_manage")
        self.assertEqual(_mcp_capability("meetings.responsibilities.set"), "people_manage")
        router = McpToolRouter(self.os, self.identity)
        result = router.call("clients.roster.create", {"workspace_id": "ws_roster", "roles": [{"role_key": "client_success_dri", "person_id": "person_roster"}, {"role_key": "client_success_backup", "person_id": "person_backup"}]})
        self.assertNotIn("error", result)
        read_only = AuthenticatedIdentity(self.identity.principal_id, self.identity.organization_id, self.identity.person_id, self.identity.auth_type, frozenset({"workspace_read"}), self.identity.scopes, self.identity.workspace_id)
        denied = McpToolRouter(self.os, read_only).call("clients.roster.create", {"workspace_id": "ws_roster", "roles": [{"role_key": "account_lead", "person_id": "person_roster"}]})
        self.assertEqual(denied.get("error"), "AuthorizationError")

    def test_meeting_responsibility_rest_mcp_parity(self) -> None:
        roles = [{"role_key": "client_success_dri", "person_id": "person_roster"}, {"role_key": "client_success_backup", "person_id": "person_backup"}]
        self.os.client_ops.create_client_roster("org_roster", "ws_roster", "person_roster", roles)
        meeting = self.os.client_ops.create_meeting("org_roster", "ws_roster", "person_roster", "Weekly", datetime.now(timezone.utc))
        status, body = self.request("POST", "/meetings/responsibilities", {"workspace_id": "ws_roster", "meeting_id": meeting.id, "facilitator_person_id": "person_roster"})
        self.assertEqual(status, 200); self.assertEqual(body["facilitator_person_id"], "person_roster")
        router = McpToolRouter(self.os, self.identity)
        result = router.call("meetings.responsibilities.get", {"workspace_id": "ws_roster", "meeting_id": meeting.id})
        self.assertEqual(result["meeting_id"], meeting.id); self.assertEqual(result["facilitator_person_id"], "person_roster")


if __name__ == "__main__":
    unittest.main()
