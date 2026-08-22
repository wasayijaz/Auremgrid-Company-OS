from __future__ import annotations

import unittest
import threading
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.demo_agency import DEMO_CREATIVE_PREVIEW, ORG_ID, seed_realistic_agency_demo
from auremgrid.services.brain import CompanyOS
from auremgrid.api.http import serve
from tests.auth_support import issue_identity


class RealisticAgencyDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")

    def tearDown(self) -> None:
        self.os.close()

    def test_seed_has_three_clients_linked_records_and_evidence(self) -> None:
        result = seed_realistic_agency_demo(self.os)
        self.assertEqual(result["workspaces"], ["ws_prime_clinics", "ws_base_ryder", "ws_evolve"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM projects WHERE organization_id=?", (ORG_ID,)).fetchone()[0], 6)
        for table in ("work_items", "deliverables", "reviews", "risks", "decisions", "campaigns", "creative_assets", "content_items", "performance_insights"):
            self.assertGreaterEqual(self.os.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 3, table)
        self.assertEqual(dict(self.os.store.conn.execute("SELECT status,COUNT(*) FROM work_items GROUP BY status").fetchall()), {"captured": 3, "client_review": 3, "review": 3, "shipped": 3})
        self.assertEqual(dict(self.os.store.conn.execute("SELECT status,COUNT(*) FROM reviews GROUP BY status").fetchall()), {"approved": 3, "open": 3, "revision_requested": 3})
        self.assertEqual({row[0] for row in self.os.store.conn.execute("SELECT severity FROM risks")}, {"low", "medium", "high"})
        _token, identity = issue_identity(self.os, ORG_ID, "person_realistic_owner", "ws_prime_clinics", "act_ws_prime_clinics")
        brain = self.os.dashboard.brain(identity, ORG_ID, "ws_prime_clinics", "person_realistic_owner")
        self.assertGreaterEqual(brain["summary"]["sources"], 1)
        self.assertGreaterEqual(brain["summary"]["current_truths"], 4)
        self.assertGreaterEqual(brain["summary"]["history"], 4)
        self.assertGreaterEqual(brain["summary"]["decisions"], 1)
        self.assertGreaterEqual(brain["summary"]["preferences"], 1)
        self.assertTrue(brain["collections"]["current_truth"])
        self.assertTrue(brain["collections"]["preferences"])
        self.assertGreaterEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM documents WHERE workspace_id LIKE 'ws_%' AND source_id IN (SELECT id FROM sources WHERE source_key LIKE 'realistic_fixture_%')").fetchone()[0], 3)
        self.assertEqual(self.os.agency_ops.finance_status(ORG_ID, "person_realistic_owner")["status"], "not_connected")

    def test_seed_is_idempotent(self) -> None:
        seed_realistic_agency_demo(self.os)
        before = {table: self.os.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("projects", "work_items", "deliverables", "reviews", "risks", "decisions", "campaigns", "creative_assets", "content_items", "performance_insights")}
        seed_realistic_agency_demo(self.os)
        after = {table: self.os.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        self.assertEqual(before, after)

    def test_existing_prose_source_is_upgraded_with_structured_facts(self) -> None:
        self.os.create_organization("Auremgrid Realistic Agency", ORG_ID)
        owner = self.os.create_person(ORG_ID, "Mara Chen", "person_realistic_owner@demo.invalid", role="owner", person_id="person_realistic_owner")
        workspace = self.os.create_organization_workspace(ORG_ID, "Prime Clinics", "client", "ws_prime_clinics")
        self.os.add_person_to_workspace(ORG_ID, workspace.id, owner.id, "admin")
        actor = self.os.create_actor(workspace.id, "Prime Account Lead", "admin", "act_ws_prime_clinics")
        self.os.ingest_text(workspace.id, actor.id, "realistic_fixture_ws_prime_clinics", "Prime Clinics quarterly brief. Approved positioning is current.", "fixture://old")
        seed_realistic_agency_demo(self.os)
        facts = self.os.store.conn.execute("SELECT COUNT(*) FROM facts f JOIN sources s ON s.id=f.source_id WHERE s.workspace_id=? AND s.source_key=?", (workspace.id, "realistic_fixture_ws_prime_clinics")).fetchone()[0]
        self.assertGreaterEqual(facts, 4)
        sources_before = self.os.store.conn.execute("SELECT COUNT(*) FROM sources WHERE workspace_id=? AND source_key=?", (workspace.id, "realistic_fixture_ws_prime_clinics")).fetchone()[0]
        seed_realistic_agency_demo(self.os)
        self.assertEqual(sources_before, self.os.store.conn.execute("SELECT COUNT(*) FROM sources WHERE workspace_id=? AND source_key=?", (workspace.id, "realistic_fixture_ws_prime_clinics")).fetchone()[0])

    def test_can_attach_scenario_to_existing_demo_org_for_dashboard(self) -> None:
        self.os.seed_demo()
        result = seed_realistic_agency_demo(self.os, "org_demo", "person_demo_owner")
        self.assertEqual(result["organization_id"], "org_demo")
        _token, identity = issue_identity(self.os, "org_demo", "person_demo_owner", "ws_prime_clinics")
        self.assertEqual(len(self.os.dashboard.client_hq(identity, "org_demo", "ws_prime_clinics", "person_demo_owner")["projects"]), 2)
        self.assertEqual(self.os.company.workspace_scope("ws_prime_clinics")["organization_id"], "org_demo")

    def test_creative_fixture_previews_are_real_assets_and_non_demo_media_is_untouched(self) -> None:
        external_org = self.os.create_organization("External Customer", "org_external")
        external_person = self.os.create_person(external_org.id, "External Owner", "external@example.invalid", role="owner", person_id="person_external")
        external_ws = self.os.create_organization_workspace(external_org.id, "External Workspace", "client", "ws_external")
        self.os.add_person_to_workspace(external_org.id, external_ws.id, external_person.id, "admin")
        external_project = self.os.create_project(external_org.id, external_ws.id, external_person.id, "External Project")
        external_deliverable = self.os.create_deliverable(external_org.id, external_ws.id, external_person.id, external_project.id, "Customer creative", "ad_creative")
        seed_realistic_agency_demo(self.os)
        rows = self.os.store.conn.execute("SELECT preview_url FROM deliverables WHERE organization_id=? AND type='ad_creative'", (ORG_ID,)).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["preview_url"] == DEMO_CREATIVE_PREVIEW for row in rows))
        self.assertIsNone(self.os.company.get_deliverable(external_ws.id, external_deliverable.id).preview_url)
        asset = Path(__file__).parents[1] / "src" / "auremgrid" / "api" / "dashboard" / "demo-agency-creative.svg"
        self.assertTrue(asset.is_file())
        self.assertIn("Synthetic demo fixture preview", asset.read_text(encoding="utf-8"))
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", DEMO_CREATIVE_PREVIEW)
            response = conn.getresponse(); body = response.read(); conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")
            self.assertIn(b"Synthetic demo fixture preview", body)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
