import unittest
from datetime import datetime, timezone

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS


class ClientHQAccountabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org_hq")
        self.client = self.os.create_organization_workspace(self.org.id, "Client", "client", "ws_hq")
        self.other = self.os.create_organization_workspace(self.org.id, "Other", "client", "ws_other_hq")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner", person_id="person_hq")
        self.dri = self.os.create_person(self.org.id, "DRI", person_id="person_hq_dri")
        self.backup = self.os.create_person(self.org.id, "Backup", person_id="person_hq_backup")
        for person in (self.owner, self.dri, self.backup):
            self.os.add_person_to_workspace(self.org.id, self.client.id, person.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.other.id, self.owner.id, "admin")
        self.identity = AuthenticatedIdentity(
            "principal_hq", self.org.id, self.owner.id, "session",
            frozenset({"workspace_read", "workspace_write", "people_manage", "workflow_run"}),
            workspace_id=self.client.id,
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_empty_roster_is_explicit_and_summary_is_present(self) -> None:
        view = self.os.dashboard.client_hq(self.identity, self.org.id, self.client.id, self.owner.id)
        self.assertIsNone(view["current_roster"])
        self.assertIsNone(view["account_team"]["dri"])
        self.assertEqual(view["summary"]["open_risks"], 0)
        self.assertEqual(view["summary"]["unanswered_important_messages"], 0)
        self.assertIn("workflow_board", view)
        self.assertEqual(view["readiness"], view["workflow_board"])

    def test_roster_slots_and_meeting_default_then_override_are_projected(self) -> None:
        roster = self.os.client_ops.create_client_roster(
            self.org.id, self.client.id, self.owner.id,
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "default_meeting_facilitator", "person_id": self.dri.id},
                {"role_key": "default_meeting_note_taker", "person_id": self.backup.id},
            ], datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        meeting = self.os.client_ops.create_meeting(
            self.org.id, self.client.id, self.owner.id, "Weekly",
            datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
        view = self.os.dashboard.client_hq(self.identity, self.org.id, self.client.id, self.owner.id)
        self.assertEqual(view["current_roster"]["id"], roster["id"])
        self.assertEqual(view["account_team"]["dri"]["id"], self.dri.id)
        self.assertEqual(view["account_team"]["backup"]["id"], self.backup.id)
        row = view["meeting_responsibilities"][0]
        self.assertEqual(row["meeting_id"], meeting.id)
        self.assertEqual(row["facilitator_person_id"], self.dri.id)
        self.os.client_ops.set_meeting_responsibilities(
            self.org.id, self.client.id, self.owner.id, meeting.id,
            facilitator_person_id=self.backup.id,
        )
        updated = self.os.dashboard.client_hq(self.identity, self.org.id, self.client.id, self.owner.id)
        self.assertEqual(updated["meeting_responsibilities"][0]["facilitator_person_id"], self.backup.id)
        self.assertEqual(updated["meeting_responsibilities"][0]["source"]["facilitator"], "explicit")

    def test_cross_workspace_and_forged_identity_are_denied_without_disclosure(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.dashboard.client_hq(self.identity, self.org.id, self.other.id, self.owner.id)
        forged = AuthenticatedIdentity(
            "forged", self.org.id, "someone_else", "session", frozenset({"workspace_read"}),
            workspace_id=self.client.id,
        )
        with self.assertRaises(AuthorizationError):
            self.os.dashboard.client_hq(forged, self.org.id, self.client.id, self.owner.id)

    def test_roster_does_not_create_actions_for_business_viewer(self) -> None:
        viewer = AuthenticatedIdentity(
            "business", self.org.id, self.owner.id, "session", frozenset({"workspace_read"}),
            workspace_id=self.client.id,
        )
        view = self.os.dashboard.client_hq(viewer, self.org.id, self.client.id, self.owner.id)
        self.assertFalse(any(item.get("allowed_actions") for item in view["workflow_board"]["runs"]))
        self.assertEqual(view["account_team"]["dri"], None)
        self.assertEqual(view["finance"], {"status": "not_authorized"})
        self.assertEqual(view["brain"], {"status": "not_authorized"})

    def test_overdue_summary_uses_the_latest_work_deadline(self) -> None:
        item = self.os.work_ops.create(
            self.org.id, self.client.id, self.owner.id, "Deadline", "Track", "Client",
            deadline="2099-01-01",
        )
        self.os.work_ops.update(
            self.org.id, self.client.id, self.owner.id, item.id,
            {"deadline": "2000-01-01"},
        )
        view = self.os.dashboard.client_hq(self.identity, self.org.id, self.client.id, self.owner.id)
        self.assertEqual(view["summary"]["overdue_work"], 1)


if __name__ == "__main__":
    unittest.main()
