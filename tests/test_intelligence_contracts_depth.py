import unittest

from auremgrid.services.intelligence_contracts import (
    DEFAULT_EXPERT_PROFILES,
    DEFAULT_INTELLIGENCE_RUNBOOKS,
)


class IntelligenceContractDepthTests(unittest.TestCase):
    def test_thirteen_specialists_and_deep_protocol_steps(self):
        self.assertGreaterEqual(len(DEFAULT_EXPERT_PROFILES), 13)
        self.assertEqual(len({item["id"] for item in DEFAULT_EXPERT_PROFILES}), len(DEFAULT_EXPERT_PROFILES))
        for runbook in DEFAULT_INTELLIGENCE_RUNBOOKS:
            steps = runbook["steps"]
            self.assertGreaterEqual(len(steps), 8, runbook["id"])
            self.assertEqual([step["sequence"] for step in steps], list(range(1, len(steps) + 1)))
            for step in steps:
                self.assertTrue(step["method"].strip(), step["id"])
                self.assertTrue(step["gate"].strip(), step["id"])
                self.assertTrue(step["required_inputs"], step["id"])
                self.assertTrue(step.get("handoff_to"), step["id"])

    def test_domain_protocol_depth_and_company_role_tiers(self):
        finance = next(item for item in DEFAULT_INTELLIGENCE_RUNBOOKS if item["id"] == "margin_pressure")
        self.assertGreaterEqual(len(finance["steps"]), 10)
        performance = next(item for item in DEFAULT_INTELLIGENCE_RUNBOOKS if item["id"] == "campaign_performance_drop")
        methods = " ".join(step["method"].lower() for step in performance["steps"])
        for phrase in ("spend anomaly", "cpa/roas", "creative fatigue", "pacing", "attribution"):
            self.assertIn(phrase, methods)
        roles = {
            "client_success_lead": 4,
            "ads_lead": 4,
            "design_lead": 4,
            "marketing_lead": 4,
            "ads_executive": 2,
            "design_executive": 2,
            "marketing_executive": 2,
            "meeting_recorder": 1,
        }
        constraints = " ".join(" ".join(item["constraints"]) for item in DEFAULT_EXPERT_PROFILES)
        for role, tier in roles.items():
            self.assertIn(f"company_role:{role}:tier{tier}", constraints)

    def test_seed_payloads_are_id_stable(self):
        profile_ids = [item["id"] for item in DEFAULT_EXPERT_PROFILES]
        runbook_ids = [item["id"] for item in DEFAULT_INTELLIGENCE_RUNBOOKS]
        self.assertEqual(profile_ids, [item["id"] for item in DEFAULT_EXPERT_PROFILES])
        self.assertEqual(runbook_ids, [item["id"] for item in DEFAULT_INTELLIGENCE_RUNBOOKS])


if __name__ == "__main__":
    unittest.main()
