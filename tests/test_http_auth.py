from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class HttpAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.os.create_organization("Auremgrid", "org_auth_http")
        self.os.create_organization_workspace("org_auth_http", "Allowed", "client", "ws_allowed")
        self.os.create_organization_workspace("org_auth_http", "Restricted", "client", "ws_restricted")
        self.os.create_person(
            "org_auth_http", "Owner", "owner@auth.test", role="owner", person_id="person_owner"
        )
        self.os.add_person_to_workspace("org_auth_http", "ws_allowed", "person_owner", "admin")
        self.os.create_actor("ws_allowed", "Bound actor", "admin", "actor_allowed")
        self.os.create_actor("ws_restricted", "Foreign actor", "admin", "actor_restricted")
        self.token, self.identity = issue_identity(
            self.os, "org_auth_http", "person_owner", "ws_allowed", "actor_allowed"
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

    def request(self, method: str, path: str, token: str | None = None, payload: dict | None = None) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_missing_credential_is_401_and_identity_is_derived(self) -> None:
        status, body = self.request("GET", "/workflows/templates?organization_id=org_auth_http")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "authentication_error")
        status, me = self.request("GET", "/auth/me?workspace_id=ws_allowed", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(me["person_id"], "person_owner")

    def test_forged_person_and_cross_workspace_are_denied(self) -> None:
        status, _ = self.request(
            "GET", "/workflows/templates?organization_id=org_auth_http&person_id=someone_else", self.token
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            "GET",
            "/workflows/runs?organization_id=org_auth_http&workspace_id=ws_restricted&person_id=person_owner",
            self.token,
        )
        self.assertEqual(status, 403)

    def test_api_token_scopes_cannot_expand_role_capabilities(self) -> None:
        api_token = self.os.auth.create_api_token(
            self.identity.principal_id, "read only", ["workspace_read"]
        )
        status, _ = self.request(
            "POST",
            "/workflows/runs",
            api_token["token"],
            {"workspace_id": "ws_allowed", "template_id": "client_request"},
        )
        self.assertEqual(status, 403)

    def test_authenticated_token_creation_and_session_rotation(self) -> None:
        status, created = self.request(
            "POST", "/auth/api-tokens", self.token,
            {"name": "read-service", "scopes": ["workspace_read"]},
        )
        self.assertEqual(status, 201)
        status, me = self.request("GET", "/auth/me?workspace_id=ws_allowed", created["token"])
        self.assertEqual(status, 200)
        self.assertEqual(me["auth_type"], "api_token")
        status, rotated = self.request("POST", "/auth/sessions/rotate", self.token, {})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/auth/me", self.token)[0], 401)
        self.assertEqual(self.request("GET", "/auth/me", rotated["token"])[0], 200)

    def test_mcp_rejects_forged_actor_and_person_arguments(self) -> None:
        router = McpToolRouter(self.os, self.identity)
        forged = router.call(
            "brain.search",
            {"workspace_id": "ws_allowed", "actor_id": "actor_restricted", "person_id": "someone_else", "query": "x"},
        )
        self.assertEqual(forged["error"], "AuthorizationError")

    def test_authenticated_job_control_is_scoped_and_idempotent(self) -> None:
        payload = {
            "workspace_id": "ws_allowed",
            "type": "report.generate",
            "payload": {"report_type": "client_weekly"},
            "idempotency_key": "weekly-report-1",
        }
        status, first = self.request("POST", "/jobs", self.token, payload)
        self.assertEqual(status, 201)
        status, second = self.request("POST", "/jobs", self.token, payload)
        self.assertEqual(status, 201)
        self.assertEqual(first["id"], second["id"])
        status, result = self.request(
            "GET", f"/jobs/get?workspace_id=ws_allowed&job_id={first['id']}", self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["job"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
