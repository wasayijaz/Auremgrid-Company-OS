from __future__ import annotations

import sqlite3
import unittest

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS


class FinanceCampaignCreativeCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.reviewer = self.os.create_person(self.org.id, "Reviewer")
        self.operator = self.os.create_person(self.org.id, "Operator")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.reviewer.id, "operator")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.operator.id, "operator")

    def tearDown(self) -> None:
        self.os.close()

    def test_client_economics_uses_only_connected_sourced_records(self) -> None:
        with self.assertRaises(ValidationError):
            self.os.agency_ops.record_cost(
                self.org.id, self.ws.id, self.owner.id, 100, "labor", "2026-08-10", "manual"
            )
        self.os.agency_ops.connect_finance(self.org.id, self.owner.id, "accounting-test")
        self.os.agency_ops.record_revenue(
            self.org.id, self.ws.id, self.owner.id, 5000, "2026-08-01", "accounting-test"
        )
        self.os.agency_ops.record_cost(
            self.org.id, self.ws.id, self.owner.id, 1500, "labor", "2026-08-10", "timesheets"
        )
        self.os.agency_ops.record_cost(
            self.org.id, self.ws.id, self.owner.id, 200, "contractor", "2026-08-11", "ledger"
        )
        self.os.agency_ops.record_software_cost(
            self.org.id, self.ws.id, self.owner.id, "Design Suite", 300, "2026-08-01", "ledger"
        )
        self.os.agency_ops.record_ai_usage_cost(
            self.org.id, self.ws.id, self.owner.id, "provider", "model", 10000, 100,
            "2026-08-12", "usage-ledger",
        )
        self.os.agency_ops.record_budget(
            self.org.id, self.ws.id, self.owner.id, 6000, "2026-08-01", "2026-08-31"
        )

        economics = self.os.agency_ops.calculate_client_economics(
            self.org.id, self.ws.id, self.owner.id, "2026-08-01", "2026-08-31"
        )
        self.assertEqual(economics["revenue"], 5000)
        self.assertEqual(economics["labor_cost"], 1500)
        self.assertEqual(economics["software_cost"], 300)
        self.assertEqual(economics["ai_cost"], 100)
        self.assertEqual(economics["other_cost"], 200)
        self.assertEqual(economics["gross_contribution"], 2900)
        self.assertEqual(economics["margin"], 0.58)
        status = self.os.agency_ops.finance_status(self.org.id, self.owner.id, self.ws.id)
        self.assertEqual(status["budget"], 6000)
        self.assertEqual(status["latest_economics"]["id"], economics["id"])

    def test_campaign_and_creative_lifecycles_are_gated_and_auditable(self) -> None:
        campaign = self.os.agency_ops.create_campaign(
            self.org.id, self.ws.id, self.owner.id, "Launch", "Leads", "meta"
        )
        with self.assertRaises(ValidationError):
            self.os.agency_ops.transition_campaign(
                self.org.id, self.ws.id, self.owner.id, campaign["id"], "completed"
            )
        for status in ("scheduled", "active", "paused", "active", "completed"):
            campaign = self.os.agency_ops.transition_campaign(
                self.org.id, self.ws.id, self.owner.id, campaign["id"], status, f"Move to {status}"
            )
        detail = self.os.agency_ops.campaign_detail(
            self.org.id, self.ws.id, self.owner.id, campaign["id"]
        )
        self.assertEqual(campaign["status"], "completed")
        self.assertEqual(len(detail["events"]), 5)
        self.assertEqual(detail["allowed_transitions"], [])

        asset = self.os.agency_ops.create_creative(
            self.org.id, self.ws.id, self.operator.id, "Launch ad", "image", campaign_id=campaign["id"]
        )
        version = self.os.agency_ops.create_creative_version(
            self.org.id, self.ws.id, self.operator.id, asset["id"], "https://example.test/v1.png", "First cut"
        )
        self.assertEqual(version["version"], 1)
        asset = self.os.agency_ops.transition_creative(
            self.org.id, self.ws.id, self.operator.id, asset["id"], "in_review", "Ready",
            self.reviewer.id,
        )
        with self.assertRaises(AuthorizationError):
            self.os.agency_ops.transition_creative(
                self.org.id, self.ws.id, self.operator.id, asset["id"], "approved", "Looks good"
            )
        asset = self.os.agency_ops.transition_creative(
            self.org.id, self.ws.id, self.reviewer.id, asset["id"], "changes_requested", "Tighten copy"
        )
        asset = self.os.agency_ops.transition_creative(
            self.org.id, self.ws.id, self.operator.id, asset["id"], "draft", "Revising"
        )
        self.os.agency_ops.create_creative_version(
            self.org.id, self.ws.id, self.operator.id, asset["id"], "https://example.test/v2.png", "Copy revised"
        )
        self.os.agency_ops.transition_creative(
            self.org.id, self.ws.id, self.operator.id, asset["id"], "in_review", "Ready again",
            self.reviewer.id,
        )
        approved = self.os.agency_ops.transition_creative(
            self.org.id, self.ws.id, self.reviewer.id, asset["id"], "approved", "Approved"
        )
        detail = self.os.agency_ops.creative_detail(
            self.org.id, self.ws.id, self.owner.id, asset["id"]
        )
        self.assertEqual(approved["approval_state"], "approved")
        self.assertEqual(approved["final_url"], "https://example.test/v2.png")
        self.assertEqual([item["version"] for item in detail["versions"]], [1, 2])
        self.assertEqual(len(detail["events"]), 5)
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute(
                "UPDATE creative_review_events SET note='rewritten' WHERE asset_id=?", (asset["id"],)
            )


if __name__ == "__main__":
    unittest.main()
