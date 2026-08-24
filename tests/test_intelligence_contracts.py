from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.intelligence_contracts import ExpertResult
from auremgrid.services.brain import CompanyOS
from auremgrid.services.intelligence_contracts import DEFAULT_EXPERT_PROFILES, DEFAULT_INTELLIGENCE_RUNBOOKS
from tests.auth_support import LATEST_SCHEMA_VERSION, issue_identity


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class IntelligenceContractTests(unittest.TestCase):
    def test_schema_42_seeds_native_profiles_and_runbooks_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contracts.sqlite"
            first = CompanyOS(path)
            try:
                self.assertEqual(first.store.schema_version, LATEST_SCHEMA_VERSION)
                self.assertEqual(
                    first.store.conn.execute("SELECT COUNT(*) FROM expert_profiles").fetchone()[0],
                    13,
                )
                self.assertEqual(
                    first.store.conn.execute("SELECT COUNT(*) FROM intelligence_runbooks").fetchone()[0],
                    12,
                )
                profile_hashes = {
                    row["id"]: row["content_hash"]
                    for row in first.store.conn.execute("SELECT id,content_hash FROM expert_profiles")
                }
            finally:
                first.close()

            second = CompanyOS(path)
            try:
                self.assertEqual(
                    second.store.conn.execute("SELECT COUNT(*) FROM expert_profiles").fetchone()[0],
                    13,
                )
                self.assertEqual(
                    second.store.conn.execute("SELECT COUNT(*) FROM intelligence_runbooks").fetchone()[0],
                    12,
                )
                profile_columns = {
                    row["name"] for row in second.store.conn.execute("PRAGMA table_info(expert_profiles)").fetchall()
                }
                self.assertTrue({
                    "specialty", "mission", "required_inputs_json", "allowed_domains_json",
                    "allowed_tools_json", "required_evidence_json", "reasoning_method",
                    "output_schema_json", "evaluation_criteria_json", "escalation_policy",
                    "fallback_policy", "max_context", "max_iterations", "capability_level",
                } <= profile_columns)
                runbook_columns = {
                    row["name"] for row in second.store.conn.execute("PRAGMA table_info(intelligence_runbooks)").fetchall()
                }
                self.assertTrue({
                    "trigger", "required_domains_json", "required_evidence_json",
                    "specialists_json", "topology", "stages_json", "quality_gates_json",
                    "contradiction_policy", "scenario_policy", "escalation_policy",
                    "max_iterations", "output_contract_json",
                } <= runbook_columns)
                reopened_hashes = {
                    row["id"]: row["content_hash"]
                    for row in second.store.conn.execute("SELECT id,content_hash FROM expert_profiles")
                }
                self.assertEqual(reopened_hashes, profile_hashes)
            finally:
                second.close()

    def test_native_pack_names_and_definition_audit_ledger(self) -> None:
        os = CompanyOS(":memory:")
        try:
            profile_names = {
                row["name"] for row in os.store.conn.execute("SELECT name FROM expert_profiles WHERE status='active'")
            }
            self.assertEqual(profile_names, {
                "Account Strategist", "Relationship Analyst", "Delivery Analyst", "Performance Analyst",
                "Finance & Scope Analyst", "Capacity Planner", "Brand / Creative Analyst", "Research Analyst",
                "Risk Analyst", "Scenario Analyst", "Historical Analogue Analyst", "Reality Checker",
                "Executive Synthesizer",
            })
            runbook_ids = {
                row["id"] for row in os.store.conn.execute("SELECT id FROM intelligence_runbooks WHERE status='active'")
            }
            self.assertEqual(runbook_ids, {
                "client_health_drop", "client_churn_risk", "renewal_review", "scope_overrun", "margin_pressure",
                "project_delay", "campaign_performance_drop", "creative_fatigue", "client_relationship_problem",
                "team_overload", "account_expansion_opportunity", "quarterly_account_review",
            })
            audit = {
                row["entity_type"]: row["count"]
                for row in os.store.conn.execute(
                    """SELECT entity_type, COUNT(*) AS count
                       FROM ledger_audit
                       WHERE principal_type='system' AND principal_id='intelligence_contracts'
                         AND action='create'
                       GROUP BY entity_type"""
                )
            }
            self.assertEqual(audit, {"expert_profile": 13, "intelligence_runbook": 12})
            self.assertNotIn("cosmo_", json.dumps({"profiles": sorted(profile_names), "runbooks": sorted(runbook_ids)}).lower())
        finally:
            os.close()

    def test_definitions_are_immutable_versioned_contracts(self) -> None:
        os = CompanyOS(":memory:")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                os.store.conn.execute(
                    "UPDATE expert_profiles SET summary='changed' WHERE id='account_strategist'"
                )
            os.store.conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                os.store.conn.execute(
                    "DELETE FROM intelligence_runbooks WHERE id='client_health_drop'"
                )
            os.store.conn.rollback()
        finally:
            os.close()

    def test_acl_scoped_facade_filters_tools_by_capability(self) -> None:
        os = CompanyOS(":memory:")
        try:
            os.seed_demo(FIXTURES)
            owner_token, owner_identity = issue_identity(
                os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin"
            )
            self.assertTrue(owner_token)
            owner_profiles = os.intelligence_contracts.list_profiles_for_identity(owner_identity, "ws_alpha")
            self.assertEqual(len(owner_profiles), 13)
            finance = next(item for item in owner_profiles if item["id"] == "finance_scope_analyst")
            for field in (
                "specialty", "mission", "required_inputs", "allowed_domains",
                "allowed_tools", "required_evidence", "reasoning_method",
                "output_schema", "evaluation_criteria", "escalation_policy",
                "fallback_policy", "max_context", "max_iterations", "capability_level",
            ):
                self.assertIn(field, finance)
            self.assertIn("finance.read", finance["allowed_tool_refs"])
            self.assertIn("finance.read", finance["allowed_tools"])
            self.assertIn("reports.generate", finance["allowed_tool_refs"])

            viewer = os.create_person("org_demo", "Viewer", "viewer@demo.invalid", role="member", person_id="person_contract_viewer")
            os.add_person_to_workspace("org_demo", "ws_alpha", viewer.id, "viewer")
            _viewer_token, viewer_identity = issue_identity(os, "org_demo", viewer.id, "ws_alpha")
            viewer_profiles = os.intelligence_contracts.list_profiles_for_identity(viewer_identity, "ws_alpha")
            viewer_finance = next(item for item in viewer_profiles if item["id"] == "finance_scope_analyst")
            self.assertNotIn("finance.read", viewer_finance["allowed_tool_refs"])
            self.assertNotIn("finance.read", viewer_finance["allowed_tools"])
            self.assertNotIn("reports.generate", viewer_finance["allowed_tool_refs"])

            outsider = os.create_person("org_demo", "Outsider", "outsider@demo.invalid", role="member", person_id="person_contract_outsider")
            with self.assertRaises(AuthorizationError):
                os.intelligence_contracts.list_profiles("org_demo", "ws_alpha", outsider.id)
        finally:
            os.close()

    def test_pack_is_compact_native_and_not_generic_agent_catalog(self) -> None:
        os = CompanyOS(":memory:")
        try:
            raw_profiles = [
                dict(row)
                for row in os.store.conn.execute(
                    "SELECT * FROM expert_profiles ORDER BY name"
                ).fetchall()
            ]
            raw_runbooks = [
                dict(row)
                for row in os.store.conn.execute(
                    "SELECT * FROM intelligence_runbooks ORDER BY name"
                ).fetchall()
            ]
            payload = json.dumps({"profiles": raw_profiles, "runbooks": raw_runbooks}).lower()
            self.assertNotIn("agent_roles", payload)
            self.assertNotIn("default_write_permissions", payload)
            self.assertNotIn("freeform_prompt", payload)
            self.assertNotIn("system_prompt", payload)
            self.assertNotIn("full_company_context", payload)
            self.assertEqual(
                {item["id"] for item in DEFAULT_EXPERT_PROFILES},
                {row["id"] for row in raw_profiles},
            )
            self.assertEqual(
                {item["id"] for item in DEFAULT_INTELLIGENCE_RUNBOOKS},
                {row["id"] for row in raw_runbooks},
            )
            stored_steps = json.loads(raw_runbooks[0]["steps_json"])
            stored_stages = json.loads(raw_runbooks[0]["stages_json"])
            output_contract = json.loads(raw_runbooks[0]["output_contract_json"])
            self.assertTrue(stored_steps)
            self.assertTrue(stored_stages)
            self.assertIn("required", output_contract)
            self.assertIn("gate", stored_steps[0])
            self.assertIn("historical_analogues", ExpertResult(status="available", scope={}).to_dict())
        finally:
            os.close()


if __name__ == "__main__":
    unittest.main()
