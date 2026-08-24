from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class AssetRegistryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org_asset_api")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client", "ws_asset_api")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Other", "client", "ws_asset_api_other")
        self.owner = self.os.create_person(self.org.id, "Owner", "owner@asset-api.test", role="owner", person_id="person_asset_api")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.token, self.identity = issue_identity(self.os, self.org.id, self.owner.id, self.ws.id)
        self.asset = self.os.asset_recovery.register_asset(
            self.identity, self.org.id, self.ws.id, "Launch brief", "document",
            "s3://assets/launch-brief.pdf", "critical", {"source": "manual"},
            size_bytes=12, sha256="a" * 64,
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

    def get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", path, headers={"Authorization": f"Bearer {token or self.token}"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_workspace_scoped_list_and_detail_expose_integrity_review_and_audit(self) -> None:
        status, listed = self.get(f"/assets?workspace_id={self.ws.id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["assets"]), 1)
        row = listed["assets"][0]
        self.assertEqual(row["id"], self.asset["id"])
        self.assertEqual(row["sha256"], "a" * 64)
        self.assertEqual(row["checksum"], "a" * 64)
        self.assertEqual(row["retention_class"], "critical")
        self.assertIsNone(row["review_status"])
        status, detail = self.get(
            f"/assets/detail?workspace_id={self.ws.id}&asset_id={self.asset['id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["asset"]["metadata"], {"source": "manual"})
        self.assertEqual(len(detail["recovery_audit"]), 1)
        self.assertEqual(detail["recovery_audit"][0]["action"], "register")

    def test_cross_workspace_asset_read_is_denied(self) -> None:
        status, body = self.get(f"/assets?workspace_id={self.other_ws.id}")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "authorization_error")

    def test_asset_registry_reads_are_not_mutating(self) -> None:
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM asset_recovery_audit").fetchone()[0]
        status, _ = self.get(f"/assets/detail?workspace_id={self.ws.id}&asset_id={self.asset['id']}")
        self.assertEqual(status, 200)
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM asset_recovery_audit").fetchone()[0]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
