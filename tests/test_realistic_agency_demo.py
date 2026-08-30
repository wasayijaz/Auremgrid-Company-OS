from __future__ import annotations

import unittest
import threading
import tempfile
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.demo_agency import DEMO_CREATIVE_PREVIEW, ORG_ID, seed_realistic_agency_demo
from auremgrid.services.brain import CompanyOS
from auremgrid.api.http import serve
from auremgrid.services.worker import run_one_job
from auremgrid.storage.backup import create_backup, restore_backup
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

    def test_seed_exposes_realistic_operating_depth_without_external_side_effects(self) -> None:
        result = seed_realistic_agency_demo(self.os)
        conn = self.os.store.conn
        scoped_tables = ("meetings", "conversations", "signals", "opportunities", "agents", "agent_tasks", "agent_runs",
                         "automations", "report_runs", "feedback_patterns", "forecasts", "retention_policies",
                         "integrations", "client_intake_requests")
        for table in scoped_tables:
            self.assertGreaterEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE organization_id=?", (ORG_ID,)).fetchone()[0], 1, table)
        for table in ("messages", "touchpoints", "automation_runs"):
            self.assertGreaterEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1, table)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM agents WHERE organization_id=?", (ORG_ID,)).fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_runs WHERE organization_id=? AND status='completed'", (ORG_ID,)).fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT DISTINCT status FROM automations WHERE organization_id=?", (ORG_ID,)).fetchone()[0], "training")
        self.assertEqual(conn.execute("SELECT DISTINCT status FROM automation_runs WHERE automation_id IN (SELECT id FROM automations WHERE organization_id=?)", (ORG_ID,)).fetchone()[0], "waiting_approval")
        self.assertTrue(conn.execute("SELECT 1 FROM feedback_patterns WHERE organization_id=? AND preference_status='proposed'", (ORG_ID,)).fetchone())
        self.assertTrue(conn.execute("SELECT 1 FROM forecasts WHERE organization_id=? AND forecast_type='capacity'", (ORG_ID,)).fetchone())
        self.assertEqual(conn.execute("SELECT status FROM integrations WHERE organization_id=? AND source='clickup'", (ORG_ID,)).fetchone()[0], "not_connected")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM finance_connections WHERE organization_id=?", (ORG_ID,)).fetchone()[0], 0)
        self.assertEqual(result["operating_depth"]["agents"], 3)

    def test_clean_database_agency_rehearsal_covers_hardened_operating_loop(self) -> None:
        result = seed_realistic_agency_demo(self.os)
        org, owner, ws = result["organization_id"], "person_realistic_owner", "ws_prime_clinics"
        operator, client = "person_realistic_operator", "person_client_prime"
        _token, identity = issue_identity(self.os, org, owner, ws, "act_ws_prime_clinics")

        command = self.os.dashboard.command(org, owner)
        self.assertEqual(command["metrics"]["active_clients"], 3)
        self.assertEqual(command["metrics"]["finance_status"], "not_connected")
        self.assertGreaterEqual(len(command["agents"]), 3)
        self.assertTrue(command["cosmo"]["writes_require_canonical_routes"])
        hq = self.os.dashboard.client_hq(identity, org, ws, owner)
        self.assertGreaterEqual(len(hq["people"]), 4)
        self.assertGreaterEqual(len(hq["meetings"]), 1)
        self.assertGreaterEqual(len(hq["decisions"]), 1)
        self.assertGreaterEqual(len(hq["risks"]), 2)
        self.assertGreaterEqual(len(hq["insights"]["opportunities"]), 1)
        self.assertGreaterEqual(len(hq["campaigns"]), 1)
        self.assertGreaterEqual(len(hq["content"]), 1)
        self.assertGreaterEqual(len(hq["creative"]), 2)
        self.assertEqual(hq["finance"]["status"], "not_connected")
        brain = self.os.dashboard.brain(identity, org, ws, owner)
        self.assertGreaterEqual(brain["summary"]["current_truths"], 4)
        self.assertTrue(brain["collections"]["preferences"])

        contract = self.os.client_ops.create_contract(
            org, ws, operator, "retainer", "monthly", "2026-08-01", 9000, end_date="2026-09-30",
        )
        allowance = self.os.client_ops.add_scope_allowance(
            org, ws, operator, contract["id"], "creative", "monthly", included_quantity=3,
        )
        usage = self.os.client_ops.record_scope_usage(
            org, ws, operator, contract["id"], allowance["id"], "2026-08-01", 3, 1, 1,
        )
        self.assertGreater(usage["usage_percent"], 100)
        self.assertEqual(self.os.client_ops.scope_status(org, ws, owner)["summary"]["over_scope"], 1)
        self.os.client_ops.create_client_roster(
            org,
            ws,
            owner,
            [
                {"role_key": "client_success_dri", "person_id": operator},
                {"role_key": "client_success_backup", "person_id": owner},
                {"role_key": "wing_lead", "wing": "strategy", "person_id": operator},
                {"role_key": "wing_executive", "wing": "creative", "person_id": operator},
            ],
        )

        workflow = self.os.workflow_ops.create_run(
            org,
            ws,
            operator,
            {
                "key": "fixture_rehearsal_launch",
                "name": "Fixture rehearsal launch",
                "version": "1",
                "stages": [
                    {"key": "brief", "name": "Brief", "sequence": 1,
                     "assignee": {"wing": "strategy", "role": "lead"}, "required_evidence": ["brief"],
                     "handoff_to": {"wing": "creative", "role": "designer"}, "handoff_contract": "approved brief and evidence links"},
                    {"key": "creative", "name": "Creative", "sequence": 2, "depends_on": ["brief"],
                     "assignee": {"wing": "creative", "role": "designer"}, "required_evidence": ["preview"], "requires_approval": True},
                ],
            },
            idempotency_key="fixture-rehearsal-workflow",
        )
        self.os.workflow_ops.start_stage(org, ws, operator, workflow["id"], "brief")
        self.os.workflow_ops.submit_evidence(org, ws, operator, workflow["id"], "brief", "brief", text="approved brief")
        self.os.workflow_ops.complete_stage(org, ws, operator, workflow["id"], "brief")
        self.os.workflow_ops.acknowledge_handoff(
            org, ws, operator, workflow["id"], "brief", "creative", "approved brief and evidence links"
        )
        self.os.workflow_ops.start_stage(org, ws, operator, workflow["id"], "creative")
        self.os.workflow_ops.submit_evidence(org, ws, operator, workflow["id"], "creative", "preview", uri=DEMO_CREATIVE_PREVIEW)
        stage_approval = self.os.agency_ops.request_approval(
            org, "person", operator, f"workflow:{workflow['id']}:creative", "workflow_stage_approval",
            {"run_id": workflow["id"], "stage_key": "creative"}, "fixture workflow gate", "human", ws, owner,
        )
        self.os.workflow_ops.request_approval(org, ws, operator, workflow["id"], "creative", "ready", stage_approval["id"])
        self.os.agency_ops.decide_approval(org, owner, stage_approval["id"], True, "approved")
        self.os.workflow_ops.decide_approval(
            org, ws, owner, workflow["id"], "creative", "approve", "approved", stage_approval["id"],
        )
        self.os.workflow_ops.complete_stage(org, ws, operator, workflow["id"], "creative")
        self.assertEqual(self.os.workflow_ops.summary(org, ws, owner, workflow["id"])["run"]["status"], "completed")

        source = self.os.store.conn.execute(
            "SELECT id FROM sources WHERE workspace_id=? AND source_key LIKE 'realistic_fixture_%' LIMIT 1", (ws,)
        ).fetchone()
        recommendation = self.os.intelligence_learning.record_recommendation(
            org, ws, owner, "Approve the over-scope follow-up before changing the launch plan.",
            runbook_id="client_health_drop", runbook_version=1,
            profile_contributors=[{"profile_id": "account_strategist", "version": 1, "role": "lead"}],
            confidence=0.76,
            options=[{"id": "approve_followup", "label": "Approve follow-up"}, {"id": "defer", "label": "Defer"}],
            recommended_option_id="approve_followup",
            evidence_refs=[{"type": "source", "id": source["id"]}],
            evaluation_window_start=datetime.now(timezone.utc).isoformat(),
            evaluation_window_end=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            generated_by={"type": "runbook", "id": "client_health_drop"},
            idempotency_key="fixture-rehearsal-recommendation",
        )
        self.assertEqual(recommendation["recommended_option_id"], "approve_followup")
        snapshot = self.os.proactive_intelligence.refresh_snapshot(org, owner, "executive")
        self.assertGreaterEqual(len(snapshot["attention"]), 1)

        self.os.auth.create_principal(org, owner, "person_realistic_owner@demo.invalid")
        agent = next(agent for agent in self.os.agent_ops.seed_primary_agents(org, owner) if agent["name"] == "Luna")
        action_payload = {
            "workspace_id": ws,
            "recipient_person_id": owner,
            "reason": "Fixture recommendation approved for local follow-up",
            "source_type": "intelligence_recommendation",
            "source_id": recommendation["id"],
            "idempotency_key": "fixture-rehearsal-notification",
        }
        action_approval = self.os.agency_ops.request_approval(
            org, "person", owner, "Create local follow-up notification", "notification.create",
            action_payload, "approved reversible fixture action", policy="auto", workspace_id=ws,
        )
        action_descriptor = {
            "action": "create_notification",
            "kind": "notification.create",
            "safe": True,
            "one_way": False,
            "payload": action_payload,
        }
        task = self.os.agent_ops.enqueue_task(
            org, owner, agent["id"], "Execute fixture approved recommendation", "Create the approved local notification",
            ws, action_descriptor=action_descriptor, approval_request_id=action_approval["id"],
        )
        run = self.os.agent_ops.start_run(org, owner, agent["id"], task["id"])
        job = run_one_job(self.os, org, ws, "fixture-rehearsal-worker")
        self.assertEqual(job["status"], "succeeded")
        detail = self.os.agent_ops.run_detail(org, owner, run["id"])
        self.assertEqual(detail["action_execution_boundary"]["status"], "succeeded")
        self.assertEqual(detail["action_executions"][0]["action"], "create_notification")
        self.assertEqual(self.os.agency_ops.attention(org, owner, 1)[0]["source_id"], recommendation["id"])

        intake = self.os.store.conn.execute(
            "SELECT id FROM client_intake_requests WHERE organization_id=? AND workspace_id=? AND submitted_by_person_id=? AND status='pending' LIMIT 1",
            (org, ws, client),
        ).fetchone()
        accepted = self.os.client_portal.accept_intake_request(org, ws, operator, intake["id"])
        self.assertEqual(accepted["status"], "accepted")
        self.assertIsNotNone(accepted["work_item_id"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = create_backup(self.os.store.conn, root / "agency.sqlite")
            restored = restore_backup(root / "agency.sqlite", root / "restored.sqlite")
            reopened = CompanyOS(root / "restored.sqlite")
            try:
                self.assertEqual(backup["integrity"], "ok")
                self.assertEqual(restored["integrity"], "ok")
                self.assertEqual(reopened.store.conn.execute("SELECT COUNT(*) FROM workspaces WHERE id LIKE 'ws_%'").fetchone()[0], 3)
                state = dict(reopened.store.conn.execute("SELECT key,value FROM system_state").fetchall())
                self.assertEqual(state["recovery_mode"], "1")
                self.assertEqual(state["outbound_dispatch"], "disabled")
                self.assertEqual(reopened.rebuild_projections()["status"], "healthy")
            finally:
                reopened.close()

        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM finance_connections WHERE organization_id=?", (org,)).fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT status FROM integrations WHERE organization_id=? AND source='clickup'", (org,)).fetchone()[0], "not_connected")

    def test_seed_is_idempotent(self) -> None:
        seed_realistic_agency_demo(self.os)
        before = {table: self.os.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("projects", "work_items", "deliverables", "reviews", "risks", "decisions", "campaigns", "creative_assets", "content_items", "performance_insights", "meetings", "conversations", "messages", "touchpoints", "signals", "opportunities", "agents", "agent_tasks", "agent_runs", "automations", "automation_runs", "report_runs", "feedback_patterns", "forecasts", "retention_policies", "integrations", "client_intake_requests")}
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
