from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class PilotFeedbackHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.os.create_organization("Auremgrid", "org_pilot")
        self.os.create_organization_workspace("org_pilot", "Allowed", "client", "ws_allowed")
        self.os.create_organization_workspace("org_pilot", "Restricted", "client", "ws_restricted")
        self.os.create_person("org_pilot", "Owner", "owner@pilot.test", role="owner", person_id="person_owner")
        self.os.add_person_to_workspace("org_pilot", "ws_allowed", "person_owner", "admin")
        self.os.create_actor("ws_allowed", "Allowed actor", "admin", "actor_allowed")
        self.token, _ = issue_identity(self.os, "org_pilot", "person_owner", "ws_allowed", "actor_allowed")
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.os.close()

    def request(self, payload: dict, token: str | None = None) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        connection.request("POST", "/pilot/verdicts", body=json.dumps(payload), headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_authenticated_verdict_capture_persists_append_only_row(self) -> None:
        status, body = self.request(
            {
                "workspace_id": "ws_allowed",
                "scenario_id": "attention.usefulness",
                "verdict": "positive",
                "notes": "matched the operator review",
            },
            self.token,
        )

        self.assertEqual(status, 201)
        self.assertEqual(body["organization_id"], "org_pilot")
        self.assertEqual(body["recorded_by_person_id"], "person_owner")
        self.assertEqual(body["verdict"], "positive")
        row = self.os.store.conn.execute("SELECT * FROM pilot_operator_verdicts WHERE id=?", (body["id"],)).fetchone()
        self.assertEqual(row["scenario_id"], "attention.usefulness")
        self.assertEqual(row["notes"], "matched the operator review")

    def test_unauthenticated_verdict_capture_is_rejected(self) -> None:
        status, body = self.request(
            {"workspace_id": "ws_allowed", "scenario_id": "attention.usefulness", "verdict": "positive"}
        )

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "authentication_error")

    def test_foreign_workspace_verdict_capture_is_rejected(self) -> None:
        status, body = self.request(
            {"workspace_id": "ws_restricted", "scenario_id": "attention.usefulness", "verdict": "positive"},
            self.token,
        )

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "authorization_error")

    def test_foreign_org_verdict_capture_is_rejected(self) -> None:
        self.os.create_organization("Other", "org_other")
        self.os.create_organization_workspace("org_other", "Other workspace", "client", "ws_other")
        self.os.create_person("org_other", "Other Owner", "other@pilot.test", role="owner", person_id="person_other")
        self.os.add_person_to_workspace("org_other", "ws_other", "person_other", "admin")
        self.os.create_actor("ws_other", "Other actor", "admin", "actor_other")
        other_token, _ = issue_identity(self.os, "org_other", "person_other", "ws_other", "actor_other")

        status, body = self.request(
            {"workspace_id": "ws_allowed", "scenario_id": "attention.usefulness", "verdict": "positive"},
            other_token,
        )

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "authorization_error")


if __name__ == "__main__":
    unittest.main()
