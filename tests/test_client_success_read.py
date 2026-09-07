"""Tests for ClientSuccessReadService."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.client_success_read import ClientSuccessReadService


class ClientSuccessReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org_a = self.os.create_organization("Prime Agency", "org_prime")
        self.org_b = self.os.create_organization("Rival Agency", "org_rival")

        self.owner_a = self.os.create_person(self.org_a.id, "Alice Owner", role="owner", person_id="person_owner_a")
        self.dri_a = self.os.create_person(self.org_a.id, "Bob DRI", role="member", person_id="person_dri_a")
        self.owner_b = self.os.create_person(self.org_b.id, "Mallory Owner", role="owner", person_id="person_owner_b")

        self.client_1 = self.os.create_organization_workspace(self.org_a.id, "Lumina Media", "client", "ws_lumina")
        self.client_2 = self.os.create_organization_workspace(self.org_a.id, "Apex Tech", "client", "ws_apex")
        self.client_b = self.os.create_organization_workspace(self.org_b.id, "Rival Brand", "client", "ws_rival")

        for p in (self.owner_a, self.dri_a):
            self.os.add_person_to_workspace(self.org_a.id, self.client_1.id, p.id, "admin")
            self.os.add_person_to_workspace(self.org_a.id, self.client_2.id, p.id, "admin")
        self.os.add_person_to_workspace(self.org_b.id, self.client_b.id, self.owner_b.id, "admin")

        self.service = ClientSuccessReadService(self.os)

    def tearDown(self) -> None:
        self.os.close()

    def test_get_client_health_snapshot_complete(self) -> None:
        self.os.client_ops.create_client_roster(
            self.org_a.id, self.client_1.id, self.owner_a.id,
            [
                {"role_key": "client_success_dri", "person_id": self.dri_a.id},
                {"role_key": "client_success_backup", "person_id": self.owner_a.id},
            ],
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        self.os.client_ops.create_contract(
            self.org_a.id, self.client_1.id, self.owner_a.id, "retainer", "monthly", "2026-01-01"
        )
        self.os.store.conn.execute(
            """UPDATE contracts SET renewal_date='2026-12-31', status='active'
               WHERE organization_id=? AND workspace_id=?""",
            (self.org_a.id, self.client_1.id),
        )
        self.os.store.conn.commit()

        self.os.client_ops.create_risk(
            self.org_a.id, self.client_1.id, self.owner_a.id,
            "churn", "high", 0.7, "Contract renewal at risk due to creative delays",
            "Urgent executive check-in", "Delayed sprint deliverables",
        )
        self.os.client_ops.create_risk(
            self.org_a.id, self.client_1.id, self.owner_a.id,
            "delivery", "medium", 0.4, "Minor delay on TikTok cutdowns",
            "Follow up with video editor", "Waiting on footage",
        )

        self.os.client_ops.create_meeting(
            self.org_a.id, self.client_1.id, self.owner_a.id,
            "Bi-Weekly Progress Sync", datetime(2026, 9, 5, tzinfo=timezone.utc),
            summary="Review Q3 campaign progress and asset deliverables.",
        )

        scope = {
            "organization_id": self.org_a.id,
            "workspace_id": self.client_1.id,
            "person_id": self.owner_a.id,
        }
        snapshot = self.service.get_client_health_snapshot(scope, self.client_1.id)

        self.assertEqual(snapshot["organization_id"], self.org_a.id)
        self.assertEqual(snapshot["client_id"], self.client_1.id)
        self.assertEqual(snapshot["client_name"], "Lumina Media")

        account = snapshot["account"]
        self.assertEqual(account["workspace_id"], self.client_1.id)
        self.assertIsNotNone(account["roster"])

        risks = snapshot["open_risks"]
        self.assertEqual(risks["count"], 2)
        self.assertEqual(risks["critical_or_high_count"], 1)
        self.assertEqual(risks["items"][0]["severity"], "high")

        renewals = snapshot["renewal_dates"]
        self.assertEqual(renewals["active_contracts_count"], 1)
        self.assertEqual(renewals["nearest_renewal_date"], "2026-12-31")
        self.assertIsNotNone(renewals["days_until_renewal"])

        interactions = snapshot["recent_interactions"]
        self.assertEqual(interactions["recent_meetings_count"], 1)
        self.assertEqual(interactions["recent_meetings"][0]["title"], "Bi-Weekly Progress Sync")

        rollup = snapshot["status_rollup"]
        self.assertEqual(rollup["overall_status"], "at_risk")
        self.assertEqual(rollup["open_risks_count"], 2)
        self.assertEqual(rollup["critical_risks_count"], 1)
        self.assertEqual(rollup["nearest_renewal_date"], "2026-12-31")

    def test_list_client_health_snapshots(self) -> None:
        scope = {
            "organization_id": self.org_a.id,
            "person_id": self.owner_a.id,
        }
        summaries = self.service.list_client_health_snapshots(scope)

        self.assertEqual(len(summaries), 2)
        client_ids = {s["client_id"] for s in summaries}
        self.assertEqual(client_ids, {self.client_1.id, self.client_2.id})
        for s in summaries:
            self.assertEqual(s["organization_id"], self.org_a.id)
            self.assertIn("overall_status", s)
            self.assertIn("health_score", s)

    def test_cross_organization_and_cross_workspace_access_denied(self) -> None:
        scope_rival = {
            "organization_id": self.org_b.id,
            "workspace_id": self.client_b.id,
            "person_id": self.owner_b.id,
        }
        with self.assertRaises(AuthorizationError):
            self.service.get_client_health_snapshot(scope_rival, self.client_1.id)

        scope_mismatch = {
            "organization_id": self.org_a.id,
            "workspace_id": self.client_2.id,
            "person_id": self.owner_a.id,
        }
        with self.assertRaises(AuthorizationError):
            self.service.get_client_health_snapshot(scope_mismatch, self.client_1.id)

        scope_unknown = {
            "organization_id": self.org_a.id,
            "workspace_id": "ws_nonexistent",
            "person_id": self.owner_a.id,
        }
        with self.assertRaises((AuthorizationError, NotFoundError)):
            self.service.get_client_health_snapshot(scope_unknown, "ws_nonexistent")

        with self.assertRaises(ValidationError):
            self.service.get_client_health_snapshot({}, self.client_1.id)

    def test_empty_projections_for_new_client(self) -> None:
        scope = {
            "organization_id": self.org_a.id,
            "workspace_id": self.client_2.id,
            "person_id": self.owner_a.id,
        }
        snapshot = self.service.get_client_health_snapshot(scope, self.client_2.id)

        self.assertEqual(snapshot["client_name"], "Apex Tech")
        self.assertEqual(snapshot["open_risks"]["count"], 0)
        self.assertEqual(snapshot["open_risks"]["critical_or_high_count"], 0)
        self.assertEqual(snapshot["open_risks"]["items"], [])
        self.assertEqual(snapshot["renewal_dates"]["contracts_count"], 0)
        self.assertEqual(snapshot["recent_interactions"]["recent_meetings_count"], 0)
        self.assertEqual(snapshot["status_rollup"]["overall_status"], "healthy")
        self.assertEqual(snapshot["status_rollup"]["health_score"], 1.0)


if __name__ == "__main__":
    unittest.main()
