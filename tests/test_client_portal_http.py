from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class ClientPortalHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.os.create_organization("Agency", "org_portal_http")
        self.os.create_organization_workspace("org_portal_http", "Prime", "client", "ws_portal_http")
        self.os.create_person(
            "org_portal_http", "Owner", "owner@portal.test", role="owner", person_id="person_owner_http",
        )
        self.os.add_person_to_workspace("org_portal_http", "ws_portal_http", "person_owner_http", "admin")
        self.os.create_actor("ws_portal_http", "Owner actor", "admin", "actor_owner_http")
        self.staff_token, _ = issue_identity(
            self.os, "org_portal_http", "person_owner_http", "ws_portal_http", "actor_owner_http",
        )
        self.os.create_person(
            "org_portal_http", "Client Rep", "rep@portal.test", role="client", person_id="person_client_http",
        )
        self.os.add_person_to_workspace("org_portal_http", "ws_portal_http", "person_client_http", "client")
        self.client_token, _ = issue_identity(self.os, "org_portal_http", "person_client_http", "ws_portal_http")

        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.os.close()

    def request(self, method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_client_submits_intake_and_staff_accepts_via_http(self) -> None:
        status, item = self.request(
            "POST", "/client-portal/intake", self.client_token,
            {"organization_id": "org_portal_http", "workspace_id": "ws_portal_http",
             "title": "New landing page", "request": "Need a page for launch"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(item["status"], "pending")

        status, queue = self.request(
            "GET",
            f"/client-portal/intake/queue?organization_id=org_portal_http&workspace_id=ws_portal_http",
            self.staff_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(queue["intake_requests"]), 1)

        status, accepted = self.request(
            "POST", "/client-portal/intake/accept", self.staff_token,
            {"organization_id": "org_portal_http", "workspace_id": "ws_portal_http",
             "intake_request_id": item["id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(accepted["status"], "accepted")

    def test_client_cannot_call_staff_only_intake_queue_endpoint_without_workspace_access(self) -> None:
        # A client identity does have workspace_read in its own workspace, so
        # confirm the queue endpoint still requires the caller be a member of
        # that exact workspace rather than any organization member.
        other_org = "org_portal_http"
        status, _ = self.request(
            "GET",
            f"/client-portal/intake/queue?organization_id={other_org}&workspace_id=does_not_exist",
            self.client_token,
        )
        self.assertIn(status, (403, 404))

    def test_client_cannot_call_same_workspace_staff_queue_endpoint(self) -> None:
        status, _ = self.request(
            "GET",
            "/client-portal/intake/queue?organization_id=org_portal_http&workspace_id=ws_portal_http",
            self.client_token,
        )
        self.assertEqual(status, 403)

    def test_client_identity_lacks_workspace_write_capability(self) -> None:
        status, me = self.request(
            "GET", "/auth/me?organization_id=org_portal_http&workspace_id=ws_portal_http", self.client_token,
        )
        self.assertEqual(status, 200)
        self.assertNotIn("workspace_write", me["capabilities"])
        self.assertIn("client_portal", me["capabilities"])

    def test_client_cannot_submit_intake_using_staff_capability_routes(self) -> None:
        # /work/capture requires workspace_write, which the client role does
        # not have; the client-portal front door is the only path in.
        status, _ = self.request(
            "POST", "/work/capture", self.client_token,
            {"organization_id": "org_portal_http", "workspace_id": "ws_portal_http",
             "title": "Sneaky direct work", "request": "x", "requested_by": "client"},
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
