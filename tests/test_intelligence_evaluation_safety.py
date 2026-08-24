from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from pathlib import Path

from auremgrid.services.brain import CompanyOS


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class IntelligenceEvaluationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        self.safety = self.os.intelligence_evaluation_safety

    def tearDown(self):
        self.os.close()

    def test_shadow_only_and_cost_cap(self):
        run = self.safety.start("org_demo", "person_demo_owner", "reasoning", workspace_id="ws_alpha", provider="x", model="m")
        self.assertEqual(run["shadow_only"], 1)
        done = self.safety.complete("org_demo", "person_demo_owner", run["id"], cost_amount=99, input_tokens=1, output_tokens=1)
        self.assertEqual(done["status"], "capped")
        self.assertEqual(done["cap_reason"], "cost_cap")

    def test_breaker_persists_and_blocks_after_threshold(self):
        for _ in range(3):
            run = self.safety.start("org_demo", "person_demo_owner", "breaker", workspace_id="ws_alpha")
            self.safety.complete("org_demo", "person_demo_owner", run["id"], cost_amount=99)
        decision = self.safety.can_start("org_demo", "person_demo_owner", "breaker")
        self.assertFalse(decision["allowed"])
        with self.assertRaises(Exception):
            self.safety.start("org_demo", "person_demo_owner", "breaker", workspace_id="ws_alpha")

    def test_evaluation_does_not_change_agent_routing(self):
        before = self.os.agent_ops.resolve_level(["strategize"]).value
        run = self.safety.start("org_demo", "person_demo_owner", "routing", workspace_id="ws_alpha")
        self.safety.complete("org_demo", "person_demo_owner", run["id"], evaluator_score=.2)
        after = self.os.agent_ops.resolve_level(["strategize"]).value
        self.assertEqual(before, after)

    def test_breaker_state_survives_restart(self):
        self.os.close()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "restart.sqlite")
            first = CompanyOS(path)
            first.seed_demo(FIXTURES)
            safety = first.intelligence_evaluation_safety
            safety.configure_policy("org_demo", "person_demo_owner", "restart", breaker_threshold=1, breaker_open_seconds=300)
            run = safety.start("org_demo", "person_demo_owner", "restart", workspace_id="ws_alpha")
            safety.complete("org_demo", "person_demo_owner", run["id"], cost_amount=99)
            first.close()
            second = CompanyOS(path)
            decision = second.intelligence_evaluation_safety.can_start("org_demo", "person_demo_owner", "restart")
            self.assertFalse(decision["allowed"])
            second.close()

    def test_complete_rejects_wrong_workspace_and_duplicate_completion(self):
        run = self.safety.start("org_demo", "person_demo_owner", "scope", workspace_id="ws_alpha")
        with self.assertRaises(Exception):
            self.safety.complete("org_demo", "person_demo_owner", run["id"], workspace_id="ws_beta")
        self.safety.complete("org_demo", "person_demo_owner", run["id"], workspace_id="ws_alpha")
        with self.assertRaises(Exception):
            self.safety.complete("org_demo", "person_demo_owner", run["id"], workspace_id="ws_alpha")

    def test_breaker_counts_only_recent_events(self):
        self.safety.configure_policy("org_demo", "person_demo_owner", "rolling", breaker_threshold=2, breaker_window_seconds=1)
        run = self.safety.start("org_demo", "person_demo_owner", "rolling", workspace_id="ws_alpha")
        self.safety.complete("org_demo", "person_demo_owner", run["id"], workspace_id="ws_alpha", cost_amount=99)
        row = self.os.store.conn.execute("SELECT * FROM intelligence_evaluation_policies WHERE organization_id=? AND task_class=?", ("org_demo", "rolling")).fetchone()
        self.assertEqual(row["failure_count"], 1)


if __name__ == "__main__":
    unittest.main()
