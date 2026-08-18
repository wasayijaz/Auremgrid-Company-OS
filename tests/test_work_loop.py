from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class WorkLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)

    def tearDown(self) -> None:
        self.os.close()

    def test_intake_requires_the_front_door(self) -> None:
        with self.assertRaises(ValidationError):
            self.os.capture_work("ws_alpha", "act_alpha_admin", "", "need a page", "Channel Lead")

    def test_work_cannot_skip_definition_of_done(self) -> None:
        item = self.os.capture_work(
            "ws_alpha",
            "act_alpha_admin",
            "New ad",
            "Need a retargeting ad",
            "Channel Lead",
        )
        item = self.os.assign_work("ws_alpha", "act_alpha_admin", item.id, "act_alpha_operator")
        item = self.os.start_work("ws_alpha", "act_alpha_operator", item.id)
        with self.assertRaises(ValidationError):
            self.os.submit_review("ws_alpha", "act_alpha_operator", item.id)

    def test_account_brief_combines_brain_work_and_touchpoint(self) -> None:
        brief = self.os.account_brief("ws_alpha", "act_alpha_operator", query="consultation price")
        payload = brief.to_dict()
        self.assertIsNotNone(payload["brain"])
        self.assertTrue(payload["playbooks"])
        self.assertTrue(payload["open_work"])
        self.assertEqual(payload["open_work"][0]["title"], "Retargeting ad set")
        self.assertIsNotNone(payload["latest_touchpoint"])
        self.assertGreaterEqual(payload["days_since_touchpoint"], 0)
        self.assertFalse(payload["evidence"]["unknown"])

    def test_touchpoint_silence_is_visible(self) -> None:
        self.os.record_touchpoint(
            "ws_beta",
            "act_beta_admin",
            "Old check-in",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        brief = self.os.account_brief("ws_beta", "act_beta_admin")
        self.assertGreaterEqual(brief.days_since_touchpoint or 0, 30)

    def test_review_must_be_closed_before_ship(self) -> None:
        item = self.os.capture_work(
            "ws_beta",
            "act_beta_admin",
            "Intro week page",
            "Refresh the intro-week landing page",
            "Studio lead",
        )
        item = self.os.assign_work("ws_beta", "act_beta_admin", item.id, "act_beta_admin")
        item = self.os.start_work("ws_beta", "act_beta_admin", item.id)
        self.os.mark_dod(
            "ws_beta",
            "act_beta_admin",
            item.id,
            {
                "mobile_responsive": True,
                "assets_exported": True,
                "creative_safe_zone": True,
                "copy_spellchecked": True,
                "handoff_notes": True,
            },
        )
        item = self.os.submit_review("ws_beta", "act_beta_admin", item.id)
        with self.assertRaises(ValidationError):
            self.os.ship_work("ws_beta", "act_beta_admin", item.id)
        item = self.os.close_review("ws_beta", "act_beta_admin", item.id, approved=True)
        shipped = self.os.ship_work("ws_beta", "act_beta_admin", item.id)
        self.assertEqual(shipped.status, "shipped")


if __name__ == "__main__":
    unittest.main()
