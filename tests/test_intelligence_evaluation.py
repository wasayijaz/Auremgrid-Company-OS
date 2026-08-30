from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from auremgrid.cli import main
from auremgrid.services.intelligence_evaluation import run_intelligence_evaluations


class IntelligenceEvaluationTests(unittest.TestCase):
    def test_builtin_contract_scenarios_pass(self):
        result = run_intelligence_evaluations()
        self.assertTrue(result["passed"])
        self.assertEqual(result["summary"]["passed"], result["summary"]["total"])
        self.assertGreaterEqual(result["summary"]["total"], 7)

    def test_v1_goldens_assert_semantic_evidence_contracts(self):
        result = run_intelligence_evaluations()
        checks = {item["name"]: item for item in result["checks"]}
        for name in (
            "evidence_citations", "acl_evidence_scope", "stale_evidence_degraded",
            "decision_outcome_learning_chain", "assumptions_visible",
            "orchestrator_profile_fanout", "runbook_evaluation_no_canonical_writes",
        ):
            self.assertIn(name, checks)
            self.assertTrue(checks[name]["passed"], checks[name]["detail"])
        self.assertGreaterEqual(sum(name.startswith("runbook_") for name in checks), 12)

    def test_cli_returns_json_and_success(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["evaluate-intelligence"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["suite"], "auremgrid_intelligence_contract")


if __name__ == "__main__":
    unittest.main()
