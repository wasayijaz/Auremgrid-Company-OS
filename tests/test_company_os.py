from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter
from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class CompanyOSTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)

    def tearDown(self) -> None:
        self.os.close()

    def test_cross_workspace_isolation(self) -> None:
        alpha = self.os.search("ws_alpha", "act_alpha_operator", "intro week")
        beta = self.os.search("ws_beta", "act_beta_admin", "consultation price")
        self.assertTrue(alpha.unknown)
        self.assertTrue(beta.unknown)
        self.assertFalse(any("Client Beta" in item.citation.evidence_span for item in alpha.items))
        self.assertFalse(any("Client Alpha" in item.citation.evidence_span for item in beta.items))

    def test_source_acl_hides_restricted_facts(self) -> None:
        operator = self.os.search("ws_alpha", "act_alpha_operator", "retainer")
        admin = self.os.search("ws_alpha", "act_alpha_admin", "retainer")
        self.assertTrue(operator.unknown)
        self.assertFalse(admin.unknown)
        self.assertTrue(any(item.kind == "fact" and item.payload["object"] == "1600 USD" for item in admin.items))

    def test_ingest_is_idempotent(self) -> None:
        first = self.os.ingest_path("ws_alpha", "act_alpha_admin", FIXTURES / "client_alpha" / "brand.md")
        second = self.os.ingest_path("ws_alpha", "act_alpha_admin", FIXTURES / "client_alpha" / "brand.md")
        self.assertFalse(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.source.id, second.source.id)

    def test_temporal_supersession_and_as_of(self) -> None:
        current = self.os.search(
            "ws_alpha",
            "act_alpha_operator",
            "consultation price",
            as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        previous = self.os.search(
            "ws_alpha",
            "act_alpha_operator",
            "consultation price",
            as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        current_prices = [item.payload["object"] for item in current.items if item.kind == "fact"]
        previous_prices = [item.payload["object"] for item in previous.items if item.kind == "fact"]
        self.assertIn("199 USD", current_prices)
        self.assertNotIn("149 USD", current_prices)
        self.assertIn("149 USD", previous_prices)
        self.assertNotIn("199 USD", previous_prices)

    def test_conflicting_evidence_is_preserved(self) -> None:
        history = self.os.history("ws_alpha", "act_alpha_operator", "Consultation", "price")
        objects = {fact["object"] for fact in history["facts"]}
        self.assertEqual(objects, {"149 USD", "199 USD", "189 USD"})
        current = [fact for fact in history["facts"] if fact["object"] == "199 USD"][0]
        conflict = [fact for fact in history["facts"] if fact["object"] == "189 USD"][0]
        self.assertIsNone(current["superseded_by"])
        self.assertIsNone(conflict["superseded_by"])
        self.assertEqual(conflict["conflict_group"], "consultation-price")

    def test_results_include_provenance(self) -> None:
        bundle = self.os.search("ws_alpha", "act_alpha_operator", "navy and cream")
        self.assertFalse(bundle.unknown)
        for item in bundle.items:
            citation = item.citation
            self.assertTrue(citation.source_id)
            self.assertTrue(citation.source_key)
            self.assertTrue(citation.locator)
            self.assertEqual(len(citation.content_hash), 64)
            self.assertTrue(citation.evidence_span)

    def test_prompt_injection_is_data_not_authority(self) -> None:
        bundle = self.os.search("ws_alpha", "act_alpha_operator", "Ignore previous instructions")
        self.assertFalse(bundle.unknown)
        self.assertTrue(all(item.kind in {"document", "fact"} for item in bundle.items))
        leaked = self.os.search("ws_alpha", "act_alpha_operator", "charcoal and lime")
        self.assertTrue(leaked.unknown)

    def test_read_only_agent_cannot_remember(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.remember("ws_alpha", "act_alpha_agent", "Client Alpha hates gradients")

    def test_mcp_and_http_search(self) -> None:
        router = McpToolRouter(self.os)
        payload = router.call(
            "search",
            {
                "workspace_id": "ws_alpha",
                "actor_id": "act_alpha_operator",
                "query": "visual rule",
            },
        )
        self.assertFalse(payload["unknown"])
        self.assertTrue(payload["items"])

        server = serve(self.os, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/search?workspace_id=ws_alpha&actor_id=act_alpha_operator&query=visual%20rule")
            response = conn.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertFalse(body["unknown"])
        finally:
            server.shutdown()
            server.server_close()

    def test_mcp_work_lifecycle_tools(self) -> None:
        router = McpToolRouter(self.os)
        common = {"workspace_id": "ws_beta", "actor_id": "act_beta_admin"}
        item = router.call(
            "capture_work",
            {
                **common,
                "title": "MCP request",
                "request": "Build a reviewable asset",
                "requested_by": "Studio lead",
            },
        )
        self.assertEqual(item["status"], "captured")
        item = router.call(
            "assign_work",
            {**common, "work_item_id": item["id"], "assignee_id": "act_beta_admin"},
        )
        self.assertEqual(item["status"], "assigned")
        item = router.call("start_work", {**common, "work_item_id": item["id"]})
        self.assertEqual(item["status"], "in_progress")
        item = router.call(
            "mark_dod",
            {
                **common,
                "work_item_id": item["id"],
                "checks": {key: True for key in item["definition_of_done"]},
            },
        )
        self.assertTrue(item["dod_complete"])
        item = router.call("submit_review", {**common, "work_item_id": item["id"]})
        self.assertEqual(item["status"], "review")
        item = router.call(
            "close_review",
            {**common, "work_item_id": item["id"], "approved": True},
        )
        self.assertEqual(item["status"], "client_review")
        item = router.call("ship_work", {**common, "work_item_id": item["id"]})
        self.assertEqual(item["status"], "shipped")

    def test_dashboard_route_serves_operating_surface(self) -> None:
        server = serve(self.os, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/dashboard")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.getheader("Content-Type", ""))
            self.assertEqual(body.count("<h1>"), 1)
            for marker in ("health-score", "capture-modal", "command-form", "Client brain coverage"):
                self.assertIn(marker, body)
            self.assertNotIn("<pre", body)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_work_route_validates_workspace_and_json(self) -> None:
        server = serve(self.os, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            conn = HTTPConnection(host, port, timeout=5)
            body = json.dumps(
                {
                    "workspace_id": "ws_beta",
                    "actor_id": "act_beta_admin",
                    "title": "HTTP request",
                    "request": "Build a reviewable asset",
                    "requested_by": "Studio lead",
                }
            )
            conn.request("POST", "/work/capture", body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            item = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(item["status"], "captured")

            cross_workspace = json.dumps(
                {
                    "workspace_id": "ws_beta",
                    "actor_id": "act_beta_admin",
                    "work_item_id": item["id"],
                    "assignee_id": "act_alpha_operator",
                }
            )
            conn.request("POST", "/work/assign", body=cross_workspace, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            error = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 404)
            self.assertEqual(error["error"], "not_found")

            conn.request("POST", "/work/close-review", body="not-json", headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_offline_sqlite_file_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "company-os.sqlite"
            first = CompanyOS(db_path)
            first.seed_demo(FIXTURES)
            first.close()
            second = CompanyOS(db_path)
            bundle = second.search("ws_beta", "act_beta_admin", "intro week")
            second.close()
            self.assertFalse(bundle.unknown)


if __name__ == "__main__":
    unittest.main()
