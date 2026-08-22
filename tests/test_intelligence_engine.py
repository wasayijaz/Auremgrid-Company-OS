from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.domain.errors import AuthorizationError
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
            result["context"]["pipeline"][:4],
            ["evidence", "situation", "changes", "hypotheses"],
        )
        for stage in ("scenarios", "impact", "recommendation", "decision", "workflow", "outcome", "learning"):
            self.assertIn(stage, result["context"]["pipeline"])
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
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "GET",
                "/dashboard/intelligence?organization_id=org_demo&workspace_id=ws_alpha&person_id=person_demo_owner"
                "&what_if_additional_clients=2&what_if_hours_per_new_client=18"
                "&what_if_leave_hours_delta=8&what_if_hiring_hours_delta=12"
                "&what_if_client_action=keep&what_if_client_revenue_delta=5000"
                "&what_if_client_cost_delta=1700&what_if_client_hours_delta=24",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            retained = body["context"]["scenario_inputs"]["retained_inputs"]
            self.assertEqual(retained["additional_clients"], 2.0)
            self.assertEqual(retained["client_action"], "keep")
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
        self.assertTrue(all("retained_inputs" in item and "constraints" in item and "mitigations" in item for item in finding["scenarios"]))

    def test_parameterized_what_if_context_plan_and_calibration_are_explicit(self) -> None:
        result = self.os.intelligence.workspace(
            "org_demo",
            "ws_alpha",
            "person_demo_owner",
            "act_alpha_admin",
            what_if={
                "capacity_hours_delta": 6,
                "work_hours_delta": 3,
                "scope_usage_delta": 2,
                "finance_amount_delta": 1000,
                "client_health_delta": -0.05,
                "deadline_days_delta": 4,
            },
        )
        self.assertEqual(result["context"]["scenario_inputs"]["retained_inputs"]["capacity_hours_delta"], 6.0)
        self.assertEqual(result["context"]["scope_contract"]["workspace"]["id"], "ws_alpha")
        self.assertIn("client", result["scope_contract"])
        self.assertIn("projects", result["scope_contract"])
        self.assertIn("campaigns", result["scope_contract"])
        self.assertIn("people", result["scope_contract"])
        scenario = next(
            item for finding in result["findings"] for item in finding["scenarios"]
            if item["name"] == "parameterized_what_if"
        )
        self.assertEqual(scenario["retained_inputs"]["work_hours_delta"], 3.0)
        self.assertIn("capacity", scenario["domain_impacts"])
        self.assertTrue(scenario["constraints"])
        self.assertTrue(scenario["mitigations"])
        plan = result["recommended_plan"]
        self.assertEqual(plan["status"], "proposed_read_only")
        self.assertGreaterEqual(len(plan["steps"]), 3)
        self.assertTrue(all("depends_on" in step and "resources" in step and "deadline" in step and "risks" in step for step in plan["steps"]))
        evaluation = result["recommendation_evaluation"]
        self.assertIn(evaluation["status"], {"pending_outcome", "outcome_backed"})
        self.assertIn("calibrated_confidence", evaluation)
        self.assertIn("calibration_delta", evaluation)

    def test_selected_operating_context_is_validated_inside_the_workspace(self) -> None:
        project = self.os.create_project("org_demo", "ws_alpha", "person_demo_owner", "Context project")
        result = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin",
            context_type="project", context_id=project.id,
        )
        self.assertEqual(result["scope_contract"]["current"]["type"], "project")
        self.assertEqual(result["scope_contract"]["current"]["id"], project.id)
        with self.assertRaises(AuthorizationError):
            self.os.intelligence.workspace(
                "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin",
                context_type="project", context_id="project_not_visible",
            )

    def test_decision_workflow_outcome_learning_chain_is_reported(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.os.store.conn.execute(
            """INSERT INTO decisions(
                id,organization_id,workspace_id,project_id,campaign_id,statement,rationale,
                decided_by_person_id,participant_person_ids,source_id,source_locator,evidence,
                created_at,effective_from,effective_until,superseded_by,tags,affected_entities
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "dec_intel_chain",
                "org_demo",
                "ws_alpha",
                None,
                None,
                "Unblock launch workflow",
                "Launch blocker needs accountable workflow follow-up.",
                "person_demo_owner",
                '["person_demo_owner"]',
                None,
                None,
                "Blocked launch work requires a decision.",
                (now - timedelta(days=5)).isoformat(),
                (now - timedelta(days=5)).isoformat(),
                None,
                None,
                "[]",
                "[]",
            ),
        )
        self.os.store.conn.execute(
            """INSERT INTO workflow_runs(
                id, organization_id, workspace_id, definition_id, definition_version_id,
                definition_key, definition_version, definition_name, template_snapshot, status,
                created_by_person_id, idempotency_key, due_at, sla_minutes, escalation_at,
                blocked_reason, created_at, updated_at, started_at, completed_at, cancelled_at, version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "wrun_intel_chain", "org_demo", "ws_alpha", "wdef", "wver",
                "launch_workflow", 1, "Launch workflow", "{}", "completed",
                "person_demo_owner", None, now.isoformat(), None, None,
                None, (now - timedelta(days=4)).isoformat(), now.isoformat(),
                (now - timedelta(days=4)).isoformat(), now.isoformat(), None, 1,
            ),
        )
        self.os.store.conn.execute(
            """INSERT INTO workflow_stage_runs(
                id, run_id, stage_key, name, sequence, status, assignee_wing, assignee_role,
                assignee_person_id, required_evidence, requires_approval, handoff_to_wing,
                handoff_to_role, handoff_to_person_id, on_reject_stage_key, due_at, blocked_reason,
                created_at, updated_at, started_at, completed_at, cancelled_at, version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "wstage_intel_chain", "wrun_intel_chain", "launch", "Unblock launch", 1,
                "completed", "Operations", "Owner", "person_demo_owner", "[]", 0,
                None, None, None, None, now.isoformat(), None,
                (now - timedelta(days=4)).isoformat(), now.isoformat(),
                (now - timedelta(days=4)).isoformat(), now.isoformat(), None, 1,
            ),
        )
        self.os.store.conn.execute(
            """INSERT INTO workflow_transition_history(
                id, run_id, stage_run_id, actor_person_id, action, from_status, to_status,
                reason, metadata, idempotency_key, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "whist_intel_chain", "wrun_intel_chain", "wstage_intel_chain",
                "person_demo_owner", "complete", "active", "completed",
                "Unblock launch workflow completed", "{}", None, now.isoformat(),
            ),
        )
        self.os.store.conn.execute(
            """INSERT INTO work_events(id,workspace_id,work_item_id,actor_id,action,from_status,to_status,detail,recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "wev_intel_chain", "ws_alpha", "work_demo_consultation_page",
                "act_alpha_admin",
                "complete", "in_progress", "shipped",
                "Unblock launch workflow shipped", now.isoformat(),
            ),
        )
        self.os.store.conn.execute(
            """INSERT INTO feedback_events(
                id, organization_id, workspace_id, pattern_id, category, raw_feedback,
                source_type, source_id, recorded_by_person_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "fb_intel_chain", "org_demo", "ws_alpha", None, "process",
                "Launch workflow unblocked the delivery path.", "manual",
                "wrun_intel_chain", "person_demo_owner", now.isoformat(),
            ),
        )
        self.os.store.conn.commit()
        result = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        link = next(
            item for item in result["decision_action_outcome_learning"]
            if item["decision"]["id"] == "dec_intel_chain"
        )
        self.assertTrue(link["workflow"])
        self.assertEqual(link["evaluation"]["status"], "validated")
        self.assertGreaterEqual(link["evaluation"]["outcome_count"], 1)
        self.assertGreaterEqual(link["evaluation"]["learning_count"], 1)
        self.assertEqual(result["recommendation_evaluation"]["status"], "outcome_backed")

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

    def test_expanded_growth_staffing_and_client_decision_scenarios_retain_inputs(self) -> None:
        result = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin",
            what_if={
                "additional_clients": 2,
                "hours_per_new_client": 18,
                "leave_hours_delta": 8,
                "hiring_hours_delta": 12,
                "client_action": "keep",
                "client_revenue_delta": 5000,
                "client_cost_delta": 1700,
                "client_hours_delta": 24,
            },
        )
        scenarios = {item["name"]: item for finding in result["findings"] for item in finding["scenarios"]}
        self.assertTrue({"growth_plus_clients", "keep_client", "drop_client"} <= set(scenarios))
        retained = scenarios["growth_plus_clients"]["retained_inputs"]
        self.assertEqual(retained["additional_clients"], 2.0)
        self.assertEqual(retained["leave_hours_delta"], 8.0)
        self.assertEqual(retained["hiring_hours_delta"], 12.0)
        self.assertEqual(scenarios["keep_client"]["retained_inputs"]["client_action"], "keep")
        self.assertIn("evidence", scenarios["growth_plus_clients"])
        self.assertIn("constraints", scenarios["drop_client"])
        self.assertEqual(result["context"]["scenario_inputs"]["projection"]["added_client_hours"], 36.0)
        keep_finance = scenarios["keep_client"]["domain_impacts"]["finance"]
        drop_finance = scenarios["drop_client"]["domain_impacts"]["finance"]
        self.assertNotEqual(keep_finance, drop_finance)
        self.assertIn("margin delta 3300.0", keep_finance)
        self.assertIn("margin delta -3300.0", drop_finance)

    def test_portfolio_analogues_are_cross_workspace_acl_scoped_and_executive_top_three_is_narrative(self) -> None:
        visible = self.os.create_organization_workspace("org_demo", "Visible client", "client", "ws_visible_analogue")
        self.os.add_person_to_workspace("org_demo", visible.id, "person_demo_owner", "admin")
        self.os.create_actor(visible.id, "Visible actor", "admin", "act_visible_analogue")
        risk = self.os.client_ops.create_risk(
            "org_demo", visible.id, "person_demo_owner", "delivery", "high", 0.8,
            "Launch blocker", "launch blocker repeated", "Resolve launch blocker",
        )
        self.os.store.conn.execute(
            "UPDATE risks SET status='resolved',resolved_at=?,resolution=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), "Resolved through owner review", risk.id),
        )
        hidden = self.os.create_organization_workspace("org_demo", "Hidden client", "client", "ws_hidden_analogue")
        self.os.store.conn.execute(
            "INSERT INTO risks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "risk_hidden_analogue", "org_demo", hidden.id, None, "delivery", "critical", 0.95,
                "launch blocker hidden", "person_demo_owner", datetime.now(timezone.utc).isoformat(),
                "resolved", "launch blocker repeated hidden", "hidden", "2026-08-20T00:00:00+00:00", "Hidden",
            ),
        )
        self.os.store.conn.commit()
        portfolio = self.os.intelligence.portfolio("org_demo", "person_demo_owner")
        analogues = portfolio["portfolio"]["historical_analogues"]
        self.assertTrue(any(item["source"]["workspace_id"] == visible.id for item in analogues))
        self.assertTrue(all(item["source"]["workspace_id"] != hidden.id for item in analogues))
        self.assertTrue(all("outcome_stats" in item and "resolution_rate" in item["outcome_stats"] for item in analogues))
        brief = self.os.intelligence.executive_brief("org_demo", "person_demo_owner")
        self.assertIn("top_three", brief["sections"])
        self.assertIn("narrative", brief["sections"])
        self.assertLessEqual(len(brief["sections"]["top_three"]), 3)


if __name__ == "__main__":
    unittest.main()
