from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class IntelligenceEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        self.token, self.identity = issue_identity(
            self.os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin"
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_workspace_contract_contains_pipeline_provenance_and_safe_actions(self) -> None:
        result = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            result["context"]["pipeline"],
            ["evidence", "situation", "changes", "hypotheses", "scenarios", "impact", "recommendation"],
        )
        self.assertTrue(result["findings"])
        finding = result["findings"][0]
        self.assertTrue(finding["evidence"])
        self.assertTrue(all("citation" in evidence and "object_ref" in evidence for evidence in finding["evidence"]))
        self.assertTrue(all(action["safe"] and not action["one_way"] and action["status"] == "proposed" for action in finding["actions"]))

    def test_query_is_scoped_and_unknown_query_is_insufficient(self) -> None:
        matched = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin", query="consultation price"
        )
        self.assertEqual(matched["context"]["query"], "consultation price")
        self.assertGreater(matched["context"]["evidence_count"], 0)
        self.assertEqual(len(matched["findings"]), 1)
        self.assertIn("consultation price", matched["findings"][0]["title"].lower())
        self.assertEqual(matched["findings"][0]["actions"], [])
        unknown = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin", query="no such evidence phrase"
        )
        self.assertEqual(unknown["status"], "insufficient_evidence")
        self.assertEqual(unknown["degraded_reason"], "query_no_visible_evidence")
        self.assertEqual(unknown["findings"], [])

    def test_http_surface_enforces_identity_scope(self) -> None:
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "GET",
                "/dashboard/intelligence?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body["scope"]["workspace_id"], "ws_alpha")
            self.assertIn("findings", body)
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "GET",
                "/dashboard/intelligence/executive?organization_id=org_demo&person_id=person_demo_owner",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body["type"], "executive_brief")
            self.assertIn("portfolio", body)
        finally:
            server.shutdown()
            server.server_close()

    def test_historical_watermark_excludes_future_rows_and_actions(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.os.store.conn.execute("UPDATE work_items SET updated_at=? WHERE workspace_id=?", (future, "ws_alpha"))
        self.os.store.conn.commit()
        historical = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin",
            as_of=datetime.now(timezone.utc) - timedelta(days=1),
        )
        self.assertEqual(historical["context"]["open_work_count"], 0)
        self.assertTrue(all(not finding["actions"] and not finding["action_descriptors"] for finding in historical["findings"]))

    def test_read_only_workspace_member_never_receives_mutation_actions(self) -> None:
        viewer = self.os.create_person(
            "org_demo", "Read Only", "viewer@demo.invalid", role="member", person_id="person_demo_viewer"
        )
        self.os.add_person_to_workspace("org_demo", "ws_alpha", viewer.id, "viewer")
        result = self.os.intelligence.workspace("org_demo", "ws_alpha", viewer.id)
        self.assertTrue(result["findings"])
        self.assertTrue(all(not finding["actions"] and not finding["action_descriptors"] for finding in result["findings"]))

    def test_cross_domain_reasoning_scenarios_analogues_and_learning_are_explicit(self) -> None:
        result = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        for domain in (
            "work", "risks", "campaign_metrics", "finance", "scope",
            "client_health", "capacity", "reviews", "decisions", "signals",
        ):
            self.assertIn(domain, result["domains"])
        self.assertIn("cross_domain_relationships", result)
        self.assertIn("historical_analogues", result)
        self.assertIn("decision_action_outcome_learning", result)
        finding = result["findings"][0]
        self.assertIn("causal_links", finding)
        self.assertTrue(finding["hypotheses"])
        self.assertTrue(all("supporting_evidence" in item and "opposing_evidence" in item for item in finding["hypotheses"]))
        self.assertTrue(all("assumptions" in item and "domain_impacts" in item and "confidence" in item for item in finding["scenarios"]))

    def test_portfolio_and_executive_brief_use_only_permitted_workspaces(self) -> None:
        portfolio = self.os.intelligence.portfolio("org_demo", "person_demo_owner")
        permitted = {
            row["workspace_id"] for row in self.os.store.conn.execute(
                "SELECT workspace_id FROM workspace_memberships WHERE person_id=?", ("person_demo_owner",)
            ).fetchall()
        }
        self.assertEqual({item["scope"]["workspace_id"] for item in portfolio["workspaces"]}, permitted)
        self.assertIsNone(portfolio["portfolio"]["finance"]["recognized_revenue"])
        brief = self.os.intelligence.executive_brief("org_demo", "person_demo_owner")
        self.assertEqual(brief["type"], "executive_brief")
        self.assertIn("attention", brief["sections"])
        self.assertIn("constraints", brief["sections"])


if __name__ == "__main__":
    unittest.main()
