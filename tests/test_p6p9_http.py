from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class P6P9HttpTests(unittest.TestCase):
    def setUp(self):
        self.os = CompanyOS(":memory:")
        self.os.create_organization("Agency", "org_p6p9")
        self.os.create_organization_workspace("org_p6p9", "Main", "internal", "ws_p6p9")
        self.os.create_person("org_p6p9", "Owner", "owner@p6p9.test", role="owner", person_id="person_p6p9")
        self.os.add_person_to_workspace("org_p6p9", "ws_p6p9", "person_p6p9", "admin")
        self.os.create_actor("ws_p6p9", "Owner actor", "admin", "actor_p6p9")
        self.token, _ = issue_identity(self.os, "org_p6p9", "person_p6p9", "ws_p6p9", "actor_p6p9")
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5); self.os.close()

    def request(self, method, path, payload=None):
        c = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload); headers["Content-Type"] = "application/json"
        c.request(method, path, body, headers); r = c.getresponse(); data = json.loads(r.read()); c.close()
        return r.status, data

    def test_feedback_record_and_list(self):
        status, result = self.request("POST", "/feedback/record", {"workspace_id": "ws_p6p9", "category": "design", "raw_feedback": "Use blue", "source_type": "test"})
        self.assertEqual(status, 200); self.assertEqual(result["preference_status"], "observing")
        status, result = self.request("GET", "/feedback/patterns?workspace_id=ws_p6p9")
        self.assertEqual(status, 200); self.assertEqual(len(result), 1)

    def test_forecast_generate(self):
        for i, amount in enumerate((100, 120)):
            self.os.store.conn.execute("INSERT INTO revenues (id,organization_id,workspace_id,amount,currency,kind,recognized_at,source) VALUES (?,?,?,?,?,?,?,?)", (f"r{i}", "org_p6p9", "ws_p6p9", amount, "USD", "subscription", f"2026-0{i+5}-01", "test"))
        self.os.store.conn.commit()
        status, result = self.request("POST", "/forecasts/generate", {"forecast_type": "revenue"})
        self.assertEqual(status, 200); self.assertEqual(len(result), 3)
        status, result = self.request("GET", "/forecasts?forecast_type=revenue")
        self.assertEqual(status, 200); self.assertEqual(len(result), 3)

    def test_retention_policy(self):
        status, result = self.request("POST", "/retention/policies", {"scope": "organization", "data_category": "feedback", "max_age_days": 30, "action": "delete"})
        self.assertEqual(status, 200); self.assertEqual(result["scope"], "organization")
        status, result = self.request("GET", "/retention/policies?scope=organization")
        self.assertEqual(status, 200); self.assertEqual(len(result), 1)


if __name__ == "__main__": unittest.main()
