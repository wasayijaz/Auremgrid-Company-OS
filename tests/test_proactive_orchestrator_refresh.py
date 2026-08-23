from __future__ import annotations

import unittest
from pathlib import Path

from auremgrid.services.brain import CompanyOS
from auremgrid.services.worker import run_one_job
from tests.auth_support import issue_identity


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ProactiveOrchestratorRefreshTests(unittest.TestCase):
    def setUp(self):
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        _, self.identity = issue_identity(self.os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin")

    def tearDown(self): self.os.close()

    def test_selected_runbook_links_trace_and_degraded_no_match(self):
        runbook = self.os.intelligence_contracts.list_runbooks("org_demo", "ws_alpha", "person_demo_owner")[0]
        snapshot = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner", "workspace", "ws_alpha", actor_id="act_alpha_admin", runbook_id=runbook["id"])
        self.assertIn("orchestration", snapshot["payload"])
        self.assertTrue(snapshot["payload"]["trace_id"])
        lifecycle = self.os.proactive_intelligence.attention_lifecycle(self.identity, "ws_alpha")
        self.assertTrue(any(item["trace_id"] for item in lifecycle))
        no_match = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner", "workspace", "ws_alpha", actor_id="act_alpha_admin", runbook_id="missing-runbook")
        self.assertEqual(no_match["payload"]["orchestration"]["status"], "degraded")

    def test_worker_restart_path_preserves_optional_refresh(self):
        job = self.os.proactive_intelligence.enqueue_refresh(self.identity, "workspace", "ws_alpha", runbook_id="missing-runbook")
        result = run_one_job(self.os, "org_demo", "ws_alpha", "worker-test")
        self.assertEqual(result["status"], "succeeded")

    def test_hidden_workspace_is_not_orchestrated(self):
        self.os.create_organization_workspace("org_demo", "Hidden", "client", "ws_hidden_final")
        self.assertEqual(self.os.proactive_intelligence.attention_lifecycle(self.identity, "ws_alpha"), [])


if __name__ == "__main__": unittest.main()
