from __future__ import annotations

import unittest
from pathlib import Path

from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ProactiveAttentionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        _, self.identity = issue_identity(self.os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin")

    def tearDown(self): self.os.close()

    def test_lifecycle_dedupe_and_resurface(self):
        first = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner", "workspace", "ws_alpha", actor_id="act_alpha_admin")
        rows = self.os.proactive_intelligence.attention_lifecycle(self.identity, "ws_alpha")
        self.assertTrue(rows)
        fp = rows[0]["fingerprint"]
        self.os.proactive_intelligence.update_attention_status(self.identity, fp, "resolved", "done")
        second = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner", "workspace", "ws_alpha", actor_id="act_alpha_admin", as_of=None)
        rows2 = self.os.proactive_intelligence.attention_lifecycle(self.identity, "ws_alpha")
        self.assertEqual(len(rows2), len(rows))

    def test_workspace_isolation(self):
        rows = self.os.proactive_intelligence.attention_lifecycle(self.identity, "ws_alpha")
        self.assertTrue(all(row["workspace_id"] == "ws_alpha" for row in rows))

    def test_rejected_status_is_not_executable(self):
        first = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner", "workspace", "ws_alpha", actor_id="act_alpha_admin")
        rows = self.os.proactive_intelligence.attention_lifecycle(self.identity, "ws_alpha")
        if rows:
            item = self.os.proactive_intelligence.update_attention_status(self.identity, rows[0]["fingerprint"], "dismissed", "rejected")
            self.assertEqual(item["status"], "dismissed")
            self.assertEqual(item["approval_request_id"], None)

    def test_action_requires_approved_current_scope(self):
        self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner", "workspace", "ws_alpha", actor_id="act_alpha_admin")
        row = self.os.proactive_intelligence.attention_lifecycle(self.identity, "ws_alpha")[0]
        approval = self.os.agency_ops.request_approval(
            "org_demo", "person", "person_demo_owner", "Lifecycle action", "intelligence.review", {}, "review", "human", "ws_alpha", "person_demo_owner"
        )
        with self.assertRaises(Exception):
            self.os.proactive_intelligence.mark_action_acted_on(self.identity, row["fingerprint"], approval["id"])
        self.os.agency_ops.decide_approval("org_demo", "person_demo_owner", approval["id"], True)
        acted = self.os.proactive_intelligence.mark_action_acted_on(self.identity, row["fingerprint"], approval["id"])
        self.assertEqual(acted["status"], "acted_on")


if __name__ == "__main__": unittest.main()
